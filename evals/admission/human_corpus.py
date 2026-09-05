"""Freeze independent human judgments before current-policy comparison.

This module consumes only private blind-review artifacts. It rejects policy fields
in all human inputs, preserves Reviewer A and Reviewer B separately, and emits a
final immutable human corpus only after every high-consequence case is dual
reviewed and every disagreement is resolved.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast

from evals.admission.reviewer_adjudication import _forbid_policy
from evals.admission.schema import LabelRecord, digest


def _require_object(value: object, error: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(error)
    return cast(dict[str, Any], value)


def _records(value: Mapping[str, Any], field: str) -> list[dict[str, Any]]:
    records = value.get(field)
    if not isinstance(records, list):
        raise ValueError("invalid_records")
    return [_require_object(record, "invalid_record") for record in records]


def _dimensions(judgment: Mapping[str, Any], *, resolution: bool) -> dict[str, Any]:
    flags = set(judgment.get("flags", ()))
    storage = judgment["expected_storage_disposition"]
    review = judgment["human_review_required"]
    if storage == "reject":
        action = "reject"
    elif storage == "defer" or review == "yes":
        action = "review"
    elif storage == "retain":
        action = "automatic_admission"
    else:
        action = "unknown"
    return {
        "atomic": "unknown",
        "proposition_count": "unknown",
        "attribution": "unavailable",
        "source_span": "unavailable",
        "evidence_span": "unavailable",
        "assertion_origin": "unknown",
        "expected_kind": judgment["expected_kind"],
        "expected_subject_or_domain": "unknown",
        "expected_scope": "unknown",
        "retention_value": judgment["retention_value"],
        "epistemic_state": judgment["epistemic_state"],
        "factual_outcome": judgment.get("factual_outcome"),
        "consequence": judgment["consequence"],
        "expected_storage_disposition": storage,
        "expected_startup_eligibility": judgment["expected_startup_eligibility"],
        "expected_governed_semantic_eligibility": judgment[
            "expected_governed_semantic_eligibility"
        ],
        "human_review_required": review,
        "acceptable_abstention": "unknown",
        "conflict_expected": judgment.get(
            "conflict_expected", "yes" if "conflict" in flags else "unknown"
        ),
        "dispute_expected": "unknown",
        "supersession_expected": judgment.get(
            "supersession_expected", "yes" if "supersession" in flags else "unknown"
        ),
        "temporal_validity_issue": judgment.get(
            "temporal_validity_issue", "yes" if "temporal" in flags else "unknown"
        ),
        "scope_visibility_concern": "yes" if "scope" in flags else "unknown",
        "evidence_independence": "unknown",
        "expected_blockers": None,
        "expected_next_action": action,
    }


def _human_judgment(
    judgment: Mapping[str, Any], *, adjudicator_ref: str, frozen_at: datetime, resolution: bool
) -> dict[str, Any]:
    return {
        "adjudicator_ref": adjudicator_ref,
        "adjudicated_at": frozen_at.isoformat(),
        "adjudicator_confidence": "high" if resolution else "unknown",
        "reason_code": "operator_ratified_resolution"
        if resolution
        else "independent_blind_reviewer_b",
        "dimensions": _dimensions(judgment, resolution=resolution),
        "usefulness": None,
    }


def _validate_a_freeze(
    frozen: Mapping[str, Any], packet: Mapping[str, Any]
) -> list[dict[str, Any]]:
    _forbid_policy(frozen)
    if frozen.get("artifact_schema_version") != "engram-reviewer-a-frozen-v1":
        raise ValueError("invalid_reviewer_a_freeze")
    claimed = frozen.get("frozen_digest")
    unsigned = dict(frozen)
    unsigned.pop("frozen_digest", None)
    if claimed != digest(unsigned):
        raise ValueError("reviewer_a_digest_mismatch")
    records = _records(frozen, "records")
    packet_cases = _records(packet, "cases")
    packet_ids = [str(case.get("review_case_id")) for case in packet_cases]
    record_ids = [str(record.get("review_case_id")) for record in records]
    if (
        len(records) != len(packet_cases)
        or set(record_ids) != set(packet_ids)
        or len(set(record_ids)) != len(record_ids)
    ):
        raise ValueError("reviewer_a_membership_mismatch")
    return records


def assert_reveal_gate(corpus: Mapping[str, Any]) -> None:
    if corpus.get("artifact_schema_version") != "engram-final-human-corpus-v1":
        raise ValueError("reveal_gate_failed")
    summary = _require_object(corpus.get("summary"), "reveal_gate_failed")
    if summary.get("case_count") != summary.get("final_valid_label_count"):
        raise ValueError("reveal_gate_failed")
    if summary.get("unresolved_disagreement_count") != 0:
        raise ValueError("reveal_gate_failed")
    if summary.get("high_consequence_without_b_count") != 0:
        raise ValueError("reveal_gate_failed")
    records = _records(corpus, "records")
    if len(records) != summary["case_count"]:
        raise ValueError("reveal_gate_failed")
    for record in records:
        LabelRecord.model_validate(record["label"])


def finalize_human_corpus(
    packet: Mapping[str, Any],
    reviewer_a_frozen: Mapping[str, Any],
    reviewer_b_ledger: Mapping[str, Any],
    adjudication: Mapping[str, Any],
    *,
    frozen_at: datetime,
) -> dict[str, Any]:
    """Create a completed, immutable human corpus without evaluating policy."""
    if frozen_at.tzinfo is None:
        raise ValueError("aware_frozen_at_required")
    _forbid_policy(packet)
    _forbid_policy(reviewer_b_ledger)
    _forbid_policy(adjudication)
    if packet.get("packet_schema_version") != "engram-blind-review-packet-v1":
        raise ValueError("invalid_blind_packet")
    a_records = _validate_a_freeze(reviewer_a_frozen, packet)
    if (
        reviewer_b_ledger.get("artifact_version") != "eng-calibration-001b-reviewer-b-ledger-v1"
        or reviewer_b_ledger.get("policy_blind") is not True
        or reviewer_b_ledger.get("reviewer_b_status") != "completed_independent"
    ):
        raise ValueError("invalid_reviewer_b_ledger")
    if (
        adjudication.get("artifact_version") != "eng-calibration-001b-adjudication-resolution-v1"
        or adjudication.get("policy_blind") is not True
        or adjudication.get("adjudication_status") != "operator_ratified"
    ):
        raise ValueError("invalid_adjudication")
    b_records = _records(reviewer_b_ledger, "records")
    resolution_records = _records(adjudication, "records")
    if reviewer_b_ledger.get("case_count") != len(b_records) or adjudication.get(
        "reviewer_b_count"
    ) != len(b_records):
        raise ValueError("reviewer_b_count_mismatch")
    b_by_id = {str(record.get("review_case_id")): record for record in b_records}
    resolution_by_id = {str(record.get("review_case_id")): record for record in resolution_records}
    if len(b_by_id) != len(b_records) or set(b_by_id) != set(resolution_by_id):
        raise ValueError("adjudication_membership_mismatch")
    final_records: list[dict[str, Any]] = []
    disagreements = 0
    for record in a_records:
        case_id = str(record["review_case_id"])
        label = _require_object(record.get("label"), "invalid_reviewer_a_label")
        high = label["reviewer_a"]["dimensions"]["consequence"] == "high"
        updated = dict(label)
        if case_id in b_by_id:
            b = b_by_id[case_id]
            if b.get("original_case") != record.get("case"):
                raise ValueError("reviewer_b_case_number_mismatch")
            reviewer_b = _human_judgment(
                _require_object(b.get("reviewer_b"), "invalid_reviewer_b"),
                adjudicator_ref="reviewer_b",
                frozen_at=frozen_at,
                resolution=False,
            )
            resolution = resolution_by_id[case_id]
            if resolution.get("original_case") != record.get("case"):
                raise ValueError("adjudication_case_number_mismatch")
            final_resolution = _human_judgment(
                _require_object(resolution.get("final"), "invalid_resolution"),
                adjudicator_ref="operator_resolution",
                frozen_at=frozen_at,
                resolution=True,
            )
            differs = updated["reviewer_a"]["dimensions"] != reviewer_b["dimensions"]
            disagreements += int(differs)
            updated.update(
                {
                    "reviewer_b": reviewer_b,
                    "resolution": final_resolution if differs else None,
                    "disagreement": "resolved" if differs else "none",
                    "review_stage": "complete",
                }
            )
        elif high:
            raise ValueError("high_consequence_without_reviewer_b")
        LabelRecord.model_validate(updated)
        final_records.append({"case": record["case"], "review_case_id": case_id, "label": updated})
    if set(b_by_id) - {str(record["review_case_id"]) for record in a_records}:
        raise ValueError("reviewer_b_membership_mismatch")
    final_dimensions = [
        record["label"]["resolution"] or record["label"]["reviewer_a"] for record in final_records
    ]
    high_without_b = sum(
        item["dimensions"]["consequence"] == "high" and record["label"]["reviewer_b"] is None
        for item, record in zip(final_dimensions, final_records, strict=True)
    )
    compared_dimensions = [
        (record["label"]["reviewer_a"]["dimensions"], record["label"]["reviewer_b"]["dimensions"])
        for record in final_records
        if record["label"]["reviewer_b"] is not None
    ]
    inter_rater = {
        field: {
            "compared": len(compared_dimensions),
            "agreed": sum(a[field] == b[field] for a, b in compared_dimensions),
        }
        for field in (
            "expected_kind",
            "retention_value",
            "epistemic_state",
            "consequence",
            "expected_storage_disposition",
            "expected_startup_eligibility",
            "expected_governed_semantic_eligibility",
            "human_review_required",
            "conflict_expected",
            "supersession_expected",
            "temporal_validity_issue",
        )
    }
    summary = {
        "case_count": len(final_records),
        "final_valid_label_count": len(final_records),
        "reviewer_a_count": len(final_records),
        "reviewer_b_count": len(b_records),
        "disagreement_count": disagreements,
        "resolved_disagreement_count": disagreements,
        "unresolved_disagreement_count": 0,
        "high_consequence_without_b_count": high_without_b,
        "inter_rater": inter_rater,
        "final_distributions": {
            field: dict(
                sorted(Counter(item["dimensions"][field] for item in final_dimensions).items())
            )
            for field in (
                "expected_kind",
                "retention_value",
                "consequence",
                "expected_storage_disposition",
                "expected_startup_eligibility",
                "expected_governed_semantic_eligibility",
                "human_review_required",
                "temporal_validity_issue",
                "conflict_expected",
                "supersession_expected",
            )
        },
    }
    artifact = {
        "artifact_schema_version": "engram-final-human-corpus-v1",
        "reviewer_a_frozen_digest": reviewer_a_frozen["frozen_digest"],
        "reviewer_b_digest": digest(reviewer_b_ledger),
        "adjudication_digest": digest(adjudication),
        "frozen_at": frozen_at.isoformat(),
        "records": final_records,
        "summary": summary,
    }
    assert_reveal_gate(artifact)
    artifact["final_corpus_digest"] = digest(artifact)
    return artifact
