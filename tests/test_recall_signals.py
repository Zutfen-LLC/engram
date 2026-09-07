"""Unit tests for the separated recall signal model (issues #160 / #186).

Pure-function contract tests — no DB. These pin the core invariants:

* relevance, utility, epistemic state, governance, and risk stay separate;
* importance/utility can reorder admitted items but never change epistemic
  state or the admission decision;
* unknown evidence is admitted-or-withheld-and-marked, never converted into a
  numeric trust floor;
* for the V2-bound candidate profiles (issue #186) the admission authority is
  the exact #158 ``risk_aware_shadow_v1`` per-surface decision — never
  ``review_status``, and never a #159 Path-A binding. Missing, stale,
  mismatched, or unsupported V2 state fails closed with an explicit code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from engram.admission_policy import AdmissionPolicyDecision
from engram.models import MemoryItem
from engram.recall_profiles import EXPLORATORY_PROFILE, GOVERNED_PROFILE
from engram.recall_signals import (
    RECALL_ADMISSION_POLICY_VERSION,
    SIGNALS_VERSION,
    AdmissionAssessmentBinding,
    RecallAdmissionDecision,
    build_v2_surface_binding,
    compute_signal_rank_score,
    compute_utility_score,
    decide_recall_admission,
    derive_epistemic_state,
    signal_item_fields,
    structured_warning_codes,
    v2_local_gate_withhold_reason,
)

_NOW = datetime(2026, 9, 6, tzinfo=UTC)


def _make_item(**overrides: Any) -> MemoryItem:
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "tenant_id": uuid4(),
        "workspace_id": None,
        "principal_id": uuid4(),
        "content": "test memory content",
        "content_hash": "sha256:abc123",
        "kind": "fact",
        "visibility": "workspace",
        "review_status": "active",
        "memory_confidence": 0.5,
        "source_trust": 0.5,
        "human_verified": False,
        "verified_by": None,
        "verified_at": None,
        "importance": 0.5,
        "pinned": False,
        "last_recalled_at": None,
        "recall_count": 0,
        "startup_recall_count": 0,
        "last_verified_at": None,
        "source_type": "manual",
        "source_session": None,
        "source_uri": None,
        "extracted_by_model": None,
        "extraction_confidence": None,
        "conflicts_with_item_id": None,
        "conflict_type": None,
        "conflict_resolution_status": None,
        "conflict_resolved_by": None,
        "conflict_resolved_at": None,
        "sensitivity": "normal",
        "external_id": None,
        "external_source": None,
        "valid_from": _NOW - timedelta(days=1),
        "valid_to": None,
        "superseded_by": None,
        "created_at": _NOW - timedelta(days=1),
        "wing": None,
        "room": None,
        "subject_type": None,
        "subject_id": None,
        "subject_name": None,
    }
    defaults.update(overrides)
    return MemoryItem(**defaults)


@dataclass
class _FakeResolution:
    """The resolver result shape (``admission_shadow.ResolvedV2Decision``)."""

    status: str
    decision: AdmissionPolicyDecision
    assessment: Any = None


def _persisted_row(
    decision: AdmissionPolicyDecision,
    *,
    schema_version: str | None = None,
    policy_contract_version: str | None = None,
    policy_artifact_digest: str | None = None,
    decision_hash: str | None = None,
) -> SimpleNamespace:
    """A persisted-row stand-in whose identity defaults to the fresh
    decision's (the ``current`` case) and diverges per argument otherwise."""
    return SimpleNamespace(
        id=uuid4(),
        schema_version=schema_version if schema_version is not None else decision.schema_version,
        policy_contract_version=(
            policy_contract_version
            if policy_contract_version is not None
            else decision.policy_version
        ),
        policy_config_digest=(
            policy_artifact_digest
            if policy_artifact_digest is not None
            else decision.policy_config_digest
        ),
        decision_hash=decision_hash if decision_hash is not None else decision.decision_hash,
    )


def _v2_decision(
    *,
    governed: str = "allow",
    exploratory: str = "allow",
    startup: str = "withhold",
) -> AdmissionPolicyDecision:
    """One representative #158 decision envelope with per-surface outputs."""
    return AdmissionPolicyDecision(
        schema_version="engram.admission-assessment.v2",
        profile_key="risk_aware_shadow_v1",
        policy_version="risk-aware-shadow-v1",
        policy_config_digest="sha256:" + "a" * 64,
        decision_hash="sha256:" + "b" * 64,
        risk_state="low",
        epistemic_state="supported",
        retention_state="retain",
        effective_assessment_refs=(
            {"assessment_id": str(uuid4()), "purpose": "combined", "canonical_hash": "sha256:x"},
        ),
        highest_admission_tier="semantic_governed" if governed == "allow" else "none",
        surface_decisions={
            "semantic_exploratory": exploratory,
            "semantic_governed": governed,
            "startup": startup,
        },
        blocker_codes=(),
        reason_codes=("governed_evidence_qualified",),
        next_actions=("none",),
        observation_window_hours=0,
        eligible_at=None,
        next_evaluation_at=None,
    )


def _current(decision: AdmissionPolicyDecision, **divergence: Any) -> _FakeResolution:
    """A ``current`` resolution: a row whose identity matches the decision.

    ``divergence`` kwargs forward to :func:`_persisted_row` to build the
    non-current shapes (a stale hash, a mismatched digest, an unsupported
    schema) while keeping the status vocabulary explicit at the call site.
    """
    return _FakeResolution(
        status="current",
        decision=decision,
        assessment=_persisted_row(decision, **divergence),
    )


# ---- utility ----


