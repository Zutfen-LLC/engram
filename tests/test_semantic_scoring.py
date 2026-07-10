"""Unit tests for trust-weighted semantic scoring (engram.semantic).

These are pure-function tests (no DB) covering the trust blend that
re-ranks semantic search/recall results. Integration coverage of end-to-end
ranking lives in tests/test_search.py and tests/test_semantic_recall.py.
"""

from __future__ import annotations

from engram.semantic import (
    _PROPOSED_REVIEW_FACTOR,
    _TRUST_MIN,
    _UNRESOLVED_CONFLICT_FACTOR,
    SEMANTIC_SCORING_VERSION,
    compute_semantic_trust_score,
)


def _trust(
    *,
    source_trust: float = 0.5,
    memory_confidence: float = 0.5,
    importance: float = 0.5,
    human_verified: bool = False,
    review_status: str = "active",
    conflict_resolution_status: str | None = None,
) -> float:
    return compute_semantic_trust_score(
        source_trust=source_trust,
        memory_confidence=memory_confidence,
        importance=importance,
        human_verified=human_verified,
        review_status=review_status,
        conflict_resolution_status=conflict_resolution_status,
    )


def test_scoring_version_is_semantic_v2() -> None:
    assert SEMANTIC_SCORING_VERSION == "semantic-v2"


def test_high_trust_active_item_scores_high() -> None:
    """A fully-trusted, verified, active item gets the max blend (clamped 1.0)."""
    trust = _trust(
        source_trust=1.0,
        memory_confidence=1.0,
        importance=1.0,
        human_verified=True,
        review_status="active",
    )
    assert trust == 1.0


def test_low_trust_floor_clamps_above_zero() -> None:
    """Even a zero-trust item keeps the floor so similarity still matters."""
    trust = _trust(
        source_trust=0.0,
        memory_confidence=0.0,
        importance=0.0,
        human_verified=False,
        review_status="proposed",
    )
    assert trust == _TRUST_MIN


def test_unresolved_conflict_penalty_applied() -> None:
    base = _trust(
        source_trust=0.8,
        memory_confidence=0.8,
        importance=0.8,
        human_verified=True,
        review_status="active",
    )
    penalized = _trust(
        source_trust=0.8,
        memory_confidence=0.8,
        importance=0.8,
        human_verified=True,
        review_status="active",
        conflict_resolution_status="unresolved",
    )
    assert penalized == base * _UNRESOLVED_CONFLICT_FACTOR


def test_proposed_penalty_applied() -> None:
    active = _trust(review_status="active")
    proposed = _trust(review_status="proposed")
    # Proposed loses the review-status blend term AND takes the multiplier.
    assert proposed < active
    # Verify the multiplier specifically: same inputs except review_status, the
    # proposed base (without the 0.05 active term) times the factor.
    proposed_base = (
        0.30 * 0.5 + 0.30 * 0.5 + 0.25 * 0.5 + 0.10 * 0.0 + 0.05 * 0.0
    )
    assert proposed == max(_TRUST_MIN, min(1.0, proposed_base * _PROPOSED_REVIEW_FACTOR))


def test_human_verified_raises_trust() -> None:
    unverified = _trust(human_verified=False)
    verified = _trust(human_verified=True)
    assert verified > unverified
    # The verified bonus contributes exactly _TRUST_W_VERIFIED (0.10) more.
    assert abs((verified - unverified) - 0.10) < 1e-9


def test_high_trust_can_outrank_slightly_closer_low_trust() -> None:
    """The core requirement: a slightly-more-similar low-trust item must not
    outrank a slightly-less-similar high-trust item under semantic-v2.

    This models the final semantic_score = similarity * trust_score.
    """
    high_trust = _trust(
        source_trust=0.9, memory_confidence=0.9, importance=0.8, human_verified=True
    )
    low_trust = _trust(
        source_trust=0.4, memory_confidence=0.4, importance=0.3, review_status="proposed"
    )
    # Low-trust item is slightly more similar; high-trust slightly less.
    low_similarity = 0.95
    high_similarity = 0.85
    low_score = low_similarity * low_trust
    high_score = high_similarity * high_trust
    assert high_score > low_score


def test_trust_is_deterministic() -> None:
    """Same inputs -> same trust score."""
    a = _trust(source_trust=0.7, memory_confidence=0.6, importance=0.4)
    b = _trust(source_trust=0.7, memory_confidence=0.6, importance=0.4)
    assert a == b
