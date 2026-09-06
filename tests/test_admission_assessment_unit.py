"""Pure, database-free proof of the #159 decision contract.

Covers the outcome vocabulary and its precedence, the next-action mapping, the
cooling-versus-insufficient-evidence and unknown-versus-missing distinctions,
reason codes, and the digest/hash determinism rules. Everything here is a
statement about the decision contract itself, so none of it needs PostgreSQL.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from engram.admission_assessment import (
    ADMISSION_OUTCOMES,
    ADMISSION_REASON_CODES,
    CONFLICT_RECHECK_STATUSES,
    NEXT_ACTION_ORDER,
    POLICY_CONTRACT_VERSION,
    POLICY_PROFILE_KEY,
    SCHEMA_VERSION,
    AdmissionAssessmentError,
    LaneQualification,
    ResolvedAdmission,
    build_decision,
    canonical_blocker_order,
    classify_outcome,
    decision_hash,
    digest,
    input_state_payload,
    next_actions_for,
    next_evaluation_for,
    policy_config_payload,
    reason_codes_for,
    resolve_projection_status,
    summary_payload,
)
from engram.promotion import PROMOTION_BLOCKER_CODES

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

QUALIFIED = LaneQualification(
    legacy_trust_qualified=True,
    legacy_age_qualified=True,
    evidence_trust_qualified=False,
    evidence_age_qualified=False,
)
AWAITING_AGE = LaneQualification(
    legacy_trust_qualified=True,
    legacy_age_qualified=False,
    evidence_trust_qualified=False,
    evidence_age_qualified=False,
)
NO_LANE = LaneQualification(False, False, False, False)


class _Item:
    """The minimal item surface the decision builder reads."""

    def __init__(self, **overrides: object) -> None:
        self.tenant_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
        self.id = uuid.UUID("22222222-2222-4222-8222-222222222222")
        self.content_hash = "sha256:" + "a" * 64
        self.kind = "fact"
        self.review_status = "proposed"
        self.valid_to = None
        self.superseded_by = None
        self.source_type = "manual"
        self.source_trust = 0.8
        self.source_confidence_prior = 0.6
        self.memory_confidence = 0.9
        self.retention_confidence = 0.8
        self.retention_disposition = "retain"
        self.retention_evidence_at = NOW - timedelta(days=10)
        self.conflict_resolution_status = None
        self.conflicts_with_item_id = None
        self.authority = 10
        self.sensitivity = "normal"
        self.human_verified = False
        self.visibility = "private"
        self.created_at = NOW - timedelta(days=30)
        for key, value in overrides.items():
            setattr(self, key, value)


def _policy(**overrides: object) -> dict[str, object]:
    base = dict(
        confidence_threshold=0.7,
        min_age_hours=72,
        evidence_enabled=True,
        evidence_threshold=0.7,
        kind_auto_promote_allowed=True,
    )
    base.update(overrides)
    return policy_config_payload(**base)  # type: ignore[arg-type]


# --- Vocabulary is closed and matches the canonical promotion vocabulary -----


def test_blocker_vocabulary_is_exactly_the_promotion_module_vocabulary() -> None:
    """Every blocker #159 can classify comes from engram.promotion, and every
    blocker engram.promotion can emit is classified by #159 — neither side may
    drift silently."""
    from engram.admission_assessment import (
        _BLOCKED_BLOCKERS,
        _INSUFFICIENT_BLOCKERS,
        _REVIEW_BLOCKERS,
    )

    classified = _BLOCKED_BLOCKERS | _REVIEW_BLOCKERS | _INSUFFICIENT_BLOCKERS | {"age"}
    assert classified == PROMOTION_BLOCKER_CODES


def test_outcome_and_next_action_vocabularies_are_exactly_v1() -> None:
    expected_outcomes = {
        "admitted",
        "would_admit",
        "cooling",
        "review_required",
        "blocked",
        "insufficient_evidence",
        "unknown",
        "stale",
        "not_applicable",
    }
    expected_actions = {
        "wait_until",
        "classification_required",
        "human_review_required",
        "conflict_resolution_required",
        "new_evidence_required",
        "policy_reconciliation_required",
        "none",
    }
    assert expected_outcomes == ADMISSION_OUTCOMES
    assert set(NEXT_ACTION_ORDER) == expected_actions


# --- Outcome classification --------------------------------------------------


def test_successful_mutation_is_always_admitted() -> None:
    """Even carrying blockers from a superseded pre-recheck view, a completed
    mutation is admitted — nothing outranks what actually happened."""
    assert (
        classify_outcome(
            mode="authoritative",
            mutated=True,
            would_admit=False,
            live_proposal=True,
            blockers=["conflict"],
            lanes=QUALIFIED,
        )
        == "admitted"
    )


def test_shadow_equivalent_of_a_mutation_is_would_admit() -> None:
    assert (
        classify_outcome(
            mode="shadow",
            mutated=False,
            would_admit=True,
            live_proposal=True,
            blockers=[],
            lanes=QUALIFIED,
        )
        == "would_admit"
    )


def test_authoritative_would_admit_without_mutation_is_unknown_not_admitted() -> None:
    """An authoritative pass that would admit performs the mutation. If it did
    not, something in the locked state stopped it, and inventing an
    `admitted` or a `would_admit` there would misreport a state change that
    never occurred."""
    assert (
        classify_outcome(
            mode="authoritative",
            mutated=False,
            would_admit=True,
            live_proposal=True,
            blockers=[],
            lanes=QUALIFIED,
        )
        == "unknown"
    )


@pytest.mark.parametrize(
    ("blockers", "expected"),
    [
        (["conflict"], "blocked"),
        (["conflict_recheck"], "blocked"),
        (["kind_policy"], "review_required"),
        (["review_policy"], "review_required"),
        (["external_dispute"], "review_required"),
        (["confidence"], "insufficient_evidence"),
        (["evidence_score"], "insufficient_evidence"),
        (["no_retention_evidence"], "insufficient_evidence"),
        (["evidence_version"], "insufficient_evidence"),
        (["evidence_inconsistent"], "insufficient_evidence"),
        (["missing_source_prior"], "insufficient_evidence"),
        (["retention_disposition"], "insufficient_evidence"),
        (["taxonomy_confidence"], "insufficient_evidence"),
        (["evidence_disabled"], "insufficient_evidence"),
    ],
)
def test_blocker_categories(blockers: list[str], expected: str) -> None:
    assert (
        classify_outcome(
            mode="authoritative",
            mutated=False,
            would_admit=False,
            live_proposal=True,
            blockers=blockers,
            lanes=NO_LANE,
        )
        == expected
    )


def test_precedence_order_is_stale_blocked_review_cooling_insufficient_unknown() -> None:
    every_category = ["conflict", "kind_policy", "age", "confidence"]
    assert (
        classify_outcome(
            mode="authoritative",
            mutated=False,
            would_admit=False,
            live_proposal=True,
            blockers=every_category,
            lanes=AWAITING_AGE,
        )
        == "blocked"
    )
    assert (
        classify_outcome(
            mode="authoritative",
            mutated=False,
            would_admit=False,
            live_proposal=True,
            blockers=["kind_policy", "age", "confidence"],
            lanes=AWAITING_AGE,
        )
        == "review_required"
    )
    assert (
        classify_outcome(
            mode="authoritative",
            mutated=False,
            would_admit=False,
            live_proposal=True,
            blockers=["age", "confidence"],
            lanes=AWAITING_AGE,
        )
        == "cooling"
    )


def test_stale_outranks_every_other_category() -> None:
    assert (
        classify_outcome(
            mode="authoritative",
            mutated=False,
            would_admit=False,
            live_proposal=True,
            blockers=["conflict", "kind_policy", "confidence"],
            lanes=NO_LANE,
            policy_changed=True,
        )
        == "stale"
    )


def test_cooling_requires_a_lane_that_would_otherwise_qualify() -> None:
    """The critical distinction: an `age` blocker on an item no lane could
    admit is insufficient evidence, not cooling. Calling it cooling would
    promise an operator that waiting is enough."""
    assert (
        classify_outcome(
            mode="authoritative",
            mutated=False,
            would_admit=False,
            live_proposal=True,
            blockers=["age", "confidence"],
            lanes=NO_LANE,
        )
        == "insufficient_evidence"
    )
    assert (
        classify_outcome(
            mode="authoritative",
            mutated=False,
            would_admit=False,
            live_proposal=True,
            blockers=["age", "confidence"],
            lanes=AWAITING_AGE,
        )
        == "cooling"
    )


def test_an_unexplained_state_is_unknown_never_insufficient_evidence() -> None:
    assert (
        classify_outcome(
            mode="authoritative",
            mutated=False,
            would_admit=False,
            live_proposal=True,
            blockers=[],
            lanes=NO_LANE,
        )
        == "unknown"
    )
    assert (
        classify_outcome(
            mode="authoritative",
            mutated=False,
            would_admit=False,
            live_proposal=True,
            blockers=["confidence"],
            lanes=NO_LANE,
            uninterpretable=True,
        )
        == "insufficient_evidence"
    )


def test_a_non_live_item_is_not_applicable() -> None:
    assert (
        classify_outcome(
            mode="authoritative",
            mutated=False,
            would_admit=False,
            live_proposal=False,
            blockers=["confidence"],
            lanes=NO_LANE,
        )
        == "not_applicable"
    )


# --- Next actions -----------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "blockers", "expected"),
    [
        ("admitted", [], ["none"]),
        ("would_admit", [], ["none"]),
        ("not_applicable", [], ["none"]),
        ("cooling", ["age"], ["wait_until"]),
        ("blocked", ["conflict"], ["conflict_resolution_required"]),
        ("blocked", ["conflict_recheck"], ["conflict_resolution_required"]),
        ("review_required", ["kind_policy"], ["human_review_required"]),
        ("review_required", ["review_policy"], ["human_review_required"]),
        ("review_required", ["external_dispute"], ["human_review_required"]),
        ("insufficient_evidence", ["no_retention_evidence"], ["classification_required"]),
        ("insufficient_evidence", ["evidence_version"], ["classification_required"]),
        ("insufficient_evidence", ["evidence_inconsistent"], ["classification_required"]),
        ("insufficient_evidence", ["evidence_score"], ["new_evidence_required"]),
        ("insufficient_evidence", ["retention_disposition"], ["new_evidence_required"]),
        ("insufficient_evidence", ["confidence"], ["new_evidence_required"]),
        ("insufficient_evidence", ["missing_source_prior"], ["new_evidence_required"]),
        ("insufficient_evidence", ["taxonomy_confidence"], ["new_evidence_required"]),
        (
            "insufficient_evidence",
            ["evidence_disabled"],
            ["policy_reconciliation_required"],
        ),
        ("stale", [], ["policy_reconciliation_required"]),
    ],
)
def test_required_next_action_mappings(
    outcome: str, blockers: list[str], expected: list[str]
) -> None:
    assert next_actions_for(outcome=outcome, blockers=blockers) == expected  # type: ignore[arg-type]


def test_independently_required_actions_are_all_returned_in_fixed_order() -> None:
    actions = next_actions_for(
        outcome="cooling", blockers=["age", "conflict", "kind_policy", "evidence_score"]
    )
    assert actions == [
        "wait_until",
        "conflict_resolution_required",
        "human_review_required",
        "new_evidence_required",
    ]
    # Discovery order must not change the result — the hash depends on it.
    assert actions == next_actions_for(
        outcome="cooling", blockers=["evidence_score", "kind_policy", "conflict", "age"]
    )


def test_an_unknown_outcome_with_nothing_else_to_do_asks_for_a_human() -> None:
    assert next_actions_for(outcome="unknown", blockers=[]) == ["human_review_required"]


# --- Reason codes ------------------------------------------------------------


def test_reason_codes_are_closed_sorted_and_explain_the_outcome() -> None:
    reasons = reason_codes_for(
        mode="authoritative",
        outcome="cooling",
        selected_basis=None,
        blockers=["age"],
        lanes=AWAITING_AGE,
    )
    assert reasons == sorted(reasons)
    assert set(reasons) <= ADMISSION_REASON_CODES
    assert "lane_qualified_awaiting_age" in reasons
    assert "no_lane_qualified" in reasons


def test_shadow_and_legacy_import_modes_are_named_in_the_reasons() -> None:
    assert "shadow_preview" in reason_codes_for(
        mode="shadow",
        outcome="would_admit",
        selected_basis="legacy_confidence",
        blockers=[],
        lanes=QUALIFIED,
    )
    assert "legacy_import_snapshot" in reason_codes_for(
        mode="legacy_import",
        outcome="not_applicable",
        selected_basis=None,
        blockers=[],
        lanes=NO_LANE,
        legacy_evidence_unavailable=True,
    )


def test_a_lost_race_is_recorded_as_such() -> None:
    reasons = reason_codes_for(
        mode="authoritative",
        outcome="not_applicable",
        selected_basis="legacy_confidence",
        blockers=[],
        lanes=QUALIFIED,
        race_lost=True,
    )
    assert "mutation_race_lost" in reasons
    assert "item_not_live_proposal" in reasons
    assert "mutation_committed" not in reasons


# --- Digests and the canonical hash -----------------------------------------


def test_policy_config_digest_excludes_volatile_invocation_metadata() -> None:
    payload = _policy()
    assert not {"job_id", "evaluation_id", "request_id", "evaluated_at"} & set(payload)
    assert payload["policy_profile_key"] == POLICY_PROFILE_KEY
    assert payload["policy_contract_version"] == POLICY_CONTRACT_VERSION
    assert payload["kind_auto_promote_allowed"] is True


def test_policy_config_digest_moves_with_every_decision_affecting_value() -> None:
    base = digest(_policy())
    for change in (
        {"confidence_threshold": 0.71},
        {"min_age_hours": 73},
        {"evidence_enabled": False},
        {"evidence_threshold": 0.71},
        {"kind_auto_promote_allowed": False},
    ):
        assert digest(_policy(**change)) != base, change


def test_input_digest_moves_with_item_state_but_not_with_content_text() -> None:
    item = _Item()
    base = digest(input_state_payload(item, None))  # type: ignore[arg-type]
    assert digest(input_state_payload(_Item(memory_confidence=0.5), None)) != base  # type: ignore[arg-type]
    assert digest(input_state_payload(_Item(review_status="active"), None)) != base  # type: ignore[arg-type]
    assert digest(input_state_payload(_Item(kind="preference"), None)) != base  # type: ignore[arg-type]
    payload = input_state_payload(item, None)  # type: ignore[arg-type]
    assert "content" not in payload


def test_input_digest_ignores_157_evidence_assessment_identity() -> None:
    """#157 references are diagnostic in v1. If they moved the input digest a
    new evidence assessment would retroactively make every prior admission
    decision look stale."""
    payload = input_state_payload(_Item(), None)  # type: ignore[arg-type]
    assert not any("assessment" in key for key in payload)


def test_blocker_order_is_canonicalized_before_hashing() -> None:
    assert canonical_blocker_order(["confidence", "age", "confidence"]) == ("age", "confidence")
    assert canonical_blocker_order(["age", "confidence"]) == canonical_blocker_order(
        ["confidence", "age"]
    )


def _decision(**overrides: object) -> object:
    kwargs: dict[str, object] = dict(
        item=_Item(),
        run=None,
        mode="authoritative",
        mutated=False,
        live_proposal=True,
        blockers=["age"],
        selected_basis=None,
        lanes=AWAITING_AGE,
        decision_inputs={"legacy_trust_qualified": True, "legacy_age_qualified": False},
        policy_config=_policy(),
        conflict_recheck_status="not_run",
        cooling_period_start=NOW - timedelta(days=1),
        eligible_at=NOW + timedelta(days=1),
        next_evaluation_at=NOW + timedelta(days=1),
    )
    kwargs.update(overrides)
    return build_decision(**kwargs)  # type: ignore[arg-type]


def test_decision_hash_is_stable_across_blocker_discovery_order() -> None:
    first = _decision(blockers=["age", "confidence"])
    second = _decision(blockers=["confidence", "age"])
    assert first.hash() == second.hash()  # type: ignore[attr-defined]


def test_decision_hash_changes_with_every_hashed_field() -> None:
    base = _decision().hash()  # type: ignore[attr-defined]
    assert _decision(mode="shadow").hash() != base  # type: ignore[attr-defined]
    assert _decision(policy_config=_policy(min_age_hours=99)).hash() != base  # type: ignore[attr-defined]
    assert _decision(item=_Item(memory_confidence=0.1)).hash() != base  # type: ignore[attr-defined]
    assert _decision(decision_inputs={"other": 1}).hash() != base  # type: ignore[attr-defined]
    assert _decision(conflict_recheck_status="not_run_preview").hash() != base  # type: ignore[attr-defined]


def test_decision_hash_format_is_sha256_hex() -> None:
    value = _decision().hash()  # type: ignore[attr-defined]
    assert value.startswith("sha256:")
    assert len(value) == len("sha256:") + 64
    assert set(value.removeprefix("sha256:")) <= set("0123456789abcdef")


def test_envelope_carries_exactly_the_documented_fields() -> None:
    envelope = _decision().envelope()  # type: ignore[attr-defined]
    assert set(envelope) == {
        "schema_version",
        "tenant_id",
        "memory_item_id",
        "mode",
        "item_content_hash",
        "input_digest",
        "policy_profile_key",
        "policy_contract_version",
        "policy_config_digest",
        "selected_basis",
        "outcome",
        "blocker_codes",
        "reason_codes",
        "decision_inputs",
        "conflict_recheck_status",
        "cooling_period_start",
        "eligible_at",
        "next_evaluation_at",
        "next_actions",
    }
    assert envelope["schema_version"] == SCHEMA_VERSION
    # Invocation identity and mutable projection state must never be hashed.
    for excluded in ("assessment_id", "job_id", "evaluation_id", "actor_principal_id",
                     "created_at", "evaluated_at", "prior_assessment_id"):
        assert excluded not in envelope


# --- Fail-closed guards ------------------------------------------------------


def test_a_shadow_decision_can_never_claim_a_mutation() -> None:
    with pytest.raises(AdmissionAssessmentError):
        _decision(mode="shadow", mutated=True)


def test_an_unknown_conflict_recheck_status_fails_closed() -> None:
    with pytest.raises(AdmissionAssessmentError):
        _decision(conflict_recheck_status="probably_fine")
    assert "not_run_preview" in CONFLICT_RECHECK_STATUSES


def test_a_cooling_decision_without_a_due_time_fails_closed() -> None:
    with pytest.raises(AdmissionAssessmentError):
        _decision(next_evaluation_at=None)


# --- Projection resolution ---------------------------------------------------


class _Row:
    def __init__(self, mode: str = "authoritative") -> None:
        self.id = uuid.uuid4()
        self.mode = mode
        self.input_digest = "sha256:" + "1" * 64
        self.policy_config_digest = "sha256:" + "2" * 64
        self.outcome = "cooling"
        self.reason_codes = ["lane_qualified_awaiting_age"]
        self.next_actions = ["wait_until"]
        self.next_evaluation_at = NOW + timedelta(days=1)
        self.policy_profile_key = POLICY_PROFILE_KEY
        self.policy_contract_version = POLICY_CONTRACT_VERSION


def test_projection_resolution_distinguishes_all_four_states() -> None:
    row = _Row()
    assert resolve_projection_status(
        None, current_input_digest="x", current_policy_config_digest="y"
    ).status == "missing"
    assert (
        resolve_projection_status(
            row,  # type: ignore[arg-type]
            current_input_digest=row.input_digest,
            current_policy_config_digest=row.policy_config_digest,
        ).status
        == "current"
    )
    assert (
        resolve_projection_status(
            row,  # type: ignore[arg-type]
            current_input_digest="sha256:" + "9" * 64,
            current_policy_config_digest=row.policy_config_digest,
        ).status
        == "stale"
    )
    assert (
        resolve_projection_status(
            row,  # type: ignore[arg-type]
            current_input_digest=row.input_digest,
            current_policy_config_digest="sha256:" + "9" * 64,
        ).status
        == "stale"
    )
    legacy = _Row(mode="legacy_import")
    assert (
        resolve_projection_status(
            legacy,  # type: ignore[arg-type]
            current_input_digest=legacy.input_digest,
            current_policy_config_digest=legacy.policy_config_digest,
        ).status
        == "legacy_import"
    )


def test_missing_is_not_the_same_as_an_unknown_outcome() -> None:
    """A reader must be able to tell "nothing has been recorded" from "policy
    looked and could not interpret the state safely"."""
    missing = summary_payload(ResolvedAdmission("missing", None))
    assert missing["admission_assessment_status"] == "missing"
    assert missing["admission_outcome"] is None

    row = _Row()
    row.outcome = "unknown"
    unknown = summary_payload(ResolvedAdmission("current", row))  # type: ignore[arg-type]
    assert unknown["admission_assessment_status"] == "current"
    assert unknown["admission_outcome"] == "unknown"