def test_utility_is_monotonic_in_importance() -> None:
    low = compute_utility_score(
        importance=0.1, created_at=_NOW, valid_from=_NOW, now=_NOW
    )
    high = compute_utility_score(
        importance=0.9, created_at=_NOW, valid_from=_NOW, now=_NOW
    )
    assert 0.0 <= low < high <= 1.0


def test_utility_decays_with_age_but_stays_nonnegative() -> None:
    fresh = compute_utility_score(
        importance=0.5, created_at=_NOW, valid_from=_NOW, now=_NOW
    )
    old = compute_utility_score(
        importance=0.5,
        created_at=_NOW - timedelta(days=90),
        valid_from=_NOW - timedelta(days=90),
        now=_NOW,
    )
    assert fresh > old >= 0.0


def test_utility_ignores_epistemic_inputs() -> None:
    """source_trust / memory_confidence / human_verified are epistemic-state
    inputs, not utility inputs — the signature must not even accept them, and
    recall_count (exposure) must never enter utility."""
    import inspect

    params = inspect.signature(compute_utility_score).parameters
    assert "source_trust" not in params
    assert "memory_confidence" not in params
    assert "human_verified" not in params
    assert "recall_count" not in params


# ---- epistemic state ----


def test_epistemic_state_matrix() -> None:
    assert (
        derive_epistemic_state(
            review_status="proposed",
            human_verified=False,
            conflict_resolution_status=None,
        )
        == "unknown"
    )
    # Even a human-verified proposal is unadmitted evidence.
    assert (
        derive_epistemic_state(
            review_status="proposed",
            human_verified=True,
            conflict_resolution_status=None,
        )
        == "unknown"
    )
    assert (
        derive_epistemic_state(
            review_status="disputed", human_verified=False, conflict_resolution_status=None
        )
        == "contested"
    )
    assert (
        derive_epistemic_state(
            review_status="active", human_verified=False, conflict_resolution_status="unresolved"
        )
        == "contested"
    )
    assert (
        derive_epistemic_state(
            review_status="active", human_verified=True, conflict_resolution_status=None
        )
        == "supported"
    )
    assert (
        derive_epistemic_state(
            review_status="active", human_verified=False, conflict_resolution_status=None
        )
        == "insufficient_evidence"
    )


def test_importance_never_changes_epistemic_state() -> None:
    import inspect

    # importance is not even an input to the derivation, at any value.
    assert "importance" not in inspect.signature(derive_epistemic_state).parameters
    for _importance in (0.0, 0.5, 1.0):
        assert (
            derive_epistemic_state(
                review_status="active",
                human_verified=False,
                conflict_resolution_status=None,
            )
            == "insufficient_evidence"
        )


# ---- ranking ----


def test_rank_relevance_dominates_utility() -> None:
    """A large relevance gap cannot be flipped by maximum utility."""
    far_but_useful = compute_signal_rank_score(similarity=0.4, utility=1.0)
    near_but_boring = compute_signal_rank_score(similarity=0.9, utility=0.0)
    assert near_but_boring > far_but_useful


def test_rank_utility_orders_equal_relevance() -> None:
    assert compute_signal_rank_score(similarity=0.8, utility=0.9) > compute_signal_rank_score(
        similarity=0.8, utility=0.2
    )


def test_rank_is_deterministic_and_bounded() -> None:
    a = compute_signal_rank_score(similarity=0.77, utility=0.33)
    b = compute_signal_rank_score(similarity=0.77, utility=0.33)
    assert a == b
    assert 0.0 <= a <= 1.0


# ---- admission: the V2 surface gate (issue #186) ----


def test_governed_admits_only_on_current_v2_allow() -> None:
    item = _make_item(review_status="proposed")
    decision = decide_recall_admission(
        item,
        profile=GOVERNED_PROFILE,
        stay_kinds=set(),
        v2_resolution=_current(_v2_decision(governed="allow")),
    )
    assert decision.decision == "admit"
    assert decision.reason_codes == ("admitted_v2_surface_allow",)
    assert decision.surface == "semantic_governed"
    assert decision.surface_decision == "allow"
    assert decision.v2 is not None
    assert decision.v2.resolution_status == "current"
    assert decision.v2.profile_key == "risk_aware_shadow_v1"
    assert decision.v2.fresh is not None
    assert decision.v2.fresh.decision_hash == "sha256:" + "b" * 64
    assert decision.v2.persisted is not None
    assert decision.v2.persisted.decision_hash == "sha256:" + "b" * 64


