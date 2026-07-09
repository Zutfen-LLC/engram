"""Unit tests for classification → trust/visibility policy functions.

These are pure-function tests (no FastAPI/Postgres), so they run in every
environment without the v2 schema. The integration of these helpers into the
write path is covered by tests/test_remember.py.
"""

from __future__ import annotations

import pytest

from engram.classification_trust import blend_memory_confidence, narrow_visibility

# ---- narrow_visibility ----


@pytest.mark.parametrize(
    "requested,suggested,expected",
    [
        # Classifier may only narrow.
        ("tenant", "private", "private"),
        ("tenant", "workspace", "workspace"),
        ("workspace", "private", "private"),
        ("public", "tenant", "tenant"),
        ("public", "private", "private"),
        # Equal or wider suggestions keep the requested scope (never widen).
        ("private", "tenant", "private"),
        ("private", "public", "private"),
        ("workspace", "public", "workspace"),
        ("tenant", "public", "tenant"),
        ("workspace", "workspace", "workspace"),
        ("tenant", None, "tenant"),
    ],
)
def test_narrow_visibility_only_narrows(requested, suggested, expected):
    assert narrow_visibility(requested, suggested) == expected


@pytest.mark.parametrize(
    "suggested",
    [None, "PUBLIC", "share", "", "global", "n/a"],
)
def test_narrow_visibility_ignores_invalid_suggestions(suggested):
    """Unknown/invalid suggestions preserve the requested visibility verbatim."""
    assert narrow_visibility("workspace", suggested) == "workspace"


def test_narrow_visibility_preserves_unknown_requested():
    """If the caller's own visibility is somehow outside the enum, keep it."""
    assert narrow_visibility("bogus", "private") == "bogus"


# ---- blend_memory_confidence ----


def test_blend_weak_source_low_classifier_lowers_confidence():
    # sync_turn: default=0.4, trust=0.4, automated weight 0.5
    value, blended = blend_memory_confidence(
        source_default_confidence=0.4,
        classifier_confidence=0.2,
        source_trust=0.4,
        source_type="sync_turn",
    )
    assert value == pytest.approx(0.30)
    assert blended is True


def test_blend_weak_source_high_classifier_capped_by_authority():
    # A confident classifier cannot let a low-trust source self-promote.
    value, blended = blend_memory_confidence(
        source_default_confidence=0.4,
        classifier_confidence=0.9,
        source_trust=0.4,
        source_type="sync_turn",
    )
    # blended = 0.65 but authority cap = max(0.4, 0.4) = 0.4, so the cap brings
    # the result back to the default. The net effect is no change, so the
    # blended flag is False — classification did not alter the stored outcome.
    assert value == pytest.approx(0.40)
    assert blended is False


def test_blend_manual_source_not_aggressively_downrated():
    # manual_user: default=0.9, trust=0.9, authoritative weight 0.15
    value, blended = blend_memory_confidence(
        source_default_confidence=0.9,
        classifier_confidence=0.2,
        source_trust=0.9,
        source_type="manual",
    )
    # 0.85*0.9 + 0.15*0.2 = 0.795 (modest drop, not aggressive)
    assert value == pytest.approx(0.795)
    assert blended is True


def test_blend_extraction_feels_classifier_strongly():
    value, _ = blend_memory_confidence(
        source_default_confidence=0.5,
        classifier_confidence=0.3,
        source_trust=0.5,
        source_type="extraction",
    )
    assert value == pytest.approx(0.40)


def test_blend_high_authority_high_classifier_keeps_default_via_cap():
    # When the blend would exceed the authority cap, the cap wins.
    value, blended = blend_memory_confidence(
        source_default_confidence=0.9,
        classifier_confidence=0.95,
        source_trust=0.9,
        source_type="manual",
    )
    assert value == pytest.approx(0.9)
    # result equals default → not considered blended
    assert blended is False


def test_blend_clamps_to_unit_interval():
    value, _ = blend_memory_confidence(
        source_default_confidence=1.0,
        classifier_confidence=1.0,
        source_trust=1.0,
        source_type="manual",
    )
    assert value == pytest.approx(1.0)
    value, _ = blend_memory_confidence(
        source_default_confidence=0.0,
        classifier_confidence=0.0,
        source_trust=0.0,
        source_type="sync_turn",
    )
    assert value == pytest.approx(0.0)


def test_blend_no_change_when_classifier_matches_default():
    value, blended = blend_memory_confidence(
        source_default_confidence=0.4,
        classifier_confidence=0.4,
        source_trust=0.4,
        source_type="sync_turn",
    )
    assert value == pytest.approx(0.4)
    assert blended is False
