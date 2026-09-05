"""#162C holdout Reviewer-A freeze tooling.

Unlike ``evals.admission.reviewer_adjudication`` (the accepted #162B tool whose
bytes are covered by the frozen #162A ``runner_digest()``), this module lives
outside that digest glob and generalizes the expansion for the 30-case holdout
ledger contract (``eng-calibration-001c-holdout-reviewer-a-ledger-v1``):

* ratification is proven by ``policy_reveal_performed=false`` plus
  ``reviewer_a_identity=operator`` per decision (the #162B ledger instead
  carried an explicit ``reviewer_a_ratified`` flag);
* the ledger's own ``summary`` block must agree with the aggregates derived
  from the decisions — fail closed on any drift;
* every decision's ``reviewer_b_required`` flag must equal
  ``consequence=high`` so the B queue cannot be smuggled in via the flag.

Reviewer-B derivation reuses ``reviewer_adjudication.reviewer_b_queue``
unchanged: the frozen artifact keeps the ``engram-reviewer-a-frozen-v1``
schema.  This module never imports or evaluates the promotion policy.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from evals.admission.blind_review import _secure_write
from evals.admission.reviewer_adjudication import _cases, _dimensions, _forbid_policy
from evals.admission.schema import LabelRecord, digest

HOLDOUT_LEDGER_VERSION = "eng-calibration-001c-holdout-reviewer-a-ledger-v1"
HOLDOUT_DATASET_ID = "eng-calibration-001c"
HOLDOUT_DATASET_VERSION = "holdout-reviewer-a-frozen-v1"


def _summary(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    high = [d["case"] for d in decisions if d["consequence"] == "high"]
    return {
        "cases": len(decisions),
        "retain": sum(1 for d in decisions if d["retention_value"] == "retain"),
        "do_not_retain": sum(1 for d in decisions if d["retention_value"] == "do_not_retain"),
        "storage_retain": sum(
            1 for d in decisions if d["expected_storage_disposition"] == "retain"
        ),
        "storage_defer": sum(
            1 for d in decisions if d["expected_storage_disposition"] == "defer"
        ),
        "storage_reject": sum(
            1 for d in decisions if d["expected_storage_disposition"] == "reject"
        ),
        "startup_yes": sum(1 for d in decisions if d["expected_startup_eligibility"] == "yes"),
        "governed_yes": sum(
            1 for d in decisions if d["expected_governed_semantic_eligibility"] == "yes"
        ),
        "review_yes": sum(1 for d in decisions if d["human_review_required"] == "yes"),
        "high_consequence": len(high),
        "reviewer_b_cases": high,
    }


def expand_holdout_reviewer_a(
    packet: Mapping[str, Any], ledger: Mapping[str, Any], *, frozen_at: datetime
) -> dict[str, Any]:
    """Validate the 162C holdout ledger against the packet and freeze Reviewer A."""
    cases = _cases(packet)
    _forbid_policy(ledger)
    if ledger.get("ledger_version") != HOLDOUT_LEDGER_VERSION:
        raise ValueError("invalid_ledger_version")
    if (
        ledger.get("policy_blind") is not True
        or ledger.get("candidate_outputs_visible") is not False
        or ledger.get("reviewer_a_status") != "frozen"
    ):
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
    if ledger.get("summary") != _summary(decisions):
        raise ValueError("ledger_summary_drift")
    by_id = {str(decision["review_case_id"]): decision for decision in decisions}
    records: list[dict[str, Any]] = []
    for number, case in enumerate(cases, start=1):
        decision = by_id[str(case["review_case_id"])]
        if (
            decision["case"] != number
            or decision.get("policy_reveal_performed") is not False
            or decision.get("reviewer_a_identity") != "operator"
        ):
            raise ValueError("ledger_membership_mismatch")
        if bool(decision.get("reviewer_b_required")) != (decision["consequence"] == "high"):
            raise ValueError("reviewer_b_flag_drift")
        dimensions = _dimensions(decision)
        high = dimensions["consequence"] == "high"
        label = {
            "sample_id": case["review_case_id"],
            "label_schema_version": "engram-admission-label-v1",
            "dataset_id": HOLDOUT_DATASET_ID,
            "dataset_version": HOLDOUT_DATASET_VERSION,
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


def _load_json(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("json_object_required")
    return cast(dict[str, Any], raw)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--frozen-at", type=datetime.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        frozen_at = args.frozen_at
        if frozen_at.tzinfo is None:
            raise ValueError("aware_frozen_at_required")
        artifact = expand_holdout_reviewer_a(
            _load_json(args.packet), _load_json(args.ledger), frozen_at=frozen_at
        )
        _secure_write(args.output, json.dumps(artifact, sort_keys=True, indent=2) + "\n")
    except Exception:
        # Inputs are private and may contain dogfood content; never echo them.
        print('{"error":"invalid_private_reviewer_artifact"}')
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
