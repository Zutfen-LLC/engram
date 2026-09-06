"""Exact-contract calibration. Small or mismatched strata remain uncalibrated."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, TypeAdapter, model_validator

from engram.assessment_schema import (
    AssertionMode,
    AssessmentContract,
    CalibratedScore,
    Risk,
    StrictModel,
)
from engram.source_types import SourceType

MIN_CALIBRATION_SAMPLES = 50


class CalibrationBin(StrictModel):
    lower: float = Field(ge=0, le=1)
    upper: float = Field(gt=0, le=1)
    value: float = Field(ge=0, le=1)
    count: int = Field(ge=0)


class CalibrationProfile(StrictModel):
    version: str
    contract: AssessmentContract
    dataset_version: str = Field(min_length=1)
    dimension: Literal["taxonomy", "retention", "epistemic"]
    source_type: SourceType
    assertion_mode: AssertionMode
    kind: str
    risk: Risk
    bins: list[CalibrationBin] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_bins(self) -> CalibrationProfile:
        previous = 0.0
        for bucket in self.bins:
            if bucket.lower != previous or bucket.upper <= bucket.lower:
                raise ValueError("calibration bins must partition [0, 1]")
            previous = bucket.upper
        if previous != 1:
            raise ValueError("calibration bins must partition [0, 1]")
        return self


def calibrate(
    raw_value: float | None,
    *,
    profile: CalibrationProfile | None,
    contract: AssessmentContract,
    dimension: str,
    source_type: str,
    assertion_mode: str,
    kind: str,
    risk: str,
) -> CalibratedScore:
    """Return separate raw and calibrated values for an exact matching stratum."""
    result = CalibratedScore(raw_value=raw_value)
    if raw_value is None or profile is None:
        return result
    if not math.isfinite(raw_value):
        raise ValueError("score must be finite")
    # A profile names the inference contract independently of its own version.
    excluded = {"calibration_version", "calibration_digest"}
    inference = contract.model_dump(exclude=excluded)
    if profile.contract.model_dump(exclude=excluded) != inference:
        return result
    if contract.calibration_version != profile.version:
        return result
    expected = (dimension, source_type, assertion_mode, kind, risk)
    actual = (
        profile.dimension,
        profile.source_type,
        profile.assertion_mode,
        profile.kind,
        profile.risk,
    )
    if expected != actual:
        return result
    for bucket in profile.bins:
        if bucket.lower <= raw_value < bucket.upper or raw_value == bucket.upper == 1:
            if bucket.count < MIN_CALIBRATION_SAMPLES:
                return result
            # Wilson 95% interval for the labeled outcome rate in this bin.
            n, p, z = bucket.count, bucket.value, 1.96
            denominator = 1 + z * z / n
            center = (p + z * z / (2 * n)) / denominator
            half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
            return CalibratedScore(
                raw_value=raw_value,
                status="calibrated",
                calibrated_value=bucket.value,
                calibrated_band=(max(0, center - half), min(1, center + half)),
                profile_version=profile.version,
                dataset_version=profile.dataset_version,
            )
    return result


def load_profiles(path: str | None) -> list[CalibrationProfile]:
    """Read an operator-installed, bounded calibration artifact."""
    if path is None:
        return []
    with Path(path).open("rb") as source:
        data = source.read(65537)
    if len(data) > 65536:
        raise ValueError("calibration artifact exceeds 65536 bytes")
    return TypeAdapter(list[CalibrationProfile]).validate_json(data)


class CalibrationSample(StrictModel):
    raw_value: float | None = Field(default=None, ge=0, le=1)
    label: bool | None = None
    expected_kind: str
    predicted_kind: str | None = None


def calibration_report(samples: list[CalibrationSample]) -> dict[str, Any]:
    """Report held-out label metrics without fitting or installing a profile."""
    labeled = [s for s in samples if s.raw_value is not None and s.label is not None]
    bins: list[dict[str, Any]] = []
    weighted_error = 0.0
    brier = 0.0
    for index in range(10):
        lower, upper = index / 10, (index + 1) / 10
        bucket = [
            s
            for s in labeled
            if s.raw_value is not None
            and (lower <= s.raw_value < upper or s.raw_value == upper == 1)
        ]
        mean = (
            sum(float(s.raw_value) for s in bucket if s.raw_value is not None) / len(bucket)
            if bucket
            else None
        )
        frequency = sum(bool(s.label) for s in bucket) / len(bucket) if bucket else None
        if mean is not None and frequency is not None:
            weighted_error += len(bucket) * abs(mean - frequency)
        bins.append(
            {
                "lower": lower,
                "upper": upper,
                "count": len(bucket),
                "mean_raw_value": mean,
                "observed_frequency": frequency,
            }
        )
    for sample in labeled:
        assert sample.raw_value is not None
        brier += (sample.raw_value - float(bool(sample.label))) ** 2
    confusion: dict[str, dict[str, int]] = {}
    for sample in samples:
        row = confusion.setdefault(sample.expected_kind, {})
        predicted = sample.predicted_kind or "abstained"
        row[predicted] = row.get(predicted, 0) + 1
    return {
        "status": "uncalibrated",
        "sample_count": len(samples),
        "probabilistic_sample_count": len(labeled),
        "brier_score": brier / len(labeled) if labeled else None,
        "expected_calibration_error": weighted_error / len(labeled) if labeled else None,
        "abstention_rate": sum(s.raw_value is None for s in samples) / len(samples)
        if samples
        else None,
        "reliability_bins": bins,
        "kind_confusion": confusion,
        "minimum_profile_bin_samples": MIN_CALIBRATION_SAMPLES,
    }
