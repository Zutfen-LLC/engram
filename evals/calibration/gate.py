"""Fail-closed recommendation gate for dogfood shadow assessment selection.

The gate consumes explicit, identity-bound evidence. It never mutates runtime
configuration and can recommend only dogfood shadow selection.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from engram.assessment_calibration import (
    CalibrationProfile,
    calibrate,
    calibration_profiles_digest,
    load_profiles,
    verified_profiles_for_contract,
)
from engram.assessment_schema import AssessmentContract
from evals.admission.schema import Digest, Record, digest
from evals.calibration.fit import CalibrationArtifactBundle, EvidenceFloorResult

CERTIFIED_SERVING_PROFILES_REQUIRED = frozenset({"legacy"})


class AuthoritativeRecallProof(Record):
    """Mechanically verified, identity-bound HTTP/MCP evidence."""

    proof_schema: str
    campaign_id: str
    target_identity_digest: Digest
    artifact_digest: Digest
    deployed_repo_sha: str
    deployed_contract_digest: Digest
    assessment_policy_version: str
    captured_at: datetime
    ordinary_http_status: int
    ordinary_recall_profile: str
    mcp_ok: bool
    mcp_recall_profile: str
    assessment_selection_enabled: bool
    certified_serving_profiles: tuple[str, ...]
    governed_serving_authorized: bool
    exploratory_serving_authorized: bool
    evidence_sha256: Digest


class MismatchProof(Record):
    target_identity_digest: Digest
    artifact_digest: Digest
    deployed_contract_digest: Digest
    profile_set_digest: str
    baseline_calibrates: bool
    checks: dict[str, bool]


def _digest_hex(value: str) -> str:
    return value.removeprefix("sha256:")


def _load_authoritative_recall_proof(path: Path | None) -> AuthoritativeRecallProof | None:
    """Parse and hash bounded probe evidence; malformed evidence fails closed."""
    if path is None:
        return None
    try:
        payload = path.read_bytes()
        if not payload or len(payload) > 65536:
            return None
        data = json.loads(payload)
        observations = data["observations"]
        ordinary = observations["ordinary_http"]
        mcp = observations["mcp"]
        return AuthoritativeRecallProof(
            proof_schema=data["proof_schema"],
            campaign_id=data["campaign_id"],
            target_identity_digest=data["target_identity_digest"],
            artifact_digest=data["artifact_digest"],
            deployed_repo_sha=data["deployed_repo_sha"],
            deployed_contract_digest=data["deployed_contract_digest"],
            assessment_policy_version=data["assessment_policy_version"],
            captured_at=data["captured_at"],
            ordinary_http_status=ordinary["status_code"],
            ordinary_recall_profile=ordinary["effective_profile"],
            mcp_ok=mcp["ok"],
            mcp_recall_profile=mcp["effective_profile"],
            assessment_selection_enabled=observations["assessment_selection_enabled"],
            certified_serving_profiles=tuple(observations["certified_serving_profiles"]),
            governed_serving_authorized=observations["governed_serving_authorized"],
            exploratory_serving_authorized=observations["exploratory_serving_authorized"],
            evidence_sha256=hashlib.sha256(payload).hexdigest(),
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _recall_proof_matches(
    proof: AuthoritativeRecallProof | None,
    *,
    bundle: CalibrationArtifactBundle,
    deployed_contract: AssessmentContract,
    deployed_repo_sha: str,
    deployed_assessment_policy_version: str,
) -> bool:
    if proof is None:
        return False
    target = bundle.target
    now = datetime.now(UTC)
    proof_age = now - proof.captured_at if proof.captured_at.tzinfo is not None else None
    return (
        proof.proof_schema == "engram-authoritative-recall-proof-v1"
        and proof_age is not None
        and -timedelta(minutes=5) <= proof_age <= timedelta(hours=24)
        and proof.campaign_id == target.get("campaign_id")
        and proof.target_identity_digest == bundle.target_identity_digest
        and proof.artifact_digest == bundle.artifact_digest()
        and proof.deployed_repo_sha == deployed_repo_sha == target.get("repo_sha")
        and proof.deployed_contract_digest == digest(deployed_contract.model_dump(mode="json"))
        and proof.assessment_policy_version
        == deployed_assessment_policy_version
        == target.get("assessment_policy_version")
        and proof.ordinary_http_status == 200
        and proof.ordinary_recall_profile == "legacy"
        and proof.mcp_ok
        and proof.mcp_recall_profile == "legacy"
        and not proof.assessment_selection_enabled
        and set(proof.certified_serving_profiles) == CERTIFIED_SERVING_PROFILES_REQUIRED
        and not proof.governed_serving_authorized
        and not proof.exploratory_serving_authorized
    )


def gate_checks(
    *,
    artifact_path: Path,
    bundle: CalibrationArtifactBundle,
    deployed_contract: AssessmentContract,
    deployed_repo_sha: str,
    deployed_assessment_policy_version: str,
    certified_serving_profiles: set[str],
    selection_currently_enabled: bool,
    floor_result: EvidenceFloorResult,
    mismatch_proof: MismatchProof | None,
    authoritative_recall_evidence_path: Path | None,
) -> dict[str, Any]:
    """Evaluate every #202 condition and return KEEP_DISABLED on missing evidence."""
    checks: dict[str, Any] = {}
    try:
        profiles = load_profiles(str(artifact_path))
        checks["loader_parses_artifact"] = True
    except (OSError, ValueError):
        profiles = []
        checks["loader_parses_artifact"] = False
    checks["loader_profile_count"] = len(profiles)
    checks["loader_matches_bundle_profiles"] = [
        profile.model_dump(mode="json") for profile in profiles
    ] == bundle.profiles

    target = bundle.target
    checks["identity_matches_deployment"] = (
        target.get("provider_adapter") == deployed_contract.provider
        and target.get("provider_model") == deployed_contract.model
        and target.get("prompt_version") == deployed_contract.prompt_version
        and target.get("assessment_schema_version") == deployed_contract.schema_version
        and target.get("assessment_code_version") == deployed_contract.code_version
        and _digest_hex(str(target.get("provider_config_digest", "")))
        == _digest_hex(deployed_contract.config_version)
        and target.get("repo_sha") == deployed_repo_sha
        and target.get("assessment_policy_version") == deployed_assessment_policy_version
        and target.get("calibration_artifact_schema_version") == bundle.artifact_schema_version
        and target.get("calibration_dataset_version") == bundle.calibration_version
        and tuple(target.get("dimensions", ())) == ("taxonomy", "retention", "epistemic")
    )
    checks["calibration_digest_binds"] = (
        deployed_contract.calibration_version == bundle.calibration_version
        and deployed_contract.calibration_digest == calibration_profiles_digest(profiles)
        and bool(verified_profiles_for_contract(profiles, deployed_contract))
    )
    checks["floors_satisfied"] = (
        floor_result.passed
        and all(floor_result.checks.values())
        and bundle.floors_satisfied == floor_result.passed
        and bundle.floor_results == floor_result.model_dump(mode="json")
    )
    checks["holdout_support_satisfied"] = floor_result.checks.get(
        "holdout_size", False
    ) and floor_result.checks.get("holdout_labeled_support", False)

    profile_strata = {
        "/".join(
            [
                profile.dimension,
                profile.source_type,
                profile.assertion_mode,
                profile.kind,
                profile.risk,
            ]
        )
        for profile in profiles
    }
    min_bin = min(
        (bucket.count for profile in profiles for bucket in profile.bins if bucket.count > 0),
        default=None,
    )
    try:
        required_bin_support = int(bundle.floors["per_bin_support_min"])
    except (KeyError, TypeError, ValueError):
        required_bin_support = -1
    checks["unsupported_strata_fail_closed"] = (
        required_bin_support > 0
        and min_bin is not None
        and min_bin >= required_bin_support
        and set(bundle.unsupported_strata).isdisjoint(profile_strata)
    )

    expected_contract_digest = digest(deployed_contract.model_dump(mode="json"))
    checks["mismatch_proof_bound"] = (
        mismatch_proof is not None
        and mismatch_proof.target_identity_digest == bundle.target_identity_digest
        and mismatch_proof.artifact_digest == bundle.artifact_digest()
        and mismatch_proof.deployed_contract_digest == expected_contract_digest
        and mismatch_proof.profile_set_digest == (calibration_profiles_digest(profiles) or "")
    )
    checks["mismatch_resolves_uncalibrated"] = bool(
        checks["mismatch_proof_bound"]
        and mismatch_proof is not None
        and mismatch_proof.baseline_calibrates
        and mismatch_proof.checks
        and all(mismatch_proof.checks.values())
    )
    authoritative_recall_proof = _load_authoritative_recall_proof(
        authoritative_recall_evidence_path
    )
    checks["authoritative_recall_unchanged"] = _recall_proof_matches(
        authoritative_recall_proof,
        bundle=bundle,
        deployed_contract=deployed_contract,
        deployed_repo_sha=deployed_repo_sha,
        deployed_assessment_policy_version=deployed_assessment_policy_version,
    )
    checks["certified_serving_profiles_exact"] = (
        set(certified_serving_profiles) == CERTIFIED_SERVING_PROFILES_REQUIRED
    )
    checks["selection_currently_disabled"] = not selection_currently_enabled

    required = (
        "loader_parses_artifact",
        "loader_matches_bundle_profiles",
        "identity_matches_deployment",
        "calibration_digest_binds",
        "floors_satisfied",
        "holdout_support_satisfied",
        "unsupported_strata_fail_closed",
        "mismatch_proof_bound",
        "mismatch_resolves_uncalibrated",
        "authoritative_recall_unchanged",
        "certified_serving_profiles_exact",
        "selection_currently_disabled",
    )
    gate_pass = all(checks[name] is True for name in required)
    return {
        "gate_pass": gate_pass,
        "recommendation": "ENABLE_DOGFOOD_SHADOW_SELECTION" if gate_pass else "KEEP_DISABLED",
        "scope": "dogfood_shadow_selection_only",
        "checks": checks,
    }