def test_admitted_item_carries_the_full_safe_v2_binding_block() -> None:
    item = _make_item(review_status="proposed")
    resolution = _current(_v2_decision(governed="allow"))
    decision = decide_recall_admission(
        item, profile=GOVERNED_PROFILE, stay_kinds=set(), v2_resolution=resolution
    )
    payload = decision.payload()
    assert payload["surface"] == "semantic_governed"
    assert payload["surface_decision"] == "allow"
    v2 = payload["v2"]
    assert v2["profile_key"] == "risk_aware_shadow_v1"
    assert v2["resolution_status"] == "current"
    assert v2["surface"] == "semantic_governed"
    assert v2["surface_decision"] == "allow"
    # Persisted identity: what the durable row says about itself.
    persisted = v2["persisted"]
    assert persisted["assessment_id"] == str(resolution.assessment.id)
    assert persisted["schema_version"] == "engram.admission-assessment.v2"
    assert persisted["policy_contract_version"] == "risk-aware-shadow-v1"
    assert persisted["policy_artifact_digest"] == "sha256:" + "a" * 64
    assert persisted["decision_hash"] == "sha256:" + "b" * 64
    # Fresh identity and outcome: what the current policy evaluates.
    fresh = v2["fresh"]
    assert fresh["schema_version"] == "engram.admission-assessment.v2"
    assert fresh["policy_version"] == "risk-aware-shadow-v1"
    assert fresh["policy_artifact_digest"] == "sha256:" + "a" * 64
    assert fresh["decision_hash"] == "sha256:" + "b" * 64
    assert fresh["surface_decision"] == "allow"
    assert fresh["highest_admission_tier"] == "semantic_governed"
    assert fresh["risk_state"] == "low"
    assert fresh["epistemic_state"] == "supported"
    assert fresh["retention_state"] == "retain"
    assert fresh["reason_codes"] == ["governed_evidence_qualified"]
    # The binding is identity + codes only — never content or provider output.
    assert set(v2) == {"profile_key", "resolution_status", "surface", "surface_decision",
                       "persisted", "fresh"}
    assert set(persisted) == {
        "assessment_id",
        "schema_version",
        "policy_contract_version",
        "policy_artifact_digest",
        "decision_hash",
    }
    assert set(fresh) == {
        "schema_version",
        "policy_version",
        "policy_artifact_digest",
        "decision_hash",
        "surface_decision",
        "highest_admission_tier",
        "risk_state",
        "epistemic_state",
        "retention_state",
        "effective_assessment_refs",
        "observation_window_hours",
        "eligible_at",
        "next_evaluation_at",
        "blocker_codes",
        "reason_codes",
        "next_actions",
    }


def test_missing_v2_decision_fails_closed_explicitly() -> None:
    """No resolution at all — the canonical no-V2-row case. Never a fallback
    to review_status=active or low-risk defaults."""
    item = _make_item(review_status="proposed")
    decision = decide_recall_admission(item, profile=GOVERNED_PROFILE, stay_kinds=set())
    assert decision.decision == "withhold"
    assert decision.reason_codes == ("v2_decision_missing",)
    assert decision.v2 is not None
    assert decision.v2.resolution_status == "missing"
    assert decision.v2.assessment_id is None


def test_noncurrent_v2_resolutions_each_withhold_with_their_own_code() -> None:
    item = _make_item(review_status="proposed")
    for status in ("stale", "mismatched", "unsupported"):
        decision = decide_recall_admission(
            item,
            profile=GOVERNED_PROFILE,
            stay_kinds=set(),
            v2_resolution=_FakeResolution(status=status, decision=_v2_decision()),
        )
        assert decision.decision == "withhold", status
        assert decision.reason_codes == (f"v2_decision_{status}",), status
        assert decision.v2 is not None and decision.v2.resolution_status == status


def test_unknown_resolver_status_fails_closed_as_unsupported() -> None:
    item = _make_item(review_status="proposed")
    decision = decide_recall_admission(
        item,
        profile=GOVERNED_PROFILE,
        stay_kinds=set(),
        v2_resolution=_FakeResolution(status="some_future_status", decision=_v2_decision()),
    )
    assert decision.decision == "withhold"
    assert decision.reason_codes == ("v2_decision_unsupported",)


def test_v2_surface_decisions_map_exactly_per_profile() -> None:
    """withhold/review_required/blocked/unknown on the exact surface are
    consumed as-is — distinct outcomes stay distinct, never flattened."""
    item = _make_item(review_status="proposed")
    for surface_value in ("withhold", "review_required", "blocked", "unknown"):
        decision = decide_recall_admission(
            item,
            profile=GOVERNED_PROFILE,
            stay_kinds=set(),
            v2_resolution=_current(_v2_decision(governed=surface_value)),
        )
        assert decision.decision == "withhold", surface_value
        assert decision.reason_codes == (f"v2_surface_{surface_value}",), surface_value
        assert decision.surface_decision == surface_value


def test_active_item_cannot_be_admitted_by_any_v2_output() -> None:
    """review_status='active' is not a positive admission source (issue #186
    test 3): an active item is outside the policy's live-proposal domain and
    withholds as not-live even when a (stale, as it must be) row says allow."""
    item = _make_item(review_status="active")
    for governed in ("allow", "review_required"):
        decision = decide_recall_admission(
            item,
            profile=GOVERNED_PROFILE,
            stay_kinds=set(),
            v2_resolution=_current(_v2_decision(governed=governed)),
        )
        assert decision.decision == "withhold", governed
        assert decision.reason_codes == ("v2_item_not_live",)
    # And with no V2 state at all, active is equally inadmissible.
    decision = decide_recall_admission(item, profile=GOVERNED_PROFILE, stay_kinds=set())
    assert decision.decision == "withhold"


def test_unresolved_conflict_withholds_even_with_v2_allow() -> None:
    item = _make_item(review_status="proposed", conflict_resolution_status="unresolved")
    decision = decide_recall_admission(
        item,
        profile=GOVERNED_PROFILE,
        stay_kinds=set(),
        v2_resolution=_current(_v2_decision(governed="allow")),
    )
    assert decision.decision == "withhold"
    assert decision.reason_codes == ("conflict_unresolved",)


def test_governed_and_exploratory_consume_their_own_exact_surfaces() -> None:
    """One decision, two surfaces: exploratory may allow exactly what governed
    routes to review — never by inheriting governed semantics or vice versa."""
    item = _make_item(review_status="proposed")
    resolution = _current(_v2_decision(governed="review_required", exploratory="allow"))
    governed = decide_recall_admission(
        item, profile=GOVERNED_PROFILE, stay_kinds=set(), v2_resolution=resolution
    )
    exploratory = decide_recall_admission(
        item, profile=EXPLORATORY_PROFILE, stay_kinds=set(), v2_resolution=resolution
    )
    assert governed.decision == "withhold"
    assert governed.reason_codes == ("v2_surface_review_required",)
    assert exploratory.decision == "admit"
    assert exploratory.surface == "semantic_exploratory"
    assert exploratory.surface_decision == "allow"


