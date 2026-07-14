import math
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from engram.models import MemoryItem
from engram.promotion import (
    BLOCK_REVIEW_POLICY,
    PromotionSupport,
    assess_promotion_candidate,
    load_promotion_support,
)
from engram.promotion_policy import (
    EVIDENCE_SCORE_CEILING,
    PromotionPolicyError,
    choose_basis,
    evidence_score_v1,
)


@pytest.mark.parametrize(
    ("prior", "retention", "expected"),
    [(0.35, 0.90, 0.79), (0.40, 0.90, 0.80), (0.30, 0.78, 0.684)],
)
def test_evidence_score_v1_examples(prior: float, retention: float, expected: float) -> None:
    assert evidence_score_v1(prior, retention) == pytest.approx(expected)


def test_evidence_score_v1_is_capped() -> None:
    assert evidence_score_v1(1.0, 0.95) == EVIDENCE_SCORE_CEILING


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, -0.01, 1.01])
def test_evidence_score_v1_rejects_invalid_prior(value: float) -> None:
    with pytest.raises(PromotionPolicyError):
        evidence_score_v1(value, 0.9)


def test_evidence_lane_wins_when_both_lanes_pass() -> None:
    assessment = choose_basis(
        legacy_trust_qualified=True,
        legacy_age_qualified=True,
        evidence_trust_qualified=True,
        evidence_age_qualified=True,
    )
    assert assessment.selected_basis == "retention_evidence"


class _EmptyResult:
    def scalars(self) -> list[Any]:
        return []

    def all(self) -> list[Any]:
        return []


class _CountingSession:
    def __init__(self) -> None:
        self.query_count = 0

    async def execute(self, statement: object) -> _EmptyResult:
        self.query_count += 1
        return _EmptyResult()


@pytest.mark.parametrize("candidate_count", [1, 50])
async def test_support_loader_query_count_is_bounded(candidate_count: int) -> None:
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    items = [
        MemoryItem(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            principal_id=principal_id,
            content=f"candidate {index}",
            content_hash=f"sha256:{index:064x}",
            kind="fact" if index % 2 else "decision",
        )
        for index in range(candidate_count)
    ]
    session = _CountingSession()
    support = await load_promotion_support(session, items)  # type: ignore[arg-type]
    assert session.query_count == 4
    assert len(support) == candidate_count


def test_pure_assessment_reports_review_policy_denial() -> None:
    now = datetime.now(UTC)
    item = MemoryItem(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        principal_id=uuid.uuid4(),
        content="invalid transition state",
        content_hash=f"sha256:{uuid.uuid4().hex}",
        kind="fact",
        review_status="unknown",
        memory_confidence=0.9,
        created_at=now - timedelta(hours=100),
    )
    kind = type(
        "KindSupport",
        (),
        {"enabled": True, "auto_promote_from_inferred": True},
    )()
    candidate = assess_promotion_candidate(
        item,
        PromotionSupport(kind=kind, classification_run=None),  # type: ignore[arg-type]
        confidence_threshold=0.7,
        min_age_hours=72,
        evidence_enabled=False,
        evidence_threshold=0.7,
        now=now,
    )
    assert candidate.would_promote is False
    assert BLOCK_REVIEW_POLICY in candidate.blockers