def _supported_value(profile: CalibrationProfile) -> float:
    for bucket in profile.bins:
        if bucket.count >= 50:
            return (bucket.lower + bucket.upper) / 2
    raise ValueError("mismatch_proof_requires_supported_baseline_bin")


def prove_mismatch_uncalibrated(
    profiles: list[CalibrationProfile],
    artifact_contract: AssessmentContract,
    *,
    target_identity_digest: str,
    artifact_digest: str,
) -> MismatchProof:
    """Exercise production calibration for every material contract/stratum mismatch."""
    if not profiles:
        raise ValueError("mismatch_proof_requires_profiles")
    profile_digest = calibration_profiles_digest(profiles)
    if profile_digest is None or profile_digest != artifact_contract.calibration_digest:
        raise ValueError("mismatch_proof_profile_digest_mismatch")

    baseline_calibrates = True
    checks: dict[str, bool] = {}
    contract_mutations = {
        "provider": {"provider": "mismatch-provider"},
        "model": {"model": "mismatch-model"},
        "prompt_version": {"prompt_version": "mismatch-prompt"},
        "schema_version": {"schema_version": "mismatch-schema"},
        "code_version": {"code_version": "mismatch-code"},
        "config_version": {"config_version": "mismatch-config"},
        "calibration_version": {"calibration_version": "mismatch-calibration"},
    }
    for field, update in contract_mutations.items():
        mismatch_ok = True
        mismatched = artifact_contract.model_copy(update=update)
        for profile in profiles:
            raw = _supported_value(profile)
            baseline = calibrate(
                raw,
                profile=profile,
                contract=artifact_contract,
                dimension=profile.dimension,
                source_type=profile.source_type,
                assertion_mode=profile.assertion_mode,
                kind=profile.kind,
                risk=profile.risk,
            )
            baseline_calibrates = baseline_calibrates and baseline.status == "calibrated"
            result = calibrate(
                raw,
                profile=profile,
                contract=mismatched,
                dimension=profile.dimension,
                source_type=profile.source_type,
                assertion_mode=profile.assertion_mode,
                kind=profile.kind,
                risk=profile.risk,
            )
            mismatch_ok = mismatch_ok and result.status == "uncalibrated"
        checks[f"mismatch_{field}_stays_uncalibrated"] = mismatch_ok

    wrong_digest = artifact_contract.model_copy(update={"calibration_digest": "sha256:" + "0" * 64})
    checks["mismatch_calibration_digest_stays_uncalibrated"] = not verified_profiles_for_contract(
        profiles, wrong_digest
    )
    stratum_mutations = {
        "dimension": {"dimension": "mismatch"},
        "source_type": {"source_type": "mismatch"},
        "assertion_mode": {"assertion_mode": "mismatch"},
        "kind": {"kind": "mismatch"},
        "risk": {"risk": "mismatch"},
    }
    for field, update in stratum_mutations.items():
        mismatch_ok = True
        for profile in profiles:
            raw = _supported_value(profile)
            args: dict[str, str] = {
                "dimension": profile.dimension,
                "source_type": profile.source_type,
                "assertion_mode": profile.assertion_mode,
                "kind": profile.kind,
                "risk": profile.risk,
            }
            args.update(update)
            result = calibrate(raw, profile=profile, contract=artifact_contract, **args)
            mismatch_ok = mismatch_ok and result.status == "uncalibrated"
        checks[f"mismatch_{field}_stays_uncalibrated"] = mismatch_ok
    if not baseline_calibrates:
        raise ValueError("mismatch_proof_baseline_not_calibrated")
    return MismatchProof(
        target_identity_digest=target_identity_digest,
        artifact_digest=artifact_digest,
        deployed_contract_digest=digest(artifact_contract.model_dump(mode="json")),
        profile_set_digest=profile_digest,
        baseline_calibrates=baseline_calibrates,
        checks=checks,
    )


