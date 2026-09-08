"""Fail-open dark writer for authoritative semantic Context Receipts.

The caller supplies the finalized semantic evaluation. This module does not
run retrieval, V2 resolution, assessment selection, relationship expansion,
packing, or an embedding provider.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import Awaitable, Mapping
from typing import Any, Literal, TypeVar, cast
from uuid import UUID

from engram.context_manifest import canonical_json_bytes, compute_manifest_hash
from engram.context_receipt_dark_write import (
    ContextReceiptDarkWriteResult,
    ContextReceiptDarkWriteStatus,
    TelemetryStatus,
    _await_before_deadline,
)
from engram.context_receipts import (
    ContextReceiptIntegrityError,
    ContextReceiptStoreResult,
    store_context_receipt,
    verify_context_receipt_record,
)
from engram.db import apply_rls_context, async_session_factory
from engram.memory_context import ResolvedMemoryContext
from engram.recall_profiles import RECALL_PROFILE_CONTRACT_VERSION
from engram.semantic_context_manifest import (
    SemanticContextManifestV1,
    SemanticManifestDecisionContextV1,
    build_semantic_context_manifest_v1,
)

logger = logging.getLogger("engram.semantic_context_receipt_dark_write")

__all__ = ["write_semantic_context_receipt_best_effort"]

_STAGE_BUILD_DECISION_CONTEXT = "build_decision_context"
_STAGE_BUILD_MANIFEST = "build_manifest"
_STAGE_OPEN_SESSION = "open_session"
_STAGE_APPLY_RLS = "apply_rls"
_STAGE_STORE = "store"
_STAGE_RELOAD = "reload"
_STAGE_VERIFY = "verify"
_STAGE_COMMIT = "commit"
_STAGE_TIMEOUT = "timeout"
_STAGE_UNEXPECTED = "unexpected"

_T = TypeVar("_T")


class SemanticDecisionContextError(ValueError):
    """Finalized semantic provenance is missing or malformed."""


class _SemanticDarkWriteFailure(Exception):
    def __init__(self, *, stage: str, latency_ms: int) -> None:
        self.stage = stage
        self.latency_ms = latency_ms
        super().__init__("semantic context receipt dark write failed")


def _elapsed_ms(start: float) -> int:
    return max(0, round((time.monotonic() - start) * 1000))


def _required(raw_result: Mapping[str, Any], key: str) -> Any:
    if key not in raw_result:
        raise SemanticDecisionContextError(f"missing semantic decision field {key!r}")
    return raw_result[key]


def _uuid(value: Any, *, key: str, nullable: bool = False) -> UUID | None:
    if value is None and nullable:
        return None
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise SemanticDecisionContextError(f"{key} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise SemanticDecisionContextError(f"{key} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise SemanticDecisionContextError(f"{key} must be a canonical UUID")
    return parsed


def _optional_budget(value: Any, *, key: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SemanticDecisionContextError(f"{key} must be a nonnegative integer or null")
    return value


def _nonnegative_count(value: Any, *, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SemanticDecisionContextError(f"{key} must be a nonnegative integer")
    return value


def _version(value: Any, *, key: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SemanticDecisionContextError(f"{key} must be a nonempty string")
    return value


def _required_version(value: Any, *, key: str) -> str:
    parsed = _version(value, key=key)
    assert parsed is not None
    return parsed


def _build_context(
    *,
    raw_result: Mapping[str, Any],
    memory_context: ResolvedMemoryContext,
    workspace_supplied: bool,
    requested_byte_budget: int | None,
    requested_token_budget: int | None,
    requested_item_budget: int | None,
) -> tuple[UUID, SemanticManifestDecisionContextV1, Any]:
    evaluation = _required(raw_result, "_semantic_evaluation")
    recall_log_id = _uuid(_required(raw_result, "recall_log_id"), key="recall_log_id")
    assert recall_log_id is not None
    workspace_id = _uuid(_required(raw_result, "workspace_id"), key="workspace_id", nullable=True)
    profile = evaluation.profile
    profile_id = memory_context.memory_profile_id
    revision_id = memory_context.memory_profile_revision_id
    context = SemanticManifestDecisionContextV1(
        tenant_id=str(memory_context.tenant_id),
        principal_id=str(memory_context.principal_id),
        workspace_id=str(workspace_id) if workspace_id is not None else None,
        memory_profile_id=str(profile_id) if profile_id is not None else None,
        memory_profile_revision_id=str(revision_id) if revision_id is not None else None,
        memory_profile_version=memory_context.memory_profile_version,
        memory_context_version=cast(
            "Literal['memory-context-v2']", memory_context.version
        ),
        workspace_supplied=workspace_supplied,
        requested_byte_budget=requested_byte_budget,
        requested_token_budget=requested_token_budget,
        requested_item_budget=requested_item_budget,
        effective_byte_budget=_optional_budget(
            _required(raw_result, "effective_byte_budget"), key="effective_byte_budget"
        ),
        effective_token_budget=_optional_budget(
            _required(raw_result, "effective_token_budget"), key="effective_token_budget"
        ),
        effective_item_budget=_optional_budget(
            _required(raw_result, "effective_item_budget"), key="effective_item_budget"
        ),
        recall_profile=profile.key,
        recall_profile_contract_version=RECALL_PROFILE_CONTRACT_VERSION,
        scoring_version=_required_version(
            _required(raw_result, "scoring_version"), key="scoring_version"
        ),
        signals_version=_version(
            _required(raw_result, "signals_version"), key="signals_version", nullable=True
        ),
        admission_policy="recall-admission-v2" if profile.signals_enabled else None,
        relationship_relevance_version=(
            evaluation.expansion.get("version")
            if evaluation.expansion is not None
            else None
        ),
        packing_version=(
            evaluation.packing.get("version") if evaluation.packing is not None else None
        ),
        config_version=_required_version(
            _required(raw_result, "config_version"), key="config_version"
        ),
    )
    return recall_log_id, context, evaluation


async def _bounded(awaitable: Awaitable[_T], *, deadline: float, stage: str, start: float) -> _T:
    try:
        return await _await_before_deadline(awaitable, deadline=deadline)
    except TimeoutError as exc:
        raise _SemanticDarkWriteFailure(
            stage=_STAGE_TIMEOUT, latency_ms=_elapsed_ms(start)
        ) from exc
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise _SemanticDarkWriteFailure(stage=stage, latency_ms=_elapsed_ms(start)) from exc


def _verify_reloaded(
    *,
    stored: ContextReceiptStoreResult,
    original: SemanticContextManifestV1,
    recall_log_id: UUID,
    memory_context: ResolvedMemoryContext,
) -> None:
    receipt = stored.receipt
    if (
        receipt.recall_log_id != recall_log_id
        or receipt.tenant_id != memory_context.tenant_id
        or receipt.principal_id != memory_context.principal_id
    ):
        raise ContextReceiptIntegrityError("semantic receipt envelope identity mismatch")
    verified = verify_context_receipt_record(receipt)
    if not isinstance(verified, SemanticContextManifestV1):
        raise ContextReceiptIntegrityError("semantic receipt parsed as another family")
    if compute_manifest_hash(verified) != receipt.manifest_hash:
        raise ContextReceiptIntegrityError("semantic manifest hash mismatch")
    original_bytes = canonical_json_bytes(
        original.model_dump(mode="json", by_alias=True, exclude_none=False)
    )
    verified_bytes = canonical_json_bytes(
        verified.model_dump(mode="json", by_alias=True, exclude_none=False)
    )
    if original_bytes != verified_bytes:
        raise ContextReceiptIntegrityError("semantic manifest canonical bytes mismatch")


async def _write_once(
    *,
    manifest: SemanticContextManifestV1,
    recall_log_id: UUID,
    memory_context: ResolvedMemoryContext,
    deadline: float,
    start: float,
) -> ContextReceiptDarkWriteResult:
    try:
        session_cm = async_session_factory()
    except Exception as exc:
        raise _SemanticDarkWriteFailure(
            stage=_STAGE_OPEN_SESSION, latency_ms=_elapsed_ms(start)
        ) from exc
    entered = False
    try:
        session = await _bounded(
            session_cm.__aenter__(),
            deadline=deadline,
            stage=_STAGE_OPEN_SESSION,
            start=start,
        )
        entered = True
        await _bounded(
            apply_rls_context(
                session,
                tenant_id=memory_context.tenant_id,
                principal_id=memory_context.principal_id,
            ),
            deadline=deadline,
            stage=_STAGE_APPLY_RLS,
            start=start,
        )
        stored = await _bounded(
            store_context_receipt(
                session,
                tenant_id=memory_context.tenant_id,
                principal_id=memory_context.principal_id,
                recall_log_id=recall_log_id,
                manifest=manifest,
            ),
            deadline=deadline,
            stage=_STAGE_STORE,
            start=start,
        )
        await _bounded(session.flush(), deadline=deadline, stage=_STAGE_STORE, start=start)
        await _bounded(
            session.refresh(stored.receipt),
            deadline=deadline,
            stage=_STAGE_RELOAD,
            start=start,
        )
        try:
            _verify_reloaded(
                stored=stored,
                original=manifest,
                recall_log_id=recall_log_id,
                memory_context=memory_context,
            )
        except Exception as exc:
            raise _SemanticDarkWriteFailure(
                stage=_STAGE_VERIFY, latency_ms=_elapsed_ms(start)
            ) from exc
        await _bounded(session.commit(), deadline=deadline, stage=_STAGE_COMMIT, start=start)
        status: ContextReceiptDarkWriteStatus = "created" if stored.created else "idempotent"
        return ContextReceiptDarkWriteResult(
            status=status,
            latency_ms=_elapsed_ms(start),
            receipt_id=stored.receipt.id,
            verification_status="passed",
        )
    finally:
        if entered:
            exc_info = sys.exc_info()
            try:
                await _await_before_deadline(session_cm.__aexit__(*exc_info), deadline=deadline)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - cleanup stays fail-open
                pass


def _safe_log(
    *,
    result: ContextReceiptDarkWriteResult,
    memory_context: ResolvedMemoryContext,
    recall_log_id: UUID | None,
    item_count: int,
    byte_count: int,
) -> None:
    logger.info(
        "event=context_receipt_dark_write status=%s tenant_id=%s principal_id=%s "
        "mode=semantic latency_ms=%s item_count=%s byte_count=%s failure_stage=%s "
        "exception_type=%s verification_status=%s telemetry_status=%s "
        "recall_log_id=%s receipt_id=%s",
        result.status,
        memory_context.tenant_id,
        memory_context.principal_id,
        result.latency_ms,
        item_count,
        byte_count,
        result.failure_stage,
        result.exception_type,
        result.verification_status,
        result.telemetry_status,
        recall_log_id,
        result.receipt_id,
    )


async def write_semantic_context_receipt_best_effort(
    *,
    raw_result: Mapping[str, Any],
    memory_context: ResolvedMemoryContext,
    query: str,
    workspace_supplied: bool,
    requested_byte_budget: int | None,
    requested_token_budget: int | None,
    requested_item_budget: int | None,
) -> ContextReceiptDarkWriteResult:
    """Write one semantic receipt within one total deadline.

    Ordinary failures return a bounded result. Cancellation propagates.
    """
    from engram.config import settings
    from engram.usage import record_context_receipt_dark_write

    if not settings.semantic_context_receipt_dark_write_enabled:
        return ContextReceiptDarkWriteResult(status="disabled", latency_ms=0)

    item_count = 0
    byte_count = 0
    start = time.monotonic()
    deadline = start + settings.context_receipt_dark_write_timeout_seconds
    recall_log_id: UUID | None = None
    try:
        item_count = _nonnegative_count(
            _required(raw_result, "item_count"), key="item_count"
        )
        byte_count = _nonnegative_count(
            _required(raw_result, "byte_count"), key="byte_count"
        )
        recall_log_id, context, evaluation = _build_context(
            raw_result=raw_result,
            memory_context=memory_context,
            workspace_supplied=workspace_supplied,
            requested_byte_budget=requested_byte_budget,
            requested_token_budget=requested_token_budget,
            requested_item_budget=requested_item_budget,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail-open boundary
        result = ContextReceiptDarkWriteResult(
            status="failed",
            latency_ms=_elapsed_ms(start),
            failure_stage=_STAGE_BUILD_DECISION_CONTEXT,
            exception_type=type(exc).__name__,
            verification_status="failed",
        )
    else:
        try:
            manifest = build_semantic_context_manifest_v1(
                items=evaluation.items,
                working_set=evaluation.working_set,
                item_count=evaluation.item_count,
                byte_count=evaluation.byte_count,
                candidate_count=evaluation.candidate_count,
                omitted_count=_nonnegative_count(
                    _required(raw_result, "omitted_count"), key="omitted_count"
                ),
                omitted_by_admission=evaluation.omitted_by_admission,
                expansion=evaluation.expansion,
                packing=evaluation.packing,
                message=_required(raw_result, "message"),
                query=query,
                context=context,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail-open boundary
            result = ContextReceiptDarkWriteResult(
                status="failed",
                latency_ms=_elapsed_ms(start),
                failure_stage=_STAGE_BUILD_MANIFEST,
                exception_type=type(exc).__name__,
                verification_status="failed",
            )
        else:
            try:
                result = await _write_once(
                    manifest=manifest,
                    recall_log_id=recall_log_id,
                    memory_context=memory_context,
                    deadline=deadline,
                    start=start,
                )
            except _SemanticDarkWriteFailure as exc:
                status: ContextReceiptDarkWriteStatus = (
                    "timed_out" if exc.stage == _STAGE_TIMEOUT else "failed"
                )
                result = ContextReceiptDarkWriteResult(
                    status=status,
                    latency_ms=exc.latency_ms,
                    failure_stage=exc.stage,
                    exception_type=(type(exc.__cause__).__name__ if exc.__cause__ else None),
                    verification_status=None if status == "timed_out" else "failed",
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - fail-open boundary
                result = ContextReceiptDarkWriteResult(
                    status="failed",
                    latency_ms=_elapsed_ms(start),
                    failure_stage=_STAGE_UNEXPECTED,
                    exception_type=type(exc).__name__,
                    verification_status="failed",
                )

    remaining = deadline - time.monotonic()
    telemetry_status: TelemetryStatus
    if remaining <= 0:
        telemetry_status = "skipped_deadline"
    elif not settings.usage_telemetry_enabled:
        telemetry_status = "disabled"
    else:
        try:
            event_id = await asyncio.wait_for(
                record_context_receipt_dark_write(
                    tenant_id=memory_context.tenant_id,
                    principal_id=memory_context.principal_id,
                    status=result.status,
                    item_count=item_count,
                    byte_count=byte_count,
                    latency_ms=result.latency_ms,
                    mode="semantic",
                    failure_stage=result.failure_stage,
                    exception_type=result.exception_type,
                    verification_status=result.verification_status,
                ),
                timeout=remaining,
            )
            telemetry_status = "recorded" if event_id is not None else "failed"
        except TimeoutError:
            telemetry_status = "timed_out"
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - telemetry stays fail-open
            telemetry_status = "failed"

    final = ContextReceiptDarkWriteResult(
        status=result.status,
        latency_ms=result.latency_ms,
        receipt_id=result.receipt_id,
        failure_stage=result.failure_stage,
        exception_type=result.exception_type,
        verification_status=result.verification_status,
        telemetry_status=telemetry_status,
    )
    _safe_log(
        result=final,
        memory_context=memory_context,
        recall_log_id=recall_log_id,
        item_count=item_count,
        byte_count=byte_count,
    )
    return final
