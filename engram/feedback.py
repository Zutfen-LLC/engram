"""Canonical feedback transitions and their atomic memory-item effects."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from engram.memory_context import ResolvedMemoryContext, context_provenance
from engram.models import FeedbackEvent, ItemEvent, MemoryItem, Principal, RecallLog, TenantConfig

FeedbackVerdict = Literal["useful", "noise"]
FeedbackStatus = Literal["recorded", "updated", "unchanged"]
DEFAULT_FEEDBACK_DAILY_LIMIT = 500


@dataclass(frozen=True)
class FeedbackEffect:
    importance_delta: float
    reset_startup_recall_count: bool


@dataclass(frozen=True)
class FeedbackResult:
    status: FeedbackStatus
    item_id: uuid.UUID
    feedback: FeedbackVerdict
    previous_feedback: FeedbackVerdict | None
    feedback_event_id: uuid.UUID
    importance: float
    startup_recall_count: int


class RecallLogNotFoundError(Exception):
    """The supplied recall log is absent or belongs to another caller."""


class RecallLogItemMismatchError(Exception):
    """The caller owns the log, but it cannot prove this item was surfaced."""


class FeedbackRateLimitError(Exception):
    def __init__(self, *, limit: int, reset_at: datetime) -> None:
        self.limit = limit
        self.reset_at = reset_at
        super().__init__("Daily feedback limit exceeded")


def effect_for_feedback(
    *, principal_type: str, is_item_author: bool, verdict: FeedbackVerdict
) -> FeedbackEffect:
    """Return the trusted contribution for one canonical verdict."""
    if principal_type in {"user", "admin"}:
        return FeedbackEffect(0.05 if verdict == "useful" else -0.10, verdict == "useful")
    if is_item_author:
        return FeedbackEffect(0.0, False)
    return FeedbackEffect(0.025 if verdict == "useful" else -0.05, False)


def current_feedback_predicate() -> Any:
    """Shared definition of a current canonical feedback row."""
    return FeedbackEvent.superseded_at.is_(None)


async def _validate_recall_log(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    item_id: uuid.UUID,
    recall_log_id: uuid.UUID,
) -> None:
    log = (
        await session.execute(
            select(RecallLog).where(
                RecallLog.id == recall_log_id,
                RecallLog.tenant_id == tenant_id,
                RecallLog.principal_id == principal_id,
            )
        )
    ).scalar_one_or_none()
    if log is None:
        raise RecallLogNotFoundError
    if log.item_ids is None or item_id not in log.item_ids:
        raise RecallLogItemMismatchError


async def record_feedback(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    principal_type: str,
    item: Mapping[str, Any],
    verdict: FeedbackVerdict,
    recall_log_id: uuid.UUID | None,
    now: datetime | None = None,
    memory_context: ResolvedMemoryContext | None = None,
) -> FeedbackResult:
    """Apply one serializable canonical transition; caller has already locked the item.

    The global lock order is memory item, then authenticated principal. This
    serializes same-item canonicalization and per-principal daily accounting.
    """
    item_id = uuid.UUID(str(item["id"]))
    if recall_log_id is not None:
        await _validate_recall_log(
            session,
            tenant_id=tenant_id,
            principal_id=principal_id,
            item_id=item_id,
            recall_log_id=recall_log_id,
        )

    current = (
        await session.execute(
            select(FeedbackEvent).where(
                FeedbackEvent.tenant_id == tenant_id,
                FeedbackEvent.item_id == item_id,
                FeedbackEvent.principal_id == principal_id,
                current_feedback_predicate(),
            )
        )
    ).scalar_one_or_none()
    if current is not None and current.verdict == verdict:
        return FeedbackResult(
            status="unchanged",
            item_id=item_id,
            feedback=verdict,
            previous_feedback=verdict,
            feedback_event_id=current.id,
            importance=float(item["importance"] if item["importance"] is not None else 0.5),
            startup_recall_count=int(item["startup_recall_count"] or 0),
        )

    # Principal-row serialization makes count + insert safe across all workers and keys.
    locked_principal = (
        await session.execute(
            select(Principal.id)
            .where(Principal.id == principal_id, Principal.tenant_id == tenant_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if locked_principal is None:
        raise RuntimeError("authenticated principal no longer exists")
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    day_start = timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
    reset_at = day_start + timedelta(days=1)
    limit = (
        await session.execute(
            select(TenantConfig.feedback_daily_limit).where(
                TenantConfig.tenant_id == tenant_id, TenantConfig.active.is_(True)
            )
        )
    ).scalar_one_or_none()
    effective_limit = limit if limit is not None else DEFAULT_FEEDBACK_DAILY_LIMIT
    accepted = (
        await session.execute(
            select(func.count(FeedbackEvent.id)).where(
                FeedbackEvent.tenant_id == tenant_id,
                FeedbackEvent.principal_id == principal_id,
                FeedbackEvent.created_at >= day_start,
                FeedbackEvent.created_at < reset_at,
            )
        )
    ).scalar_one()
    if accepted >= effective_limit:
        raise FeedbackRateLimitError(limit=effective_limit, reset_at=reset_at)

    is_author = uuid.UUID(str(item["principal_id"])) == principal_id
    new_effect = effect_for_feedback(
        principal_type=principal_type, is_item_author=is_author, verdict=verdict
    )
    previous = cast(FeedbackVerdict | None, current.verdict if current is not None else None)
    old_delta = (
        effect_for_feedback(
            principal_type=principal_type, is_item_author=is_author, verdict=previous
        ).importance_delta
        if previous is not None
        else 0.0
    )
    applied_delta = new_effect.importance_delta - old_delta
    replacement_time = timestamp
    if current is not None:
        current.superseded_at = replacement_time
        # Make the old row non-current before inserting its replacement. This
        # avoids relying on SQLAlchemy's cross-operation ordering against the
        # partial unique index while retaining one atomic transaction.
        await session.flush()

    event = FeedbackEvent(
        tenant_id=tenant_id,
        item_id=item_id,
        principal_id=principal_id,
        verdict=verdict,
        recall_log_id=recall_log_id,
        replaces_feedback_event_id=current.id if current is not None else None,
        created_at=timestamp,
    )
    session.add(event)
    values: dict[str, Any] = {
        "importance": func.least(
            func.greatest(func.coalesce(MemoryItem.importance, 0.5) + applied_delta, 0.1), 0.95
        )
    }
    if new_effect.reset_startup_recall_count:
        values["startup_recall_count"] = 0
    row = (
        await session.execute(
            update(MemoryItem)
            .where(MemoryItem.id == item_id, MemoryItem.tenant_id == tenant_id)
            .values(**values)
            .returning(MemoryItem.importance, MemoryItem.startup_recall_count)
        )
    ).one()
    await session.flush()
    if memory_context is not None:
        session.add(
            ItemEvent(
                item_id=item_id,
                **context_provenance(memory_context),
                event_type="feedback",
                field_name="feedback",
                old_value=previous,
                new_value=verdict,
                actor_principal_id=principal_id,
                reason=f"feedback_event_id={event.id}",
                created_at=timestamp,
            )
        )
    await session.commit()
    return FeedbackResult(
        status="updated" if current is not None else "recorded",
        item_id=item_id,
        feedback=verdict,
        previous_feedback=previous,
        feedback_event_id=event.id,
        importance=float(row.importance),
        startup_recall_count=int(row.startup_recall_count),
    )
