"""Tests for the read-only recall evaluation report."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from engram.semantic_context_manifest import semantic_query_digest
from evals.admission.schema import digest
from evals.recall.contracts import usefulness_perturbation_report
from evals.recall.metrics import build_packet_change_metrics, build_profile_metrics
from evals.recall.runner import (
    _admission_strata,
    _case_strata,
    _grouped_exposure_concentration,
    _public_read_only_proof,
    _public_report,
    build_markdown_report,
)
from evals.recall.runtime_config import runtime_config_digest, runtime_config_identity
from evals.recall.schema import (
    FrozenQueryEmbedding,
    RecallCaseStrata,
    RecallEvaluationManifest,
    query_embedding_vector_digest,
)


def _manifest_payload() -> dict[str, object]:
    query = "private query sentinel"
    return {
        "schema_version": "engram-recall-evaluation-input-v2",
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
                "strata": {"corpus_scale": "typical", "query_class": "lookup"},
            }
        ],
    }


def test_manifest_digest_binds_all_private_material_input() -> None:
    payload = _manifest_payload()
    base = RecallEvaluationManifest.model_validate(payload)
    frozen = {
        **payload["cases"][0],
        "query_embedding": {
            "values": [1.0, 0.5],
            "vector_digest": query_embedding_vector_digest([1.0, 0.5]),
        },
    }
    frozen_payload = {**payload, "cases": [frozen]}
    frozen_base = RecallEvaluationManifest.model_validate(frozen_payload)
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
        ("strata", {"corpus_scale": "dense", "query_class": "lookup"}),
    ):
        changed_case = {**payload["cases"][0], field: value}
        if field == "query":
            changed_case["query_digest"] = semantic_query_digest(value)
        changed = {**payload, "cases": [changed_case]}
        assert RecallEvaluationManifest.model_validate(changed).input_digest != base.input_digest

    # The frozen query vector is private material input: binding it, or
    # changing any component of it, changes the input digest.
    assert frozen_base.input_digest != base.input_digest
    changed_vector = {
        **frozen,
        "query_embedding": {
            "values": [1.0, 0.25],
            "vector_digest": query_embedding_vector_digest([1.0, 0.25]),
        },
    }
    assert (
        RecallEvaluationManifest.model_validate(
            {**payload, "cases": [changed_vector]}
        ).input_digest
        != frozen_base.input_digest
    )
    # The digest alone is never the vector: an unrelated vector with the
    # original digest fails validation.
    with pytest.raises(ValidationError, match="frozen_query_embedding_digest_mismatch"):
        FrozenQueryEmbedding.model_validate(
            {"values": [0.9], "vector_digest": query_embedding_vector_digest([1.0, 0.5])}
        )
    embedding = FrozenQueryEmbedding.model_validate(frozen["query_embedding"])
    assert embedding.protected_identity() == {
        "dimension": 2,
        "vector_digest": query_embedding_vector_digest([1.0, 0.5]),
    }


def test_case_strata_is_a_closed_public_safe_contract() -> None:
    """Arbitrary manifest tokens can never become strata content."""
    payload = _manifest_payload()
    with pytest.raises(ValidationError):
        RecallEvaluationManifest.model_validate(
            {
                **payload,
                "cases": [{**payload["cases"][0], "strata": {"secret_dimension": "leak"}}],
            }
        )
    with pytest.raises(ValidationError):
        RecallEvaluationManifest.model_validate(
            {
                **payload,
                "cases": [
                    {**payload["cases"][0], "strata": {"corpus_scale": "arbitrary-value"}}
                ],
            }
        )
    assert RecallCaseStrata().public_counts_identity() == {}
    assert RecallCaseStrata(corpus_scale="dense").public_counts_identity() == {
        "corpus_scale": "dense"
    }
    rows = [
        {"strata": {"corpus_scale": "dense", "query_class": "lookup"}},
        {"strata": {"corpus_scale": "dense", "query_class": None}},
        {"strata": {"corpus_scale": None, "query_class": None}},
    ]
    assert _case_strata(rows) == {
        "corpus_scale": {"dense": 2},
        "query_class": {"lookup": 1},
    }


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


def test_packing_omission_metrics_consume_the_exact_production_contract() -> None:
    """Packet-change aggregates read the frozen ``packing["omitted"]`` block."""
    legacy = {
        "items": [{"id": "a"}],
        "item_count": 1,
        "byte_count": 10,
        "omitted_by_admission": {},
    }
    candidate = {
        "items": [{"id": "a"}],
        "item_count": 1,
        "byte_count": 10,
        "omitted_by_admission": {},
        "packing": {
            "version": "recall-packing-v1",
            "selected_count": 1,
            "conflict_pairs_preserved": 0,
            "omitted": {"budget": 2, "redundant_known_root": 1},
            # A stray non-production key must never be consumed as an alias.
            "omitted_by_reason": {"budget": 99},
        },
    }

    change = build_packet_change_metrics(legacy, candidate)
    aggregated = build_profile_metrics(
        [{"legacy": legacy, "candidate": candidate, "labels": {}}]
    )

    assert change["packing_omission_reasons"] == {"budget": 2, "redundant_known_root": 1}
    assert (
        aggregated["packet_change"]["packing_omission_reasons"]
        == {"budget": 2, "redundant_known_root": 1}
    )


def test_admission_strata_counts_each_resolved_item_exactly_once() -> None:
    """The bulk resolution summary is the single V2 denominator."""
    admitted_item = {"id": "i1"}
    packet = {
        "profile": "governed",
        "items": [admitted_item, {"id": "i2"}],
        "admission_diagnostics": [
            # Both withheld diagnostics carry resolution status, but they
            # are already inside the bulk summary counts below. Counting
            # them again into v2_resolution_state would double count.
            {"decision": "withhold", "reason_codes": ["v2_surface_review_required"],
             "v2_resolution_status": "current"},
            {"decision": "withhold", "reason_codes": ["admission_assessment_stale"],
             "v2_resolution_status": "missing"},
        ],
        "v2_resolution": {
            "profile_key": "risk_aware_shadow_v1",
            "resolved_count": 4,
            "resolution_status_counts": {"current": 2, "missing": 1, "stale": 1},
            "query_count": 4,
        },
    }

    strata = _admission_strata([packet])

    assert strata["v2_resolution_state"] == {"current": 2, "missing": 1, "stale": 1}
    assert strata["v2_admission_outcome"] == {"admitted": 2, "withhold": 2}
    assert strata["candidate_eligibility"] == {"eligible": 2, "withheld": 2}
    assert strata["primary_admission_blocker"] == {
        "admission_assessment_stale": 1,
        "v2_surface_review_required": 1,
    }
    # Mechanical exactly-once property on this no-truncation fixture: every
    # resolved item is either admitted in the packet or itemized as exactly
    # one withheld diagnostic, and the authoritative resolution total equals
    # that sum — the diagnostic statuses did not add a second contribution.
    admitted = len(packet["items"])
    withheld = len(packet["admission_diagnostics"])
    assert sum(strata["v2_resolution_state"].values()) == admitted + withheld
    assert strata["v2_resolution_state"]["current"] == 2  # not 3 (diagnostic) + 2 (bulk)
    assert strata["v2_resolution_state"]["missing"] == 1  # not 2 (diagnostic) + 1 (bulk)


def _full_proof() -> dict[str, Any]:
    return {
        "transaction": "REPEATABLE READ READ ONLY",
        "snapshot_digest_verified": "c" * 64,
        "before_after_equal": True,
        "tracked_tables": ["memory_items"],
        "tracked_counters": {"memory_items": 3},
        "total_db_statement_count": 120,
        "case_db_statement_counts": {
            "private-case-1": {
                "primary_comparison": 40,
                "neutral_usefulness_counterfactual": 35,
                "total": 75,
            },
            "private-case-2": {
                "primary_comparison": 40,
                "neutral_usefulness_counterfactual": 0,
                "total": 40,
            },
        },
        "metadata_query_count": 2,
        "fixed_setup_statement_count": 2,
        "fixed_finalization_statement_count": 1,
        "per_case_sums_reconcile_to_total": True,
        "query_embedding_sources": {"frozen_manifest": 1, "provider_capture": 1},
        "runtime_config_digest": "d" * 64,
        "provider_calls": {
            "semantic_query_embedding": 1,
            "classification": 0,
            "assessment": 0,
            "other_model": 0,
        },
        "provider_call_expectations": {
            "semantic_query_embedding": 1,
            "classification": 0,
            "assessment": 0,
            "other_model": 0,
        },
        "provider_call_expectations_met": True,
        "candidate_receipts_persisted": 0,
    }


def test_public_read_only_proof_is_aggregate_only() -> None:
    public = _public_read_only_proof(_full_proof())

    artifact = json.dumps(public, sort_keys=True)
    assert "private-case-1" not in artifact
    assert "private-case-2" not in artifact
    accounting = public["query_accounting"]
    assert accounting["case_count"] == 2
    assert accounting["per_case_primary_comparison"] == {
        "count": 2,
        "min": 40,
        "max": 40,
        "mean": 40.0,
    }
    assert accounting["per_case_neutral_usefulness_counterfactual"] == {
        "count": 2,
        "min": 0,
        "max": 35,
        "mean": 17.5,
    }
    assert accounting["per_case_total"] == {"count": 2, "min": 40, "max": 75, "mean": 57.5}
    assert accounting["per_case_sums_reconcile_to_total"] is True
    assert public["deterministic_replay"] == {
        "frozen_embedding_case_count": 1,
        "provider_capture_case_count": 1,
    }
    assert public["query_embedding_sources"] == {
        "frozen_manifest": 1,
        "provider_capture": 1,
    }


def test_runtime_config_identity_binds_every_material_setting() -> None:
    """Changing each outcome-affecting setting invalidates the identity."""
    from engram.config import settings

    baseline = runtime_config_digest()
    identity = runtime_config_identity()
    drift_fields = (
        "recall_byte_budget",
        "recall_item_budget",
        "relationship_expansion_enabled",
        "recall_semantic_expansion_seed_limit",
        "recall_candidate_ceiling",
        "max_graph_neighbors_per_item",
        "max_graph_expanded_items",
        "max_tunnel_neighbors_per_item",
        "max_tunnel_additions",
        "relationship_score_weight_semantic",
        "relationship_score_weight_relationship",
        "relationship_score_weight_tunnel",
        "relationship_score_weight_importance",
        "assessment_selection_enabled",
        "assessment_effective_contract_hash",
        "assessment_policy_version",
        "embedding_provider",
    )
    for field in drift_fields:
        original = getattr(settings, field)
        replacement: Any
        if isinstance(original, bool):
            replacement = not original
        elif isinstance(original, int):
            replacement = original + 1
        elif isinstance(original, float):
            replacement = original + 0.01
        else:
            replacement = "drifted-contract-hash"
        setattr(settings, field, replacement)
        try:
            assert runtime_config_digest() != baseline, field
            # A drifted string value is materially present in the closed
            # projection (honest binding, not an opaque "changed" flag).
            if isinstance(original, str):
                assert "drifted-contract-hash" in json.dumps(runtime_config_identity())
        finally:
            setattr(settings, field, original)
    assert runtime_config_digest() == baseline

    # The projection is closed: it contains no secrets and no unconstrained
    # settings dump.
    flattened_keys = {
        key
        for group in identity.values()
        if isinstance(group, dict)
        for key in group
    }
    assert flattened_keys == {
        "recall_byte_budget",
        "recall_item_budget",
        "relationship_expansion_enabled",
        "recall_semantic_expansion_seed_limit",
        "recall_candidate_ceiling",
        "max_graph_neighbors_per_item",
        "max_graph_expanded_items",
        "max_tunnel_neighbors_per_item",
        "max_tunnel_additions",
        "relationship_score_weight_semantic",
        "relationship_score_weight_relationship",
        "relationship_score_weight_tunnel",
        "relationship_score_weight_importance",
        "assessment_selection_enabled",
        "assessment_effective_contract_hash",
        "assessment_policy_version",
        "embedding_provider",
        "openai_base_url_digest",
    }
    assert "openai_api_key" not in json.dumps(identity)
    # The base URL is bound by digest only, never by value.
    original_url = settings.openai_base_url
    settings.openai_base_url = "https://internal.example.invalid/v1"
    try:
        projection = json.dumps(runtime_config_identity(), sort_keys=True)
        assert "internal.example.invalid" not in projection
        assert runtime_config_digest() != baseline
    finally:
        settings.openai_base_url = original_url


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


@pytest.mark.parametrize(
    ("packets", "expected_count", "expected_hhi"),
    [
        ([], 0, None),
        ([{"items": [{"kind": "fact", "evaluation_strata": {"source_type": "manual"}}]}], 1, 1.0),
        (
            [
                {
                    "items": [
                        {"kind": "fact", "evaluation_strata": {"source_type": "manual"}},
                        {"kind": "fact", "evaluation_strata": {"source_type": "manual"}},
                        {"kind": "decision", "evaluation_strata": {"source_type": "import"}},
                    ]
                }
            ],
            3,
            5 / 9,
        ),
    ],
)
def test_grouped_exposure_concentration_has_deterministic_boundaries(
    packets: list[dict[str, Any]], expected_count: int, expected_hhi: float | None
) -> None:
    report = _grouped_exposure_concentration(packets, field="source_type")

    assert report["concentration"]["exposure_count"] == expected_count
    assert report["concentration"]["hhi"] == expected_hhi


@pytest.mark.asyncio
async def test_provider_observer_detects_an_actual_prohibited_gateway(monkeypatch) -> None:
    """A call to the shared assessment gateway cannot be reported as zero."""
    from engram import assessment_provider
    from engram.provider_observer import observe_provider_calls

    class RefusingClient:
        def __init__(self, **_kwargs: object) -> None:
            raise RuntimeError("test provider refused")

    monkeypatch.setattr(assessment_provider, "AsyncOpenAI", RefusingClient)
    with (
        observe_provider_calls() as calls,
        pytest.raises(RuntimeError, match="test provider refused"),
    ):
        await assessment_provider.assess_content("safe test input", "fact")
    assert calls["assessment"] == 1


def test_public_artifacts_exclude_all_protected_sentinels() -> None:
    """Build the final JSON and Markdown artifacts, not a packet helper."""
    manifest_payload = _manifest_payload()
    protected = {
        "case_id": "CASE-SENTINEL-198",
        "query": "QUERY-SENTINEL-198",
        "tenant": "00000000-0000-0000-0000-000000000198",
        "principal": "00000000-0000-0000-0000-000000000199",
        "workspace": "WORKSPACE-SENTINEL-198",
        "content": "MEMORY-CONTENT-SENTINEL-198",
        "label": "REVIEWER-SENTINEL-198",
        "strata_key": "STRATA-KEY-SENTINEL-198",
        "strata_value": "STRATA-VALUE-SENTINEL-198",
        "embedding": "0.70710678118654752440084436210485",
    }
    manifest_payload["memory_context"] = {
        **manifest_payload["memory_context"],
        "tenant_id": protected["tenant"],
        "principal_id": protected["principal"],
    }
    manifest_payload["cases"] = [
        {
            **manifest_payload["cases"][0],
            "case_id": protected["case_id"],
            "query": protected["query"],
            "query_digest": semantic_query_digest(protected["query"]),
            "workspace": protected["workspace"],
            "labels": {
                "case_id": protected["case_id"],
                "snapshot_digest": "c" * 64,
                "contamination": {protected["label"]: "unknown"},
                "usefulness": {},
                "label_set_digest": digest(
                    {"contamination": {protected["label"]: "unknown"}, "usefulness": {}}
                ),
            },
        }
    ]
    manifest = RecallEvaluationManifest.model_validate(manifest_payload)
    packet = {
        "profile": "legacy",
        "items": [
            {
                "id": protected["label"],
                "content": protected["content"],
                "kind": "fact",
                "evaluation_strata": {"source_type": "manual"},
            }
        ],
        "item_count": 1,
        "byte_count": 1,
        "token_count": 1,
        "effective_item_budget": 2,
        "effective_byte_budget": 2,
        "effective_token_budget": 2,
        "omitted_by_admission": {},
    }
    candidate = {
        **packet,
        "profile": "governed",
        "admission_diagnostics": [
            {"item_id": protected["label"], "decision": "withhold", "reason_codes": ["x"]}
        ],
        "v2_resolution": {"resolution_status_counts": {"current": 1}},
    }
    case_row = {
        "case_id": manifest.cases[0].case_id,
        "strata": manifest.cases[0].strata.model_dump(mode="json"),
        "labels": manifest.cases[0].labels.model_dump(mode="json"),
        "query_embedding_identity": {
            "dimension": 2,
            "vector_digest": query_embedding_vector_digest(
                [float(protected["embedding"]), 0.5]
            ),
        },
        "legacy": packet,
        "candidates": [candidate, {**candidate, "profile": "exploratory"}],
    }
    public = _public_report(
        manifest,
        [case_row],
        mutation_proof=_public_read_only_proof(_full_proof()),
    )
    artifact = json.dumps(public, sort_keys=True) + build_markdown_report(public)
    for sentinel in protected.values():
        assert sentinel not in artifact, sentinel
    # Case identity and per-case statement counts are private-only.
    assert "case_db_statement_counts" not in json.dumps(public)
    assert "CASE-SENTINEL" not in artifact
