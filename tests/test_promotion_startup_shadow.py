"""Pure B5 parity-vocabulary tests; no database or lifecycle mutation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from engram.promotion import PromotionCandidate
from engram.promotion_startup_shadow import (
    PARITY_OUTCOMES,
    classify_startup_promotion_parity,
)


def _candidate(
    *,
    would_promote: bool = False,
    selected_basis: str | None = None,
    legacy_confidence: float = 0.5,
    legacy_threshold: float = 0.7,
) -> PromotionCandidate:
    now = datetime.now(UTC)
    return PromotionCandidate(
        item_id=uuid.uuid4(),
        would_promote=would_promote,
        selected_basis=selected_basis,  # type: ignore[arg-type]
        blockers=[],
        legacy_confidence=legacy_confidence,
        legacy_threshold=legacy_threshold,
        evidence_score=None,
        evidence_threshold=0.7,
        taxonomy_confidence=None,
        retention_disposition=None,
        classification_run_id=None,
        cooling_period_start=None,
        eligible_at=now if would_promote else None,
        legacy_eligible_at=now + timedelta(hours=72),
        evidence_cooling_period_start=None,
        evidence_eligible_at=None,
        kind="fact",
        kind_auto_promote_allowed=True,
        conflict_recheck_status="not_run",
    )


def test_parity_vocabulary_is_closed_and_content_free() -> None:
    assert set(PARITY_OUTCOMES) == {
        "parity_no_action",
        "parity_already_committed",
        "parity_durably_scheduled",
        "mismatch_missing_obligation",
        "mismatch_state",
        "unknown",
    }


def test_shadow_requires_canonical_prerequisites() -> None:
    assert (
        classify_startup_promotion_parity(
            review_status="proposed",
            candidate=_candidate(would_promote=True, selected_basis="legacy_confidence"),
            current_obligation_covered=False,
            prerequisites_enabled=False,
        )
        == "unknown"
    )


@pytest.mark.parametrize(
    ("candidate", "covered", "expected"),
    [
        (
            _candidate(would_promote=True, selected_basis="legacy_confidence"),
            True,
            "parity_durably_scheduled",
        ),
        (
            _candidate(would_promote=True, selected_basis="legacy_confidence"),
            False,
            "mismatch_missing_obligation",
        ),
        (_candidate(legacy_confidence=0.9), True, "parity_durably_scheduled"),
        (_candidate(legacy_confidence=0.9), False, "mismatch_missing_obligation"),
        (_candidate(), False, "parity_no_action"),
    ],
)
def test_shadow_reuses_obligation_semantics(
    candidate: PromotionCandidate, covered: bool, expected: str
) -> None:
    assert (
        classify_startup_promotion_parity(
            review_status="proposed",
            candidate=candidate,
            current_obligation_covered=covered,
            prerequisites_enabled=True,
        )
        == expected
    )


def test_shadow_distinguishes_committed_and_unexplained_state() -> None:
    candidate = _candidate(would_promote=True, selected_basis="legacy_confidence")
    assert (
        classify_startup_promotion_parity(
            review_status="active",
            candidate=candidate,
            current_obligation_covered=False,
            prerequisites_enabled=True,
        )
        == "parity_already_committed"
    )
    assert (
        classify_startup_promotion_parity(
            review_status="rejected",
            candidate=candidate,
            current_obligation_covered=False,
            prerequisites_enabled=True,
        )
        == "mismatch_state"
    )


def test_shadow_accepts_a_healthy_mixed_version_legacy_obligation() -> None:
    """The reconciliation union marks exact legacy path_a coverage healthy.

    The observer receives that union as ``current_obligation_covered`` rather
    than looking only at canonical job coverage, preserving B5 mixed-version
    safety while old queued jobs drain.
    """
    assert (
        classify_startup_promotion_parity(
            review_status="proposed",
            candidate=_candidate(would_promote=True, selected_basis="legacy_confidence"),
            current_obligation_covered=True,
            prerequisites_enabled=True,
        )
        == "parity_durably_scheduled"
    )
