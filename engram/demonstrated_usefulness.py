"""Root-agnostic demonstrated-usefulness utility inputs (issue #196)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

from sqlalchemy import and_, select
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
    useful_count = sum(entry.verdict == "useful" for entry in feedback)
    noise_count = sum(entry.verdict == "noise" for entry in feedback)
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

    qualifying: dict[uuid.UUID, list[QualifyingFeedback]] = {
        item_id: [] for item_id in candidate_by_id
    }
    excluded_self: dict[uuid.UUID, int] = {item_id: 0 for item_id in candidate_by_id}
    excluded_unbound: dict[uuid.UUID, int] = {item_id: 0 for item_id in candidate_by_id}
    rows = (
        await session.execute(
            select(
                FeedbackEvent.item_id.label("item_id"),
                FeedbackEvent.verdict.label("verdict"),
                FeedbackEvent.principal_id.label("actor_id"),
                RecallLog.tenant_id.label("recall_log_tenant_id"),
                RecallLog.principal_id.label("recall_log_principal_id"),
                RecallLog.item_ids.label("recall_log_item_ids"),
            )
            .select_from(FeedbackEvent)
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
        )
    ).mappings().all()
    for raw_row in rows:
        row = cast(dict[str, Any], raw_row)
        item_id = cast(uuid.UUID, row["item_id"])
        candidate = candidate_by_id.get(item_id)
        if candidate is None:
            continue
        actor_id = cast(uuid.UUID, row["actor_id"])
        if actor_id == candidate.principal_id:
            excluded_self[item_id] += 1
            continue
        exposed_item_ids = cast(Sequence[uuid.UUID] | None, row["recall_log_item_ids"])
        if (
            row["recall_log_tenant_id"] != tenant_id
            or row["recall_log_principal_id"] != actor_id
            or exposed_item_ids is None
            or item_id not in exposed_item_ids
        ):
            excluded_unbound[item_id] += 1
            continue
        qualifying[item_id].append(
            QualifyingFeedback(verdict=cast(FeedbackVerdict, row["verdict"]))
        )
    return {
        item_id: summarize_demonstrated_usefulness(
            feedback,
            excluded_self_or_author_count=excluded_self[item_id],
            excluded_unbound_exposure_count=excluded_unbound[item_id],
        )
        for item_id, feedback in qualifying.items()
    }


__all__ = [
    "DEMONSTRATED_USEFULNESS_VERSION",
    "DemonstratedUsefulnessState",
    "DemonstratedUsefulnessSummary",
    "FeedbackVerdict",
    "QualifyingFeedback",
    "load_demonstrated_usefulness",
    "summarize_demonstrated_usefulness",
]
