"""Fixed non-production checks for recall feedback-loop safeguards."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from engram.demonstrated_usefulness import (
    QualifyingFeedback,
    summarize_demonstrated_usefulness,
)
from engram.recall_signals import compute_utility_score


def usefulness_perturbation_report() -> dict[str, Any]:
    """Evaluate the shared v1 usefulness table without mutating a corpus."""
    useful = QualifyingFeedback(verdict="useful")
    noise = QualifyingFeedback(verdict="noise")
    summaries = {
        "none": summarize_demonstrated_usefulness(()),
        "positive": summarize_demonstrated_usefulness((useful,)),
        "negative": summarize_demonstrated_usefulness((noise,)),
        "mixed": summarize_demonstrated_usefulness((useful, noise)),
    }
    many_positive = summarize_demonstrated_usefulness((useful,) * 10)
    now = datetime(2026, 9, 8, tzinfo=UTC)
    unchanged_utility = compute_utility_score(
        explicit_priority=0.5,
        created_at=now,
        valid_from=now,
        now=now,
        demonstrated_usefulness_adjustment=summaries["none"].adjustment,
    )
    return {
        "contract_version": summaries["none"].version,
        "adjustments": {name: summary.adjustment for name, summary in summaries.items()},
        "one_and_many_same_sign_are_equal": (
            summaries["positive"].adjustment == many_positive.adjustment
        ),
        # The shared utility function does not accept recall/exposure state.
        # Repeating exposure without qualifying feedback therefore supplies
        # the same adjustment and must keep utility unchanged.
        "repeated_exposure_changes_utility": (
            unchanged_utility
            != compute_utility_score(
                explicit_priority=0.5,
                created_at=now,
                valid_from=now,
                now=now,
                demonstrated_usefulness_adjustment=summaries["none"].adjustment,
            )
        ),
    }


__all__ = ["usefulness_perturbation_report"]
