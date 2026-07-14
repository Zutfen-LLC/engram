"""Creation, validation, binding, and cleanup for classification receipts."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
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
    memory_item_id: UUID | None = None,
    now: datetime | None = None,
) -> ClassificationRun:
    created_at = now or datetime.now(UTC)
    context_hash, context_length = hash_context(context)
    return ClassificationRun(
        tenant_id=tenant_id,
        principal_id=principal_id,
        memory_item_id=memory_item_id,
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
        reason=result.reason,
        provenance=result.provenance,
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
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


def bind_run(run: ClassificationRun, item: MemoryItem) -> None:
    run.memory_item_id = item.id
    item.retention_confidence = run.retention_confidence
    item.retention_disposition = run.retention_disposition
    item.retention_evidence_at = run.created_at


async def cleanup_expired_unbound_runs(
    session: AsyncSession, tenant_id: UUID, *, limit: int = 1000
) -> int:
    ids = (
        await session.execute(
            select(ClassificationRun.id)
            .where(
                ClassificationRun.tenant_id == tenant_id,
                ClassificationRun.memory_item_id.is_(None),
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
                ClassificationRun.memory_item_id.is_(None),
            )
        )
    return len(ids)
