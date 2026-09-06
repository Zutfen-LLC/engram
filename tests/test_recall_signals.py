"""Unit tests for the separated recall signal model (issue #160 / ENG-RECALL-003).

Pure-function contract tests — no DB. These pin the core ENG-RECALL-003
invariants:

* relevance, utility, epistemic state, governance, and risk stay separate;
* importance/utility can reorder admitted items but never change epistemic
  state or the admission decision;
* unknown evidence is admitted-or-withheld-and-marked, never converted into a
  numeric trust floor.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from engram.models import MemoryItem
from engram.recall_profiles import EXPLORATORY_PROFILE, GOVERNED_PROFILE
from engram.recall_signals import (
    RECALL_ADMISSION_POLICY_VERSION,
    SIGNALS_VERSION,
    AdmissionAssessmentBinding,
    compute_signal_rank_score,
    compute_utility_score,
    decide_recall_admission,
    derive_epistemic_state,
    signal_item_fields,
    structured_warning_codes,
)

_NOW = datetime(2026, 9, 6, tzinfo=UTC)


def _make_item(**overrides: Any) -> MemoryItem:
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "tenant_id": uuid4(),
        "workspace_id": None,
        "principal_id": uuid4(),
        "content": "test memory content",
        "content_hash": "sha256:abc123",
        "kind": "fact",
        "visibility": "workspace",
        "review_status": "active",
        "memory_confidence": 0.5,
        "source_trust": 0.5,
        "human_verified": False,
        "verified_by": None,
        "verified_at": None,
        "importance": 0.5,
        "pinned": False,
        "last_recalled_at": None,
        "recall_count": 0,
        "startup_recall_count": 0,
        "last_verified_at": None,
        "source_type": "manual",
        "source_session": None,
        "source_uri": None,
        "extracted_by_model": None,
        "extraction_confidence": None,
        "conflicts_with_item_id": None,
        "conflict_type": None,
        "conflict_resolution_status": None,
        "conflict_resolved_by": None,
        "conflict_resolved_at": None,
        "sensitivity": "normal",
        "external_id": None,
        "external_source": None,
        "valid_from": _NOW - timedelta(days=1),
        "valid_to": None,
        "superseded_by": None,
        "created_at": _NOW - timedelta(days=1),
        "wing": None,
        "room": None,
        "subject_type": None,
        "subject_id": None,
        "subject_name": None,
    }
    defaults.update(overrides)
    return MemoryItem(**defaults)


# ---- utility ----


def test_utility_is_monotonic_in_importance() -> None:
    low = compute_utility_score(
        importance=0.1, created_at=_NOW, valid_from=_NOW, now=_NOW
    )
    high = compute_utility_score(
        importance=0.9, created_at=_NOW, valid_from=_NOW, now=_NOW
    )
    assert 0.0 <= low < high <= 1.0


def test_utility_decays_with_age_but_stays_nonnegative() -> None:
    fresh = compute_utility_score(
        importance=0.5, created_at=_NOW, valid_from=_NOW, now=_NOW
    )
    old = compute_utility_score(
        importance=0.5,
        created_at=_NOW - timedelta(days=90),
        valid_from=_NOW - timedelta(days=90),
        now=_NOW,
    )
    assert fresh > old >= 0.0


def test_utility_ignores_epistemic_inputs() -> None:
    """source_trust / memory_confidence / human_verified are epistemic-state
    inputs, not utility inputs — the signature must not even accept them, and
    recall_count (exposure) must never enter utility."""
    import inspect

    params = inspect.signature(compute_utility_score).parameters
    assert "source_trust" not in params
    assert "memory_confidence" not in params
    assert "human_verified" not in params
    assert "recall_count" not in params


# ---- epistemic state ----


def test_epistemic_state_matrix() -> None:
    assert (
        derive_epistemic_state(
            review_status="proposed",
            human_verified=False,
            conflict_resolution_status=None,
        )
        == "unknown"
    )
    # Even a human-verified proposal is unadmitted evidence.
    assert (
        derive_epistemic_state(
            review_status="proposed",
            human_verified=True,
            conflict_resolution_status=None,
        )
        == "unknown"
    )
    assert (
        derive_epistemic_state(
            review_status="disputed", human_verified=False, conflict_resolution_status=None
        )
        == "contested"
    )
    assert (
        derive_epistemic_state(
            review_status="active",
            human_verified=False,
            conflict_resolution_status="unresolved",
        )
        == "contested"
    )
    assert (
        derive_epistemic_state(
            review_status="active", human_verified=True, conflict_resolution_status=None
        )
        == "supported"
    )
    assert (
        derive_epistemic_state(
            review_status="active", human_verified=False, conflict_resolution_status=None
        )
        == "insufficient_evidence"
    )


def test_importance_never_changes_epistemic_state() -> None:
    import inspect

    # importance is not even an input to the derivation, at any value.
    assert "importance" not in inspect.signature(derive_epistemic_state).parameters
    for _importance in (0.0, 0.5, 1.0):
        assert (
            derive_epistemic_state(
                review_status="active",
                human_verified=False,
                conflict_resolution_status=None,
            )
            == "insufficient_evidence"
        )


# ---- ranking ----


def test_rank_relevance_dominates_utility() -> None:
    """A large relevance gap cannot be flipped by maximum utility."""
    far_but_useful = compute_signal_rank_score(similarity=0.4, utility=1.0)
    near_but_boring = compute_signal_rank_score(similarity=0.9, utility=0.0)
    assert near_but_boring > far_but_useful


def test_rank_utility_orders_equal_relevance() -> None:
    assert compute_signal_rank_score(similarity=0.8, utility=0.9) > compute_signal_rank_score(
        similarity=0.8, utility=0.2
    )


def test_rank_is_deterministic_and_bounded() -> None:
    a = compute_signal_rank_score(similarity=0.77, utility=0.33)
    b = compute_signal_rank_score(similarity=0.77, utility=0.33)
    assert a == b
    assert 0.0 <= a <= 1.0


# ---- admission: governed ----


def test_governed_admits_active_item_without_assessment() -> None:
    decision = decide_recall_admission(_make_item(), profile=GOVERNED_PROFILE, stay_kinds=set())
    assert decision.decision == "admit"
    assert decision.reason_codes == ("admitted_review_active",)
    assert decision.assessment_id is None


def test_governed_binds_current_admitted_assessment() -> None:
    binding = AdmissionAssessmentBinding(
        assessment_id=str(uuid4()), status="current", outcome="admitted"
    )
    decision = decide_recall_admission(
        _make_item(), profile=GOVERNED_PROFILE, stay_kinds=set(), assessment=binding
    )
    assert decision.decision == "admit"
    assert decision.assessment_id == binding.assessment_id
    assert decision.assessment_status == "current"
    assert decision.assessment_outcome == "admitted"


def test_governed_withholds_highly_important_proposal() -> None:
    """Issue eval requirement 3: a highly similar (or important) proposal with
    unknown evidence is excluded from governed mode — no numeric trust floor
    lets it through."""
    item = _make_item(review_status="proposed", importance=1.0)
    decision = decide_recall_admission(item, profile=GOVERNED_PROFILE, stay_kinds=set())
    assert decision.decision == "withhold"
    assert "proposed_not_admitted" in decision.reason_codes


def test_governed_withholds_item_with_stale_assessment() -> None:
    """Issue eval requirement 4: an active item under a stale policy
    assessment is withheld, not silently served."""
    binding = AdmissionAssessmentBinding(
        assessment_id=str(uuid4()), status="stale", outcome="admitted"
    )
    decision = decide_recall_admission(
        _make_item(), profile=GOVERNED_PROFILE, stay_kinds=set(), assessment=binding
    )
    assert decision.decision == "withhold"
    assert "admission_assessment_stale" in decision.reason_codes


def test_governed_withholds_blocked_assessment() -> None:
    binding = AdmissionAssessmentBinding(
        assessment_id=str(uuid4()), status="current", outcome="blocked"
    )
    decision = decide_recall_admission(
        _make_item(), profile=GOVERNED_PROFILE, stay_kinds=set(), assessment=binding
    )
    assert decision.decision == "withhold"
    assert "admission_blocked" in decision.reason_codes


def test_governed_admits_but_marks_legacy_import_assessment() -> None:
    binding = AdmissionAssessmentBinding(
        assessment_id=str(uuid4()), status="legacy_import", outcome="admitted"
    )
    decision = decide_recall_admission(
        _make_item(), profile=GOVERNED_PROFILE, stay_kinds=set(), assessment=binding
    )
    assert decision.decision == "admit"
    assert "admission_legacy_import" in decision.reason_codes


def test_governed_disputed_follows_stay_kind_policy() -> None:
    stay_item = _make_item(review_status="disputed", kind="doctrine")
    decision = decide_recall_admission(
        stay_item, profile=GOVERNED_PROFILE, stay_kinds={"doctrine"}
    )
    assert decision.decision == "admit"
    assert "admitted_disputed_stay_kind" in decision.reason_codes

    leave_item = _make_item(review_status="disputed", kind="whisper")
    decision = decide_recall_admission(
        leave_item, profile=GOVERNED_PROFILE, stay_kinds={"doctrine"}
    )
    assert decision.decision == "withhold"
    assert "review_status_ineligible" in decision.reason_codes


def test_disputed_stay_kind_admission_binds_assessment() -> None:
    """An admitted disputed stay-kind item carries its assessment binding —
    served items must be bound to the decision that authorized them."""
    binding = AdmissionAssessmentBinding(
        assessment_id=str(uuid4()), status="current", outcome="admitted"
    )
    decision = decide_recall_admission(
        _make_item(review_status="disputed", kind="doctrine"),
        profile=GOVERNED_PROFILE,
        stay_kinds={"doctrine"},
        assessment=binding,
    )
    assert decision.decision == "admit"
    assert decision.assessment_id == binding.assessment_id
    assert decision.assessment_status == "current"


def test_disputed_stay_kind_blocked_assessment_withholds_in_every_profile() -> None:
    """An explicit policy block wins even for a governed stay kind — a
    disputed stay-kind item must not slip past a blocked assessment."""
    binding = AdmissionAssessmentBinding(
        assessment_id=str(uuid4()), status="current", outcome="blocked"
    )
    for profile in (GOVERNED_PROFILE, EXPLORATORY_PROFILE):
        decision = decide_recall_admission(
            _make_item(review_status="disputed", kind="doctrine"),
            profile=profile,
            stay_kinds={"doctrine"},
            assessment=binding,
        )
        assert decision.decision == "withhold"
        assert "admission_blocked" in decision.reason_codes


# ---- admission: exploratory ----


def test_exploratory_admits_proposal_as_unknown_evidence() -> None:
    decision = decide_recall_admission(
        _make_item(review_status="proposed"), profile=EXPLORATORY_PROFILE, stay_kinds=set()
    )
    assert decision.decision == "admit"
    assert "exploratory_proposal" in decision.reason_codes


def test_exploratory_admits_but_marks_stale_assessment() -> None:
    """Exploratory marks stale assessments instead of withholding — its whole
    purpose is structured uncertainty — but a hard policy block still wins."""
    binding = AdmissionAssessmentBinding(
        assessment_id=str(uuid4()), status="stale", outcome="admitted"
    )
    decision = decide_recall_admission(
        _make_item(), profile=EXPLORATORY_PROFILE, stay_kinds=set(), assessment=binding
    )
    assert decision.decision == "admit"
    assert "admission_assessment_stale" in decision.reason_codes


def test_exploratory_withholds_blocked_assessment() -> None:
    binding = AdmissionAssessmentBinding(
        assessment_id=str(uuid4()), status="current", outcome="blocked"
    )
    for review_status in ("active", "proposed"):
        decision = decide_recall_admission(
            _make_item(review_status=review_status),
            profile=EXPLORATORY_PROFILE,
            stay_kinds=set(),
            assessment=binding,
        )
        assert decision.decision == "withhold"
        assert "admission_blocked" in decision.reason_codes


def test_exploratory_disputed_non_stay_kind_withheld() -> None:
    decision = decide_recall_admission(
        _make_item(review_status="disputed", kind="whisper"),
        profile=EXPLORATORY_PROFILE,
        stay_kinds={"doctrine"},
    )
    assert decision.decision == "withhold"


# ---- admission invariants ----


def test_admission_ignores_relevance_and_utility_inputs() -> None:
    """Admission reads governance state only. The signature must not accept
    similarity/importance — popularity can never buy admission."""
    import inspect

    params = inspect.signature(decide_recall_admission).parameters
    assert "similarity" not in params
    assert "importance" not in params
    assert "recall_count" not in params


def test_importance_never_changes_admission() -> None:
    for importance in (0.0, 1.0):
        item = _make_item(review_status="proposed", importance=importance)
        assert (
            decide_recall_admission(item, profile=GOVERNED_PROFILE, stay_kinds=set()).decision
            == "withhold"
        )
        item = _make_item(review_status="active", importance=importance)
        assert (
            decide_recall_admission(item, profile=GOVERNED_PROFILE, stay_kinds=set()).decision
            == "admit"
        )


# ---- structured warnings + item fields ----


def test_warning_codes_are_machine_readable_and_mirror_warnings() -> None:
    codes = structured_warning_codes(
        review_status="proposed", conflict_resolution_status="unresolved"
    )
    assert "unreviewed" in codes
    assert "conflict_unresolved" in codes
    # disputed implies the conflict code even without an explicit status
    codes = structured_warning_codes(review_status="disputed", conflict_resolution_status=None)
    assert "conflict_unresolved" in codes
    assert "disputed" in codes


def test_signal_item_fields_expose_separate_signals_with_versions() -> None:
    item = _make_item(review_status="proposed", importance=0.8)
    decision = decide_recall_admission(item, profile=EXPLORATORY_PROFILE, stay_kinds=set())
    fields = signal_item_fields(
        item, decision=decision, similarity=0.9, now=_NOW
    )
    assert fields["signals_version"] == SIGNALS_VERSION
    assert fields["relevance_score"] == 0.9
    assert 0.0 <= fields["utility_score"] <= 1.0
    assert fields["epistemic_state"] == "unknown"
    assert "evidence_unknown" in fields["warning_codes"]
    assert fields["admission"]["profile"] == "exploratory"
    assert fields["admission"]["decision"] == "admit"
    assert fields["admission"]["policy_version"] == RECALL_ADMISSION_POLICY_VERSION
    assert "unreviewed" in fields["warnings"]
    # The rank score is reproducible from its published inputs.
    assert fields["score"] == compute_signal_rank_score(
        similarity=0.9, utility=fields["utility_score"]
    )


def test_signal_item_fields_never_expose_a_blended_trust_score() -> None:
    decision = decide_recall_admission(_make_item(), profile=GOVERNED_PROFILE, stay_kinds=set())
    fields = signal_item_fields(_make_item(), decision=decision, similarity=0.5, now=_NOW)
    assert "trust_score" not in fields
