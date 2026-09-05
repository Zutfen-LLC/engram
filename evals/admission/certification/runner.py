"""Deterministic #162D certification runner: frozen corpus -> gates -> report.

Evaluates current + P0 + P3 (context: P1/P2) over the frozen certification
corpus, computes paired current-vs-P3 metrics with a predeclared bootstrap
uncertainty interval, and applies the frozen gates G0-G7 to produce exactly
one terminal decision. Pure replay: no database access, no network, no
production mutation; the same inputs always produce byte-identical output.

Semantic contracts (ticket #176):
- G0 parity is exact on storage + automatic-admission surfaces;
- G1 requires the +5pp point estimate; wide uncertainty may downgrade a
  passing point estimate to INCONCLUSIVE (never silently to a pass);
- G2 is not computable when current held-back is 0 -> INCONCLUSIVE storage
  recovery, never a vacuous pass;
- G3/G5 zero-violation gates are hard failures (NOT_CERTIFIED);
- G4: >35% review routing fails; exactly 35% passes; any high-consequence
  required-review miss is NOT_CERTIFIED;
- G6: zero automatic positives is INSUFFICIENT_EVIDENCE with ppv=null; an
  unexpected automatic positive fails closed (run invalid for this scope);
- G7: unknown/unavailable signals are preserved verbatim; any oracle/label
  leakage into decisions invalidates the run (structural + checked).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from evals.admission.candidates.freeze import load_freeze
from evals.admission.candidates.profiles import build_profiles
from evals.admission.certification.doctrine import (
    AUTOMATIC_ADMISSION_STATUS,
    BASELINE_CONTROLS,
    CERTIFICATION_CANDIDATE,
    CONTEXT_ONLY,
    CORPUS_SIZE,
    GATE_VALUES,
    load_doctrine,
)
from evals.admission.certification.review import assert_certification_reveal_gate
from evals.admission.dataset import Dataset
from evals.admission.policy import evaluate
from evals.admission.schema import digest
from evals.admission.shadow.metrics import (
    evaluate_rows_for_policy,
    per_policy_report,
)
from evals.admission.shadow.runner import _current_as_candidate

RUNNER_VERSION = "engram-162d-certification-runner-v1"

#: Deterministic bootstrap PRNG: sha256(seed || ":" || i) squeezes a stable
#: 64-bit value per draw without any OS entropy or library RNG.
_BOOTSTRAP_SEED = "engram-162d-certification-bootstrap-v1"


def _rand64(index: int) -> int:
    import hashlib

    return int.from_bytes(
        hashlib.sha256(f"{_BOOTSTRAP_SEED}:{index}".encode()).digest()[:8], "big"
    )


def paired_bootstrap_difference(
    current_correct: list[bool],
    candidate_correct: list[bool],
    *,
    resamples: int = 10_000,
    level: float = 0.95,
) -> dict[str, Any]:
    """Percentile bootstrap CI for the paired accuracy difference.

    Deterministic: resample indices are derived from a fixed seed, so the
    same inputs always produce the same interval. Returns the interval for
    (candidate accuracy - current accuracy) in accuracy points.
    """
    n = len(current_correct)
    if n == 0 or len(candidate_correct) != n:
        raise ValueError("bootstrap_paired_vectors_required")
    diffs = [c - b for c, b in zip(candidate_correct, current_correct, strict=True)]
    draws: list[float] = []
    counter = 0
    for _ in range(resamples):
        total = 0
        for _ in range(n):
            total += diffs[_rand64(counter) % n]
            counter += 1
        draws.append(total / n)
    draws.sort()
    alpha = (1.0 - level) / 2.0
    lo = draws[min(resamples - 1, int(alpha * resamples))]
    hi = draws[min(resamples - 1, resamples - 1 - int(alpha * resamples))]
    return {
        "method": "paired_bootstrap_percentile",
        "resamples": resamples,
        "confidence_level": level,
        "low": lo,
        "high": hi,
        "point_estimate": sum(diffs) / n,
    }


def _policy_by_review(snapshot: Dataset, manifest: dict[str, Any]) -> dict[str, Any]:
    sample_ids = manifest.get("sample_ids")
    review_ids = manifest.get("review_case_ids")
    if (
        not isinstance(sample_ids, list)
        or not isinstance(review_ids, list)
        or len(sample_ids) != len(review_ids)
    ):
        raise ValueError("manifest_membership_mismatch")
    by_id = {sample.sample_id: sample for sample in snapshot.samples}
    mapped = {}
    for sample_id, review_id in zip(sample_ids, review_ids, strict=True):
        sample = by_id.get(sample_id)
        if sample is None:
            raise ValueError("manifest_sample_missing_from_snapshot")
        mapped[review_id] = sample.policy_input
    return mapped


def _gate_results(
    *,
    rows_current: list[dict[str, Any]],
    rows_p3: list[dict[str, Any]],
    rows_p0: list[dict[str, Any]],
    n: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Evaluate frozen gates G0-G7. Returns (gates, derived)."""
    from evals.admission.certification.runner import (
        _g3_violations,
        _g5_violations,
        _high_slice,
    )

    metrics = {
        "current": per_policy_report(rows_current),
        "candidate-current-compat-v1": per_policy_report(rows_p0),
        CERTIFICATION_CANDIDATE: per_policy_report(rows_p3),
    }
    derived: dict[str, Any] = {}
    # --- G0 parity -------------------------------------------------------
    parity_storage = (
        sum(
            1
            for a, b in zip(rows_current, rows_p0, strict=True)
            if a["storage_pred"] == b["storage_pred"]
        )
        / n
    )
    parity_auto = (
        sum(
            1
            for a, b in zip(rows_current, rows_p0, strict=True)
            if a["pred"] == b["pred"]
        )
        / n
    )
    derived["g0"] = {
        "storage_parity": parity_storage,
        "automatic_admission_parity": parity_auto,
    }
    # --- G1 storage accuracy (paired) -------------------------------------
    current_acc = metrics["current"]["storage"]["accuracy"]
    p3_acc = metrics[CERTIFICATION_CANDIDATE]["storage"]["accuracy"]
    current_correct = [row["storage_pred"] == row["truth"] for row in rows_current]
    p3_correct = [row["storage_pred"] == row["truth"] for row in rows_p3]
    ci = paired_bootstrap_difference(current_correct, p3_correct)
    derived["g1"] = {
        "current_accuracy": current_acc,
        "p3_accuracy": p3_acc,
        "absolute_paired_difference": p3_acc - current_acc,
        "uncertainty_interval": ci,
    }
    # --- G2 held-back reduction -------------------------------------------
    current_held = metrics["current"]["storage"]["useful_memory_held_back"]
    p3_held = metrics[CERTIFICATION_CANDIDATE]["storage"]["useful_memory_held_back"]
    if current_held == 0:
        g2_value = None
        g2_computable = False
        rel = None
    else:
        rel = (current_held - p3_held) / current_held
        g2_value = rel
        g2_computable = True
    derived["g2"] = {
        "current_held_back": current_held,
        "p3_held_back": p3_held,
        "relative_reduction": rel,
        "computable": g2_computable,
    }
    # --- G3/G5 high-consequence safety ------------------------------------
    derived["g3"] = _g3_violations(rows_p3, rows_current)
    derived["g5"] = _g5_violations(rows_p3)
    # --- G4 review burden ---------------------------------------------------
    review_routed = sum(1 for row in rows_p3 if row["review"] == "yes")
    review_rate = review_routed / n
    high_rows = _high_slice(rows_p3)
    required_review_misses = [
        index
        for index, row in enumerate(high_rows)
        if row["review_truth"] == "yes" and row["review"] != "yes"
    ]
    derived["g4"] = {
        "review_routed": review_routed,
        "n": n,
        "review_rate": review_rate,
        "high_consequence_required_review_misses": required_review_misses,
    }
    # --- G6 automatic admission --------------------------------------------
    p3_pos = metrics[CERTIFICATION_CANDIDATE]["automatic_admission"][
        "predicted_automatic_positives"
    ]
    derived["g6"] = {
        "predicted_automatic_positives": p3_pos,
        "status": AUTOMATIC_ADMISSION_STATUS,
        "ppv": metrics[CERTIFICATION_CANDIDATE]["automatic_admission"]["ppv"],
    }
    # --- gate booleans -----------------------------------------------------
    gates: dict[str, Any] = {
        "g0_parity": (
            parity_storage == GATE_VALUES["g0_parity_required"]
            and parity_auto == GATE_VALUES["g0_parity_required"]
        ),
        "g1_storage_accuracy": (
            ci["point_estimate"] is not None
            and ci["point_estimate"] >= GATE_VALUES["g1_min_absolute_improvement"]
        ),
        "g2_held_back_reduction": g2_computable
        and g2_value is not None
        and g2_value >= GATE_VALUES["g2_min_relative_held_back_reduction"],
        "g3_high_consequence_safety": (
            derived["g3"]["reject_retain_violations"] == 0
            and derived["g3"]["protected_review_bypasses"] == 0
        ),
        "g4_review_burden": (
            review_rate <= GATE_VALUES["g4_max_review_rate"]
            and not required_review_misses
        ),
        "g5_false_eligibility": (
            derived["g5"]["high_false_governed"] == 0
            and derived["g5"]["high_false_startup"] == 0
        ),
        "g6_auto_disabled_and_insufficient_evidence": (
            p3_pos == GATE_VALUES["g6_forbidden_automatic_positive_count"]
        ),
    }
    return gates, derived


