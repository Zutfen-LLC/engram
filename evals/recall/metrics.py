"""Pure, deterministic metrics for recall evaluation reports."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Any


def _item_ids(packet: dict[str, Any]) -> list[str]:
    return [str(item["id"]) for item in packet["items"]]


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def build_packet_change_metrics(
    legacy: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    """Compare one candidate packet against its legacy control packet."""
    legacy_ids = _item_ids(legacy)
    candidate_ids = _item_ids(candidate)
    legacy_set = set(legacy_ids)
    candidate_set = set(candidate_ids)
    intersection = legacy_set & candidate_set
    union = legacy_set | candidate_set
    membership_changed = legacy_set != candidate_set
    ordering_only = not membership_changed and legacy_ids != candidate_ids
    omissions = Counter(candidate.get("omitted_by_admission", {}))
    # The exact frozen recall-packing-v1 summary vocabulary: packet packing
    # summaries publish omission counts under "omitted" (see
    # engram.recall_packing.PackingResult.summary). There is deliberately no
    # production alias for "omitted_by_reason" — the evaluator consumes the
    # frozen production contract as published.
    packing = candidate.get("packing") or {}
    packing_omissions = Counter(packing.get("omitted", {}))
    return {
        "packet_count": 1,
        "identical_packet_rate": 1.0 if legacy_ids == candidate_ids else 0.0,
        "membership_change_rate": 1.0 if membership_changed else 0.0,
        "ordering_only_change_rate": 1.0 if ordering_only else 0.0,
        "mean_jaccard": len(intersection) / len(union) if union else 1.0,
        "added_items": len(candidate_set - legacy_set),
        "removed_items": len(legacy_set - candidate_set),
        "item_count_delta": int(candidate["item_count"]) - int(legacy["item_count"]),
        "byte_count_delta": int(candidate["byte_count"]) - int(legacy["byte_count"]),
        "token_count_delta": int(candidate.get("token_count", 0))
        - int(legacy.get("token_count", 0)),
        "omission_reasons": dict(sorted(omissions.items())),
        "packing_omission_reasons": dict(sorted(packing_omissions.items())),
    }


def _sum_packet_change(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    count = len(rows)
    omission_reasons: Counter[str] = Counter()
    packing_omission_reasons: Counter[str] = Counter()
    for row in rows:
        omission_reasons.update(row["omission_reasons"])
        packing_omission_reasons.update(row["packing_omission_reasons"])
    return {
        "packet_count": count,
        "identical_packet_rate": _rate(
            sum(row["identical_packet_rate"] == 1.0 for row in rows), count
        ),
        "membership_change_rate": _rate(
            sum(row["membership_change_rate"] == 1.0 for row in rows), count
        ),
        "ordering_only_change_rate": _rate(
            sum(row["ordering_only_change_rate"] == 1.0 for row in rows), count
        ),
        "mean_jaccard": _rate(sum(row["mean_jaccard"] for row in rows), count),
        "added_items": sum(row["added_items"] for row in rows),
        "removed_items": sum(row["removed_items"] for row in rows),
        "item_count_delta": sum(row["item_count_delta"] for row in rows),
        "byte_count_delta": sum(row["byte_count_delta"] for row in rows),
        "token_count_delta": sum(row["token_count_delta"] for row in rows),
        "omission_reasons": dict(sorted(omission_reasons.items())),
        "packing_omission_reasons": dict(sorted(packing_omission_reasons.items())),
    }


def _classify_labels(rows: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    contamination: Counter[str] = Counter()
    usefulness: Counter[str] = Counter()
    for row in rows:
        labels = row.get("labels", {})
        legacy_ids = set(_item_ids(row["legacy"]))
        candidate_ids = set(_item_ids(row["candidate"]))
        packet_ids = legacy_ids | candidate_ids
        for item_id, label in labels.get("contamination", {}).items():
            if item_id not in packet_ids:
                raise ValueError("label_item_absent_from_compared_packets")
            if label == "unknown":
                contamination["unknown"] += 1
            elif label == "contaminated":
                if item_id in legacy_ids and item_id not in candidate_ids:
                    contamination["avoided"] += 1
                elif item_id not in legacy_ids and item_id in candidate_ids:
                    contamination["introduced"] += 1
                elif item_id in legacy_ids and item_id in candidate_ids:
                    contamination["retained"] += 1
                else:  # Defensive: packet_ids check above makes this unreachable.
                    raise ValueError("label_item_membership_unresolvable")
            elif label == "acceptable":
                # An acceptable item removed is a coverage effect, never
                # evidence that contamination was avoided.
                contamination["acceptable"] += 1
            else:
                raise ValueError("malformed_contamination_label")
        for item_id, label in labels.get("usefulness", {}).items():
            if item_id not in packet_ids:
                raise ValueError("label_item_absent_from_compared_packets")
            if label == "unknown":
                usefulness["unknown"] += 1
            elif label == "useful" and item_id in legacy_ids:
                usefulness[
                    "legacy_useful_retained"
                    if item_id in candidate_ids
                    else "legacy_useful_withheld"
                ] += 1
            elif label == "useful" and item_id in candidate_ids:
                usefulness["candidate_only_useful"] += 1
            else:
                raise ValueError("malformed_usefulness_label")

    known_contamination = (
        contamination["avoided"] + contamination["introduced"] + contamination["retained"]
    )
    contamination_report = {
        "avoided": contamination["avoided"],
        "introduced": contamination["introduced"],
        "retained": contamination["retained"],
        "acceptable": contamination["acceptable"],
        "unknown": contamination["unknown"],
        "known_denominator": known_contamination,
        "avoided_rate": _rate(contamination["avoided"], known_contamination),
        "introduced_rate": _rate(contamination["introduced"], known_contamination),
    }
    usefulness_report = {
        "legacy_useful_retained": usefulness["legacy_useful_retained"],
        "legacy_useful_withheld": usefulness["legacy_useful_withheld"],
        "candidate_only_useful": usefulness["candidate_only_useful"],
        "unknown": usefulness["unknown"],
    }
    return contamination_report, usefulness_report


def build_profile_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate packet change, contamination, and usefulness labels.

    Labels are optional review evidence. A missing or unknown label stays
    unknown. The evaluator never infers truth from a packet difference.
    """
    packet_changes = [build_packet_change_metrics(row["legacy"], row["candidate"]) for row in rows]
    contamination, usefulness = _classify_labels(rows)
    return {
        "packet_change": _sum_packet_change(packet_changes),
        "contamination": contamination,
        "usefulness": usefulness,
    }


__all__ = ["build_packet_change_metrics", "build_profile_metrics"]
