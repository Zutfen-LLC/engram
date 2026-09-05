"""Prove schema, privacy, reproducibility, and policy isolation contracts."""

from __future__ import annotations

import ast
import copy
import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from evals.admission.dataset import Dataset, data_digest, select_samples
from evals.admission.policy import PolicyInput, evaluate
from evals.admission.reference import reference
from evals.admission.report import report, result_artifact
from evals.admission.schema import LabelRecord, Sampling, digest

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "evals/admission/contract-v1.json"


@pytest.fixture
def dataset():
    return Dataset.model_validate_json(PATH.read_bytes())


def test_fixture_and_schema_are_reproducible(dataset):
    import jsonschema

    for model in (Dataset, LabelRecord, type(dataset.manifest)):
        schema = json.loads((ROOT / f"evals/schema/{model.__name__}-v1.json").read_text())
        assert schema == model.model_json_schema()
    jsonschema.validate(dataset.model_dump(mode="json"), Dataset.model_json_schema())
    assert report(dataset) == report(Dataset.model_validate_json(dataset.model_dump_json()))
    assert digest(dataset.manifest.model_dump(mode="json")) == digest(
        dict(reversed(list(dataset.manifest.model_dump(mode="json").items())))
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("retention_value", "verified_correct"),
        ("epistemic_state", "retain"),
        ("factual_outcome", "adequately_supported"),
        ("consequence", "fact"),
        ("evidence_independence", "different_principals"),
        ("atomic", "maybe"),
    ],
)
def test_enum_dimensions_fail_closed(dataset, field, value):
    label = dataset.samples[0].label.model_dump(mode="json")
    label["reviewer_a"]["dimensions"][field] = value
    with pytest.raises(ValidationError):
        LabelRecord.model_validate(label)


def test_required_fields_and_unknown(dataset):
    label = dataset.samples[0].label.model_dump(mode="json")
    del label["fixture_role"]
    with pytest.raises(ValidationError):
        LabelRecord.model_validate(label)
    label = dataset.samples[-4].label
    assert label.reviewer_a.dimensions.epistemic_state == "unknown"


@pytest.mark.parametrize("change", ["membership", "content", "label", "schema", "sampling"])
def test_material_changes_change_digest_or_fail(dataset, change):
    raw = dataset.model_dump(mode="json")
    original = digest(raw)
    if change == "membership":
        raw["samples"].pop()
    elif change == "content":
        raw["samples"][0]["content"] = "Changed proposition."
    elif change == "label":
        raw["samples"][0]["label"]["reviewer_a"]["dimensions"]["retention_value"] = "uncertain"
    elif change == "schema":
        raw["manifest"]["label_schema_version"] = "engram-admission-label-v2"
    else:
        raw["manifest"]["sampling"]["selection_seed"] = "changed"
    assert digest(raw) != original
    if change != "sampling":
        with pytest.raises(ValidationError):
            Dataset.model_validate(raw)


def test_version_mismatch_even_with_rehashed_data(dataset):
    sample = dataset.samples[0]
    changed = sample.model_copy(
        update={"label": sample.label.model_copy(update={"dataset_version": "2"})}
    )
    samples = (changed, *dataset.samples[1:])
    raw = dataset.model_dump(mode="json")
    raw["samples"] = [s.model_dump(mode="json") for s in samples]
    raw["manifest"]["data_digest"] = data_digest(samples, dataset.config, dataset.evaluation_at)
    with pytest.raises(ValidationError, match="dataset_version_mismatch"):
        Dataset.model_validate(raw)


def test_dual_review_and_disagreement(dataset):
    raw = dataset.samples[6].label.model_dump(mode="json")
    raw["label_origin"] = "human_adjudicated"
    with pytest.raises(ValidationError, match="dual_review_required"):
        LabelRecord.model_validate(raw)
    raw["reviewer_b"] = copy.deepcopy(raw["reviewer_a"])
    raw["reviewer_b"]["adjudicator_ref"] = "reviewer-b"
    raw["reviewer_b"]["dimensions"]["retention_value"] = "uncertain"
    raw["disagreement"] = "unresolved"
    assert LabelRecord.model_validate(raw).final_dimensions() is None
    raw["disagreement"] = "resolved"
    with pytest.raises(ValidationError, match="resolution_required"):
        LabelRecord.model_validate(raw)
    raw["resolution"] = copy.deepcopy(raw["reviewer_a"])
    raw["resolution"]["adjudicator_ref"] = "resolver"
    assert LabelRecord.model_validate(raw).final_dimensions().retention_value == "retain"


