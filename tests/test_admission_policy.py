"""Pure contract tests for the #158 risk-aware shadow policy."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jsonschema  # type: ignore[import-untyped]
import pytest

from engram.admission_policy import (
    AdmissionItemState,
    EffectiveAssessmentState,
    PolicyLoadError,
    evaluate_admission_profile,
    load_admission_policy,
)

NOW = datetime(2026, 9, 6, tzinfo=UTC)
CONTRACT_HASH = "sha256:d907baa0b6c6aab01cd4a848f5f967f69aa857200b26160ce4bfe50b9f0e718d"


def item(**changes: object) -> AdmissionItemState:
    values: dict[str, object] = {
        "item_id": "00000000-0000-0000-0000-000000000001",
        "tenant_id": "00000000-0000-0000-0000-000000000002",
        "content_hash": "sha256:" + "a" * 64,
        "kind": "fact",
        "source_type": "manual",
        "assertion_mode": "direct_statement",
        "origin": "direct_user",
        "review_status": "proposed",
        "created_at": NOW - timedelta(hours=80),
        "valid_to": None,
        "superseded_by": None,
        "unresolved_conflict": False,
        "external_dispute": False,
        "governed_review_required": False,
        "human_verified": False,
    }
    values.update(changes)
    return AdmissionItemState(**values)


def evidence(**changes: object) -> EffectiveAssessmentState:
    values: dict[str, object] = {
        "selection_status": "selected",
        "contract_hash": CONTRACT_HASH,
        "assessment_refs": (
            {
                "assessment_id": "00000000-0000-0000-0000-000000000003",
                "contract_hash": CONTRACT_HASH,
                "canonical_hash": "sha256:" + "c" * 64,
                "purpose": "combined",
            },
        ),
        "risk_state": "low",
        "epistemic_state": "supported",
        "retention_state": "retain",
        "calibrated": True,
    }
    values.update(changes)
    return EffectiveAssessmentState(**values)


def test_checked_in_candidate_policy_has_a_deterministic_digest() -> None:
    policy = load_admission_policy("risk_aware_shadow_v1")

    assert policy.profile_key == "risk_aware_shadow_v1"
    assert policy.artifact_digest.startswith("sha256:")
    assert load_admission_policy("risk_aware_shadow_v1") == policy


def test_checked_in_policy_validates_against_the_static_schema() -> None:
    artifact = json.loads(Path("policies/admission/risk_aware_shadow_v1.json").read_text())
    schema = json.loads(Path("schemas/admission-policy-v1.schema.json").read_text())

    jsonschema.Draft202012Validator(schema).validate(artifact)


def test_high_risk_fact_requires_governed_and_startup_review() -> None:
    decision = evaluate_admission_profile(
        item(), evidence(risk_state="high"), load_admission_policy("risk_aware_shadow_v1"), NOW
    )

    assert decision.surface_decisions == {
        "semantic_exploratory": "allow",
        "semantic_governed": "review_required",
        "startup": "review_required",
    }
    assert decision.highest_admission_tier == "semantic_exploratory"
    assert "risk_high" in decision.blocker_codes


def test_unknown_risk_never_falls_back_to_low_risk() -> None:
    decision = evaluate_admission_profile(
        item(), evidence(risk_state="unknown"), load_admission_policy("risk_aware_shadow_v1"), NOW
    )

    assert decision.surface_decisions["semantic_governed"] == "review_required"
    assert decision.surface_decisions["startup"] == "review_required"
    assert "risk_unknown" in decision.blocker_codes


@pytest.mark.parametrize(
    "status",
    ["disabled", "absent", "stale", "mismatched", "failed", "uncalibrated"],
)
def test_each_nonselected_assessment_state_remains_visible(status: str) -> None:
    decision = evaluate_admission_profile(
        item(),
        evidence(
            selection_status=status,
            risk_state="unknown",
            epistemic_state="unknown",
            retention_state="unknown",
            calibrated=False,
        ),
        load_admission_policy("risk_aware_shadow_v1"),
        NOW,
    )

    assert f"assessment_{status}" in decision.blocker_codes
    assert decision.surface_decisions == {
        "semantic_exploratory": "allow",
        "semantic_governed": "review_required",
        "startup": "review_required",
    }


def test_high_risk_with_missing_provenance_remains_review_required() -> None:
    decision = evaluate_admission_profile(
        item(origin="unknown"),
        evidence(risk_state="high"),
        load_admission_policy("risk_aware_shadow_v1"),
        NOW,
    )

    assert "risk_high" in decision.blocker_codes
    assert "provenance_origin_missing" in decision.blocker_codes
    assert decision.surface_decisions["semantic_governed"] == "review_required"
    assert decision.surface_decisions["startup"] == "review_required"


def test_unknown_risk_with_missing_provenance_remains_review_required() -> None:
    decision = evaluate_admission_profile(
        item(assertion_mode="unknown"),
        evidence(
            risk_state="unknown",
            epistemic_state="unknown",
            retention_state="unknown",
            calibrated=False,
        ),
        load_admission_policy("risk_aware_shadow_v1"),
        NOW,
    )

    assert "risk_unknown" in decision.blocker_codes
    assert "provenance_assertion_mode_missing" in decision.blocker_codes
    assert decision.surface_decisions["semantic_governed"] == "review_required"
    assert decision.surface_decisions["startup"] == "review_required"


def test_required_origin_prevents_qualified_admission() -> None:
    decision = evaluate_admission_profile(
        item(origin="unknown"), evidence(), load_admission_policy("risk_aware_shadow_v1"), NOW
    )

    assert "provenance_origin_missing" in decision.blocker_codes
    assert decision.surface_decisions["semantic_governed"] == "withhold"


def test_declared_output_map_changes_the_evaluation() -> None:
    policy = load_admission_policy("risk_aware_shadow_v1")
    changed = replace(
        policy,
        outputs={
            **policy.outputs,
            "qualified": {
                "semantic_exploratory": "allow",
                "semantic_governed": "withhold",
                "startup": "blocked",
            },
        },
    )

    decision = evaluate_admission_profile(item(created_at=NOW), evidence(), changed, NOW)

    assert decision.surface_decisions["semantic_governed"] == "withhold"


def test_declared_precedence_changes_the_evaluation() -> None:
    policy = load_admission_policy("risk_aware_shadow_v1")
    changed = replace(
        policy,
        precedence=("qualified", *tuple(rule for rule in policy.precedence if rule != "qualified")),
    )

    decision = evaluate_admission_profile(
        item(), evidence(risk_state="medium", retention_state="transient"), changed, NOW
    )

    assert decision.surface_decisions["semantic_governed"] == "allow"


def test_evidence_starved_medium_risk_fact_routes_to_review() -> None:
    decision = evaluate_admission_profile(
        item(),
        evidence(risk_state="medium", epistemic_state="insufficient_evidence", calibrated=False),
        load_admission_policy("risk_aware_shadow_v1"),
        NOW,
    )

    assert decision.surface_decisions["semantic_governed"] == "review_required"
    assert decision.surface_decisions["startup"] == "review_required"
    assert "epistemic_insufficient" in decision.blocker_codes


def test_evidence_starved_low_risk_fact_remains_withheld() -> None:
    decision = evaluate_admission_profile(
        item(),
        evidence(risk_state="low", epistemic_state="insufficient_evidence", calibrated=False),
        load_admission_policy("risk_aware_shadow_v1"),
        NOW,
    )

    assert decision.surface_decisions["semantic_governed"] == "withhold"
    assert decision.surface_decisions["startup"] == "withhold"


def test_low_risk_governed_surface_has_no_observation_window() -> None:
    decision = evaluate_admission_profile(
        item(created_at=NOW), evidence(), load_admission_policy("risk_aware_shadow_v1"), NOW
    )

    assert decision.surface_decisions["semantic_governed"] == "allow"
    assert decision.highest_admission_tier == "semantic_governed"
    assert decision.observation_window_hours == 0
    assert decision.eligible_at == NOW
    assert decision.decision_hash == (
        "sha256:5b0d9aeefd700ff98f859062f484d17eacd31fac0b97e6b473284c381fb3fd6f"
    )


def test_v2_golden_decision_validates_against_the_checked_in_contract() -> None:
    policy = load_admission_policy("risk_aware_shadow_v1")
    subject = item(created_at=NOW)
    decision = evaluate_admission_profile(subject, evidence(), policy, NOW)
    schema = json.loads(Path("schemas/admission-assessment-v2.schema.json").read_text())

    jsonschema.Draft202012Validator(schema).validate(decision.envelope(subject))


def test_medium_risk_governed_surface_waits_for_72_hours_only() -> None:
    policy = load_admission_policy("risk_aware_shadow_v1")
    before = evaluate_admission_profile(
        item(created_at=NOW - timedelta(hours=71)), evidence(risk_state="medium"), policy, NOW
    )
    after = evaluate_admission_profile(
        item(created_at=NOW - timedelta(hours=72)), evidence(risk_state="medium"), policy, NOW
    )

    assert before.surface_decisions["semantic_governed"] == "withhold"
    assert after.surface_decisions["semantic_governed"] == "allow"
    assert before.risk_state == after.risk_state == "medium"
    assert before.epistemic_state == after.epistemic_state == "supported"
    assert before.retention_state == after.retention_state == "retain"


def test_existing_human_verified_authority_reaches_the_startup_tier() -> None:
    decision = evaluate_admission_profile(
        item(created_at=NOW, human_verified=True),
        evidence(),
        load_admission_policy("risk_aware_shadow_v1"),
        NOW,
    )

    assert decision.surface_decisions["startup"] == "allow"
    assert decision.highest_admission_tier == "startup"


def test_missing_policy_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from engram import admission_policy

    monkeypatch.setattr(admission_policy, "POLICY_DIRECTORY", tmp_path)
    with pytest.raises(PolicyLoadError, match="not found"):
        load_admission_policy("risk_aware_shadow_v1")


def test_policy_digest_drift_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from engram import admission_policy

    source = Path("policies/admission/risk_aware_shadow_v1.json")
    target = tmp_path / source.name
    target.write_text(source.read_text().replace("risk-aware-shadow-v1", "drifted-v1"))
    monkeypatch.setattr(admission_policy, "POLICY_DIRECTORY", tmp_path)

    with pytest.raises(PolicyLoadError, match="digest drift"):
        load_admission_policy("risk_aware_shadow_v1")
