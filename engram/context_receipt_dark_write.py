"""Startup Context Receipt dark-write orchestrator (ENG-CONTEXT-002B).

Wires the canonical ``ContextManifestV1`` (ENG-CONTEXT-001) and the durable
``context_receipts`` storage substrate (ENG-CONTEXT-002A) into the production
startup-recall path as a **default-off, fail-open** dark write.

When explicitly enabled, a successful startup recall additionally:

1. builds ``ContextManifestV1`` from the *finalized* ``RecallResponse`` and
   the *actual* resolved execution context (no re-reads of mutable memory
   rows);
2. persists one immutable ``context_receipts`` row on a dedicated,
   short-lived, non-owner app-role session;
3. reloads the stored JSONB through PostgreSQL;
4. recanonicalizes and verifies the reloaded manifest and hashes;
5. commits the receipt **only after** verification succeeds;
6. leaves the original ``RecallResponse`` unchanged.

A receipt failure must NEVER fail the recall request, modify the response,
delete/roll back the already-committed recall log, poison the caller's
request session, suppress retrieval-success telemetry, or expose raw
content/working_set/exception messages in logs or metrics.

This module has no FastAPI dependencies and no route models. It is a pure
orchestration layer over :mod:`engram.context_manifest`,
:mod:`engram.context_receipts`, :mod:`engram.db`, and
:mod:`engram.usage`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal, cast
from uuid import UUID

from engram.context_manifest import (
    MANIFEST_CONTRACT_VERSION,
    PACKET_RENDER_VERSION,
    ContextManifestEffectiveV1,
    ContextManifestRequestedV1,
    ContextManifestRequestInputV1,
    ContextManifestSubjectV1,
    ContextManifestV1,
    ContextManifestVersionsV1,
    RecallResponseLike,
    build_startup_context_manifest_v1,
    canonical_json_bytes,
    compute_manifest_hash,
)
from engram.context_receipts import (
    ContextReceiptIntegrityError,
    ContextReceiptStoreResult,
    store_context_receipt,
    verify_context_receipt_record,
)
from engram.db import apply_rls_context, async_session_factory
from engram.memory_context import ResolvedMemoryContext

logger = logging.getLogger("engram.context_receipt_dark_write")

__all__ = [
    "ContextReceiptDarkWriteResult",
    "ContextReceiptDarkWriteStatus",
    "StartupDecisionContextError",
    "StartupReceiptDecisionContext",
    "build_startup_decision_context_from_result",
    "write_startup_context_receipt_best_effort",
]

ContextReceiptDarkWriteStatus = Literal[
    "disabled",
    "created",
    "idempotent",
    "failed",
    "timed_out",
]

# Bounded internal failure-stage vocabulary. Never derived from raw exception
# messages.
_FAILURE_STAGE_BUILD_DECISION_CONTEXT = "build_decision_context"
_FAILURE_STAGE_BUILD_MANIFEST = "build_manifest"
_FAILURE_STAGE_OPEN_SESSION = "open_session"
_FAILURE_STAGE_APPLY_RLS = "apply_rls"
_FAILURE_STAGE_STORE = "store"
_FAILURE_STAGE_RELOAD = "reload"
_FAILURE_STAGE_VERIFY = "verify"
_FAILURE_STAGE_COMMIT = "commit"
_FAILURE_STAGE_TIMEOUT = "timeout"
_FAILURE_STAGE_UNEXPECTED = "unexpected"


@dataclass(frozen=True)
class StartupReceiptDecisionContext:
    """Immutable decision context for one startup receipt.

    Constructed by the route from the original ``RecallRequest`` and the
    actual startup engine result. ``workspace_supplied`` means the caller
    supplied a workspace field (``req.workspace is not None``); the slug is
    never stored. Effective budgets are the exact values used by budget
    enforcement and written to ``RecallLog``. Startup v1 never enforces an
    item budget, so ``effective_item_budget`` is always ``None``.
    """

    workspace_supplied: bool

    requested_byte_budget: int | None
    requested_token_budget: int | None
    requested_item_budget: int | None

    effective_workspace_id: UUID | None
    effective_byte_budget: int | None
    effective_token_budget: int | None
    effective_item_budget: None

    scoring_version: str
    config_version: str
    candidate_strategy_version: str


class StartupDecisionContextError(Exception):
    """The executed recall result is missing or malformed a required internal
    decision-provenance key.

    Raised by :func:`build_startup_decision_context_from_result` when a key
    the receipt must attest (candidate strategy version, effective budgets) is
    absent or wrongly typed. This is evidence-generation code: a missing field
    must NOT be inferred to a default — it produces a bounded dark-write
    failure and no receipt. The route's defense-in-depth guard translates this
    into a bounded failure event (exception type only, never the message).
    """


def _require_key(result: dict[str, Any], key: str) -> Any:
    """Return ``result[key]``, raising if the key is absent.

    Evidence code must never infer an executed value from a missing key — a
    missing internal value means the engine did not attest it, and the receipt
    must fail open rather than record an assumed value.
    """
    if key not in result:
        raise StartupDecisionContextError(
            f"startup recall result is missing required decision key {key!r}"
        )
    return result[key]


def _require_int_or_none(value: Any, *, key: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise StartupDecisionContextError(
            f"startup recall result key {key!r} must be an int or None"
        )
    return value


def build_startup_decision_context_from_result(
    *,
    req_workspace_supplied: bool,
    req_byte_budget: int | None,
    req_token_budget: int | None,
    req_item_budget: int | None,
    result: dict[str, Any],
    scoring_version: str,
    config_version: str,
) -> StartupReceiptDecisionContext:
    """Build a :class:`StartupReceiptDecisionContext` from the caller's exact
    request and the engine's executed result, **requiring** the internal
    decision keys rather than inferring defaults.

    Required engine keys (all validated for presence and type):

      - ``candidate_strategy_version`` (str) — the exact candidate-selection
        strategy the engine used. Never defaulted.
      - ``effective_byte_budget`` (int | None) — the resolved byte budget the
        engine used and wrote to RecallLog.
      - ``effective_token_budget`` (int | None) — the resolved token budget.
      - ``effective_item_budget`` (must be None) — startup v1 never enforces an
        item budget; the engine must explicitly attest ``None``.

    A missing or malformed key raises :class:`StartupDecisionContextError`.
    The route's defense-in-depth guard catches it, logs the exception type,
    and produces a bounded dark-write failure — **no receipt is created** with
    an inferred value.

    ``workspace_id`` is derived from ``result["workspace_id"]`` (the resolved
    authorized workspace reference, or None for principal-scoped recall).
    """
    candidate_strategy_version = _require_key(result, "candidate_strategy_version")
    if not isinstance(candidate_strategy_version, str) or isinstance(
        candidate_strategy_version, bool
    ):
        raise StartupDecisionContextError(
            "startup recall result key 'candidate_strategy_version' must be a str"
        )

    effective_byte = _require_int_or_none(
        _require_key(result, "effective_byte_budget"), key="effective_byte_budget"
    )
    effective_token = _require_int_or_none(
        _require_key(result, "effective_token_budget"), key="effective_token_budget"
    )

    effective_item = _require_key(result, "effective_item_budget")
    if effective_item is not None:
        raise StartupDecisionContextError(
            "startup recall result key 'effective_item_budget' must be None "
            "(startup v1 never enforces an item budget)"
        )

    workspace_id_raw = result.get("workspace_id")
    effective_workspace_id: UUID | None
    if workspace_id_raw is None or workspace_id_raw == "":
        effective_workspace_id = None
    else:
        try:
            effective_workspace_id = UUID(str(workspace_id_raw))
        except (ValueError, AttributeError, TypeError) as exc:
            raise StartupDecisionContextError(
                "startup recall result key 'workspace_id' is not a valid UUID"
            ) from exc

    return StartupReceiptDecisionContext(
        workspace_supplied=req_workspace_supplied,
        requested_byte_budget=req_byte_budget,
        requested_token_budget=req_token_budget,
        requested_item_budget=req_item_budget,
        effective_workspace_id=effective_workspace_id,
        effective_byte_budget=effective_byte,
        effective_token_budget=effective_token,
        effective_item_budget=None,
        scoring_version=scoring_version,
        config_version=config_version,
        candidate_strategy_version=candidate_strategy_version,
    )


@dataclass(frozen=True)
class ContextReceiptDarkWriteResult:
    """Internal observability result of one dark-write attempt.

    Never enters ``RecallResponse``. ``receipt_id`` is set only on
    ``created``/``idempotent``. ``failure_stage``/``exception_type`` are set
    only on ``failed``/``timed_out`` and carry only bounded vocabulary / the
    exception *type* (never the message).
    """

    status: ContextReceiptDarkWriteStatus
    latency_ms: int
    receipt_id: UUID | None = None
    failure_stage: str | None = None
    exception_type: str | None = None


# ─── Manifest construction ──────────────────────────────────────────────


def _build_subject(
    *,
    memory_context: ResolvedMemoryContext,
    decision_context: StartupReceiptDecisionContext,
) -> ContextManifestSubjectV1:
    return ContextManifestSubjectV1(
        tenant_id=str(memory_context.tenant_id),
        principal_id=str(memory_context.principal_id),
        workspace_id=(
            str(decision_context.effective_workspace_id)
            if decision_context.effective_workspace_id is not None
            else None
        ),
        # ResolvedMemoryContext.version is the string constant
        # MEMORY_CONTEXT_VERSION ("memory-context-v2"); cast to the Literal
        # the manifest contract requires.
        memory_context_version=cast(
            "Literal['memory-context-v2']", memory_context.version
        ),
        memory_profile_id=(
            str(memory_context.memory_profile_id)
            if memory_context.memory_profile_id is not None
            else None
        ),
        memory_profile_revision_id=(
            str(memory_context.memory_profile_revision_id)
            if memory_context.memory_profile_revision_id is not None
            else None
        ),
        memory_profile_version=memory_context.memory_profile_version,
    )


def _build_request_input(
    *,
    decision_context: StartupReceiptDecisionContext,
) -> ContextManifestRequestInputV1:
    return ContextManifestRequestInputV1(
        requested=ContextManifestRequestedV1(
            workspace_supplied=decision_context.workspace_supplied,
            byte_budget=decision_context.requested_byte_budget,
            token_budget=decision_context.requested_token_budget,
            item_budget=decision_context.requested_item_budget,
        ),
        effective=ContextManifestEffectiveV1(
            workspace_id=(
                str(decision_context.effective_workspace_id)
                if decision_context.effective_workspace_id is not None
                else None
            ),
            byte_budget=decision_context.effective_byte_budget,
            token_budget=decision_context.effective_token_budget,
            item_budget=None,
        ),
        # Startup query_digest is always null (startup recall has no query).
        query_digest=None,
    )


def _build_versions(
    decision_context: StartupReceiptDecisionContext,
) -> ContextManifestVersionsV1:
    return ContextManifestVersionsV1(
        scoring_version=decision_context.scoring_version,
        config_version=decision_context.config_version,
        candidate_strategy_version=decision_context.candidate_strategy_version,
        manifest_contract_version=MANIFEST_CONTRACT_VERSION,
        packet_render_version=PACKET_RENDER_VERSION,
    )


def _build_manifest(
    *,
    response: RecallResponseLike,
    memory_context: ResolvedMemoryContext,
    decision_context: StartupReceiptDecisionContext,
) -> ContextManifestV1:
    subject = _build_subject(
        memory_context=memory_context, decision_context=decision_context
    )
    request_context = _build_request_input(decision_context=decision_context)
    versions = _build_versions(decision_context)
    return build_startup_context_manifest_v1(
        response=response,
        subject_context=subject,
        request_context=request_context,
        decision_versions=versions,
    )


# ─── Verification before commit ─────────────────────────────────────────


def _verify_reloaded_record(
    *,
    reloaded_receipt_id: UUID,
    stored_result: ContextReceiptStoreResult,
    original_manifest: ContextManifestV1,
    recall_log_id: UUID,
    tenant_id: UUID,
    principal_id: UUID,
) -> ContextManifestV1:
    """Verify the PostgreSQL-reloaded receipt before the transaction commits.

    Proves:
      - reloaded receipt ID matches the stored result;
      - recall-log ID matches;
      - tenant and principal match;
      - recomputed manifest hash equals the reloaded envelope hash;
      - packet hash agrees;
      - canonical JSON bytes of the verified reloaded manifest equal the
        canonical JSON bytes of the original built manifest;
      - created/idempotent status is preserved (caller decides from
        ``stored_result.created``).

    Raises :class:`ContextReceiptIntegrityError` on any mismatch.
    """
    if reloaded_receipt_id != stored_result.receipt.id:
        raise ContextReceiptIntegrityError(
            "reloaded receipt id does not match the stored result id"
        )
    if stored_result.receipt.recall_log_id != recall_log_id:
        raise ContextReceiptIntegrityError(
            "reloaded receipt recall_log_id does not match the request"
        )
    if stored_result.receipt.tenant_id != tenant_id:
        raise ContextReceiptIntegrityError(
            "reloaded receipt tenant_id does not match the request"
        )
    if stored_result.receipt.principal_id != principal_id:
        raise ContextReceiptIntegrityError(
            "reloaded receipt principal_id does not match the request"
        )

    verified_manifest = verify_context_receipt_record(stored_result.receipt)

    recomputed_hash = compute_manifest_hash(verified_manifest)
    if recomputed_hash != stored_result.receipt.manifest_hash:
        raise ContextReceiptIntegrityError(
            "recomputed manifest hash does not match reloaded envelope hash"
        )
    if verified_manifest.packet.hash != stored_result.receipt.packet_hash:
        raise ContextReceiptIntegrityError(
            "verified manifest packet hash does not match reloaded packet hash"
        )

    original_bytes = canonical_json_bytes(
        original_manifest.model_dump(mode="json", exclude_none=False, by_alias=True)
    )
    reloaded_bytes = canonical_json_bytes(
        verified_manifest.model_dump(mode="json", exclude_none=False, by_alias=True)
    )
    if original_bytes != reloaded_bytes:
        raise ContextReceiptIntegrityError(
            "canonical JSON of the verified reloaded manifest does not equal "
            "the canonical JSON of the original built manifest"
        )
    return verified_manifest


# ─── Raising implementation (testable) ──────────────────────────────────


async def _write_startup_context_receipt_once(
    *,
    response: RecallResponseLike,
    recall_log_id: UUID,
    memory_context: ResolvedMemoryContext,
    decision_context: StartupReceiptDecisionContext,
) -> ContextReceiptDarkWriteResult:
    """Execute one enabled dark-write attempt end to end.

    Raises on any failure (manifest construction, DB, integrity, timeout).
    The best-effort wrapper (:func:`write_startup_context_receipt_best_
    effort`) translates these into ``failed``/``timed_out`` results.
    """
    start = time.monotonic()

    # 1. Build manifest from the finalized response (no DB work).
    try:
        manifest = _build_manifest(
            response=response,
            memory_context=memory_context,
            decision_context=decision_context,
        )
    except Exception as exc:
        latency = _elapsed_ms(start)
        raise _DarkWriteFailure(
            stage=_FAILURE_STAGE_BUILD_MANIFEST, latency_ms=latency
        ) from exc

    tenant_id = memory_context.tenant_id
    principal_id = memory_context.principal_id

    # 2. Dedicated, short-lived, non-owner app-role session. The caller's
    #    request session is never used: the recall log is already committed,
    #    a receipt DB error must not poison the request session, and rollback
    #    must affect only the optional receipt attempt.
    try:
        session_cm = async_session_factory()
    except Exception as exc:
        latency = _elapsed_ms(start)
        raise _DarkWriteFailure(
            stage=_FAILURE_STAGE_OPEN_SESSION, latency_ms=latency
        ) from exc

    async with session_cm as session:
        # 2a. Apply tenant/principal RLS.
        try:
            await apply_rls_context(
                session, tenant_id=tenant_id, principal_id=principal_id
            )
        except Exception as exc:
            latency = _elapsed_ms(start)
            raise _DarkWriteFailure(
                stage=_FAILURE_STAGE_APPLY_RLS, latency_ms=latency
            ) from exc

        # 3. Store (idempotent insert; loads parent recall log + validates
        #    overlap). Does not commit.
        try:
            stored_result = await store_context_receipt(
                session,
                tenant_id=tenant_id,
                principal_id=principal_id,
                recall_log_id=recall_log_id,
                manifest=manifest,
            )
        except Exception as exc:
            latency = _elapsed_ms(start)
            raise _DarkWriteFailure(
                stage=_FAILURE_STAGE_STORE, latency_ms=latency
            ) from exc

        # 4. Flush so the row reflects the INSERT.
        try:
            await session.flush()
        except Exception as exc:
            latency = _elapsed_ms(start)
            raise _DarkWriteFailure(
                stage=_FAILURE_STAGE_STORE, latency_ms=latency
            ) from exc

        # 5. Force a database reload of the receipt (all envelope + JSONB
        #    fields from PostgreSQL).
        try:
            await session.refresh(stored_result.receipt)
        except Exception as exc:
            latency = _elapsed_ms(start)
            raise _DarkWriteFailure(
                stage=_FAILURE_STAGE_RELOAD, latency_ms=latency
            ) from exc

        # 6. Verify the reloaded record and compare against the original
        #    built manifest before commit.
        try:
            _verify_reloaded_record(
                reloaded_receipt_id=stored_result.receipt.id,
                stored_result=stored_result,
                original_manifest=manifest,
                recall_log_id=recall_log_id,
                tenant_id=tenant_id,
                principal_id=principal_id,
            )
        except Exception as exc:
            latency = _elapsed_ms(start)
            raise _DarkWriteFailure(
                stage=_FAILURE_STAGE_VERIFY, latency_ms=latency
            ) from exc

        # 7. Commit only after verification succeeds.
        try:
            await session.commit()
        except Exception as exc:
            latency = _elapsed_ms(start)
            raise _DarkWriteFailure(
                stage=_FAILURE_STAGE_COMMIT, latency_ms=latency
            ) from exc

    latency = _elapsed_ms(start)
    status: ContextReceiptDarkWriteStatus = (
        "created" if stored_result.created else "idempotent"
    )
    return ContextReceiptDarkWriteResult(
        status=status,
        latency_ms=latency,
        receipt_id=stored_result.receipt.id,
    )


# ─── Best-effort wrapper (route boundary) ───────────────────────────────


class _DarkWriteFailure(Exception):
    """Internal: a fail-open dark-write failure carrying bounded diagnostics.

    Never raised out of the best-effort wrapper. ``stage`` is from the bounded
    vocabulary; the underlying exception (if any) is preserved as ``__cause__``
    so tests can inspect it without the route ever seeing the message.
    """

    def __init__(self, *, stage: str, latency_ms: int) -> None:
        self.stage = stage
        self.latency_ms = latency_ms
        super().__init__(f"context receipt dark write failed at stage {stage!r}")


def _elapsed_ms(start: float) -> int:
    return max(0, round((time.monotonic() - start) * 1000))


def _safe_log(
    *,
    status: ContextReceiptDarkWriteStatus,
    latency_ms: int,
    tenant_id: UUID,
    principal_id: UUID,
    recall_log_id: UUID,
    receipt_id: UUID | None,
    item_count: int,
    failure_stage: str | None,
    exception_type: str | None,
) -> None:
    """Emit one bounded structured log for an enabled attempt.

    Success fields include verification=passed; failure fields carry only the
    bounded failure_stage and exception *type* (never the message). No raw
    content, working_set, query text, manifest JSON, or canonical JSON is
    ever logged. ``logger.exception`` is prohibited on this fail-open path
    because exception representations may include bound values.
    """
    if status in ("created", "idempotent"):
        logger.info(
            "event=context_receipt_dark_write status=%s tenant_id=%s "
            "principal_id=%s recall_log_id=%s receipt_id=%s mode=startup "
            "latency_ms=%s item_count=%s verification=passed",
            status,
            tenant_id,
            principal_id,
            recall_log_id,
            receipt_id,
            latency_ms,
            item_count,
        )
    else:
        logger.warning(
            "event=context_receipt_dark_write status=%s tenant_id=%s "
            "principal_id=%s recall_log_id=%s mode=startup latency_ms=%s "
            "failure_stage=%s exception_type=%s",
            status,
            tenant_id,
            principal_id,
            recall_log_id,
            latency_ms,
            failure_stage,
            exception_type,
        )


async def write_startup_context_receipt_best_effort(
    *,
    response: RecallResponseLike,
    recall_log_id: UUID,
    memory_context: ResolvedMemoryContext,
    decision_context: StartupReceiptDecisionContext,
) -> ContextReceiptDarkWriteResult:
    """Best-effort startup context-receipt dark write.

    Contract:
      - returns ``disabled`` immediately when the feature flag is false and
        performs NO receipt work (no manifest, no DB session, no telemetry);
      - when enabled, builds only startup ``ContextManifestV1`` from the
        finalized response, persists through the ENG-CONTEXT-002A repository
        on a dedicated app-role session, verifies the PostgreSQL-reloaded
        record, and commits only after verification;
      - catches ordinary failures and returns ``failed`` or ``timed_out``;
      - never raises an ordinary dark-write failure to the route.

    asyncio cancellation is NOT swallowed: it propagates normally so the
    request/task is cancelled as expected. Only ordinary ``Exception``
    subclasses are fail-open.
    """
    # Local import to keep the disabled path free of any settings import work
    # and to avoid a module-load cycle in edge cases.
    from engram.config import settings
    from engram.usage import record_context_receipt_dark_write

    if not settings.context_receipt_dark_write_enabled:
        return ContextReceiptDarkWriteResult(
            status="disabled", latency_ms=0
        )

    timeout_seconds = settings.context_receipt_dark_write_timeout_seconds
    tenant_id = memory_context.tenant_id
    principal_id = memory_context.principal_id
    item_count = int(getattr(response, "item_count", 0))
    byte_count = int(getattr(response, "byte_count", 0))

    outer_start = time.monotonic()
    try:
        result = await asyncio.wait_for(
            _write_startup_context_receipt_once(
                response=response,
                recall_log_id=recall_log_id,
                memory_context=memory_context,
                decision_context=decision_context,
            ),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        latency = _elapsed_ms(outer_start)
        result = ContextReceiptDarkWriteResult(
            status="timed_out",
            latency_ms=latency,
            failure_stage=_FAILURE_STAGE_TIMEOUT,
        )
    except _DarkWriteFailure as exc:
        result = ContextReceiptDarkWriteResult(
            status="failed",
            latency_ms=exc.latency_ms,
            failure_stage=exc.stage,
            exception_type=type(exc.__cause__).__name__ if exc.__cause__ else None,
        )
    except asyncio.CancelledError:
        # Cancellation must propagate normally — never swallowed.
        raise
    except Exception as exc:  # noqa: BLE001 — fail-open boundary
        result = ContextReceiptDarkWriteResult(
            status="failed",
            latency_ms=_elapsed_ms(outer_start),
            failure_stage=_FAILURE_STAGE_UNEXPECTED,
            exception_type=type(exc).__name__,
        )

    _safe_log(
        status=result.status,
        latency_ms=result.latency_ms,
        tenant_id=tenant_id,
        principal_id=principal_id,
        recall_log_id=recall_log_id,
        receipt_id=result.receipt_id,
        item_count=item_count,
        failure_stage=result.failure_stage,
        exception_type=result.exception_type,
    )

    # Usage telemetry (bounded aggregate metadata only; respects the global
    # usage_telemetry_enabled flag). Telemetry failure must not alter the
    # dark-write result or the recall response — swallowed inside
    # record_usage_event_best_effort.
    #
    # The telemetry call opens its own dedicated DB session and may await a
    # commit; with usage telemetry enabled (the recommended dogfood config), a
    # stalled telemetry connection could otherwise hold the recall request
    # beyond the configured receipt timeout. Bound it with the REMAINING
    # deadline so the total dark-write time (attempt + telemetry) never
    # exceeds the configured timeout. The already-determined receipt result is
    # preserved regardless of the telemetry outcome.
    verification_status: str | None
    if result.status in ("created", "idempotent"):
        verification_status = "passed"
    elif result.status == "timed_out":
        verification_status = None
    else:
        verification_status = "failed"
    elapsed = time.monotonic() - outer_start
    remaining = timeout_seconds - elapsed
    if remaining > 0:
        try:
            await asyncio.wait_for(
                record_context_receipt_dark_write(
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    status=result.status,
                    item_count=item_count,
                    byte_count=byte_count,
                    latency_ms=result.latency_ms,
                    mode="startup",
                    failure_stage=result.failure_stage,
                    exception_type=result.exception_type,
                    verification_status=verification_status,
                ),
                timeout=remaining,
            )
        except TimeoutError:
            # Telemetry exceeded the remaining budget. Preserve the receipt
            # result; telemetry is best-effort and its absence does not alter
            # the dark-write outcome. A bounded warning is already emitted by
            # the structured log above.
            pass
        except Exception:  # noqa: BLE001 — telemetry must never raise here
            pass

    return result