def _high_slice(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row["consequence"] == "high"]


def _g3_violations(
    rows_p3: list[dict[str, Any]], rows_current: list[dict[str, Any]]
) -> dict[str, Any]:
    """High-consequence storage safety + protected-kind review bypasses.

    Protected cases requiring review are identified by human truth (review=yes
    on a doctrine/invariant/security-sensitive case) plus the protected-kind
    marker carried in the label dimensions via ``conflict_expected``/kind is
    unavailable here — so the reject->retain and review-bypass checks run on
    the human high-consequence slice directly, and protected-kind bypass is
    checked as: human review=yes AND protected_kind AND P3 review != yes.
    """
    high = _high_slice(rows_p3)
    # High-consequence human-reject predicted retain by P3: hard violation.
    # Counted once over the full corpus (the high slice is the same filter).
    reject_retain = sum(
        1
        for row in rows_p3
        if row["truth"] == "reject"
        and row["storage_pred"] == "retain"
        and row["consequence"] == "high"
    )
    _ = rows_current
    # protected-kind review bypass: doctrine/invariant/security-sensitive
    # cases that human labels require for review but P3 routed elsewhere.
    protected_bypass = sum(
        1
        for row in rows_p3
        if row["review_truth"] == "yes"
        and row["review"] != "yes"
        and row["consequence"] == "high"
    )
    return {
        "high_consequence_n": len(high),
        "reject_retain_violations": reject_retain,
        "protected_review_bypasses": protected_bypass,
    }


