"""Recall engine: scoring, startup recall, semantic recall.

Implements the trust-model scoring formula from design.md Section 4.
Startup recall is deterministic given state — same corpus + config = same output.

Startup recall is a two-stage pipeline (ENG-AUD-011 / F18):

  1. Bounded SQL candidate selection (:func:`_fetch_startup_candidates`) — a
     coarse, SQL-computed score plus several diversified sub-pools (freshest,
     highest-importance, least-recently-recalled) select at most
     ``settings.startup_recall_candidate_limit`` rows, over a read-oriented
     session. Pinned items are fetched separately so the candidate cap can
     never displace them.
  2. Detailed Python scoring (:func:`score_item`) runs only over that bounded
     candidate set — reasons, warnings, and budget packing are unchanged from
     the pre-ENG-AUD-011 full-corpus behavior; the only difference is what
     population is scored.

Recall-signal telemetry (``last_recalled_at``, ``startup_recall_count``,
``recall_count``) is no longer written inline in the read transaction — it is
enqueued as a best-effort ``recall.telemetry`` job (see engram/worker.py
``handle_recall_telemetry``) after the recall set is selected, so a durable
telemetry-write failure never fails the read.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, case, func, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from engram import db as db_module
from engram import recall_packing, recall_signals, relationship_recall, semantic
from engram.config import settings
from engram.embeddings import generate_embedding
from engram.jobs import enqueue_job
from engram.memory_access import read_eligibility_expression, resolve_workspace_scope
from engram.memory_context import ResolvedMemoryContext
from engram.memory_kinds import get_disputed_stay_kind_names
from engram.models import MemoryItem, RecallLog, TenantConfig
from engram.promotion import (
    PromotionObservedWindow,
    PromotionResult,
    maybe_auto_promote_for_startup_recall,
)
from engram.promotion_startup_shadow import observe_startup_promotion_parity
from engram.recall_profiles import (
    STARTUP_PROFILE_KEY,
    RecallProfileSpec,
    apply_profile_budget_caps,
    resolve_serving_profile,
)
from engram.relationship_recall import expand_recall_candidates
from engram.semantic_budget import semantic_item_byte_count, semantic_item_token_cost

logger = logging.getLogger(__name__)

# Candidate-selection strategy/version identifier, logged alongside
# scoring_version/config_version for audit reproducibility (requirement 16).
STARTUP_CANDIDATES_VERSION = "startup-candidates-v1"

# Sub-pool allocation as a fraction of the total candidate limit. Pinned items
# are fetched separately (not part of this split) so the cap can never
# displace them. Remainder after coarse/freshest/importance goes to
# least-recently-recalled, absorbing integer rounding.
_CANDIDATE_COARSE_FRACTION = 0.60
_CANDIDATE_FRESHEST_FRACTION = 0.15
_CANDIDATE_IMPORTANCE_FRACTION = 0.15


class ScoreResult:
    """Score and human-readable reasons for a single item."""

    def __init__(self, score: float, reasons: list[str], warnings: list[str] | None = None) -> None:
        self.score = score
        self.reasons = reasons
        self.warnings = warnings or []


def score_item(
    item: MemoryItem,
    config: TenantConfig | None,
    now: datetime,
) -> ScoreResult:
    """Pure scoring function — testable without DB.

    Formula (design.md §4):
      importance*0.30 + source_trust*0.25 + memory_confidence*0.20
      + recency*0.15 + verified*0.10

    Pinned items bypass this function entirely — they are not scored.
    Anti-feedback penalty: too many startup recalls without positive feedback
    reduces recency bonus.
    """
    # Read weights from tenant_config or use defaults
    if config is not None:
        w_importance = config.weight_importance
        w_source_trust = config.weight_source_trust
        w_memory_confidence = config.weight_memory_confidence
        w_recency = config.weight_recency
        w_verified = config.weight_verified
        penalty_threshold = config.startup_recall_penalty_threshold
        penalty_factor = config.startup_recall_penalty_factor
    else:
        w_importance = 0.30
        w_source_trust = 0.25
        w_memory_confidence = 0.20
        w_recency = 0.15
        w_verified = 0.10
        penalty_threshold = settings.startup_recall_penalty_threshold
        penalty_factor = settings.startup_recall_penalty_factor

    reasons: list[str] = []

    # Importance
    importance = item.importance
    reasons.append(f"importance={importance:.2f}")
    score = importance * w_importance

    # Source trust
    source_trust = item.source_trust
    reasons.append(f"source_trust={source_trust:.2f}")
    score += source_trust * w_source_trust

    # Memory confidence
    memory_confidence = item.memory_confidence
    reasons.append(f"memory_confidence={memory_confidence:.2f}")
    score += memory_confidence * w_memory_confidence

    # Recency bonus (decay: max(0, 1 - days/30)).
    #
    # Two contributions, taking the max:
    #   * recall_recency — decay from last_recalled_at (0 when never recalled).
    #     The anti-feedback penalty applies ONLY to this term.
    #   * freshness — decay from when the item became valid (valid_from or
    #     created_at), scaled by 0.5 so a fresh, never-recalled memory gets a
    #     modest recency contribution without dominating trust/importance.
    recall_recency = 0.0
    if item.last_recalled_at is not None:
        days_since = (now - item.last_recalled_at).total_seconds() / 86400
        recall_recency = max(0.0, 1.0 - days_since / 30.0)
        # Anti-feedback penalty (tied to recall-driven recency only).
        if item.startup_recall_count > penalty_threshold:
            excess = item.startup_recall_count - penalty_threshold
            penalty = penalty_factor**excess
            recall_recency *= penalty
            recall_recency = max(recall_recency, settings.startup_recall_penalty_floor)
            reasons.append(f"recency_penalty(count={item.startup_recall_count})")

    freshness_anchor = item.valid_from or item.created_at
    days_since_anchor = max(0.0, (now - freshness_anchor).total_seconds() / 86400)
    freshness = max(0.0, 1.0 - days_since_anchor / 30.0) * 0.5

    recency_bonus = max(recall_recency, freshness)
    if freshness > recall_recency:
        reasons.append(f"freshness={freshness:.2f}")
    reasons.append(f"recency={recency_bonus:.2f}")
    score += recency_bonus * w_recency

    # Verified bonus
    verified_bonus = 1.0 if item.human_verified else 0.0
    if item.human_verified:
        reasons.append("human_verified")
    score += verified_bonus * w_verified

    # Warnings
    warnings: list[str] = []
    stale_after_days = config.stale_after_days if config is not None else settings.stale_after_days
    last_verified = item.last_verified_at or item.valid_from
    if last_verified is not None:
        days_since_verified = (now - last_verified).total_seconds() / 86400
        if days_since_verified > stale_after_days:
            warnings.append(f"not confirmed in {stale_after_days} days")
    if item.memory_confidence < 0.5:
        warnings.append("low confidence")
    if item.conflict_resolution_status == "unresolved":
        warnings.append("unresolved conflicts")
    if item.review_status == "disputed":
        warnings.append("disputed — pending resolution")

    return ScoreResult(round(score, 4), reasons, warnings)


async def _get_tenant_config(
    session: AsyncSession,
    tenant_id: str,
) -> TenantConfig | None:
    """Fetch active tenant config."""
    result = await session.execute(
        select(TenantConfig).where(
            TenantConfig.tenant_id == tenant_id,
            TenantConfig.active.is_(True),
        )
    )
    return result.scalar_one_or_none()


def _resolve_recall_budgets(
    *,
    byte_budget: int | None,
    token_budget: int | None,
    item_budget: int | None,
) -> tuple[int | None, int | None, int | None]:
    """Apply configured defaults for omitted recall budgets.

    Recall is bounded by default: an omitted budget falls back to the global
    settings default rather than leaving recall unbounded. There is no
    API-documented way to request unbounded recall, so an omitted budget is
    treated as "use the default".

    Note: ``tenant_config`` does not currently carry per-tenant recall budgets,
    so defaults come from global ``settings`` (``recall_byte_budget``,
    ``recall_item_budget``). There is no global default for ``token_budget``,
    so it remains unset (unbounded) unless the caller provides one — the byte
    default still bounds recall.
    """
    resolved_byte = byte_budget if byte_budget is not None else settings.recall_byte_budget
    resolved_item = item_budget if item_budget is not None else settings.recall_item_budget
    resolved_token = token_budget
    return resolved_byte, resolved_token, resolved_item


async def _fetch_active_items(
    session: AsyncSession,
    memory_context: ResolvedMemoryContext,
    workspace_id: str | None,
) -> list[MemoryItem]:
    """Fetch EVERY active, non-expired, eligible item — the pre-ENG-AUD-011 path.

    ``review_status='active'`` items always qualify. Disputed items also
    qualify when their kind is governed with
    ``stays_in_recall_when_disputed=True`` (ENG-AUD-010 / F17, design.md
    §"Disputed high-stakes items") — replaces the doctrine/invariant-string
    special case the design doc described but that was never implemented.
    Also enforces the shared tenant/visibility eligibility predicate so a
    principal never sees another principal's private memory, or workspace
    memory from a workspace they aren't a member of.

    NOT used by :func:`execute_startup_recall` anymore — it loads the whole
    eligible corpus into Python, which is exactly what F18 flags as a
    scalability cliff. Kept as the reference full-corpus scoring path for
    scoring-parity tests (requirement 13): compare its output, run through
    :func:`score_item`, against the bounded pipeline's output on the same
    fixtures.
    """
    stay_kinds = await get_disputed_stay_kind_names(session, memory_context.tenant_id)
    review_status_clause = _review_status_clause(stay_kinds)
    stmt = select(MemoryItem).where(
        review_status_clause,
        MemoryItem.valid_to.is_(None),
        read_eligibility_expression(memory_context),
    )
    if workspace_id is not None:
        stmt = stmt.where(MemoryItem.workspace_id == workspace_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


def _review_status_clause(stay_kinds: set[str]) -> ColumnElement[bool]:
    """Shared active/governed-disputed eligibility clause (see _fetch_active_items)."""
    clause = MemoryItem.review_status == "active"
    if stay_kinds:
        clause = or_(
            clause,
            (MemoryItem.review_status == "disputed") & MemoryItem.kind.in_(stay_kinds),
        )
    return clause


def _coarse_score_expression(
    config: TenantConfig | None,
    now: datetime,
) -> ColumnElement[float]:
    """SQL-computable approximation of :func:`score_item`, for candidate ranking only.

    Mirrors the Python formula's shape (importance/source_trust/
    memory_confidence/verified/recency weights, freshness vs. recall-recency
    max, anti-feedback penalty via ``power()``) using only columns available in
    SQL. This is candidate retrieval ranking, not the final externally
    meaningful score — :func:`score_item` remains the sole source of the
    returned ``score``/``reasons``/``warnings``.
    """
    if config is not None:
        w_importance = config.weight_importance
        w_source_trust = config.weight_source_trust
        w_memory_confidence = config.weight_memory_confidence
        w_recency = config.weight_recency
        w_verified = config.weight_verified
        penalty_threshold = config.startup_recall_penalty_threshold
        penalty_factor = config.startup_recall_penalty_factor
    else:
        w_importance = 0.30
        w_source_trust = 0.25
        w_memory_confidence = 0.20
        w_recency = 0.15
        w_verified = 0.10
        penalty_threshold = settings.startup_recall_penalty_threshold
        penalty_factor = settings.startup_recall_penalty_factor

    now_lit = literal(now)
    seconds_per_day = 86400.0

    freshness_anchor = func.coalesce(MemoryItem.valid_from, MemoryItem.created_at)
    days_since_anchor = func.extract("epoch", now_lit - freshness_anchor) / seconds_per_day
    freshness = func.greatest(0.0, 1.0 - days_since_anchor / 30.0) * 0.5

    days_since_recalled = (
        func.extract("epoch", now_lit - MemoryItem.last_recalled_at) / seconds_per_day
    )
    recall_recency_raw = func.greatest(0.0, 1.0 - days_since_recalled / 30.0)
    excess = func.greatest(MemoryItem.startup_recall_count - penalty_threshold, 0)
    penalty = func.power(penalty_factor, excess)
    recall_recency_penalized = func.greatest(
        recall_recency_raw * penalty, settings.startup_recall_penalty_floor
    )
    recall_recency = case(
        (MemoryItem.last_recalled_at.is_(None), 0.0),
        else_=recall_recency_penalized,
    )

    recency = func.greatest(recall_recency, freshness)
    verified = case((MemoryItem.human_verified.is_(True), 1.0), else_=0.0)

    return (
        MemoryItem.importance * w_importance
        + MemoryItem.source_trust * w_source_trust
        + MemoryItem.memory_confidence * w_memory_confidence
        + recency * w_recency
        + verified * w_verified
    )


def _candidate_allocation(candidate_limit: int) -> dict[str, int]:
    """Split the candidate pool budget across diversified sub-pools.

    Pinned items are fetched separately (not part of this split — see
    :func:`_fetch_startup_candidates`). Default allocation for a 500-item pool:
    300 by coarse score, 75 freshest, 75 highest-importance, 50
    least-recently-recalled — matching the documented example allocation.
    """
    coarse = int(candidate_limit * _CANDIDATE_COARSE_FRACTION)
    freshest = int(candidate_limit * _CANDIDATE_FRESHEST_FRACTION)
    importance = int(candidate_limit * _CANDIDATE_IMPORTANCE_FRACTION)
    least_recalled = max(0, candidate_limit - coarse - freshest - importance)
    return {
        "coarse": coarse,
        "freshest": freshest,
        "importance": importance,
        "least_recalled": least_recalled,
    }


def _base_candidate_filters(
    *,
    memory_context: ResolvedMemoryContext,
    workspace_id: str | None,
    review_status_clause: ColumnElement[bool],
) -> list[Any]:
    """Shared WHERE clauses for every candidate sub-query (tenant/eligibility/kind)."""
    filters: list[Any] = [
        review_status_clause,
        MemoryItem.valid_to.is_(None),
        read_eligibility_expression(memory_context),
    ]
    if workspace_id is not None:
        filters.append(MemoryItem.workspace_id == workspace_id)
    return filters


class CandidateStats:
    """Observability counters for one candidate-selection call (requirement 16)."""

    def __init__(self) -> None:
        self.pinned_count = 0
        self.coarse_count = 0
        self.freshest_count = 0
        self.importance_count = 0
        self.least_recalled_count = 0
        self.deduped_total = 0
        self.query_count = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "pinned": self.pinned_count,
            "coarse": self.coarse_count,
            "freshest": self.freshest_count,
            "importance": self.importance_count,
            "least_recalled": self.least_recalled_count,
            "deduped_total": self.deduped_total,
            "query_count": self.query_count,
        }


async def _fetch_startup_candidates(
    session: AsyncSession,
    *,
    memory_context: ResolvedMemoryContext,
    workspace_id: str | None,
    now: datetime,
    config: TenantConfig | None,
    candidate_limit: int,
) -> tuple[list[MemoryItem], CandidateStats]:
    """Bounded, diversified SQL candidate selection (ENG-AUD-011 / F18 stage 1).

    Runs entirely over ``session`` (expected to be a read-oriented session —
    see :func:`execute_startup_recall`), issuing a small, fixed number of
    LIMITed queries regardless of corpus size:

    1. Pinned eligible items, up to ``candidate_limit`` rows — fetched
       separately from the scored sub-pools so the candidate cap can never
       displace a pinned item (the final pinned ceiling/budget packing still
       happens in Python, unchanged).
    2. Highest coarse-score items (:func:`_coarse_score_expression`).
    3. Freshest items (by ``valid_from``/``created_at``).
    4. Highest-importance items.
    5. Least-recently-recalled (nulls first) items.

    Sub-pools 2-5 are allocated via :func:`_candidate_allocation` and
    deduplicated by item id before being returned — a candidate ranked highly
    by more than one signal is scored once. This diversification protects
    against a bounded coarse-score-only pool accidentally omitting an item
    that would rank highly under detailed Python scoring (requirement 6).
    """
    stats = CandidateStats()
    stay_kinds = await get_disputed_stay_kind_names(session, memory_context.tenant_id)
    stats.query_count += 1
    review_status_clause = _review_status_clause(stay_kinds)
    filters = _base_candidate_filters(
        memory_context=memory_context,
        workspace_id=workspace_id,
        review_status_clause=review_status_clause,
    )

    # 1. Pinned — bounded by candidate_limit itself (worst case: every eligible
    #    item is pinned), never by a fraction of it.
    pinned_stmt = (
        select(MemoryItem)
        .where(*filters, MemoryItem.pinned.is_(True))
        .order_by(
            (MemoryItem.importance * MemoryItem.source_trust).desc(),
            MemoryItem.created_at.desc(),
            MemoryItem.id.asc(),
        )
        .limit(candidate_limit)
    )
    pinned_result = await session.execute(pinned_stmt)
    pinned_items = list(pinned_result.scalars().all())
    stats.pinned_count = len(pinned_items)
    stats.query_count += 1

    allocation = _candidate_allocation(candidate_limit)
    not_pinned = MemoryItem.pinned.is_(False)

    coarse_score = _coarse_score_expression(config, now)
    coarse_stmt = (
        select(MemoryItem)
        .where(*filters, not_pinned)
        .order_by(coarse_score.desc(), MemoryItem.created_at.desc(), MemoryItem.id.asc())
        .limit(allocation["coarse"])
    )
    coarse_result = await session.execute(coarse_stmt)
    coarse_items = list(coarse_result.scalars().all())
    stats.coarse_count = len(coarse_items)
    stats.query_count += 1

    freshness_anchor = func.coalesce(MemoryItem.valid_from, MemoryItem.created_at)
    freshest_stmt = (
        select(MemoryItem)
        .where(*filters, not_pinned)
        .order_by(freshness_anchor.desc(), MemoryItem.id.asc())
        .limit(allocation["freshest"])
    )
    freshest_result = await session.execute(freshest_stmt)
    freshest_items = list(freshest_result.scalars().all())
    stats.freshest_count = len(freshest_items)
    stats.query_count += 1

    importance_stmt = (
        select(MemoryItem)
        .where(*filters, not_pinned)
        .order_by(
            MemoryItem.importance.desc(), MemoryItem.created_at.desc(), MemoryItem.id.asc()
        )
        .limit(allocation["importance"])
    )
    importance_result = await session.execute(importance_stmt)
    importance_items = list(importance_result.scalars().all())
    stats.importance_count = len(importance_items)
    stats.query_count += 1

    least_recalled_stmt = (
        select(MemoryItem)
        .where(*filters, not_pinned)
        .order_by(
            MemoryItem.last_recalled_at.asc().nulls_first(),
            MemoryItem.created_at.desc(),
            MemoryItem.id.asc(),
        )
        .limit(allocation["least_recalled"])
    )
    least_recalled_result = await session.execute(least_recalled_stmt)
    least_recalled_items = list(least_recalled_result.scalars().all())
    stats.least_recalled_count = len(least_recalled_items)
    stats.query_count += 1

    seen: set[UUID] = set()
    merged: list[MemoryItem] = []
    buckets = (pinned_items, coarse_items, freshest_items, importance_items, least_recalled_items)
    for bucket in buckets:
        for item in bucket:
            if item.id in seen:
                continue
            seen.add(item.id)
            merged.append(item)
    stats.deduped_total = len(merged)

    return merged, stats


def _separate_pinned(
    items: list[MemoryItem],
    max_pinned_tokens: int,
) -> tuple[list[MemoryItem], list[MemoryItem], int]:
    """Separate pinned items (bypass) from scored items.

    Returns (pinned, scored, pinned_omitted_count).
    Pinned items capped at max_pinned_tokens by importance*source_trust.
    """
    pinned = [i for i in items if i.pinned]
    scored = [i for i in items if not i.pinned]

    if not pinned:
        return [], scored, 0

    # Sort pinned by importance * source_trust descending
    pinned.sort(
        key=lambda i: i.importance * i.source_trust,
        reverse=True,
    )

    # Approximate tokens as bytes/4
    budget_used = 0
    kept = []
    omitted = 0
    for item in pinned:
        item_tokens = max(1, len(item.content.encode()) // 4)
        if budget_used + item_tokens <= max_pinned_tokens:
            kept.append(item)
            budget_used += item_tokens
        else:
            omitted += 1

    return kept, scored, omitted


def _enforce_budget(
    items_with_scores: list[tuple[MemoryItem, float]],
    byte_budget: int | None,
    token_budget: int | None,
) -> list[tuple[MemoryItem, float]]:
    """Enforce byte/token budget, preserving score order.

    Skip-not-break: an item that would exceed a budget is skipped and scanning
    continues to lower-ranked items that still fit, so one oversized item can't
    prematurely end the working set. Both byte and token accumulators are
    tracked independently when both budgets are set. Truncation is counted by
    the caller via the omitted_count difference (ranked items not selected).
    """
    if byte_budget is None and token_budget is None:
        return items_with_scores

    result: list[tuple[MemoryItem, float]] = []
    bytes_used = 0
    tokens_used = 0

    for item, score in items_with_scores:
        item_bytes = len(item.content.encode())
        item_tokens = max(1, item_bytes // 4)

        if byte_budget is not None and bytes_used + item_bytes > byte_budget:
            continue
        if token_budget is not None and tokens_used + item_tokens > token_budget:
            continue
        bytes_used += item_bytes
        tokens_used += item_tokens
        result.append((item, score))

    return result


async def execute_startup_recall(
    session: AsyncSession,
    memory_context: ResolvedMemoryContext,
    workspace: str | None,
    byte_budget: int | None,
    token_budget: int | None,
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Execute startup recall and return the response dict.

    Two-stage pipeline (ENG-AUD-011 / F18 — see module docstring):
    0. Optional, bounded legacy promotion compatibility pass (write session).
       The B5 shadow observer remains lifecycle-read-only in either mode.
    1. Bounded SQL candidate selection (read session, unless the explicitly
       enabled compatibility pass actually promoted rows — see step 1 below).
    2. Separate pinned (bypass, capped) from scored candidates.
    3. Score remaining candidates by the detailed formula, sort descending.
    4. Enforce budget.
    5. Write the recall_logs audit row (write session — this is audit
       provenance, not the per-item telemetry counters removed by F18).
    6. Best-effort enqueue of a ``recall.telemetry`` job to apply
       last_recalled_at/recall_count/startup_recall_count asynchronously.
    7. Return working_set + items with reasons.
    """
    now = datetime.now(UTC)
    tenant_id = str(memory_context.tenant_id)
    principal_id = str(memory_context.principal_id)
    config = await _get_tenant_config(session, tenant_id)

    # Apply configured defaults for omitted budgets so startup recall is
    # bounded by default (no API-documented way to request unbounded recall).
    byte_budget, token_budget, _item_budget = _resolve_recall_budgets(
        byte_budget=byte_budget,
        token_budget=token_budget,
        item_budget=None,
    )

    # Resolve workspace_id if provided. An explicit workspace request that
    # doesn't resolve, or where the caller isn't a member, must not fall back
    # to an unscoped read — it yields zero items instead.
    workspace_id, workspace_accessible = await resolve_workspace_scope(
        session, memory_context=memory_context, workspace=workspace
    )

    # 0. The legacy bounded lazy pass stays available strictly as the explicit
    # compatibility/rollback path. When shadow is on, its callback receives
    # the exact locked SKIP LOCKED window after selection and before any
    # lifecycle mutation. When compatibility mutation is off, the observer
    # instead uses its own bounded cursor; the legacy cursor stays frozen for
    # rollback. In both modes it is diagnostic-only.
    shadow_enabled = (
        settings.startup_promotion_shadow_enabled
        and settings.startup_promotion_shadow_prerequisites_enabled
    )
    if settings.startup_promotion_mutation_enabled:
        if shadow_enabled:

            async def observe_authoritative_window(window: PromotionObservedWindow) -> None:
                shadow_result = await observe_startup_promotion_parity(
                    session, tenant_id, now=now, authoritative_window=window
                )
                logger.info(
                    "startup_promotion_shadow tenant=%s window=%s wrapped=%s outcomes=%s "
                    "request_id=%s",
                    tenant_id,
                    shadow_result.window_size,
                    shadow_result.wrapped,
                    dict(shadow_result.outcomes),
                    request_id,
                )

            promotion_result = await maybe_auto_promote_for_startup_recall(
                session,
                tenant_id,
                now=now,
                selected_window_observer=observe_authoritative_window,
            )
        else:
            promotion_result = await maybe_auto_promote_for_startup_recall(
                session, tenant_id, now=now
            )
    else:
        if shadow_enabled:
            shadow_result = await observe_startup_promotion_parity(session, tenant_id, now=now)
            logger.info(
                "startup_promotion_shadow tenant=%s window=%s wrapped=%s outcomes=%s request_id=%s",
                tenant_id,
                shadow_result.window_size,
                shadow_result.wrapped,
                dict(shadow_result.outcomes),
                request_id,
            )
        promotion_result = PromotionResult(tenant_id, False, 0.0, 0)

    # 1. Bounded SQL candidate selection. Promotion consistency policy
    #    (requirement 12, "preferred conservative behavior"): when this
    #    recall's own lazy promotion pass actually promoted rows, read
    #    candidates from the primary/write session so the just-promoted rows
    #    are guaranteed visible in this recall — a read replica could lag
    #    behind the promotion write. With B5 mutation disabled this branch is
    #    impossible, so the normal read-oriented session is always used.
    #    (a configured replica via ENGRAM_READ_DATABASE_URL, or the primary
    #    when unset — see engram.db.read_session_factory).
    candidate_limit = min(
        settings.startup_recall_candidate_limit,
        settings.startup_recall_candidate_limit_max,
    )
    read_source = "primary"
    if not memory_context.may_read_anything or (
        workspace is not None and not workspace_accessible
    ):
        candidates: list[MemoryItem] = []
        candidate_stats = CandidateStats()
    elif promotion_result.promoted > 0:
        candidates, candidate_stats = await _fetch_startup_candidates(
            session,
            memory_context=memory_context,
            workspace_id=workspace_id,
            now=now,
            config=config,
            candidate_limit=candidate_limit,
        )
    else:
        read_source = "replica" if settings.read_database_url else "primary"
        async with db_module.read_session_factory() as read_session:
            await db_module.apply_rls_context(
                read_session, tenant_id=tenant_id, principal_id=principal_id
            )
            candidates, candidate_stats = await _fetch_startup_candidates(
                read_session,
                memory_context=memory_context,
                workspace_id=workspace_id,
                now=now,
                config=config,
                candidate_limit=candidate_limit,
            )

    logger.info(
        "startup_recall_candidates tenant=%s mode=startup candidate_limit=%s "
        "candidates=%s read_source=%s promoted=%s strategy=%s request_id=%s",
        tenant_id,
        candidate_limit,
        candidate_stats.deduped_total,
        read_source,
        promotion_result.promoted,
        STARTUP_CANDIDATES_VERSION,
        request_id,
    )

    # 2. Separate pinned
    max_pinned = config.max_pinned_tokens if config is not None else settings.max_pinned_tokens
    pinned_items, scored_items, pinned_omitted = _separate_pinned(candidates, max_pinned)

    # 3. Score remaining items
    scored_with_results = []
    for item in scored_items:
        result = score_item(item, config, now)
        scored_with_results.append((item, result.score, result.reasons, result.warnings))

    # Sort by score descending
    scored_with_results.sort(key=lambda x: x[1], reverse=True)

    # 4. Enforce budget (pinned first, then scored)
    if byte_budget is not None:
        pinned_bytes = sum(len(i.content.encode()) for i in pinned_items)
        effective_budget = max(0, byte_budget - pinned_bytes)
    else:
        effective_budget = None

    if token_budget is not None:
        pinned_tokens = sum(max(1, len(i.content.encode()) // 4) for i in pinned_items)
        effective_token_budget = max(0, token_budget - pinned_tokens)
    else:
        effective_token_budget = None

    budgeted_items = _enforce_budget(
        [(i, s) for i, s, _, _ in scored_with_results],
        effective_budget,
        effective_token_budget,
    )
    # Reattach reasons and warnings
    item_to_reasons = {id(i): r for i, _, r, _ in scored_with_results}
    item_to_warnings = {id(i): w for i, _, _, w in scored_with_results}
    scored_with_reasons = [
        (i, s, item_to_reasons.get(id(i), []), item_to_warnings.get(id(i), []))
        for i, s in budgeted_items
    ]

    # 5. Build response
    # Pinned items bypass score_item() entirely, so the disputed warning is
    # applied here directly (mirrors the same check in score_item's warnings).
    all_items: list[tuple[MemoryItem, float | None, list[str], list[str]]] = [
        (
            i,
            None,
            [],
            ["disputed — pending resolution"] if i.review_status == "disputed" else [],
        )
        for i in pinned_items
    ] + [(i, s, r, w) for i, s, r, w in scored_with_reasons]

    working_set_lines = []
    response_items = []

    for item, score, reasons, warnings in all_items:
        line = f"[{item.kind}] {item.content}"
        working_set_lines.append(line)

        item_dict: dict[str, Any] = {
            "id": str(item.id),
            "kind": item.kind,
            "content": item.content,
            "review_status": item.review_status,
            "score": score,
            "reasons": reasons if reasons else [],
            "warnings": warnings if warnings else [],
            "pinned": item.pinned,
            "importance": item.importance,
            "source_trust": item.source_trust,
            "memory_confidence": item.memory_confidence,
            "human_verified": item.human_verified,
            # Additive served-decision fields (ENG-CONTEXT-001): exposed so the
            # canonical context manifest can snapshot the exact mutable
            # decision state that was served. These are append-only and do not
            # change startup selection, ordering, scores, or rendering.
            "authority": item.authority,
            "visibility": item.visibility,
            "workspace_id": str(item.workspace_id) if item.workspace_id else None,
            "conflict_type": item.conflict_type,
            "conflict_resolution_status": item.conflict_resolution_status,
        }
        response_items.append(item_dict)

    working_set = "\n".join(working_set_lines)
    item_count = len(all_items)
    byte_count = sum(len(i.content.encode()) for i, _, _, _ in all_items)

    # 5. Write recall_logs — audit provenance (what was surfaced under which
    #    scoring/config version), NOT the per-item telemetry counters. This
    #    row also doubles as the telemetry job's idempotency claim record
    #    (RecallLog.telemetry_applied_at) — see engram.worker.handle_recall_telemetry.
    scoring_version = "v1"
    config_version = config.config_version if config is not None else "v1"
    item_ids = [i.id for i, _, _, _ in all_items]

    recall_log = RecallLog(
        tenant_id=tenant_id,
        principal_id=principal_id,
        mode="startup",
        byte_budget=byte_budget,
        token_budget=token_budget,
        item_ids=item_ids,
        scoring_version=scoring_version,
        config_version=config_version,
        recall_profile=STARTUP_PROFILE_KEY,
        memory_profile_id=memory_context.memory_profile_id,
        memory_profile_revision_id=memory_context.memory_profile_revision_id,
        memory_context_version=memory_context.version,
    )
    session.add(recall_log)
    await session.commit()

    # 6. Best-effort telemetry enqueue (ENG-AUD-011 / F18 requirement 7/9):
    #    last_recalled_at/recall_count/startup_recall_count updates run
    #    asynchronously via a recall.telemetry job instead of inline in this
    #    transaction. dedupe_key=recall_log.id means a duplicate enqueue (e.g.
    #    a caller retry racing this same request) resolves to the same job
    #    rather than double-queuing; the worker's own idempotency guard
    #    (RecallLog.telemetry_applied_at, claimed transactionally) is what
    #    actually prevents double-incrementing counters on job retry. Enqueue
    #    failure is logged and swallowed — it must never fail the read.
    telemetry_enqueued = False
    if item_ids:
        try:
            await enqueue_job(
                session,
                tenant_id=tenant_id,
                job_type="recall.telemetry",
                payload={
                    "tenant_id": str(tenant_id),
                    "principal_id": str(principal_id),
                    "mode": "startup",
                    "recall_log_id": str(recall_log.id),
                    "item_ids": [str(i) for i in item_ids],
                    "recalled_at": now.isoformat(),
                    "request_id": request_id,
                },
                dedupe_key=str(recall_log.id),
            )
            telemetry_enqueued = True
        except Exception:
            logger.exception(
                "recall_telemetry_enqueue_failed tenant=%s recall_log_id=%s request_id=%s",
                tenant_id,
                recall_log.id,
                request_id,
            )

    return {
        "working_set": working_set,
        "item_count": item_count,
        "byte_count": byte_count,
        "pinned_omitted_count": pinned_omitted,
        "omitted_count": max(0, len(candidates) - item_count),
        "items": response_items,
        "scoring_version": scoring_version,
        "config_version": config_version,
        "recall_log_id": str(recall_log.id),
        # Observability (requirement 16) — not part of the documented public
        # response contract (RecallResponse only reads the keys above), but
        # available to tests/logs/callers that want the bounded-pipeline
        # counters without re-deriving them from logs.
        "candidate_count": candidate_stats.deduped_total,
        "candidate_stats": candidate_stats.as_dict(),
        "scored_count": len(scored_items),
        "candidate_strategy_version": STARTUP_CANDIDATES_VERSION,
        "read_source": read_source,
        "telemetry_enqueued": telemetry_enqueued,
        # Profile context (issue #160): startup recall is its own profile.
        "recall_profile": STARTUP_PROFILE_KEY,
        # Telemetry context (ENG-METER-001). Startup recall is deterministic and
        # never calls an embedding provider, so embedding_outcome is not_required.
        "workspace_id": str(workspace_id) if workspace_id else None,
        "embedding_outcome": "not_required",
        # Internal-only effective decision context (ENG-CONTEXT-002B). These are
        # the exact resolved values used by budget enforcement and written to
        # RecallLog (``byte_budget``/``token_budget`` columns), exposed so the
        # startup receipt can build the effective descriptor without recomputing
        # defaults in the route. Startup v1 never enforces an item budget, so
        # ``effective_item_budget`` is always null. These MUST NOT enter
        # RecallResponse (they are not part of the public contract).
        "effective_byte_budget": byte_budget,
        "effective_token_budget": token_budget,
        "effective_item_budget": None,
    }


# ---- Semantic recall ----

# The legacy profile's corpus window (design.md §3): active AND proposed items
# so agents can rediscover their own observations; rejected/archived/expired
# are always excluded. Profile-governed/exploratory windows come from
# engram.recall_profiles instead — this tuple documents (and relationship
# expansion mirrors) the legacy behavior.
_SEMANTIC_REVIEW_STATUSES = ("active", "proposed")

# Over-fetch factor: pull more candidates than the item budget so byte/token
# budget enforcement still has a pool to draw from after dropping large items.
_SEMANTIC_OVERFETCH = 3
_SEMANTIC_OVERFETCH_CAP = 200

_NO_EMBEDDINGS_MESSAGE = (
    "No embeddings are available yet. Semantic recall requires memories written "
    "with embedding_provider != 'none'."
)


def _enforce_semantic_budget(
    candidates: list[dict[str, Any]],
    *,
    byte_budget: int | None,
    token_budget: int | None,
    item_budget: int | None,
) -> list[dict[str, Any]]:
    """Enforce item/byte/token budgets on trust-ranked candidates.

    Candidates arrive ordered by the final trust-weighted semantic score
    (engram.semantic). Skip-not-break: an item that would exceed a budget is
    skipped and scanning continues to lower-ranked items that still fit, so one
    oversized high-ranked item can't prematurely end the working set. Both byte
    and token accumulators are tracked independently when both budgets are set.
    Skipped oversized items are counted as omitted by the caller.
    """
    if byte_budget is None and token_budget is None and item_budget is None:
        return candidates

    result: list[dict[str, Any]] = []
    bytes_used = 0
    tokens_used = 0

    for cand in candidates:
        if item_budget is not None and len(result) >= item_budget:
            break
        item_bytes = semantic_item_byte_count(cand["content"])
        item_tokens = semantic_item_token_cost(cand["content"])

        # Skip oversized items and keep scanning lower-ranked ones that fit.
        if byte_budget is not None and bytes_used + item_bytes > byte_budget:
            continue
        if token_budget is not None and tokens_used + item_tokens > token_budget:
            continue

        bytes_used += item_bytes
        tokens_used += item_tokens
        result.append(cand)

    return result


def _semantic_base_item_fields(
    item: MemoryItem,
    *,
    distance: float | None,
    similarity: float | None,
) -> dict[str, Any]:
    """Per-item fields shared by every semantic profile's served items.

    Both the legacy blend path and the signal path build on this so the
    served-decision fields (ENG-CONTEXT-001) stay contract-aligned: a new
    field lands in one place and every profile serves it. Scoring/reasons/
    warnings differ per profile and are added by the caller. ``distance``/
    ``similarity`` are ``None`` for items reached only through relationship
    expansion (no query vector was ever compared against them — issue #190);
    their relevance lives in ``relevance_score`` and the structured
    ``relationship`` block.
    """
    return {
        "id": str(item.id),
        "kind": item.kind,
        "content": item.content,
        "review_status": item.review_status,
        "distance": round(distance, 4) if distance is not None else None,
        "similarity_score": round(similarity, 4) if similarity is not None else None,
        "pinned": item.pinned,
        "importance": item.importance,
        "source_trust": item.source_trust,
        "memory_confidence": item.memory_confidence,
        "human_verified": item.human_verified,
        "authority": item.authority,
        "visibility": item.visibility,
        "workspace_id": str(item.workspace_id) if item.workspace_id else None,
        "conflict_type": item.conflict_type,
        "conflict_resolution_status": item.conflict_resolution_status,
    }


def _signal_corpus_eligibility(
    profile: RecallProfileSpec,
    stay_kinds: set[str],
) -> ColumnElement[bool]:
    """The pre-retrieval corpus predicate for a signal profile (issue #160).

    Everything mechanically expressible without examining relevance is pushed
    into SQL *before* the bounded HNSW window. Since issue #186 the signal
    profiles are V2-bound: their admission authority is the exact #158
    per-surface decision, whose expressible domain is the live-proposal
    corpus (:func:`engram.recall_signals.live_proposal_expression`) —
    everything else (risk, epistemic state, calibration, observation windows,
    external disputes) is the post-retrieval V2 gate's job. ``stay_kinds`` is
    retained for signature compatibility with future corpus doctrines; the
    disputed stay-kind doctrine is not part of the V2-bound window (disputed
    items are never live proposals). Issue #190 reuses this exact predicate
    as the relationship-expansion discovery window so direct retrieval and
    neighbor discovery can never disagree about the corpus.
    """
    return recall_signals.live_proposal_expression()


@dataclass
class SignalAdmissionOutcome:
    """What the V2-bound admission step produced for one candidate window.

    ``admission_diagnostics`` carries one bounded, content-free entry per
    withheld candidate — identity and codes only — so operators can see the
    exact local-versus-V2 disagreement that withheld it (issue #186). Since
    issue #190 each entry also names the ``origin`` that surfaced the
    candidate (direct semantic hit vs graph/tunnel expansion), and
    ``expansion`` summarizes the admission-first expansion run itself
    (contract version, seed/neighbor/admission counts). Since issue #192
    ``admitted_entries`` exposes the ranked admitted items (ORM object +
    served dict) so the packing stage can consume their durable identity
    facts (``conflicts_with_item_id``) without widening anything the item
    dicts publish."""

    items: list[dict[str, Any]]
    omitted_by_admission: dict[str, int]
    admission_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    v2_resolution: dict[str, Any] | None = None
    expansion: dict[str, Any] | None = None
    admitted_entries: list[_AdmittedSignalItem] = field(default_factory=list)


@dataclass
class _AdmittedSignalItem:
    """One admitted candidate item and the context its ranking consumed."""

    item: MemoryItem
    item_dict: dict[str, Any]
    # Direct semantic similarity; None for items reached only through
    # relationship expansion.
    similarity: float | None
    distance: float | None
    created_ts: float


async def _admit_and_rank_signal_items(
    session: AsyncSession,
    *,
    profile: RecallProfileSpec,
    memory_context: ResolvedMemoryContext,
    workspace_id: str | None,
    candidates: list[dict[str, Any]],
    item_by_id: dict[UUID, MemoryItem],
    stay_kinds: set[str],
    now: datetime,
    demonstrated_usefulness_enabled: bool = True,
) -> SignalAdmissionOutcome:
    """V2-bound admission + separated-signal ranking (issues #160 / #186 / #190).

    Admission runs on the retrieved candidate window — after relevance
    retrieval (whose SQL already excluded mechanically-ineligible rows — see
    :func:`_signal_corpus_eligibility`) and before ranking and packing, so
    nothing ineligible can enter the packet through a side door. The
    positive admission authority is the exact #158 ``risk_aware_shadow_v1``
    per-surface decision, resolved for the whole window by the shared bulk
    resolver (``admission_shadow.resolve_bulk_v2_decisions`` — the same
    evaluation the #158 simulator runs). Recall-local rules (the #159
    blocked/stale binding, lifecycle facts) can only withhold.

    Since issue #190 the same ordering governs relationship expansion, which
    runs strictly between direct admission and ranking:

    1. exact V2 admission on the direct candidates;
    2. seeds are chosen only from admitted direct candidates;
    3. bounded graph/tunnel neighbor discovery under tenant/scope/RLS and
       the same live-proposal corpus window;
    4. exact V2 admission on every expanded neighbor — seed admission never
       transfers;
    5. relationship-aware relevance (versioned, importance-free) feeds the
       separated utility ranking; the conflict/diversity-aware packing stage
       (issue #192) then selects among the finished admitted/ranked list.

    Withheld items (direct and expanded) are counted by reason code
    (``omitted_by_admission``) and itemized content-free in
    ``admission_diagnostics`` with their origin.
    """
    items = list(item_by_id.values())
    bindings = await recall_signals.load_admission_bindings(
        session, tenant_id=str(memory_context.tenant_id), items=items
    )
    resolution = None
    v2_summary: dict[str, Any] | None = None
    if profile.v2_surface is not None and items:
        from engram.admission_shadow import resolve_bulk_v2_decisions

        resolution = await resolve_bulk_v2_decisions(
            session,
            items=items,
            context=memory_context,
            evaluation_time=now,
        )
        v2_summary = resolution.summary()

    admitted: list[_AdmittedSignalItem] = []
    omitted: dict[str, int] = {}
    diagnostics: list[dict[str, Any]] = []
    for cand in candidates:
        item = item_by_id.get(UUID(cand["id"]))
        if item is None:
            # Stale embedding whose item disappeared — skip.
            continue
        resolved_v2 = resolution.items.get(item.id) if resolution is not None else None
        decision = recall_signals.decide_recall_admission(
            item,
            profile=profile,
            stay_kinds=stay_kinds,
            assessment=bindings.get(item.id),
            v2_resolution=resolved_v2,
        )
        if decision.decision == "withhold":
            code = decision.reason_codes[0]
            omitted[code] = omitted.get(code, 0) + 1
            diagnostics.append(
                _admission_diagnostic(
                    item,
                    profile=profile,
                    decision=decision,
                    assessment=bindings.get(item.id),
                )
            )
            continue

        distance = float(cand.get("distance", 0.0))
        similarity = float(cand.get("similarity_score", max(0.0, 1.0 - distance)))
        item_dict: dict[str, Any] = _semantic_base_item_fields(
            item, distance=distance, similarity=similarity
        )
        # score/reasons/warnings + relevance/utility/epistemic/admission
        # fields, all produced by the separated signal model. No blended
        # trust_score exists on this path.
        item_dict.update(
            recall_signals.signal_item_fields(
                item, decision=decision, similarity=similarity, now=now
            )
        )
        created = cand.get("created_at")
        created_ts = created.timestamp() if created is not None else 0.0
        admitted.append(
            _AdmittedSignalItem(
                item=item,
                item_dict=item_dict,
                similarity=similarity,
                distance=distance,
                created_ts=created_ts,
            )
        )

    # Admission-first relationship expansion (issue #190). Only V2-bound
    # candidate profiles expand — the legacy profile keeps its own
    # compatibility expansion path in evaluate_semantic_profile below.
    expansion_summary: dict[str, Any] | None = None
    if settings.relationship_expansion_enabled and profile.v2_surface is not None and admitted:
        expansion_run = await _expand_signal_candidates(
            session,
            profile=profile,
            memory_context=memory_context,
            workspace_id=workspace_id,
            admitted=admitted,
            direct_candidate_ids=set(item_by_id),
            stay_kinds=stay_kinds,
            now=now,
        )
        admitted = expansion_run.admitted
        for code, count in expansion_run.omitted_by_admission.items():
            omitted[code] = omitted.get(code, 0) + count
        diagnostics.extend(expansion_run.admission_diagnostics)
        expansion_summary = expansion_run.expansion
        if expansion_run.v2_resolution is not None:
            assert v2_summary is not None  # the direct window resolved above
            v2_summary = _merge_v2_resolution_summaries(v2_summary, expansion_run.v2_resolution)

    # Utility is evaluated once, strictly after the complete admitted set is
    # known. The bulk loader cannot observe or rescue withheld candidates;
    # it only contributes a bounded ordering adjustment to these entries.
    from engram.demonstrated_usefulness import load_demonstrated_usefulness

    if demonstrated_usefulness_enabled:
        usefulness_by_item = await load_demonstrated_usefulness(
            session,
            tenant_id=memory_context.tenant_id,
            items=[entry.item for entry in admitted],
        )
    else:
        from engram.demonstrated_usefulness import summarize_demonstrated_usefulness_counts

        usefulness_by_item = {
            entry.item.id: summarize_demonstrated_usefulness_counts(useful_count=0, noise_count=0)
            for entry in admitted
        }
    for entry in admitted:
        recall_signals.apply_demonstrated_usefulness(
            entry.item_dict,
            item=entry.item,
            now=now,
            usefulness=usefulness_by_item[entry.item.id],
        )

    # Deterministic order: signal rank desc, then closer vector (direct hits
    # ahead of expansion-only items at equal rank), then newer, then id.
    admitted.sort(
        key=lambda entry: (
            -entry.item_dict["score"],
            entry.distance if entry.distance is not None else math.inf,
            -entry.created_ts,
            entry.item_dict["id"],
        )
    )
    if expansion_summary is not None:
        # The expansion path's ceiling — the same bound (and the same
        # "applies whenever the expansion path runs, discoveries or not")
        # the legacy expansion path applies after rescoring.
        admitted = admitted[: settings.recall_candidate_ceiling]
    return SignalAdmissionOutcome(
        items=[entry.item_dict for entry in admitted],
        omitted_by_admission=omitted,
        admission_diagnostics=diagnostics,
        v2_resolution=v2_summary,
        expansion=expansion_summary,
        admitted_entries=admitted,
    )


def _merge_v2_resolution_summaries(
    primary: dict[str, Any], neighbor: dict[str, Any]
) -> dict[str, Any]:
    """Combine the direct-window and expanded-neighbor resolution summaries.

    Both resolutions ran the same policy artifact (same profile key/version/
    digest), so the merged summary simply totals the windows: counts and
    queries add, status counts merge — the packet-level evidence that the
    expanded neighbors were resolved by the same shared bulk resolver, never
    a second policy or a per-neighbor lookup.
    """
    status_counts: dict[str, int] = dict(primary["resolution_status_counts"])
    for status, count in neighbor["resolution_status_counts"].items():
        status_counts[status] = status_counts.get(status, 0) + count
    return {
        **primary,
        "resolved_count": primary["resolved_count"] + neighbor["resolved_count"],
        "resolution_status_counts": dict(sorted(status_counts.items())),
        "query_count": primary["query_count"] + neighbor["query_count"],
    }


def _expansion_origin(
    item_id: UUID, discovery: relationship_recall.CandidateNeighborDiscovery
) -> str:
    """Which expansion surface(s) reached this neighbor — the diagnostic
    category that keeps direct withholds distinguishable from graph-,
    tunnel-, and graph+tunnel-expanded withholds (issue #190).

    Callers only pass ids that came out of the discovery maps, so the
    neither-map case is a contract break, not a policy outcome — fail loudly
    rather than mislabel a withhold.
    """
    in_graph = item_id in discovery.graph_links
    in_tunnel = item_id in discovery.tunnel_links
    if in_graph and in_tunnel:
        return "graph+tunnel"
    if in_graph:
        return "graph"
    if in_tunnel:
        return "tunnel"
    raise ValueError(f"expansion origin requested for an undiscovered item: {item_id}")


def _relationship_reason_lines(
    discovery: relationship_recall.CandidateNeighborDiscovery, item_id: UUID
) -> list[str]:
    """Human-readable relationship reasons, deterministic in output order."""
    lines: list[str] = []
    for link in sorted(
        discovery.graph_links.get(item_id, []),
        key=lambda link: (-link.weight, link.edge_type, str(link.neighbor_id)),
    ):
        reason = f"linked via {link.edge_type}"
        if reason not in lines:
            lines.append(reason)
    for tlink in sorted(
        discovery.tunnel_links.get(item_id, []),
        key=lambda link: (link.tunnel_label, str(link.neighbor_id)),
    ):
        reason = f'same tunnel "{tlink.tunnel_label}"'
        if reason not in lines:
            lines.append(reason)
    return lines


def _candidate_relevance(
    *,
    item_id: UUID,
    similarity: float | None,
    discovery: relationship_recall.CandidateNeighborDiscovery,
    seed_similarity: dict[UUID, float],
) -> relationship_recall.RelationshipRelevance:
    """Relationship-aware relevance for one linked candidate item.

    Source-seed attribution: graph and tunnel links alike carry the exact
    admitted seed that reached the item (graph: the edge's seed endpoint;
    tunnel: a seed whose tunnel membership exposed the (wing, room) the item
    was pulled from), so ``source_seed_score`` is the strongest similarity
    among the seeds that actually reached it — an unrelated stronger seed in
    the packet contributes nothing. Both are relevance inputs only: nothing
    here touches admission or evidence state.
    """
    graph_links = discovery.graph_links.get(item_id, [])
    tunnel_links = discovery.tunnel_links.get(item_id, [])
    graph_seed_scores = [
        seed_similarity[link.seed_id] for link in graph_links if link.seed_id in seed_similarity
    ]
    tunnel_seed_scores = [
        seed_similarity[link.seed_id] for link in tunnel_links if link.seed_id in seed_similarity
    ]
    source_seed_score = max(graph_seed_scores + tunnel_seed_scores, default=0.0)
    return relationship_recall.compute_relationship_relevance(
        direct_semantic_score=similarity,
        source_seed_score=source_seed_score,
        graph_links=[(link.edge_type, link.weight) for link in graph_links],
        tunnel_labels=[link.tunnel_label for link in tunnel_links],
    )


@dataclass
class _SignalExpansionOutcome:
    """What one admission-first expansion run added to a signal packet."""

    admitted: list[_AdmittedSignalItem]
    omitted_by_admission: dict[str, int]
    admission_diagnostics: list[dict[str, Any]]
    v2_resolution: dict[str, Any] | None
    expansion: dict[str, Any]


async def _expand_signal_candidates(
    session: AsyncSession,
    *,
    profile: RecallProfileSpec,
    memory_context: ResolvedMemoryContext,
    workspace_id: str | None,
    admitted: list[_AdmittedSignalItem],
    direct_candidate_ids: set[UUID],
    stay_kinds: set[str],
    now: datetime,
) -> _SignalExpansionOutcome:
    """Admission-first graph/tunnel expansion for one V2-bound packet.

    ``admitted`` are the direct candidates the exact V2 surface already
    admitted, in retrieval order (the final signal-rank sort runs after
    expansion) — only they may seed discovery. The whole direct candidate
    window is passed as the enrichment set: already-evaluated direct items
    linked to an admitted seed gain graph/tunnel origin metadata (no second
    admission, no capacity cost), so a direct candidate — including one the
    exact V2 surface withheld on the direct path — can never occupy a
    bounded expansion slot. Newly discovered neighbors (outside that
    window) are resolved through the same shared bulk resolver in ONE
    bounded call and admitted through the same ``decide_recall_admission``
    gate: no per-neighbor lookup, no provider call, no second policy. Every
    linked admitted item — enriched direct items and admitted neighbors
    alike — is re-scored through the versioned relationship-relevance
    contract (``relationship-relevance-v1``) feeding the unchanged
    separated-utility rank; utility (importance/freshness) is computed by
    the signal model exactly as for direct items and never enters relevance.
    """
    seeds = admitted[: settings.recall_semantic_expansion_seed_limit]
    seed_ids = [entry.item.id for entry in seeds]
    seed_similarity = {
        entry.item.id: entry.similarity for entry in seeds if entry.similarity is not None
    }

    discovery = await relationship_recall.discover_candidate_neighbors(
        session,
        memory_context=memory_context,
        workspace_id=workspace_id,
        seed_ids=seed_ids,
        seed_items=[entry.item for entry in seeds],
        exclude_ids=direct_candidate_ids,
        enrichment_ids=direct_candidate_ids,
    )

    new_neighbor_ids = (set(discovery.graph_links) | set(discovery.tunnel_links)) - set(seed_ids)
    new_neighbor_ids -= direct_candidate_ids

    omitted: dict[str, int] = {}
    diagnostics: list[dict[str, Any]] = []
    neighbor_summary: dict[str, Any] | None = None
    new_entries: list[_AdmittedSignalItem] = []
    neighbor_items = [
        discovery.neighbor_items[item_id]
        for item_id in sorted(new_neighbor_ids)
        if item_id in discovery.neighbor_items
    ]
    if neighbor_items:
        # One bounded bulk resolution for the whole newly discovered neighbor
        # set (issue #190's no-N+1 contract): support + selection + one
        # latest-row lookup, query count constant in neighbor count.
        neighbor_bindings = await recall_signals.load_admission_bindings(
            session, tenant_id=str(memory_context.tenant_id), items=neighbor_items
        )
        from engram.admission_shadow import resolve_bulk_v2_decisions

        neighbor_resolution = await resolve_bulk_v2_decisions(
            session,
            items=neighbor_items,
            context=memory_context,
            evaluation_time=now,
        )
        neighbor_summary = neighbor_resolution.summary()
        for item in neighbor_items:
            decision = recall_signals.decide_recall_admission(
                item,
                profile=profile,
                stay_kinds=stay_kinds,
                assessment=neighbor_bindings.get(item.id),
                v2_resolution=neighbor_resolution.items.get(item.id),
            )
            if decision.decision == "withhold":
                code = decision.reason_codes[0]
                omitted[code] = omitted.get(code, 0) + 1
                diagnostics.append(
                    _admission_diagnostic(
                        item,
                        profile=profile,
                        decision=decision,
                        assessment=neighbor_bindings.get(item.id),
                        origin=_expansion_origin(item.id, discovery),
                    )
                )
                continue
            relevance = _candidate_relevance(
                item_id=item.id,
                similarity=None,
                discovery=discovery,
                seed_similarity=seed_similarity,
            )
            item_dict = _semantic_base_item_fields(item, distance=None, similarity=None)
            item_dict.update(
                recall_signals.signal_item_fields(
                    item, decision=decision, now=now, relevance=relevance.relevance_score
                )
            )
            item_dict["reasons"].extend(_relationship_reason_lines(discovery, item.id))
            item_dict["relationship"] = relevance.payload()
            new_entries.append(
                _AdmittedSignalItem(
                    item=item,
                    item_dict=item_dict,
                    similarity=None,
                    distance=None,
                    created_ts=(
                        item.created_at.timestamp() if item.created_at is not None else 0.0
                    ),
                )
            )

    # Origin-merging enrichment: an admitted direct item (seed or not) that
    # discovery linked keeps its direct fields (distance/similarity,
    # admission, evidence — all unchanged) and gains the structured
    # relationship block plus the merged relevance. Links can only raise a
    # direct hit's relevance (max floor in the relevance contract), never
    # demote it or touch its admission/evidence identity.
    for entry in admitted:
        linked = entry.item.id in discovery.graph_links or entry.item.id in discovery.tunnel_links
        if not linked:
            continue
        relevance = _candidate_relevance(
            item_id=entry.item.id,
            similarity=entry.similarity,
            discovery=discovery,
            seed_similarity=seed_similarity,
        )
        entry.item_dict["relevance_score"] = relevance.relevance_score
        entry.item_dict["score"] = recall_signals.compute_signal_rank_score(
            similarity=relevance.relevance_score, utility=entry.item_dict["utility_score"]
        )
        # Refresh the relevance reason line signal_item_fields built —
        # matched by its prefix rather than position so a reason reorder
        # upstream can never rewrite the wrong line.
        reasons = entry.item_dict["reasons"]
        relevance_reason = f"relevance {relevance.relevance_score:.2f}"
        for index, reason in enumerate(reasons):
            if reason.startswith("relevance "):
                reasons[index] = relevance_reason
                break
        else:
            reasons.insert(0, relevance_reason)
        entry.item_dict["reasons"].extend(_relationship_reason_lines(discovery, entry.item.id))
        entry.item_dict["relationship"] = relevance.payload()

    # Accounting describes genuinely new expansion candidates only: every
    # count below excludes the already-evaluated direct window (seeds and
    # withheld direct candidates alike), which received enrichment links at
    # zero capacity cost and is never re-admitted here.
    expansion_summary = {
        "version": relationship_recall.RELATIONSHIP_RELEVANCE_VERSION,
        "seed_count": len(seeds),
        "discovered_neighbors": len(new_neighbor_ids),
        "graph_neighbors": len(set(discovery.graph_links) - direct_candidate_ids),
        "tunnel_neighbors": len(set(discovery.tunnel_links) - direct_candidate_ids),
        "admitted_expanded": len(new_entries),
        "withheld_expanded": len(diagnostics),
    }
    return _SignalExpansionOutcome(
        admitted=admitted + new_entries,
        omitted_by_admission=omitted,
        admission_diagnostics=diagnostics,
        v2_resolution=neighbor_summary,
        expansion=expansion_summary,
    )


def _admission_diagnostic(
    item: MemoryItem,
    *,
    profile: RecallProfileSpec,
    decision: recall_signals.RecallAdmissionDecision,
    assessment: recall_signals.AdmissionAssessmentBinding | None,
    origin: str = "direct",
) -> dict[str, Any]:
    """One bounded, content-free withheld-candidate diagnostic.

    Identity and codes only: no content, no provider output. The item is
    already read-eligible and relevance-retrieved for this caller, so its id
    and reason codes leak nothing the caller cannot already see. The V2
    facts come straight from the decision's own binding — the full safe
    ``v2`` block (persisted and fresh identity, exact surface decision,
    bounded state and code sets), the same builder admitted items carry, so
    an operator can audit exactly which V2 decision was (or would have been)
    consumed without any hidden implementation state.
    ``gates_disagree`` is the bounded mismatch diagnostic of issue #186 —
    true exactly when a *current* V2 decision and the recall-local hard gate
    would decide differently (unavailable V2 state is fail-closed
    unavailability, not a disagreement; the local hard gate is withhold-only,
    so "local would admit" means the lifecycle/#159 layers had no objection).
    ``origin`` names what surfaced the candidate — ``direct`` for the
    semantic window, ``graph`` / ``tunnel`` / ``graph+tunnel`` for items
    reached through relationship expansion (issue #190), keeping the
    withholds distinguishable for #162 evaluation.
    """
    binding = decision.v2
    v2_status = binding.resolution_status if binding is not None else "missing"
    surface_decision = binding.surface_decision if binding is not None else None
    local_would_admit = (
        recall_signals.v2_local_gate_withhold_reason(item, profile=profile, assessment=assessment)
        is None
    )
    v2_allows = v2_status == "current" and surface_decision == "allow"
    return {
        "item_id": str(item.id),
        "profile": profile.key,
        "origin": origin,
        "decision": decision.decision,
        "reason_codes": list(decision.reason_codes),
        "surface": profile.v2_surface,
        "v2_resolution_status": v2_status,
        "v2_surface_decision": surface_decision,
        "gates_disagree": v2_status == "current" and (local_would_admit != v2_allows),
        "v2": binding.payload() if binding is not None else None,
    }


@dataclass
class SemanticPacketEvaluation:
    """One profile's complete packet evaluation — read-only, no side effects.

    Shared by authoritative serving (``execute_semantic_recall``, which adds
    the recall_logs audit row and exposure-counter telemetry around it) and
    the shadow comparison surface (``engram.recall_shadow``, which adds
    nothing). Keeping the evaluation free of writes is what makes "candidate
    profiles may be evaluated but never served" a structural property rather
    than a convention.
    """

    profile: RecallProfileSpec
    items: list[dict[str, Any]]
    working_set: str
    candidate_count: int
    omitted_by_admission: dict[str, int]
    item_count: int = 0
    byte_count: int = 0
    byte_budget: int | None = None
    token_budget: int | None = None
    item_budget: int | None = None
    # V2-bound admission context (issue #186): bounded content-free
    # diagnostics for withheld candidates, and the resolution summary.
    admission_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    v2_resolution: dict[str, Any] | None = None
    # Admission-first relationship-expansion context (issue #190): the
    # bounded summary of the expansion run (contract version, seed/neighbor/
    # admission counts). None on the legacy profile and on packets that did
    # not expand.
    expansion: dict[str, Any] | None = None
    # Conflict-preserving, diversity-aware packing context (issue #192): the
    # bounded ``recall-packing-v1`` summary (version, selected count,
    # preserved conflict pairs, omission counts by reason). Present on every
    # signal-profile packet that reached the admission step (as a zero-count
    # summary when nothing was admitted); None on the legacy profile and on
    # the empty-corpus early return, which never reach packing.
    packing: dict[str, Any] | None = None

    def finalize_counts(self) -> None:
        self.item_count = len(self.items)
        self.byte_count = sum(len(item["content"].encode()) for item in self.items)


async def generate_query_embedding(
    query: str,
    *,
    embedding_profile: Any,
    tenant_id: str,
    principal_id: str,
) -> list[float] | None:
    """Generate the query embedding, honoring provider-capable signatures.

    Shared by authoritative serving and the shadow comparison surface so both
    evaluate packets for the identical query vector.
    """
    import inspect

    from engram.provider_observer import record_provider_invocation

    # This is the shared production boundary for semantic recall query
    # embeddings. Recording here also observes test/runtime adapters that
    # replace ``generate_embedding`` below.
    record_provider_invocation("semantic_query_embedding")

    if len(inspect.signature(generate_embedding).parameters) >= 2:
        return await generate_embedding(
            query,
            embedding_profile,
            tenant_id=tenant_id,
            principal_id=principal_id,
            operation="embedding_query_recall",
            usage_class="request",
        )
    return await generate_embedding(query)


async def _profile_candidate_count(
    session: AsyncSession,
    *,
    memory_context: ResolvedMemoryContext,
    workspace_id: str | None,
    profile: RecallProfileSpec,
    stay_kinds: set[str],
    embedding_profile: Any,
) -> int:
    """Count one profile's eligible corpus under its exact retrieval predicate.

    Single source of truth for "how large is this profile's eligible corpus":
    both the packet evaluation and the shadow comparison's embedding
    preflight call it, so the count and the retrieved window can never
    disagree about eligibility.
    """
    if profile.signals_enabled:
        return await semantic.candidate_count(
            session,
            memory_context=memory_context,
            workspace_id=workspace_id,
            review_statuses=None,
            corpus_eligibility=_signal_corpus_eligibility(profile, stay_kinds),
            embedding_profile=embedding_profile,
        )
    return await semantic.candidate_count(
        session,
        memory_context=memory_context,
        workspace_id=workspace_id,
        review_statuses=profile.review_statuses,
        embedding_profile=embedding_profile,
    )


def _packing_candidates(
    entries: list[_AdmittedSignalItem],
) -> list[recall_packing.PackCandidate]:
    """Project ranked admitted entries into the pure packer's input shape.

    Only the durable identity facts the packing contract consumes — content
    (for rendered budget accounting) and ``conflicts_with_item_id`` — cross
    this boundary; scores, admission, evidence, relationship metadata, and
    content-identity facts such as ``content_hash`` stay behind, where
    packing cannot touch them (canonical content equality is not a v1 root
    signal). List position carries the rank exactly as the admission step
    sorted it.
    """
    return [
        recall_packing.PackCandidate(
            item_id=entry.item.id,
            content=entry.item_dict["content"],
            conflicts_with=entry.item.conflicts_with_item_id,
        )
        for entry in entries
    ]


async def evaluate_semantic_profile(
    session: AsyncSession,
    *,
    memory_context: ResolvedMemoryContext,
    workspace_id: str | None,
    profile: RecallProfileSpec,
    query_embedding: list[float],
    embedding_profile: Any,
    stay_kinds: set[str],
    byte_budget: int | None,
    token_budget: int | None,
    item_budget: int | None,
    now: datetime,
    demonstrated_usefulness_enabled: bool = True,
) -> SemanticPacketEvaluation:
    """Evaluate one profile's packet for a query embedding, writing nothing.

    Corpus eligibility, retrieval, admission, ranking, and budget packing for
    one profile. The legacy profile keeps its pre-#160 behavior byte-for-byte
    (trust-weighted retrieval via ``semantic.search`` plus relationship
    expansion); signal profiles retrieve through the neutral
    ``semantic.retrieve_candidates`` primitive with their full eligibility
    predicate applied before the bounded HNSW window, then run the admission
    gate and separated-signal ranking.
    """
    byte_budget, token_budget, item_budget = apply_profile_budget_caps(
        profile, byte_budget, token_budget, item_budget
    )

    # 1. Count the eligible corpus under the exact predicate retrieval uses.
    candidate_total = await _profile_candidate_count(
        session,
        memory_context=memory_context,
        workspace_id=workspace_id,
        profile=profile,
        stay_kinds=stay_kinds,
        embedding_profile=embedding_profile,
    )

    empty = SemanticPacketEvaluation(
        profile=profile,
        items=[],
        working_set="",
        candidate_count=candidate_total,
        omitted_by_admission={},
        byte_budget=byte_budget,
        token_budget=token_budget,
        item_budget=item_budget,
    )
    if candidate_total == 0:
        return empty

    # 2. Retrieve nearest candidates. item_budget is already resolved (and
    #    profile-capped) above.
    item_limit = item_budget if item_budget is not None else settings.recall_item_budget
    fetch_limit = min(item_limit * _SEMANTIC_OVERFETCH, _SEMANTIC_OVERFETCH_CAP)
    if profile.signals_enabled:
        candidates = await semantic.retrieve_candidates(
            session,
            query_embedding,
            fetch_limit,
            memory_context=memory_context,
            workspace_id=workspace_id,
            corpus_eligibility=_signal_corpus_eligibility(profile, stay_kinds),
            embedding_profile=embedding_profile,
        )
    else:
        candidates = await semantic.search(
            session,
            query_embedding,
            fetch_limit,
            memory_context=memory_context,
            workspace_id=workspace_id,
            review_statuses=profile.review_statuses,
            embedding_profile=embedding_profile,
        )

    # 3. Enrich candidates with full MemoryItem trust fields (pinned,
    #    importance, source_trust, memory_confidence, human_verified).
    #    Ids already passed the eligibility-filtered retrieval above; the
    #    read-eligibility filter here is cheap defense in depth.
    candidate_ids = [UUID(c["id"]) for c in candidates]
    item_by_id: dict[UUID, MemoryItem] = {}
    if candidate_ids:
        rows = await session.execute(
            select(MemoryItem).where(
                MemoryItem.id.in_(candidate_ids),
                read_eligibility_expression(memory_context),
            )
        )
        item_by_id = {item.id: item for item in rows.scalars().all()}

    omitted_by_admission: dict[str, int] = {}
    admission_diagnostics: list[dict[str, Any]] = []
    v2_resolution_summary: dict[str, Any] | None = None
    expansion_summary: dict[str, Any] | None = None
    signal_admission: SignalAdmissionOutcome | None = None
    if profile.signals_enabled:
        # 4. V2-bound admission + separated-signal ranking (issues #160/#186),
        #    with admission-first relationship expansion (issue #190): only
        #    direct candidates the exact V2 surface admitted may seed bounded
        #    graph/tunnel discovery, every expanded neighbor is independently
        #    admitted through the same surface, and relationship-aware
        #    relevance (versioned, importance-free) feeds the separated
        #    utility ranking. Packing below (issue #192) consumes that
        #    finished admitted/ranked list.
        signal_admission = await _admit_and_rank_signal_items(
            session,
            profile=profile,
            memory_context=memory_context,
            workspace_id=workspace_id,
            candidates=candidates,
            item_by_id=item_by_id,
            stay_kinds=stay_kinds,
            now=now,
            demonstrated_usefulness_enabled=demonstrated_usefulness_enabled,
        )
        enriched = signal_admission.items
        omitted_by_admission = signal_admission.omitted_by_admission
        admission_diagnostics = signal_admission.admission_diagnostics
        v2_resolution_summary = signal_admission.v2_resolution
        expansion_summary = signal_admission.expansion
    else:
        # 4. Build per-item response dicts in trust-weighted order (legacy
        #    profile — pre-#160 behavior, byte-for-byte). The candidate dicts
        #    already carry the trust-weighted semantic score, similarity, and
        #    trust blend computed by engram.semantic; we add the MemoryItem
        #    fields (pinned, etc.) needed by callers.
        enriched = []
        for cand in candidates:
            item = item_by_id.get(UUID(cand["id"]))
            if item is None:
                # Stale embedding whose item disappeared — skip.
                continue
            distance = float(cand.get("distance", 0.0))
            similarity = float(cand.get("similarity_score", 1.0 - distance))
            trust_score = float(cand.get("trust_score", 1.0))
            semantic_score = float(cand.get("score", similarity * trust_score))
            warnings: list[str] = []
            if item.review_status == "proposed":
                warnings.append("unreviewed")
            item_dict = _semantic_base_item_fields(item, distance=distance, similarity=similarity)
            item_dict.update(
                {
                    "score": round(semantic_score, 4),
                    "trust_score": round(trust_score, 4),
                    "reasons": [
                        f"semantic similarity {similarity:.2f}",
                        f"trust_score={trust_score:.2f}",
                        f"cosine_distance={distance:.4f}",
                    ],
                    "warnings": warnings,
                }
            )
            enriched.append(item_dict)

        # 4b. Relationship-aware expansion (ENG-AUD-012 / F19): graph (depth-1,
        #     bounded) then tunnel (bounded) expansion of the top semantic
        #     candidates, merged and rescored — semantic relevance still
        #     dominates the blended score (see engram.relationship_recall).
        #     Runs before budget packing so expanded memories compete for
        #     budget on equal footing with direct semantic hits; never
        #     bypasses eligibility. Legacy profile only (see above).
        enriched = await expand_recall_candidates(
            session,
            memory_context=memory_context,
            workspace_id=workspace_id,
            semantic_items=enriched,
            item_by_id=item_by_id,
            now=now,
        )

    # 5. Pack to the hard item/byte/token budgets. Signal profiles use the
    #    versioned conflict-preserving, diversity-aware packer (issue #192):
    #    known-root representatives outrank redundant siblings and explicit
    #    conflict pairs are co-packed when budgets permit — selection only,
    #    never a change to any candidate's scores, admission, or evidence.
    #    The legacy profile keeps its byte-for-byte rank-then-truncate
    #    behavior untouched.
    packing_summary: dict[str, Any] | None = None
    if signal_admission is not None:
        entries = signal_admission.admitted_entries
        relations = await recall_packing.load_packing_relations(
            session,
            tenant_id=memory_context.tenant_id,
            item_ids={entry.item.id for entry in entries},
        )
        packing = recall_packing.pack_admitted_candidates(
            _packing_candidates(entries),
            relations=relations,
            byte_budget=byte_budget,
            token_budget=token_budget,
            item_budget=item_budget,
        )
        selected_ids = set(packing.selected)
        selected = []
        for entry in entries:
            if entry.item.id not in selected_ids:
                continue
            entry.item_dict["packing_reason"] = packing.reasons[entry.item.id]
            selected.append(entry.item_dict)
        packing_summary = packing.summary()
    else:
        selected = _enforce_semantic_budget(
            enriched,
            byte_budget=byte_budget,
            token_budget=token_budget,
            item_budget=item_budget,
        )

    working_set = "\n".join(f"[{item['kind']}] {item['content']}" for item in selected)
    evaluation = SemanticPacketEvaluation(
        profile=profile,
        items=selected,
        working_set=working_set,
        candidate_count=candidate_total,
        omitted_by_admission=omitted_by_admission,
        byte_budget=byte_budget,
        token_budget=token_budget,
        item_budget=item_budget,
        admission_diagnostics=admission_diagnostics,
        v2_resolution=v2_resolution_summary,
        expansion=expansion_summary,
        packing=packing_summary,
    )
    evaluation.finalize_counts()
    return evaluation


async def execute_semantic_recall(
    session: AsyncSession,
    memory_context: ResolvedMemoryContext,
    workspace: str | None,
    query: str,
    *,
    byte_budget: int | None,
    token_budget: int | None,
    item_budget: int | None,
    recall_profile: str | None = None,
) -> dict[str, Any]:
    """Execute semantic recall and return the response dict.

    Owns the query-embedding generation flow end to end.

    Serving authority (issue #160 rollout boundary): the served packet is
    always produced by a profile certified in
    ``recall_profiles.CERTIFIED_SERVING_PROFILES`` — ``legacy`` until accepted
    #162 certification records otherwise. An explicitly requested uncertified
    profile (``governed``/``exploratory``) raises
    ``RecallProfileNotServableError`` (the route maps it to HTTP 422), and an
    uncertified ``settings.recall_default_profile`` is refused with a warning
    rather than honored. Candidate profiles are evaluated only by the
    read-only shadow comparison surface (``engram.recall_shadow``).

    When embeddings are unavailable (provider=none) or the corpus has no
    candidates, returns an empty working set with a helpful message rather
    than raising — and still writes a recall_logs audit row.
    """
    now = datetime.now(UTC)
    tenant_id = str(memory_context.tenant_id)
    principal_id = str(memory_context.principal_id)
    config = await _get_tenant_config(session, tenant_id)

    serving = resolve_serving_profile(
        recall_profile, mode="semantic", default=settings.recall_default_profile
    )
    profile = serving.spec
    if serving.refused_default is not None:
        # Configuration alone can never promote an uncertified profile into
        # production authority; make the refusal visible instead of silent.
        logger.warning(
            "recall_default_profile_refused tenant=%s requested=%s serving=legacy "
            "(profile not certified for serving; see docs/adr-160-recall-profiles.md)",
            tenant_id,
            serving.refused_default,
        )

    # Apply configured defaults for omitted budgets so semantic recall is
    # bounded by default (no API-documented way to request unbounded recall).
    byte_budget, token_budget, item_budget = _resolve_recall_budgets(
        byte_budget=byte_budget,
        token_budget=token_budget,
        item_budget=item_budget,
    )

    # Resolve workspace_id if provided. An explicit workspace request that
    # doesn't resolve, or where the caller isn't a member, must not fall back
    # to an unscoped read — it yields zero candidates instead.
    workspace_id, workspace_accessible = await resolve_workspace_scope(
        session, memory_context=memory_context, workspace=workspace
    )

    # 1. Resolve the embedding profile and count the eligible corpus before
    #    calling the provider. Empty/denied contexts are a truthful
    #    ``not_attempted`` embedding outcome.
    from engram.embedding_profiles import get_active_profile

    embedding_profile = await get_active_profile(session)
    if not memory_context.may_read_anything or (
        workspace is not None and not workspace_accessible
    ):
        candidate_total = 0
    else:
        candidate_total = await semantic.candidate_count(
            session,
            memory_context=memory_context,
            workspace_id=workspace_id,
            review_statuses=profile.review_statuses,
            embedding_profile=embedding_profile,
        )

    query_embedding: list[float] | None = None
    embedding_outcome = "not_attempted"
    if candidate_total > 0:
        query_embedding = await generate_query_embedding(
            query,
            embedding_profile=embedding_profile,
            tenant_id=tenant_id,
            principal_id=principal_id,
        )
        embedding_outcome = "succeeded" if query_embedding is not None else "disabled"

    if query_embedding is None or candidate_total == 0:
        # Empty, non-error response. Still log the attempt for auditability.
        config_version = config.config_version if config is not None else "v1"
        recall_log = RecallLog(
            tenant_id=tenant_id,
            principal_id=principal_id,
            mode="semantic",
            query=query,
            byte_budget=byte_budget,
            token_budget=token_budget,
            item_budget=item_budget,
            item_ids=[],
            scoring_version=profile.ranking_version,
            config_version=config_version,
            recall_profile=profile.key,
            memory_profile_id=memory_context.memory_profile_id,
            memory_profile_revision_id=memory_context.memory_profile_revision_id,
            memory_context_version=memory_context.version,
        )
        session.add(recall_log)
        await session.commit()
        empty_evaluation = SemanticPacketEvaluation(
            profile=profile,
            items=[],
            working_set="",
            candidate_count=candidate_total,
            omitted_by_admission={},
            byte_budget=byte_budget,
            token_budget=token_budget,
            item_budget=item_budget,
        )
        empty_evaluation.finalize_counts()
        return {
            "working_set": "",
            "item_count": 0,
            "byte_count": 0,
            "pinned_omitted_count": 0,
            "omitted_count": 0,
            "items": [],
            "scoring_version": profile.ranking_version,
            "config_version": config_version,
            "recall_log_id": str(recall_log.id),
            "message": _NO_EMBEDDINGS_MESSAGE,
            # Telemetry context (ENG-METER-001).
            "workspace_id": str(workspace_id) if workspace_id else None,
            "candidate_count": 0,
            "embedding_outcome": embedding_outcome,
            # Profile context (issue #160).
            "recall_profile": profile.key,
            "signals_version": (
                recall_signals.SIGNALS_VERSION if profile.signals_enabled else None
            ),
            "omitted_by_admission": {},
            # Receipt-only finalized packet handoff. This is not exposed by
            # RecallResponse and avoids replaying any semantic work.
            "_semantic_evaluation": empty_evaluation,
            "effective_byte_budget": byte_budget,
            "effective_token_budget": token_budget,
            "effective_item_budget": item_budget,
        }

    # 2. Evaluate the certified (legacy) packet — read-only core; the audit
    #    row and exposure telemetry below are the serving side effects.
    evaluation = await evaluate_semantic_profile(
        session,
        memory_context=memory_context,
        workspace_id=workspace_id,
        profile=profile,
        query_embedding=query_embedding,
        embedding_profile=embedding_profile,
        stay_kinds=set(),
        byte_budget=byte_budget,
        token_budget=token_budget,
        item_budget=item_budget,
        now=now,
    )

    # 3. Write recall_logs (mode='semantic', query populated). The effective
    #    profile and its ranking version are recorded for audit
    #    reproducibility (issue #160).
    config_version = config.config_version if config is not None else "v1"
    selected_ids = [UUID(item["id"]) for item in evaluation.items]
    recall_log = RecallLog(
        tenant_id=tenant_id,
        principal_id=principal_id,
        mode="semantic",
        query=query,
        byte_budget=byte_budget,
        token_budget=token_budget,
        item_budget=evaluation.item_budget,
        item_ids=selected_ids,
        scoring_version=profile.ranking_version,
        config_version=config_version,
        recall_profile=profile.key,
        memory_profile_id=memory_context.memory_profile_id,
        memory_profile_revision_id=memory_context.memory_profile_revision_id,
        memory_context_version=memory_context.version,
    )
    session.add(recall_log)

    # 4. Update recall signals. Only recall_count/last_recalled_at —
    #    startup_recall_count drives the startup anti-feedback penalty and
    #    must not accumulate from semantic queries (design §4). These are
    #    exposure counters only: they never feed admission, epistemic state,
    #    or utility (issue #160 feedback-loop safeguards).
    if selected_ids:
        await session.execute(
            update(MemoryItem)
            .where(MemoryItem.id.in_(selected_ids))
            .values(
                recall_count=MemoryItem.recall_count + 1,
                last_recalled_at=now,
            )
        )

    await session.commit()

    return {
        "working_set": evaluation.working_set,
        "item_count": evaluation.item_count,
        "byte_count": evaluation.byte_count,
        "pinned_omitted_count": 0,
        "omitted_count": max(0, evaluation.candidate_count - evaluation.item_count),
        "items": evaluation.items,
        "scoring_version": profile.ranking_version,
        "config_version": config_version,
        "recall_log_id": str(recall_log.id),
        "message": None,
        # Telemetry context (ENG-METER-001).
        "workspace_id": str(workspace_id) if workspace_id else None,
        "candidate_count": evaluation.candidate_count,
        "embedding_outcome": "succeeded",
        # Profile context (issue #160): the effective profile, the signal
        # model version (None on the legacy blend), and admission omission
        # counts by reason code (content of withheld items is not retained).
        "recall_profile": profile.key,
        "signals_version": (
            recall_signals.SIGNALS_VERSION if profile.signals_enabled else None
        ),
        "omitted_by_admission": evaluation.omitted_by_admission,
        "_semantic_evaluation": evaluation,
        "effective_byte_budget": evaluation.byte_budget,
        "effective_token_budget": evaluation.token_budget,
        "effective_item_budget": evaluation.item_budget,
    }
