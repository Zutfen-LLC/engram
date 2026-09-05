"""Contracts for the blinded Reviewer A freeze and Reviewer B handoff."""

from __future__ import annotations

import copy
from datetime import UTC, datetime

import pytest


def _packet() -> dict[str, object]:
    return {
        "packet_schema_version": "engram-blind-review-packet-v1",
        "selection_digest": "a" * 64,
        "case_count": 2,
        "cases": [
            {
                "review_case_id": "rvw_aaaaaaaaaaaaaaaaaaaaaaaa",
                "content": "first private memory",
                "stored_kind": "fact",
                "source_assertion_mode": {
                    "source_type": "manual",
                    "safe_provenance": "unknown_not_recorded",
                },
                "captured": {"at": "2026-09-01T00:00:00+00:00", "age_bucket": "72hto7d"},
                "decision_time_evidence_context": {
                    "recorded": "none_recorded",
                    "assertion_origin": "unknown_not_recorded",
                    "evidence_root_independence": "unknown_not_recorded",
                },
                "governance_context": {
                    "conflict": "none_recorded",
                    "external_dispute": "none_recorded",
                    "supersession": "none_recorded",
                    "temporal_validity": "unknown_not_recorded",
                    "scope": "unknown_not_recorded",
                },
            },
            {
                "review_case_id": "rvw_bbbbbbbbbbbbbbbbbbbbbbbb",
                "content": "second private memory",
                "stored_kind": "doctrine",
                "source_assertion_mode": {
                    "source_type": "migration",
                    "safe_provenance": "unknown_not_recorded",
                },
                "captured": {"at": "2026-09-01T00:00:00+00:00", "age_bucket": "72hto7d"},
                "decision_time_evidence_context": {
                    "recorded": "none_recorded",
                    "assertion_origin": "unknown_not_recorded",
                    "evidence_root_independence": "unknown_not_recorded",
                },
                "governance_context": {
                    "conflict": "none_recorded",
                    "external_dispute": "none_recorded",
                    "supersession": "none_recorded",
                    "temporal_validity": "unknown_not_recorded",
                    "scope": "unknown_not_recorded",
                },
            },
        ],
    }


def _decision(case: int, review_case_id: str, *, consequence: str = "low") -> dict[str, object]:
    return {
        "case": case,
        "review_case_id": review_case_id,
        "expected_kind": "doctrine" if consequence == "high" else "fact",
        "atomic": "no",
        "proposition_count": "multiple",
        "retention_value": "retain",
        "epistemic_state": "unknown",
        "consequence": consequence,
        "expected_storage_disposition": "defer" if consequence == "high" else "retain",
        "expected_startup_eligibility": "no",
        "expected_governed_semantic_eligibility": "no",
        "human_review_required": "yes" if consequence == "high" else "no",
        "temporal_validity_issue": "unknown",
        "supersession_expected": "unknown",
        "acceptable_abstention": "yes",
        "expected_scope": "unknown",
        "assertion_origin": "unknown",
        "evidence_independence": "unknown",
        "scope_visibility_concern": "unknown",
        "dispute_expected": "unknown",
        "conflict_expected": "unknown",
        "factual_outcome": None,
        "expected_next_action": "review" if consequence == "high" else "automatic_admission",
        "reviewer_a_ratified": True,
        "reviewer_a_identity": "operator",
    }


def _ledger() -> dict[str, object]:
    return {
        "ledger_version": "eng-calibration-001b-reviewer-a-ledger-v1",
        "policy_blind": True,
        "reviewer_a_status": "frozen",
        "case_count": 2,
        "decisions": [
            _decision(1, "rvw_aaaaaaaaaaaaaaaaaaaaaaaa"),
            _decision(2, "rvw_bbbbbbbbbbbbbbbbbbbbbbbb", consequence="high"),
        ],
    }


def test_expansion_is_deterministic_and_preserves_unknown_provenance() -> None:
    from evals.admission.reviewer_adjudication import expand_reviewer_a

    first = expand_reviewer_a(_packet(), _ledger(), frozen_at=datetime(2026, 9, 5, tzinfo=UTC))
    second = expand_reviewer_a(_packet(), _ledger(), frozen_at=datetime(2026, 9, 5, tzinfo=UTC))
    assert first == second
    label = first["records"][0]["label"]
    assert label["reviewer_a"]["dimensions"]["assertion_origin"] == "unknown"
    assert label["reviewer_a"]["dimensions"]["evidence_independence"] == "unknown"
    assert label["reviewer_a"]["dimensions"]["source_span"] == "unavailable"


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown"])
def test_ledger_membership_fails_closed(mutation: str) -> None:
    from evals.admission.reviewer_adjudication import expand_reviewer_a

    ledger = _ledger()
    decisions = ledger["decisions"]
    assert isinstance(decisions, list)
    if mutation == "missing":
        decisions.pop()
    elif mutation == "duplicate":
        decisions.append(copy.deepcopy(decisions[0]))
    else:
        decisions[0]["review_case_id"] = "rvw_cccccccccccccccccccccccc"
    with pytest.raises(ValueError, match="ledger_membership_mismatch"):
        expand_reviewer_a(_packet(), ledger, frozen_at=datetime(2026, 9, 5, tzinfo=UTC))


def test_high_consequence_freeze_is_schema_valid_but_reviewer_b_pending() -> None:
    from evals.admission.reviewer_adjudication import expand_reviewer_a

    frozen = expand_reviewer_a(_packet(), _ledger(), frozen_at=datetime(2026, 9, 5, tzinfo=UTC))
    high = frozen["records"][1]["label"]
    assert high["reviewer_b"] is None
    assert high["review_stage"] == "reviewer_b_pending"


def test_reviewer_b_packet_derives_only_high_and_leaks_no_reviewer_a_or_policy() -> None:
    from evals.admission.reviewer_adjudication import expand_reviewer_a, reviewer_b_queue

    frozen = expand_reviewer_a(_packet(), _ledger(), frozen_at=datetime(2026, 9, 5, tzinfo=UTC))
    packet, state = reviewer_b_queue(frozen, _packet())
    assert [case["review_case_id"] for case in packet["cases"]] == ["rvw_bbbbbbbbbbbbbbbbbbbbbbbb"]
    text = str(packet)
    assert "reviewer_a" not in text and "consequence" not in text and "current_policy" not in text
    assert state["cases"][0]["reviewer_b"] is None