def test_summary_payload_never_leaks_decision_inputs_or_provider_detail() -> None:
    row = _Row()
    payload = summary_payload(ResolvedAdmission("current", row))  # type: ignore[arg-type]
    assert set(payload) == {
        "admission_assessment_id",
        "admission_assessment_status",
        "admission_outcome",
        "admission_policy_profile",
        "admission_policy_version",
        "admission_reason_codes",
        "admission_next_actions",
        "next_evaluation_at",
    }


# --- Due time ----------------------------------------------------------------


def test_next_evaluation_is_the_boundary_when_one_exists_and_none_otherwise() -> None:
    due = NOW + timedelta(hours=5)
    assert next_evaluation_for(outcome_eligible_at=due, now=NOW) == due
    # A boundary already in the past is not a future due time.
    assert next_evaluation_for(outcome_eligible_at=NOW - timedelta(hours=1), now=NOW) is None
    # No deterministic due time is invented for a state only an external event
    # can change.
    assert next_evaluation_for(outcome_eligible_at=None, now=NOW) is None
    assert next_evaluation_for(
        outcome_eligible_at=None, now=NOW, fallback_hours=24
    ) == NOW + timedelta(hours=24)


def test_decision_hash_helper_matches_the_decision_objects_hash() -> None:
    decision = _decision()
    assert decision_hash(decision.envelope()) == decision.hash()  # type: ignore[attr-defined]
