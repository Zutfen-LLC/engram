"""Recall engine: scoring, startup recall, semantic recall.

Implements the trust-model scoring formula from design.md Section 4.
Startup recall is deterministic given state — same corpus + config = same output.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from engram.config import settings
from engram.models import MemoryItem, RecallLog, TenantConfig


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

    # Recency bonus (decay: max(0, 1 - days/30))
    recency_bonus = 0.0
    if item.last_recalled_at is not None:
        days_since = (now - item.last_recalled_at).total_seconds() / 86400
        recency_bonus = max(0.0, 1.0 - days_since / 30.0)
        # Anti-feedback penalty
        if item.startup_recall_count > penalty_threshold:
            excess = item.startup_recall_count - penalty_threshold
            penalty = penalty_factor ** excess
            recency_bonus *= penalty
            recency_bonus = max(recency_bonus, settings.startup_recall_penalty_floor)
            reasons.append(
                f"recency_penalty(count={item.startup_recall_count})"
            )
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


async def _fetch_active_items(
    session: AsyncSession,
    tenant_id: str,
    workspace_id: str | None,
) -> list[MemoryItem]:
    """Fetch active, non-expired items for startup recall.

    Only review_status='active' and valid_to IS NULL enter startup recall.
    """
    stmt = select(MemoryItem).where(
        MemoryItem.tenant_id == tenant_id,
        MemoryItem.review_status == "active",
        MemoryItem.valid_to.is_(None),
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
    """Enforce byte/token budget, preserving score order."""
    if byte_budget is None and token_budget is None:
        return items_with_scores

    result = []
    budget_used = 0

    for item, score in items_with_scores:
        item_bytes = len(item.content.encode())
        item_tokens = max(1, item_bytes // 4)

        if token_budget is not None:
            if budget_used + item_tokens > token_budget:
                break
            budget_used += item_tokens
        elif byte_budget is not None:
            if budget_used + item_bytes > byte_budget:
                break
            budget_used += item_bytes

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

    # Resolve workspace_id if provided
    workspace_id = None
    if workspace is not None:
        ws_result = await session.execute(
            text(
                "SELECT id FROM workspaces WHERE tenant_id = :tid AND slug = :slug"
            ),
            {"tid": tenant_id, "slug": workspace},
        )
        workspace_id = ws_result.scalar_one_or_none()

    # 1. Fetch active items
    items = await _fetch_active_items(session, tenant_id, workspace_id)

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