def write_public_report(
    *, bundle: CalibrationArtifactBundle, gate: dict[str, Any], output: Path
) -> str:
    report = {
        "calibration_version": bundle.calibration_version,
        "label_guide_version": bundle.label_guide_version,
        "target_identity_digest": bundle.target_identity_digest,
        "sampling_manifest_digest": bundle.sampling_manifest_digest,
        "split_manifest_digest": bundle.split_manifest_digest,
        "floors": bundle.floors,
        "floor_results": bundle.floor_results,
        "fitting_method": bundle.fitting_method,
        "profile_count": len(bundle.profiles),
        "supported_strata": sorted(
            f"{p['dimension']}/{p['source_type']}/{p['assertion_mode']}/{p['kind']}/{p['risk']}"
            for p in bundle.profiles
        ),
        "unsupported_strata": bundle.unsupported_strata,
        "holdout_metrics": bundle.holdout_metrics,
        "floors_satisfied": bundle.floors_satisfied,
        "artifact_digest": bundle.artifact_digest(),
        "selection_gate": {
            "gate_pass": gate["gate_pass"],
            "recommendation": gate["recommendation"],
            "scope": gate["scope"],
            "checks": gate["checks"],
        },
    }
    payload = (json.dumps(report, sort_keys=True, indent=2) + "\n").encode()
    output.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()
