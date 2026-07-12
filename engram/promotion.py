"""Auto-promotion Path A for proposed memories.

Design.md §3 — an item promotes when ALL of the following hold:
  - ``review_status = 'proposed'``
  - ``memory_confidence >= auto_promote_confidence_threshold`` (default 0.7)
  - ``created_at`` is older than ``auto_promote_min_age_hours`` (default 72h)
  - no unresolved conflict at write time
    (``conflict_resolution_status IS NULL OR = 'accepted'``)
  - no dispute event in ``item_events``/``feedback_events`` from another
    principal (:func:`has_external_dispute_event`)
  - a promotion-time conflict recheck against live active memories finds
    nothing that blocks promotion (:func:`engram.conflicts.check_promotion_conflict`)
  - ``tenant_config.auto_promote_enabled`` is true

The service reads thresholds from the active ``tenant_config`` row — never
hardcoded constants — with fallbacks that match the schema defaults so a
misconfigured/missing tenant_config still promotes conservatively.

This slice implements Path A only (age + confidence + no conflict + no
dispute + conflict recheck). Path B (quorum-based, ``feedback_events``
"useful" counts) is intentionally out of scope — see design.md §3.

Audit: each promotion writes an ``item_events`` row with
``event_type='review_change'``, ``field_name='review_status'``,
``old_value='proposed'``, ``new_value='active'`` and a reason that names
auto-promotion, the invocation ``source``, and which gates were checked —
mirroring the manual ``POST /items/{id}/review`` path. Items blocked by the
promotion-time conflict recheck get a ``conflict_resolution`` event and are
marked ``conflict_resolution_status='unresolved'`` so a later scan doesn't
silently re-attempt (and re-log) the same recheck.

Serialization (P0-FIX-004A): auto-promotion and the caller-facing review
endpoint (``POST /v1/items/{item_id}/review``) both mutate the same item's
``review_status``. Promotion obtains mutation authority over each candidate
with ``SELECT ... FOR UPDATE SKIP LOCKED`` before evaluating any gate that can
lead to a mutation. A row locked by a concurrent review is skipped this sweep
(not promoted, not counted, reconsidered later if still eligible) rather than
blocking the whole batch. The final ``proposed -> active`` (and conflict-marker)
update is guarded against the expected state (proposed / live / not superseded)
with ``RETURNING``; a zero-row guarded update is a skip or concurrent-state
outcome, never permission to write an event. The state transition and its event
share one transaction, and an event is written only for a row the guarded
update actually transitioned. This allows either operation to win the lock while
preserving a valid serial history: a review that commits first is observed by
promotion (which then skips without overwriting it); a promotion that commits
first leaves the review to re-evaluate from the committed ``active`` state.

Invocation — three entry points, all sharing this one service function so the
gates can never drift apart:
  - lazy, bounded, tenant-scoped: ``maybe_auto_promote_for_startup_recall``,
    called from ``engram.recall.execute_startup_recall`` before active items
    are selected (source=``startup_recall``);
  - a thin CLI command (``engram promote-proposed``, source=``cli``) that
    loops every tenant;
  - a thin admin endpoint (``POST /v1/admin/promote``, source=``admin_endpoint``)
    that handles the caller's tenant.
None of these add a scheduler or job queue — deployment runs the CLI on cron
or systemd for full sweeps; the startup-recall hook keeps day-to-day recall
current between sweeps.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from engram.config import settings
from engram.conflicts import PromotionConflictCheck, check_promotion_conflict
from engram.feedback import current_feedback_predicate
from engram.internal_actors import (
    REVIEW_AUTOMATION_INTERNAL_KEY,
    InternalActorInvariantError,
    resolve_internal_system_actor,
)
from engram.models import FeedbackEvent, ItemEvent, MemoryItem, TenantConfig
from engram.review_policy import TrustedReviewOperation, evaluate_transition

# Fallbacks match the schema defaults in migrations/001_init.sql. Used only
# when a tenant has no active tenant_config row — the normal path reads live
# values from tenant_config.
_FALLBACK_CONFIDENCE_THRESHOLD = 0.7
_FALLBACK_MIN_AGE_HOURS = 72

# --- Trusted internal review actor identity (V2-BL-003B) -------------------
#
# The trusted review actor is identified by a server-owned ``internal_key``
# column on ``principals``, NOT by its mutable display name. This replaces the
# V2-BL-003A name-based resolution (``name = 'system'``) which was vulnerable to
# collision: an agent/user/admin principal named ``system`` would be returned by
# the old ``(tenant_id, name)`` upsert, recreating false self-approval audit
# trails.
#
# The canonical internal key for the trusted review and promotion actor:
TRUSTED_REVIEW_INTERNAL_KEY = REVIEW_AUTOMATION_INTERNAL_KEY
TrustedActorInvariantError = InternalActorInvariantError


async def resolve_trusted_system_actor(session: AsyncSession, tenant_id: str) -> uuid.UUID:
    """Compatibility wrapper for the review-automation actor resolver."""
    return await resolve_internal_system_actor(
        session, tenant_id=tenant_id, internal_key=REVIEW_AUTOMATION_INTERNAL_KEY
    )


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
    # New in ENG-AUD-007.
    skipped_dispute: int = 0
    skipped_conflict_recheck: int = 0
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
        f"skipped_dispute={result.skipped_dispute} "
        f"skipped_conflict_recheck={result.skipped_conflict_recheck} "
        f"skipped_disabled={result.skipped_disabled}"
    )


async def has_external_dispute_event(session: AsyncSession, item: MemoryItem) -> bool:
    """True if a principal other than ``item.principal_id`` has disputed this item.

    A "dispute event" is defined as either of:
      - an ``item_events`` row with ``event_type='review_change'``,
        ``field_name='review_status'``, ``new_value='disputed'``, whose
        ``actor_principal_id`` is not the item's own ``principal_id`` — i.e.
        someone else moved this item's review status to ``disputed`` at some
        point (even if it was later reset back to ``proposed``);
      - a ``feedback_events`` row with ``verdict='noise'`` whose
        ``principal_id`` is not the item's own ``principal_id`` — i.e.
        another principal recalled this item and flagged it as noise.

    The item creator's own uncertainty — self-disputing their own item, or
    giving their own item "noise" feedback — never counts; only an *external*
    signal blocks Path A. Manual/admin review can still promote or reject
    outside Path A regardless of this gate.
    """
    dispute_event = (
        await session.execute(
            select(ItemEvent.id)
            .where(
                ItemEvent.item_id == item.id,
                ItemEvent.event_type == "review_change",
                ItemEvent.field_name == "review_status",
                ItemEvent.new_value == "disputed",
                ItemEvent.actor_principal_id.is_not(None),
                ItemEvent.actor_principal_id != item.principal_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if dispute_event is not None:
        return True

    negative_feedback = (
        await session.execute(
            select(FeedbackEvent.id)
            .where(
                FeedbackEvent.item_id == item.id,
                FeedbackEvent.verdict == "noise",
                current_feedback_predicate(),
                FeedbackEvent.principal_id != item.principal_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return negative_feedback is not None


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
    source: str = "cli",
) -> PromotionResult:
    """Promote eligible proposed memories for a tenant (Path A).

    When ``tenant_id`` is None the tenant is read from the session's RLS
    context (``app.tenant_id``) the same way the route layer does.

    ``source`` identifies the caller for provenance in the promotion event's
    ``reason`` — one of ``"startup_recall"``, ``"cli"``, or ``"admin_endpoint"``.
    It does not change promotion behavior; all three entry points use the
    same gates.

    Returns a :class:`PromotionResult` with per-reason skip counts.

    The function commits on success so callers (CLI / endpoint) don't need to
    manage transaction boundaries. It is idempotent: a second run re-scans the
    same set but finds nothing left to promote (those rows are now ``active``
    or ``conflict_resolution_status='unresolved'``).
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

    # Fetch candidates WITH row locks (P0-FIX-004A). ``FOR UPDATE SKIP LOCKED``
    # is the mutation authority: a row currently locked by a concurrent review
    # is skipped this sweep (reconsidered later if still eligible) rather than
    # blocking the whole batch. The values read here are the locked, current
    # values — the gate evaluation below is revalidation under the lock, not a
    # stale-snapshot check. A skipped locked row is absent from ``scanned`` and
    # from every result counter: this invocation did not examine it.
    dialect_name = session.bind.dialect.name if session.bind is not None else None
    stmt = (
        select(MemoryItem)
        .where(
            MemoryItem.tenant_id == tenant_id,
            MemoryItem.review_status == "proposed",
            MemoryItem.valid_to.is_(None),
        )
        .order_by(MemoryItem.created_at.asc())
    )
    if dialect_name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    if limit is not None:
        stmt = stmt.limit(limit)

    candidates = list((await session.execute(stmt)).scalars().all())
    result.scanned = len(candidates)

    to_promote: list[MemoryItem] = []
    blocked_by_conflict: list[tuple[MemoryItem, PromotionConflictCheck]] = []
    for item in candidates:
        # Revalidation under the row lock — confirm the locked state still
        # satisfies every Path A gate before any mutation decision. A
        # concurrently reviewed row that lost the lock race is already absent
        # from ``candidates`` (SKIP LOCKED); these checks guard the remaining
        # gates against the locked snapshot.
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
        # No unresolved conflict at write time: status must be NULL or
        # 'accepted'.
        if item.conflict_resolution_status == "unresolved":
            result.skipped_conflict += 1
            continue
        # No dispute event from another principal.
        if await has_external_dispute_event(session, item):
            result.skipped_dispute += 1
            continue
        # Promotion-time conflict recheck: don't rely solely on the
        # write-time conflict status, since a later active write can create a
        # conflict this item never saw.
        conflict_check = await check_promotion_conflict(session, item)
        if conflict_check is not None:
            result.skipped_conflict_recheck += 1
            blocked_by_conflict.append((item, conflict_check))
            continue
        to_promote.append(item)

    # Trusted internal events (conflict recheck + promotion below) share one
    # actor per invocation, resolved lazily so a scan with nothing to write
    # never touches the principals table.
    trusted_actor_id: uuid.UUID | None = None
    if blocked_by_conflict or to_promote:
        trusted_actor_id = await resolve_trusted_system_actor(session, tenant_id)

    if blocked_by_conflict:
        for item, check in blocked_by_conflict:
            old_conflict_status = item.conflict_resolution_status
            # Guarded: only mark conflict metadata on a row that is still a
            # live proposed candidate under our lock. RETURNING confirms the
            # transition; a zero-row result means the row is no longer
            # eligible and no event is written.
            blocking_result = await session.execute(
                update(MemoryItem)
                .where(
                    MemoryItem.id == item.id,
                    MemoryItem.tenant_id == tenant_id,
                    MemoryItem.review_status == "proposed",
                    MemoryItem.valid_to.is_(None),
                    MemoryItem.superseded_by.is_(None),
                )
                .values(
                    conflict_resolution_status="unresolved",
                    conflicts_with_item_id=check.conflicting_item_id,
                )
                .returning(MemoryItem.id),
                execution_options={"synchronize_session": False},
            )
            if blocking_result.scalar_one_or_none() is None:
                continue
            session.add(
                ItemEvent(
                    item_id=item.id,
                    event_type="conflict_resolution",
                    field_name="conflict_resolution_status",
                    old_value=old_conflict_status,
                    new_value="unresolved",
                    actor_principal_id=trusted_actor_id,
                    reason=(
                        f"auto-promotion Path A conflict recheck ({source}): "
                        f"blocked by {check.verdict} against item "
                        f"{check.conflicting_item_id} "
                        f"({'embedding' if check.used_embeddings else 'heuristic fallback'}) "
                        f"— {check.reason}"
                    ),
                )
            )

    if to_promote:
        for item in to_promote:
            decision = evaluate_transition(
                principal_id=item.principal_id,
                principal_type="system",
                item_author_principal_id=item.principal_id,
                current_status=item.review_status,
                requested_status="active",
                trusted_operation=TrustedReviewOperation.PROMOTION,
            )
            if not decision.allowed:
                raise RuntimeError("review policy rejected trusted Path A promotion")
        promote_ids = [item.id for item in to_promote]
        # Guarded promotion update (P0-FIX-004A): the WHERE clause re-checks
        # the expected state (proposed, live, not superseded) even though we
        # hold the row lock, so a zero-row result is a skip or concurrent-state
        # outcome — never permission to write an event. RETURNING reports
        # exactly which rows actually transitioned; events and result counts
        # are derived solely from that set.
        promotion_result = await session.execute(
            update(MemoryItem)
            .where(
                MemoryItem.tenant_id == tenant_id,
                MemoryItem.id.in_(promote_ids),
                MemoryItem.review_status == "proposed",
                MemoryItem.valid_to.is_(None),
                MemoryItem.superseded_by.is_(None),
            )
            .values(review_status="active")
            .returning(MemoryItem.id),
            execution_options={"synchronize_session": False},
        )
        actually_promoted: set[uuid.UUID] = {
            row[0] for row in promotion_result.all()
        }
        if actually_promoted:
            reason = (
                f"auto-promotion Path A ({source}): memory_confidence >= {threshold} "
                f"and age >= {min_age_hours}h, no unresolved conflict, no external "
                "dispute event, promotion-time conflict recheck clear"
            )
            promoted_ids: list[uuid.UUID] = []
            for item in to_promote:
                if item.id not in actually_promoted:
                    continue
                session.add(
                    ItemEvent(
                        item_id=item.id,
                        event_type="review_change",
                        field_name="review_status",
                        old_value="proposed",
                        new_value="active",
                        actor_principal_id=trusted_actor_id,
                        reason=reason,
                    )
                )
                promoted_ids.append(item.id)
            result.promoted = len(promoted_ids)
            result.promoted_ids = promoted_ids

    await session.commit()
    return result


async def maybe_auto_promote_for_startup_recall(
    session: AsyncSession,
    tenant_id: str,
    *,
    now: datetime | None = None,
) -> PromotionResult:
    """Bounded, tenant-scoped Path A promotion pass invoked lazily from
    ``POST /v1/recall`` (``mode='startup'``), before active items are
    selected (design.md §3, ENG-AUD-007 F11).

    Delegates entirely to :func:`auto_promote_proposed_memories` — no
    promotion logic is duplicated here — bounded by
    ``settings.startup_promotion_limit`` so a request handler never scans an
    unbounded proposed-item backlog. ``tenant_config.auto_promote_enabled`` is
    honored by the shared service function; when disabled this is a cheap
    no-op (one count query, no writes).

    Not invoked for semantic recall in this slice (see design.md — Path A
    promotion is a startup-recall concern only for now).
    """
    return await auto_promote_proposed_memories(
        session,
        tenant_id,
        now=now,
        limit=settings.startup_promotion_limit,
        source="startup_recall",
    )
