"""Startup Context Receipt dark-write orchestrator (ENG-CONTEXT-002B).

Wires the canonical ``ContextManifestV1`` (ENG-CONTEXT-001) and the durable
``context_receipts`` storage substrate (ENG-CONTEXT-002A) into the production
startup-recall path as a **default-off, fail-open** dark write.

When explicitly enabled, a successful startup recall additionally:

1. parses and validates every required executed-result provenance field from
   the raw startup engine result (no inference, no defaults);
2. builds ``ContextManifestV1`` from the *finalized* ``RecallResponse`` and
   the *actual* resolved execution context (no re-reads of mutable memory
   rows);
3. persists one immutable ``context_receipts`` row on a dedicated,
   short-lived, non-owner app-role session;
4. reloads the stored JSONB through PostgreSQL;
5. recanonicalizes and verifies the reloaded manifest and hashes;
6. commits the receipt **only after** verification succeeds;
7. leaves the original ``RecallResponse`` unchanged.

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
from collections.abc import Mapping
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
    "StartupReceiptExecutedContext",
    "parse_startup_executed_context",
    "write_startup_context_receipt_best_effort",
]

ContextReceiptDarkWriteStatus = Literal[
    "disabled",
    "created",
    "idempotent",
    "failed",
    "timed_out",
]

# Bounded telemetry-status vocabulary recorded in the structured log and the
# usage-event metadata. ``skipped_deadline`` marks a hard timeout that left no
# time even for the bounded usage-event attempt (the structured log is still
# authoritative for that attempt).
TelemetryStatus = Literal["recorded", "disabled", "failed", "timed_out", "skipped_deadline"]

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

# Required executed-result keys. The receipt must attest exactly what the
# startup engine attested; absence is never inferred to a default.
_REQUIRED_EXECUTED_KEYS: tuple[str, ...] = (
    "recall_log_id",
    "workspace_id",
    "scoring_version",
    "config_version",
    "candidate_strategy_version",
    "effective_byte_budget",
    "effective_token_budget",
    "effective_item_budget",
)


class StartupDecisionContextError(Exception):
    """The executed recall result is missing or malformed a required internal
    decision-provenance key.

    Raised by :func:`parse_startup_executed_context` when a key the receipt
    must attest (recall-log identity, resolved workspace, scoring/config/
    candidate-strategy versions, effective budgets) is absent or wrongly
    typed. This is evidence-generation code: a missing field must NOT be
    inferred to a default — it produces a bounded dark-write failure
    (``failure_stage=build_decision_context``) and no receipt. The
    best-effort wrapper translates this into a bounded failure event
    (exception type only, never the message).
    """


def _require_key(raw_result: Mapping[str, Any], key: str) -> Any:
    """Return ``raw_result[key]``, raising if the key is absent.

    Evidence code must never infer an executed value from a missing key — a
    missing internal value means the engine did not attest it, and the receipt
    must fail open rather than record an assumed value. ``dict.get`` would
    mask absence as ``None``; only an explicit ``None`` value is a valid
    nullable attestation.
    """
    if key not in raw_result:
        raise StartupDecisionContextError(
            f"startup recall result is missing required decision key {key!r}"
        )
    return raw_result[key]


def _require_canonical_uuid_str(value: Any, *, key: str) -> UUID:
    """Require a canonical UUID string or UUID object, returning a UUID.

    Rejects ``None``, whitespace-padded values, and noncanonical string
    representations (uppercase hex, missing hyphens, braces). A noncanonical
    representation attests something the engine never produced (the engine
    emits ``str(UUID(...))`` which is canonical lowercase hyphenated), so it
    is evidence-contract failure rather than a parse convenience.
    """
    if value is None:
        raise StartupDecisionContextError(
            f"startup recall result key {key!r} must not be null"
        )
    if isinstance(value, UUID):
        # A UUID object is always canonical when re-rendered; accept it.
        return value
    if not isinstance(value, str) or isinstance(value, bool):
        raise StartupDecisionContextError(
            f"startup recall result key {key!r} must be a canonical UUID string"
        )
    stripped = value.strip()
    if not stripped or stripped != value:
        raise StartupDecisionContextError(
            f"startup recall result key {key!r} must be a canonical UUID "
            "(whitespace-padded values are rejected)"
        )
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise StartupDecisionContextError(
            f"startup recall result key {key!r} is not a valid UUID"
        ) from exc
    # Reject noncanonical string representations (uppercase hex, missing
    # hyphens, braces, urn: prefixes): the engine always emits canonical
    # lowercase ``str(UUID(...))``, so a noncanonical form is evidence the
    # value did not come from the canonical path.
    if str(parsed) != value:
        raise StartupDecisionContextError(
            f"startup recall result key {key!r} must be a canonical UUID "
            "(noncanonical representation rejected)"
        )
    return parsed


def _require_optional_canonical_uuid(value: Any, *, key: str) -> UUID | None:
    """Require an explicit ``None`` or a canonical UUID.

    ``None`` is a valid attestation (principal-scoped recall has no
    workspace). An empty string is NOT null — it is a malformed attestation.
    A missing key is rejected upstream by :func:`_require_key`.
    """
    if value is None:
        return None
    if isinstance(value, str) and value == "":
        raise StartupDecisionContextError(
            f"startup recall result key {key!r} is an empty string, not null"
        )
    return _require_canonical_uuid_str(value, key=key)


def _require_version_str(value: Any, *, key: str) -> str:
    """Require a nonempty, non-blank string version attestation.

    Rejects Boolean and non-string values (a numeric ``1`` is not a version)
    and blank/whitespace-only strings. Never defaults to ``"v1"`` — that
    default belongs only to the public ``RecallResponse`` compatibility
    surface, never to receipt evidence.
    """
    if not isinstance(value, str) or isinstance(value, bool):
        raise StartupDecisionContextError(
            f"startup recall result key {key!r} must be a nonempty string"
        )
    if not value.strip():
        raise StartupDecisionContextError(
            f"startup recall result key {key!r} must not be blank"
        )
    return value


def _require_optional_nonneg_int(value: Any, *, key: str) -> int | None:
    """Require ``None`` or a nonnegative integer budget.

    Rejects Boolean (a budget must not be a flag) and negative values.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise StartupDecisionContextError(
            f"startup recall result key {key!r} must be an int or None"
        )
    if value < 0:
        raise StartupDecisionContextError(
            f"startup recall result key {key!r} must be nonnegative"
        )
    return value


