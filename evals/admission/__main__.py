"""Validate an artifact and print a content-free baseline."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from evals.admission.dataset import Dataset
from evals.admission.report import report, result_artifact


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--results", type=Path)
    args = parser.parse_args()
    try:
        dataset = Dataset.model_validate_json(args.dataset.read_bytes())
        if args.results:
            target = args.results.resolve()
            if dataset.manifest.privacy_class.startswith("private_") and target.is_relative_to(
                Path(__file__).resolve().parents[2]
            ):
                raise ValueError("private_output_must_be_outside_repository")
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as output:
                output.write(json.dumps(result_artifact(dataset), sort_keys=True, indent=2) + "\n")
        print(json.dumps(report(dataset), sort_keys=True, indent=2))
    except Exception:
        # Validation exceptions can contain private input. Never print them.
        print('{"error":"invalid_dataset_or_evaluation"}')
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
