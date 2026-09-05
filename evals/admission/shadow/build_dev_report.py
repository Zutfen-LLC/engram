"""Compute the public development comparison report for #162C.

Replays the protected 50-case #162B corpus through current + candidate
policies and writes only aggregate, content-free results. Determinism is
proven by double replay + byte comparison before anything is committed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from evals.admission.schema import digest


def build_public_report(report: dict[str, Any]) -> dict[str, Any]:
    per_policy: dict[str, Any] = {}
    for name, metrics in report["metrics_by_policy"].items():
        per_policy[name] = {
            "storage": metrics["storage"],
            "automatic_admission": metrics["automatic_admission"],
            "high_consequence": metrics["high_consequence"],
            "governed_semantic_eligibility": metrics["governed_semantic_eligibility"],
            "startup_eligibility": metrics["startup_eligibility"],
            "human_review_burden": metrics["human_review_burden"],
            "unknown_abstention": metrics["unknown_abstention"],
            "cost_weighted_errors": metrics["cost_weighted_errors"],
            "reason_code_inventory": sorted(
                {
                    reason
                    for case in report["per_case"]
                    for reason in case["candidates"].get(name, {}).get(
                        "reason_codes", []
                    )
                }
            )
            if name != "current"
            else ["p0_mirror_of_current"],
            "digest": metrics["digest"],
        }
    # Pareto view over candidates (objectives lower-is-better)
    points: dict[str, dict[str, float]] = {}
    for name in per_policy:
        if name == "current":
            continue
        metrics = report["metrics_by_policy"][name]
        points[name] = {
            "high_consequence_false_auto": float(
                metrics["high_consequence"]["false_automatic_positives"]
            ),
            "false_auto_low_medium": float(
                metrics["cost_weighted_errors"]["counts"]["low_medium_false_auto"]
            ),
            "useful_held": float(metrics["storage"]["useful_memory_held_back"]),
            "unnecessary_review": float(
                metrics["human_review_burden"]["unnecessary_review_burden"]
            ),
            "cost_weighted_total": float(
                metrics["cost_weighted_errors"]["weighted_total"]
            ),
        }
    from evals.admission.shadow.metrics import pareto_frontier

    pareto = pareto_frontier(
        points,
        (
            "high_consequence_false_auto",
            "false_auto_low_medium",
            "useful_held",
            "unnecessary_review",
            "cost_weighted_total",
        ),
    )
    return {
        "report_schema_version": "engram-162c-development-comparison-v1",
        "dataset_role": "dogfood-development-v1",
        "dataset_role_classification": (
            "development / hypothesis-generating; corpus informed candidate "
            "design; NOT unbiased certification; #162D owns gates"
        ),
        "runner_version": report["runner_version"],
        "freeze_digest": report["freeze_digest"],
        "snapshot_digest": report["snapshot_digest"],
        "tranche_selection_digest": report["tranche_selection_digest"],
        "final_corpus_digest": report["final_corpus_digest"],
        "code_sha": report["code_sha"],
        "evaluation_at": report["evaluation_at"],
        "n_cases": report["n_cases"],
        "per_policy": per_policy,
        "pareto": {
            "objectives_lower_is_better": pareto["objectives_lower_is_better"],
            "frontier": pareto["pareto_frontier"],
            "dominated_by": pareto["dominated_by"],
            "points": points,
        },
        "limitations": [
            "Development corpus: candidate structure was designed after seeing "
            "#162B findings; these numbers cannot certify generalization.",
            "Automatic-admission PPV is undefined (null) for every policy: all "
            "predicted zero automatic positives on this tranche.",
            "No high-consequence automatic admission occurred under any "
            "candidate; safety results are necessary-not-sufficient evidence.",
            "Cost weights are predeclared ordinal judgments (schedule v1); "
            "#162D owns any rollout gate.",
        ],
        "private_note": (
            "Private per-case record: /protected outside Git; this report is "
            "content-free."
        ),
    }


def main() -> int:
    private_report_path = Path(sys.argv[1])
    output = Path(sys.argv[2])
    report = json.loads(private_report_path.read_text())
    public = build_public_report(report)
    public["report_digest"] = digest(
        {k: v for k, v in public.items() if k != "report_digest"}
    )
    output.write_text(json.dumps(public, sort_keys=True, indent=2) + "\n")
    print(json.dumps(public["pareto"], indent=1)[:600])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