def _require_effective_item_budget_null(value: Any) -> None:
    """Startup v1 never enforces an item budget; the engine must attest None."""
    if value is not None:
        raise StartupDecisionContextError(
            "startup recall result key 'effective_item_budget' must be None "
            "(startup v1 never enforces an item budget)"
        )


@dataclass(frozen=True)
class StartupReceiptExecutedContext:
    """Immutable, fully-validated provenance parsed from the raw startup
    engine result.

    Returned by :func:`parse_startup_executed_context`. Every field is
    guaranteed to have been explicitly attested by the engine (no inferred
    defaults) and to satisfy the canonical-form / type contract the receipt
    must attest.
    """

    recall_log_id: UUID
    workspace_id: UUID | None
    scoring_version: str
    config_version: str
    candidate_strategy_version: str
    effective_byte_budget: int | None
    effective_token_budget: int | None
    # Startup v1 never enforces an item budget; the parser requires the
    # engine to attest ``None`` and rejects any other value. The literal
    # ``None`` type makes that contract impossible to violate at the type
    # level once parsed.
    effective_item_budget: None


def parse_startup_executed_context(
    raw_result: Mapping[str, Any],
) -> StartupReceiptExecutedContext:
    """Parse and validate every required executed-result provenance field.

    The raw startup result must contain every key in
    :data:`_REQUIRED_EXECUTED_KEYS` with a value that satisfies the
    canonical-form/type contract. ``dict.get`` is never used: a missing key
    is rejected, never inferred to a default. Returns a fully-validated
    :class:`StartupReceiptExecutedContext` or raises
    :class:`StartupDecisionContextError`.

    This is the single production parser used by both the route and every
    test helper, so tests cannot construct evidence in a way production
    forbids.
    """
    recall_log_id = _require_canonical_uuid_str(
        _require_key(raw_result, "recall_log_id"), key="recall_log_id"
    )
    workspace_id = _require_optional_canonical_uuid(
        _require_key(raw_result, "workspace_id"), key="workspace_id"
    )
    scoring_version = _require_version_str(
        _require_key(raw_result, "scoring_version"), key="scoring_version"
    )
    config_version = _require_version_str(
        _require_key(raw_result, "config_version"), key="config_version"
    )
    candidate_strategy_version = _require_version_str(
        _require_key(raw_result, "candidate_strategy_version"),
        key="candidate_strategy_version",
    )
    effective_byte_budget = _require_optional_nonneg_int(
        _require_key(raw_result, "effective_byte_budget"),
        key="effective_byte_budget",
    )
    effective_token_budget = _require_optional_nonneg_int(
        _require_key(raw_result, "effective_token_budget"),
        key="effective_token_budget",
    )
    _require_effective_item_budget_null(
        _require_key(raw_result, "effective_item_budget")
    )
    return StartupReceiptExecutedContext(
        recall_log_id=recall_log_id,
        workspace_id=workspace_id,
        scoring_version=scoring_version,
        config_version=config_version,
        candidate_strategy_version=candidate_strategy_version,
        effective_byte_budget=effective_byte_budget,
        effective_token_budget=effective_token_budget,
        effective_item_budget=None,
    )


