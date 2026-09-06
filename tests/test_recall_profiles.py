"""Unit tests for the recall profile registry (issue #160 / ENG-RECALL-003).

Pure-function contract tests — no DB. The registry is the policy selector for
recall admission: which review states are eligible, whether the separated
signal model and admission gate apply, and which budget caps constrain each
profile.
"""

from __future__ import annotations

import pytest

from engram.recall_profiles import (
    EXPLORATORY_PROFILE,
    GOVERNED_PROFILE,
    LEGACY_PROFILE,
    STARTUP_PROFILE_KEY,
    apply_profile_budget_caps,
    resolve_recall_profile,
)

# ---- registry shape ----


def test_legacy_profile_preserves_current_semantic_behavior() -> None:
    """legacy = the named compatibility profile: active+proposed, no admission
    gate, no signal model, no budget caps, existing ranking version."""
    assert LEGACY_PROFILE.key == "legacy"
    assert LEGACY_PROFILE.review_statuses == ("active", "proposed")
    assert LEGACY_PROFILE.signals_enabled is False
    assert LEGACY_PROFILE.ranking_version == "semantic-v3"
    assert LEGACY_PROFILE.item_budget_cap is None
    assert LEGACY_PROFILE.byte_budget_cap is None
    assert LEGACY_PROFILE.token_budget_cap is None


def test_governed_profile_admits_only_reviewed_corpus() -> None:
    """governed = ordinary operational recall: no proposed items, admission
    gate on, separated signals on."""
    assert GOVERNED_PROFILE.key == "governed"
    assert "proposed" not in GOVERNED_PROFILE.review_statuses
    assert "active" in GOVERNED_PROFILE.review_statuses
    # Disputed items enter the window but must survive the stay-kind gate.
    assert "disputed" in GOVERNED_PROFILE.review_statuses
    assert GOVERNED_PROFILE.signals_enabled is True
    assert GOVERNED_PROFILE.ranking_version == "semantic-signals-v1"


def test_exploratory_profile_includes_proposals_with_lower_budgets() -> None:
    assert EXPLORATORY_PROFILE.key == "exploratory"
    assert EXPLORATORY_PROFILE.review_statuses == ("active", "proposed")
    assert EXPLORATORY_PROFILE.signals_enabled is True
    # Exploratory packets are bounded tighter than governed ones.
    assert EXPLORATORY_PROFILE.item_budget_cap is not None
    assert EXPLORATORY_PROFILE.item_budget_cap < 50
    assert EXPLORATORY_PROFILE.byte_budget_cap is not None
    assert EXPLORATORY_PROFILE.byte_budget_cap < 4096


# ---- resolution ----


def test_semantic_mode_defaults_to_legacy() -> None:
    assert resolve_recall_profile(None, mode="semantic").key == "legacy"
    assert resolve_recall_profile("governed", mode="semantic").key == "governed"
    assert resolve_recall_profile("exploratory", mode="semantic").key == "exploratory"
    assert resolve_recall_profile("legacy", mode="semantic").key == "legacy"


def test_startup_mode_resolves_to_startup_profile() -> None:
    assert resolve_recall_profile(None, mode="startup").key == STARTUP_PROFILE_KEY
    assert resolve_recall_profile("startup", mode="startup").key == STARTUP_PROFILE_KEY


def test_semantic_only_profiles_rejected_for_startup_mode() -> None:
    with pytest.raises(ValueError, match="mode='semantic'"):
        resolve_recall_profile("governed", mode="startup")
    with pytest.raises(ValueError, match="mode='semantic'"):
        resolve_recall_profile("exploratory", mode="startup")


def test_unknown_profile_rejected_with_valid_options() -> None:
    with pytest.raises(ValueError, match="legacy"):
        resolve_recall_profile("review", mode="semantic")
    with pytest.raises(ValueError, match="governed"):
        resolve_recall_profile("audit", mode="semantic")


def test_profiles_are_singletons() -> None:
    """The registry hands out frozen specs so callers cannot mutate policy."""
    assert resolve_recall_profile("governed", mode="semantic") is GOVERNED_PROFILE
    with pytest.raises(AttributeError):
        GOVERNED_PROFILE.signals_enabled = False  # type: ignore[misc]


# ---- budget caps ----


def test_budget_caps_apply_only_when_profile_defines_them() -> None:
    assert apply_profile_budget_caps(GOVERNED_PROFILE, 999999, 888888, 777) == (
        999999,
        888888,
        777,
    )


def test_budget_caps_lower_but_never_raise_budgets() -> None:
    byte_cap = EXPLORATORY_PROFILE.byte_budget_cap
    item_cap = EXPLORATORY_PROFILE.item_budget_cap
    assert item_cap is not None and byte_cap is not None
    # Above the cap: clamped down.
    assert apply_profile_budget_caps(EXPLORATORY_PROFILE, 999999, 999999, 999) == (
        byte_cap,
        999999,
        item_cap,
    )
    # Below the cap: caller's tighter budget wins.
    assert apply_profile_budget_caps(EXPLORATORY_PROFILE, 10, 20, 3) == (10, 20, 3)
    # Unset budgets stay unset.
    assert apply_profile_budget_caps(EXPLORATORY_PROFILE, None, None, None) == (
        None,
        None,
        None,
    )
