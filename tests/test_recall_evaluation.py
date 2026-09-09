"""Tests for the read-only recall evaluation report."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from engram.semantic_context_manifest import semantic_query_digest
from evals.admission.schema import digest
from evals.recall.contracts import usefulness_perturbation_report
from evals.recall.metrics import build_packet_change_metrics, build_profile_metrics
from evals.recall.schema import RecallEvaluationManifest


def _manifest_payload() -> dict[str, object]:
    query = "private query sentinel"
    return {
        "schema_version": "engram-recall-evaluation-input-v1",
        "baseline_sha": "a" * 40,
        "repository_sha": "b" * 40,
        "snapshot_digest": "c" * 64,
        "snapshot_at": datetime(2026, 9, 8, tzinfo=UTC),
        "evaluation_at": datetime(2026, 9, 8, tzinfo=UTC),
        "tenant_config_version": "config-v1",
        "embedding_profile_key": "openai:text-embedding-3-small:1536",
        "memory_context": {
            "version": "memory-context-v2",
            "tenant_id": str(UUID(int=1)),
            "principal_id": str(UUID(int=2)),
        },
        "cases": [
            {
                "case_id": "case-1",
                "query": query,
                "query_digest": semantic_query_digest(query),
                "workspace": "private-workspace",
                "byte_budget": 10,
                "token_budget": 11,
                "item_budget": 12,
                "strata": {"kind": "fact"},
            }
        ],
    }


def test_manifest_digest_binds_all_private_material_input() -> None:
    payload = _manifest_payload()
    base = RecallEvaluationManifest.model_validate(payload)
    mutations = (
        ("memory_context", {**payload["memory_context"], "principal_id": str(UUID(int=3))}),
        ("evaluation_at", datetime(2026, 9, 9, tzinfo=UTC)),
        ("tenant_config_version", "config-v2"),
        ("embedding_profile_key", "other-profile"),
    )
    for field, value in mutations:
        changed = {**payload, field: value}
        assert RecallEvaluationManifest.model_validate(changed).input_digest != base.input_digest

    for field, value in (
        ("workspace", "other-workspace"),
        ("byte_budget", 20),
        ("token_budget", 21),
        ("item_budget", 22),
        ("case_id", "case-2"),
        ("query", "another private query"),
        ("strata", {"kind": "decision"}),
    ):
        changed_case = {**payload["cases"][0], field: value}
        if field == "query":
            changed_case["query_digest"] = semantic_query_digest(value)
        changed = {**payload, "cases": [changed_case]}
        assert RecallEvaluationManifest.model_validate(changed).input_digest != base.input_digest


def test_manifest_rejects_terminal_recommendation_and_unbound_labels() -> None:
    payload = _manifest_payload()
    with pytest.raises(ValidationError):
        RecallEvaluationManifest.model_validate(
            {**payload, "terminal_recommendation": "READY_FOR_162_RECALL_CERTIFICATION"}
        )

    labels = {"contamination": {"item-1": "contaminated"}, "usefulness": {}}
    with pytest.raises(ValidationError, match="label_case_id_mismatch"):
        RecallEvaluationManifest.model_validate(
            {**payload, "cases": [{**payload["cases"][0], "labels": labels}]}
        )
    labels.update(
        {
            "case_id": "case-1",
            "snapshot_digest": "c" * 64,
            "label_set_digest": digest(
                {"contamination": {"item-1": "contaminated"}, "usefulness": {}}
            ),
        }
    )
    assert RecallEvaluationManifest.model_validate(
        {**payload, "cases": [{**payload["cases"][0], "labels": labels}]}
    ).input_digest


def test_metrics_are_deterministic_and_keep_unknown_labels_unknown() -> None:
    legacy = {
        "items": [{"id": "a"}, {"id": "b"}],
        "item_count": 2,
        "byte_count": 20,
        "effective_byte_budget": 100,
        "effective_token_budget": 50,
        "effective_item_budget": 3,
        "omitted_by_admission": {},
    }
    governed = {
        "items": [{"id": "b"}, {"id": "c"}],
        "item_count": 2,
        "byte_count": 18,
        "effective_byte_budget": 100,
        "effective_token_budget": 50,
        "effective_item_budget": 3,
        "omitted_by_admission": {"missing_assessment": 1},
    }

    change = build_packet_change_metrics(legacy, governed)
    profile = build_profile_metrics(
        [
            {
                "legacy": legacy,
                "candidate": governed,
                "labels": {
                    "contamination": {"a": "contaminated", "c": "unknown"},
                    "usefulness": {"a": "useful", "c": "unknown"},
                },
            }
        ]
    )

    assert change == {
        "packet_count": 1,
        "identical_packet_rate": 0.0,
        "membership_change_rate": 1.0,
        "ordering_only_change_rate": 0.0,
        "mean_jaccard": 1 / 3,
        "added_items": 1,
        "removed_items": 1,
        "item_count_delta": 0,
        "byte_count_delta": -2,
        "token_count_delta": 0,
        "omission_reasons": {"missing_assessment": 1},
        "packing_omission_reasons": {},
    }
    assert profile["contamination"] == {
        "avoided": 1,
        "introduced": 0,
        "retained": 0,
        "acceptable": 0,
        "unknown": 1,
        "known_denominator": 1,
        "avoided_rate": 1.0,
        "introduced_rate": 0.0,
    }
    assert profile["usefulness"] == {
        "legacy_useful_retained": 0,
        "legacy_useful_withheld": 1,
        "candidate_only_useful": 0,
        "unknown": 1,
    }


def test_usefulness_perturbation_uses_the_shared_non_amplifying_contract() -> None:
    report = usefulness_perturbation_report()

    assert report["adjustments"] == {
        "none": 0.0,
        "positive": 0.10,
        "negative": -0.10,
        "mixed": 0.0,
    }
    assert report["one_and_many_same_sign_are_equal"] is True
    assert report["repeated_exposure_changes_utility"] is False


def test_labels_are_neutral_and_profile_effects_are_derived_from_membership() -> None:
    common = {
        "byte_count": 0,
        "item_count": 1,
        "token_count": 0,
        "omitted_by_admission": {},
    }
    legacy = {**common, "item_count": 2, "items": [{"id": "contaminated"}, {"id": "acceptable"}]}
    governed = {**common, "items": [{"id": "acceptable"}]}
    exploratory = {**common, "items": [{"id": "contaminated"}]}
    labels = {
        "contamination": {"contaminated": "contaminated", "acceptable": "acceptable"},
        "usefulness": {"acceptable": "useful", "contaminated": "unknown"},
    }
    governed_metrics = build_profile_metrics(
        [{"legacy": legacy, "candidate": governed, "labels": labels}]
    )
    exploratory_metrics = build_profile_metrics(
        [{"legacy": legacy, "candidate": exploratory, "labels": labels}]
    )

    assert governed_metrics["contamination"]["avoided"] == 1
    assert governed_metrics["usefulness"]["legacy_useful_retained"] == 1
    assert exploratory_metrics["contamination"]["retained"] == 1
    assert exploratory_metrics["usefulness"]["legacy_useful_withheld"] == 1
