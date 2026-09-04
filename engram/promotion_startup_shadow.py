"""Read-only startup-promotion parity observation for ENG-PROMOTION-003B5.

The observer is deliberately not a promotion path. During compatibility mode
it receives the exact bounded window the authoritative legacy pass selected
with ``FOR UPDATE SKIP LOCKED``. After cutover it uses its own bounded,
content-free cursor to inspect canonical coverage while the legacy cursor stays
frozen for rollback. It never locks memory rows, enqueues work, writes item
events, or changes a memory lifecycle field.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import Select, and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from engram.config import settings
from engram.models import (
    MemoryItem,
    PromotionStartupShadowState,
)
from engram.promotion import (
    PromotionCandidate,
    _config,
    _config_values,
    _kind_promotion_allowed,
    assess_promotion_candidate,
    load_promotion_support,
)
from engram.promotion_reconciliation import (
    _classify_repair,
    _ItemObligation,
    _next_boundary,
    _window_job_states,
)

StartupPromotionParityOutcome = Literal[
    "parity_no_action",
    "parity_already_committed",
    "parity_durably_scheduled",
    "mismatch_missing_obligation",
    "mismatch_state",
    "unknown",
]

PARITY_OUTCOMES: tuple[StartupPromotionParityOutcome, ...] = (
    "parity_no_action",
    "parity_already_committed",
    "parity_durably_scheduled",
    "mismatch_missing_obligation",
    "mismatch_state",
    "unknown",
)


@dataclass
class StartupPromotionShadowResult:
    """Content-free result for one bounded shadow window."""

    tenant_id: str
    window_size: int = 0
    wrapped: bool = False
    concurrent_skipped: bool = False
    outcomes: Counter[str] = field(default_factory=Counter)

    @property
    def mismatch_count(self) -> int:
        return self.outcomes["mismatch_missing_obligation"] + self.outcomes["mismatch_state"]


def classify_startup_promotion_parity(
    *,
    review_status: str,
    candidate: PromotionCandidate,
    current_obligation_covered: bool,
    prerequisites_enabled: bool,
) -> StartupPromotionParityOutcome:
    """Classify one observer row without implementing promotion policy again.

    ``candidate`` comes from :func:`assess_promotion_candidate`; queue coverage
    comes from reconciliation's exact-boundary obligation probe.  The only
    decision made here is diagnostic vocabulary, never lifecycle authority.
    """
    if not prerequisites_enabled:
        return "unknown"
    if review_status == "active":
        return "parity_already_committed"
    if review_status != "proposed":
        return "mismatch_state"
    repair_class = _classify_repair(candidate)
    if repair_class == "terminal":
        return "parity_no_action"
    if current_obligation_covered:
        return "parity_durably_scheduled"
    # Both eligible-now and cooling candidates have an evaluator-derived
    # current obligation.  The reconciliation probe recognizes due/overdue
    # work and requires exact run_after equality while cooling.
    return "mismatch_missing_obligation"


def _shadow_window(
    tenant_id: str,
    cursor: PromotionStartupShadowState | None,
    limit: int,
) -> Select[tuple[MemoryItem]]:
    """The post-cutover diagnostic rotation topology without a row lock.

    Shadow observation must remain capable of running alongside startup,
    worker, and review activity without becoming a second mutation authority.
    The cursor is diagnostic-only and independent from the legacy rollback
    cursor, so this SELECT intentionally has no ``FOR UPDATE`` clause.
    """
    stmt = (
        select(MemoryItem)
        .where(
            MemoryItem.tenant_id == uuid.UUID(tenant_id),
            MemoryItem.review_status == "proposed",
            MemoryItem.valid_to.is_(None),
            _kind_promotion_allowed(),
        )
        .order_by(MemoryItem.created_at.asc(), MemoryItem.id.asc())
        .limit(limit)
    )
    if cursor is not None and cursor.cursor_created_at is not None:
        stmt = stmt.where(
            or_(
                MemoryItem.created_at > cursor.cursor_created_at,
                and_(
                    MemoryItem.created_at == cursor.cursor_created_at,
                    MemoryItem.id > cursor.cursor_item_id,
                ),
            )
        )
    return stmt


async def observe_startup_promotion_parity(
    session: AsyncSession,
    tenant_id: str,
    *,
    now: datetime | None = None,
    authoritative_window: Sequence[MemoryItem] | None = None,
) -> StartupPromotionShadowResult:
    """Observe one bounded, non-authoritative parity window.

    Caller-owned transaction semantics are intentional: startup recall's
    existing recall-log commit persists this diagnostic state, while the
    compatibility path's legacy promotion commit does likewise.  The observer
    itself never commits, so it cannot make an independent lifecycle change.
    """
    moment = now or datetime.now(UTC)
    result = StartupPromotionShadowResult(tenant_id=tenant_id)
    prerequisites_enabled = settings.startup_promotion_shadow_prerequisites_enabled
    if not prerequisites_enabled:
        result.outcomes["unknown"] += 1
        return result

    state = (
        await session.execute(
            select(PromotionStartupShadowState).where(
                PromotionStartupShadowState.tenant_id == uuid.UUID(tenant_id)
            )
        )
    ).scalar_one_or_none()
    # Compatibility callers hand off the exact rows selected by the legacy
    # locking query. Never query a reconstructed equivalent window here:
    # SKIP LOCKED may have selected a different page under concurrency.
    if authoritative_window is not None:
        items = list(authoritative_window)
        wrapped = False
    else:
        limit = settings.startup_promotion_limit
        items = list((await session.execute(_shadow_window(tenant_id, state, limit))).scalars())
        wrapped = False
        if not items and state is not None and state.cursor_created_at is not None:
            items = list((await session.execute(_shadow_window(tenant_id, None, limit))).scalars())
            wrapped = bool(items)

    result.window_size = len(items)
    result.wrapped = wrapped
    config = await _config(session, tenant_id)
    enabled, threshold, min_age, evidence_enabled, evidence_threshold = _config_values(config)
    support_map = await load_promotion_support(session, items)
    obligations: dict[uuid.UUID, _ItemObligation] = {}
    candidates: dict[uuid.UUID, PromotionCandidate] = {}
    if enabled:
        for item in items:
            candidate = assess_promotion_candidate(
                item,
                support_map[item.id],
                confidence_threshold=threshold,
                min_age_hours=min_age,
                evidence_enabled=evidence_enabled,
                evidence_threshold=evidence_threshold,
                now=moment,
            )
            candidates[item.id] = candidate
            repair_class = _classify_repair(candidate)
            obligations[item.id] = _ItemObligation(
                boundary=_next_boundary(candidate, moment)[0]
                if repair_class != "terminal"
                else None,
                classification_run_id=candidate.classification_run_id,
            )
    job_states = await _window_job_states(
        session, tenant_id=tenant_id, obligations=obligations, now=moment
    )
    for item in items:
        if not enabled or item.superseded_by is not None:
            outcome: StartupPromotionParityOutcome = "parity_no_action"
        else:
            outcome = classify_startup_promotion_parity(
                review_status=item.review_status,
                candidate=candidates[item.id],
                # Mixed-version safety is load-bearing during B5: an exact,
                # healthy legacy path_a job covers the same current
                # obligation until the queue drains, just as reconciliation
                # treats it as healthy.
                current_obligation_covered=job_states[item.id].promotion_covers_obligation,
                prerequisites_enabled=True,
            )
        result.outcomes[outcome] += 1

    counters = {outcome: result.outcomes[outcome] for outcome in PARITY_OUTCOMES}
    if authoritative_window is not None:
        # Aggregate every compatibility window atomically. Unlike the
        # independent post-cutover cursor, dropping a concurrent observation
        # here would omit an authoritative SKIP LOCKED window from parity
        # accounting. This never moves the post-cutover diagnostic cursor.
        insert_stmt = pg_insert(PromotionStartupShadowState).values(
            tenant_id=uuid.UUID(tenant_id),
            last_observed_at=moment,
            last_window_size=result.window_size,
            last_wrapped=False,
            updated_at=moment,
            **counters,
        )
        excluded = insert_stmt.excluded
        await session.execute(
            insert_stmt.on_conflict_do_update(
                index_elements=[PromotionStartupShadowState.tenant_id],
                set_={
                    "last_observed_at": excluded.last_observed_at,
                    "last_window_size": excluded.last_window_size,
                    "last_wrapped": excluded.last_wrapped,
                    "updated_at": excluded.updated_at,
                    **{
                        outcome: getattr(PromotionStartupShadowState, outcome)
                        + getattr(excluded, outcome)
                        for outcome in PARITY_OUTCOMES
                    },
                },
            )
        )
        return result

    if state is None:
        inserted = await session.scalar(
            pg_insert(PromotionStartupShadowState)
            .values(
                tenant_id=uuid.UUID(tenant_id),
                cursor_created_at=items[-1].created_at if items else None,
                cursor_item_id=items[-1].id if items else None,
                rotation=1 if wrapped else 0,
                last_observed_at=moment,
                last_window_size=result.window_size,
                last_wrapped=result.wrapped,
                updated_at=moment,
                **counters,
            )
            .on_conflict_do_nothing(index_elements=[PromotionStartupShadowState.tenant_id])
            .returning(PromotionStartupShadowState.tenant_id)
        )
        result.concurrent_skipped = inserted is None
        return result

    # Optimistic compare-and-swap gives diagnostic state a deterministic
    # single-winner progression without a memory-row or state-row lock. A
    # concurrent observer simply drops its duplicate observation; it has no
    # lifecycle side effect and the next startup call resumes from the winner.
    values: dict[str, object] = {
        "rotation": state.rotation + (1 if wrapped else 0),
        "last_observed_at": moment,
        "last_window_size": result.window_size,
        "last_wrapped": result.wrapped,
        "updated_at": moment,
        **{outcome: getattr(state, outcome) + counters[outcome] for outcome in PARITY_OUTCOMES},
    }
    if items:
        values["cursor_created_at"] = items[-1].created_at
        values["cursor_item_id"] = items[-1].id
    advanced = await session.scalar(
        update(PromotionStartupShadowState)
        .where(
            PromotionStartupShadowState.tenant_id == uuid.UUID(tenant_id),
            PromotionStartupShadowState.cursor_created_at.is_not_distinct_from(
                state.cursor_created_at
            ),
            PromotionStartupShadowState.cursor_item_id.is_not_distinct_from(state.cursor_item_id),
        )
        .values(**values)
        .returning(PromotionStartupShadowState.tenant_id)
    )
    result.concurrent_skipped = advanced is None
    return result
