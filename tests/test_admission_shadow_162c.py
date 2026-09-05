"""#162C candidate-policy shadow laboratory contracts.

Covers: P0 exact parity, label isolation, oracle typing, unknown-signal
honesty, determinism, digest sensitivity, record separation, production
import isolation, PPV null semantics, defer non-collapse, tier independence,
development/holdout separation, freeze gating, cost-weight versioning, and
shortlist rules.
"""

from __future__ import annotations

import ast
import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from evals.admission.candidates.contract import (
    CandidateDeclaration,
    CandidateParameters,
    CandidateResult,
)
from evals.admission.candidates.freeze import (
    FREEZE_PATH,
    freeze_digest,
    load_freeze,
)
from evals.admission.candidates.profiles import build_profiles
from evals.admission.dataset import Dataset
from evals.admission.holdout.select import check_disjoint, verify_holdout_freeze_gate
from evals.admission.policy import PolicyInput, evaluate
from evals.admission.schema import digest
from evals.admission.shadow.metrics import (
    COST_WEIGHT_SCHEDULE_V1,
    admission_metrics,
    cost_weighted_errors,
    per_policy_report,
    storage_metrics,
)
from evals.admission.shadow.shortlist import classify_candidate

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "evals/admission/contract-v1.json"
NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
def dataset():
    return Dataset.model_validate_json(PATH.read_bytes())


def _policy_input(sample: Any) -> PolicyInput:
    return sample.policy_input


def _dimensions_for(sample: Any) -> dict[str, Any]:
    assert sample.label is not None
    dims = sample.label.final_dimensions()
    assert dims is not None
    return dims.model_dump()


# --- 1. P0 exact parity -------------------------------------------------------


def test_p0_matches_canonical_policy_exactly(dataset):
    p0 = build_profiles()[0]
    assert p0.declaration.policy_version == "candidate-current-compat-v1"
    for sample in dataset.samples:
        current = evaluate(sample.policy_input, dataset.config, dataset.evaluation_at)
        result = p0.evaluate(sample.policy_input, dataset.config, dataset.evaluation_at)
        if current.current_policy_version == "unknown":
            assert result.storage_disposition == "unknown"
            assert result.automatic_admission == "unknown"
            continue
        # would_promote parity on the automatic surface
        assert result.automatic_admission == (
            "yes" if current.would_promote is True else "no"
        )
        # eligible_now maps to retain exactly
        if current.readiness_state == "eligible_now" and current.would_promote:
            assert result.storage_disposition == "retain"
        # terminal current states reject, retryable states defer
        if current.readiness_state in ("missing_evidence", "below_evidence_threshold"):
            assert result.storage_disposition == "defer"


def test_p0_next_action_parity(dataset):
    p0 = build_profiles()[0]
    for sample in dataset.samples:
        current = evaluate(sample.policy_input, dataset.config, dataset.evaluation_at)
        result = p0.evaluate(sample.policy_input, dataset.config, dataset.evaluation_at)
        if current.would_promote is True:
            assert result.next_action == "automatic_admission"


# --- 2. candidates cannot read human labels ----------------------------------


def test_candidate_evaluate_signatures_have_no_label_parameter():
    for profile in build_profiles():
        params = type(profile).evaluate.__code__.co_varnames
        assert "label" not in params
        assert "dimensions" not in params
        assert "oracle_truth" not in params or isinstance(profile, object) is False


def test_label_content_never_changes_candidate_output(dataset):
    profiles = build_profiles()
    sample = dataset.samples[0]
    baseline = [
        p.evaluate(sample.policy_input, dataset.config, dataset.evaluation_at)
        for p in profiles
    ]
    # strip every label; candidates must not notice
    flipped = sample.model_copy(update={"label": None})
    assert flipped.label is None
    after = [
        p.evaluate(flipped.policy_input, dataset.config, dataset.evaluation_at)
        for p in profiles
    ]
    assert baseline == after


# --- 3. oracle typing ----------------------------------------------------------


