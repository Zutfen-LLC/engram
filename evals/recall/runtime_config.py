"""Closed identity of deployment settings that can change a replay outcome.

Issue #198 requires deterministic replay for fixed frozen input, state,
configuration, and time. PostgreSQL-stored state is covered by the snapshot
digest; this module binds the deployment-level ``settings.*`` values the
legacy/governed/exploratory comparison call graph actually reads.

The projection is a closed, explicitly enumerated list — never a dump of the
settings object. Secrets (API keys, credentials, raw internal URLs) are
excluded; where a value could identify private infrastructure, only a digest
is recorded. Unknown or unavailable values are preserved honestly rather
than coerced.
"""

from __future__ import annotations

from typing import Any, Final

from evals.admission.schema import digest

RUNTIME_CONFIG_IDENTITY_VERSION: Final = "engram-recall-runtime-config-v1"


def runtime_config_identity() -> dict[str, Any]:
    """Return the material outcome-affecting deployment settings.

    Every field below cites the call-graph site that reads it. Adding a
    setting here requires that citation; nothing else from ``Settings``
    belongs in a replay identity.
    """
    from engram.config import settings

    return {
        "version": RUNTIME_CONFIG_IDENTITY_VERSION,
        # engram/recall.py::_resolve_recall_budgets — defaults applied when a
        # manifest case omits its budget (token budgets have no default).
        "recall_budget_defaults": {
            "recall_byte_budget": int(settings.recall_byte_budget),
            "recall_item_budget": int(settings.recall_item_budget),
        },
        # engram/recall.py::_admit_and_rank_signal_items /
        # _expand_signal_candidates and engram/relationship_recall.py — the
        # expansion toggles, per-seed/total caps, merged-set ceiling, and
        # relationship relevance weights that shape the candidate corpus,
        # ranking, and packing.
        "relationship_expansion": {
            "relationship_expansion_enabled": bool(
                settings.relationship_expansion_enabled
            ),
            "recall_semantic_expansion_seed_limit": int(
                settings.recall_semantic_expansion_seed_limit
            ),
            "recall_candidate_ceiling": int(settings.recall_candidate_ceiling),
            "max_graph_neighbors_per_item": int(settings.max_graph_neighbors_per_item),
            "max_graph_expanded_items": int(settings.max_graph_expanded_items),
            "max_tunnel_neighbors_per_item": int(
                settings.max_tunnel_neighbors_per_item
            ),
            "max_tunnel_additions": int(settings.max_tunnel_additions),
            "relationship_score_weight_semantic": float(
                settings.relationship_score_weight_semantic
            ),
            "relationship_score_weight_relationship": float(
                settings.relationship_score_weight_relationship
            ),
            "relationship_score_weight_tunnel": float(
                settings.relationship_score_weight_tunnel
            ),
            "relationship_score_weight_importance": float(
                settings.relationship_score_weight_importance
            ),
        },
        # engram/admission_shadow.py::resolve_bulk_v2_decisions /
        # _assessment_state and engram/assessments.py::
        # _effective_assessment_selection_bulk_counted — whether #157
        # effective selection participates in each V2 decision, which
        # effective contract hash it accepts, and the selection policy
        # version the selected-vs-mismatched check compares against.
        "assessment_selection": {
            "assessment_selection_enabled": bool(settings.assessment_selection_enabled),
            "assessment_effective_contract_hash": str(
                settings.assessment_effective_contract_hash
            ),
            "assessment_policy_version": str(settings.assessment_policy_version),
        },
        # engram/embeddings.py::generate_embedding — the provider gate and the
        # provider host identity for the initial query-embedding capture
        # (deterministic replay itself uses the frozen vector, but capture
        # feasibility and vector provenance depend on these). Only a digest
        # of the base URL is recorded: the URL itself may name private
        # infrastructure.
        "query_embedding_capture": {
            "embedding_provider": str(settings.embedding_provider),
            "openai_base_url_digest": (
                digest(str(settings.openai_base_url))
                if settings.openai_base_url
                else None
            ),
        },
    }


def runtime_config_digest() -> str:
    """Digest of the closed outcome-affecting settings projection."""
    return digest(runtime_config_identity())


__all__ = [
    "RUNTIME_CONFIG_IDENTITY_VERSION",
    "runtime_config_digest",
    "runtime_config_identity",
]
