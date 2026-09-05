"""Report operational observations separately from labeled comparisons."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from engram.promotion_policy import (
    EVIDENCE_PROMOTION_POLICY_VERSION,
    LEGACY_PROMOTION_POLICY_VERSION,
)
from evals.admission.dataset import Dataset, operational_counts
from evals.admission.policy import evaluate
from evals.admission.schema import digest


def report(dataset: Dataset) -> dict[str, object]:
    metrics: Counter[str] = Counter()
    for sample in dataset.samples:
        if sample.label is None:
            metrics["unlabeled"] += 1
            continue
        label = sample.label.final_dimensions()
        if label is None:
            metrics["unresolved_excluded"] += 1
            continue
        result = evaluate(sample.policy_input, dataset.config, dataset.evaluation_at)
        metrics["labeled"] += 1
        metrics[
            "synthetic_authored"
            if sample.label.label_origin == "synthetic_authored"
            else "human_adjudicated"
        ] += 1
        if label.expected_kind != "unknown":
            metrics["kind_comparisons"] += 1
            metrics["kind_matches"] += label.expected_kind == result.actual_kind
        else:
            metrics["kind_unknown"] += 1
        if result.would_promote is None:
            metrics["policy_unknown"] += 1
            metrics["acceptable_unknown"] += label.acceptable_abstention == "yes"
            continue
        automatic = result.would_promote
        metrics[f"retention_{label.retention_value}_automatic_{automatic}"] += 1
        if label.human_review_required != "unknown":
            metrics["review_comparisons"] += 1
            metrics["review_required_automatic_failures"] += (
                label.human_review_required == "yes" and automatic
            )
        if label.consequence == "high":
            metrics["high_consequence_comparisons"] += 1
            metrics["high_consequence_failures"] += automatic and (
                label.human_review_required == "yes"
                or label.expected_startup_eligibility == "no"
                or label.expected_storage_disposition == "reject"
            )
        if label.expected_blockers is not None:
            metrics["blocker_comparisons"] += 1
            metrics["blocker_matches"] += set(label.expected_blockers) == set(result.blocker_codes)
        if label.expected_next_action != "unknown":
            # Only automatic readiness and cooling have observable next actions here.
            # A terminal policy blocker does not establish a human review requirement.
            action = (
                "automatic_admission"
                if automatic
                else "wait"
                if result.readiness_state == "cooling"
                else "unknown"
            )
            metrics["next_action_comparisons"] += 1
            metrics["next_action_matches"] += action == label.expected_next_action
        metrics["acceptable_abstention_labeled"] += label.acceptable_abstention == "yes"
    return {
        "report_version": "engram-admission-baseline-v1",
        "runner_digest": runner_digest(),
        "policy_versions_evaluated": [
            LEGACY_PROMOTION_POLICY_VERSION,
            EVIDENCE_PROMOTION_POLICY_VERSION,
        ],
        "code_sha": dataset.manifest.code_sha,
        "manifest_digest": digest(dataset.manifest.model_dump(mode="json")),
        "dataset_digest": dataset.manifest.data_digest,
        "snapshot_as_of": dataset.manifest.snapshot_as_of.isoformat(),
        "evaluation_at": dataset.evaluation_at.isoformat(),
        "config": dataset.config.model_dump(mode="json") if dataset.config else "unknown",
        "operational_observations": operational_counts(dataset),
        "labeled_contract_comparisons": dict(sorted(metrics.items())),
        "limitations": [
            "Static promotion readiness only; conflict_recheck_status=not_run.",
            "Kind comparison uses captured taxonomy; no classifier is executed.",
            "Retention comparison measures promotion readiness, not capture rejection.",
            "A blocked promotion does not establish an epistemic abstention or review requirement.",
            "No dogfood precision, factual accuracy, calibration, or rollout gates are claimed.",
            "Synthetic judgments are authored expectations. They are not human dogfood labels.",
        ],
    }


def runner_digest() -> str:
    return digest({p.name: p.read_text() for p in sorted(Path(__file__).parent.glob("*.py"))})


def result_artifact(dataset: Dataset) -> dict[str, object]:
    return {
        "result_schema_version": "engram-admission-policy-results-v1",
        "manifest_digest": digest(dataset.manifest.model_dump(mode="json")),
        "runner_digest": runner_digest(),
        "evaluation_at": dataset.evaluation_at.isoformat(),
        "config": dataset.config.model_dump(mode="json") if dataset.config else None,
        "results": [
            evaluate(s.policy_input, dataset.config, dataset.evaluation_at).model_dump(mode="json")
            for s in dataset.samples
        ],
    }