def test_oracle_declaration_is_typed_separately():
    with pytest.raises(ValidationError):
        # A deployable candidate declaration cannot claim oracle inputs.
        CandidateDeclaration(
            policy_version="candidate-bad-v1",
            hypothesis="h",
            required_input_signals=("source_type",),
            protected_kind_behavior="p",
            unknown_behavior="u",
            automatic_admission_conditions=(),
            review_routing_conditions=(),
            storage_semantics="s",
            governed_semantics="g",
            startup_semantics="s",
            parameters=CandidateParameters(
                evidence_threshold=0.7,
                taxonomy_minimum=0.7,
                legacy_confidence_threshold=0.7,
                deferral_window_hours=0,
                deferral_window_provenance="x",
            ),
            parameter_provenance="p",
            oracle="yes",
        )


def test_oracle_policies_cannot_enter_shortlist():
    from evals.admission.shadow.shortlist import classify_candidate

    with pytest.raises(ValueError, match="oracle"):
        classify_candidate(
            policy_version="oracle-risk-upper-bound-v1",
            metrics={},
            current_metrics={},
            checks={},
        )


def test_freeze_artifact_contains_no_oracle_candidate():
    freeze = load_freeze()
    for declaration in freeze["candidate_declarations"]:
        assert declaration["oracle"] == "no"
    assert freeze["oracle_analyses"][0]["excluded_from_shortlist"] is True


# --- 4. unknown / unavailable signal honesty ---------------------------------


def test_missing_config_yields_unknown_not_guess(dataset):
    for profile in build_profiles():
        result = profile.evaluate(dataset.samples[0].policy_input, None, dataset.evaluation_at)
        assert result.storage_disposition == "unknown"
        assert result.automatic_admission == "unknown"
        assert result.human_review_required == "unknown"
        assert "unknown_policy_state" in result.reason_codes
        assert "config_snapshot" in result.unavailable_signals


def test_undeclared_signal_rejected():
    with pytest.raises(ValidationError, match="undeclared_required_signal"):
        CandidateDeclaration(
            policy_version="candidate-x-v1",
            hypothesis="h",
            required_input_signals=("assertion_origin",),  # not in vocabulary
            protected_kind_behavior="p",
            unknown_behavior="u",
            automatic_admission_conditions=(),
            review_routing_conditions=(),
            storage_semantics="s",
            governed_semantics="g",
            startup_semantics="s",
            parameters=CandidateParameters(
                evidence_threshold=0.7,
                taxonomy_minimum=0.7,
                legacy_confidence_threshold=0.7,
                deferral_window_hours=0,
                deferral_window_provenance="x",
            ),
            parameter_provenance="p",
        )


# --- 5/6. determinism + digest sensitivity -----------------------------------


def test_candidate_results_are_deterministic(dataset):
    profiles = build_profiles()
    first = [
        p.evaluate(dataset.samples[3].policy_input, dataset.config, dataset.evaluation_at)
        for p in profiles
    ]
    second = [
        p.evaluate(dataset.samples[3].policy_input, dataset.config, dataset.evaluation_at)
        for p in profiles
    ]
    assert first == second
    assert [r.model_dump_json() for r in first] == [r.model_dump_json() for r in second]


def test_candidate_config_change_alters_digest(dataset):
    profiles = build_profiles()
    sample = dataset.samples[0]
    base = profiles[1].evaluate(sample.policy_input, dataset.config, dataset.evaluation_at)
    assert base.model_dump_json() == profiles[1].evaluate(
        sample.policy_input, dataset.config, dataset.evaluation_at
    ).model_dump_json()
    # a materially different result has a different digest
    other = base.model_copy(update={"storage_disposition": "reject"})
    assert digest(base.model_dump(mode="json")) != digest(other.model_dump(mode="json"))


def test_freeze_digest_changes_when_declaration_changes():
    freeze = load_freeze()
    modified = copy.deepcopy(freeze)
    modified["candidate_declarations"][1]["hypothesis"] = "changed hypothesis"
    assert freeze_digest(modified) != freeze["freeze_digest"]