def test_blocked_surface_withholds_on_both_candidate_profiles() -> None:
    item = _make_item(review_status="proposed")
    resolution = _current(_v2_decision(governed="blocked", exploratory="blocked"))
    for profile in (GOVERNED_PROFILE, EXPLORATORY_PROFILE):
        decision = decide_recall_admission(
            item, profile=profile, stay_kinds=set(), v2_resolution=resolution
        )
        assert decision.decision == "withhold"
        assert decision.reason_codes == ("v2_surface_blocked",)


def test_path_a_binding_never_fabricates_v2_authority() -> None:
    """A current #159 Path-A assessment cannot admit anything on its own
    (issue #186 test 7) — without a current V2 decision the item withholds."""
    item = _make_item(review_status="proposed")
    binding = AdmissionAssessmentBinding(
        assessment_id=str(uuid4()), status="current", outcome="admitted"
    )
    decision = decide_recall_admission(
        item, profile=GOVERNED_PROFILE, stay_kinds=set(), assessment=binding
    )
    assert decision.decision == "withhold"
    assert decision.reason_codes == ("v2_decision_missing",)


def test_path_a_blocked_and_stale_bindings_withhold_despite_v2_allow() -> None:
    """Recall-local defense in depth can only withhold: a #159 blocked
    outcome (every profile) or stale projection (strict profiles) wins over a
    V2 allow — the local boundaries stay stronger than any V2 decision."""
    blocked = AdmissionAssessmentBinding(
        assessment_id=str(uuid4()), status="current", outcome="blocked"
    )
    stale = AdmissionAssessmentBinding(
        assessment_id=str(uuid4()), status="stale", outcome="admitted"
    )
    item = _make_item(review_status="proposed")
    allow = _current(_v2_decision(governed="allow", exploratory="allow"))
    for profile in (GOVERNED_PROFILE, EXPLORATORY_PROFILE):
        decision = decide_recall_admission(
            item, profile=profile, stay_kinds=set(), assessment=blocked, v2_resolution=allow
        )
        assert decision.decision == "withhold"
        assert decision.reason_codes == ("admission_blocked",)
    governed_stale = decide_recall_admission(
        item, profile=GOVERNED_PROFILE, stay_kinds=set(), assessment=stale, v2_resolution=allow
    )
    assert governed_stale.decision == "withhold"
    assert governed_stale.reason_codes == ("admission_assessment_stale",)


def test_local_withhold_preserves_the_resolved_v2_binding() -> None:
    """BLOCKER 1 regression (#186 review): when a recall-local #159 rule wins
    and withholds, the result still exposes the exact V2 state it disagreed
    with — resolution ``current``, the exact surface decision ``allow``, the
    persisted/fresh identity — instead of misreporting the V2 state as
    ``missing``. Local precedence for the final decision and primary reason
    code is unchanged; only the V2 visibility is preserved."""
    blocked = AdmissionAssessmentBinding(
        assessment_id=str(uuid4()), status="current", outcome="blocked"
    )
    stale = AdmissionAssessmentBinding(
        assessment_id=str(uuid4()), status="stale", outcome="admitted"
    )
    item = _make_item(review_status="proposed")

    # 1. Current V2 allow + current #159 blocked: withholds everywhere, but
    #    the binding shows exactly what V2 said.
    allow = _current(_v2_decision(governed="allow", exploratory="allow"))
    for profile in (GOVERNED_PROFILE, EXPLORATORY_PROFILE):
        decision = decide_recall_admission(
            item, profile=profile, stay_kinds=set(), assessment=blocked, v2_resolution=allow
        )
        assert decision.decision == "withhold", profile.key
        assert decision.reason_codes == ("admission_blocked",), profile.key
        assert decision.surface == profile.v2_surface
        assert decision.surface_decision == "allow"
        assert decision.assessment_id == blocked.assessment_id
        assert decision.assessment_outcome == "blocked"
        assert decision.v2 is not None
        assert decision.v2.resolution_status == "current"
        assert decision.v2.surface_decision == "allow"
        assert decision.v2.persisted is not None
        assert decision.v2.fresh is not None
        assert decision.v2.fresh.surface_decision == "allow"
        payload = decision.payload()
        assert payload["v2"]["resolution_status"] == "current"
        assert payload["v2"]["fresh"]["surface_decision"] == "allow"
        assert payload["v2"]["persisted"]["decision_hash"] == payload["v2"]["fresh"][
            "decision_hash"
        ]

    # 2. Current governed V2 allow + stale #159 binding: governed is strict,
    #    so the stale durable outcome withholds — and the binding still shows
    #    the current V2 allow it withheld against.
    governed_stale = decide_recall_admission(
        item,
        profile=GOVERNED_PROFILE,
        stay_kinds=set(),
        assessment=stale,
        v2_resolution=_current(_v2_decision(governed="allow")),
    )
    assert governed_stale.decision == "withhold"
    assert governed_stale.reason_codes == ("admission_assessment_stale",)
    assert governed_stale.assessment_status == "stale"
    assert governed_stale.v2 is not None
    assert governed_stale.v2.resolution_status == "current"
    assert governed_stale.v2.surface_decision == "allow"


