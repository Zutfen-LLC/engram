"""Calibration contract tests through the public calibration interface."""

import pytest

from engram.assessment_calibration import CalibrationProfile, calibrate
from engram.assessment_schema import AssessmentContract


def test_calibration_requires_exact_contract_and_sufficient_samples():
    contract = AssessmentContract(
        provider="openai",
        model="model-a",
        config_version="fixture",
        calibration_version="fixture-v1",
    )
    profile = CalibrationProfile(
        version="fixture-v1",
        contract=contract,
        dataset_version="labeled-v1",
        dimension="retention",
        source_type="manual",
        assertion_mode="unknown",
        kind="fact",
        risk="unknown",
        bins=[{"lower": 0, "upper": 1, "value": 0.7, "count": 100}],
    )
    kwargs = dict(
        profile=profile,
        contract=contract,
        dimension="retention",
        source_type="manual",
        assertion_mode="unknown",
        kind="fact",
        risk="unknown",
    )
    result = calibrate(0.9, **kwargs)
    assert result.status == "calibrated"
    assert result.raw_value == 0.9
    assert result.calibrated_value == 0.7
    assert calibrate(None, **kwargs).calibrated_value is None
    assert calibrate(0, **kwargs).raw_value == 0
    kwargs["contract"] = contract.model_copy(update={"model": "model-b"})
    result = calibrate(0.9, **kwargs)
    assert result.status == "uncalibrated"
    assert result.calibrated_value is None
    kwargs["contract"] = contract
    profile.bins[0].count = 2
    assert calibrate(0.9, **kwargs).status == "uncalibrated"
    with pytest.raises(ValueError):
        calibrate(float("nan"), **kwargs)


def test_evaluation_reports_known_brier_error_abstention_and_confusion():
    from engram.assessment_calibration import CalibrationSample, calibration_report

    report = calibration_report(
        [
            CalibrationSample(
                raw_value=0.1, label=False, expected_kind="fact", predicted_kind="fact"
            ),
            CalibrationSample(
                raw_value=0.8, label=True, expected_kind="decision", predicted_kind="fact"
            ),
            CalibrationSample(
                raw_value=None, label=True, expected_kind="fact", predicted_kind=None
            ),
        ]
    )
    assert report["brier_score"] == pytest.approx(0.025)
    assert report["expected_calibration_error"] == pytest.approx(0.15)
    assert report["abstention_rate"] == pytest.approx(1 / 3)
    assert report["sample_count"] == 3
    assert report["probabilistic_sample_count"] == 2
    assert report["status"] == "uncalibrated"
    assert report["kind_confusion"]["decision"]["fact"] == 1