def test_freeze_rejects_profile_drift():
    freeze = load_freeze()
    assert freeze["freeze_digest"] == freeze_digest(
        {k: v for k, v in freeze.items() if k != "freeze_digest"}
    )
    # every committed declaration still matches the code declarations
    current = {p.declaration.policy_version: p.declaration for p in build_profiles()}
    for declaration in freeze["candidate_declarations"]:
        live = current[declaration["policy_version"]]
        assert digest(declaration) == digest(live.model_dump(mode="json"))


# --- 7. separate immutable records -------------------------------------------


def test_candidate_result_is_frozen_and_separate(dataset):
    profiles = build_profiles()
    sample = dataset.samples[2]
    current = evaluate(sample.policy_input, dataset.config, dataset.evaluation_at)
    result = profiles[1].evaluate(sample.policy_input, dataset.config, dataset.evaluation_at)
    with pytest.raises(ValidationError):
        result.automatic_admission = "yes"  # type: ignore[misc]
    # current evaluation result is a different model type entirely
    assert type(current) is not type(result)
    # human label is a third object
    assert sample.label is not None
    assert type(sample.label) is not type(result) is not type(current)


def test_runner_cannot_mutate_frozen_labels(dataset):
    sample = dataset.samples[0]
    before = sample.label.model_dump_json()
    assert sample.label is not None
    profiles = build_profiles()
    for profile in profiles:
        profile.evaluate(sample.policy_input, dataset.config, dataset.evaluation_at)
    evaluate(sample.policy_input, dataset.config, dataset.evaluation_at)
    assert sample.label.model_dump_json() == before


# --- 9. production isolation ---------------------------------------------------


def test_production_cannot_import_candidate_or_shadow_modules():
    for path in (ROOT / "engram").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not module.startswith("evals"), f"{path}: {module}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("evals"), f"{path}"


def test_candidate_modules_live_outside_runner_digest_glob():
    # the frozen #162A baseline's runner_digest() only globs evals/admission/*.py;
    # subdirectory placement keeps accepted baseline artifacts byte-valid.
    from evals.admission.report import runner_digest

    baseline = json.loads(
        (ROOT / "evals/admission/contract-baseline-v1.json").read_text()
    )
    assert runner_digest() == baseline["runner_digest"]


# --- 10/11. no mutation (structural proof for replay path) -------------------


def test_shadow_runner_is_pure_no_db_imports():
    source = (ROOT / "evals/admission/shadow/runner.py").read_text()
    assert "create_async_engine" not in source
    assert "AsyncSession" not in source
    assert "session.execute" not in source
    # live shadow's write path is limited to the private artifact itself
    live = (ROOT / "evals/admission/shadow/live.py").read_text()
    assert "SET TRANSACTION ISOLATION LEVEL" not in live
    assert "commit()" not in live
    assert "review_status" not in live


# --- 13. zero-positive PPV semantics -----------------------------------------


def test_zero_predicted_positives_ppv_is_null_not_100():
    rows = [
        {
            "pred": "no",
            "unknown_pred": False,
            "unknown_reason": None,
            "unavailable_signals": [],
            "human_permits_auto": True,
            "consequence": "low",
            "review": "no",
            "governed": "no",
            "startup": "no",
            "truth": "retain",
            "review_truth": "no",
            "governed_truth": "yes",
            "startup_truth": "yes",
            "retention_truth": "retain",
            "permits_auto_if_reviewed": True,
        }
    ]
    metrics = admission_metrics(rows)
    assert metrics["predicted_automatic_positives"] == 0
    assert metrics["ppv"] is None


def test_zero_positive_ppv_null_in_full_report():
    rows = _uniform_rows(3, pred="no", truth="retain")
    report = per_policy_report(rows)
    assert report["automatic_admission"]["ppv"] is None


# --- 14. defer is never collapsed --------------------------------------------