def test_labels_cannot_mutate_or_enter_policy(dataset):
    sample = dataset.samples[0]
    before = sample.model_dump_json()
    first = evaluate(sample.policy_input, dataset.config, dataset.evaluation_at)
    with pytest.raises(ValidationError):
        sample.label.reviewer_a.dimensions.retention_value = "do_not_retain"
    with pytest.raises(ValidationError):
        PolicyInput.model_validate(sample.label.model_dump())
    changed = sample.label.model_copy(update={"fixture_role": "incorrect_claim"})
    assert changed != sample.label
    assert evaluate(sample.policy_input, dataset.config, dataset.evaluation_at) == first
    assert sample.model_dump_json() == before


def test_production_has_no_evaluation_imports():
    for path in (ROOT / "engram").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("evals")
            elif isinstance(node, ast.Import):
                assert all(not alias.name.startswith("evals") for alias in node.names)


def test_legacy_golden_is_not_admission_truth():
    for name in ("classification_v1.json", "corpus_v2.json", "recall_v1.json", "recall_v2.json"):
        with pytest.raises(ValidationError):
            Dataset.model_validate_json((ROOT / "evals/golden" / name).read_bytes())


def test_fixed_time_and_canonical_lanes(dataset):
    sample = next(s for s in dataset.samples if s.sample_id == "cooling")
    before = evaluate(sample.policy_input, dataset.config, dataset.evaluation_at)
    assert not before.would_promote
    assert before.readiness_state == "cooling"
    after = evaluate(sample.policy_input, dataset.config, dataset.evaluation_at + timedelta(days=4))
    assert after.would_promote
    assert after.current_policy_version == "promotion-evidence-v1"
    legacy = dataset.samples[0].policy_input.model_copy(update={"receipt": None})
    assert evaluate(legacy, dataset.config, dataset.evaluation_at).current_policy_version == (
        "promotion-legacy-v1"
    )
    assert evaluate(legacy, None, dataset.evaluation_at).would_promote is None
    disabled = dataset.config.model_copy(update={"auto_promote_enabled": False})
    assert not evaluate(legacy, disabled, dataset.evaluation_at).would_promote


def test_stratification_is_order_independent(dataset):
    sampling = Sampling(
        selection_method="stratified_hash",
        selection_seed="test",
        strata=("kind", "evidence_state"),
        per_stratum=1,
    )
    selected = select_samples(dataset.samples, dataset.config, dataset.evaluation_at, sampling)
    assert selected == select_samples(
        tuple(reversed(dataset.samples)), dataset.config, dataset.evaluation_at, sampling
    )
    assert len(selected[0]) < len(dataset.samples)
    private = tuple(s.model_copy(update={"label": None}) for s in dataset.samples)
    risk = sampling.model_copy(update={"strata": ("labeled_consequence",)})
    with pytest.raises(ValueError, match="consequence_label_required"):
        select_samples(private, dataset.config, dataset.evaluation_at, risk)


def test_private_report_and_invalid_cli_do_not_expose_content(dataset, tmp_path):
    assert all(s.content not in json.dumps(report(dataset)) for s in dataset.samples)
    sentinel = "PRIVATE_MEMORY_SENTINEL_123"
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps({"private_memory": sentinel}))
    result = subprocess.run(
        [sys.executable, "-m", "evals.admission", str(path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert sentinel not in result.stdout + result.stderr
    assert result.stderr == ""


def test_secret_rejected_even_after_rehash(dataset):
    raw = dataset.model_dump(mode="json")
    # This synthetic pattern is generated only in the test process.
    secret = "ghp_" + "X" * 36
    raw["samples"][0]["content"] = secret
    raw["samples"][0]["policy_input"]["content_hash"] = digest(secret)
    raw["samples"][0]["label"]["content_hash"] = digest(secret)
    raw["manifest"]["sample_content_hashes"][0] = digest(secret)
    from evals.admission.dataset import Sample

    samples = tuple(Sample.model_validate(s) for s in raw["samples"])
    raw["manifest"]["data_digest"] = data_digest(samples, dataset.config, dataset.evaluation_at)
    with pytest.raises(ValidationError, match="public_secret_rejected"):
        Dataset.model_validate(raw)


def test_committed_reports_and_reference_match(dataset):
    directory = ROOT / "evals/admission"
    assert json.loads((directory / "contract-baseline-v1.json").read_text()) == report(dataset)
    assert json.loads((directory / "contract-results-v1.json").read_text()) == result_artifact(
        dataset
    )
    assert (ROOT / "evals/labeling/field-reference-v1.md").read_text() == reference(dataset)
