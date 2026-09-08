"""Root-agnostic demonstrated-usefulness utility inputs (issue #196)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from sqlalchemy import and_, any_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from engram.feedback import current_feedback_predicate
from engram.models import FeedbackEvent, MemoryItem, Principal, RecallLog

DemonstratedUsefulnessState = Literal["none", "positive", "negative", "mixed"]
FeedbackVerdict = Literal["useful", "noise"]

DEMONSTRATED_USEFULNESS_VERSION: Final[Literal["demonstrated-usefulness-v1"]] = (
    "demonstrated-usefulness-v1"
)


@dataclass(frozen=True)
class QualifyingFeedback:
    """One current, externally qualified verdict for an admitted item."""

    verdict: FeedbackVerdict


@dataclass(frozen=True)
class DemonstratedUsefulnessSummary:
    """Bounded, diagnostic-only usefulness state for one admitted item."""

    version: Literal["demonstrated-usefulness-v1"]
    state: DemonstratedUsefulnessState
    adjustment: float
    qualifying_useful_actor_count: int
    qualifying_noise_actor_count: int
    excluded_self_or_author_count: int = 0
    excluded_unbound_exposure_count: int = 0


def summarize_demonstrated_usefulness(
    feedback: Sequence[QualifyingFeedback],
    *,
    excluded_self_or_author_count: int = 0,
    excluded_unbound_exposure_count: int = 0,
) -> DemonstratedUsefulnessSummary:
    """Classify qualifying verdicts with the deliberately non-amplifying v1 table."""
    return summarize_demonstrated_usefulness_counts(
        useful_count=sum(entry.verdict == "useful" for entry in feedback),
        noise_count=sum(entry.verdict == "noise" for entry in feedback),
        excluded_self_or_author_count=excluded_self_or_author_count,
        excluded_unbound_exposure_count=excluded_unbound_exposure_count,
    )


def summarize_demonstrated_usefulness_counts(
    *,
    useful_count: int,
    noise_count: int,
    excluded_self_or_author_count: int = 0,
    excluded_unbound_exposure_count: int = 0,
) -> DemonstratedUsefulnessSummary:
    """Classify bounded SQL aggregate counts with the v1 adjustment table."""
    if useful_count and noise_count:
        state: DemonstratedUsefulnessState = "mixed"
        adjustment = 0.0
    elif useful_count:
        state = "positive"
        adjustment = 0.10
    elif noise_count:
        state = "negative"
        adjustment = -0.10
    else:
        state = "none"
        adjustment = 0.0
    return DemonstratedUsefulnessSummary(
        version=DEMONSTRATED_USEFULNESS_VERSION,
        state=state,
        adjustment=adjustment,
        qualifying_useful_actor_count=useful_count,
        qualifying_noise_actor_count=noise_count,
        excluded_self_or_author_count=excluded_self_or_author_count,
        excluded_unbound_exposure_count=excluded_unbound_exposure_count,
    )


async def load_demonstrated_usefulness(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    items: Sequence[MemoryItem],
) -> dict[uuid.UUID, DemonstratedUsefulnessSummary]:
    """Load root-agnostic usefulness once for an already-admitted item set.

    This is deliberately one tenant-scoped query for the bounded packet. The
    inner principal join proves the actor remains resolvable; the outer recall
    log join retains malformed historical rows so their claimed exposure can
    be rejected rather than guessed. Neither identity is returned to callers.
    """
    candidate_by_id = {item.id: item for item in items if item.tenant_id == tenant_id}
    if not candidate_by_id:
        return {}

    is_self_or_author = FeedbackEvent.principal_id == MemoryItem.principal_id
    has_bound_exposure = and_(
        RecallLog.tenant_id == tenant_id,
        RecallLog.principal_id == FeedbackEvent.principal_id,
        MemoryItem.id == any_(RecallLog.item_ids),
    )
    is_qualified = and_(~is_self_or_author, has_bound_exposure)
    rows = (
        await session.execute(
            select(
                MemoryItem.id.label("item_id"),
                func.count()
                .filter(and_(is_qualified, FeedbackEvent.verdict == "useful"))
                .label("qualifying_useful_count"),
                func.count()
                .filter(and_(is_qualified, FeedbackEvent.verdict == "noise"))
                .label("qualifying_noise_count"),
                func.count().filter(is_self_or_author).label("excluded_self_or_author_count"),
                func.count()
                .filter(and_(~is_self_or_author, ~has_bound_exposure))
                .label("excluded_unbound_exposure_count"),
            )
            .select_from(FeedbackEvent)
            .join(
                MemoryItem,
                and_(
                    MemoryItem.id == FeedbackEvent.item_id,
                    MemoryItem.tenant_id == tenant_id,
                ),
            )
            .join(
                Principal,
                and_(
                    Principal.id == FeedbackEvent.principal_id,
                    Principal.tenant_id == tenant_id,
                ),
            )
            .outerjoin(RecallLog, RecallLog.id == FeedbackEvent.recall_log_id)
            .where(
                FeedbackEvent.tenant_id == tenant_id,
                FeedbackEvent.item_id.in_(candidate_by_id),
                current_feedback_predicate(),
            )
            .group_by(MemoryItem.id)
        )
    ).mappings().all()
    summaries = {
        item_id: summarize_demonstrated_usefulness_counts(useful_count=0, noise_count=0)
        for item_id in candidate_by_id
    }
    for row in rows:
        item_id = row["item_id"]
        if item_id not in candidate_by_id:
            continue
        summaries[item_id] = summarize_demonstrated_usefulness_counts(
            useful_count=int(row["qualifying_useful_count"]),
            noise_count=int(row["qualifying_noise_count"]),
            excluded_self_or_author_count=int(row["excluded_self_or_author_count"]),
            excluded_unbound_exposure_count=int(row["excluded_unbound_exposure_count"]),
        )
    return summaries


__all__ = [
    "DEMONSTRATED_USEFULNESS_VERSION",
    "DemonstratedUsefulnessState",
    "DemonstratedUsefulnessSummary",
    "FeedbackVerdict",
    "QualifyingFeedback",
    "load_demonstrated_usefulness",
    "summarize_demonstrated_usefulness",
    "summarize_demonstrated_usefulness_counts",
]