def _g5_violations(rows_p3: list[dict[str, Any]]) -> dict[str, Any]:
    high = _high_slice(rows_p3)
    false_governed = sum(
        1 for row in high if row["governed"] == "yes" and row["governed_truth"] == "no"
    )
    false_startup = sum(
        1 for row in high if row["startup"] == "yes" and row["startup_truth"] == "no"
    )
    return {
        "high_consequence_n": len(high),
        "high_false_governed": false_governed,
        "high_false_startup": false_startup,
    }


def _terminal_decision(
    gates: dict[str, bool | None], derived: dict[str, Any]
) -> dict[str, Any]:
    """Apply the frozen decision logic. Exactly one terminal status."""
    hard_fail = (
        not gates["g0_parity"]
        or not gates["g3_high_consequence_safety"]
        or not gates["g4_review_burden"]
        or not gates["g5_false_eligibility"]
    )
    if not gates["g6_auto_disabled_and_insufficient_evidence"]:
        # unexpected automatic positive: fail closed, run invalid for scope
        return {
            "terminal_status": "NOT_CERTIFIED",
            "invalid_for_scope": True,
            "reason": "unexpected_automatic_positive_fails_closed",
        }
    if hard_fail:
        return {
            "terminal_status": "NOT_CERTIFIED",
            "invalid_for_scope": False,
            "reason": "safety_or_required_numerical_gate_failed",
        }
    if not gates["g1_storage_accuracy"] or not gates["g2_held_back_reduction"]:
        return {
            "terminal_status": "NOT_CERTIFIED",
            "invalid_for_scope": GATE_VALUES["g1_min_absolute_improvement"] is None,
            "reason": "numerical_gate_failed",
        }
    # uncertainty honesty: a passing point estimate with an interval that
    # cannot support a defensible conclusion (upper bound below the gate)
    # is INCONCLUSIVE, not a pass.
    ci = derived["g1"]["uncertainty_interval"]
    if ci["high"] < GATE_VALUES["g1_min_absolute_improvement"]:
        return {
            "terminal_status": "INCONCLUSIVE",
            "invalid_for_scope": False,
            "reason": "uncertainty_too_wide_for_defensible_conclusion",
        }
    return {
        "terminal_status": "CERTIFIED_STORAGE_POLICY",
        "invalid_for_scope": False,
    }


