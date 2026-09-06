"""Public response contracts for admission assessments (issue #159).

Two authority tiers, deliberately separate models rather than one model with
optional fields: :class:`AdmissionAssessmentView` is what an ordinary item
reader may see, and :class:`AdmissionAssessmentDetail` adds the normalized
decision inputs and evidence references that need review/admin authority. A
reader who lacks that authority never receives the detail fields at all — they
are absent from the schema, not merely nulled out.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from engram.admission_assessment import (
    AdmissionMode,
    AdmissionNextAction,
    AdmissionOutcome,
    AdmissionProjectionStatus,
)

AdmissionRiskState = Literal["low", "medium", "high", "unknown", "not_applicable"]
AdmissionEpistemicState = Literal[
    "supported", "contested", "insufficient_evidence", "unknown", "not_applicable"
]
AdmissionRetentionState = Literal["retain", "transient", "noise", "uncertain", "unknown"]
AdmissionTier = Literal["none", "semantic_exploratory", "semantic_governed", "startup"]
AdmissionSurfaceDecision = Literal[
    "allow", "withhold", "review_required", "blocked", "unknown"
]


class AdmissionAssessmentView(BaseModel):
    """One recorded decision, safe for any reader eligible to read the item.

    Carries no normalized decision inputs, no provider or classifier
    internals, and no conflict candidate identity.
    """

    assessment_id: UUID
    schema_version: Literal[
        "engram.admission-assessment.v1", "engram.admission-assessment.v2"
    ]
    mode: AdmissionMode
    policy_profile_key: str
    policy_contract_version: str
    policy_config_digest: str
    input_digest: str
    item_content_hash: str
    selected_basis: Literal["legacy_confidence", "retention_evidence"] | None
    outcome: AdmissionOutcome
    blocker_codes: list[str]
    reason_codes: list[str]
    next_actions: list[AdmissionNextAction]
    conflict_recheck_status: str
    cooling_period_start: datetime | None
    eligible_at: datetime | None
    next_evaluation_at: datetime | None
    decision_hash: str
    evaluated_at: datetime
    trigger_type: str
    trigger_id: str
    invocation_source: str
    evaluation_id: UUID | None
    prior_assessment_id: UUID | None
    linked_item_event_id: UUID | None
    risk_state: AdmissionRiskState | None = None
    epistemic_state: AdmissionEpistemicState | None = None
    retention_state: AdmissionRetentionState | None = None
    highest_admission_tier: AdmissionTier | None = None
    surface_decisions: dict[str, AdmissionSurfaceDecision] | None = None
    observation_window_hours: int | None = None


class AdmissionAssessmentDetail(AdmissionAssessmentView):
    """Reviewer/debug view: adds normalized inputs and diagnostic evidence refs.

    ``available_memory_assessment_refs`` are #157 evidence-assessment
    identities observed at evaluation time. They are diagnostic in v1 and did
    not influence the outcome; an empty list means none was visible, never
    that none exists or that the evidence was clean.
    """

    decision_inputs: dict[str, Any] = Field(default_factory=dict)
    available_memory_assessment_refs: list[dict[str, Any]] = Field(default_factory=list)
    effective_memory_assessment_refs: list[dict[str, Any]] = Field(default_factory=list)
    classification_run_id: UUID | None = None
    job_id: UUID | None = None
    actor_principal_id: UUID | None = None


class AdmissionAssessmentCurrentResponse(BaseModel):
    """The resolved current admission state for one item and policy profile.

    ``status`` is resolved by comparing the pointed decision's input/policy
    digests against present state, never by a stored flag, so history is never
    rewritten to mark it stale. ``missing`` (nothing recorded) stays distinct
    from an ``unknown`` outcome (policy looked and could not interpret safely).
    """

    item_id: UUID
    policy_profile_key: str
    policy_contract_version: str
    status: AdmissionProjectionStatus | Literal["missing"]
    capture_enabled: bool
    current_input_digest: str
    current_policy_config_digest: str
    assessment: AdmissionAssessmentView | None = None


class AdmissionAssessmentHistory(BaseModel):
    """Bounded, newest-first immutable history for one item."""

    item_id: UUID
    policy_profile_key: Literal["path_a_compat", "risk_aware_shadow_v1"]
    assessments: list[AdmissionAssessmentView]
    next_before: UUID | None = None


class AdmissionReevaluateRequest(BaseModel):
    """A bounded reevaluation request.

    It reuses the existing #155 ``promotion.evaluate`` orchestration rather
    than adding a job type: ``trigger_id`` becomes that job's dedupe identity,
    so replaying the same request while one is pending returns the same job
    instead of queuing another.
    """

    model_config = {"extra": "forbid"}

    reason: Literal["operator_request", "policy_changed", "provenance_changed"] = (
        "operator_request"
    )
    trigger_id: str = Field(min_length=1, max_length=200)


class AdmissionReevaluateResponse(BaseModel):
    item_id: UUID
    job_id: UUID
    trigger_type: str
    trigger_id: str
    policy_profile_key: str


class AdmissionShadowSimulationRequest(BaseModel):
    """An explicit shadow-history write is never the default."""

    model_config = {"extra": "forbid"}

    persist_shadow: bool = False
    trigger_id: str | None = Field(default=None, min_length=1, max_length=200)


class AdmissionShadowSimulationView(BaseModel):
    """Safe normalized result for one V2 shadow comparison."""

    item_id: UUID
    path_a_compat: dict[str, Any]
    shadow_profile_key: Literal["risk_aware_shadow_v1"]
    shadow_policy_version: str
    shadow_policy_digest: str
    shadow_decision_hash: str
    highest_admission_tier: str
    surface_decisions: dict[str, str]
    risk_state: str
    epistemic_state: str
    retention_state: str
    effective_memory_assessment_refs: list[dict[str, str]] = Field(default_factory=list)
    blocker_codes: list[str]
    reason_codes: list[str]
    next_actions: list[str]
    observation_window_hours: int | None
    eligible_at: datetime | None
    next_evaluation_at: datetime | None
    surface_differs_from_path_a: dict[str, bool]
    persisted_shadow_assessment_id: UUID | None = None


class AdmissionShadowSimulationPageRequest(AdmissionShadowSimulationRequest):
    limit: int = Field(default=50, ge=1, le=100)
    after: UUID | None = None
    workspace_id: UUID | None = None


class AdmissionShadowSimulationPage(BaseModel):
    profile_key: Literal["risk_aware_shadow_v1"]
    scanned_count: int
    returned_count: int
    next_after: UUID | None
    changed_admissions: int
    changed_exclusions: int
    changed_review_routing: int
    strata_counts: dict[str, dict[str, int]]
    results: list[AdmissionShadowSimulationView]


__all__ = [
    "AdmissionAssessmentCurrentResponse",
    "AdmissionAssessmentDetail",
    "AdmissionAssessmentHistory",
    "AdmissionAssessmentView",
    "AdmissionReevaluateRequest",
    "AdmissionReevaluateResponse",
    "AdmissionShadowSimulationPage",
    "AdmissionShadowSimulationPageRequest",
    "AdmissionShadowSimulationRequest",
    "AdmissionShadowSimulationView",
]