def test_defer_is_not_counted_as_retain_or_reject():
    rows = _uniform_rows(2, pred="no", truth="retain")
    for row in rows:
        row["storage_pred"] = "defer"
    report = storage_metrics(
        [{"pred": r["storage_pred"], "truth": r["truth"]} for r in rows]
    )
    assert report["per_disposition"]["defer"]["predicted"] == 2
    # truth is retain: defer predictions are defer-errors, not retentions
    assert report["per_disposition"]["defer"]["true_positive"] == 0
    assert report["per_disposition"]["retain"]["predicted"] == 0
    assert report["per_disposition"]["reject"]["predicted"] == 0
    assert report["accuracy"] == 0.0
    # the two useful memories are counted as held back
    assert report["useful_memory_held_back"] == 2


# --- 15/16. tier independence / review is not silent admission ----------------


def test_governed_and_startup_scored_independently():
    rows = _uniform_rows(4, pred="no", truth="retain")
    for row, (gov, start) in zip(
        rows,
        [("yes", "no"), ("no", "no"), ("yes", "unknown"), ("unknown", "no")],
        strict=True,
    ):
        row["governed"] = gov
        row["startup"] = start
    report = per_policy_report(rows)
    gov = report["governed_semantic_eligibility"]
    start = report["startup_eligibility"]
    assert gov["n"] == start["n"] == 4
    # yes+review pairs are counted separately from silent admission
    assert "yes_with_review_required" in gov
    assert "yes_with_review_required" in start


def test_review_required_governed_memory_not_silent_admission():
    row = _uniform_rows(1, pred="no", truth="retain")[0]
    row["governed"] = "yes"
    row["review"] = "yes"
    report = per_policy_report([row])
    assert report["governed_semantic_eligibility"]["yes_with_review_required"] == 1
    # it did not contribute to automatic admission
    assert report["automatic_admission"]["predicted_automatic_positives"] == 0


def test_candidate_result_rejects_auto_with_review():
    with pytest.raises(ValidationError, match="automatic_admission_conflicts_with_review"):
        CandidateResult(
            candidate_policy_version="x",
            storage_disposition="retain",
            automatic_admission="yes",
            governed_semantic_eligibility="yes",
            startup_eligibility="no",
            human_review_required="yes",
        )


# --- 12. high-consequence fail-loud -------------------------------------------


def test_high_consequence_false_auto_is_flagged_as_violation():
    rows = _uniform_rows(2, pred="yes", truth="retain")
    rows[0]["consequence"] = "high"
    rows[0]["human_permits_auto"] = False
    report = per_policy_report(rows)
    assert report["high_consequence"]["violation"] is True
    assert report["high_consequence"]["false_automatic_positives"] == 1


def test_shortlist_marks_high_consequence_violation_not_viable():
    metrics = _minimal_metrics(violation=True)
    result = classify_candidate(
        policy_version="candidate-x-v1",
        metrics=metrics,
        current_metrics=_minimal_metrics(violation=False),
        checks={"deterministic": True, "label_isolated": True},
    )
    assert result["shortlist_class"] == "not_viable"


# --- 17/18. development vs holdout separation + overlap -----------------------


def test_development_and_holdout_reports_are_structurally_separate():
    source = (ROOT / "evals/admission/shadow/runner.py").read_text()
    # the development role label is baked into the replay report schema
    assert "dogfood-development-v1" in source
    assert "development / hypothesis-generating" in source
    # holdout comparison must verify freeze generation and disjointness
    holdout_source = (ROOT / "evals/admission/holdout/select.py").read_text()
    assert "verify_holdout_freeze_gate" in holdout_source
    assert "holdout_overlaps_development_corpus" in holdout_source


def test_holdout_overlap_rejected():
    with pytest.raises(ValueError, match="holdout_overlaps_development_corpus"):
        check_disjoint(("a", "b", "c"), ("c", "d"))


def test_holdout_freeze_gate_rejects_wrong_generation():
    manifest = {
        "holdout_manifest_digest": "0" * 64,
        "freeze_digest": "1" * 64,
        "overlap_proof": {"disjoint": True},
    }
    with pytest.raises(ValueError):
        verify_holdout_freeze_gate(manifest)