def test_local_lifecycle_withholds_also_preserve_the_v2_binding() -> None:
    """The same preservation holds for the other recall-local defenses: a
    not-live item (or an unresolved conflict) withholds with its exact V2
    state visible, not as absent V2 state."""
    allow = _current(_v2_decision(governed="allow", exploratory="allow"))
    active = decide_recall_admission(
        _make_item(review_status="active"),
        profile=GOVERNED_PROFILE,
        stay_kinds=set(),
        v2_resolution=allow,
    )
    assert active.decision == "withhold"
    assert active.reason_codes == ("v2_item_not_live",)
    assert active.v2 is not None
    assert active.v2.resolution_status == "current"
    assert active.v2.surface_decision == "allow"
    conflicted = decide_recall_admission(
        _make_item(review_status="proposed", conflict_resolution_status="unresolved"),
        profile=EXPLORATORY_PROFILE,
        stay_kinds=set(),
        v2_resolution=allow,
    )
    assert conflicted.decision == "withhold"
    assert conflicted.reason_codes == ("conflict_unresolved",)
    assert conflicted.v2 is not None
    assert conflicted.v2.resolution_status == "current"
    assert conflicted.v2.surface_decision == "allow"


def test_exploratory_admits_with_v2_allow_and_marks_stale_path_a_binding() -> None:
    """Non-strict profiles carry the stale Path-A snapshot as a mark —
    reported, never trusted as authorization (BLOCKER 1 scenario 3: the
    stale #159 state stays a mark, and the V2 binding stays current, while
    the item still admits exactly as the surface decision says)."""
    stale = AdmissionAssessmentBinding(
        assessment_id=str(uuid4()), status="stale", outcome="admitted"
    )
    item = _make_item(review_status="proposed")
    decision = decide_recall_admission(
        item,
        profile=EXPLORATORY_PROFILE,
        stay_kinds=set(),
        assessment=stale,
        v2_resolution=_current(_v2_decision(exploratory="allow")),
    )
    assert decision.decision == "admit"
    assert decision.reason_codes == ("admitted_v2_surface_allow", "admission_assessment_stale")
    # The legacy payload fields keep their #159 meaning; V2 identity is separate.
    assert decision.assessment_id == stale.assessment_id
    assert decision.assessment_status == "stale"
    assert decision.v2 is not None and decision.v2.resolution_status == "current"
    assert decision.v2.surface_decision == "allow"
    payload = decision.payload()
    assert payload["v2"]["resolution_status"] == "current"
    assert payload["v2"]["fresh"]["surface_decision"] == "allow"


def test_local_gate_helper_reports_only_withholds() -> None:
    item = _make_item(review_status="proposed")
    assert v2_local_gate_withhold_reason(item, profile=GOVERNED_PROFILE, assessment=None) is None
    assert (
        v2_local_gate_withhold_reason(
            _make_item(review_status="active"), profile=GOVERNED_PROFILE, assessment=None
        )
        == "v2_item_not_live"
    )
    assert (
        v2_local_gate_withhold_reason(
            _make_item(review_status="proposed", conflict_resolution_status="unresolved"),
            profile=GOVERNED_PROFILE,
            assessment=None,
        )
        == "conflict_unresolved"
    )
    blocked = AdmissionAssessmentBinding(
        assessment_id=str(uuid4()), status="current", outcome="blocked"
    )
    assert (
        v2_local_gate_withhold_reason(item, profile=GOVERNED_PROFILE, assessment=blocked)
        == "admission_blocked"
    )


def test_missing_binding_builder_is_explicit_not_silent() -> None:
    binding = build_v2_surface_binding(None, surface="semantic_governed")
    payload = binding.payload()
    assert payload["resolution_status"] == "missing"
    assert payload["surface"] == "semantic_governed"
    assert payload["persisted"] is None
    assert payload["fresh"] is None
    assert binding.assessment_id is None
    assert payload["surface_decision"] is None
    # Even with no row, the binding names the policy profile that would own
    # the decision — a withheld item never reads as "no policy ran".
    assert payload["profile_key"] == "risk_aware_shadow_v1"


