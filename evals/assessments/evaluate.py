"""Report calibration metrics for an explicitly labeled evaluation artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from engram.assessment_calibration import CalibrationSample, calibration_report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = json.loads(args.dataset.read_text())
    samples = [CalibrationSample.model_validate(row) for row in data["samples"]]
    report = calibration_report(samples)
    report["dataset_version"] = data["dataset_version"]
    report["purpose"] = data["purpose"]
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
