import math

import pytest

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
