"""Generate frozen semantic-context-manifest-v1 conformance vectors."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from engram.context_manifest import canonical_json_bytes, compute_manifest_hash
from engram.semantic_context_manifest import (
    SemanticManifestDecisionContextV1,
    build_semantic_context_manifest_v1,
)

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "conformance" / "semantic-context-manifest-v1" / "vectors"
TENANT = "00000000-0000-0000-0000-000000000001"
PRINCIPAL = "00000000-0000-0000-0000-000000000002"
WORKSPACE = "00000000-0000-0000-0000-000000000003"
PROFILE = "00000000-0000-0000-0000-000000000004"
REVISION = "00000000-0000-0000-0000-000000000005"
ASSESSMENT = "00000000-0000-0000-0000-000000000006"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64


def legacy_item(item_id: int, content: str, *, kind: str = "fact") -> dict[str, Any]:
    return {
        "id": f"00000000-0000-0000-0000-{item_id:012d}",
        "kind": kind,
        "content": content,
        "review_status": "active",
        "authority": 10,
        "visibility": "private",
        "workspace_id": None,
        "pinned": False,
        "score": 0.8,
        "trust_score": 0.9,
        "relevance_score": None,
        "utility_score": None,
        "reasons": ["semantic similarity 0.90"],
        "warnings": [],
        "warning_codes": None,
        "conflict_type": None,
        "conflict_resolution_status": None,
        "admission": None,
        "evidence": None,
        "relationship": None,
        "packing_reason": None,
    }


def assessment_ref() -> dict[str, Any]:
    return {
        "assessment_id": ASSESSMENT,
        "contract_hash": DIGEST_C,
        "canonical_hash": DIGEST_B,
        "purpose": "combined",
        "assertion_mode": "direct_statement",
        "origin": "user",
    }


def candidate_item(item_id: int, content: str, *, relationship: bool = False) -> dict[str, Any]:
    item = legacy_item(item_id, content)
    fresh = {
        "schema_version": "engram.admission-assessment.v2",
        "policy_version": "risk-aware-shadow-v1",
        "policy_artifact_digest": DIGEST_A,
        "decision_hash": DIGEST_B,
        "surface_decision": "allow",
        "highest_admission_tier": "semantic_governed",
        "risk_state": "low",
        "epistemic_state": "supported",
        "retention_state": "retain",
        "effective_assessment_refs": [assessment_ref()],
        "observation_window_hours": 24,
        "eligible_at": "2026-09-08T12:00:00+00:00",
        "next_evaluation_at": None,
        "blocker_codes": [],
        "reason_codes": ["evidence_supported"],
        "next_actions": [],
    }
    v2 = {
        "profile_key": "risk_aware_shadow_v1",
        "resolution_status": "current",
        "surface": "semantic_governed",
        "surface_decision": "allow",
        "persisted": {
            "assessment_id": ASSESSMENT,
            "schema_version": "engram.admission-assessment.v2",
            "policy_contract_version": "risk-aware-shadow-v1",
            "policy_artifact_digest": DIGEST_A,
            "decision_hash": DIGEST_B,
        },
        "fresh": fresh,
    }
    item.update(
        {
            "trust_score": None,
            "relevance_score": 0.8,
            "utility_score": 0.7,
            "warning_codes": [],
            "admission": {
                "profile": "governed",
                "decision": "admit",
                "policy_version": "recall-admission-v2",
                "reason_codes": ["admitted_v2_surface_allow"],
                "assessment_id": ASSESSMENT,
                "assessment_status": "current",
                "assessment_outcome": "would_admit",
                "surface": "semantic_governed",
                "surface_decision": "allow",
                "v2": v2,
            },
            "evidence": {
                "source": "v2_fresh_evaluation",
                "profile_key": "risk_aware_shadow_v1",
                "policy_version": "risk-aware-shadow-v1",
                "policy_artifact_digest": DIGEST_A,
                "decision_hash": DIGEST_B,
                "v2_resolution_status": "current",
                "epistemic_state": "supported",
                "risk_state": "low",
                "retention_state": "retain",
                "effective_assessment_refs": [assessment_ref()],
            },
            "packing_reason": "ranked",
        }
    )
    if relationship:
        item["relationship"] = {
            "version": "relationship-relevance-v1",
            "origins": ["semantic", "graph"],
            "direct": True,
            "direct_semantic_score": 0.7,
            "source_seed_score": 0.9,
            "graph_contribution": 0.5,
            "graph_edge_types": ["supports"],
            "tunnel_labels": [],
            "relevance_score": 0.8,
            "components": {"semantic": 0.6, "graph": 0.2, "tunnel": 0.0},
        }
    return item


def context(*, profile: str = "legacy", workspace: str | None = None) -> dict[str, Any]:
    signal = profile != "legacy"
    return {
        "tenant_id": TENANT,
        "principal_id": PRINCIPAL,
        "workspace_id": workspace,
        "memory_context_version": "memory-context-v2",
        "memory_profile_id": PROFILE if signal else None,
        "memory_profile_revision_id": REVISION if signal else None,
        "memory_profile_version": 3 if signal else None,
        "workspace_supplied": workspace is not None,
        "requested_byte_budget": None,
        "requested_token_budget": None,
        "requested_item_budget": None,
        "effective_byte_budget": 4096,
        "effective_token_budget": 1024,
        "effective_item_budget": 20,
        "recall_profile": profile,
        "recall_profile_contract_version": "recall-profiles-v1",
        "scoring_version": "signals-v1" if signal else "semantic-v3",
        "signals_version": "recall-signals-v1" if signal else None,
        "admission_policy": "recall-admission-v2" if signal else None,
        "relationship_relevance_version": None,
        "packing_version": "recall-packing-v1" if signal else None,
        "config_version": "v1",
    }


def packet(
    query: str,
    items: list[dict[str, Any]],
    *,
    ctx: dict[str, Any] | None = None,
    candidate_count: int | None = None,
    expansion: dict[str, Any] | None = None,
    packing: dict[str, Any] | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    working_set = "\n".join(f"[{item['kind']}] {item['content']}" for item in items)
    return {
        "query": query,
        "context": ctx or context(),
        "packet": {
            "items": items,
            "working_set": working_set,
            "item_count": len(items),
            "byte_count": sum(len(item["content"].encode("utf-8")) for item in items),
            "candidate_count": len(items) if candidate_count is None else candidate_count,
            "omitted_count": 0,
            "omitted_by_admission": {},
            "expansion": expansion,
            "packing": packing,
            "message": message,
        },
    }


def build(vector_input: dict[str, Any]) -> Any:
    pkt = vector_input["packet"]
    return build_semantic_context_manifest_v1(
        **pkt,
        query=vector_input["query"],
        context=SemanticManifestDecisionContextV1.model_validate(vector_input["context"]),
    )


def write(name: str, description: str, vector_input: dict[str, Any]) -> None:
    manifest = build(vector_input)
    payload = manifest.model_dump(mode="json", by_alias=True, exclude_none=False)
    canonical = canonical_json_bytes(payload).decode("utf-8")
    expected = {
        "manifest": payload,
        "canonical_json": canonical,
        "manifest_hash": compute_manifest_hash(manifest),
        "packet_hash": manifest.packet.hash,
        "query_digest": manifest.request.query_digest,
        "request_digest": manifest.request.request_digest,
        "served_content_hashes": [item.served_content_hash for item in manifest.items],
    }
    (OUT / f"{name}.json").write_text(
        json.dumps(
            {"name": name, "description": description, "input": vector_input, "expected": expected},
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    base = packet("alpha query", [legacy_item(10, "alpha")])
    multi = packet("ordered", [legacy_item(10, "alpha"), legacy_item(11, "beta")])
    unicode = packet("記憶 café", [legacy_item(12, "naïve 雪")])
    empty = packet("nothing", [], message="No matching memories found.")
    budgets = deepcopy(base)
    budgets["context"].update(
        {
            "requested_byte_budget": 9999,
            "requested_token_budget": 999,
            "requested_item_budget": 99,
            "effective_byte_budget": 100,
            "effective_token_budget": 25,
            "effective_item_budget": 2,
        }
    )
    workspace = packet("workspace", [legacy_item(13, "scoped")], ctx=context(workspace=WORKSPACE))
    workspace["packet"]["items"][0]["workspace_id"] = WORKSPACE
    governed_ctx = context(profile="governed")
    governed = packet(
        "governed",
        [candidate_item(14, "qualified")],
        ctx=governed_ctx,
        packing={
            "version": "recall-packing-v1",
            "selected_count": 1,
            "conflict_pairs_preserved": 0,
            "omitted": {},
        },
    )
    relationship_ctx = context(profile="governed")
    relationship_ctx["relationship_relevance_version"] = "relationship-relevance-v1"
    relationship = packet(
        "linked",
        [candidate_item(15, "linked fact", relationship=True)],
        ctx=relationship_ctx,
        expansion={
            "version": "relationship-relevance-v1",
            "seed_count": 1,
            "discovered_neighbors": 1,
            "graph_neighbors": 1,
            "tunnel_neighbors": 0,
            "admitted_expanded": 1,
            "withheld_expanded": 0,
        },
        packing={
            "version": "recall-packing-v1",
            "selected_count": 1,
            "conflict_pairs_preserved": 0,
            "omitted": {},
        },
    )
    conflict_items = [candidate_item(16, "claim A"), candidate_item(17, "claim B")]
    conflict_items[1]["packing_reason"] = "conflict_pair_preserved"
    conflict = packet(
        "conflict",
        conflict_items,
        ctx=context(profile="governed"),
        candidate_count=3,
        packing={
            "version": "recall-packing-v1",
            "selected_count": 2,
            "conflict_pairs_preserved": 1,
            "omitted": {"budget": 1},
        },
    )
    nullable = packet("nullable", [legacy_item(18, "null fields")])
    nullable["context"].update(
        {
            "effective_byte_budget": None,
            "effective_token_budget": None,
            "effective_item_budget": None,
        }
    )

    vectors = [
        ("001-legacy-single", "Legacy single-item semantic packet.", base),
        ("002-legacy-multi", "Legacy ordered multi-item packet.", multi),
        ("003-unicode", "Unicode query and content.", unicode),
        ("004-empty", "Valid empty semantic packet.", empty),
        ("005-requested-effective-budgets", "Requested and effective budgets differ.", budgets),
        ("006-workspace", "Workspace-scoped packet.", workspace),
        ("007-governed-v2", "Candidate packet with full V2 evidence.", governed),
        ("008-relationship", "Candidate relationship-expanded item.", relationship),
        ("009-conflict-packing", "Conflict-preserved packing reason.", conflict),
        ("010-nullable", "Nullable optional fields.", nullable),
    ]
    for name, description, value in vectors:
        write(name, description, value)


if __name__ == "__main__":
    main()