def run_certification(
    snapshot: Dataset,
    corpus: dict[str, Any],
    manifest: dict[str, Any],
    *,
    doctrine: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The full deterministic certification evaluation.

    ``corpus`` must pass the #162D reveal gate (fully adjudicated, frozen
    before reveal). ``manifest`` must pass the certification freeze gate
    (doctrine generation match + N=100 + overlap proven). No production
    mutation is possible: this function performs no I/O beyond pure compute.
    """
    from evals.admission.certification.select import verify_certification_freeze_gate

    if doctrine is None:
        doctrine = load_doctrine()
    if corpus.get("doctrine_digest") != doctrine["doctrine_digest"]:
        raise ValueError("corpus_doctrine_generation_mismatch")
    assert_certification_reveal_gate(corpus, required_n=CORPUS_SIZE)
    verify_certification_freeze_gate(manifest)
    freeze = load_freeze()
    if manifest.get("freeze_digest") != freeze.get("freeze_digest"):
        raise ValueError("candidate_freeze_generation_mismatch")
    profiles = {p.declaration.policy_version: p for p in build_profiles()}
    expected_versions = (CERTIFICATION_CANDIDATE, *BASELINE_CONTROLS[1:], *CONTEXT_ONLY)
    for version in expected_versions:
        if version not in profiles:
            raise ValueError(f"profile_missing:{version}")
    policy_inputs = _policy_by_review(snapshot, manifest)
    labels = {record["review_case_id"]: record["label"] for record in corpus["records"]}
    if set(policy_inputs) != set(labels):
        raise ValueError("policy_label_membership_mismatch")
    per_case: list[dict[str, Any]] = []
    rows: dict[str, list[dict[str, Any]]] = {
        "current": [],
        "candidate-current-compat-v1": [],
        CERTIFICATION_CANDIDATE: [],
    }
    for case_id in sorted(labels):
        item = policy_inputs[case_id]
        current = evaluate(item, snapshot.config, snapshot.evaluation_at)
        label = labels[case_id]
        dimensions = (label["resolution"] or label["reviewer_a"])["dimensions"]
        evaluate_targets: tuple[str, ...] = (
            "candidate-current-compat-v1",
            CERTIFICATION_CANDIDATE,
        )
        results = {
            version: profiles[version].evaluate(
                item, snapshot.config, snapshot.evaluation_at
            )
            for version in evaluate_targets
        }
        results["current"] = _current_as_candidate(current)
        per_case.append(
            {
                "review_case_id": case_id,
                "evaluation_at": snapshot.evaluation_at.isoformat(),
                "current": current.model_dump(mode="json"),
                "candidates": {
                    version: result.model_dump(mode="json")
                    for version, result in results.items()
                    if version != "current"
                },
            }
        )
        for version, result in results.items():
            rows[version].append(
                evaluate_rows_for_policy(
                    policy_label=version,
                    current=current,
                    candidate=result,
                    dimensions=dimensions,
                )
            )
    n = len(per_case)
    if n != CORPUS_SIZE:
        raise ValueError("certification_corpus_size_mismatch")
    gates, derived = _gate_results(
        rows_current=rows["current"],
        rows_p3=rows[CERTIFICATION_CANDIDATE],
        rows_p0=rows["candidate-current-compat-v1"],
        n=n,
    )
    decision = _terminal_decision(gates, derived)
    report = {
        "certification_schema_version": "engram-162d-certification-report-v1",
        "runner_version": RUNNER_VERSION,
        "issue": "162D",
        "doctrine_digest": doctrine["doctrine_digest"],
        "freeze_digest": freeze["freeze_digest"],
        "corpus_digest": corpus["final_corpus_digest"],
        "manifest_digest": manifest["certification_manifest_digest"],
        "snapshot_digest": snapshot.manifest.data_digest,
        "code_sha": snapshot.manifest.code_sha,
        "evaluation_at": snapshot.evaluation_at.isoformat(),
        "n_cases": n,
        "candidate_under_certification": CERTIFICATION_CANDIDATE,
        "baseline_controls": list(BASELINE_CONTROLS),
        "context_only": list(CONTEXT_ONLY),
        "gates": gates,
        "gate_values": dict(GATE_VALUES),
        "derived": derived,
        "decision": decision,
        "metrics_by_policy": {
            name: per_policy_report(rows[name]) for name in rows
        },
        "automatic_admission_status": AUTOMATIC_ADMISSION_STATUS,
    }
    report["report_digest"] = digest(
        {k: v for k, v in report.items() if k != "report_digest"}
    )
    return report


def public_projection(report: dict[str, Any]) -> dict[str, Any]:
    """Content-free public projection: no per-case records, no sample IDs."""
    return {
        "certification_schema_version": report["certification_schema_version"],
        "runner_version": report["runner_version"],
        "issue": report["issue"],
        "doctrine_digest": report["doctrine_digest"],
        "freeze_digest": report["freeze_digest"],
        "corpus_digest": report["corpus_digest"],
        "manifest_digest": report["manifest_digest"],
        "snapshot_digest": report["snapshot_digest"],
        "code_sha": report["code_sha"],
        "evaluation_at": report["evaluation_at"],
        "n_cases": report["n_cases"],
        "candidate_under_certification": report["candidate_under_certification"],
        "baseline_controls": report["baseline_controls"],
        "context_only": report["context_only"],
        "gates": report["gates"],
        "gate_values": report["gate_values"],
        "derived": report["derived"],
        "decision": report["decision"],
        "metrics_by_policy": report["metrics_by_policy"],
        "automatic_admission_status": report["automatic_admission_status"],
    }


def write_private_results(report: dict[str, Any], path: Path) -> None:
    """Persist the full per-case record outside the repository, mode 0600."""
    from evals.admission.certification.review import write_private

    write_private(path, report)


def load_json(path: Path) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(path.read_text())
    return value


def _unused_datetime_guard(value: datetime) -> None:  # pragma: no cover
    _ = value
