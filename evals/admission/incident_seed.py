"""Build a private, content-free-on-public incident seed from a frozen corpus.

The seed is evaluative history, not production input.  It deliberately keeps
review-case IDs only in the private artifact and records no memory content.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from typing import Any, cast

from evals.admission.human_corpus import assert_reveal_gate
from evals.admission.schema import digest

_PRIVATE_SCHEMA = "engram-admission-incident-seed-v1"
_PUBLIC_SCHEMA = "engram-admission-incident-seed-aggregate-v1"


def _dimensions(record: Mapping[str, Any]) -> Mapping[str, Any]:
    label = record["label"]
    return cast(Mapping[str, Any], label["resolution"] or label["reviewer_a"])


def _select(
    records: list[Mapping[str, Any]], predicate: Callable[[Mapping[str, Any]], bool]
) -> Mapping[str, Any] | None:
    matches = [record for record in records if predicate(_dimensions(record)["dimensions"])]
    return min(matches, key=lambda record: str(record["review_case_id"])) if matches else None


def build_incident_seed(
    corpus: Mapping[str, Any], comparison: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Derive distinct genuine examples; state unavailable classes explicitly."""
    assert_reveal_gate(corpus)
    if comparison.get("final_corpus_digest") != corpus.get("final_corpus_digest"):
        raise ValueError("comparison_corpus_identity_mismatch")
    records = list(corpus["records"])
    selected: list[tuple[str, Mapping[str, Any] | None, str]] = [
        (
            "held_back_despite_likely_value",
            _select(
                records,
                lambda d: d["retention_value"] == "retain"
                and d["expected_storage_disposition"] == "retain",
            ),
            (
                "Human retained/storage-retain judgment was not automatically admitted "
                "by current policy."
            ),
        ),
        (
            "stale_or_superseded",
            _select(
                records,
                lambda d: d["factual_outcome"] == "became_outdated"
                and d["supersession_expected"] == "yes",
            ),
            (
                "Later outcome is separately recorded as outdated; this is not injected "
                "into decision-time support."
            ),
        ),
        (
            "conflict_involved",
            _select(records, lambda d: d["conflict_expected"] == "yes"),
            (
                "Conflict context requires governance-aware handling rather than a "
                "promotion inference."
            ),
        ),
        (
            "poor_extraction_or_non_propositional",
            _select(
                records,
                lambda d: d["atomic"] == "no" or d["proposition_count"] == "zero",
            ),
            (
                "A retention judgment does not make a non-atomic or non-propositional "
                "extraction usable."
            ),
        ),
    ]
    incidents: list[dict[str, Any]] = []
    used: set[str] = set()
    for incident_class, record, why in selected:
        if record is None or str(record["review_case_id"]) in used:
            continue
        used.add(str(record["review_case_id"]))
        dimensions = _dimensions(record)["dimensions"]
        incidents.append(
            {
                "incident_id": f"inc-{len(incidents) + 1:02d}",
                "incident_class": incident_class,
                "review_case_id": record["review_case_id"],
                "decision_time_evidence_state": {
                    "epistemic_state": dimensions["epistemic_state"],
                    "retention_value": dimensions["retention_value"],
                    "expected_storage_disposition": dimensions["expected_storage_disposition"],
                    "conflict_expected": dimensions["conflict_expected"],
                    "supersession_expected": dimensions["supersession_expected"],
                    "temporal_validity_issue": dimensions["temporal_validity_issue"],
                },
                "human_expected_handling": {
                    "startup": dimensions["expected_startup_eligibility"],
                    "governed_semantic": dimensions["expected_governed_semantic_eligibility"],
                    "review_required": dimensions["human_review_required"],
                },
                "later_observed_outcome_or_context": dimensions["factual_outcome"],
                "why_it_is_an_incident_or_example": why,
                "source_snapshot_digest": comparison["snapshot_digest"],
                "source_final_corpus_digest": corpus["final_corpus_digest"],
                "privacy_classification": "private_dogfood_no_content_in_seed",
            }
        )
    unavailable = [
        "correctly_useful_with_verified_later_outcome",
        "incorrectly_retained_with_verified_later_outcome",
        "wrongly_promoted_or_admitted",
        "delayed_or_orphaned_by_orchestration",
    ]
    private = {
        "artifact_schema_version": _PRIVATE_SCHEMA,
        "source_snapshot_digest": comparison["snapshot_digest"],
        "source_final_corpus_digest": corpus["final_corpus_digest"],
        "incidents": incidents,
        "unavailable_incident_classes": unavailable,
        "limitations": (
            "No qualifying case observed in this tranche/history for each unavailable class; "
            "later outcome is not used as decision-time evidence."
        ),
        "privacy": "private_operator_artifact; review_case_ids are never published",
    }
    private["incident_seed_digest"] = digest(private)
    public = {
        "artifact_schema_version": _PUBLIC_SCHEMA,
        "private_incident_seed_digest": private["incident_seed_digest"],
        "source_snapshot_digest": comparison["snapshot_digest"],
        "source_final_corpus_digest": corpus["final_corpus_digest"],
        "incident_count": len(incidents),
        "incident_class_distribution": dict(
            sorted(Counter(x["incident_class"] for x in incidents).items())
        ),
        "unavailable_incident_classes": unavailable,
        "decision_time_vs_later_outcome": (
            "Each private incident keeps decision-time evidence/state separate from later "
            "observed outcome/context; public evidence contains no case identifiers or content."
        ),
        "privacy": "aggregate_only_no_raw_membership_or_content",
        "limitations": private["limitations"],
    }
    return private, public
