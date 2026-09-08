"""Pure contract tests for semantic-context-manifest-v1."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal, get_args

import pytest
from pydantic import ValidationError

from engram.context_manifest import compute_manifest_hash, sha256_digest
from engram.recall import _enforce_semantic_budget
from engram.recall_signals import V2_ADMISSION_PROFILE_KEY
from engram.semantic_budget import semantic_item_token_cost
from engram.semantic_context_manifest import (
    SEMANTIC_MANIFEST_CONTRACT_VERSION,
    SemanticConflictResolutionStatusV1,
    SemanticConflictTypeV1,
    SemanticContextManifestV1,
    SemanticManifestDecisionContextV1,
    SemanticV2ProfileKeyV1,
    build_semantic_context_manifest_v1,
    semantic_query_digest,
)

TENANT = "00000000-0000-0000-0000-000000000001"
PRINCIPAL = "00000000-0000-0000-0000-000000000002"
ITEM_A = "00000000-0000-0000-0000-000000000010"
ITEM_B = "00000000-0000-0000-0000-000000000011"
ROOT = Path(__file__).resolve().parent.parent
SEMANTIC_VECTORS = ROOT / "conformance" / "semantic-context-manifest-v1" / "vectors"


def _context(
    *,
    profile: Literal["legacy", "governed", "exploratory"] = "legacy",
    token_budget: int | None = None,
) -> SemanticManifestDecisionContextV1:
    return SemanticManifestDecisionContextV1(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        workspace_id=None,
        memory_profile_id=None,
        memory_profile_revision_id=None,
        memory_profile_version=None,
        workspace_supplied=False,
        requested_byte_budget=None,
        requested_token_budget=None,
        requested_item_budget=None,
        effective_byte_budget=1000,
        effective_token_budget=token_budget,
        effective_item_budget=10,
        recall_profile=profile,
        recall_profile_contract_version="recall-profiles-v1",
        scoring_version="semantic-v3",
        signals_version=None,
        admission_policy=None,
        relationship_relevance_version=None,
        packing_version=None,
        config_version="v1",
    )


def _item(item_id: str, content: str) -> dict[str, object]:
    return {
        "id": item_id,
        "kind": "fact",
        "content": content,
        "review_status": "active",
        "authority": 10,
        "visibility": "private",
        "workspace_id": None,
        "pinned": False,
        "score": 0.8,
        "trust_score": 0.9,
        "reasons": ["semantic similarity 0.90"],
        "warnings": [],
        "conflict_type": None,
        "conflict_resolution_status": None,
    }


def _build(*, query: str = "find café", items: list[dict[str, object]] | None = None) -> Any:
    selected = [_item(ITEM_A, "alpha")] if items is None else items
    working_set = "\n".join(f"[{item['kind']}] {item['content']}" for item in selected)
    return build_semantic_context_manifest_v1(
        items=selected,
        working_set=working_set,
        item_count=len(selected),
        byte_count=sum(len(str(item["content"]).encode()) for item in selected),
        candidate_count=len(selected),
        omitted_count=0,
        omitted_by_admission={},
        expansion=None,
        packing=None,
        message=None,
        query=query,
        context=_context(),
    )


def _vector_manifest(name: str) -> dict[str, Any]:
    vector = json.loads((SEMANTIC_VECTORS / name).read_text())
    return vector["expected"]["manifest"]


def _set_fresh_and_evidence(manifest: dict[str, Any], field: str, value: Any) -> None:
    item = manifest["items"][0]
    item["admission"]["v2"]["fresh"][field] = value
    item["evidence"][field] = value


def _set_assessment_ref_field(manifest: dict[str, Any], field: str, value: str) -> None:
    item = manifest["items"][0]
    item["admission"]["v2"]["fresh"]["effective_assessment_refs"][0][field] = value
    item["evidence"]["effective_assessment_refs"][0][field] = value


def test_legacy_manifest_is_deterministic_and_does_not_store_query() -> None:
    first = _build()
    second = _build()
    payload = first.model_dump(mode="json", by_alias=True, exclude_none=False)

    assert compute_manifest_hash(first) == compute_manifest_hash(second)
    assert first.versions.manifest_contract_version == SEMANTIC_MANIFEST_CONTRACT_VERSION
    assert first.request.query_digest == semantic_query_digest("find café")
    assert "find café" not in str(payload)
    assert first.packet.hash == sha256_digest(b"[fact] alpha")
    assert first.items[0].served_content_hash == sha256_digest(b"alpha")


def test_query_and_item_order_change_identity() -> None:
    one = _build(query="one")
    two = _build(query="two")
    ordered = _build(items=[_item(ITEM_A, "alpha"), _item(ITEM_B, "beta")])
    reversed_ = _build(items=[_item(ITEM_B, "beta"), _item(ITEM_A, "alpha")])

    assert one.request.query_digest != two.request.query_digest
    assert compute_manifest_hash(one) != compute_manifest_hash(two)
    assert ordered.packet.hash != reversed_.packet.hash
    assert compute_manifest_hash(ordered) != compute_manifest_hash(reversed_)


def test_builder_rejects_finalized_packet_contradictions() -> None:
    with pytest.raises(ValueError, match="item_count"):
        build_semantic_context_manifest_v1(
            items=[_item(ITEM_A, "alpha")],
            working_set="[fact] alpha",
            item_count=0,
            byte_count=5,
            candidate_count=1,
            omitted_count=0,
            omitted_by_admission={},
            expansion=None,
            packing=None,
            message=None,
            query="q",
            context=_context(),
        )


def test_builder_rejects_incomplete_v2_decision_identity() -> None:
    candidate = _item(ITEM_A, "alpha")
    candidate.update(
        {
            "trust_score": None,
            "relevance_score": 0.9,
            "utility_score": 0.8,
            "admission": {},
            "evidence": {},
            "packing_reason": None,
        }
    )
    with pytest.raises(ValueError, match="admission"):
        build_semantic_context_manifest_v1(
            items=[candidate],
            working_set="[fact] alpha",
            item_count=1,
            byte_count=5,
            candidate_count=1,
            omitted_count=0,
            omitted_by_admission={},
            expansion=None,
            packing={
                "version": "recall-packing-v1",
                "selected_count": 1,
                "conflict_pairs_preserved": 0,
                "omitted": {},
            },
            message=None,
            query="q",
            context=_context(profile="governed"),
        )
    with pytest.raises(ValueError, match="working_set"):
        build_semantic_context_manifest_v1(
            items=[_item(ITEM_A, "alpha")],
            working_set="[fact] changed",
            item_count=1,
            byte_count=5,
            candidate_count=1,
            omitted_count=0,
            omitted_by_admission={},
            expansion=None,
            packing=None,
            message=None,
            query="q",
            context=_context(),
        )


def test_exact_four_byte_content_fits_one_token_in_serving_and_manifest() -> None:
    candidate = _item(ITEM_A, "abcd")
    selected = _enforce_semantic_budget(
        [candidate], byte_budget=None, token_budget=1, item_budget=None
    )

    assert selected == [candidate]
    assert semantic_item_token_cost("abcd") == 1
    manifest = build_semantic_context_manifest_v1(
        items=selected,
        working_set="[fact] abcd",
        item_count=1,
        byte_count=4,
        candidate_count=1,
        omitted_count=0,
        omitted_by_admission={},
        expansion=None,
        packing=None,
        message=None,
        query="q",
        context=_context(token_budget=1),
    )
    assert manifest.result.item_count == 1


def test_manifest_sums_the_same_per_item_token_cost_as_serving() -> None:
    candidates = [_item(ITEM_A, "abcde"), _item(ITEM_B, "abcdefg")]
    selected = _enforce_semantic_budget(
        candidates, byte_budget=None, token_budget=2, item_budget=None
    )
    assert selected == candidates
    assert sum(semantic_item_token_cost(str(item["content"])) for item in selected) == 2
    working_set = "\n".join(f"[{item['kind']}] {item['content']}" for item in selected)
    build_semantic_context_manifest_v1(
        items=selected,
        working_set=working_set,
        item_count=2,
        byte_count=12,
        candidate_count=2,
        omitted_count=0,
        omitted_by_admission={},
        expansion=None,
        packing=None,
        message=None,
        query="q",
        context=_context(token_budget=2),
    )


def test_skipped_oversized_item_and_later_selected_item_pass_manifest() -> None:
    oversized = _item(ITEM_A, "abcdefgh")
    smaller = _item(ITEM_B, "abcd")
    selected = _enforce_semantic_budget(
        [oversized, smaller], byte_budget=None, token_budget=1, item_budget=None
    )
    assert selected == [smaller]
    build_semantic_context_manifest_v1(
        items=selected,
        working_set="[fact] abcd",
        item_count=1,
        byte_count=4,
        candidate_count=2,
        omitted_count=1,
        omitted_by_admission={},
        expansion=None,
        packing=None,
        message=None,
        query="q",
        context=_context(token_budget=1),
    )


@pytest.mark.parametrize("budget,accepted", [(2, True), (1, False)])
def test_manifest_token_budget_exact_boundary_and_one_below(budget: int, accepted: bool) -> None:
    items = [_item(ITEM_A, "abcdefgh")]
    kwargs = dict(
        items=items,
        working_set="[fact] abcdefgh",
        item_count=1,
        byte_count=8,
        candidate_count=1,
        omitted_count=0,
        omitted_by_admission={},
        expansion=None,
        packing=None,
        message=None,
        query="q",
        context=_context(token_budget=budget),
    )
    if accepted:
        build_semantic_context_manifest_v1(**kwargs)
    else:
        with pytest.raises(ValueError, match="token budget"):
            build_semantic_context_manifest_v1(**kwargs)


@pytest.mark.parametrize("value", ["low", "medium", "high", "unknown", "not_applicable"])
def test_every_canonical_risk_state_parses(value: str) -> None:
    manifest = _vector_manifest("007-governed-v2.json")
    _set_fresh_and_evidence(manifest, "risk_state", value)
    assert SemanticContextManifestV1.model_validate(manifest).items[0].evidence is not None


def test_invalid_risk_state_fails() -> None:
    manifest = _vector_manifest("007-governed-v2.json")
    _set_fresh_and_evidence(manifest, "risk_state", "probably_safe")
    with pytest.raises(ValidationError):
        SemanticContextManifestV1.model_validate(manifest)


@pytest.mark.parametrize("value", ["retain", "transient", "noise", "uncertain", "unknown"])
def test_every_canonical_retention_state_parses(value: str) -> None:
    manifest = _vector_manifest("007-governed-v2.json")
    _set_fresh_and_evidence(manifest, "retention_state", value)
    assert SemanticContextManifestV1.model_validate(manifest).items[0].evidence is not None


def test_invalid_retention_state_fails() -> None:
    manifest = _vector_manifest("007-governed-v2.json")
    _set_fresh_and_evidence(manifest, "retention_state", "forever")
    with pytest.raises(ValidationError):
        SemanticContextManifestV1.model_validate(manifest)


@pytest.mark.parametrize("value", ["none", "semantic_exploratory", "semantic_governed", "startup"])
def test_every_canonical_admission_tier_parses(value: str) -> None:
    manifest = _vector_manifest("007-governed-v2.json")
    manifest["items"][0]["admission"]["v2"]["fresh"]["highest_admission_tier"] = value
    SemanticContextManifestV1.model_validate(manifest)


def test_invalid_admission_tier_fails() -> None:
    manifest = _vector_manifest("007-governed-v2.json")
    manifest["items"][0]["admission"]["v2"]["fresh"]["highest_admission_tier"] = "super_trusted"
    with pytest.raises(ValidationError):
        SemanticContextManifestV1.model_validate(manifest)


@pytest.mark.parametrize(
    "value",
    [
        "direct_statement",
        "tool_observation",
        "quoted_source",
        "derived_summary",
        "inference",
        "unknown",
    ],
)
def test_every_canonical_assertion_mode_parses(value: str) -> None:
    manifest = _vector_manifest("007-governed-v2.json")
    _set_assessment_ref_field(manifest, "assertion_mode", value)
    SemanticContextManifestV1.model_validate(manifest)


def test_invalid_assertion_mode_fails() -> None:
    manifest = _vector_manifest("007-governed-v2.json")
    _set_assessment_ref_field(manifest, "assertion_mode", "explicit")
    with pytest.raises(ValidationError):
        SemanticContextManifestV1.model_validate(manifest)


@pytest.mark.parametrize("value", ["user", "assistant", "system", "tool", "unknown"])
def test_every_canonical_origin_parses(value: str) -> None:
    manifest = _vector_manifest("007-governed-v2.json")
    _set_assessment_ref_field(manifest, "origin", value)
    SemanticContextManifestV1.model_validate(manifest)


def test_invalid_origin_fails() -> None:
    manifest = _vector_manifest("007-governed-v2.json")
    _set_assessment_ref_field(manifest, "origin", "external")
    with pytest.raises(ValidationError):
        SemanticContextManifestV1.model_validate(manifest)


@pytest.mark.parametrize(
    "value",
    [
        "wait_until",
        "classification_required",
        "human_review_required",
        "conflict_resolution_required",
        "new_evidence_required",
        "policy_reconciliation_required",
        "none",
    ],
)
def test_every_canonical_admission_next_action_parses(value: str) -> None:
    manifest = _vector_manifest("007-governed-v2.json")
    manifest["items"][0]["admission"]["v2"]["fresh"]["next_actions"] = [value]
    SemanticContextManifestV1.model_validate(manifest)


def test_invalid_admission_next_action_fails() -> None:
    manifest = _vector_manifest("007-governed-v2.json")
    manifest["items"][0]["admission"]["v2"]["fresh"]["next_actions"] = ["retry_later"]
    with pytest.raises(ValidationError):
        SemanticContextManifestV1.model_validate(manifest)


def test_nullable_policy_states_remain_distinct_from_unknown() -> None:
    null_manifest = _vector_manifest("007-governed-v2.json")
    for field in ("risk_state", "retention_state"):
        _set_fresh_and_evidence(null_manifest, field, None)
    null_manifest["items"][0]["admission"]["v2"]["fresh"]["highest_admission_tier"] = None
    parsed_null = SemanticContextManifestV1.model_validate(null_manifest)

    unknown_manifest = _vector_manifest("007-governed-v2.json")
    for field in ("risk_state", "retention_state"):
        _set_fresh_and_evidence(unknown_manifest, field, "unknown")
    parsed_unknown = SemanticContextManifestV1.model_validate(unknown_manifest)

    assert parsed_null.items[0].evidence is not None
    assert parsed_unknown.items[0].evidence is not None
    assert parsed_null.items[0].evidence.risk_state is None
    assert parsed_null.items[0].evidence.retention_state is None
    assert parsed_unknown.items[0].evidence.risk_state == "unknown"
    assert parsed_unknown.items[0].evidence.retention_state == "unknown"


@pytest.mark.parametrize("field", ["admission_policy", "packing_version"])
def test_legacy_rejects_candidate_protocol_version(field: str) -> None:
    manifest = _vector_manifest("001-legacy-single.json")
    manifest["versions"][field] = (
        "recall-admission-v2" if field == "admission_policy" else "recall-packing-v1"
    )
    with pytest.raises(ValidationError):
        SemanticContextManifestV1.model_validate(manifest)


def test_legacy_rejects_candidate_item_facts() -> None:
    manifest = _vector_manifest("001-legacy-single.json")
    candidate = _vector_manifest("007-governed-v2.json")["items"][0]
    for field in ("admission", "evidence", "relevance_score", "utility_score", "packing_reason"):
        manifest["items"][0][field] = deepcopy(candidate[field])
    with pytest.raises(ValidationError):
        SemanticContextManifestV1.model_validate(manifest)


@pytest.mark.parametrize("profile", ["governed", "exploratory"])
@pytest.mark.parametrize("field", ["admission_policy", "packing_version"])
def test_candidate_profiles_require_exact_protocol_versions(profile: str, field: str) -> None:
    manifest = _vector_manifest("007-governed-v2.json")
    manifest["versions"]["recall_profile"] = profile
    manifest["items"][0]["admission"]["profile"] = profile
    surface = "semantic_governed" if profile == "governed" else "semantic_exploratory"
    manifest["items"][0]["admission"]["surface"] = surface
    manifest["items"][0]["admission"]["v2"]["surface"] = surface
    manifest["versions"][field] = None
    with pytest.raises(ValidationError):
        SemanticContextManifestV1.model_validate(manifest)


def _constraint_values(name: str) -> set[str]:
    ddl = (ROOT / "migrations" / "001_init.sql").read_text()
    match = re.search(rf"CONSTRAINT {name} CHECK \((.*?)\n    \)", ddl, re.DOTALL)
    assert match is not None
    return set(re.findall(r"'([^']+)'", match.group(1)))


def test_receipt_specific_aliases_cannot_drift_from_canonical_sources() -> None:
    assert set(get_args(SemanticV2ProfileKeyV1)) == {V2_ADMISSION_PROFILE_KEY}
    assert set(get_args(SemanticConflictTypeV1)) == _constraint_values("chk_conflict_type")
    assert set(get_args(SemanticConflictResolutionStatusV1)) == _constraint_values(
        "chk_conflict_resolution"
    )
