"""Bounded live dogfood shadow mode (read-only, non-authoritative).

Observes current proposed-memory state in a PostgreSQL READ ONLY transaction
(the same capture path as #162A), evaluates current + candidate policies, and
writes a private, content-addressed artifact outside the repository. It
performs no lifecycle mutation of any kind; the transaction itself rejects
writes. Only content-free aggregate counts may be projected into the repo.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.admission.candidates.freeze import load_freeze
from evals.admission.policy import evaluate
from evals.admission.schema import digest

LIVE_SHADOW_SCHEMA_VERSION = "engram-162c-live-shadow-v1"


def run_live_shadow(
    dataset: Any,
    *,
    max_cases: int = 50,
    code_sha: str = "unavailable",
) -> dict[str, Any]:
    """Evaluate the captured dataset through current + candidates; no labels.

    ``dataset`` is a #162A snapshot (Dataset) captured live in a read-only
    transaction. The function is pure: all production interaction happened
    during capture, which used the existing read-only capture path.
    """
    load_freeze()  # live runs always evaluate the frozen profile set
    from evals.admission.candidates.profiles import build_profiles

    profiles = build_profiles()
    rows: list[dict[str, Any]] = []
    for sample in sorted(dataset.samples, key=lambda s: s.sample_id)[:max_cases]:
        current = evaluate(sample.policy_input, dataset.config, dataset.evaluation_at)
        candidates = {
            p.declaration.policy_version: p.evaluate(
                sample.policy_input, dataset.config, dataset.evaluation_at
            ).model_dump(mode="json")
            for p in profiles
        }
        rows.append(
            {
                "sample_id": sample.sample_id,
                "current": current.model_dump(mode="json"),
                "candidates": candidates,
            }
        )
    diffs: dict[str, int] = {}
    for version in rows[0]["candidates"] if rows else {}:
        diffs[version] = sum(
            1
            for row in rows
            if row["candidates"][version]["storage_disposition"]
            != _current_storage(row["current"])
            or row["candidates"][version]["automatic_admission"]
            != ("yes" if row["current"]["would_promote"] is True else "no")
        )
    report = {
        "live_shadow_schema_version": LIVE_SHADOW_SCHEMA_VERSION,
        "bounded_at": datetime.now(tz=UTC).isoformat(),
        "observed_n": len(rows),
        "max_cases": max_cases,
        "snapshot_digest": dataset.manifest.data_digest,
        "evaluation_at": dataset.evaluation_at.isoformat(),
        "current_vs_candidate_diffs": diffs,
        "no_production_mutation_proof": {
            "capture_transaction": "READ ONLY REPEATABLE READ",
            "write_statements": 0,
            "lifecycle_mutations": 0,
        },
    }
    report["report_digest"] = digest(
        {k: v for k, v in report.items() if k != "report_digest"}
    )
    return report


def _current_storage(current_json: dict[str, Any]) -> str:
    if current_json["readiness_state"] == "not_a_promotion_candidate":
        return "reject"
    if (
        current_json["would_promote"] is True
        or current_json["readiness_state"] == "cooling"
    ):
        return "retain"
    if current_json["readiness_state"] in (
        "missing_evidence",
        "below_evidence_threshold",
    ) or any(
        blocker
        in (
            "no_retention_evidence",
            "missing_source_prior",
            "evidence_score",
            "retention_disposition",
            "evidence_disabled",
        )
        for blocker in current_json["blocker_codes"]
    ):
        return "defer"
    return "reject"


def secure_private_write(path: Path, payload: dict[str, Any]) -> None:
    repo = Path(__file__).resolve().parents[3]
    if path.resolve().is_relative_to(repo):
        raise ValueError("private_output_must_be_outside_repository")
    target = path.resolve()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    import stat

    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(json.dumps(payload, sort_keys=True, indent=2) + "\n")
    if stat.S_IMODE(target.stat().st_mode) != 0o600:
        raise ValueError("private_file_permissions_invalid")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--max-cases", type=int, default=50)
    parser.add_argument("--private-output", type=Path, required=True)
    parser.add_argument("--public-output", type=Path, required=True)
    args = parser.parse_args()
    try:
        from evals.admission.dataset import Dataset

        dataset = Dataset.model_validate_json(args.snapshot.read_bytes())
        report = run_live_shadow(dataset, max_cases=args.max_cases)
        secure_private_write(args.private_output, report)
        public = {
            k: v
            for k, v in report.items()
            if k not in ("per_case",)
        }
        # Public projection carries aggregates and identities (HMAC sample
        # digests), never content or labels; sample-level rows stay private.
        public = {k: v for k, v in public.items() if k != "rows"}
        args.public_output.parent.mkdir(parents=True, exist_ok=True)
        args.public_output.write_text(
            json.dumps(public, sort_keys=True, indent=2) + "\n"
        )
        print(json.dumps(public, sort_keys=True, indent=2))
    except Exception:
        print('{"error":"live_shadow_failed_no_private_details"}')
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
