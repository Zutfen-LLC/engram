"""Deterministic #162C metrics: current and candidate policies, separately.

Metric families: storage disposition, automatic admission, high-consequence
safety, governed semantic eligibility, startup eligibility, human-review
burden, unknown/abstention, cost-weighted errors, and a Pareto frontier view.

Semantic contracts enforced here:
- zero predicted positives => PPV null (never 100%);
- ``defer`` is never counted as retain or reject;
- governed / startup eligibility are scored independently;
- ``governed=yes + review=yes`` is reported as a distinct class and never as
  silent automatic admission;
- unknown outcomes are excluded only into clearly labeled buckets;
- cost weights come from a versioned, predeclared schedule.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from evals.admission.candidates.contract import CandidateResult
from evals.admission.policy import PolicyEvaluationResult
from evals.admission.schema import digest

STORAGE_LEVELS = ("retain", "defer", "reject", "unknown")


def precision(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def storage_metrics(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Confusion + per-disposition precision/recall against human truth.

    ``rows`` items: candidate disposition under ``pred``, human expected
    storage disposition under ``truth``. ``unknown`` predictions stay in their
    own bucket and never silently leave the denominator.
    """
    n = len(rows)
    confusion = {
        pred: {truth: 0 for truth in STORAGE_LEVELS} for pred in STORAGE_LEVELS
    }
    for row in rows:
        confusion[row["pred"]][row["truth"]] += 1
    per_level: dict[str, dict[str, Any]] = {}
    for level in STORAGE_LEVELS:
        pred_pos = sum(confusion[level].values())
        truth_pos = sum(confusion[p][level] for p in STORAGE_LEVELS)
        tp = confusion[level][level]
        per_level[level] = {
            "predicted": pred_pos,
            "human_truth": truth_pos,
            "true_positive": tp,
            "precision": precision(tp, pred_pos),
            "recall": precision(tp, truth_pos),
        }
    held_back_useful = sum(
        1
        for row in rows
        if row["truth"] == "retain" and row["pred"] in ("reject", "defer", "unknown")
    )
    return {
        "n": n,
        "confusion": confusion,
        "per_disposition": per_level,
        "accuracy": precision(
            sum(confusion[p][p] for p in STORAGE_LEVELS), n
        ),
        "useful_memory_held_back": held_back_useful,
    }


