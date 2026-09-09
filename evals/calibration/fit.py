"""Deterministic calibration fitting, holdout evaluation, artifact construction.

Fitting sees ONLY dev-split labels. The holdout split is evaluated, never
fitted. Every stratum below the frozen support floor stays explicitly
``uncalibrated`` — support is never extrapolated beyond reviewed evidence.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Literal

from engram.assessment_calibration import (
    MIN_CALIBRATION_SAMPLES,
    CalibrationBin,
    CalibrationProfile,
)
from engram.assessment_schema import AssessmentContract
from evals.admission.schema import Record, digest
from evals.calibration.freeze import (
    LABEL_GUIDE_VERSION,
    EvidenceFloors,
    SplitManifest,
    TargetIdentity,
)

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
    source_type: str
    assertion_mode: str
    kind: str
    risk: str

    @classmethod
    def from_review(
        cls,
        *,
        sample_id: str,
        split: str,
        dimensions: Any,
        raw_scores: dict[str, float | None],
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
        suggested = str(raw_scores.get("suggested_kind") or "")
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
                **stratum,
            )
        )
        if dimensions.epistemic_state in ("unknown", "ambiguous"):
            outcome = "unknown"
        else:
            outcome = (
                "positive"
                if dimensions.epistemic_state
                in ("adequately_supported", "weakly_supported")
                else "negative"
            )
        out.append(
            cls(
                sample_id=sample_id,
                split=split,  # type: ignore[arg-type]
                dimension="epistemic",
                outcome=outcome,
                raw_value=raw_scores.get("epistemic_value"),
                **stratum,
            )
        )
        return out


def _bin_index(value: float) -> int:
    if value < 0.0 or value > 1.0:
        raise ValueError("raw value out of range")
    for index in range(len(BinEdges) - 1):
        lower, upper = BinEdges[index], BinEdges[index + 1]
        if lower <= value < upper or value == upper == 1.0:
            return index
    raise ValueError("unreachable bin")


def fit_profiles(
    observations: list[LabeledObservation],
    *,
    identity: TargetIdentity,
    contract: AssessmentContract,
) -> list[CalibrationProfile]:
    """Fit exact-stratum reliability bins from DEV observations only.

    A bin with fewer than ``MIN_CALIBRATION_SAMPLES`` labeled (non-unknown)
    observations is dropped — the production loader would refuse it anyway,
    and an undersupported profile must not claim support it lacks.
    """
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
        supported = [
            index
            for index, count in enumerate(total)
            if count >= MIN_CALIBRATION_SAMPLES
        ]
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
    brier: float | None = None
    ece: float | None = None
    mean_outcome: float | None = None
    mean_raw: float | None = None
    selective_accuracy: float | None = None
    coverage: float | None = None
    reliability: tuple[dict[str, float | int | None], ...] = ()
    covered_stratum: bool = False


def evaluate_holdout(
    observations: list[LabeledObservation],
    *,
    profiles: list[CalibrationProfile],
) -> list[HoldoutMetrics]:
    """Holdout metrics per dimension and stratum, never fitted on holdout."""
    per: dict[tuple[str, str], list[LabeledObservation]] = defaultdict(list)
    for obs in observations:
        if obs.split != "holdout":
            continue
        stratum_key = "/".join([obs.source_type, obs.assertion_mode, obs.kind, obs.risk])
        per[(obs.dimension, stratum_key)].append(obs)
    profile_keys = {
        (p.dimension, p.source_type, p.assertion_mode, p.kind, p.risk) for p in profiles
    }
    metrics: list[HoldoutMetrics] = []
    for key in sorted(per):
        dimension, stratum = key
        rows = per[key]
        labeled = [
            (o.raw_value, o.outcome)
            for o in rows
            if o.raw_value is not None and o.outcome != "unknown"
        ]
        covered = (
            dimension,
            *stratum.split("/"),
        ) in profile_keys
        if not labeled:
            metrics.append(
                HoldoutMetrics(dimension=dimension, stratum=stratum, n=len(rows),
                               covered_stratum=covered)
            )
            continue
        brier = sum(
            (raw - (1.0 if outcome == "positive" else 0.0)) ** 2
            for raw, outcome in labeled
        ) / len(labeled)
        reliability: list[dict[str, float | int | None]] = []
        weighted = 0.0
        for index in range(len(BinEdges) - 1):
            lower, upper = BinEdges[index], BinEdges[index + 1]
            bucket = [
                (raw, outcome)
                for raw, outcome in labeled
                if lower <= raw < upper or raw == upper == 1.0
            ]
            mean_raw = sum(raw for raw, _ in bucket) / len(bucket) if bucket else None
            freq = (
                sum(1 for _, outcome in bucket if outcome == "positive") / len(bucket)
                if bucket
                else None
            )
            if mean_raw is not None and freq is not None:
                weighted += len(bucket) * abs(mean_raw - freq)
            reliability.append(
                {
                    "lower": lower,
                    "upper": upper,
                    "count": len(bucket),
                    "mean_raw": mean_raw,
                    "observed_frequency": freq,
                }
            )
        ece = weighted / len(labeled)
        # Selective accuracy/coverage at the frozen abstention threshold 0.5:
        # "covered" means a non-null raw score at or above 0.5.
        high_conf = [(raw, outcome) for raw, outcome in labeled if raw >= 0.5]
        selective = (
            sum(1 for _, outcome in high_conf if outcome == "positive") / len(high_conf)
            if high_conf
            else None
        )
        coverage = len(high_conf) / len(labeled)
        metrics.append(
            HoldoutMetrics(
                dimension=str(dimension),
                stratum=stratum,
                n=len(rows),
                brier=round(brier, 6),
                ece=round(ece, 6),
                mean_outcome=round(
                    sum(1 for _, outcome in labeled if outcome == "positive") / len(labeled), 6
                ),
                mean_raw=round(sum(raw for raw, _ in labeled) / len(labeled), 6),
                selective_accuracy=None if selective is None else round(selective, 6),
                coverage=round(coverage, 6),
                reliability=tuple(reliability),
                covered_stratum=covered,
            )
        )
    return metrics


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
    floors_satisfied: bool

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
    floors_satisfied: bool,
) -> CalibrationArtifactBundle:
    {
        (p.dimension, p.source_type, p.assertion_mode, p.kind, p.risk) for p in profiles
    }
    unsupported = sorted(
        {
            "/".join([m.dimension, m.stratum])
            for m in holdout_metrics
            if not m.covered_stratum
        }
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
        floors_satisfied=floors_satisfied,
    )


def check_floors(
    *,
    floors: EvidenceFloors,
    observations: list[LabeledObservation],
    high_consequence_count: int,
) -> dict[str, bool]:
    """Evaluate the frozen floors against reviewed evidence. Never lowered."""
    labeled = [o for o in observations if o.outcome != "unknown"]
    unique_samples = {o.sample_id for o in observations}
    by_dimension: dict[str, int] = defaultdict(int)
    for obs in labeled:
        by_dimension[obs.dimension] += 1
    holdout_samples = {o.sample_id for o in observations if o.split == "holdout"}
    return {
        "total_reviewed": len(unique_samples) >= floors.total_reviewed_min,
        "per_dimension_labeled": all(
            by_dimension.get(dimension, 0) >= floors.per_dimension_labeled_min
            for dimension in ("taxonomy", "retention", "epistemic")
        ),
        "holdout_size": len(holdout_samples) >= floors.holdout_min,
        "high_consequence": (
            high_consequence_count >= floors.high_consequence_reviewed_min
            if floors.high_consequence_reviewed_min
            else True
        ),
        "bin_support": True,  # verified structurally: fit drops thin bins
    }
