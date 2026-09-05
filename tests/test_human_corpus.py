"""Contracts for final human corpus freeze and post-freeze policy comparison."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest


def _judgment(*, kind: str = "fact", consequence: str = "high") -> dict[str, object]:
    return {
        "expected_kind": kind,
        "retention_value": "retain",
        "epistemic_state": "weakly_supported",
        "consequence": consequence,
        "expected_storage_disposition": "defer",
        "expected_startup_eligibility": "no",
        "expected_governed_semantic_eligibility": "yes",
        "human_review_required": "yes",
        "flags": ["temporal"],
    }


def _packet() -> dict[str, object]:
    return {
        "packet_schema_version": "engram-blind-review-packet-v1",
        "selection_digest": "a" * 64,
        "case_count": 1,
        "cases": [{"review_case_id": "rvw_aaaaaaaaaaaaaaaaaaaaaaaa"}],
    }


def _reviewer_a_frozen() -> dict[str, object]:
    return {
        "artifact_schema_version": "engram-reviewer-a-frozen-v1",
        "source_packet_schema_version": "engram-blind-review-packet-v1",
        "source_packet_digest": "b" * 64,
        "source_selection_digest": "a" * 64,
        "reviewer_a_provenance": {"policy_output_visible": False},
        "frozen_at": "2026-09-05T00:00:00+00:00",
        "records": [
            {
                "case": 1,
                "review_case_id": "rvw_aaaaaaaaaaaaaaaaaaaaaaaa",
                "label": {
                    "sample_id": "rvw_aaaaaaaaaaaaaaaaaaaaaaaa",
                    "label_schema_version": "engram-admission-label-v1",
                    "dataset_id": "eng-calibration-001b",
                    "dataset_version": "reviewer-a-frozen-v1",
                    "source_sample_ref": "rvw_aaaaaaaaaaaaaaaaaaaaaaaa",
                    "content_hash": None,
                    "fixture_role": "ordinary_claim",
                    "label_origin": "human_adjudicated",
                    "reviewer_a": {
                        "adjudicator_ref": "operator",
                        "adjudicated_at": "2026-09-05T00:00:00+00:00",
                        "adjudicator_confidence": "high",
                        "reason_code": "blind_interactive_ratification",
                        "dimensions": {
                            "atomic": "yes",
                            "proposition_count": "one",
                            "attribution": "unavailable",
                            "source_span": "unavailable",
                            "evidence_span": "unavailable",
                            "assertion_origin": "unknown",
                            "expected_kind": "fact",
                            "expected_subject_or_domain": "unknown",
                            "expected_scope": "private",
                            "retention_value": "retain",
                            "epistemic_state": "weakly_supported",
                            "factual_outcome": "not_yet_known",
                            "consequence": "high",
                            "expected_storage_disposition": "defer",
                            "expected_startup_eligibility": "no",
                            "expected_governed_semantic_eligibility": "yes",
                            "human_review_required": "yes",
                            "acceptable_abstention": "yes",
                            "conflict_expected": "no",
                            "dispute_expected": "no",
                            "supersession_expected": "no",
                            "temporal_validity_issue": "yes",
                            "scope_visibility_concern": "unknown",
                            "evidence_independence": "unknown",
                            "expected_blockers": None,
                            "expected_next_action": "review",
                        },
                        "usefulness": None,
                    },
                    "reviewer_b": None,
                    "resolution": None,
                    "disagreement": "none",
                    "review_stage": "reviewer_b_pending",
                },
            }
        ],
    }


def _ledger() -> dict[str, object]:
    return {
        "artifact_version": "eng-calibration-001b-reviewer-b-ledger-v1",
        "policy_blind": True,
        "reviewer_b_status": "completed_independent",
        "case_count": 1,
        "records": [
            {
                "original_case": 1,
                "review_case_id": "rvw_aaaaaaaaaaaaaaaaaaaaaaaa",
                "reviewer_b": _judgment(),
            }
        ],
    }


def _resolution() -> dict[str, object]:
    return {
        "artifact_version": "eng-calibration-001b-adjudication-resolution-v1",
        "policy_blind": True,
        "reviewer_b_count": 1,
        "adjudication_status": "operator_ratified",
        "records": [
            {
                "original_case": 1,
                "review_case_id": "rvw_aaaaaaaaaaaaaaaaaaaaaaaa",
                "final": _judgment(kind="doctrine"),
                "resolution_note": "ratified",
            }
        ],
    }


def test_finalization_preserves_both_reviewers_and_resolves_difference() -> None:
    from evals.admission.human_corpus import finalize_human_corpus

    frozen = _reviewer_a_frozen()
    frozen["frozen_digest"] = __import__("evals.admission.schema", fromlist=["digest"]).digest(
        frozen
    )
    artifact = finalize_human_corpus(
        _packet(), frozen, _ledger(), _resolution(), frozen_at=datetime(2026, 9, 5, tzinfo=UTC)
    )

    record = artifact["records"][0]["label"]
    assert record["reviewer_a"]["dimensions"]["expected_kind"] == "fact"
    assert record["reviewer_b"]["dimensions"]["expected_kind"] == "fact"
    assert record["resolution"]["dimensions"]["expected_kind"] == "doctrine"
    assert record["disagreement"] == "resolved"
    assert artifact["summary"]["final_valid_label_count"] == 1
    assert artifact["summary"]["unresolved_disagreement_count"] == 0


@pytest.mark.parametrize("mutation", ["policy", "membership"])
def test_reviewer_b_ledger_fails_closed(mutation: str) -> None:
    from evals.admission.human_corpus import finalize_human_corpus
    from evals.admission.schema import digest

    frozen = _reviewer_a_frozen()
    frozen["frozen_digest"] = digest(frozen)
    ledger = _ledger()
    if mutation == "policy":
        ledger["records"][0]["current_policy_version"] = "promotion-evidence-v1"  # type: ignore[index]
    else:
        ledger["records"][0]["review_case_id"] = "rvw_bbbbbbbbbbbbbbbbbbbbbbbb"  # type: ignore[index]
    with pytest.raises(ValueError):
        finalize_human_corpus(
            _packet(), frozen, ledger, _resolution(), frozen_at=datetime(2026, 9, 5, tzinfo=UTC)
        )


def test_compare_requires_immutable_complete_final_corpus() -> None:
    from evals.admission.human_corpus import assert_reveal_gate
    from evals.admission.schema import digest

    frozen = _reviewer_a_frozen()
    frozen["frozen_digest"] = digest(frozen)
    artifact = {
        "artifact_schema_version": "engram-final-human-corpus-v1",
        "reviewer_a_frozen_digest": frozen["frozen_digest"],
        "records": frozen["records"],
        "summary": {
            "final_valid_label_count": 1,
            "case_count": 1,
            "unresolved_disagreement_count": 1,
            "high_consequence_without_b_count": 1,
        },
    }
    with pytest.raises(ValueError, match="reveal_gate_failed"):
        assert_reveal_gate(artifact)
