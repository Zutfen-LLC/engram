"""Canonical trust-proof test file list for Gate B verification.

This is the single source of truth for which test modules collectively prove
the trust, scope, RLS, attribution, review, feedback, promotion, conflict,
and worker-concurrency invariants of the system.

Used by ``make trust-proof`` and ``make compose-trust-proof`` so that the
canonical selection is maintained in one place rather than scattered across
documentation prose.

Usage::

    # Print the space-separated file list (for shell expansion):
    python scripts/trust_proof_files.py

    # Or import programmatically:
    from scripts.trust_proof_files import TRUST_PROOF_FILES
"""

from __future__ import annotations

# ── Scope / visibility / eligibility ─────────────────────────────────
# F1: shared-memory-item read eligibility
# F2: workspace membership + scope completeness
SCOPE_PROOFS = (
    "tests/test_item_read_eligibility.py",
    "tests/test_scope_completeness.py",
    "tests/test_scope_enforcement.py",
    "tests/test_scope_issuance.py",
    "tests/test_scopes_unit.py",
    # ENG-SCOPE-001: truthful scope invariants and safe write defaults.
    "tests/test_memory_scope_unit.py",
    "tests/test_memory_access_unit.py",
    "tests/test_scope_write_defaults.py",
    "tests/test_scope_write_defaults_migration.py",
)

# ── Row-level security ──────────────────────────────────────────────
# F3/F5: RLS enforcement under non-owner application role
RLS_PROOFS = (
    "tests/test_rls_isolation.py",
)

# ── Supersession lifecycle ──────────────────────────────────────────
# F6: unique-index ordering, old-row transition
SUPERSEDE_PROOFS = (
    "tests/test_supersede.py",
)

# ── Trust-weighted ranking ──────────────────────────────────────────
# F10: semantic scoring, F15: recall budget/packing
RANKING_PROOFS = (
    "tests/test_semantic_scoring.py",
    "tests/test_recall_scaling.py",
    "tests/test_recall_telemetry.py",
)

# ── Memory-kind governance ──────────────────────────────────────────
# F17: governed kind registry
KIND_PROOFS = (
    "tests/test_memory_kinds.py",
)

# ── Graph expansion ─────────────────────────────────────────────────
# F19: relationship, tunnel, graph recall
GRAPH_PROOFS = (
    "tests/test_relationship_recall.py",
    "tests/test_tunnel_recall.py",
    "tests/test_graph_recall.py",
)

# ── Attribution / actor identity ────────────────────────────────────
# Post-audit trust-integrity: authenticated mutation actor
ATTRIBUTION_PROOFS = (
    "tests/test_actor_identity.py",
    "tests/test_authority_policy.py",
    "tests/test_diary_worker_attribution.py",
)

# ── Review governance ───────────────────────────────────────────────
# Transitions, authorization, concurrency, KG eligibility, trusted actors
REVIEW_PROOFS = (
    "tests/test_review_policy.py",
    "tests/test_review_authorization.py",
    "tests/test_review_concurrency.py",
    "tests/test_review_kg_integrity.py",
    "tests/test_review_transition_scope.py",
    "tests/test_trusted_actor.py",
    "tests/test_verify_idempotency.py",
)

# ── Feedback integrity ──────────────────────────────────────────────
# Canonical verdicts, policy, migration
FEEDBACK_PROOFS = (
    "tests/test_feedback_integrity.py",
    "tests/test_feedback_policy.py",
    "tests/test_feedback_migration.py",
    "tests/test_recall_feedback.py",
)

# ── Promotion lifecycle ─────────────────────────────────────────────
# Lazy promotion Path A, vs review/feedback serialization
PROMOTION_PROOFS = (
    "tests/test_promotion_policy.py",
    "tests/test_promotion_v2_migration.py",
    "tests/test_promotion.py",
    "tests/test_promotion_review_concurrency.py",
    "tests/test_promotion_feedback_concurrency.py",
)

# ── Conflict resolution ─────────────────────────────────────────────
# Governance, atomicity, authorization
CONFLICT_PROOFS = (
    "tests/test_conflict_resolution_integrity.py",
    "tests/test_conflict_resolution_postgres.py",
    "tests/test_conflicts.py",
)

# ── Worker concurrency (Gate A serialization) ───────────────────────
# AUTO_SUPERSEDE, DEDUP, conflict-flagging, manual invalidation,
# metadata PATCH + classification refine, bulk archive
WORKER_CONCURRENCY_PROOFS = (
    "tests/test_worker_auto_supersede_concurrency.py",
    "tests/test_worker_dedup_concurrency.py",
    "tests/test_worker_flagging_concurrency.py",
    "tests/test_manual_invalidation_concurrency.py",
    "tests/test_metadata_patch_concurrency.py",
)

# ── Classification ──────────────────────────────────────────────────
# Trust policy, LLM refinement, async worker
CLASSIFICATION_PROOFS = (
    "tests/test_classification_evidence_migration.py",
    "tests/test_classification_trust.py",
    "tests/test_classification.py",
    "tests/test_worker_classification.py",
    "tests/test_worker_conflict.py",
)

# ── Session-end authority ───────────────────────────────────────────
# Stable authority integration
SESSION_END_PROOFS = (
    "tests/test_session_end_defaults.py",
    "tests/test_session_end_migration.py",
)

# ── Profile write-context / execution authority (ENG-SCOPE-002C) ─────
# Candidate origin vs remember-time execution authority, durable
# first-successful-execution pinning, concurrent ingest serialization,
# migration-025 RLS/privilege isolation, and profile-bound mutation scope.
PROFILE_EXECUTION_PROOFS = (
    "tests/test_candidate_execution_context_migration.py",
    "tests/test_candidate_execution_context_postgres.py",
    "tests/test_execution_context_durability_postgres.py",
    "tests/test_profile_authorization_regressions_postgres.py",
    "tests/test_profile_write_context_migration.py",
    "tests/test_profile_write_context_unit.py",
    "tests/test_worker_audit_provenance_postgres.py",
)

# ── Context manifest contract (ENG-CONTEXT-001) ─────────────────────
# The real-PostgreSQL route-parity proof: exercises the live startup recall
# route and proves the manifest built from the served response matches the
# HTTP response exactly (ordered IDs, scores, reasons, warnings, counts,
# packet bytes) and contains no raw content. The schema contract-integrity
# proof (drift + positive/negative JSON Schema validation) is DB-free but
# is gated here so compose-trust-proof enforces it with no skips.
# Pure-Python determinism / privacy / golden-vector coverage runs in the
# root suite.
CONTEXT_MANIFEST_PROOFS = (
    "tests/test_context_manifest_route.py",
    "tests/test_context_manifest_schema.py",
)

# ── Canonical aggregate ─────────────────────────────────────────────
TRUST_PROOF_FILES: tuple[str, ...] = (
    SCOPE_PROOFS
    + RLS_PROOFS
    + SUPERSEDE_PROOFS
    + RANKING_PROOFS
    + KIND_PROOFS
    + GRAPH_PROOFS
    + ATTRIBUTION_PROOFS
    + REVIEW_PROOFS
    + FEEDBACK_PROOFS
    + PROMOTION_PROOFS
    + CONFLICT_PROOFS
    + WORKER_CONCURRENCY_PROOFS
    + CLASSIFICATION_PROOFS
    + SESSION_END_PROOFS
    + PROFILE_EXECUTION_PROOFS
    + CONTEXT_MANIFEST_PROOFS
)


if __name__ == "__main__":
    print(" ".join(TRUST_PROOF_FILES))