def test_binding_separates_persisted_from_fresh_identity_per_resolution_status() -> None:
    """BLOCKER 3 regression (#186 review): the serialized binding never mixes
    the persisted row's identity with the fresh evaluation's. Each status
    keeps both sides individually visible and mechanically comparable — for
    ``current`` they agree; for every divergence the differing fields stay
    distinct instead of being silently collapsed."""
    decision = _v2_decision(governed="allow")
    item = _make_item(review_status="proposed")

    def _payload(resolution: _FakeResolution | None) -> dict[str, Any]:
        decided = decide_recall_admission(
            item, profile=GOVERNED_PROFILE, stay_kinds=set(), v2_resolution=resolution
        )
        assert decided.decision == "withhold" or resolution is not None
        return decided.payload()["v2"]

    # current: persisted and fresh identities agree.
    v2 = _payload(_current(decision))
    assert v2["resolution_status"] == "current"
    assert v2["persisted"]["schema_version"] == v2["fresh"]["schema_version"]
    assert v2["persisted"]["policy_artifact_digest"] == v2["fresh"]["policy_artifact_digest"]
    assert v2["persisted"]["decision_hash"] == v2["fresh"]["decision_hash"]

    # missing (a row-less resolution): no persisted identity; the fresh
    # evaluation still describes what the current policy produces.
    v2 = _payload(_FakeResolution(status="missing", decision=decision, assessment=None))
    assert v2["resolution_status"] == "missing"
    assert v2["persisted"] is None
    assert v2["fresh"]["decision_hash"] == decision.decision_hash
    assert v2["fresh"]["surface_decision"] == "allow"

    # stale: the differing decision hashes are both visible.
    v2 = _payload(
        _FakeResolution(
            status="stale",
            decision=decision,
            assessment=_persisted_row(decision, decision_hash="sha256:" + "c" * 64),
        )
    )
    assert v2["resolution_status"] == "stale"
    assert v2["persisted"]["decision_hash"] == "sha256:" + "c" * 64
    assert v2["fresh"]["decision_hash"] == "sha256:" + "b" * 64
    assert v2["persisted"]["decision_hash"] != v2["fresh"]["decision_hash"]

    # mismatched: the persisted and current artifact digests are both visible.
    v2 = _payload(
        _FakeResolution(
            status="mismatched",
            decision=decision,
            assessment=_persisted_row(decision, policy_artifact_digest="sha256:" + "d" * 64),
        )
    )
    assert v2["resolution_status"] == "mismatched"
    assert v2["persisted"]["policy_artifact_digest"] == "sha256:" + "d" * 64
    assert v2["fresh"]["policy_artifact_digest"] == "sha256:" + "a" * 64
    assert v2["persisted"]["policy_artifact_digest"] != v2["fresh"]["policy_artifact_digest"]

    # unsupported: the persisted row's non-V2 schema stays visible while the
    # fresh evaluation still identifies the current V2 schema.
    v2 = _payload(
        _FakeResolution(
            status="unsupported",
            decision=decision,
            assessment=_persisted_row(
                decision, schema_version="engram.admission-assessment.v1"
            ),
        )
    )
    assert v2["resolution_status"] == "unsupported"
    assert v2["persisted"]["schema_version"] == "engram.admission-assessment.v1"
    assert v2["fresh"]["schema_version"] == "engram.admission-assessment.v2"


def test_v2_vocabulary_cannot_drift_from_the_resolver() -> None:
    """The gate's status tuple and profile key mirror the #158 resolver's
    canonical vocabulary; a change on either side must land on both."""
    from typing import get_args

    from engram.admission_shadow import SHADOW_PROFILE_KEY, V2ResolutionStatus
    from engram.recall_signals import V2_ADMISSION_PROFILE_KEY, V2_RESOLUTION_STATUSES

    assert set(V2_RESOLUTION_STATUSES) == set(get_args(V2ResolutionStatus))
    assert V2_ADMISSION_PROFILE_KEY == SHADOW_PROFILE_KEY


# ---- withheld-candidate diagnostics (issue #186 review blockers 1 + 2) ------


def _diagnostic(
    item: MemoryItem,
    *,
    profile: Any,
    assessment: AdmissionAssessmentBinding | None,
    resolution: _FakeResolution | None,
) -> dict[str, Any]:
    from engram.recall import _admission_diagnostic

    decision = decide_recall_admission(
        item, profile=profile, stay_kinds=set(), assessment=assessment, v2_resolution=resolution
    )
    return _admission_diagnostic(
        item, profile=profile, decision=decision, assessment=assessment
    )


def test_withheld_diagnostic_reports_the_blocked_local_v2_disagreement() -> None:
    """BLOCKER 1 regression at the diagnostic layer: a #159-blocked withhold
    over a current V2 allow reports resolution ``current``, surface decision
    ``allow``, and ``gates_disagree`` true — never a fabricated ``missing``
    with a silent agreement."""
    blocked = AdmissionAssessmentBinding(
        assessment_id=str(uuid4()), status="current", outcome="blocked"
    )
    item = _make_item(review_status="proposed")
    resolution = _current(_v2_decision(governed="allow", exploratory="allow"))
    for profile in (GOVERNED_PROFILE, EXPLORATORY_PROFILE):
        diagnostic = _diagnostic(
            item, profile=profile, assessment=blocked, resolution=resolution
        )
        assert diagnostic["decision"] == "withhold", profile.key
        assert diagnostic["reason_codes"] == ["admission_blocked"], profile.key
        assert diagnostic["v2_resolution_status"] == "current", profile.key
        assert diagnostic["v2_surface_decision"] == "allow", profile.key
        assert diagnostic["gates_disagree"] is True, profile.key


def test_withheld_diagnostic_reports_the_governed_stale_disagreement() -> None:
    """BLOCKER 1 regression, scenario 2: a stale #159 binding withholding a
    governed candidate over a current V2 allow is reported as the local/V2
    disagreement it is."""
    stale = AdmissionAssessmentBinding(
        assessment_id=str(uuid4()), status="stale", outcome="admitted"
    )
    diagnostic = _diagnostic(
        _make_item(review_status="proposed"),
        profile=GOVERNED_PROFILE,
        assessment=stale,
        resolution=_current(_v2_decision(governed="allow")),
    )
    assert diagnostic["decision"] == "withhold"
    assert diagnostic["reason_codes"] == ["admission_assessment_stale"]
    assert diagnostic["v2_resolution_status"] == "current"
    assert diagnostic["v2_surface_decision"] == "allow"
    assert diagnostic["gates_disagree"] is True


def test_withheld_diagnostic_distinguishes_unavailability_from_disagreement() -> None:
    """A fail-closed V2 unavailability (no row, or a non-current row) is not
    a gate disagreement — ``gates_disagree`` stays false there."""
    item = _make_item(review_status="proposed")
    missing = _diagnostic(item, profile=GOVERNED_PROFILE, assessment=None, resolution=None)
    assert missing["reason_codes"] == ["v2_decision_missing"]
    assert missing["v2_resolution_status"] == "missing"
    assert missing["v2_surface_decision"] is None
    assert missing["gates_disagree"] is False

    stale_row = _FakeResolution(
        status="stale",
        decision=_v2_decision(governed="allow"),
        assessment=_persisted_row(_v2_decision(governed="allow"), decision_hash="sha256:c"),
    )
    unavailable = _diagnostic(
        item, profile=GOVERNED_PROFILE, assessment=None, resolution=stale_row
    )
    assert unavailable["v2_resolution_status"] == "stale"
    assert unavailable["gates_disagree"] is False


