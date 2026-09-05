"""Read-only current-policy comparison for a frozen human corpus."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

from evals.admission.dataset import Dataset
from evals.admission.human_corpus import assert_reveal_gate
from evals.admission.policy import evaluate
from evals.admission.schema import digest


def _counter(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def compare_frozen_corpus(
    corpus: Mapping[str, Any], snapshot: Dataset, tranche: Mapping[str, Any]
) -> dict[str, Any]:
    """Evaluate only captured policy inputs after a completed human freeze."""
    unsigned = dict(corpus)
    claimed = unsigned.pop("final_corpus_digest", None)
    if claimed != digest(unsigned):
        raise ValueError("final_corpus_digest_mismatch")
    assert_reveal_gate(corpus)
    if tranche.get("snapshot_identity") != snapshot.manifest.data_digest:
        raise ValueError("snapshot_identity_mismatch")
    sample_ids = tranche.get("sample_ids")
    review_ids = tranche.get("review_case_ids")
    if (
        not isinstance(sample_ids, list)
        or not isinstance(review_ids, list)
        or len(sample_ids) != len(review_ids)
    ):
        raise ValueError("tranche_membership_mismatch")
    policy_by_review = {
        review_id: sample.policy_input
        for sample_id, review_id in zip(sample_ids, review_ids, strict=True)
        if (sample := next((s for s in snapshot.samples if s.sample_id == sample_id), None))
        is not None
    }
    labels = {record["review_case_id"]: record["label"] for record in corpus["records"]}
    if set(policy_by_review) != set(labels):
        raise ValueError("policy_label_membership_mismatch")
    rows: list[dict[str, Any]] = []
    for case_id in sorted(labels):
        label = labels[case_id]
        dimensions = (label["resolution"] or label["reviewer_a"])["dimensions"]
        result = evaluate(policy_by_review[case_id], snapshot.config, snapshot.evaluation_at)
        automatic = result.would_promote is True
        permitted = (
            dimensions["expected_storage_disposition"] == "retain"
            and dimensions["expected_startup_eligibility"] == "yes"
            and dimensions["expected_governed_semantic_eligibility"] == "yes"
            and dimensions["human_review_required"] == "no"
        )
        rows.append(
            {
                "human": dimensions,
                "policy": result.model_dump(mode="json"),
                "automatic": automatic,
                "human_permits_automatic": permitted,
                "agreement": automatic == permitted,
            }
        )
    automatic_rows = [row for row in rows if row["automatic"]]
    held = [row for row in rows if not row["automatic"]]
    false_auto = [row for row in automatic_rows if not row["human_permits_automatic"]]
    high_auto = [row for row in automatic_rows if row["human"]["consequence"] == "high"]
    high_false_auto = [row for row in high_auto if not row["human_permits_automatic"]]
    retention_cross = {
        value: {
            "automatic": sum(
                row["automatic"] for row in rows if row["human"]["retention_value"] == value
            ),
            "blocked_or_held": sum(
                not row["automatic"] for row in rows if row["human"]["retention_value"] == value
            ),
        }
        for value in ("retain", "do_not_retain", "uncertain")
    }
    consequence = {}
    for value in ("low", "medium", "high"):
        selected = [row for row in rows if row["human"]["consequence"] == value]
        consequence[value] = {
            "labeled": len(selected),
            "agreement": sum(row["agreement"] for row in selected),
            "mismatch": sum(not row["agreement"] for row in selected),
            "automatic": sum(row["automatic"] for row in selected),
            "false_automatic": sum(
                row["automatic"] and not row["human_permits_automatic"] for row in selected
            ),
        }
    blockers = _counter([blocker for row in rows for blocker in row["policy"]["blocker_codes"]])
    readiness = _counter([row["policy"]["readiness_state"] for row in rows])
    lanes = _counter([row["policy"]["current_selected_lane"] for row in rows])
    taxonomy = sum(row["human"]["expected_kind"] != row["policy"]["actual_kind"] for row in rows)
    synthesis = {
        "missing_evidence_or_source_prior": sum(
            any(
                "evidence" in blocker or "confidence" in blocker or "source" in blocker
                for blocker in row["policy"]["blocker_codes"]
            )
            and row["human"]["retention_value"] == "retain"
            for row in held
        ),
        "kind_or_taxonomy_gate": sum(
            any(
                "kind" in blocker or "taxonomy" in blocker
                for blocker in row["policy"]["blocker_codes"]
            )
            for row in rows
        ),
        "cooling_delay": sum(row["policy"]["readiness_state"] == "cooling" for row in held),
        "conflict_or_supersession": sum(
            row["human"]["conflict_expected"] == "yes"
            or row["human"]["supersession_expected"] == "yes"
            for row in rows
            if not row["agreement"]
        ),
        "review_gate_missing": sum(
            row["automatic"] and row["human"]["human_review_required"] == "yes" for row in rows
        ),
        "retention_epistemic_or_temporal": sum(
            row["human"]["retention_value"] == "retain"
            and row["human"]["expected_storage_disposition"] != "retain"
            for row in held
        ),
        "non_propositional_or_extraction": sum(
            row["human"]["expected_kind"] == "unknown" for row in rows if not row["agreement"]
        ),
    }
    return {
        "report_schema_version": "engram-human-policy-comparison-v1",
        "final_corpus_digest": claimed,
        "snapshot_digest": snapshot.manifest.data_digest,
        "tranche_selection_digest": tranche.get("selection_digest"),
        "evaluation_at": snapshot.evaluation_at.isoformat(),
        "policy_config": snapshot.config.model_dump(mode="json") if snapshot.config else "unknown",
        "overall": {
            "final_labeled_n": len(rows),
            "automatic_admission_count": len(automatic_rows),
            "human_expected_storage_disposition": _counter(
                [row["human"]["expected_storage_disposition"] for row in rows]
            ),
            "agreement_count": sum(row["agreement"] for row in rows),
            "mismatch_count": sum(not row["agreement"] for row in rows),
        },
        "automatic_admission": {
            "human_permitted": len(automatic_rows) - len(false_auto),
            "false_automatic": len(false_auto),
            "precision_ppv": None
            if not automatic_rows
            else (len(automatic_rows) - len(false_auto)) / len(automatic_rows),
            "high_consequence_automatic": len(high_auto),
            "high_consequence_false_automatic": len(high_false_auto),
            "high_consequence_precision_ppv": None
            if not high_auto
            else (len(high_auto) - len(high_false_auto)) / len(high_auto),
        },
        "held_back": {
            "human_retention_retain": sum(
                row["human"]["retention_value"] == "retain" for row in held
            ),
            "human_expected_storage_retain": sum(
                row["human"]["expected_storage_disposition"] == "retain" for row in held
            ),
            "human_startup_eligible": sum(
                row["human"]["expected_startup_eligibility"] == "yes" for row in held
            ),
            "human_governed_eligible": sum(
                row["human"]["expected_governed_semantic_eligibility"] == "yes" for row in held
            ),
        },
        "review_requirement": {
            "review_yes_automatic": sum(
                row["automatic"] and row["human"]["human_review_required"] == "yes" for row in rows
            ),
            "review_no_terminal_blocked": sum(
                not row["automatic"]
                and row["human"]["human_review_required"] == "no"
                and row["policy"]["terminal_under_current_policy"] is True
                for row in rows
            ),
            "high_consequence_review_yes_automatic": sum(
                row["automatic"]
                and row["human"]["human_review_required"] == "yes"
                and row["human"]["consequence"] == "high"
                for row in rows
            ),
        },
        "retention_cross_tab": retention_cross,
        "consequence": consequence,
        "taxonomy": {"stored_kind_vs_human_expected_disagreements": taxonomy},
        "epistemic_support": _counter([row["human"]["epistemic_state"] for row in rows]),
        "policy_state": {"selected_lanes": lanes, "readiness": readiness, "blockers": blockers},
        "synthesis_counts": synthesis,
    }
