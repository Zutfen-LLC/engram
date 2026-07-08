"""Auto-promotion Path A for proposed memories.

Design.md §3 — an item promotes when:
  - ``review_status = 'proposed'``
  - ``memory_confidence >= auto_promote_confidence_threshold`` (default 0.7)
  - ``created_at`` is older than ``auto_promote_min_age_hours`` (default 72h)
  - no unresolved conflict (``conflict_resolution_status IS NULL OR = 'accepted'``)
  - tenant_config.auto_promote_enabled is true

The service reads thresholds from the active ``tenant_config`` row — never
hardcoded constants — with fallbacks that match the schema defaults so a
misconfigured/missing tenant_config still promotes conservatively.

This slice implements Path A only (age + confidence + no conflict). Path B
(quorum-based, ``feedback_events``) is intentionally out of scope.

Audit: each promotion writes an ``item_events`` row with
``event_type='review_change'``, ``field_name='review_status'``,
``old_value='proposed'``, ``new_value='active'`` and a reason that names
auto-promotion — mirroring the manual ``POST /items/{id}/review`` path.

Invocation: a thin CLI command (``engram promote-proposed``) loops every
tenant, and a thin admin endpoint (``POST /v1/admin/promote``) handles the
caller's tenant. Neither adds a scheduler — deployment runs the CLI on cron
or systemd.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from engram.models import ItemEvent, MemoryItem, TenantConfig

# Fallbacks match the schema defaults in migrations/001_init.sql. Used only
# when a tenant has no active tenant_config row — the normal path reads live
# values from tenant_config.
_FALLBACK_CONFIDENCE_THRESHOLD = 0.7
_FALLBACK_MIN_AGE_HOURS = 72


@dataclass
class PromotionResult:
    """Summary of one auto-promotion invocation for a single tenant."""

    tenant_id: str
    enabled: bool
    confidence_threshold: float
    min_age_hours: int
    scanned: int = 0
    promoted: int = 0
    skipped_confidence: int = 0
    skipped_age: int = 0
    skipped_conflict: int = 0
    skipped_disabled: int = 0
    promoted_ids: list[uuid.UUID] = field(default_factory=list)


def summarize(result: PromotionResult) -> str:
    """Human-readable single-tenant summary for CLI output / endpoint response."""
    if not result.enabled:
        return (
            f"tenant={result.tenant_id} auto-promotion disabled; "
            f"scanned={result.scanned} promoted=0"
        )
    return (
        f"tenant={result.tenant_id} "
        f"threshold={result.confidence_threshold} "
        f"min_age_hours={result.min_age_hours} "
        f"scanned={result.scanned} promoted={result.promoted} "
        f"skipped_confidence={result.skipped_confidence} "
        f"skipped_age={result.skipped_age} "
        f"skipped_conflict={result.skipped_conflict} "
        f"skipped_disabled={result.skipped_disabled}"
    )


def _resolve_thresholds(config: TenantConfig | None) -> tuple[float, int]:
    """Read (confidence_threshold, min_age_hours) from tenant_config or fallback."""
    if config is not None:
        return config.auto_promote_confidence_threshold, config.auto_promote_min_age_hours
    return _FALLBACK_CONFIDENCE_THRESHOLD, _FALLBACK_MIN_AGE_HOURS


async def _fetch_config_for_tenant(
    session: AsyncSession, tenant_id: str
) -> TenantConfig | None:
    result = await session.execute(
        select(TenantConfig).where(
            TenantConfig.tenant_id == tenant_id,
            TenantConfig.active.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def auto_promote_proposed_memories(
    session: AsyncSession,
    tenant_id: str | None = None,
    *,
    now: datetime | None = None,
    limit: int | None = None,
) -> PromotionResult:
    """Promote eligible proposed memories for a tenant (Path A).

    When ``tenant_id`` is None the tenant is read from the session's RLS
    context (``app.tenant_id``) the same way the route layer does.

    Returns a :class:`PromotionResult` with per-reason skip counts.

    The function commits on success so callers (CLI / endpoint) don't need to
    manage transaction boundaries. It is idempotent: a second run re-scans the
    same set but finds nothing left to promote (those rows are now ``active``).
    """
    effective_now = now or datetime.now(UTC)

    if tenant_id is None:
        # Resolve tenant from RLS session context (mirrors _resolve_tenant_id
        # in the route layer, but treats missing context as empty rather than
        # raising since this is also a pure service function).
        tid = (
            await session.execute(text("SELECT current_setting('app.tenant_id', true)::text"))
        ).scalar_one_or_none()
        if not tid:
            return PromotionResult(
                tenant_id="",
                enabled=False,
                confidence_threshold=_FALLBACK_CONFIDENCE_THRESHOLD,
                min_age_hours=_FALLBACK_MIN_AGE_HOURS,
                scanned=0,
                promoted=0,
                skipped_disabled=0,
            )
        tenant_id = tid

    config = await _fetch_config_for_tenant(session, tenant_id)
    threshold, min_age_hours = _resolve_thresholds(config)
    enabled = bool(config.auto_promote_enabled) if config is not None else True

    result = PromotionResult(
        tenant_id=tenant_id,
        enabled=enabled,
        confidence_threshold=threshold,
        min_age_hours=min_age_hours,
    )

    # Count candidates even when disabled so the summary stays informative.
    if not enabled:
        count_stmt = (
            select(MemoryItem.id)
            .where(
                MemoryItem.tenant_id == tenant_id,
                MemoryItem.review_status == "proposed",
                MemoryItem.valid_to.is_(None),
            )
        )
        disabled_rows = (await session.execute(count_stmt)).all()
        result.scanned = len(disabled_rows)
        result.skipped_disabled = len(disabled_rows)
        await session.commit()
        return result

    age_cutoff = effective_now - timedelta(hours=min_age_hours)

    # Fetch candidates; filter confidence/age/conflict in Python so each skip
    # reason is recorded distinctly.
    stmt = (
        select(MemoryItem)
        .where(
            MemoryItem.tenant_id == tenant_id,
            MemoryItem.review_status == "proposed",
            MemoryItem.valid_to.is_(None),
        )
        .order_by(MemoryItem.created_at.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)

    candidates = list((await session.execute(stmt)).scalars().all())
    result.scanned = len(candidates)

    to_promote: list[MemoryItem] = []
    for item in candidates:
        # Exclude superseded/terminal states defensively (valid_to IS NULL can
        # coexist with superseded_by transiently during supersession writes).
        if item.superseded_by is not None:
            continue
        if item.memory_confidence < threshold:
            result.skipped_confidence += 1
            continue
        if item.created_at > age_cutoff:
            result.skipped_age += 1
            continue
        # No unresolved conflict: status must be NULL or 'accepted'.
        if item.conflict_resolution_status == "unresolved":
            result.skipped_conflict += 1
            continue
        to_promote.append(item)

    if to_promote:
        promote_ids = [item.id for item in to_promote]
        await session.execute(
            update(MemoryItem)
            .where(MemoryItem.id.in_(promote_ids))
            .values(review_status="active")
        )
        reason = (
            "auto-promotion Path A: memory_confidence >= "
            f"{threshold} and age >= {min_age_hours}h with no unresolved conflict"
        )
        for item in to_promote:
            session.add(
                ItemEvent(
                    item_id=item.id,
                    event_type="review_change",
                    field_name="review_status",
                    old_value="proposed",
                    new_value="active",
                    actor_principal_id=item.principal_id,
                    reason=reason,
                )
            )
        result.promoted = len(to_promote)
        result.promoted_ids = promote_ids

    await session.commit()
    return result