def test_withheld_diagnostic_carries_the_full_safe_v2_binding() -> None:
    """BLOCKER 2 regression: a withheld candidate's diagnostic carries the
    same bounded safe binding an admitted item exposes — persisted and fresh
    identity, exact surface, and the bounded state/code sets — enough to
    identify the exact V2 decision without hidden implementation state."""
    item = _make_item(review_status="proposed")
    resolution = _current(_v2_decision(governed="review_required", exploratory="allow"))
    diagnostic = _diagnostic(
        item, profile=GOVERNED_PROFILE, assessment=None, resolution=resolution
    )
    assert diagnostic["reason_codes"] == ["v2_surface_review_required"]
    v2 = diagnostic["v2"]
    assert v2["profile_key"] == "risk_aware_shadow_v1"
    assert v2["resolution_status"] == "current"
    assert v2["surface"] == "semantic_governed"
    assert v2["surface_decision"] == "review_required"
    assert v2["persisted"]["assessment_id"] == str(resolution.assessment.id)
    assert v2["persisted"]["schema_version"] == "engram.admission-assessment.v2"
    assert v2["persisted"]["policy_contract_version"] == "risk-aware-shadow-v1"
    assert v2["persisted"]["policy_artifact_digest"] == "sha256:" + "a" * 64
    assert v2["persisted"]["decision_hash"] == v2["fresh"]["decision_hash"]
    assert v2["fresh"]["policy_version"] == "risk-aware-shadow-v1"
    assert v2["fresh"]["highest_admission_tier"] == "none"
    assert v2["fresh"]["risk_state"] == "low"
    assert v2["fresh"]["epistemic_state"] == "supported"
    assert v2["fresh"]["retention_state"] == "retain"
    assert v2["fresh"]["effective_assessment_refs"][0]["purpose"] == "combined"
    assert v2["fresh"]["observation_window_hours"] == 0
    assert v2["fresh"]["blocker_codes"] == []
    assert v2["fresh"]["reason_codes"] == ["governed_evidence_qualified"]
    assert v2["fresh"]["next_actions"] == ["none"]
    # Same contract as the admitted item's block — identity and codes only.
    assert set(v2) == {"profile_key", "resolution_status", "surface", "surface_decision",
                       "persisted", "fresh"}


def test_withheld_diagnostic_binding_is_content_free() -> None:
    """The diagnostic and its binding never carry item content, provider
    output, or free-form evaluation text — only bounded identity and codes."""
    item = _make_item(review_status="proposed", content="secret-ish item content")
    diagnostic = _diagnostic(
        item,
        profile=GOVERNED_PROFILE,
        assessment=None,
        resolution=_current(_v2_decision(governed="blocked")),
    )
    serialized = str(diagnostic)
    assert "secret-ish item content" not in serialized


# ---- admission invariants ----


def test_admission_ignores_relevance_and_utility_inputs() -> None:
    """Admission reads governance state only. The signature must not accept
    similarity/importance — popularity can never buy admission."""
    import inspect

    params = inspect.signature(decide_recall_admission).parameters
    assert "similarity" not in params
    assert "importance" not in params
    assert "recall_count" not in params


def test_importance_never_changes_admission() -> None:
    resolution = _current(_v2_decision(governed="allow", exploratory="allow"))
    for importance in (0.0, 1.0):
        proposed = _make_item(review_status="proposed", importance=importance)
        assert (
            decide_recall_admission(
                proposed,
                profile=GOVERNED_PROFILE,
                stay_kinds=set(),
                v2_resolution=resolution,
            ).decision
            == "admit"
        )
        active = _make_item(review_status="active", importance=importance)
        assert (
            decide_recall_admission(
                active,
                profile=GOVERNED_PROFILE,
                stay_kinds=set(),
                v2_resolution=resolution,
            ).decision
            == "withhold"
        )


# ---- structured warnings + item fields ----


def test_warning_codes_are_machine_readable_and_mirror_warnings() -> None:
    codes = structured_warning_codes(
        review_status="proposed", conflict_resolution_status="unresolved"
    )
    assert "unreviewed" in codes
    assert "conflict_unresolved" in codes
    # disputed implies the conflict code even without an explicit status
    codes = structured_warning_codes(review_status="disputed", conflict_resolution_status=None)
    assert "conflict_unresolved" in codes
    assert "disputed" in codes


def test_signal_item_fields_expose_separate_signals_with_versions() -> None:
    item = _make_item(review_status="proposed", importance=0.8)
    decision = decide_recall_admission(
        item,
        profile=EXPLORATORY_PROFILE,
        stay_kinds=set(),
        v2_resolution=_current(_v2_decision(exploratory="allow")),
    )
    fields = signal_item_fields(
        item, decision=decision, similarity=0.9, now=_NOW
    )
    assert fields["signals_version"] == SIGNALS_VERSION
    assert fields["relevance_score"] == 0.9
    assert 0.0 <= fields["utility_score"] <= 1.0
    assert fields["epistemic_state"] == "unknown"
    assert "evidence_unknown" in fields["warning_codes"]
    assert fields["admission"]["profile"] == "exploratory"
    assert fields["admission"]["decision"] == "admit"
    assert fields["admission"]["policy_version"] == RECALL_ADMISSION_POLICY_VERSION
    assert fields["admission"]["v2"]["resolution_status"] == "current"
    assert "unreviewed" in fields["warnings"]
    # The rank score is reproducible from its published inputs.
    assert fields["score"] == compute_signal_rank_score(
        similarity=0.9, utility=fields["utility_score"]
    )


