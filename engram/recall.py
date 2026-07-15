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
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, case, func, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from engram import db as db_module
from engram import semantic
from engram.config import settings
from engram.embeddings import generate_embedding
from engram.jobs import enqueue_job
from engram.memory_access import eligibility_expression, resolve_workspace_scope
from engram.memory_kinds import get_disputed_stay_kind_names
from engram.models import MemoryItem, RecallLog, TenantConfig
from engram.promotion import maybe_auto_promote_for_startup_recall
from engram.relationship_recall import RECALL_SCORING_VERSION, expand_recall_candidates

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
    tenant_id: str,
    principal_id: str,
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
    stay_kinds = await get_disputed_stay_kind_names(session, tenant_id)
    review_status_clause = _review_status_clause(stay_kinds)
    stmt = select(MemoryItem).where(
        MemoryItem.tenant_id == tenant_id,
        review_status_clause,
        MemoryItem.valid_to.is_(None),
        eligibility_expression(principal_id),
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
    tenant_id: str,
    principal_id: str,
    workspace_id: str | None,
    review_status_clause: ColumnElement[bool],
) -> list[Any]:
    """Shared WHERE clauses for every candidate sub-query (tenant/eligibility/kind)."""
    filters: list[Any] = [
        MemoryItem.tenant_id == tenant_id,
        review_status_clause,
        MemoryItem.valid_to.is_(None),
        eligibility_expression(principal_id),
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
    tenant_id: str,
    principal_id: str,
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
    stay_kinds = await get_disputed_stay_kind_names(session, tenant_id)
    stats.query_count += 1
    review_status_clause = _review_status_clause(stay_kinds)
    filters = _base_candidate_filters(
        tenant_id=tenant_id,
        principal_id=principal_id,
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
    tenant_id: str,
    principal_id: str,
    workspace: str | None,
    byte_budget: int | None,
    token_budget: int | None,
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Execute startup recall and return the response dict.

    Two-stage pipeline (ENG-AUD-011 / F18 — see module docstring):
    0. Lazy, bounded promotion pass (write session).
    1. Bounded SQL candidate selection (read session, unless this call's
       promotion pass actually promoted rows — see step 1 below).
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
        session, tenant_id=tenant_id, principal_id=principal_id, workspace=workspace
    )

    # 0. Lazy, bounded, tenant-scoped Path A promotion pass (design.md §3,
    #    ENG-AUD-007 F11) — runs before candidates are selected so an item
    #    that becomes eligible between recalls can appear in this working set
    #    rather than waiting for the next CLI/admin sweep. Honors
    #    tenant_config.auto_promote_enabled and settings.startup_promotion_limit
    #    internally; a disabled tenant pays only a single count query. This is
    #    a write and always runs on the primary session.
    promotion_result = await maybe_auto_promote_for_startup_recall(session, tenant_id, now=now)

    # 1. Bounded SQL candidate selection. Promotion consistency policy
    #    (requirement 12, "preferred conservative behavior"): when this
    #    recall's own lazy promotion pass actually promoted rows, read
    #    candidates from the primary/write session so the just-promoted rows
    #    are guaranteed visible in this recall — a read replica could lag
    #    behind the promotion write. Otherwise use the read-oriented session
    #    (a configured replica via ENGRAM_READ_DATABASE_URL, or the primary
    #    when unset — see engram.db.read_session_factory).
    candidate_limit = min(
        settings.startup_recall_candidate_limit,
        settings.startup_recall_candidate_limit_max,
    )
    read_source = "primary"
    if workspace is not None and not workspace_accessible:
        candidates: list[MemoryItem] = []
        candidate_stats = CandidateStats()
    elif promotion_result.promoted > 0:
        candidates, candidate_stats = await _fetch_startup_candidates(
            session,
            tenant_id=tenant_id,
            principal_id=principal_id,
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
                tenant_id=tenant_id,
                principal_id=principal_id,
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
        # Telemetry context (ENG-METER-001). Startup recall is deterministic and
        # never calls an embedding provider, so embedding_outcome is not_required.
        "workspace_id": str(workspace_id) if workspace_id else None,
        "embedding_outcome": "not_required",
    }


# ---- Semantic recall ----

# Semantic recall includes active AND proposed items (design.md §3) so agents
# can rediscover their own observations. Rejected/archived/expired are excluded
# by the review_status + valid_to filter in semantic.search().
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
        item_bytes = len(cand["content"].encode())
        item_tokens = max(1, item_bytes // 4)

        # Skip oversized items and keep scanning lower-ranked ones that fit.
        if byte_budget is not None and bytes_used + item_bytes > byte_budget:
            continue
        if token_budget is not None and tokens_used + item_tokens > token_budget:
            continue

        bytes_used += item_bytes
        tokens_used += item_tokens
        result.append(cand)

    return result


async def execute_semantic_recall(
    session: AsyncSession,
    tenant_id: str,
    principal_id: str,
    workspace: str | None,
    query: str,
    *,
    byte_budget: int | None,
    token_budget: int | None,
    item_budget: int | None,
) -> dict[str, Any]:
    """Execute semantic recall and return the response dict.

    Owns the query-embedding generation flow end to end. Eligibility mirrors
    design.md §3: active AND proposed items, valid_to IS NULL. Proposed items
    are tagged ``warnings: ["unreviewed"]`` so callers can distinguish them
    from reviewed/active memories.

    When embeddings are unavailable (provider=none) or the corpus has no
    candidates, returns an empty working set with a helpful message rather
    than raising — and still writes a recall_logs audit row.
    """
    now = datetime.now(UTC)
    config = await _get_tenant_config(session, tenant_id)

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
        session, tenant_id=tenant_id, principal_id=principal_id, workspace=workspace
    )

    # 1. Generate the query embedding. This is the single place the query
    #    vector is produced; the shared semantic.search() never embeds.
    import inspect

    from engram.embedding_profiles import get_active_profile

    active_profile = await get_active_profile(session)
    if len(inspect.signature(generate_embedding).parameters) >= 2:
        query_embedding = await generate_embedding(
            query,
            active_profile,
            tenant_id=tenant_id,
            principal_id=principal_id,
            operation="embedding_query_recall",
            usage_class="request",
        )
    else:
        query_embedding = await generate_embedding(query)

    if workspace is not None and not workspace_accessible:
        candidate_total = 0
    else:
        candidate_total = await semantic.candidate_count(
            session,
            tenant_id=tenant_id,
            principal_id=principal_id,
            workspace_id=workspace_id,
            review_statuses=_SEMANTIC_REVIEW_STATUSES,
            profile=active_profile,
        )

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
            item_ids=[],
            scoring_version=RECALL_SCORING_VERSION,
            config_version=config_version,
        )
        session.add(recall_log)
        await session.commit()
        return {
            "working_set": "",
            "item_count": 0,
            "byte_count": 0,
            "pinned_omitted_count": 0,
            "omitted_count": 0,
            "items": [],
            "scoring_version": RECALL_SCORING_VERSION,
            "config_version": config_version,
            "recall_log_id": str(recall_log.id),
            "message": _NO_EMBEDDINGS_MESSAGE,
            # Telemetry context (ENG-METER-001).
            "workspace_id": str(workspace_id) if workspace_id else None,
            "candidate_count": 0,
            "embedding_outcome": "disabled" if query_embedding is None else "succeeded",
        }

    # 2. Retrieve nearest candidates by cosine similarity, scoped to the
    #    caller's tenant/principal/workspace eligibility (engram.memory_access).
    #    item_budget is already resolved to a default above.
    item_limit = item_budget if item_budget is not None else settings.recall_item_budget
    fetch_limit = min(item_limit * _SEMANTIC_OVERFETCH, _SEMANTIC_OVERFETCH_CAP)
    candidates = await semantic.search(
        session,
        query_embedding,
        fetch_limit,
        tenant_id=tenant_id,
        principal_id=principal_id,
        workspace_id=workspace_id,
        review_statuses=_SEMANTIC_REVIEW_STATUSES,
        profile=active_profile,
    )

    # 4. Enrich candidates with full MemoryItem trust fields (pinned,
    #    importance, source_trust, memory_confidence, human_verified).
    #    Ids already passed the eligibility-filtered search above; the
    #    tenant_id filter here is cheap defense in depth.
    candidate_ids = [UUID(c["id"]) for c in candidates]
    item_by_id: dict[UUID, MemoryItem] = {}
    if candidate_ids:
        rows = await session.execute(
            select(MemoryItem).where(
                MemoryItem.id.in_(candidate_ids),
                MemoryItem.tenant_id == tenant_id,
            )
        )
        item_by_id = {item.id: item for item in rows.scalars().all()}

    # 5. Build per-item response dicts in trust-weighted order. The candidate
    #    dicts already carry the trust-weighted semantic score, similarity, and
    #    trust blend computed by engram.semantic; we add the MemoryItem fields
    #    (pinned, etc.) needed by callers.
    enriched: list[dict[str, Any]] = []
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
        enriched.append(
            {
                "id": str(item.id),
                "kind": item.kind,
                "content": item.content,
                "score": round(semantic_score, 4),
                "distance": round(distance, 4),
                "similarity_score": round(similarity, 4),
                "trust_score": round(trust_score, 4),
                "review_status": item.review_status,
                "reasons": [
                    f"semantic similarity {similarity:.2f}",
                    f"trust_score={trust_score:.2f}",
                    f"cosine_distance={distance:.4f}",
                ],
                "warnings": warnings,
                "pinned": item.pinned,
                "importance": item.importance,
                "source_trust": item.source_trust,
                "memory_confidence": item.memory_confidence,
                "human_verified": item.human_verified,
            }
        )

    # 5b. Relationship-aware expansion (ENG-AUD-012 / F19): graph (depth-1,
    #     bounded) then tunnel (bounded) expansion of the top semantic
    #     candidates, merged and rescored — semantic relevance still
    #     dominates the blended score (see engram.relationship_recall). Runs
    #     before budget packing so expanded memories compete for budget on
    #     equal footing with direct semantic hits; never bypasses eligibility.
    enriched = await expand_recall_candidates(
        session,
        tenant_id=tenant_id,
        principal_id=principal_id,
        workspace_id=workspace_id,
        semantic_items=enriched,
        item_by_id=item_by_id,
        now=now,
    )

    # 6. Enforce item/byte/token budgets.
    selected = _enforce_semantic_budget(
        enriched,
        byte_budget=byte_budget,
        token_budget=token_budget,
        item_budget=item_budget,
    )

    # 7. Build working set + counts.
    working_set_lines = [f"[{item['kind']}] {item['content']}" for item in selected]
    working_set = "\n".join(working_set_lines)
    item_count = len(selected)
    byte_count = sum(len(item["content"].encode()) for item in selected)

    # 8. Write recall_logs (mode='semantic', query populated).
    config_version = config.config_version if config is not None else "v1"
    selected_ids = [UUID(item["id"]) for item in selected]
    recall_log = RecallLog(
        tenant_id=tenant_id,
        principal_id=principal_id,
        mode="semantic",
        query=query,
        byte_budget=byte_budget,
        token_budget=token_budget,
        item_ids=selected_ids,
        scoring_version=RECALL_SCORING_VERSION,
        config_version=config_version,
    )
    session.add(recall_log)

    # 9. Update recall signals. Only recall_count/last_recalled_at —
    #    startup_recall_count drives the startup anti-feedback penalty and
    #    must not accumulate from semantic queries (design §4).
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
        "working_set": working_set,
        "item_count": item_count,
        "byte_count": byte_count,
        "pinned_omitted_count": 0,
        "omitted_count": max(0, candidate_total - item_count),
        "items": selected,
        "scoring_version": RECALL_SCORING_VERSION,
        "config_version": config_version,
        "recall_log_id": str(recall_log.id),
        "message": None,
        # Telemetry context (ENG-METER-001).
        "workspace_id": str(workspace_id) if workspace_id else None,
        "candidate_count": candidate_total,
        "embedding_outcome": "succeeded",
    }
