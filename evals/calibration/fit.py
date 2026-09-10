"""Deterministic calibration fitting, holdout evaluation, artifact construction.

Fitting sees ONLY dev-split labels. The holdout split is evaluated, never
fitted. Every stratum below the frozen support floor stays explicitly
``uncalibrated`` — support is never extrapolated beyond reviewed evidence.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime

from engram.assessment_calibration import (
    MIN_CALIBRATION_SAMPLES,
    CalibrationBin,
    CalibrationProfile,
    calibrate,
)
from engram.assessment_schema import AssessmentContract, AssessmentDimensions
from evals.admission.schema import Digest, LabelRecord, Record, digest
from evals.calibration.freeze import (
    LABEL_GUIDE_VERSION,
    EvidenceFloors,
    FrameRow,
    SamplingManifest,
    SplitManifest,
    TargetIdentity,
    protected_frame_digest,
    sample_id_for,
    validate_split_membership,
)
from evals.calibration.review import verify_ledger

DimensionName = Literal["taxonomy", "retention", "epistemic"]
BinEdges: tuple[float, ...] = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)


class LabeledObservation(Record):
    """One reviewed sample joined to its captured raw provider scores."""

    sample_id: str
    split: Literal["dev", "holdout"]
    dimension: DimensionName
    # The calibration question per dimension: is the provider's raw score an
    # honest signal of the reviewed outcome for this dimension?
    outcome: Literal["positive", "negative", "unknown"]
    raw_value: float | None = None
    suggested_kind: str | None = None
    source_type: str
    assertion_mode: str
    kind: str
    risk: str
    consequence: Literal["low", "medium", "high", "unknown"] = "unknown"

    @classmethod
    def from_review(
        cls,
        *,
        sample_id: str,
        split: str,
        dimensions: Any,
        raw_scores: dict[str, float | None],
        suggested_kind: str | None,
        stratum: dict[str, str],
    ) -> list[LabeledObservation]:
        """Derive per-dimension outcomes from a final adjudicated label.

        The reviewer dimension vocabulary is the #162 admission-label schema;
        the mapping below is the frozen calibration mapping (v1):
          taxonomy  positive  <=> adjudicated expected_kind == provider suggested_kind
          retention positive  <=> retention_value == retain
          epistemic positive  <=> epistemic_state in {adequately_supported, weakly_supported}
        outcome "unknown" preserves reviewer uncertainty (uncertain/unknown
        states) — it never counts toward bin support.
        """
        out: list[LabeledObservation] = []
        suggested = str(suggested_kind or "")
        if dimensions.expected_kind == "unknown" or not suggested:
            outcome: Literal["positive", "negative", "unknown"] = "unknown"
        else:
            outcome = "positive" if dimensions.expected_kind == suggested else "negative"
        out.append(
            cls(
                sample_id=sample_id,
                split=split,  # type: ignore[arg-type]
                dimension="taxonomy",
                outcome=outcome,
                raw_value=raw_scores.get("taxonomy_value"),
                suggested_kind=suggested or None,
                consequence=dimensions.consequence,
                **stratum,
            )
        )
        if dimensions.retention_value == "uncertain":
            outcome = "unknown"
        else:
            outcome = "positive" if dimensions.retention_value == "retain" else "negative"
        out.append(
            cls(
                sample_id=sample_id,
                split=split,  # type: ignore[arg-type]
                dimension="retention",
                outcome=outcome,
                raw_value=raw_scores.get("retention_value"),
                consequence=dimensions.consequence,
                **stratum,
            )
        )
        if dimensions.epistemic_state in ("unknown", "ambiguous"):
            outcome = "unknown"
        else:
            outcome = (
                "positive"
                if dimensions.epistemic_state in ("adequately_supported", "weakly_supported")
                else "negative"
            )
        out.append(
            cls(
                sample_id=sample_id,
                split=split,  # type: ignore[arg-type]
                dimension="epistemic",
                outcome=outcome,
                raw_value=raw_scores.get("epistemic_value"),
                consequence=dimensions.consequence,
                **stratum,
            )
        )
        return out


class AssessmentExecutionReceipt(Record):
    """One content-free, target-bound assessment execution receipt."""

    sample_id: str
    input_content_hash: str
    execution_id: str
    captured_at: AwareDatetime
    provider_request_digest: Digest
    provider_response_digest: Digest
    assessment: AssessmentDimensions
    receipt_digest: Digest

    def verified_payload_digest(self) -> Digest:
        return digest(self.model_dump(mode="json", exclude={"receipt_digest"}))


def _bin_index(value: float) -> int:
    if value < 0.0 or value > 1.0:
        raise ValueError("raw value out of range")
    for index in range(len(BinEdges) - 1):
        lower, upper = BinEdges[index], BinEdges[index + 1]
        if lower <= value < upper or value == upper == 1.0:
            return index
    raise ValueError("unreachable bin")


def _validate_observation_membership(
    observations: list[LabeledObservation], split: SplitManifest
) -> None:
    assignments = {sample_id: "dev" for sample_id in split.dev_ids}
    assignments.update({sample_id: "holdout" for sample_id in split.holdout_ids})
    seen: set[tuple[str, str]] = set()
    for observation in observations:
        key = (observation.sample_id, observation.dimension)
        if key in seen:
            raise ValueError("duplicate_observation")
        seen.add(key)
        expected_split = assignments.get(observation.sample_id)
        if expected_split is None:
            raise ValueError("observation_not_in_frozen_split")
        if observation.split != expected_split:
            raise ValueError("observation_split_mismatch")


def _verify_observation_evidence(
    path: Path,
    expected_sha256: str,
    *,
    sampling: SamplingManifest,
    target_identity: TargetIdentity,
    assessment_contract: AssessmentContract,
    split: SplitManifest,
    frame: list[FrameRow],
    reviewed_records: list[LabelRecord],
) -> list[LabeledObservation]:
    payload = path.read_bytes()
    if not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), expected_sha256):
        raise ValueError("assessment_evidence_digest_mismatch")
    contract_digest = digest(assessment_contract.model_dump(mode="json"))
    if target_identity.identity_digest() != sampling.target_identity_digest:
        raise ValueError("assessment_target_identity_mismatch")
    if (
        assessment_contract.schema_version != target_identity.assessment_schema_version
        or assessment_contract.prompt_version != target_identity.prompt_version
        or assessment_contract.code_version != target_identity.assessment_code_version
        or assessment_contract.provider != target_identity.provider_adapter
        or assessment_contract.model != target_identity.provider_model
        or assessment_contract.config_version != target_identity.provider_config_digest
        or assessment_contract.calibration_version != target_identity.calibration_dataset_version
    ):
        raise ValueError("assessment_contract_target_mismatch")
    envelope = json.loads(payload)
    if envelope.get("evidence_schema") != "engram-calibration-assessment-evidence-v1":
        raise ValueError("assessment_evidence_schema_mismatch")
    if (
        envelope.get("target_identity_digest") != target_identity.identity_digest()
        or envelope.get("sampling_manifest_digest") != sampling.manifest_digest()
        or envelope.get("assessment_contract_digest") != contract_digest
        or envelope.get("frame_digest") != sampling.frame_digest
    ):
        raise ValueError("assessment_evidence_identity_mismatch")
    if protected_frame_digest(frame) != sampling.frame_digest:
        raise ValueError("assessment_frame_digest_mismatch")
    frame_by_id = {(row.sample_id or sample_id_for(row.item_uuid)): row for row in frame}
    if set(frame_by_id) != set(sampling.sample_ids):
        raise ValueError("assessment_frame_membership_mismatch")
    expected_hashes = dict(zip(sampling.sample_ids, sampling.sample_hashes, strict=True))
    if any(
        frame_by_id[sample_id].content_hash != expected_hashes[sample_id]
        for sample_id in expected_hashes
    ):
        raise ValueError("assessment_frame_content_hash_mismatch")
    receipts = [
        AssessmentExecutionReceipt.model_validate(row) for row in envelope.get("executions", [])
    ]
    if (
        len(receipts) != len(sampling.sample_ids)
        or {receipt.sample_id for receipt in receipts} != set(sampling.sample_ids)
        or len({receipt.execution_id for receipt in receipts}) != len(receipts)
    ):
        raise ValueError("assessment_evidence_membership_mismatch")
    records_by_id = {record.sample_id: record for record in reviewed_records}
    split_by_id = {sample_id: "dev" for sample_id in split.dev_ids}
    split_by_id.update({sample_id: "holdout" for sample_id in split.holdout_ids})
    observations: list[LabeledObservation] = []
    for receipt in receipts:
        if not hmac.compare_digest(receipt.verified_payload_digest(), receipt.receipt_digest):
            raise ValueError("assessment_execution_receipt_digest_mismatch")
        expected_request_digest = digest(
            {
                "sample_id": receipt.sample_id,
                "input_content_hash": receipt.input_content_hash,
                "target_identity_digest": target_identity.identity_digest(),
                "assessment_contract_digest": contract_digest,
            }
        )
        if receipt.provider_request_digest != expected_request_digest:
            raise ValueError("assessment_execution_request_mismatch")
        if receipt.input_content_hash != expected_hashes[receipt.sample_id]:
            raise ValueError("assessment_execution_input_mismatch")
        scores = (
            receipt.assessment.taxonomy,
            receipt.assessment.retention,
            receipt.assessment.epistemic,
        )
        if any(score.status != "uncalibrated" for score in scores):
            raise ValueError("assessment_execution_must_capture_raw_scores")
        frozen = frame_by_id[receipt.sample_id]
        dimensions = records_by_id[receipt.sample_id].final_dimensions()
        if dimensions is None:
            raise ValueError("assessment_evidence_requires_completed_review")
        observations.extend(
            LabeledObservation.from_review(
                sample_id=receipt.sample_id,
                dimensions=dimensions,
                raw_scores={
                    "taxonomy_value": receipt.assessment.taxonomy.raw_value,
                    "retention_value": receipt.assessment.retention.raw_value,
                    "epistemic_value": receipt.assessment.epistemic.raw_value,
                },
                suggested_kind=receipt.assessment.suggested_kind,
                stratum={
                    "source_type": frozen.source_type,
                    "assertion_mode": frozen.assertion_mode,
                    "kind": frozen.kind,
                    "risk": frozen.risk,
                },
                split=split_by_id[receipt.sample_id],
            )
        )
    return observations


def fit_profiles(
    observations: list[LabeledObservation],
    *,
    identity: TargetIdentity,
    contract: AssessmentContract,
    split: SplitManifest,
) -> list[CalibrationProfile]:
    """Fit exact-stratum reliability bins from DEV observations only.

    A bin with fewer than ``MIN_CALIBRATION_SAMPLES`` labeled (non-unknown)
    observations is dropped — the production loader would refuse it anyway,
    and an undersupported profile must not claim support it lacks.
    """
    _validate_observation_membership(observations, split)
    grouped: dict[tuple[str, ...], list[LabeledObservation]] = defaultdict(list)
    for obs in observations:
        if obs.split != "dev":
            continue
        grouped[
            (
                obs.dimension,
                obs.source_type,
                obs.assertion_mode,
                obs.kind,
                obs.risk,
            )
        ].append(obs)
    profiles: list[CalibrationProfile] = []
    for key in sorted(grouped):
        dimension, source_type, assertion_mode, kind, risk = key
        bins: list[dict[str, Any]] = [
            {"lower": BinEdges[i], "upper": BinEdges[i + 1], "value": 0.0, "count": 0}
            for i in range(len(BinEdges) - 1)
        ]
        positive: list[int] = [0] * (len(BinEdges) - 1)
        total: list[int] = [0] * (len(BinEdges) - 1)
        for obs in grouped[key]:
            if obs.raw_value is None or obs.outcome == "unknown":
                continue
            index = _bin_index(obs.raw_value)
            total[index] += 1
            if obs.outcome == "positive":
                positive[index] += 1
        supported = [index for index, count in enumerate(total) if count >= MIN_CALIBRATION_SAMPLES]
        if not supported:
            continue
        for index in supported:
            bins[index]["value"] = positive[index] / total[index]
            bins[index]["count"] = total[index]
        profiles.append(
            CalibrationProfile(
                version=identity.calibration_dataset_version,
                contract=contract,
                dataset_version=identity.calibration_dataset_version,
                dimension=dimension,  # type: ignore[arg-type]
                source_type=source_type,  # type: ignore[arg-type]
                assertion_mode=assertion_mode,  # type: ignore[arg-type]
                kind=kind,
                risk=risk,  # type: ignore[arg-type]
                bins=[CalibrationBin.model_validate(b) for b in bins],
            )
        )
    return profiles


class HoldoutMetrics(Record):
    dimension: str
    stratum: str
    n: int
    population_n: int
    brier: float | None = None
    ece: float | None = None
    raw_brier: float | None = None
    raw_ece: float | None = None
    mean_outcome: float | None = None
    mean_raw: float | None = None
    mean_calibrated: float | None = None
    selective_accuracy: float | None = None
    coverage: float | None = None
    reliability: tuple[dict[str, float | int | None], ...] = ()
    covered_stratum: bool = False


def _index_profiles(
    profiles: list[CalibrationProfile],
) -> dict[tuple[str, str, str, str, str], CalibrationProfile]:
    indexed = {_profile_key(profile): profile for profile in profiles}
    if len(indexed) != len(profiles):
        raise ValueError("duplicate_calibration_profile_key")
    return indexed


def evaluate_holdout(
    observations: list[LabeledObservation],
    *,
    profiles: list[CalibrationProfile],
    split: SplitManifest,
) -> list[HoldoutMetrics]:
    """Evaluate fitted calibration values on frozen holdout membership only."""
    _validate_observation_membership(observations, split)
    per: dict[tuple[str, str], list[LabeledObservation]] = defaultdict(list)
    for obs in observations:
        if obs.split != "holdout":
            continue
        stratum_key = "/".join([obs.source_type, obs.assertion_mode, obs.kind, obs.risk])
        per[(obs.dimension, stratum_key)].append(obs)
    profile_by_key = _index_profiles(profiles)

    def ece(pairs: list[tuple[float, str]]) -> float:
        weighted = 0.0
        for index in range(len(BinEdges) - 1):
            lower, upper = BinEdges[index], BinEdges[index + 1]
            bucket = [
                (value, outcome)
                for value, outcome in pairs
                if lower <= value < upper or value == upper == 1.0
            ]
            if bucket:
                mean_value = sum(value for value, _ in bucket) / len(bucket)
                frequency = sum(1 for _, outcome in bucket if outcome == "positive") / len(bucket)
                weighted += len(bucket) * abs(mean_value - frequency)
        return weighted / len(pairs)

    metrics: list[HoldoutMetrics] = []
    for key in sorted(per):
        dimension, stratum = key
        rows = per[key]
        labeled = [
            observation
            for observation in rows
            if observation.raw_value is not None and observation.outcome != "unknown"
        ]
        source_type, assertion_mode, kind, risk = stratum.split("/", 3)
        profile = profile_by_key.get((dimension, source_type, assertion_mode, kind, risk))
        if not labeled:
            metrics.append(
                HoldoutMetrics(
                    dimension=dimension,
                    stratum=stratum,
                    n=0,
                    population_n=len(rows),
                    covered_stratum=profile is not None,
                )
            )
            continue
        raw_pairs: list[tuple[float, str]] = []
        for observation in labeled:
            if observation.raw_value is None:
                raise ValueError("labeled_observation_missing_raw_value")
            raw_pairs.append((observation.raw_value, observation.outcome))
        calibrated_pairs: list[tuple[float, str]] = []
        if profile is not None:
            for observation in labeled:
                result = calibrate(
                    dimension=observation.dimension,
                    raw_value=observation.raw_value,
                    source_type=observation.source_type,
                    assertion_mode=observation.assertion_mode,
                    kind=observation.kind,
                    risk=observation.risk,
                    contract=profile.contract,
                    profile=profile,
                )
                if result.status == "calibrated" and result.calibrated_value is not None:
                    calibrated_pairs.append((result.calibrated_value, observation.outcome))
        raw_brier = sum(
            (value - (1.0 if outcome == "positive" else 0.0)) ** 2 for value, outcome in raw_pairs
        ) / len(raw_pairs)
        calibrated_brier = (
            sum(
                (value - (1.0 if outcome == "positive" else 0.0)) ** 2
                for value, outcome in calibrated_pairs
            )
            / len(calibrated_pairs)
            if calibrated_pairs
            else None
        )
        reliability: list[dict[str, float | int | None]] = []
        for index in range(len(BinEdges) - 1):
            lower, upper = BinEdges[index], BinEdges[index + 1]
            bucket = [
                (value, outcome)
                for value, outcome in calibrated_pairs
                if lower <= value < upper or value == upper == 1.0
            ]
            reliability.append(
                {
                    "lower": lower,
                    "upper": upper,
                    "count": len(bucket),
                    "mean_calibrated": (
                        sum(value for value, _ in bucket) / len(bucket) if bucket else None
                    ),
                    "observed_frequency": (
                        sum(1 for _, outcome in bucket if outcome == "positive") / len(bucket)
                        if bucket
                        else None
                    ),
                }
            )
        high_confidence = [(value, outcome) for value, outcome in calibrated_pairs if value >= 0.5]
        selective = (
            sum(1 for _, outcome in high_confidence if outcome == "positive") / len(high_confidence)
            if high_confidence
            else None
        )
        metrics.append(
            HoldoutMetrics(
                dimension=str(dimension),
                stratum=stratum,
                n=len(calibrated_pairs),
                population_n=len(rows),
                brier=None if calibrated_brier is None else round(calibrated_brier, 6),
                ece=None if not calibrated_pairs else round(ece(calibrated_pairs), 6),
                raw_brier=round(raw_brier, 6),
                raw_ece=round(ece(raw_pairs), 6),
                mean_outcome=round(
                    sum(1 for _, outcome in raw_pairs if outcome == "positive") / len(raw_pairs),
                    6,
                ),
                mean_raw=round(sum(value for value, _ in raw_pairs) / len(raw_pairs), 6),
                mean_calibrated=(
                    round(sum(value for value, _ in calibrated_pairs) / len(calibrated_pairs), 6)
                    if calibrated_pairs
                    else None
                ),
                selective_accuracy=None if selective is None else round(selective, 6),
                coverage=round(len(calibrated_pairs) / len(raw_pairs), 6),
                reliability=tuple(reliability),
                covered_stratum=profile is not None,
            )
        )
    return metrics


class EvidenceFloorResult(Record):
    """Evidence-derived result for every frozen floor, with partial support visible."""

    sampling_manifest_digest: str
    split_manifest_digest: str
    ledger_sha256: str
    reviewer_a_packet_sha256: str
    reviewer_b_packet_sha256: str
    assessment_evidence_sha256: str
    assessment_contract_digest: str
    full_population_dual_review: Literal[True]
    checks: dict[str, bool]
    dimension_support: dict[str, dict[str, Any]]
    stratum_support: dict[str, dict[str, Any]]
    bin_support: dict[str, dict[str, Any]]
    failures: tuple[str, ...]
    passed: bool


class CalibrationArtifactBundle(Record):
    """The versioned artifact bundle. The profiles list is the exact payload the
    production loader consumes; everything else is provenance/binding."""

    artifact_schema_version: Literal["engram.calibration-profiles-v1"] = (
        "engram.calibration-profiles-v1"
    )
    calibration_version: str
    label_guide_version: str = LABEL_GUIDE_VERSION
    target: dict[str, Any]
    target_identity_digest: str
    sampling_manifest_digest: str
    split_manifest_digest: str
    floors: dict[str, Any]
    fitting_method: Literal["exact-stratum-reliability-bins-v1"]
    profiles: list[dict[str, Any]]
    holdout_metrics: list[dict[str, Any]]
    unsupported_strata: list[str]
    floor_results: dict[str, Any]
    floors_satisfied: bool
    authoritative_recall_evidence_digest: Digest

    def artifact_digest(self) -> str:
        return digest(self.model_dump(mode="json"))

    def to_loader_payload(self) -> bytes:
        """The exact bytes the production ``load_profiles`` consumes."""
        import json

        return json.dumps(self.profiles, sort_keys=True, separators=(",", ":")).encode()


def build_artifact(
    *,
    identity: TargetIdentity,
    contract: AssessmentContract,
    sampling_digest: str,
    split: SplitManifest,
    floors: EvidenceFloors,
    profiles: list[CalibrationProfile],
    holdout_metrics: list[HoldoutMetrics],
    floor_result: EvidenceFloorResult,
    authoritative_recall_evidence_digest: Digest,
) -> CalibrationArtifactBundle:
    _index_profiles(profiles)
    if floor_result.sampling_manifest_digest != sampling_digest:
        raise ValueError("floor_sampling_manifest_mismatch")
    if floor_result.split_manifest_digest != split.split_digest():
        raise ValueError("floor_split_manifest_mismatch")
    unsupported = sorted(
        {"/".join([m.dimension, m.stratum]) for m in holdout_metrics if not m.covered_stratum}
    )
    return CalibrationArtifactBundle(
        fitting_method="exact-stratum-reliability-bins-v1",
        calibration_version=identity.calibration_dataset_version,
        target=identity.model_dump(mode="json"),
        target_identity_digest=identity.identity_digest(),
        sampling_manifest_digest=sampling_digest,
        split_manifest_digest=split.split_digest(),
        floors=floors.model_dump(mode="json"),
        profiles=[p.model_dump(mode="json") for p in profiles],
        holdout_metrics=[m.model_dump(mode="json") for m in holdout_metrics],
        unsupported_strata=unsupported,
        floor_results=floor_result.model_dump(mode="json"),
        floors_satisfied=floor_result.passed,
        authoritative_recall_evidence_digest=authoritative_recall_evidence_digest,
    )


def _profile_key(profile: CalibrationProfile) -> tuple[str, str, str, str, str]:
    return (
        profile.dimension,
        profile.source_type,
        profile.assertion_mode,
        profile.kind,
        profile.risk,
    )


def _observation_key(obs: LabeledObservation) -> tuple[str, str, str, str, str]:
    return (obs.dimension, obs.source_type, obs.assertion_mode, obs.kind, obs.risk)


def check_floors(
    *,
    floors: EvidenceFloors,
    ledger_path: Path,
    expected_ledger_sha256: str,
    reviewer_a_packet_sha256: str,
    reviewer_b_packet_sha256: str,
    expected_dataset_id: str,
    expected_dataset_version: str,
    assessment_evidence_path: Path,
    expected_assessment_evidence_sha256: str,
    target_identity: TargetIdentity,
    assessment_contract: AssessmentContract,
    frame: list[FrameRow],
    profiles: list[CalibrationProfile],
    sampling: SamplingManifest,
    split: SplitManifest,
) -> EvidenceFloorResult:
    """Evaluate every frozen floor from reviewer, split, and fitted-profile evidence."""
    validate_split_membership(sampling, split)
    verified_ledger = verify_ledger(
        ledger_path,
        expected_ledger_sha256,
        sampling=sampling,
        reviewer_a_packet_sha256=reviewer_a_packet_sha256,
        reviewer_b_packet_sha256=reviewer_b_packet_sha256,
        expected_dataset_id=expected_dataset_id,
        expected_dataset_version=expected_dataset_version,
    )
    reviewed_records = list(verified_ledger.records)
    record_ids = [record.sample_id for record in reviewed_records]
    if len(record_ids) != len(set(record_ids)):
        raise ValueError("duplicate_review_record")
    if set(record_ids) != set(sampling.sample_ids):
        raise ValueError("review_records_do_not_match_frozen_sample")
    observations = _verify_observation_evidence(
        assessment_evidence_path,
        expected_assessment_evidence_sha256,
        sampling=sampling,
        target_identity=target_identity,
        assessment_contract=assessment_contract,
        split=split,
        frame=frame,
        reviewed_records=reviewed_records,
    )
    _validate_observation_membership(observations, split)
    completed = [
        record
        for record in reviewed_records
        if record.label_origin == "human_adjudicated"
        and record.review_stage == "complete"
        and record.final_dimensions() is not None
    ]
    completed_ids = {record.sample_id for record in completed}
    labeled = [
        obs
        for obs in observations
        if obs.sample_id in completed_ids and obs.outcome != "unknown" and obs.raw_value is not None
    ]
    by_dimension = {
        dimension: len({obs.sample_id for obs in labeled if obs.dimension == dimension})
        for dimension in ("taxonomy", "retention", "epistemic")
    }
    non_unknown_fraction = {
        dimension: (by_dimension[dimension] / len(completed_ids) if completed_ids else 0.0)
        for dimension in ("taxonomy", "retention", "epistemic")
    }
    holdout_ids = {
        obs.sample_id
        for obs in observations
        if obs.split == "holdout" and obs.sample_id in completed_ids
    }
    holdout_labeled = {
        dimension: len(
            {
                obs.sample_id
                for obs in labeled
                if obs.split == "holdout" and obs.dimension == dimension
            }
        )
        for dimension in ("taxonomy", "retention", "epistemic")
    }
    high_records = [
        record
        for record in reviewed_records
        if (record.final_dimensions() or record.reviewer_a.dimensions).consequence == "high"
    ]
    high_completed = [record for record in completed if record in high_records]
    dual_complete = all(
        record.reviewer_b is not None
        and record.reviewer_b.adjudicator_ref != record.reviewer_a.adjudicator_ref
        and record.disagreement != "unresolved"
        for record in high_records
    )

    stratum_support: dict[str, dict[str, Any]] = {}
    bin_support: dict[str, dict[str, Any]] = {}
    profile_keys = {_profile_key(profile) for profile in profiles}
    observed_keys = {_observation_key(obs) for obs in labeled if obs.split == "dev"}
    for key in sorted(profile_keys | observed_keys):
        name = "/".join(key)
        count = len(
            {
                obs.sample_id
                for obs in labeled
                if obs.split == "dev" and _observation_key(obs) == key
            }
        )
        claimed_supported = key in profile_keys
        stratum_support[name] = {
            "labeled_dev": count,
            "required": floors.per_stratum_min,
            "claimed_supported": claimed_supported,
            "supported": claimed_supported and count >= floors.per_stratum_min,
        }
    for profile in profiles:
        name = "/".join(_profile_key(profile))
        for index, bucket in enumerate(profile.bins):
            bin_name = f"{name}/bin-{index}"
            claimed_supported = bucket.count > 0
            bin_support[bin_name] = {
                "fitted_count": bucket.count,
                "required": floors.per_bin_support_min,
                "claimed_supported": claimed_supported,
                "supported": claimed_supported and bucket.count >= floors.per_bin_support_min,
            }

    dimension_support: dict[str, dict[str, Any]] = {}
    for dimension in ("taxonomy", "retention", "epistemic"):
        names = [
            name
            for name, result in stratum_support.items()
            if name.startswith(f"{dimension}/") and result["claimed_supported"]
        ]
        bins = [
            name
            for name, result in bin_support.items()
            if name.startswith(f"{dimension}/") and result["claimed_supported"]
        ]
        supported = (
            by_dimension[dimension] >= floors.per_dimension_labeled_min
            and non_unknown_fraction[dimension] >= floors.per_dimension_non_unknown_fraction_min
            and bool(names)
            and bool(bins)
            and all(stratum_support[name]["supported"] for name in names)
            and all(bin_support[name]["supported"] for name in bins)
        )
        dimension_support[dimension] = {
            "labeled": by_dimension[dimension],
            "required": floors.per_dimension_labeled_min,
            "non_unknown_fraction": non_unknown_fraction[dimension],
            "non_unknown_fraction_required": floors.per_dimension_non_unknown_fraction_min,
            "profile_count": sum(key[0] == dimension for key in profile_keys),
            "supported": supported,
        }

    high_labeled_by_dimension = {
        dimension: len(
            {
                obs.sample_id
                for obs in labeled
                if obs.split == "dev" and obs.consequence == "high" and obs.dimension == dimension
            }
        )
        for dimension in ("taxonomy", "retention", "epistemic")
    }
    required_high_labeled = min(floors.high_consequence_reviewed_min, floors.per_stratum_min)
    high_strata: dict[tuple[str, str, str, str, str], set[str]] = defaultdict(set)
    for obs in labeled:
        if (
            obs.split == "dev"
            and obs.consequence == "high"
            and _observation_key(obs) in profile_keys
        ):
            high_strata[_observation_key(obs)].add(obs.sample_id)
    high_strata_supported = floors.high_consequence_reviewed_min == 0 or (
        bool(high_strata)
        and all(len(sample_ids) >= floors.per_stratum_min for sample_ids in high_strata.values())
    )
    claimed_bins = [result for result in bin_support.values() if result["claimed_supported"]]
    claimed_strata = [result for result in stratum_support.values() if result["claimed_supported"]]
    holdout_support_required = min(floors.holdout_min, floors.per_stratum_min)

    checks = {
        "total_reviewed": len(completed_ids) >= floors.total_reviewed_min,
        "per_dimension_labeled": all(
            count >= floors.per_dimension_labeled_min for count in by_dimension.values()
        ),
        "per_dimension_non_unknown_fraction": all(
            value >= floors.per_dimension_non_unknown_fraction_min
            for value in non_unknown_fraction.values()
        ),
        "holdout_size": len(holdout_ids) >= floors.holdout_min,
        "holdout_labeled_support": all(
            count >= holdout_support_required for count in holdout_labeled.values()
        ),
        "high_consequence": len(high_completed) >= floors.high_consequence_reviewed_min,
        "high_consequence_labeled_support": all(
            count >= required_high_labeled for count in high_labeled_by_dimension.values()
        ),
        "dual_review_high_consequence": (not floors.dual_review_high_consequence) or dual_complete,
        "bin_support": bool(claimed_bins) and all(result["supported"] for result in claimed_bins),
        "per_stratum_support": bool(claimed_strata)
        and all(result["supported"] for result in claimed_strata),
        "high_consequence_strata_support": high_strata_supported,
        "all_dimensions_explicitly_supported": all(
            result["supported"] for result in dimension_support.values()
        ),
    }
    failures = tuple(sorted(name for name, passed in checks.items() if not passed))
    return EvidenceFloorResult(
        sampling_manifest_digest=sampling.manifest_digest(),
        split_manifest_digest=split.split_digest(),
        ledger_sha256=verified_ledger.ledger_sha256,
        reviewer_a_packet_sha256=verified_ledger.reviewer_a_packet_sha256,
        reviewer_b_packet_sha256=verified_ledger.reviewer_b_packet_sha256,
        assessment_evidence_sha256=expected_assessment_evidence_sha256,
        assessment_contract_digest=digest(assessment_contract.model_dump(mode="json")),
        full_population_dual_review=True,
        checks=checks,
        dimension_support=dimension_support,
        stratum_support=stratum_support,
        bin_support=bin_support,
        failures=failures,
        passed=not failures,
    )