@dataclass(frozen=True)
class StartupReceiptDecisionContext:
    """Immutable decision context for one startup receipt.

    Constructed by :func:`write_startup_context_receipt_best_effort` from the
    original ``RecallRequest`` and the validated executed provenance.
    ``workspace_supplied`` means the caller supplied a workspace field
    (``req.workspace is not None``); the slug is never stored. Effective
    budgets are the exact values the engine attested (parsed from the raw
    result, never defaulted). Startup v1 never enforces an item budget, so
    ``effective_item_budget`` is always ``None``.
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


# ─── Internal result + bounded failure type ─────────────────────────────


@dataclass(frozen=True)
class ContextReceiptDarkWriteResult:
    """Internal observability result of one dark-write attempt.

    Never enters ``RecallResponse``. ``receipt_id`` is set only on
    ``created``/``idempotent``. ``failure_stage``/``exception_type`` are set
    only on ``failed``/``timed_out`` and carry only bounded vocabulary / the
    exception *type* (never the message). ``verification_status`` and
    ``telemetry_status`` carry the bounded observability vocabulary used by
    the standardized structured log and the usage-event metadata.
    """

    status: ContextReceiptDarkWriteStatus
    latency_ms: int
    receipt_id: UUID | None = None
    failure_stage: str | None = None
    exception_type: str | None = None
    verification_status: str | None = None
    telemetry_status: TelemetryStatus | None = None


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


def _verification_status_for(
    status: ContextReceiptDarkWriteStatus,
) -> str | None:
    if status in ("created", "idempotent"):
        return "passed"
    if status == "timed_out":
        return None
    return "failed"


# ─── Raising implementation (testable) ──────────────────────────────────


async def _write_startup_context_receipt_once(
    *,
    response: RecallResponseLike,
    recall_log_id: UUID,
    memory_context: ResolvedMemoryContext,
    decision_context: StartupReceiptDecisionContext,
    deadline: float,
) -> ContextReceiptDarkWriteResult:
    """Execute one enabled dark-write attempt end to end under a shared
    monotonic ``deadline``.

    Raises on any failure (manifest construction, DB, integrity). The
    best-effort wrapper (:func:`write_startup_context_receipt_best_effort`)
    translates these into ``failed``/``timed_out`` results. ``deadline`` is a
    ``time.monotonic()`` absolute cutoff; each awaited stage checks the
    remaining budget before running and translates a missed deadline into
    ``_DarkWriteFailure(stage=timeout)``.
    """
    start = time.monotonic()

    def _remaining() -> float:
        return deadline - time.monotonic()

    # 1. Build manifest from the finalized response (no DB work).
    try:
        manifest = _build_manifest(
            response=response,
            memory_context=memory_context,
            decision_context=decision_context,
        )
    except Exception as exc:
        raise _DarkWriteFailure(
            stage=_FAILURE_STAGE_BUILD_MANIFEST, latency_ms=_elapsed_ms(start)
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
        raise _DarkWriteFailure(
            stage=_FAILURE_STAGE_OPEN_SESSION, latency_ms=_elapsed_ms(start)
        ) from exc

    async with session_cm as session:
        # 2a. Apply tenant/principal RLS.
        try:
            await apply_rls_context(
                session, tenant_id=tenant_id, principal_id=principal_id
            )
        except Exception as exc:
            raise _DarkWriteFailure(
                stage=_FAILURE_STAGE_APPLY_RLS, latency_ms=_elapsed_ms(start)
            ) from exc

        # 3. Store (idempotent insert; loads parent recall log + validates
        #    overlap). Does not commit. Bounded by the remaining deadline so
        #    one slow DB stage cannot consume the entire configured timeout.
        remaining = _remaining()
        if remaining <= 0:
            raise _DarkWriteFailure(
                stage=_FAILURE_STAGE_TIMEOUT, latency_ms=_elapsed_ms(start)
            )
        try:
            stored_result = await asyncio.wait_for(
                store_context_receipt(
                    session,
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    recall_log_id=recall_log_id,
                    manifest=manifest,
                ),
                timeout=remaining,
            )
        except TimeoutError as exc:
            raise _DarkWriteFailure(
                stage=_FAILURE_STAGE_TIMEOUT, latency_ms=_elapsed_ms(start)
            ) from exc
        except Exception as exc:
            raise _DarkWriteFailure(
                stage=_FAILURE_STAGE_STORE, latency_ms=_elapsed_ms(start)
            ) from exc

        # 4. Flush so the row reflects the INSERT.
        try:
            await session.flush()
        except Exception as exc:
            raise _DarkWriteFailure(
                stage=_FAILURE_STAGE_STORE, latency_ms=_elapsed_ms(start)
            ) from exc

        # 5. Force a database reload of the receipt (all envelope + JSONB
        #    fields from PostgreSQL).
        remaining = _remaining()
        if remaining <= 0:
            raise _DarkWriteFailure(
                stage=_FAILURE_STAGE_TIMEOUT, latency_ms=_elapsed_ms(start)
            )
        try:
            await asyncio.wait_for(
                session.refresh(stored_result.receipt), timeout=remaining
            )
        except TimeoutError as exc:
            raise _DarkWriteFailure(
                stage=_FAILURE_STAGE_TIMEOUT, latency_ms=_elapsed_ms(start)
            ) from exc
        except Exception as exc:
            raise _DarkWriteFailure(
                stage=_FAILURE_STAGE_RELOAD, latency_ms=_elapsed_ms(start)
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
            raise _DarkWriteFailure(
                stage=_FAILURE_STAGE_VERIFY, latency_ms=_elapsed_ms(start)
            ) from exc

        # 7. Commit only after verification succeeds.
        remaining = _remaining()
        if remaining <= 0:
            raise _DarkWriteFailure(
                stage=_FAILURE_STAGE_TIMEOUT, latency_ms=_elapsed_ms(start)
            )
        try:
            await asyncio.wait_for(session.commit(), timeout=remaining)
        except TimeoutError as exc:
            raise _DarkWriteFailure(
                stage=_FAILURE_STAGE_TIMEOUT, latency_ms=_elapsed_ms(start)
            ) from exc
        except Exception as exc:
            raise _DarkWriteFailure(
                stage=_FAILURE_STAGE_COMMIT, latency_ms=_elapsed_ms(start)
            ) from exc

    latency = _elapsed_ms(start)
    status: ContextReceiptDarkWriteStatus = (
        "created" if stored_result.created else "idempotent"
    )
    return ContextReceiptDarkWriteResult(
        status=status,
        latency_ms=latency,
        receipt_id=stored_result.receipt.id,
        verification_status="passed",
    )


# ─── Best-effort wrapper (route boundary) ───────────────────────────────


def _safe_log(
    *,
    status: ContextReceiptDarkWriteStatus,
    latency_ms: int,
    tenant_id: UUID,
    principal_id: UUID,
    recall_log_id: UUID | None,
    receipt_id: UUID | None,
    item_count: int,
    failure_stage: str | None,
    exception_type: str | None,
    verification_status: str | None,
    telemetry_status: TelemetryStatus | None,
) -> None:
    """Emit exactly one bounded structured log per enabled attempt.

    Required fields are always present (``event``, ``status``, ``tenant_id``,
    ``principal_id``, ``mode=startup``, ``latency_ms``, ``item_count``,
    ``failure_stage``, ``exception_type``, ``verification_status``,
    ``telemetry_status``). Fields only known after successful provenance
    parsing (``recall_log_id``, ``receipt_id``) are null until then. No raw
    content, working_set, query text, manifest JSON, canonical JSON, or
    workspace slug is ever logged. ``logger.exception`` is prohibited on this
    fail-open path because exception representations may include bound values.
    """
    logger.info(
        "event=context_receipt_dark_write status=%s tenant_id=%s "
        "principal_id=%s mode=startup latency_ms=%s item_count=%s "
        "failure_stage=%s exception_type=%s verification_status=%s "
        "telemetry_status=%s recall_log_id=%s receipt_id=%s",
        status,
        tenant_id,
        principal_id,
        latency_ms,
        item_count,
        failure_stage,
        exception_type,
        verification_status,
        telemetry_status,
        recall_log_id,
        receipt_id,
    )


async def write_startup_context_receipt_best_effort(
    *,
    response: RecallResponseLike,
    raw_result: Mapping[str, Any],
    memory_context: ResolvedMemoryContext,
    requested_workspace_supplied: bool,
    requested_byte_budget: int | None,
    requested_token_budget: int | None,
    requested_item_budget: int | None,
) -> ContextReceiptDarkWriteResult:
    """Best-effort startup context-receipt dark write.

    This is the single best-effort entrypoint the route calls. It owns the
    complete enabled sequence:

    1. check the feature flag — return ``disabled`` immediately when false
       (no manifest work, no decision-context work, no receipt session, no
       telemetry, no receipt log);
    2. start the total monotonic deadline BEFORE executed-result validation;
    3. parse and validate executed provenance from ``raw_result``
       (``build_decision_context`` failure stage on any missing/malformed
       key);
    4. construct the decision context;
    5. build the manifest;
    6. open the dedicated app-role session;
    7. apply RLS;
    8. store;
    9. reload;
    10. verify;
    11. commit;
    12. construct one :class:`ContextReceiptDarkWriteResult`;
    13. attempt bounded usage telemetry (only when deadline remains);
    14. emit one standardized structured log;
    15. return the result.

    Catches ordinary failures and returns ``failed`` or ``timed_out``; never
    raises an ordinary dark-write failure to the route.

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
            status="disabled",
            latency_ms=0,
        )

    timeout_seconds = settings.context_receipt_dark_write_timeout_seconds
    tenant_id = memory_context.tenant_id
    principal_id = memory_context.principal_id
    item_count = int(getattr(response, "item_count", 0))
    byte_count = int(getattr(response, "byte_count", 0))

    outer_start = time.monotonic()
    deadline = outer_start + timeout_seconds

    # The provenance parse is synchronous but still runs inside the deadline
    # so its elapsed time reduces the remaining budget for the awaited DB
    # stages. A build_decision_context failure is an enabled attempt: it
    # produces one ``failed`` result, one structured log, and one bounded
    # usage-event attempt (when the deadline still has room).
    recall_log_id: UUID | None
    try:
        executed = parse_startup_executed_context(raw_result)
        recall_log_id = executed.recall_log_id
        decision_context = StartupReceiptDecisionContext(
            workspace_supplied=requested_workspace_supplied,
            requested_byte_budget=requested_byte_budget,
            requested_token_budget=requested_token_budget,
            requested_item_budget=requested_item_budget,
            effective_workspace_id=executed.workspace_id,
            effective_byte_budget=executed.effective_byte_budget,
            effective_token_budget=executed.effective_token_budget,
            effective_item_budget=None,
            scoring_version=executed.scoring_version,
            config_version=executed.config_version,
            candidate_strategy_version=executed.candidate_strategy_version,
        )
    except StartupDecisionContextError as exc:
        latency = _elapsed_ms(outer_start)
        result = ContextReceiptDarkWriteResult(
            status="failed",
            latency_ms=latency,
            failure_stage=_FAILURE_STAGE_BUILD_DECISION_CONTEXT,
            exception_type=type(exc).__name__,
            verification_status="failed",
        )
        recall_log_id = None
        # Fall through to the unified telemetry + log + return path below.
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — fail-open boundary
        latency = _elapsed_ms(outer_start)
        result = ContextReceiptDarkWriteResult(
            status="failed",
            latency_ms=latency,
            failure_stage=_FAILURE_STAGE_BUILD_DECISION_CONTEXT,
            exception_type=type(exc).__name__,
            verification_status="failed",
        )
        recall_log_id = None
    else:
        # Provenance parsed cleanly — run the DB-backed attempt under the
        # shared deadline.
        try:
            result = await _write_startup_context_receipt_once(
                response=response,
                recall_log_id=recall_log_id,
                memory_context=memory_context,
                decision_context=decision_context,
                deadline=deadline,
            )
        except _DarkWriteFailure as exc:
            if exc.stage == _FAILURE_STAGE_TIMEOUT:
                status: ContextReceiptDarkWriteStatus = "timed_out"
            else:
                status = "failed"
            result = ContextReceiptDarkWriteResult(
                status=status,
                latency_ms=exc.latency_ms,
                failure_stage=exc.stage,
                exception_type=(
                    type(exc.__cause__).__name__ if exc.__cause__ else None
                ),
                verification_status=_verification_status_for(status),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — fail-open boundary
            latency = _elapsed_ms(outer_start)
            result = ContextReceiptDarkWriteResult(
                status="failed",
                latency_ms=latency,
                failure_stage=_FAILURE_STAGE_UNEXPECTED,
                exception_type=type(exc).__name__,
                verification_status="failed",
            )

    # Bounded usage telemetry. Attempted for every enabled attempt where
    # sufficient deadline remains (including decision-context failures),
    # because the structured log alone is authoritative but the usage-event
    # row is the queryable long-term record. The call opens its own
    # dedicated DB session and may await a commit; with usage telemetry
    # enabled (the recommended dogfood config), a stalled telemetry
    # connection could otherwise hold the recall request beyond the
    # configured receipt timeout. Bound it with the REMAINING deadline so
    # the total dark-write time never exceeds the configured timeout. The
    # already-determined receipt result is preserved regardless of the
    # telemetry outcome.
    elapsed = time.monotonic() - outer_start
    remaining = timeout_seconds - elapsed
    telemetry_status: TelemetryStatus
    if remaining <= 0:
        # Primary operation consumed the entire deadline: emit the
        # standardized structured log (still authoritative), record
        # ``skipped_deadline``, and return promptly. Do NOT extend the total
        # request time merely to write telemetry.
        telemetry_status = "skipped_deadline"
    else:
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
                    verification_status=result.verification_status,
                ),
                timeout=remaining,
            )
            telemetry_status = "recorded"
        except TimeoutError:
            telemetry_status = "timed_out"
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — telemetry must never raise here
            telemetry_status = "failed"

    final_result = ContextReceiptDarkWriteResult(
        status=result.status,
        latency_ms=result.latency_ms,
        receipt_id=result.receipt_id,
        failure_stage=result.failure_stage,
        exception_type=result.exception_type,
        verification_status=result.verification_status,
        telemetry_status=telemetry_status,
    )

    _safe_log(
        status=final_result.status,
        latency_ms=final_result.latency_ms,
        tenant_id=tenant_id,
        principal_id=principal_id,
        recall_log_id=recall_log_id,
        receipt_id=final_result.receipt_id,
        item_count=item_count,
        failure_stage=final_result.failure_stage,
        exception_type=final_result.exception_type,
        verification_status=final_result.verification_status,
        telemetry_status=final_result.telemetry_status,
    )

    return final_result
