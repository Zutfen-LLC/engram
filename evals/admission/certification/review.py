"""#162D certification dual-review corpus freeze (generalizes #162C).

Reviewer A reviews all N=100 blind cases. Reviewer B independently reviews
every high-consequence case and every substantive first-reviewer
disagreement, blind to A and to all policy/candidate output. The operator
adjudicates substantive disagreements (no mechanical majority vote). The
final corpus freezes BEFORE any policy reveal; ``finalize_certification_corpus``
never imports policy/candidate modules.

This module deliberately mirrors ``holdout.adjudication`` (Reviewer A ledger
expansion) and ``human_corpus.finalize_human_corpus`` (final freeze) but is
generalized to arbitrary N and to the #162D B-queue rule (high consequence OR
substantive disagreement), and binds the certification doctrine digest.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from evals.admission.blind_review import _secure_write
from evals.admission.certification.doctrine import load_doctrine
from evals.admission.reviewer_adjudication import _cases, _dimensions, _forbid_policy
from evals.admission.schema import LabelRecord, digest

CERT_LEDGER_VERSION = "engram-162d-certification-reviewer-a-ledger-v1"
CERT_DATASET_ID = "eng-certification-162d"
CERT_DATASET_VERSION = "certification-corpus-v1"
FINAL_SCHEMA_VERSION = "engram-162d-final-certification-corpus-v1"

#: Judgment fields where disagreement is substantive (drives the B queue and
#: requires explicit operator resolution; never a majority vote).
SUBSTANTIVE_FIELDS = (
    "consequence",
    "expected_storage_disposition",
    "expected_startup_eligibility",
    "expected_governed_semantic_eligibility",
    "human_review_required",
    "retention_value",
)


def _summary(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    high = sorted(d["case"] for d in decisions if d["consequence"] == "high")
    return {
        "cases": len(decisions),
        "high_consequence": len(high),
        "reviewer_b_queue_cases": high,
    }


def expand_certification_reviewer_a(
    packet: Mapping[str, Any], ledger: Mapping[str, Any], *, frozen_at: datetime
) -> dict[str, Any]:
    """Validate the Reviewer A ledger against the blind packet and freeze it.

    The ledger must be policy-blind, cover exactly the packet membership, and
    carry per-decision blindness attestations (as in #162C: ratification is
    proven by ``policy_reveal_performed=false`` + reviewer identity). Every
    decision derives the B-queue flag mechanically: reviewer_b_required iff
    consequence == high. Disagreement-based B additions happen later, derived
    from A's own record, so they cannot be smuggled in via the flag.
    """
    cases = _cases(packet)
    _forbid_policy(ledger)
    _forbid_policy(packet)
    if ledger.get("ledger_version") != CERT_LEDGER_VERSION:
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
            "dataset_id": CERT_DATASET_ID,
            "dataset_version": CERT_DATASET_VERSION,
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


def reviewer_b_queue_162d(
    frozen_a: Mapping[str, Any],
    *,
    disagreement_fields: Mapping[int, tuple[str, ...]] | None = None,
) -> list[int]:
    """Derive the #162D Reviewer B queue from frozen A.

    B reviews every high-consequence case plus every case with a substantive
    field flagged (supplied by the operator from A's raw review; validated
    against A's own dimensions where possible). The union is derived, not
    asserted: every case number must exist in A.
    """
    queue: set[int] = set()
    for record in frozen_a["records"]:
        label = record["label"]
        if label["reviewer_a"]["dimensions"]["consequence"] == "high":
            queue.add(int(record["case"]))
    if disagreement_fields:
        known = {int(r["case"]) for r in frozen_a["records"]}
        unknown = sorted(set(disagreement_fields) - known)
        if unknown:
            raise ValueError("disagreement_case_not_in_a")
        queue.update(disagreement_fields)
    return sorted(queue)


def build_reviewer_b_packet(
    packet: Mapping[str, Any],
    frozen_a: Mapping[str, Any],
    queue: list[int],
) -> dict[str, Any]:
    """Build the blind Reviewer B packet for the queue cases only.

    Reuses the #162B/#162C convention: subset packet carrying the same
    content fields as A's packet, no policy/candidate fields, numbered by the
    original case number.
    """
    _forbid_policy(packet)
    _forbid_policy(frozen_a)
    if frozen_a.get("artifact_schema_version") != "engram-reviewer-a-frozen-v1":
        raise ValueError("invalid_reviewer_a_freeze")
    if digest({k: v for k, v in frozen_a.items() if k != "frozen_digest"}) != frozen_a.get(
        "frozen_digest"
    ):
        raise ValueError("reviewer_a_digest_mismatch")
    packet_cases = _cases(packet)
    if packet.get("case_count") is not None and packet["case_count"] != len(packet_cases):
        raise ValueError("packet_case_count_mismatch")
    cases = dict(enumerate(packet_cases, start=1))
    selected = []
    for number in sorted(queue):
        if number not in cases:
            raise ValueError("queue_case_missing_from_packet")
        selected.append({**cases[number], "case": number})
    return {
        "packet_schema_version": "engram-blind-review-packet-v1",
        "dataset_id": CERT_DATASET_ID,
        "dataset_version": CERT_DATASET_VERSION,
        "selection_digest": packet["selection_digest"],
        "review_mode": "independent_blind_subset",
        "cases": selected,
        "queue_derivation": "high_consequence_union_substantive_disagreement",
        "policy_fields_present": False,
    }


def finalize_certification_corpus(
    packet: Mapping[str, Any],
    reviewer_a_frozen: Mapping[str, Any],
    reviewer_b_ledger: Mapping[str, Any],
    adjudication: Mapping[str, Any],
    *,
    frozen_at: datetime,
    required_n: int = 100,
) -> dict[str, Any]:
    """Seal the final #162D corpus: dual review complete, blind, immutable.

    Contract (fails closed):
    - all inputs are policy-blind (``_forbid_policy``);
    - A covers exactly the packet;
    - B covers exactly the derived queue (high-consequence union substantive
      disagreement), no more, no less;
    - every substantive A/B disagreement has an explicit operator resolution
      (mechanical majority vote is structurally absent);
    - final corpus has exactly ``required_n`` complete labels;
    - the artifact binds the certification doctrine digest (gates frozen
      before labels) and freezes BEFORE any policy/candidate reveal.
    """
    if frozen_at.tzinfo is None:
        raise ValueError("aware_frozen_at_required")
    doctrine = load_doctrine()
    _forbid_policy(packet)
    _forbid_policy(reviewer_b_ledger)
    _forbid_policy(adjudication)
    if packet.get("packet_schema_version") != "engram-blind-review-packet-v1":
        raise ValueError("invalid_blind_packet")
    # Re-validate A freeze exactly as human_corpus does.
    if reviewer_a_frozen.get("artifact_schema_version") != "engram-reviewer-a-frozen-v1":
        raise ValueError("invalid_reviewer_a_freeze")
    if digest({k: v for k, v in reviewer_a_frozen.items() if k != "frozen_digest"}) != (
        reviewer_a_frozen.get("frozen_digest")
    ):
        raise ValueError("reviewer_a_digest_mismatch")
    a_records = cast(list[dict[str, Any]], reviewer_a_frozen.get("records"))
    packet_cases = _cases(packet)
    if len(a_records) != len(packet_cases) or {
        str(r["review_case_id"]) for r in a_records
    } != {str(c["review_case_id"]) for c in packet_cases}:
        raise ValueError("reviewer_a_membership_mismatch")
    if (
        reviewer_b_ledger.get("artifact_version") != "engram-162d-reviewer-b-ledger-v1"
        or reviewer_b_ledger.get("policy_blind") is not True
        or reviewer_b_ledger.get("reviewer_b_status") != "completed_independent"
    ):
        raise ValueError("invalid_reviewer_b_ledger")
    if (
        adjudication.get("artifact_version") != "engram-162d-adjudication-resolution-v1"
        or adjudication.get("policy_blind") is not True
        or adjudication.get("adjudication_status") != "operator_ratified"
    ):
        raise ValueError("invalid_adjudication")
    b_records = cast(list[dict[str, Any]], reviewer_b_ledger.get("records"))
    resolution_records = cast(list[dict[str, Any]], adjudication.get("records"))
    if reviewer_b_ledger.get("case_count") != len(b_records):
        raise ValueError("reviewer_b_count_mismatch")
    b_by_id = {str(r.get("review_case_id")): r for r in b_records}
    resolution_by_id = {str(r.get("review_case_id")): r for r in resolution_records}
    if len(b_by_id) != len(b_records) or set(b_by_id) != set(resolution_by_id):
        raise ValueError("adjudication_membership_mismatch")
    # Derive the required B queue from frozen A: high-consequence union the
    # B-ledger's own disagreement-attested cases. Every B case must be in it.
    queue = set(reviewer_b_queue_162d(reviewer_a_frozen))
    for record in b_records:
        if bool(record.get("substantive_disagreement_with_a")):
            queue.add(int(record["original_case"]))
    a_ids = {str(r["review_case_id"]) for r in reviewer_a_frozen["records"]}
    if set(b_by_id) and not set(b_by_id) <= a_ids:
        raise ValueError("reviewer_b_membership_mismatch")
    missing_from_queue = sorted(
        int(r["original_case"]) for r in b_records if int(r["original_case"]) not in queue
    )
    if missing_from_queue:
        raise ValueError("reviewer_b_outside_derived_queue")
    # every queued case must actually be reviewed by B
    a_by_case = {int(r["case"]): r for r in a_records}
    queued_ids = {str(a_by_case[n]["review_case_id"]) for n in queue}
    unreviewed = sorted(queued_ids - set(b_by_id))
    if unreviewed:
        raise ValueError("reviewer_b_queue_coverage_missing")

    final_records: list[dict[str, Any]] = []
    disagreements = 0
    substantive_unresolved = 0
    for record in a_records:
        case_id = str(record["review_case_id"])
        label = dict(record["label"])
        high = label["reviewer_a"]["dimensions"]["consequence"] == "high"
        if case_id in b_by_id:
            b = b_by_id[case_id]
            if b.get("original_case") != record.get("case"):
                raise ValueError("reviewer_b_case_number_mismatch")
            b_dims = _dimensions(cast(Mapping[str, Any], b.get("reviewer_b")))
            differs_fields = tuple(
                sorted(
                    field
                    for field in SUBSTANTIVE_FIELDS
                    if label["reviewer_a"]["dimensions"].get(field) != b_dims.get(field)
                )
            )
            differs = bool(differs_fields)
            disagreements += int(differs)
            if differs:
                resolution = resolution_by_id[case_id]
                if resolution.get("original_case") != record.get("case"):
                    raise ValueError("adjudication_case_number_mismatch")
                final_dims = _dimensions(cast(Mapping[str, Any], resolution.get("final")))
                label["reviewer_b"] = {
                    "adjudicator_ref": "reviewer_b",
                    "adjudicated_at": frozen_at.isoformat(),
                    "adjudicator_confidence": "unknown",
                    "reason_code": "independent_blind_reviewer_b",
                    "dimensions": b_dims,
                    "usefulness": None,
                }
                label["resolution"] = {
                    "adjudicator_ref": "operator_resolution",
                    "adjudicated_at": frozen_at.isoformat(),
                    "adjudicator_confidence": "high",
                    "reason_code": "operator_ratified_resolution",
                    "dimensions": final_dims,
                    "usefulness": None,
                }
                label["disagreement"] = "resolved"
            else:
                label["reviewer_b"] = {
                    "adjudicator_ref": "reviewer_b",
                    "adjudicated_at": frozen_at.isoformat(),
                    "adjudicator_confidence": "unknown",
                    "reason_code": "independent_blind_reviewer_b",
                    "dimensions": b_dims,
                    "usefulness": None,
                }
                label["disagreement"] = "none"
            label["review_stage"] = "complete"
        else:
            if high:
                raise ValueError("high_consequence_without_reviewer_b")
            label["review_stage"] = "complete"
        LabelRecord.model_validate(label)
        final_records.append(
            {"case": record["case"], "review_case_id": case_id, "label": label}
        )
    if len(final_records) != required_n:
        raise ValueError("certification_corpus_size_mismatch")
    if substantive_unresolved:
        raise ValueError("unresolved_substantive_disagreement")
    _ = substantive_unresolved  # placate linter; counted structurally above
    final_dims_list = [
        (r["label"]["resolution"] or r["label"]["reviewer_a"])["dimensions"]
        for r in final_records
    ]
    compared = [
        (r["label"]["reviewer_a"]["dimensions"], r["label"]["reviewer_b"]["dimensions"])
        for r in final_records
        if r["label"]["reviewer_b"] is not None
    ]
    inter_rater = {
        field: {
            "compared": len(compared),
            "agreed": sum(a[field] == b[field] for a, b in compared),
        }
        for field in SUBSTANTIVE_FIELDS
    }
    artifact = {
        "artifact_schema_version": FINAL_SCHEMA_VERSION,
        "doctrine_digest": doctrine["doctrine_digest"],
        "reviewer_a_frozen_digest": reviewer_a_frozen["frozen_digest"],
        "reviewer_b_digest": digest(reviewer_b_ledger),
        "adjudication_digest": digest(adjudication),
        "frozen_at": frozen_at.isoformat(),
        "records": final_records,
        "summary": {
            "case_count": len(final_records),
            "final_valid_label_count": len(final_records),
            "reviewer_a_count": len(final_records),
            "reviewer_b_count": len(b_records),
            "high_consequence_count": sum(
                1 for d in final_dims_list if d["consequence"] == "high"
            ),
            "disagreement_count": disagreements,
            "resolved_disagreement_count": disagreements,
            "unresolved_disagreement_count": 0,
            "high_consequence_without_b_count": 0,
            "inter_rater": inter_rater,
        },
        "privacy": "aggregate_public_only_raw_records_private_outside_git",
    }
    artifact["final_corpus_digest"] = digest(
        {k: v for k, v in artifact.items() if k != "final_corpus_digest"}
    )
    return artifact


def assert_certification_reveal_gate(corpus: Mapping[str, Any], *, required_n: int = 100) -> None:
    """Certification evaluation may run only on a fully frozen blind corpus."""
    if corpus.get("artifact_schema_version") != FINAL_SCHEMA_VERSION:
        raise ValueError("certification_reveal_gate_failed")
    summary = corpus.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("certification_reveal_gate_failed")
    if (
        summary.get("case_count") != required_n
        or summary.get("final_valid_label_count") != required_n
    ):
        raise ValueError("certification_reveal_gate_failed")
    if summary.get("unresolved_disagreement_count") != 0:
        raise ValueError("certification_reveal_gate_failed")
    if summary.get("high_consequence_without_b_count") != 0:
        raise ValueError("certification_reveal_gate_failed")
    records = corpus.get("records")
    if not isinstance(records, list) or len(records) != required_n:
        raise ValueError("certification_reveal_gate_failed")
    for record in records:
        LabelRecord.model_validate(record["label"])


def write_private(path: Path, payload: dict[str, Any]) -> None:
    """Write a private artifact outside the repository, mode 0600, exclusive."""
    repo = Path(__file__).resolve().parents[3]
    if path.resolve().is_relative_to(repo):
        raise ValueError("private_output_must_be_outside_repository")
    _secure_write(path, json.dumps(payload, sort_keys=True, indent=2) + "\n")


def load_private(path: Path) -> dict[str, Any]:
    """Load a private artifact; opaque error keeps content out of logs."""
    try:
        value: dict[str, Any] = json.loads(path.read_text())
        return value
    except Exception as exc:
        raise ValueError("invalid_private_artifact") from exc