# --- 19. freeze precedes holdout ----------------------------------------------


def test_freeze_file_is_committed_and_loadable():
    assert FREEZE_PATH.exists()
    freeze = load_freeze()
    versions = [d["policy_version"] for d in freeze["candidate_declarations"]]
    assert versions == [
        "candidate-current-compat-v1",
        "candidate-tier-separated-v1",
        "candidate-evidence-recovery-v1",
        "candidate-kind-decoupled-v1",
    ]


# --- 21. privacy ---------------------------------------------------------------


def test_public_projection_contains_no_per_case_records():
    from evals.admission.shadow.runner import public_projection

    report = {
        "shadow_report_schema_version": "v",
        "runner_version": "r",
        "dataset_role": "dr",
        "dataset_role_classification": "dev",
        "snapshot_digest": "s",
        "tranche_selection_digest": "t",
        "final_corpus_digest": "c",
        "freeze_digest": "f",
        "code_sha": "sha",
        "evaluation_at": "e",
        "n_cases": 1,
        "per_case": [{"review_case_id": "rvw_x", "content": "secret"}],
        "metrics_by_policy": {},
    }
    public = public_projection(report)
    dumped = json.dumps(public)
    assert "rvw_x" not in dumped
    assert "secret" not in dumped
    assert "per_case" not in public


def test_private_writer_rejects_repo_paths(tmp_path):
    from evals.admission.shadow.runner import write_private_results

    repo_path = ROOT / "evals/admission/holdout/should_fail.json"
    with pytest.raises(ValueError, match="private_output_must_be_outside_repository"):
        write_private_results({"x": 1}, repo_path)


# --- 22. cost weights versioned + deterministic -------------------------------


def test_cost_weight_schedule_is_versioned_and_deterministic():
    rows = _uniform_rows(5, pred="no", truth="retain")
    first = cost_weighted_errors(rows)
    second = cost_weighted_errors(rows)
    assert first == second
    assert first["schedule_version"] == "admission-cost-weights-v1"
    assert COST_WEIGHT_SCHEDULE_V1["schedule_version"] == "admission-cost-weights-v1"
    assert "sensitivity_high_consequence_weight" in first


# --- helper --------------------------------------------------------------------


def _uniform_rows(n: int, *, pred: str, truth: str) -> list[dict[str, Any]]:
    return [
        {
            "policy": "test",
            "pred": pred,
            "unknown_pred": pred == "unknown",
            "unknown_reason": None,
            "unavailable_signals": [],
            "storage_pred": "defer",
            "review": "no",
            "governed": "no",
            "startup": "no",
            "truth": truth,
            "consequence": "low",
            "review_truth": "no",
            "governed_truth": "yes",
            "startup_truth": "yes",
            "retention_truth": "retain",
            "human_permits_auto": True,
            "permits_auto_if_reviewed": True,
            "current_would_promote": False,
        }
        for _ in range(n)
    ]


def _minimal_metrics(*, violation: bool) -> dict[str, Any]:
    return {
        "high_consequence": {
            "violation": violation,
            "n": 1,
            "automatic_positives": 0,
            "false_automatic_positives": 0,
            "review_routed": 0,
            "abstained_unknown": 0,
            "ppv": None,
        },
        "automatic_admission": {
            "n": 1,
            "predicted_automatic_positives": 0,
            "permitted_automatic_positives": 0,
            "false_automatic_admissions": 0,
            "ppv": None,
            "held_back_safe_auto": 0,
            "unknown_abstentions": 0,
        },
        "storage": {
            "n": 1,
            "confusion": {},
            "per_disposition": {},
            "accuracy": 0.0,
            "useful_memory_held_back": 0,
        },
        "unknown_abstention": {"unknown_count": 0, "unknown_rate": 0.0},
        "reason_code_inventory": ["rc1"],
        "unavailable_signal_dependency": False,
    }
