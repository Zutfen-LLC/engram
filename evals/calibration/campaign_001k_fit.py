"""Campaign 001k (#216) Phases 3-7: observations, fitting, holdout, artifact.

FIX2-217 correction round. The fitting boundary now accepts ONLY verified
capabilities:

- provider outputs enter exclusively through ``ProviderEvidence216`` (a
  run-identity-BOUND record whose fields are validated, never optional
  private-attribute fallthroughs) consumed via ``stage_provider_values`` /
  ``reused_provider_values`` — there is no ``dict[str, dict[str, Any]]``
  provider-mapping parameter anywhere on a fitting/evaluation boundary;
- fresh DEV/HOLDOUT labels enter exclusively through
  ``FreshLabelAuthority216`` — a capability derived ONLY from a
  ``VerifiedConsensusLedger`` (the frozen #206 consensus machinery:
  three provenance-bound lanes, deterministic audit selection, protected
  human queue, full re-derivation) that additionally proves the exact #216
  stage bindings (campaign, protocol, stage membership, sampling-manifest
  digest, source packet digest, lane digests, queue evidence digest) —
  free-form ``labels_by_sample`` dicts are no longer accepted anywhere;
- fitting, holdout evaluation, artifact construction, and floor evaluation
  remain reused UNCHANGED from ``evals.calibration.fit`` (deterministic
  exact-stratum-reliability-bins-v1, MIN_CALIBRATION_SAMPLES bin floor,
  uncalibrated undersupported strata).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import model_validator

from engram.assessment_schema import AssessmentContract
from evals.admission.schema import Record
from evals.calibration.campaign_001k import (
    DIMENSIONS_001K,
    REPLAY3_SHA256,
    ReusedLabelSet,
)
from evals.calibration.fit import LabeledObservation, fit_profiles
from evals.calibration.freeze import FrameRow, SamplingManifest, SplitManifest, TargetIdentity

# ---------------------------------------------------------------------------
# FIX2-217-4: the one and only valid 001k target authority
# ---------------------------------------------------------------------------

#: The exact frozen #216 contract every 001k target identity must satisfy
#: field-exactly (FIX2-217-4). A hash-valid artifact that is not this exact
#: contract is NOT the 001k authority.
TARGET_001K_CONTRACT: dict[str, Any] = {
    "campaign_id": "eng-calibration-001k",
    "prompt_version": "engram.assess.3",
    "dimensions": ("taxonomy", "retention"),
    "assessment_schema_version": "engram.assessment.v1",
    "assessment_code_version": "assessment-engine-v1",
    "provider_adapter": "openai",
    "provider_model": "deepseek-ai/DeepSeek-V4-Flash",
    "calibration_dataset_version": "calibration-157-dogfood-v3-216",
    "assessment_policy_version": "assessment-selection-v1",
    "calibration_artifact_schema_version": "engram.calibration-profiles-v1",
    "label_guide_version": "engram-calibration-guide-157-v1",
    "canonicalization_version": "assessment-evidence-manifest-v1",
}
#: Canonical frozen provider parameters (ordering-independent comparison).
TARGET_001K_PROVIDER_PARAMS: dict[str, Any] = {
    "temperature": 0,
    "max_tokens": 1024,
    "input_limit": 16000,
}

_CONTRACT_STRING_FIELDS = (
    "campaign_id",
    "prompt_version",
    "assessment_schema_version",
    "assessment_code_version",
    "provider_adapter",
    "provider_model",
    "calibration_dataset_version",
    "assessment_policy_version",
    "calibration_artifact_schema_version",
    "label_guide_version",
    "canonicalization_version",
)


def verify_target_identity_001k(identity: TargetIdentity) -> TargetIdentity:
    """Mechanically require the frozen #216 001k contract, field-exactly.

    Digest self-consistency alone proves nothing (FIX2-217-4): this checks
    every frozen contract axis — campaign, prompt, dimensions, schema and
    code-contract versions, provider adapter/model, canonical provider
    parameters, dataset identity — and returns the VERIFIED identity for use
    downstream. The provider config digest must be in production form
    (``sha256:<64hex>``); the exact value is deployment-bound and validated
    separately by the campaign freeze against the deployed contract.
    """
    for field in _CONTRACT_STRING_FIELDS:
        if getattr(identity, field) != TARGET_001K_CONTRACT[field]:
            raise ValueError(f"target_identity_not_001k_contract:{field}")
    if tuple(identity.dimensions) != TARGET_001K_CONTRACT["dimensions"]:
        raise ValueError("target_identity_not_001k_contract:dimensions")
    if dict(identity.provider_params) != TARGET_001K_PROVIDER_PARAMS:
        raise ValueError("target_identity_not_001k_contract:provider_params")
    if not identity.provider_config_digest.startswith("sha256:"):
        raise ValueError("target_identity_not_001k_contract:provider_config_digest_form")
    return identity


# ---------------------------------------------------------------------------
# FIX2-217-1: provider evidence as a real verified capability
# ---------------------------------------------------------------------------

#: Runs whose outputs may back fitting, by allowed campaign stage.
FITTING_RUN_KINDS: tuple[str, ...] = (
    "issue-214-protected-200-case-replay-assess3",  # reused-200 dev only
    "issue-216-dev-102-assess3",  # exact fresh DEV stage
    "issue-216-holdout-100-assess3",  # exact fresh HOLDOUT stage
)

_RUN_KIND_TO_STAGE: dict[str, Literal["dev", "holdout"]] = {
    "issue-214-protected-200-case-replay-assess3": "dev",
    "issue-216-dev-102-assess3": "dev",
    "issue-216-holdout-100-assess3": "holdout",
}

_CASE_STATUSES: tuple[str, ...] = ("ok", "error", "abstained")

#: The reviewed historical-authority contract for the #214 replay-3 run
#: (FIX2-217-1). The replay predates the 001k target identity, so it cannot
#: carry a 001k target digest; it is bound through its OWN accepted execution
#: identity: exact protected artifact SHA-256, exact run kind, prompt, model,
#: the contract axes the retained evidence records, its 200-case population,
#: and its preserved strict-parser failures.
REPLAY3_HISTORICAL_AUTHORITY: dict[str, Any] = {
    "artifact_sha256": REPLAY3_SHA256,
    "run_kind": "issue-214-protected-200-case-replay-assess3",
    "prompt_version": "engram.assess.3",
    "provider_adapter": "openai",
    "provider_model": "deepseek-ai/DeepSeek-V4-Flash",
    "schema_version": "engram.assessment.v1",
    "code_version": "assessment-engine-v1",
    "execution_identity": "1dc42fca1f3062a06d0486fb9a53803e77410706",
    "expected_population": 200,
}


class _EvidenceCapability:
    """Opaque module-private seal: only ``ProviderEvidence216`` issues it."""

    __slots__ = ("payload_digest",)

    def __init__(self, payload_digest: str) -> None:
        self.payload_digest = payload_digest


class ProviderCase216(Record):
    """One provider-run case record, parsed BEFORE any dict conversion.

    Retained evidence fields (content hash binding, governed kind, observed
    model) are carried for provenance; only ``values`` feeds calibration.
    """

    sample_id: str
    status: Literal["ok", "error", "abstained"]
    values: dict[str, Any] | None = None
    error_type: str | None = None
    content_sha256: str | None = None
    governed_kind: str | None = None
    model: str | None = None

    @model_validator(mode="after")
    def status_shape(self) -> ProviderCase216:
        if self.status == "ok":
            if not isinstance(self.values, dict) or not self.values:
                raise ValueError("provider_case_ok_requires_values")
            if self.error_type is not None:
                raise ValueError("provider_case_ok_forbids_error_type")
        else:
            if self.values is not None:
                raise ValueError("provider_case_failure_forbids_values")
            if not self.error_type:
                raise ValueError("provider_case_failure_requires_error_type")
        return self


class ProviderEvidence216(Record):
    """Identity-BOUND assess.3 provider outputs for a 001k population stage.

    FIX2-217-1: every identity field is a REQUIRED validated field — never an
    optional private attribute or ``getattr(..., None)`` fallthrough. A run
    that cannot represent its identity fails at parse time.

    FIX2-217-2: ``values_by_sample`` is private. Verified stage-scoped views
    are issued only through ``stage_provider_values`` (fresh runs) /
    ``reused_provider_values`` (historical replay runs), each of which proves
    the complete run identity against the frozen 001k target authority or the
    reviewed historical replay-3 authority BEFORE anything is returned.
    """

    evidence_schema: Literal["engram-calibration-provider-evidence-216-v2"] = (
        "engram-calibration-provider-evidence-216-v2"
    )
    run_kind: str
    prompt_version: str
    provider_adapter: str
    provider_model: str
    schema_version: str
    code_version: str
    code_git_head: str
    target_identity_digest: str | None
    provider_config_digest: str | None
    artifact_sha256: str
    case_count: int
    cases: tuple[ProviderCase216, ...]
    _evidence_capability: Any = None

    @model_validator(mode="after")
    def run_identity_shape(self) -> ProviderEvidence216:
        if self.run_kind not in FITTING_RUN_KINDS:
            raise ValueError("provider_evidence_unknown_run_kind")
        if self.prompt_version != "engram.assess.3":
            raise ValueError("provider_evidence_wrong_prompt_version")
        # FIX2-217-1: code_git_head participates in the run identity — for
        # fresh 001k runs it must equal the target's campaign tooling SHA;
        # for the historical replay it must equal the accepted historical
        # execution identity. The comparison happens in the stage/reuse
        # verifiers below; here it must merely be a recorded 40-hex SHA.
        if len(self.code_git_head) != 40 or any(
            c not in "0123456789abcdef" for c in self.code_git_head
        ):
            raise ValueError("provider_evidence_missing_execution_identity")
        ids = [case.sample_id for case in self.cases]
        if len(set(ids)) != len(ids):
            # Duplicates are rejected mechanically BEFORE any dictionary
            # conversion could normalize them away.
            raise ValueError("provider_evidence_duplicate_sample_id")
        if self.case_count != len(self.cases):
            raise ValueError("provider_evidence_case_count_mismatch")
        for case in self.cases:
            if case.status not in _CASE_STATUSES:
                raise ValueError(f"provider_evidence_unknown_case_status:{case.status}")
        return self

    # -- construction from protected bytes ------------------------------------

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        artifact_sha256: str,
    ) -> ProviderEvidence216:
        """Parse untrusted payload bytes into an UNSEALED record.

        This deliberately does not grant access to provider values.  The
        caller-provided digest is retained as parsed metadata only; it is not
        evidence that any bytes were checked.  Only ``load_verified`` may
        attach the private verification capability.
        """
        raw_cases = payload.get("cases")
        if not isinstance(raw_cases, list) or not raw_cases:
            raise ValueError("provider_evidence_cases_missing")
        # Mechanical duplicate detection over the authoritative LIST before
        # any keyed form exists.
        seen: set[str] = set()
        for case in raw_cases:
            sid = case.get("sample_id") if isinstance(case, dict) else None
            if not isinstance(sid, str) or not sid:
                raise ValueError("provider_evidence_case_missing_sample_id")
            if sid in seen:
                raise ValueError("provider_evidence_duplicate_sample_id")
            seen.add(sid)
        cases = tuple(ProviderCase216.model_validate(case) for case in raw_cases)
        return cls(
            run_kind=str(payload["run_kind"]),
            prompt_version=str(payload["prompt_version"]),
            provider_adapter=str(payload.get("provider_adapter", "")),
            provider_model=str(payload.get("model", "")),
            schema_version=str(payload.get("schema_version", "")),
            code_version=str(payload.get("code_version", "")),
            code_git_head=str(payload.get("code_git_head", "")),
            target_identity_digest=payload.get("target_identity_digest"),
            provider_config_digest=payload.get("provider_config_digest"),
            artifact_sha256=artifact_sha256,
            case_count=len(cases),
            cases=cases,
        )

    @classmethod
    def load_verified(
        cls,
        path: Path,
        *,
        expected_sha256: str,
    ) -> ProviderEvidence216:
        """Digest-verified loading of one protected provider-evidence artifact."""
        payload_bytes = path.read_bytes()
        actual = hashlib.sha256(payload_bytes).hexdigest()
        if not hmac.compare_digest(actual, expected_sha256):
            raise ValueError("provider_evidence_artifact_digest_mismatch")
        evidence = cls.from_payload(json.loads(payload_bytes), artifact_sha256=actual)
        object.__setattr__(
            evidence,
            "_evidence_capability",
            _EvidenceCapability(evidence._binding_digest()),
        )
        return evidence

    def _binding_digest(self) -> str:
        """Bind the complete parsed state, not a caller-supplied digest claim."""
        payload = self.model_dump(mode="json")
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _require_capability(self) -> None:
        cap = getattr(self, "_evidence_capability", None)
        if not isinstance(cap, _EvidenceCapability) or not hmac.compare_digest(
            cap.payload_digest, self._binding_digest()
        ):
            raise ValueError("provider_evidence_capability_invalid")

    # -- verified accessors ----------------------------------------------------

    def _values_by_sample(self) -> dict[str, dict[str, Any]]:
        """Private keyed view over the validated case records."""
        return {
            case.sample_id: dict(case.values or {}) for case in self.cases if case.status == "ok"
        }

    def successful_ids(self) -> frozenset[str]:
        return frozenset(c.sample_id for c in self.cases if c.status == "ok")

    def error_ids(self) -> frozenset[str]:
        """Explicit error/abstention IDs (never fabricated into values)."""
        return frozenset(c.sample_id for c in self.cases if c.status != "ok")

    def all_case_ids(self) -> frozenset[str]:
        return frozenset(c.sample_id for c in self.cases)

    def ok_count(self) -> int:
        return sum(1 for c in self.cases if c.status == "ok")

    def error_count(self) -> int:
        return sum(1 for c in self.cases if c.status != "ok")

    def _require_identity(
        self, *, prompt: str, adapter: str, model: str, schema: str, code: str
    ) -> None:
        if self.prompt_version != prompt:
            raise ValueError("provider_evidence_prompt_mismatch")
        if adapter and self.provider_adapter != adapter:
            raise ValueError("provider_evidence_adapter_mismatch")
        if self.provider_model != model:
            raise ValueError("provider_evidence_model_mismatch")
        if self.schema_version != schema:
            raise ValueError("provider_evidence_schema_mismatch")
        if self.code_version != code:
            raise ValueError("provider_evidence_code_version_mismatch")

    def stage_provider_values(
        self,
        *,
        target_identity: TargetIdentity,
        expected_population: frozenset[str],
        expected_stage: Literal["dev", "holdout"],
    ) -> dict[str, dict[str, Any]]:
        """Verified values for a FRESH 001k provider run (FIX2-217-1).

        Fails closed on the complete run authority BEFORE returning anything:

        - exact prompt / adapter / model / schema / code contract == target;
        - the run MUST record the exact frozen 001k target identity digest
          (a missing digest is a failure, never "skip target verification");
        - the run's recorded provider-config digest MUST equal the target's;
        - ``code_git_head`` MUST equal the target's campaign tooling SHA
          (the execution identity the freeze was bound to);
        - population proof is EXACT equality over all case IDs (successful
          plus errors/abstentions) — never a subset of successful IDs.
        """
        self._require_capability()
        if _RUN_KIND_TO_STAGE.get(self.run_kind) != expected_stage:
            raise ValueError("provider_evidence_stage_mismatch")
        verify_target_identity_001k(target_identity)
        self._require_identity(
            prompt=target_identity.prompt_version,
            adapter=target_identity.provider_adapter,
            model=target_identity.provider_model,
            schema=target_identity.assessment_schema_version,
            code=target_identity.assessment_code_version,
        )
        recorded_target = self.target_identity_digest
        if not recorded_target:
            raise ValueError("provider_evidence_target_identity_missing")
        if not hmac.compare_digest(recorded_target, target_identity.identity_digest()):
            raise ValueError("provider_evidence_target_identity_mismatch")
        if self.provider_config_digest is None:
            raise ValueError("provider_evidence_config_digest_missing")
        if not hmac.compare_digest(
            self.provider_config_digest, target_identity.provider_config_digest
        ):
            raise ValueError("provider_evidence_config_mismatch")
        if not hmac.compare_digest(self.code_git_head, target_identity.campaign_tooling_repo_sha):
            raise ValueError("provider_evidence_execution_identity_mismatch")
        self._prove_population(expected_population)
        expected_count = 102 if expected_stage == "dev" else 100
        if self.case_count != expected_count:
            raise ValueError("provider_evidence_stage_case_count_mismatch")
        return self._values_by_sample()

    def reused_provider_values(
        self,
        *,
        expected_population: frozenset[str],
    ) -> dict[str, dict[str, Any]]:
        """Verified values for the HISTORICAL #214 replay-3 run (FIX2-217-1).

        The replay predates the 001k target identity; it is bound through the
        reviewed historical-authority contract (exact artifact SHA-256, run
        kind, prompt, adapter/model, schema/code axes, execution identity,
        and its exact 200-case population). The retained evidence genuinely
        lacks a provider-config digest; that absence is handled HERE, in the
        explicit reviewed contract — never by silently skipping verification.
        """
        self._require_capability()
        auth = REPLAY3_HISTORICAL_AUTHORITY
        if not hmac.compare_digest(self.artifact_sha256, auth["artifact_sha256"]):
            raise ValueError("replay3_authority_artifact_mismatch")
        if self.run_kind != auth["run_kind"]:
            raise ValueError("replay3_authority_run_kind_mismatch")
        self._require_identity(
            prompt=auth["prompt_version"],
            adapter=auth["provider_adapter"],
            model=auth["provider_model"],
            schema=auth["schema_version"],
            code=auth["code_version"],
        )
        if not hmac.compare_digest(self.code_git_head, auth["execution_identity"]):
            raise ValueError("replay3_authority_execution_identity_mismatch")
        if self.target_identity_digest is not None:
            # The historical replay cannot truthfully claim a 001k identity.
            raise ValueError("replay3_authority_unexpected_target_digest")
        if len(expected_population) != auth["expected_population"]:
            raise ValueError("replay3_authority_population_size_mismatch")
        self._prove_population(expected_population)
        return self._values_by_sample()

    def _prove_population(self, expected_population: frozenset[str]) -> None:
        """all_case_ids == expected_population — exactly (FIX2-217-1).

        Missing IDs, extra IDs, duplicated IDs (rejected at parse), or
        foreign IDs all fail. A subset of successful IDs is NOT a population
        proof because errors/abstentions are part of the population.
        """
        all_ids = self.all_case_ids()
        if all_ids != expected_population:
            missing = len(expected_population - all_ids)
            extra = sorted(all_ids - expected_population)[:3]
            raise ValueError(
                f"provider_evidence_population_mismatch:{missing}_missing:extra={extra}"
            )


# ---------------------------------------------------------------------------
# FIX2-217-3: fresh labels as a verified campaign-ledger capability
# ---------------------------------------------------------------------------


class _LabelCapability:
    """Opaque module-private seal for the fresh-label authority."""

    __slots__ = ("binding_digest",)

    def __init__(self, binding_digest: str) -> None:
        self.binding_digest = binding_digest


class FreshLabelAuthority216(Record):
    """Verified fresh-label authority for ONE 001k stage (FIX2-217-3).

    The ONLY constructor is :meth:`from_verified_ledger`, which requires a
    genuine ``VerifiedConsensusLedger`` (capability-checked) plus the exact
    frozen #216 stage bindings. Labels reach ``LabeledObservation`` projection
    exclusively through this capability; duplicate labels are detected while
    reading the authoritative ledger wrapper list, BEFORE any keyed form.
    """

    authority_schema: Literal["engram-calibration-fresh-labels-216-v1"] = (
        "engram-calibration-fresh-labels-216-v1"
    )
    campaign_id: str
    protocol_version: str
    stage: Literal["dev", "holdout"]
    sampling_manifest_digest: str
    source_packet_digest: str
    lane_digests: tuple[str, ...]
    queue_evidence_sha256: str
    expected_membership_digest: str
    labels: tuple[tuple[str, dict[str, Any], str], ...]  # (sample_id, critical, origin)
    retained_unknown_ids: tuple[str, ...]
    human_adjudicated_ids: tuple[str, ...]
    _label_capability: Any = None

    @model_validator(mode="after")
    def stage_shape(self) -> FreshLabelAuthority216:
        if self.campaign_id != "eng-calibration-001k":
            raise ValueError("fresh_label_authority_campaign_mismatch")
        if self.protocol_version != "eng-calibration-consensus-206-v1":
            raise ValueError("fresh_label_authority_protocol_mismatch")
        if len(self.lane_digests) != 3:
            raise ValueError("fresh_label_authority_lane_count")
        ids = [row[0] for row in self.labels]
        if len(set(ids)) != len(ids):
            raise ValueError("fresh_label_authority_duplicate_sample_id")
        if not set(self.retained_unknown_ids) <= set(ids):
            raise ValueError("fresh_label_authority_unknown_not_subset")
        if not set(self.human_adjudicated_ids) <= set(ids):
            raise ValueError("fresh_label_authority_human_not_subset")
        return self

    @classmethod
    def from_verified_ledger(
        cls,
        verified_ledger: Any,
        *,
        stage: Literal["dev", "holdout"],
        stage_sampling: SamplingManifest,
        expected_membership: frozenset[str],
        source_packet_digest: str,
    ) -> FreshLabelAuthority216:
        """Derive the stage authority from a REAL verified consensus ledger.

        Binds: campaign ID, consensus protocol, stage, the exact stage
        sampling-manifest digest, the source/neutral packet digest, the three
        frozen lane digests, the human-queue evidence digest, the exact stage
        membership, consensus results (final dimensions + origins), retained
        unknown/abstention states, and required human adjudications.
        """
        from evals.calibration.ledger import require_verification_capability

        require_verification_capability(verified_ledger)
        ledger = verified_ledger.ledger
        if ledger.campaign_id != "eng-calibration-001k":
            raise ValueError("fresh_label_ledger_campaign_mismatch")
        expected_digest = stage_sampling.manifest_digest()
        if ledger.sampling_manifest_digest != expected_digest:
            raise ValueError("fresh_label_stage_sampling_mismatch")
        if not hmac.compare_digest(ledger.source_packet_digest, source_packet_digest):
            raise ValueError("fresh_label_source_packet_mismatch")
        membership = {wrapper.sample_id for wrapper in ledger.wrappers}
        if membership != expected_membership:
            missing = len(expected_membership - membership)
            foreign = sorted(membership - expected_membership)[:3]
            raise ValueError(f"fresh_label_membership_mismatch:{missing}_missing:foreign={foreign}")
        labels: list[tuple[str, dict[str, Any], str]] = []
        retained_unknown: list[str] = []
        human_ids: list[str] = []
        for wrapper in ledger.wrappers:
            critical = dict(wrapper.final_dimensions)
            labels.append((wrapper.sample_id, critical, wrapper.final_label_origin))
            if wrapper.final_label_origin == "human_adjudicated":
                human_ids.append(wrapper.sample_id)
            vals = [
                critical.get(field)
                for field in ("expected_kind", "retention_value", "epistemic_state", "consequence")
                if field in critical
            ]
            if any(v in ("unknown", "uncertain", "ambiguous", "unverifiable") for v in vals):
                retained_unknown.append(wrapper.sample_id)
        authority = cls(
            campaign_id=ledger.campaign_id,
            protocol_version=ledger.protocol_version,
            stage=stage,
            sampling_manifest_digest=expected_digest,
            source_packet_digest=ledger.source_packet_digest,
            lane_digests=tuple(ledger.lane_digests),
            queue_evidence_sha256=ledger.queue_evidence_sha256,
            expected_membership_digest=expected_membership_digest_of(expected_membership),
            labels=tuple(sorted(labels, key=lambda row: row[0])),
            retained_unknown_ids=tuple(sorted(retained_unknown)),
            human_adjudicated_ids=tuple(sorted(human_ids)),
        )
        object.__setattr__(
            authority,
            "_label_capability",
            _LabelCapability(authority._binding_digest()),
        )
        return authority

    def _binding_digest(self) -> str:
        payload = {k: v for k, v in self.model_dump(mode="json").items() if not k.startswith("_")}
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _require_capability(self) -> None:
        cap = getattr(self, "_label_capability", None)
        if not isinstance(cap, _LabelCapability) or cap.binding_digest != self._binding_digest():
            raise ValueError("fresh_label_authority_capability_invalid")

    def labels_by_sample(self) -> dict[str, dict[str, Any]]:
        """Stage-scoped immutable-ish keyed view, authority-bound."""
        self._require_capability()
        return {sid: dict(critical) for sid, critical, _origin in self.labels}


def expected_membership_digest_of(membership: frozenset[str]) -> str:
    return hashlib.sha256(
        json.dumps(sorted(membership), separators=(",", ":")).encode()
    ).hexdigest()


# ---------------------------------------------------------------------------
# Phase 5 DEV fitting authority
# ---------------------------------------------------------------------------


class _DevFitCapability:
    __slots__ = ("binding_digest",)

    def __init__(self, binding_digest: str) -> None:
        self.binding_digest = binding_digest


class DevFitAuthority216:
    """Opaque result of canonical pre-holdout DEV fitting."""

    __slots__ = (
        "target_identity_digest",
        "split_digest",
        "dev_membership_digest",
        "dev_fitting_evidence_digest",
        "fitting_inputs_digest",
        "holdout_membership_digest",
        "candidate_bytes",
        "_capability",
    )

    def __init__(
        self,
        *,
        target_identity_digest: str,
        split_digest: str,
        dev_membership_digest: str,
        dev_fitting_evidence_digest: str,
        fitting_inputs_digest: str,
        holdout_membership_digest: str,
        candidate_bytes: bytes,
        capability: _DevFitCapability | None = None,
    ) -> None:
        self.target_identity_digest = target_identity_digest
        self.split_digest = split_digest
        self.dev_membership_digest = dev_membership_digest
        self.dev_fitting_evidence_digest = dev_fitting_evidence_digest
        self.fitting_inputs_digest = fitting_inputs_digest
        self.holdout_membership_digest = holdout_membership_digest
        self.candidate_bytes = candidate_bytes
        self._capability = capability

    def _binding_digest(self) -> str:
        payload = {
            "target_identity_digest": self.target_identity_digest,
            "split_digest": self.split_digest,
            "dev_membership_digest": self.dev_membership_digest,
            "dev_fitting_evidence_digest": self.dev_fitting_evidence_digest,
            "fitting_inputs_digest": self.fitting_inputs_digest,
            "holdout_membership_digest": self.holdout_membership_digest,
            "candidate_sha256": hashlib.sha256(self.candidate_bytes).hexdigest(),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _require_capability(self) -> None:
        if not isinstance(self._capability, _DevFitCapability) or not hmac.compare_digest(
            self._capability.binding_digest, self._binding_digest()
        ):
            raise ValueError("dev_fit_authority_capability_invalid")

    def unlock_holdout(self, protected_root: Path) -> Path:
        from evals.calibration.campaign_001k_holdout_barrier import unlock_holdout

        return unlock_holdout(protected_root=protected_root, authority=self)


# ---------------------------------------------------------------------------
# Observation construction (the ONLY fitting/evaluation boundary)
# ---------------------------------------------------------------------------


def _dimensions_view(critical: dict[str, Any]) -> Any:
    """Adapt a final critical-fields dict to the frozen Dimensions API."""

    class _View:
        __slots__ = ("expected_kind", "retention_value", "epistemic_state", "consequence")

        def __init__(self, fields: dict[str, Any]):
            self.expected_kind = fields.get("expected_kind", "unknown")
            self.retention_value = fields.get("retention_value", "uncertain")
            self.epistemic_state = fields.get("epistemic_state", "unknown")
            self.consequence = fields.get("consequence", "unknown")

    if not isinstance(critical, dict):
        raise ValueError("label_missing_final_dimensions")
    return _View(critical)


def _frame_stratum(row: FrameRow) -> dict[str, str]:
    """Frozen frame stratum -> observation vocabulary (unavailable->unknown)."""
    return {
        "source_type": row.source_type,
        "assertion_mode": "unknown" if row.assertion_mode == "unavailable" else row.assertion_mode,
        "kind": row.kind,
        "risk": "unknown" if row.risk == "unavailable" else row.risk,
    }


def _provider_case_values(
    provider_values: dict[str, dict[str, Any]], sid: str
) -> tuple[dict[str, float | None], str | None]:
    values = provider_values.get(sid)
    if values is None:
        # Honest abstention/parse failure: no provider numeric; the label
        # still counts toward review totals via the floor evaluator.
        return {
            "taxonomy_value": None,
            "retention_value": None,
            "epistemic_value": None,
        }, None
    return {
        "taxonomy_value": values.get("taxonomy_value"),
        "retention_value": values.get("retention_value"),
        "epistemic_value": values.get("epistemic_value"),
    }, values.get("suggested_kind")


def observations_from_reused(
    *,
    reused: ReusedLabelSet,
    replay_evidence: ProviderEvidence216,
    expected_population: frozenset[str],
    split: SplitManifest,
    frame_by_id: dict[str, FrameRow],
    dimensions: tuple[str, ...] = DIMENSIONS_001K,
) -> list[LabeledObservation]:
    """Dev observations for the reused-200 from synthesis labels + replay-3.

    FIX2-217-2: provider values enter ONLY through the verified historical
    replay-3 capability (``reused_provider_values``); there is no naked
    provider-mapping parameter.
    """
    provider_values = replay_evidence.reused_provider_values(
        expected_population=expected_population
    )
    split_by_id = {sid: "dev" for sid in split.dev_ids}
    split_by_id.update({sid: "holdout" for sid in split.holdout_ids})
    out: list[LabeledObservation] = []
    for label in reused.labels:
        sid = label.sample_id
        if split_by_id.get(sid) != "dev":
            raise ValueError("reused_label_must_be_dev_side")
        row = frame_by_id[sid]
        raw_scores, suggested = _provider_case_values(provider_values, sid)
        obs = LabeledObservation.from_review(
            sample_id=sid,
            split="dev",
            dimensions=_dimensions_view(label.final),
            raw_scores=raw_scores,
            suggested_kind=suggested,
            stratum=_frame_stratum(row),
        )
        out.extend(o for o in obs if o.dimension in dimensions)
    return out


def _fresh_stage_observations(
    *,
    label_authority: FreshLabelAuthority216,
    provider_values: dict[str, dict[str, Any]],
    split: SplitManifest,
    frame_by_id: dict[str, FrameRow],
    expected_ids: frozenset[str],
    expected_split: Literal["dev", "holdout"],
    stage: Literal["dev_fit", "holdout_evaluate"],
    dimensions: tuple[str, ...],
    require_complete: bool,
) -> list[LabeledObservation]:
    """Shared strict stage join over the VERIFIED label authority."""
    label_authority._require_capability()
    label_ids = {sid for sid, _critical, _origin in label_authority.labels}
    if label_ids - expected_ids:
        foreign = sorted(label_ids - expected_ids)[:3]
        raise ValueError(f"{stage}_labels_outside_frozen_membership:{foreign}")
    if require_complete and label_ids != expected_ids:
        missing = len(expected_ids - label_ids)
        raise ValueError(f"{stage}_labels_incomplete:{missing}_missing")
    split_by_id = {sid: "dev" for sid in split.dev_ids}
    split_by_id.update({sid: "holdout" for sid in split.holdout_ids})
    for sid in label_ids:
        if split_by_id.get(sid) != expected_split:
            raise ValueError(f"{stage}_label_split_mismatch:{sid}")
    critical_by_id = label_authority.labels_by_sample()
    out: list[LabeledObservation] = []
    for sid in sorted(label_ids):
        row = frame_by_id[sid]
        raw_scores, suggested = _provider_case_values(provider_values, sid)
        obs = LabeledObservation.from_review(
            sample_id=sid,
            split=expected_split,
            dimensions=_dimensions_view(critical_by_id[sid]),
            raw_scores=raw_scores,
            suggested_kind=suggested,
            stratum=_frame_stratum(row),
        )
        out.extend(o for o in obs if o.dimension in dimensions)
    return out


def dev_fit_observations(
    *,
    fresh_label_authority: FreshLabelAuthority216,
    provider_evidence: ProviderEvidence216,
    target_identity: TargetIdentity,
    split: SplitManifest,
    frame_by_id: dict[str, FrameRow],
    reuse: Any,
    dimensions: tuple[str, ...] = DIMENSIONS_001K,
    require_complete: bool = True,
) -> list[LabeledObservation]:
    """DEV-fitting observations from fresh DEV labels only.

    FIX2-217-2/FIX2-217-3: ``fresh_label_authority`` is the verified campaign
    ledger capability (stage == "dev"); ``provider_evidence`` is verified
    against the frozen 001k target with the exact fresh-202 population. The
    expected fitting population is exactly ``forced_dev_fresh ∪ dev_fresh``
    (102); a holdout ID in either input fails closed.
    """
    if fresh_label_authority.stage != "dev":
        raise ValueError("dev_fit_requires_dev_stage_authority")
    expected = frozenset(reuse.forced_dev_fresh_ids) | frozenset(reuse.dev_fresh_ids)
    provider_values = provider_evidence.stage_provider_values(
        target_identity=target_identity,
        expected_population=expected,
        expected_stage="dev",
    )
    return _fresh_stage_observations(
        label_authority=fresh_label_authority,
        provider_values=provider_values,
        split=split,
        frame_by_id=frame_by_id,
        expected_ids=expected,
        expected_split="dev",
        stage="dev_fit",
        dimensions=dimensions,
        require_complete=require_complete,
    )


def fit_canonical_dev_authority(
    *,
    protected_root: Path,
    fresh_label_authority: FreshLabelAuthority216,
    provider_evidence: ProviderEvidence216,
    reused_labels: ReusedLabelSet,
    replay_evidence: ProviderEvidence216,
    frame_by_id: dict[str, FrameRow],
    contract: AssessmentContract,
) -> DevFitAuthority216:
    """Issue a sealed candidate from canonical DEV-102 fitting inputs only.

    Target, split, reuse manifest and DEV sampling authority are loaded from
    ``protected_root``.  A caller-created ``TargetIdentity`` never crosses
    this execution boundary; HOLDOUT provider or label material is not an
    input to this operation.
    """
    from evals.calibration import campaign_001k as c216
    from evals.calibration.campaign_001k_stage_authority import verify_stage_authority

    identity = c216.load_001k_target_identity(protected_root)
    reuse = c216.ReuseManifest.model_validate(
        json.loads((protected_root / "reuse-manifest.json").read_text())
    )
    split = SplitManifest.model_validate(
        json.loads((protected_root / "split-manifest-001k.json").read_text())
    )
    sampling = SamplingManifest.model_validate(
        json.loads((protected_root / "dev-sampling-manifest.json").read_text())
    )
    stage = verify_stage_authority(
        protected_root=protected_root,
        sampling=sampling,
        source_packet_digest=fresh_label_authority.source_packet_digest,
    )
    stage.require_capability()
    fresh_label_authority._require_capability()
    if fresh_label_authority.stage != "dev" or not hmac.compare_digest(
        fresh_label_authority.expected_membership_digest, stage.membership_digest
    ):
        raise ValueError("dev_fit_authority_label_stage_mismatch")
    canonical_reused_path = protected_root / "reused-labels-001k.json"
    if not canonical_reused_path.is_file():
        raise ValueError("dev_fit_authority_reused_labels_missing")
    canonical_reused = ReusedLabelSet.model_validate(json.loads(canonical_reused_path.read_text()))
    if not hmac.compare_digest(canonical_reused.set_digest(), reused_labels.set_digest()):
        raise ValueError("dev_fit_authority_reused_labels_mismatch")
    reused_observations = observations_from_reused(
        reused=canonical_reused,
        replay_evidence=replay_evidence,
        expected_population=frozenset(label.sample_id for label in canonical_reused.labels),
        split=split,
        frame_by_id=frame_by_id,
    )
    if len({row.sample_id for row in reused_observations}) != 200:
        raise ValueError("dev_fit_authority_reused_count_mismatch")
    observations = reused_observations + dev_fit_observations(
        fresh_label_authority=fresh_label_authority,
        provider_evidence=provider_evidence,
        target_identity=identity,
        split=split,
        frame_by_id=frame_by_id,
        reuse=reuse,
    )
    if len({row.sample_id for row in observations}) != 302:
        raise ValueError("dev_fit_authority_dev_population_mismatch")
    profiles = fit_profiles(observations, identity=identity, contract=contract, split=split)
    inputs = {
        "target": identity.identity_digest(),
        "split": split.split_digest(),
        "reuse": reuse.manifest_digest(),
        "reused_labels": canonical_reused.set_digest(),
        "replay": replay_evidence._binding_digest(),
        "fresh_labels": fresh_label_authority._binding_digest(),
        "provider": provider_evidence._binding_digest(),
        "frame": {sid: row.model_dump(mode="json") for sid, row in sorted(frame_by_id.items())},
        "contract": contract.model_dump(mode="json"),
    }
    inputs_digest = hashlib.sha256(
        json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    reused_membership = frozenset(label.sample_id for label in canonical_reused.labels)
    all_dev_membership = (
        reused_membership | frozenset(reuse.forced_dev_fresh_ids) | frozenset(reuse.dev_fresh_ids)
    )
    if len(all_dev_membership) != 302:
        raise ValueError("dev_fit_authority_dev_membership_count_mismatch")
    dev_evidence_digest = hashlib.sha256(
        json.dumps(
            {
                "reused_labels": canonical_reused.set_digest(),
                "replay": replay_evidence._binding_digest(),
                "fresh_labels": fresh_label_authority._binding_digest(),
                "fresh_provider": provider_evidence._binding_digest(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    candidate_bytes = json.dumps(
        {
            "candidate_schema": "engram-calibration-pre-holdout-candidate-216-v1",
            "target_identity_digest": identity.identity_digest(),
            "split_digest": split.split_digest(),
            "dev_fresh_count": 102,
            "reused_count": 200,
            "dev_total": 302,
            "fitting_methodology": "deterministic-exact-stratum-reliability-bins-v1",
            "fitting_inputs_digest": inputs_digest,
            "profiles": [profile.model_dump(mode="json") for profile in profiles],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    authority = DevFitAuthority216(
        target_identity_digest=identity.identity_digest(),
        split_digest=split.split_digest(),
        dev_membership_digest=expected_membership_digest_of(all_dev_membership),
        dev_fitting_evidence_digest=dev_evidence_digest,
        fitting_inputs_digest=inputs_digest,
        holdout_membership_digest=expected_membership_digest_of(frozenset(reuse.holdout_ids)),
        candidate_bytes=candidate_bytes,
    )
    authority._capability = _DevFitCapability(authority._binding_digest())
    return authority


def holdout_evaluate_observations(
    *,
    fresh_label_authority: FreshLabelAuthority216,
    provider_evidence: ProviderEvidence216,
    target_identity: TargetIdentity,
    split: SplitManifest,
    frame_by_id: dict[str, FrameRow],
    reuse: Any,
    dimensions: tuple[str, ...] = DIMENSIONS_001K,
) -> list[LabeledObservation]:
    """Holdout-evaluation observations from frozen holdout labels only.

    FIX2-217-2/FIX2-217-3: exact equality against the frozen 100 holdout
    IDs; a DEV-fresh ID in the input fails closed. Callable only after
    artifact freeze (enforced by the campaign holdout barrier).
    """
    if fresh_label_authority.stage != "holdout":
        raise ValueError("holdout_evaluate_requires_holdout_stage_authority")
    expected = frozenset(reuse.holdout_ids)
    provider_values = provider_evidence.stage_provider_values(
        target_identity=target_identity,
        expected_population=expected,
        expected_stage="holdout",
    )
    return _fresh_stage_observations(
        label_authority=fresh_label_authority,
        provider_values=provider_values,
        split=split,
        frame_by_id=frame_by_id,
        expected_ids=expected,
        expected_split="holdout",
        stage="holdout_evaluate",
        dimensions=dimensions,
        require_complete=True,
    )
