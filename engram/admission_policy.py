"""Declarative, provider-free admission-policy evaluation for issue #158.

This module only evaluates checked-in policy artifacts.  It has no database,
settings, queue, provider, or wall-clock dependency.  Callers construct safe
snapshots, pass an explicit evaluation time, and decide separately whether to
persist the resulting shadow decision.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

import jsonschema  # type: ignore[import-untyped]
import rfc8785

POLICY_DIRECTORY: Path = Path(__file__).resolve().parent.parent / "policies" / "admission"
POLICY_SCHEMA_PATH: Path = (
    Path(__file__).resolve().parent.parent / "schemas" / "admission-policy-v1.schema.json"
)
POLICY_SCHEMA_VERSION: Final[str] = "engram.admission-policy.v1"
V2_SCHEMA_VERSION: Final[str] = "engram.admission-assessment.v2"
SURFACES: Final[tuple[str, str, str]] = (
    "startup",
    "semantic_governed",
    "semantic_exploratory",
)
SurfaceDecision = Literal["allow", "withhold", "review_required", "blocked", "unknown"]
RiskState = Literal["low", "medium", "high", "unknown", "not_applicable"]
EpistemicState = Literal[
    "supported", "contested", "insufficient_evidence", "unknown", "not_applicable"
]


class PolicyLoadError(ValueError):
    """A policy artifact cannot be used safely."""


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(rfc8785.dumps(value)).hexdigest()


def _artifact_digest_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value[key] for key in value if key != "artifact_digest"}


@dataclass(frozen=True)
class LoadedAdmissionPolicy:
    profile_key: str
    policy_version: str
    artifact_digest: str
    assessment_policy_version: str
    accepted_contract_hashes: frozenset[str]
    required_purposes: tuple[str, ...]
    observation_windows_hours: Mapping[str, int]
    allow_existing_human_verified_startup: bool


@dataclass(frozen=True)
class AdmissionItemState:
    item_id: str
    tenant_id: str
    content_hash: str
    kind: str
    source_type: str
    assertion_mode: str
    review_status: str
    created_at: datetime
    valid_to: datetime | None
    superseded_by: str | None
    unresolved_conflict: bool
    external_dispute: bool
    governed_review_required: bool
    human_verified: bool

    @property
    def live_proposal(self) -> bool:
        return (
            self.review_status == "proposed"
            and self.valid_to is None
            and self.superseded_by is None
        )


@dataclass(frozen=True)
class EffectiveAssessmentState:
    """Only the deterministically selected #157 assessment input.

    The simulator creates this from the #157 selection contract.  A row that
    is absent, stale, failed, disabled, mismatched, or uncalibrated keeps that
    fact explicit.  The evaluator never infers risk from kind or source.
    """

    selection_status: Literal[
        "selected", "missing", "disabled", "stale", "mismatched", "failed", "uncalibrated"
    ]
    contract_hash: str | None
    assessment_refs: tuple[Mapping[str, str], ...]
    risk_state: RiskState
    epistemic_state: EpistemicState
    retention_state: str
    calibrated: bool


@dataclass(frozen=True)
class AdmissionPolicyDecision:
    schema_version: str
    profile_key: str
    policy_version: str
    policy_config_digest: str
    decision_hash: str
    risk_state: RiskState
    epistemic_state: EpistemicState
    retention_state: str
    effective_assessment_refs: tuple[Mapping[str, str], ...]
    highest_admission_tier: str
    surface_decisions: Mapping[str, SurfaceDecision]
    blocker_codes: tuple[str, ...]
    reason_codes: tuple[str, ...]
    next_actions: tuple[str, ...]
    observation_window_hours: int | None
    eligible_at: datetime | None
    next_evaluation_at: datetime | None

    def envelope(self, item: AdmissionItemState) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "tenant_id": item.tenant_id,
            "memory_item_id": item.item_id,
            "mode": "shadow",
            "item_content_hash": item.content_hash,
            "policy_profile_key": self.profile_key,
            "policy_contract_version": self.policy_version,
            "policy_config_digest": self.policy_config_digest,
            "risk_state": self.risk_state,
            "epistemic_state": self.epistemic_state,
            "retention_state": self.retention_state,
            "effective_assessment_refs": [dict(ref) for ref in self.effective_assessment_refs],
            "highest_admission_tier": self.highest_admission_tier,
            "surface_decisions": dict(self.surface_decisions),
            "blocker_codes": list(self.blocker_codes),
            "reason_codes": list(self.reason_codes),
            "next_actions": list(self.next_actions),
            "observation_window_hours": self.observation_window_hours,
            "eligible_at": self.eligible_at.isoformat() if self.eligible_at else None,
            "next_evaluation_at": (
                self.next_evaluation_at.isoformat() if self.next_evaluation_at else None
            ),
        }


def load_admission_policy(profile_key: str) -> LoadedAdmissionPolicy:
    """Load and validate one checked-in policy without any external access."""
    allowed_key_characters = "abcdefghijklmnopqrstuvwxyz0123456789_"
    if not profile_key or any(char not in allowed_key_characters for char in profile_key):
        raise PolicyLoadError("invalid policy profile key")
    path = POLICY_DIRECTORY / f"{profile_key}.json"
    if not path.is_file():
        raise PolicyLoadError(f"policy artifact not found: {profile_key}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        schema = json.loads(POLICY_SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(raw)
    except (OSError, json.JSONDecodeError, jsonschema.ValidationError) as exc:
        raise PolicyLoadError(f"invalid policy artifact {profile_key}: {exc}") from exc
    if raw["schema_version"] != POLICY_SCHEMA_VERSION or raw["profile_key"] != profile_key:
        raise PolicyLoadError("policy artifact identity mismatch")
    actual_digest = _digest(_artifact_digest_payload(raw))
    if raw["artifact_digest"] != actual_digest:
        raise PolicyLoadError("policy artifact digest drift")
    selection = raw["assessment_selection"]
    return LoadedAdmissionPolicy(
        profile_key=raw["profile_key"],
        policy_version=raw["policy_version"],
        artifact_digest=actual_digest,
        assessment_policy_version=selection["policy_version"],
        accepted_contract_hashes=frozenset(selection["accepted_contract_hashes"]),
        required_purposes=tuple(selection["required_purposes"]),
        observation_windows_hours=dict(raw["rules"]["observation_windows_hours"]),
        allow_existing_human_verified_startup=raw["rules"]["startup"][
            "allow_existing_human_verified"
        ],
    )


def _sorted(values: set[str]) -> tuple[str, ...]:
    return tuple(sorted(values))


def _decision(
    *,
    item: AdmissionItemState,
    assessment: EffectiveAssessmentState,
    policy: LoadedAdmissionPolicy,
    risk: RiskState,
    epistemic: EpistemicState,
    retention: str,
    surfaces: Mapping[str, SurfaceDecision],
    blockers: set[str],
    reasons: set[str],
    actions: set[str],
    observation_window_hours: int | None,
    eligible_at: datetime | None,
    next_evaluation_at: datetime | None,
) -> AdmissionPolicyDecision:
    highest = next((surface for surface in SURFACES if surfaces[surface] == "allow"), "none")
    provisional = AdmissionPolicyDecision(
        schema_version=V2_SCHEMA_VERSION,
        profile_key=policy.profile_key,
        policy_version=policy.policy_version,
        policy_config_digest=policy.artifact_digest,
        decision_hash="",
        risk_state=risk,
        epistemic_state=epistemic,
        retention_state=retention,
        effective_assessment_refs=tuple(assessment.assessment_refs),
        highest_admission_tier=highest,
        surface_decisions=dict(surfaces),
        blocker_codes=_sorted(blockers),
        reason_codes=_sorted(reasons),
        next_actions=_sorted(actions),
        observation_window_hours=observation_window_hours,
        eligible_at=eligible_at,
        next_evaluation_at=next_evaluation_at,
    )
    return AdmissionPolicyDecision(
        **{**provisional.__dict__, "decision_hash": _digest(provisional.envelope(item))}
    )


def evaluate_admission_profile(
    item: AdmissionItemState,
    assessment: EffectiveAssessmentState,
    policy: LoadedAdmissionPolicy,
    evaluation_time: datetime,
) -> AdmissionPolicyDecision:
    """Evaluate the shadow profile from explicit snapshots and an explicit time."""
    if policy.profile_key != "risk_aware_shadow_v1":
        raise PolicyLoadError("unsupported admission profile")
    if evaluation_time.tzinfo is None or item.created_at.tzinfo is None:
        raise ValueError("evaluation_time and created_at must be timezone-aware")
    risk = assessment.risk_state
    epistemic = assessment.epistemic_state
    retention = assessment.retention_state
    blockers: set[str] = set()
    reasons: set[str] = {"shadow_only", "policy_risk_aware"}
    actions: set[str] = set()
    exploratory: SurfaceDecision = "allow"
    governed: SurfaceDecision = "withhold"
    startup: SurfaceDecision = "withhold"
    window: int | None = None
    eligible_at: datetime | None = None

    if not item.live_proposal:
        blockers.add("not_live_proposal")
        reasons.add("lifecycle_not_applicable")
        exploratory = governed = startup = "blocked"
    elif item.unresolved_conflict or item.external_dispute:
        blockers.add("unresolved_conflict" if item.unresolved_conflict else "external_dispute")
        reasons.add("governance_blocked")
        actions.add("conflict_resolution_required")
        exploratory = governed = startup = "blocked"
    elif item.governed_review_required:
        blockers.add("governed_review_required")
        reasons.add("existing_governance_requires_review")
        actions.add("human_review_required")
        governed = startup = "review_required"
    elif risk == "high":
        blockers.add("risk_high")
        reasons.add("high_consequence_requires_review")
        actions.add("human_review_required")
        governed = startup = "review_required"
    elif risk in {"unknown", "not_applicable"}:
        blockers.add("risk_unknown")
        reasons.add("unknown_consequence_requires_review")
        actions.add("human_review_required")
        governed = startup = "review_required"
    elif epistemic == "contested":
        blockers.add("epistemic_contested")
        reasons.add("contested_evidence_requires_review")
        actions.add("human_review_required")
        governed = startup = "review_required"
    elif (
        assessment.selection_status != "selected"
        or assessment.contract_hash not in policy.accepted_contract_hashes
        or not assessment.calibrated
        or epistemic != "supported"
        or retention != "retain"
    ):
        blockers.add(
            "epistemic_insufficient"
            if epistemic == "insufficient_evidence"
            else f"assessment_{assessment.selection_status}"
        )
        reasons.add("effective_assessment_not_qualified")
        if risk == "medium":
            governed = startup = "review_required"
            actions.add("human_review_required")
        else:
            governed = startup = "withhold"
            actions.add("new_evidence_required")
    else:
        window = policy.observation_windows_hours[risk]
        eligible_at = item.created_at + timedelta(hours=window)
        if evaluation_time >= eligible_at:
            governed = "allow"
            reasons.add("governed_evidence_qualified")
            if item.human_verified and policy.allow_existing_human_verified_startup:
                startup = "allow"
                reasons.add("existing_human_verified_startup_authority")
            else:
                blockers.add("startup_automatic_uncertified")
                reasons.add("startup_automatic_admission_disabled")
        else:
            blockers.add("observation_window")
            reasons.add("observation_window_pending")
            actions.add("wait_until")

    if not actions and governed == "allow":
        actions.add("none")
    return _decision(
        item=item,
        assessment=assessment,
        policy=policy,
        risk=risk,
        epistemic=epistemic,
        retention=retention,
        surfaces={
            "semantic_exploratory": exploratory,
            "semantic_governed": governed,
            "startup": startup,
        },
        blockers=blockers,
        reasons=reasons,
        actions=actions,
        observation_window_hours=window,
        eligible_at=eligible_at,
        next_evaluation_at=eligible_at if "wait_until" in actions else None,
    )


__all__ = [
    "AdmissionItemState",
    "AdmissionPolicyDecision",
    "EffectiveAssessmentState",
    "LoadedAdmissionPolicy",
    "POLICY_DIRECTORY",
    "PolicyLoadError",
    "V2_SCHEMA_VERSION",
    "evaluate_admission_profile",
    "load_admission_policy",
]
