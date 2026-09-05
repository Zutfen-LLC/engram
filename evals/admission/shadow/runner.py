"""Deterministic #162C shadow runner: current vs candidates vs frozen labels.

Evaluates a captured dataset through the canonical current policy and every
declared candidate profile, then — only when a frozen human corpus for the
same tranche exists — joins the frozen labels as evaluation truth. Results are
immutable, content-addressed, and byte-identical for identical inputs.

No function here mutates production state: evaluation is pure; the runner
performs no database access at all (frozen replay).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evals.admission.candidates.contract import (
    CandidateResult,
    StorageDisposition,
)
from evals.admission.candidates.freeze import load_freeze
from evals.admission.candidates.profiles import build_profiles
from evals.admission.dataset import Dataset
from evals.admission.human_corpus import assert_reveal_gate
from evals.admission.policy import PolicyEvaluationResult, evaluate
from evals.admission.schema import digest
from evals.admission.shadow.metrics import evaluate_rows_for_policy, per_policy_report

RUNNER_VERSION = "engram-162c-shadow-runner-v1"


def _policy_by_review(
    snapshot: Dataset, tranche: dict[str, Any]
) -> dict[str, Any]:
    """Map review_case_id -> captured policy input (mirrors #162B comparison)."""
    sample_ids = tranche.get("sample_ids")
    review_ids = tranche.get("review_case_ids")
    if (
        not isinstance(sample_ids, list)
        or not isinstance(review_ids, list)
        or len(sample_ids) != len(review_ids)
    ):
        raise ValueError("tranche_membership_mismatch")
    by_id = {sample.sample_id: sample for sample in snapshot.samples}
    mapped = {}
    for sample_id, review_id in zip(sample_ids, review_ids, strict=True):
        sample = by_id.get(sample_id)
        if sample is None:
            raise ValueError("tranche_sample_missing_from_snapshot")
        mapped[review_id] = sample.policy_input
    return mapped


def run_shadow(
    snapshot: Dataset,
    corpus: dict[str, Any],
    tranche: dict[str, Any],
    *,
    freeze: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate current + candidates over one tranche with frozen labels.

    ``corpus`` must pass the #162B reveal gate (fully adjudicated, immutable).
    The returned record is content-free in its public projection; per-case
    reason codes reference captured state only, never memory text.
    """
    unsigned = {k: v for k, v in corpus.items() if k != "final_corpus_digest"}
    if corpus.get("final_corpus_digest") != digest(unsigned):
        raise ValueError("final_corpus_digest_mismatch")
    assert_reveal_gate(corpus)
    if tranche.get("snapshot_identity") != snapshot.manifest.data_digest:
        raise ValueError("snapshot_identity_mismatch")
    if freeze is None:
        freeze = load_freeze()
    policy_inputs = _policy_by_review(snapshot, tranche)
    labels = {record["review_case_id"]: record["label"] for record in corpus["records"]}
    if set(policy_inputs) != set(labels):
        raise ValueError("policy_label_membership_mismatch")
    profiles = build_profiles()
    frozen_versions = tuple(
        d["policy_version"] for d in freeze["candidate_declarations"]
    )
    if frozen_versions != tuple(p.declaration.policy_version for p in profiles):
        raise ValueError("candidate_freeze_mismatch")
    per_case: list[dict[str, Any]] = []
    rows_by_policy: dict[str, list[dict[str, Any]]] = {
        p.declaration.policy_version: [] for p in profiles
    }
    rows_by_policy["current"] = []
    for case_id in sorted(labels):
        item = policy_inputs[case_id]
        current = evaluate(item, snapshot.config, snapshot.evaluation_at)
        label = labels[case_id]
        dimensions = (label["resolution"] or label["reviewer_a"])["dimensions"]
        candidate_results: dict[str, CandidateResult] = {
            profile.declaration.policy_version: profile.evaluate(
                item, snapshot.config, snapshot.evaluation_at
            )
            for profile in profiles
        }
        per_case.append(
            {
                "review_case_id": case_id,
                "evaluation_at": snapshot.evaluation_at.isoformat(),
                "current": current.model_dump(mode="json"),
                "candidates": {
                    version: result.model_dump(mode="json")
                    for version, result in candidate_results.items()
                },
            }
        )
        for version, result in candidate_results.items():
            rows_by_policy[version].append(
                evaluate_rows_for_policy(
                    policy_label=version,
                    current=current,
                    candidate=result,
                    dimensions=dimensions,
                )
            )
        rows_by_policy["current"].append(
            evaluate_rows_for_policy(
                policy_label="current",
                current=current,
                candidate=_current_as_candidate(current),
                dimensions=dimensions,
            )
        )
    metrics = {
        name: per_policy_report(rows) for name, rows in rows_by_policy.items()
    }
    report = {
        "shadow_report_schema_version": "engram-162c-shadow-comparison-v1",
        "runner_version": RUNNER_VERSION,
        "dataset_role": "dogfood-development-v1",
        "dataset_role_classification": "development / hypothesis-generating",
        "snapshot_digest": snapshot.manifest.data_digest,
        "tranche_selection_digest": tranche.get("selection_digest"),
        "final_corpus_digest": corpus["final_corpus_digest"],
        "freeze_digest": freeze.get("freeze_digest"),
        "code_sha": snapshot.manifest.code_sha,
        "evaluation_at": snapshot.evaluation_at.isoformat(),
        "n_cases": len(per_case),
        "per_case": per_case,
        "metrics_by_policy": metrics,
    }
    report["report_digest"] = digest(
        {k: v for k, v in report.items() if k != "report_digest"}
    )
    return report


def _current_as_candidate(current: PolicyEvaluationResult) -> CandidateResult:
    """Project the current policy onto the candidate result shape.

    Used only so current and candidates flow through identical metric code.
    The projection preserves the current policy's semantics: single Boolean,
    storage mirror, no review surface.
    """
    if current.current_policy_version == "unknown":
        return CandidateResult(
            candidate_policy_version="current-unknown",
            storage_disposition="unknown",
            automatic_admission="unknown",
            governed_semantic_eligibility="unknown",
            startup_eligibility="unknown",
            human_review_required="unknown",
        )
    automatic = current.would_promote is True
    storage: StorageDisposition
    if current.readiness_state == "not_a_promotion_candidate":
        storage = "reject"
    elif automatic or current.readiness_state == "cooling":
        storage = "retain"
    elif current.readiness_state in (
        "missing_evidence",
        "below_evidence_threshold",
        "below_legacy_confidence_threshold",
    ) or any(
        blocker
        in ("no_retention_evidence", "missing_source_prior", "evidence_score",
            "retention_disposition", "evidence_disabled")
        for blocker in current.blocker_codes
    ):
        storage = "defer"
    else:
        storage = "reject"
    return CandidateResult(
        candidate_policy_version=f"current-{current.current_policy_version}",
        storage_disposition=storage,
        automatic_admission="yes" if automatic else "no",
        governed_semantic_eligibility="yes" if automatic else "no",
        startup_eligibility="yes" if automatic else "no",
        human_review_required="no",
    )


def write_private_results(report: dict[str, Any], path: Path) -> str:
    """Persist the full per-case record outside the repository, mode 0600."""
    repo = Path(__file__).resolve().parents[3]
    if path.resolve().is_relative_to(repo):
        raise ValueError("private_output_must_be_outside_repository")
    import os

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(json.dumps(report, sort_keys=True, indent=2) + "\n")
    return path.name


def public_projection(report: dict[str, Any]) -> dict[str, Any]:
    """Content-free aggregate for the repository: no per-case records."""
    return {
        "shadow_report_schema_version": report["shadow_report_schema_version"],
        "runner_version": report["runner_version"],
        "dataset_role": report["dataset_role"],
        "dataset_role_classification": report["dataset_role_classification"],
        "snapshot_digest": report["snapshot_digest"],
        "tranche_selection_digest": report["tranche_selection_digest"],
        "final_corpus_digest": report["final_corpus_digest"],
        "freeze_digest": report["freeze_digest"],
        "code_sha": report["code_sha"],
        "evaluation_at": report["evaluation_at"],
        "n_cases": report["n_cases"],
        "metrics_by_policy": report["metrics_by_policy"],
        "report_digest_scope": (
            "digest covers the private per-case record; this projection omits it"
        ),
    }
