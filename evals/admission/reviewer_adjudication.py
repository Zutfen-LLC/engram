"""Private blind Reviewer-A freeze and independent Reviewer-B handoff tooling.

This module never imports or evaluates the promotion policy.  It consumes the
already-blind packet and a human-ratified compact ledger only.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from evals.admission.blind_review import _secure_write, markdown_packet
from evals.admission.schema import LabelRecord, digest

_POLICY_FIELDS = frozenset(
    {
        "current_policy_version",
        "current_selected_lane",
        "would_promote",
        "blocker_codes",
        "readiness_state",
        "terminal_under_current_policy",
        "current_job_state",
        "promotion_score",
        "promotion_threshold",
        "candidate_policy",
    }
)


def _forbid_policy(value: object) -> None:
    if isinstance(value, Mapping):
        if _POLICY_FIELDS.intersection(value):
            raise ValueError("policy_field_present")
        for child in value.values():
            _forbid_policy(child)
    elif isinstance(value, list):
        for child in value:
            _forbid_policy(child)


def _cases(packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    _forbid_policy(packet)
    if packet.get("packet_schema_version") != "engram-blind-review-packet-v1":
        raise ValueError("invalid_blind_packet")
    cases = cast(list[dict[str, Any]], packet.get("cases"))
    if packet.get("case_count") != len(cases) or not cases:
        raise ValueError("packet_case_count_mismatch")
    ids = [case.get("review_case_id") for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate_packet_case")
    return cases


def _dimensions(decision: Mapping[str, Any]) -> dict[str, Any]:
    # The packet explicitly says provenance and evidence roots were not
    # recorded.  Preserve those unknowns; source/evidence spans themselves were
    # absent, so their handbook value is unavailable.
    return {
        "atomic": decision["atomic"],
        "proposition_count": decision["proposition_count"],
        "attribution": "unavailable",
        "source_span": "unavailable",
        "evidence_span": "unavailable",
        "assertion_origin": decision["assertion_origin"],
        "expected_kind": decision["expected_kind"],
        "expected_subject_or_domain": "unknown",
        "expected_scope": decision["expected_scope"],
        "retention_value": decision["retention_value"],
        "epistemic_state": decision["epistemic_state"],
        "factual_outcome": decision["factual_outcome"],
        "consequence": decision["consequence"],
        "expected_storage_disposition": decision["expected_storage_disposition"],
        "expected_startup_eligibility": decision["expected_startup_eligibility"],
        "expected_governed_semantic_eligibility": decision[
            "expected_governed_semantic_eligibility"
        ],
        "human_review_required": decision["human_review_required"],
        "acceptable_abstention": decision["acceptable_abstention"],
        "conflict_expected": decision["conflict_expected"],
        "dispute_expected": decision["dispute_expected"],
        "supersession_expected": decision["supersession_expected"],
        "temporal_validity_issue": decision["temporal_validity_issue"],
        "scope_visibility_concern": decision["scope_visibility_concern"],
        "evidence_independence": decision["evidence_independence"],
        "expected_blockers": None,
        "expected_next_action": decision["expected_next_action"],
    }


def expand_reviewer_a(
    packet: Mapping[str, Any], ledger: Mapping[str, Any], *, frozen_at: datetime
) -> dict[str, Any]:
    """Validate and freeze a compact ratified ledger without policy access."""
    cases = _cases(packet)
    _forbid_policy(ledger)
    if ledger.get("ledger_version") != "eng-calibration-001b-reviewer-a-ledger-v1":
        raise ValueError("invalid_ledger_version")
    if ledger.get("policy_blind") is not True or ledger.get("reviewer_a_status") != "frozen":
        raise ValueError("ledger_not_blind_and_frozen")
    decisions = cast(list[dict[str, Any]], ledger.get("decisions"))
    if ledger.get("case_count") != len(cases) or len(decisions) != len(cases):
        raise ValueError("ledger_membership_mismatch")
    packet_ids = [str(case["review_case_id"]) for case in cases]
    decision_ids = [str(decision.get("review_case_id")) for decision in decisions]
    case_numbers = [decision.get("case") for decision in decisions]
    if set(packet_ids) != set(decision_ids) or len(decision_ids) != len(set(decision_ids)):
        raise ValueError("ledger_membership_mismatch")
    if set(case_numbers) != set(range(1, len(cases) + 1)):
        raise ValueError("ledger_membership_mismatch")
    by_id = {str(decision["review_case_id"]): decision for decision in decisions}
    records: list[dict[str, Any]] = []
    for number, case in enumerate(cases, start=1):
        decision = by_id[str(case["review_case_id"])]
        if decision["case"] != number or decision.get("reviewer_a_ratified") is not True:
            raise ValueError("ledger_membership_mismatch")
        dimensions = _dimensions(decision)
        high = dimensions["consequence"] == "high"
        label = {
            "sample_id": case["review_case_id"],
            "label_schema_version": "engram-admission-label-v1",
            "dataset_id": "eng-calibration-001b",
            "dataset_version": "reviewer-a-frozen-v1",
            "source_sample_ref": case["review_case_id"],
            "content_hash": None,
            "fixture_role": "non_propositional"
            if dimensions["proposition_count"] == "zero"
            else "ordinary_claim",
            "label_origin": "human_adjudicated",
            "reviewer_a": {
                "adjudicator_ref": "operator",
                "adjudicated_at": frozen_at.isoformat(),
                "adjudicator_confidence": "high",
                "reason_code": "blind_interactive_ratification",
                "dimensions": dimensions,
                "usefulness": None,
            },
            "reviewer_b": None,
            "resolution": None,
            "disagreement": "none",
            "review_stage": "reviewer_b_pending" if high else "complete",
        }
        LabelRecord.model_validate(label)
        records.append({"case": number, "review_case_id": case["review_case_id"], "label": label})
    frozen = {
        "artifact_schema_version": "engram-reviewer-a-frozen-v1",
        "source_packet_schema_version": packet["packet_schema_version"],
        "source_packet_digest": digest(packet),
        "source_selection_digest": packet["selection_digest"],
        "reviewer_a_provenance": {
            "reviewer_identity": "operator",
            "review_mode": "blind_interactive_ratification",
            "facilitator": "AI-assisted schema mapping",
            "human_authority": "operator",
            "policy_output_visible": False,
        },
        "frozen_at": frozen_at.isoformat(),
        "records": records,
    }
    frozen["frozen_digest"] = digest(frozen)
    return frozen


def reviewer_b_queue(
    frozen: Mapping[str, Any], packet: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Derive an independent B packet exclusively from frozen high labels."""
    cases = _cases(packet)
    if frozen.get("artifact_schema_version") != "engram-reviewer-a-frozen-v1":
        raise ValueError("invalid_reviewer_a_freeze")
    original = dict(frozen)
    claimed = original.pop("frozen_digest", None)
    if claimed != digest(original):
        raise ValueError("frozen_digest_mismatch")
    records = cast(list[dict[str, Any]], frozen.get("records"))
    high_ids = [
        record["review_case_id"]
        for record in records
        if record["label"]["reviewer_a"]["dimensions"]["consequence"] == "high"
    ]
    by_id = {str(case["review_case_id"]): case for case in cases}
    if set(high_ids) - set(by_id) or len(high_ids) != len(set(high_ids)):
        raise ValueError("reviewer_b_queue_membership_mismatch")
    b_cases = [by_id[case_id] for case_id in high_ids]
    packet_b = {
        "packet_schema_version": "engram-reviewer-b-blind-packet-v1",
        "selection_digest": digest({"source_packet": digest(packet), "case_ids": high_ids}),
        "source_packet_digest": digest(packet),
        "case_count": len(b_cases),
        "cases": b_cases,
    }
    _forbid_policy(packet_b)
    state = {
        "review_state_schema_version": "engram-reviewer-b-state-v1",
        "packet_digest": digest(packet_b),
        "cases": [
            {"review_case_id": item["review_case_id"], "reviewer_b": None} for item in b_cases
        ],
    }
    return packet_b, state


