"""Pure policy tests for V2-BL-005 feedback authority."""

from __future__ import annotations

import pytest

from engram.feedback import effect_for_feedback


@pytest.mark.parametrize(
    ("principal_type", "is_author", "verdict", "delta", "reset"),
    [
        ("user", False, "useful", 0.05, True),
        ("user", False, "noise", -0.10, False),
        ("admin", True, "useful", 0.05, True),
        ("admin", True, "noise", -0.10, False),
        ("agent", False, "useful", 0.025, False),
        ("agent", False, "noise", -0.05, False),
        ("agent", True, "useful", 0.0, False),
        ("agent", True, "noise", 0.0, False),
        ("system", False, "useful", 0.025, False),
        ("system", False, "noise", -0.05, False),
        ("system", True, "useful", 0.0, False),
        ("system", True, "noise", 0.0, False),
    ],
)
def test_effect_for_feedback_is_exhaustive(
    principal_type: str,
    is_author: bool,
    verdict: str,
    delta: float,
    reset: bool,
) -> None:
    effect = effect_for_feedback(  # type: ignore[arg-type]
        principal_type=principal_type, is_item_author=is_author, verdict=verdict
    )
    assert effect.importance_delta == delta
    assert effect.reset_startup_recall_count is reset


@pytest.mark.parametrize(
    ("principal_type", "is_author", "old", "new", "net"),
    [
        ("user", False, "useful", "noise", -0.15),
        ("user", False, "noise", "useful", 0.15),
        ("admin", False, "useful", "noise", -0.15),
        ("admin", False, "noise", "useful", 0.15),
        ("agent", False, "useful", "noise", -0.075),
        ("agent", False, "noise", "useful", 0.075),
        ("system", False, "useful", "noise", -0.075),
        ("system", False, "noise", "useful", 0.075),
        ("agent", True, "useful", "noise", 0.0),
        ("agent", True, "noise", "useful", 0.0),
    ],
)
def test_replacement_net_delta(
    principal_type: str, is_author: bool, old: str, new: str, net: float
) -> None:
    old_effect = effect_for_feedback(  # type: ignore[arg-type]
        principal_type=principal_type, is_item_author=is_author, verdict=old
    )
    new_effect = effect_for_feedback(  # type: ignore[arg-type]
        principal_type=principal_type, is_item_author=is_author, verdict=new
    )
    assert new_effect.importance_delta - old_effect.importance_delta == pytest.approx(net)
