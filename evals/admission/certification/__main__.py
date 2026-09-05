"""Command-line interface for the #162D certification workflow.

Every subcommand writes private artifacts ONLY to paths outside the
repository (enforced), mode 0600, and fails closed with opaque errors so
private dogfood content never reaches logs or Git.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from evals.admission.certification.doctrine import load_doctrine, write_doctrine
from evals.admission.certification.review import (
    assert_certification_reveal_gate,
    expand_certification_reviewer_a,
    finalize_certification_corpus,
    write_private,
)
from evals.admission.certification.runner import (
    public_projection,
    run_certification,
)
from evals.admission.certification.select import select_certification_corpus
from evals.admission.dataset import Dataset


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("aware_datetime_required")
    return parsed


def _read_dataset(path: Path) -> Dataset:
    return Dataset.model_validate_json(path.read_bytes())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    freeze = sub.add_parser(
        "write-doctrine", help="write the versioned certification doctrine artifact"
    )
    freeze.add_argument("--code-sha", required=True)
    freeze.add_argument("--frozen-at", type=_aware, required=True)

    select_cmd = sub.add_parser("select-corpus", help="select the fresh N=100 corpus")
    select_cmd.add_argument("--snapshot", type=Path, required=True)
    select_cmd.add_argument("--development-tranche", type=Path, required=True)
    select_cmd.add_argument("--holdout-manifest", type=Path, required=True)
    select_cmd.add_argument("--prior-snapshot", type=Path, default=None)
    select_cmd.add_argument("--seed", required=True)
    select_cmd.add_argument("--code-sha", required=True)
    select_cmd.add_argument("--snapshot-key-file", type=Path, required=True)
    select_cmd.add_argument("--output", type=Path, required=True)

    expand_a = sub.add_parser(
        "expand-reviewer-a", help="validate + freeze the Reviewer A ledger"
    )
    expand_a.add_argument("--packet", type=Path, required=True)
    expand_a.add_argument("--ledger", type=Path, required=True)
    expand_a.add_argument("--frozen-at", type=_aware, required=True)
    expand_a.add_argument("--output", type=Path, required=True)

    finalize = sub.add_parser(
        "finalize-corpus", help="seal the dual-reviewed final corpus"
    )
    finalize.add_argument("--packet", type=Path, required=True)
    finalize.add_argument("--reviewer-a-frozen", type=Path, required=True)
    finalize.add_argument("--reviewer-b-ledger", type=Path, required=True)
    finalize.add_argument("--adjudication", type=Path, required=True)
    finalize.add_argument("--frozen-at", type=_aware, required=True)
    finalize.add_argument("--output", type=Path, required=True)

    run = sub.add_parser("run", help="deterministic certification evaluation")
    run.add_argument("--snapshot", type=Path, required=True)
    run.add_argument("--corpus", type=Path, required=True)
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--private-output", type=Path, required=True)
    run.add_argument("--public-output", type=Path, required=True)

    args = parser.parse_args()
    try:
        if args.command == "write-doctrine":
            value = write_doctrine(code_sha=args.code_sha, frozen_at=args.frozen_at)
            print(json.dumps({"doctrine_digest": value}))
        elif args.command == "select-corpus":
            key = args.snapshot_key_file.read_bytes().strip()
            prior = _read_dataset(args.prior_snapshot) if args.prior_snapshot else None
            manifest = select_certification_corpus(
                _read_dataset(args.snapshot),
                args.development_tranche,
                args.holdout_manifest,
                seed=args.seed,
                code_sha=args.code_sha,
                snapshot_key=key,
                prior_snapshot=prior,
                frozen_at=None,
            )
            write_private(args.output, manifest)
            print(
                json.dumps(
                    {
                        "final_n": manifest["final_n"],
                        "shortfall": manifest["shortfall"],
                        "digest": manifest["certification_manifest_digest"],
                        "all_disjoint": manifest["overlap_proof"]["all_disjoint"],
                    }
                )
            )
        elif args.command == "expand-reviewer-a":
            packet = json.loads(args.packet.read_text())
            ledger = json.loads(args.ledger.read_text())
            frozen = expand_certification_reviewer_a(
                packet, ledger, frozen_at=args.frozen_at
            )
            write_private(args.output, frozen)
            print(json.dumps({"frozen_digest": frozen["frozen_digest"]}))
        elif args.command == "finalize-corpus":
            corpus = finalize_certification_corpus(
                json.loads(args.packet.read_text()),
                json.loads(args.reviewer_a_frozen.read_text()),
                json.loads(args.reviewer_b_ledger.read_text()),
                json.loads(args.adjudication.read_text()),
                frozen_at=args.frozen_at,
            )
            write_private(args.output, corpus)
            print(
                json.dumps(
                    {
                        "final_corpus_digest": corpus["final_corpus_digest"],
                        "cases": corpus["summary"]["case_count"],
                        "reviewer_b_count": corpus["summary"]["reviewer_b_count"],
                    }
                )
            )
        elif args.command == "run":
            corpus = json.loads(args.corpus.read_text())
            manifest = json.loads(args.manifest.read_text())
            report = run_certification(
                _read_dataset(args.snapshot), corpus, manifest
            )
            # determinism proof at run time: evaluate twice, byte-compare
            again = run_certification(
                _read_dataset(args.snapshot), corpus, manifest
            )
            if report != again:
                raise ValueError("nondeterministic_certification_run")
            from evals.admission.certification.runner import write_private_results

            write_private_results(report, args.private_output)
            args.public_output.write_text(
                json.dumps(public_projection(report), sort_keys=True, indent=2) + "\n"
            )
            print(
                json.dumps(
                    {
                        "decision": report["decision"]["terminal_status"],
                        "gates": report["gates"],
                        "report_digest": report["report_digest"],
                    }
                )
            )
        else:  # pragma: no cover
            parser.error("unknown_command")
        _ = load_doctrine  # referenced for CLI-adjacent validation parity
        _ = assert_certification_reveal_gate
        return 0
    except Exception:
        # Private inputs may contain dogfood content; never echo them.
        print('{"error":"certification_cli_failed"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
