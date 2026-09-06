"""Versioned assessment contracts. Retention measures usefulness, not truth."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

Purpose = Literal["taxonomy", "retention", "epistemic", "risk", "combined"]
Reason = Literal[
    "provider_recovery",
    "model_upgrade",
    "provenance_added",
    "human_correction",
    "policy_rollout",
    "manual",
]
EpistemicState = Literal[
    "supported",
    "contested",
    "insufficient_evidence",
    "unknown",
    "not_applicable",
]
Risk = Literal["low", "moderate", "high", "unknown"]
AssertionMode = Literal[
    "direct_statement",
    "tool_observation",
    "quoted_source",
    "derived_summary",
    "inference",
    "unknown",
]
Origin = Literal["user", "assistant", "system", "tool", "unknown"]
JobStatus = Literal["pending", "running", "succeeded", "failed", "dead", "cancelled"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class AssessmentContract(StrictModel):
    schema_version: Literal["engram.assessment.v1"] = "engram.assessment.v1"
    prompt_version: Literal["engram.assess.1"] = "engram.assess.1"
    code_version: Literal["assessment-engine-v1"] = "assessment-engine-v1"
    provider: str = Field(max_length=128)
    model: str = Field(max_length=256)
    config_version: str = Field(max_length=256)
    calibration_version: str = Field(default="uncalibrated", max_length=128)
    calibration_digest: str | None = None


class CalibratedScore(StrictModel):
    raw_value: float | None = Field(default=None, ge=0, le=1, strict=True)
    status: Literal["calibrated", "uncalibrated"] = "uncalibrated"
    calibrated_value: float | None = Field(default=None, ge=0, le=1)
    calibrated_band: tuple[float, float] | None = None
    profile_version: str | None = None
    dataset_version: str | None = None

    @model_validator(mode="after")
    def calibration_requires_profile(self) -> CalibratedScore:
        if self.status == "calibrated" and (
            self.raw_value is None
            or self.calibrated_value is None
            or self.calibrated_band is None
            or not self.profile_version
            or not self.dataset_version
        ):
            raise ValueError("calibrated values require a matching profile and dataset")
        if self.status == "uncalibrated" and (
            self.calibrated_value is not None or self.calibrated_band is not None
        ):
            raise ValueError("uncalibrated scores cannot carry calibrated values")
        return self


class AssessmentDimensions(StrictModel):
    taxonomy: CalibratedScore = Field(default_factory=CalibratedScore)
    suggested_kind: str | None = Field(default=None, max_length=64)
    retention: CalibratedScore = Field(default_factory=CalibratedScore)
    retention_disposition: Literal["retain", "transient", "noise", "uncertain"] = "uncertain"
    epistemic: CalibratedScore = Field(default_factory=CalibratedScore)
    epistemic_state: EpistemicState = "unknown"
    risk: Risk = "unknown"
    assertion_mode: AssertionMode = "unknown"
    origin: Origin = "unknown"
    reason_codes: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def supported_requires_calibration(self) -> AssessmentDimensions:
        if self.epistemic_state == "supported" and self.epistemic.status != "calibrated":
            raise ValueError("supported evidence requires a calibrated value and band")
        return self


class ReassessRequest(StrictModel):
    purpose: Purpose = "combined"
    target: AssessmentContract | None = None
    reason: Reason = "manual"


class ReassessBatchRequest(ReassessRequest):
    workspace_id: UUID | None = None
    after_item_id: UUID | None = None
    limit: int = Field(default=20, ge=1, le=100)


class ReassessResponse(StrictModel):
    request_id: UUID
    item_id: UUID
    job_id: UUID
    job_status: JobStatus
    attempts: int
    max_attempts: int
    target: AssessmentContract
    input_digest: str


class AssessmentView(StrictModel):
    assessment_id: UUID
    purpose: Purpose
    schema_version: str
    contract_hash: str
    input_digest: str
    created_at: datetime
    prior_assessment_id: UUID | None
    state: Literal["completed", "failed", "disabled", "stale", "legacy"]
    dimensions: AssessmentDimensions
    canonical_hash: str
    canonicalization_version: Literal["pg-jsonb-v1"] = "pg-jsonb-v1"


class AssessmentHistory(StrictModel):
    policy_version: str
    effective: dict[str, AssessmentView] = Field(default_factory=dict)
    assessments: list[AssessmentView] = Field(default_factory=list)
    next_before: UUID | None = None
