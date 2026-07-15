"""Creation, validation, binding, and cleanup for classification receipts."""

from __future__ import annotations

import copy
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from engram.canonicalize import canonicalize, content_hash
from engram.classification import ClassificationResult
from engram.models import ClassificationRun, MemoryItem

CANONICALIZATION_VERSION = "canonical-v1"
CLASSIFICATION_OUTPUT_VERSION = "classification-v2"
RETENTION_POLICY_VERSION = "retention-v1"
UNBOUND_RECEIPT_TTL = timedelta(hours=1)
CONTEXT_REDACTION_MARKER = "[context redacted]"


class ClassificationRunBindingError(RuntimeError):
    """The receipt is already consumed or is in an invalid binding state."""


def _redact_context(value: Any, context: str | None) -> Any:
    """Return a deep, context-redacted copy of a durable value."""
    if isinstance(value, str):
        if context:
            return value.replace(context, CONTEXT_REDACTION_MARKER)
        return value
    if isinstance(value, dict):
        return {str(key): _redact_context(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_context(item, context) for item in value]
    if isinstance(value, tuple):
        return [_redact_context(item, context) for item in value]
    return copy.deepcopy(value)


def durable_provenance(
    result: ClassificationResult, *, context: str | None
) -> dict[str, Any]:
    """Build allowlisted, normalized provenance safe for durable storage."""
    source = result.provenance
    provenance: dict[str, Any] = {
        "provider": str(source.get("provider", "none")),
        "mode": str(source.get("mode", "unknown")),
        "suggested_taxonomy": {
            "kind": result.suggested_kind,
            "wing": result.suggested_wing,
            "room": result.suggested_room,
        },
        "suggested_visibility": result.suggested_visibility,
        "taxonomy_confidence": result.taxonomy_confidence,
        "retention_confidence": result.retention_confidence,
        "retention_disposition": result.retention_disposition,
        "rules_matched": list(result.rules_matched),
        "classification_version": CLASSIFICATION_OUTPUT_VERSION,
        "retention_policy_version": RETENTION_POLICY_VERSION,
    }
    if source.get("model") is not None:
        provenance["model"] = str(source["model"])
    if source.get("threshold") is not None:
        provenance["taxonomy_threshold"] = source["threshold"]
    if source.get("error_type") is not None:
        provenance["provider_error"] = {"type": str(source["error_type"])}
    return cast(dict[str, Any], _redact_context(provenance, context))


def hash_context(context: str | None) -> tuple[str | None, int | None]:
    """Hash exact submitted context without retaining its raw contents."""
    if not context:
        return None, None
    return hashlib.sha256(context.encode("utf-8")).hexdigest(), len(context)


def hash_content(content: str) -> str:
    return content_hash(canonicalize(content))


def new_run(
    *,
    tenant_id: UUID,
    principal_id: UUID,
    content: str,
    source_type: str,
    workspace_id: UUID | None,
    context: str | None,
    result: ClassificationResult,
    ingest_id: UUID | None = None,
    now: datetime | None = None,
) -> ClassificationRun:
    created_at = now or datetime.now(UTC)
    context_hash, context_length = hash_context(context)
    persisted_reason = _redact_context(result.reason, context)
    persisted_provenance = durable_provenance(result, context=context)
    return ClassificationRun(
        tenant_id=tenant_id,
        principal_id=principal_id,
        ingest_id=ingest_id,
        content_hash=hash_content(content),
        canonicalization_version=CANONICALIZATION_VERSION,
        source_type=source_type,
        workspace_id=workspace_id,
        context_hash=context_hash,
        context_length=context_length,
        suggested_kind=result.suggested_kind,
        suggested_wing=result.suggested_wing,
        suggested_room=result.suggested_room,
        suggested_visibility=result.suggested_visibility,
        taxonomy_confidence=result.taxonomy_confidence,
        retention_confidence=result.retention_confidence,
        retention_disposition=result.retention_disposition,
        reason=persisted_reason,
        provenance=persisted_provenance,
        classification_version=CLASSIFICATION_OUTPUT_VERSION,
        retention_policy_version=RETENTION_POLICY_VERSION,
        created_at=created_at,
        expires_at=created_at + UNBOUND_RECEIPT_TTL,
    )


async def lock_run(session: AsyncSession, run_id: UUID) -> ClassificationRun | None:
    return (
        await session.execute(
            select(ClassificationRun)
            .where(ClassificationRun.id == run_id)
            .with_for_update()
        )
    ).scalar_one_or_none()


async def bound_run_for_item(
    session: AsyncSession, item_id: UUID, *, for_update: bool = False
) -> ClassificationRun | None:
    stmt = select(ClassificationRun).where(ClassificationRun.memory_item_id == item_id)
    if for_update:
        # The caller is asking for authoritative persisted evidence, not a
        # possibly pre-flush identity-map snapshot. This matters for REAL
        # confidence columns: PostgreSQL stores them at single precision, so
        # an unrefreshed Python value (for example 0.9) can differ from the
        # value copied into the bound memory item after a database round trip.
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalar_one_or_none()


def bind_run(
    run: ClassificationRun, item: MemoryItem, *, bound_at: datetime | None = None
) -> datetime:
    """Consume a receipt once and attach its retention evidence atomically."""
    if run.bound_at is not None or run.memory_item_id is not None:
        raise ClassificationRunBindingError("classification run is already bound")
    binding_time = bound_at or datetime.now(UTC)
    run.memory_item_id = item.id
    run.bound_at = binding_time
    item.retention_confidence = run.retention_confidence
    item.retention_disposition = run.retention_disposition
    item.retention_evidence_at = run.created_at
    return binding_time


async def cleanup_expired_unbound_runs(
    session: AsyncSession, tenant_id: UUID, *, limit: int = 1000
) -> int:
    ids = (
        await session.execute(
            select(ClassificationRun.id)
            .where(
                ClassificationRun.tenant_id == tenant_id,
                ClassificationRun.bound_at.is_(None),
                ClassificationRun.expires_at < datetime.now(UTC),
            )
            .order_by(ClassificationRun.expires_at)
            .limit(min(limit, 1000))
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()
    if ids:
        await session.execute(
            delete(ClassificationRun).where(
                ClassificationRun.id.in_(ids),
                ClassificationRun.bound_at.is_(None),
            )
        )
    return len(ids)