def test_signal_item_fields_never_expose_a_blended_trust_score() -> None:
    decision = RecallAdmissionDecision(
        profile="governed", decision="admit", reason_codes=("admitted_v2_surface_allow",)
    )
    fields = signal_item_fields(_make_item(), decision=decision, similarity=0.5, now=_NOW)
    assert "trust_score" not in fields


# ---- memory_confidence is not epistemic confidence ----


def test_memory_confidence_is_not_even_a_signal_input() -> None:
    """``memory_confidence`` is the historical source-policy prior for
    automated captures — never epistemic confidence. No separated-signal
    function may accept it, so it can never move epistemic state, admission,
    or a warning."""
    import inspect

    for fn in (compute_utility_score, derive_epistemic_state, structured_warning_codes):
        assert "memory_confidence" not in inspect.signature(fn).parameters
    assert "memory_confidence" not in inspect.signature(decide_recall_admission).parameters


def test_changing_memory_confidence_changes_no_signal_output() -> None:
    """Two items identical except ``memory_confidence`` produce byte-identical
    separated-signal output — most importantly no generic warning that a
    caller could reasonably read as a factual/epistemic confidence claim
    (the ``low_confidence`` conflation #160 removes)."""
    resolution = _current(_v2_decision(governed="allow"))
    low = signal_item_fields(
        _make_item(review_status="proposed", memory_confidence=0.1),
        decision=decide_recall_admission(
            _make_item(review_status="proposed", memory_confidence=0.1),
            profile=GOVERNED_PROFILE,
            stay_kinds=set(),
            v2_resolution=resolution,
        ),
        similarity=0.8,
        now=_NOW,
    )
    high = signal_item_fields(
        _make_item(review_status="proposed", memory_confidence=0.95),
        decision=decide_recall_admission(
            _make_item(review_status="proposed", memory_confidence=0.95),
            profile=GOVERNED_PROFILE,
            stay_kinds=set(),
            v2_resolution=resolution,
        ),
        similarity=0.8,
        now=_NOW,
    )
    assert low == high
    assert low["epistemic_state"] == "unknown"
    assert "low_confidence" not in low["warning_codes"]
    assert "low confidence" not in [w.lower() for w in low["warnings"]]
    # The misleading code is gone from the vocabulary entirely.
    from engram.recall_signals import _WARNING_TEXT

    assert "low_confidence" not in _WARNING_TEXT


# ---- exposure counters never feed signals ----


def test_exposure_counters_change_no_signal_output() -> None:
    """recall_count / last_recalled_at / startup_recall_count are exposure
    telemetry only — a heavily-served item must be indistinguishable from a
    never-served one in utility, epistemic state, admission, and warnings
    (feedback-loop safeguard)."""
    resolution = _current(_v2_decision(governed="allow"))
    fresh = _make_item(
        review_status="proposed", recall_count=0, startup_recall_count=0, last_recalled_at=None
    )
    hot = _make_item(
        review_status="proposed",
        recall_count=10_000,
        startup_recall_count=10_000,
        last_recalled_at=_NOW,
    )
    decision_fresh = decide_recall_admission(
        fresh, profile=GOVERNED_PROFILE, stay_kinds=set(), v2_resolution=resolution
    )
    decision_hot = decide_recall_admission(
        hot, profile=GOVERNED_PROFILE, stay_kinds=set(), v2_resolution=resolution
    )
    assert decision_fresh == decision_hot
    fields_fresh = signal_item_fields(fresh, decision=decision_fresh, similarity=0.7, now=_NOW)
    fields_hot = signal_item_fields(hot, decision=decision_hot, similarity=0.7, now=_NOW)
    assert fields_fresh == fields_hot


# ---- importance orders but never admits ----


def test_importance_changes_utility_and_rank_but_not_admission_or_epistemic() -> None:
    """Importance is the caller's explicit priority: among admitted items it
    moves utility and therefore rank, but it can never change the admission
    decision or the epistemic state at any value."""
    resolution = _current(_v2_decision(governed="allow", exploratory="allow"))
    low_item = _make_item(review_status="proposed", importance=0.0)
    high_item = _make_item(review_status="proposed", importance=1.0)
    for item in (low_item, high_item):
        governed = decide_recall_admission(
            item, profile=GOVERNED_PROFILE, stay_kinds=set(), v2_resolution=resolution
        )
        assert governed.decision == "admit"
        assert governed.reason_codes == ("admitted_v2_surface_allow",)
        exploratory = decide_recall_admission(
            item, profile=EXPLORATORY_PROFILE, stay_kinds=set(), v2_resolution=resolution
        )
        assert exploratory.decision == "admit"

    low_fields = signal_item_fields(
        low_item,
        decision=RecallAdmissionDecision(profile="exploratory", decision="admit", reason_codes=()),
        similarity=0.8,
        now=_NOW,
    )
    high_fields = signal_item_fields(
        high_item,
        decision=RecallAdmissionDecision(profile="exploratory", decision="admit", reason_codes=()),
        similarity=0.8,
        now=_NOW,
    )
    assert high_fields["utility_score"] > low_fields["utility_score"]
    assert high_fields["score"] > low_fields["score"]