def admission_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Automatic-admission PPV and held-back-safe counts, null at zero positives."""
    predicted_pos = [row for row in rows if row["pred"] == "yes"]
    false_auto = [row for row in predicted_pos if not row["human_permits_auto"]]
    return {
        "n": len(rows),
        "predicted_automatic_positives": len(predicted_pos),
        "permitted_automatic_positives": len(predicted_pos) - len(false_auto),
        "false_automatic_admissions": len(false_auto),
        "ppv": precision(
            len(predicted_pos) - len(false_auto), len(predicted_pos)
        ),
        "held_back_safe_auto": sum(
            1
            for row in rows
            if row["pred"] != "yes"
            and row["human_permits_auto"]
            and row["unknown_pred"] is False
        ),
        "unknown_abstentions": sum(1 for row in rows if row["unknown_pred"]),
    }


def high_consequence_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """High-consequence slice, reported separately and fail-loud."""
    subset = [row for row in rows if row["consequence"] == "high"]
    predicted_pos = [row for row in subset if row["pred"] == "yes"]
    false_auto = [row for row in predicted_pos if not row["human_permits_auto"]]
    return {
        "n": len(subset),
        "automatic_positives": len(predicted_pos),
        "false_automatic_positives": len(false_auto),
        "review_routed": sum(1 for row in subset if row["review"] == "yes"),
        "abstained_unknown": sum(1 for row in subset if row["unknown_pred"]),
        "ppv": precision(len(predicted_pos) - len(false_auto), len(predicted_pos)),
        "violation": any(
            row["pred"] == "yes" and row["consequence"] == "high"
            and not row["human_permits_auto"]
            for row in rows
        ),
    }


def eligibility_metrics(rows: list[dict[str, Any]], *, surface: str) -> dict[str, Any]:
    """Governed/startup eligibility precision/recall against human labels.

    ``governed=yes + review=yes`` pairs are counted separately so a review
    requirement can never masquerade as silent admission.
    """
    pred_key = surface
    truth_key = f"{surface}_truth"
    levels = ("yes", "no", "unknown")
    confusion = {p: {t: 0 for t in levels} for p in levels}
    for row in rows:
        confusion[row[pred_key]][row[truth_key]] += 1
    per_level = {}
    for level in levels:
        pred_pos = sum(confusion[level].values())
        truth_pos = sum(confusion[p][level] for p in levels)
        tp = confusion[level][level]
        per_level[level] = {
            "predicted": pred_pos,
            "human_truth": truth_pos,
            "true_positive": tp,
            "precision": precision(tp, pred_pos),
            "recall": precision(tp, truth_pos),
        }
    return {
        "n": len(rows),
        "confusion": confusion,
        "per_level": per_level,
        "yes_with_review_required": sum(
            1 for row in rows if row[pred_key] == "yes" and row["review"] == "yes"
        ),
        "false_eligible": sum(
            1
            for row in rows
            if row[pred_key] == "yes" and row[truth_key] == "no"
        ),
        "held_back_eligible": sum(
            1
            for row in rows
            if row[pred_key] in ("no", "unknown") and row[truth_key] == "yes"
        ),
    }


def review_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    review_routed = [row for row in rows if row["review"] == "yes"]
    high_routed = [row for row in review_routed if row["consequence"] == "high"]
    unnecessary = [
        row
        for row in review_routed
        if row["review_truth"] == "no"
        and row["truth"] == "retain"
        and row["consequence"] != "high"
        and row["permits_auto_if_reviewed"] is True
    ]
    recovered = sum(
        1 for row in review_routed if row["truth"] == "retain" and row["review_truth"] != "yes"
    )
    return {
        "review_routed": len(review_routed),
        "review_rate": precision(len(review_routed), len(rows)),
        "high_consequence_review_coverage": precision(
            len(high_routed),
            sum(1 for row in rows if row["consequence"] == "high"),
        ),
        "unnecessary_review_burden": len(unnecessary),
        "useful_recovery_per_additional_review": precision(
            recovered, len(review_routed)
        ),
    }


def unknown_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    unknown = [row for row in rows if row["unknown_pred"]]
    return {
        "unknown_count": len(unknown),
        "unknown_rate": precision(len(unknown), len(rows)),
        "reasons": dict(sorted(Counter(row["unknown_reason"] for row in unknown).items())),
        "unavailable_signals": dict(
            sorted(
                Counter(
                    signal for row in unknown for signal in row["unavailable_signals"]
                ).items()
            )
        ),
    }


#: Predeclared cost-weight schedule v1. Weights are ordinal severity judgments
#: fixed BEFORE any candidate outcome was observed; the sensitivity range
#: below is the only analysis permitted on alternatives until #162D.
COST_WEIGHT_SCHEDULE_V1: dict[str, Any] = {
    "schedule_version": "admission-cost-weights-v1",
    "weights": {
        "high_consequence_false_auto": 100.0,
        "low_medium_false_auto": 25.0,
        "useful_storage_held_indefinitely": 10.0,
        "unnecessary_review": 2.0,
        "startup_false_positive": 40.0,
        "governed_false_positive": 30.0,
        "reject_vs_retain_error": 15.0,
    },
    "provenance": (
        "ordinal severity fixed before evaluation; not fitted to any outcome; "
        "#162D owns final gates"
    ),
}

#: Small predeclared sensitivity grid: each weight is additionally evaluated
#: at half and double its schedule value, independently.
SENSITIVITY_FACTORS = (0.5, 2.0)


def cost_weighted_errors(rows: list[dict[str, Any]]) -> dict[str, Any]:
    w: dict[str, float] = COST_WEIGHT_SCHEDULE_V1["weights"]
    counts = {
        "high_consequence_false_auto": sum(
            1
            for row in rows
            if row["pred"] == "yes"
            and not row["human_permits_auto"]
            and row["consequence"] == "high"
        ),
        "low_medium_false_auto": sum(
            1
            for row in rows
            if row["pred"] == "yes"
            and not row["human_permits_auto"]
            and row["consequence"] in ("low", "medium", "unknown")
        ),
        "useful_storage_held_indefinitely": sum(
            1
            for row in rows
            if row["truth"] == "retain"
            and row["storage_pred"] in ("reject", "unknown")
        ),
        "unnecessary_review": sum(
            1
            for row in rows
            if row["review"] == "yes" and row["review_truth"] == "no"
        ),
        "startup_false_positive": sum(
            1
            for row in rows
            if row["startup"] == "yes" and row["startup_truth"] == "no"
        ),
        "governed_false_positive": sum(
            1
            for row in rows
            if row["governed"] == "yes" and row["governed_truth"] == "no"
        ),
        "reject_vs_retain_error": sum(
            1
            for row in rows
            if row["truth"] == "retain" and row["storage_pred"] == "reject"
        ),
    }
    total = sum(w[name] * count for name, count in counts.items())
    sensitivity: dict[str, float] = {}
    for factor in SENSITIVITY_FACTORS:
        scaled = sum(
            w[name] * (factor if name == "high_consequence_false_auto" else 1.0) * count
            for name, count in counts.items()
        )
        sensitivity[f"high_consequence_weight_x{factor}"] = scaled
    return {
        "schedule_version": COST_WEIGHT_SCHEDULE_V1["schedule_version"],
        "counts": counts,
        "weighted_total": total,
        "sensitivity_high_consequence_weight": sensitivity,
    }


def pareto_frontier(
    points: dict[str, dict[str, float]], objectives: tuple[str, ...]
) -> dict[str, Any]:
    """Dominance view over named candidates; lower is better for every objective."""
    names = list(points)
    dominated_by: dict[str, list[str]] = {name: [] for name in names}
    for a in names:
        for b in names:
            if a == b:
                continue
            if all(points[b][o] <= points[a][o] for o in objectives) and any(
                points[b][o] < points[a][o] for o in objectives
            ):
                dominated_by[a].append(b)
    frontier = [name for name in names if not dominated_by[name]]
    return {
        "objectives_lower_is_better": list(objectives),
        "pareto_frontier": sorted(frontier),
        "dominated_by": {name: sorted(ways) for name, ways in dominated_by.items() if ways},
    }


def evaluate_rows_for_policy(
    *,
    policy_label: str,
    current: PolicyEvaluationResult,
    candidate: CandidateResult,
    dimensions: dict[str, Any],
) -> dict[str, Any]:
    """Build one metric-ready row from current/candidate output + human truth.

    The only place human truth enters — exclusively as the evaluation target,
    never as policy input (candidates ran before this function is called).
    """
    permits_auto = (
        dimensions["expected_storage_disposition"] == "retain"
        and dimensions["expected_startup_eligibility"] == "yes"
        and dimensions["expected_governed_semantic_eligibility"] == "yes"
        and dimensions["human_review_required"] == "no"
    )
    return {
        "policy": policy_label,
        "pred": candidate.automatic_admission,
        "unknown_pred": candidate.automatic_admission == "unknown",
        "unknown_reason": (
            candidate.reason_codes[0]
            if candidate.automatic_admission == "unknown" and candidate.reason_codes
            else None
        ),
        "unavailable_signals": list(candidate.unavailable_signals),
        "storage_pred": candidate.storage_disposition,
        "review": candidate.human_review_required,
        "governed": candidate.governed_semantic_eligibility,
        "startup": candidate.startup_eligibility,
        "truth": dimensions["expected_storage_disposition"],
        "consequence": dimensions["consequence"],
        "review_truth": dimensions["human_review_required"],
        "governed_truth": dimensions["expected_governed_semantic_eligibility"],
        "startup_truth": dimensions["expected_startup_eligibility"],
        "retention_truth": dimensions["retention_value"],
        "human_permits_auto": permits_auto,
        "permits_auto_if_reviewed": (
            dimensions["expected_storage_disposition"] == "retain"
            and dimensions["expected_startup_eligibility"] == "yes"
            and dimensions["expected_governed_semantic_eligibility"] == "yes"
            and dimensions["human_review_required"] != "yes"
        ),
        "current_would_promote": current.would_promote,
    }


def per_policy_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    storage_rows = [
        {"pred": row["storage_pred"], "truth": row["truth"]} for row in rows
    ]
    return {
        "storage": storage_metrics(storage_rows),
        "automatic_admission": admission_metrics(rows),
        "high_consequence": high_consequence_metrics(rows),
        "governed_semantic_eligibility": eligibility_metrics(rows, surface="governed"),
        "startup_eligibility": eligibility_metrics(rows, surface="startup"),
        "human_review_burden": review_metrics(rows),
        "unknown_abstention": unknown_metrics(rows),
        "cost_weighted_errors": cost_weighted_errors(rows),
        "digest": digest({"rows": rows}),
    }
