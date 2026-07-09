"""Recall engine: scoring, startup recall, semantic recall.

Implements the trust-model scoring formula from design.md Section 4.
Startup recall is deterministic given state — same corpus + config = same output.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from engram import semantic
from engram.config import settings
from engram.embeddings import generate_embedding
from engram.memory_access import eligibility_expression, resolve_workspace_scope
from engram.models import MemoryItem, RecallLog, TenantConfig
from engram.promotion import maybe_auto_promote_for_startup_recall


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
            penalty = penalty_factor ** excess
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
    stale_after_days = (
        config.stale_after_days
        if config is not None
        else settings.stale_after_days
    )
    last_verified = item.last_verified_at or item.valid_from
    if last_verified is not None:
        days_since_verified = (now - last_verified).total_seconds() / 86400
        if days_since_verified > stale_after_days:
            warnings.append(f"not confirmed in {stale_after_days} days")
    if item.memory_confidence < 0.5:
        warnings.append("low confidence")
    if item.conflict_resolution_status == "unresolved":
        warnings.append("unresolved conflicts")

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
    """Fetch active, non-expired items for startup recall.

    Only review_status='active' and valid_to IS NULL enter startup recall.
    Also enforces the shared tenant/visibility eligibility predicate so a
    principal never sees another principal's private memory, or workspace
    memory from a workspace they aren't a member of.
    """
    stmt = select(MemoryItem).where(
        MemoryItem.tenant_id == tenant_id,
        MemoryItem.review_status == "active",
        MemoryItem.valid_to.is_(None),
        eligibility_expression(principal_id),
    )
    if workspace_id is not None:
        stmt = stmt.where(MemoryItem.workspace_id == workspace_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


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
) -> dict[str, Any]:
    """Execute startup recall and return the response dict.

    Flow:
    1. Fetch active items (review_status='active', valid_to IS NULL)
    2. Separate pinned (bypass, capped) from scored
    3. Score remaining items by formula, sort descending
    4. Enforce budget
    5. Write recall_logs entry
    6. Increment recall_count, update last_recalled_at
    7. Return working_set + items with reasons
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
    #    ENG-AUD-007 F11) — runs before active items are selected so an item
    #    that becomes eligible between recalls can appear in this working set
    #    rather than waiting for the next CLI/admin sweep. Honors
    #    tenant_config.auto_promote_enabled and settings.startup_promotion_limit
    #    internally; a disabled tenant pays only a single count query.
    await maybe_auto_promote_for_startup_recall(session, tenant_id, now=now)

    # 1. Fetch active items
    if workspace is not None and not workspace_accessible:
        items: list[MemoryItem] = []
    else:
        items = await _fetch_active_items(session, tenant_id, principal_id, workspace_id)

    # 2. Separate pinned
    max_pinned = (
        config.max_pinned_tokens
        if config is not None
        else settings.max_pinned_tokens
    )
    pinned_items, scored_items, pinned_omitted = _separate_pinned(items, max_pinned)

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
        pinned_tokens = sum(
            max(1, len(i.content.encode()) // 4) for i in pinned_items
        )
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
    all_items: list[tuple[MemoryItem, float | None, list[str], list[str]]] = (
        [(i, None, [], []) for i in pinned_items]
        + [(i, s, r, w) for i, s, r, w in scored_with_reasons]
    )

    working_set_lines = []
    response_items = []

    for item, score, reasons, warnings in all_items:
        line = f"[{item.kind}] {item.content}"
        working_set_lines.append(line)

        item_dict: dict[str, Any] = {
            "id": str(item.id),
            "kind": item.kind,
            "content": item.content,
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

    # 6. Write recall_logs
    scoring_version = "v1"
    config_version = config.config_version if config is not None else "v1"

    recall_log = RecallLog(
        tenant_id=tenant_id,
        principal_id=principal_id,
        mode="startup",
        byte_budget=byte_budget,
        token_budget=token_budget,
        item_ids=[i.id for i, _, _, _ in all_items],
        scoring_version=scoring_version,
        config_version=config_version,
    )
    session.add(recall_log)

    # 7. Update recall_count and last_recalled_at
    item_ids = [i.id for i, _, _, _ in all_items]
    await session.execute(
        update(MemoryItem)
        .where(MemoryItem.id.in_(item_ids))
        .values(
            recall_count=MemoryItem.recall_count + 1,
            startup_recall_count=MemoryItem.startup_recall_count + 1,
            last_recalled_at=now,
        )
    )

    await session.commit()

    return {
        "working_set": working_set,
        "item_count": item_count,
        "byte_count": byte_count,
        "pinned_omitted_count": pinned_omitted,
        "omitted_count": max(0, len(items) - item_count),
        "items": response_items,
        "scoring_version": scoring_version,
        "config_version": config_version,
        "recall_log_id": str(recall_log.id),
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
            scoring_version=semantic.SEMANTIC_SCORING_VERSION,
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
            "scoring_version": semantic.SEMANTIC_SCORING_VERSION,
            "config_version": config_version,
            "recall_log_id": str(recall_log.id),
            "message": _NO_EMBEDDINGS_MESSAGE,
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
        scoring_version=semantic.SEMANTIC_SCORING_VERSION,
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
        "scoring_version": semantic.SEMANTIC_SCORING_VERSION,
        "config_version": config_version,
        "recall_log_id": str(recall_log.id),
        "message": None,
    }