def _load_json(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("json_object_required")
    return cast(dict[str, Any], raw)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("expand-reviewer-a")
    freeze.add_argument("--packet", type=Path, required=True)
    freeze.add_argument("--ledger", type=Path, required=True)
    freeze.add_argument("--frozen-at", type=datetime.fromisoformat, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    queue = commands.add_parser("reviewer-b-queue")
    queue.add_argument("--frozen", type=Path, required=True)
    queue.add_argument("--packet", type=Path, required=True)
    queue.add_argument("--json-output", type=Path, required=True)
    queue.add_argument("--markdown-output", type=Path, required=True)
    queue.add_argument("--state-output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "expand-reviewer-a":
            frozen_at = args.frozen_at
            if frozen_at.tzinfo is None:
                raise ValueError("aware_frozen_at_required")
            artifact = expand_reviewer_a(
                _load_json(args.packet), _load_json(args.ledger), frozen_at=frozen_at
            )
            _secure_write(args.output, json.dumps(artifact, sort_keys=True, indent=2) + "\n")
        else:
            packet_b, state = reviewer_b_queue(_load_json(args.frozen), _load_json(args.packet))
            _secure_write(args.json_output, json.dumps(packet_b, sort_keys=True, indent=2) + "\n")
            _secure_write(args.markdown_output, markdown_packet(packet_b) + "\n")
            _secure_write(args.state_output, json.dumps(state, sort_keys=True, indent=2) + "\n")
    except Exception:
        # Inputs are private and may contain dogfood content; never echo them.
        print('{"error":"invalid_private_reviewer_artifact"}')
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
