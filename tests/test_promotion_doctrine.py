"""Promotion-doctrine regression tests (ENG-PROMOTION-003A / issue #154).

These tests keep the repository's *documentation-facing claims* aligned with
actual field semantics:

* no surface may claim Path B (usage quorum) promotes, or that useful feedback
  accumulates promotion evidence, while no implemented policy version declares
  a quorum lane;
* production classification must not be described as blending classifier
  confidence into ``memory_confidence`` (it does not);
* the required-retention-confidence math must match the active policy version.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import get_args

import pytest

from engram.promotion_policy import (
    EVIDENCE_RETENTION_WEIGHT,
    EVIDENCE_SCORE_CEILING,
    EVIDENCE_SOURCE_PRIOR_WEIGHT,
    PromotionBasis,
    PromotionPolicyError,
    evidence_score_v1,
    required_retention_confidence_v1,
)
from engram.promotion_readiness import (
    EVIDENCE_STATE_BOUND_BELOW_THRESHOLD,
    EVIDENCE_STATE_BOUND_QUALIFIED,
    EVIDENCE_STATE_MALFORMED_STALE,
    EVIDENCE_STATE_NONE,
    JOB_STATE_DEAD,
    JOB_STATE_MISSING,
    JOB_STATE_OVERDUE,
    JOB_STATE_SCHEDULED,
    READINESS_BELOW_LEGACY,
    READINESS_BELOW_TAXONOMY,
    READINESS_BELOW_THRESHOLD,
    READINESS_CONFLICT_OR_DISPUTE,
    READINESS_COOLING,
    READINESS_ELIGIBLE_NOW,
    READINESS_EVIDENCE_DISABLED,
    READINESS_KIND_POLICY,
    READINESS_MALFORMED_EVIDENCE,
    READINESS_MISSING_EVIDENCE,
    READINESS_REVIEW_POLICY,
    ReadinessClassification,
    classify_readiness,
    readiness_state_from_blockers,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Claim patterns that assert unimplemented Path B behavior as active policy.
_STALE_PATH_B_PATTERNS = (
    re.compile(r"(?i)path\s+b\s+(requires|promotes)\b"),
    re.compile(r"(?i)requires\s+(2\+|two)\s+distinct\s+non-author\s+principals"),
    re.compile(r"(?i)marking\s+it\s+useful\s+via\s+feedback"),
)

_SCANNED_SUFFIXES = {".py", ".md", ".json", ".yml", ".yaml", ".toml", ".txt", ".sql", ".sh"}
# Directory names never descended into. The scan is a pure filesystem walk —
# the CI test image has no `git` binary, so `git ls-files` is not an option —
# which also means untracked scratch files are scanned; keeping the tree
# clean is already the repo's convention.
_SCANNED_PRUNED_DIRS = {
    ".git",
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "test-results",
}


def _path_b_is_implemented() -> bool:
    """True only if a promotion policy version declares a quorum basis.

    The guard below intentionally fails while the only implemented bases are
    ``legacy_confidence`` and ``retention_evidence``. When a future policy
    version implements Path B it must add its basis to ``PromotionBasis``,
    which re-enables the previously stale language.
    """
    return set(get_args(PromotionBasis)) - {"legacy_confidence", "retention_evidence"} != set()


def _tracked_text_files() -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SCANNED_PRUNED_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix.lower() not in _SCANNED_SUFFIXES:
                continue
            if path == Path(__file__):
                continue
            files.append(path)
    return sorted(files)


def test_no_stale_path_b_claim_in_repository() -> None:
    """The 'Path B requires two useful principals' claim must not return.

    Runs only while Path B is unimplemented; an implemented policy version
    (a new ``PromotionBasis`` lane) re-enables the language.
    """
    if _path_b_is_implemented():
        pytest.skip("an implemented promotion policy version declares a Path B basis")
    offenders: list[str] = []
    for path in _tracked_text_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if any(pattern.search(line) for pattern in _STALE_PATH_B_PATTERNS):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Stale Path B promotion claims found (Path B is deferred/unimplemented; "
        "positive feedback is not promotion evidence):\n" + "\n".join(offenders)
    )


def test_eval_corpus_promotion_doctrine_matches_policy() -> None:
    corpus = json.loads((REPO_ROOT / "evals" / "golden" / "corpus_v2.json").read_text())
    contents = [memory["content"] for memory in corpus["memories"]]
    stale = [c for c in contents if any(p.search(c) for p in _STALE_PATH_B_PATTERNS)]
    assert not stale, f"eval corpus carries stale promotion doctrine: {stale}"
    doctrine = [c for c in contents if "Path A" in c and "Path B" in c]
    assert doctrine, "expected the promotion-doctrine corpus entry to mention both paths"
    assert any("deferred" in c and "not promotion evidence" in c for c in doctrine), (
        "promotion doctrine must state that Path B is deferred and that useful "
        "feedback is not promotion evidence"
    )
    assert any(
        "never raises confidence" in c and "importance only" in c for c in contents
    ), "corpus must not claim usefulness signals raise memory_confidence"


def test_worker_classification_refine_docstring_makes_no_blend_claim() -> None:
    from engram.worker import handle_classification_refine

    docstring = handle_classification_refine.__doc__ or ""
    assert "monotonic" not in docstring.lower()
    # The stale positive claim ("blend memory_confidence") must be gone;
    # explicit negative statements ("does not blend ...") are required.
    normalized = re.sub(r"\s+", " ", docstring.lower())
    assert "blend memory_confidence" not in normalized
    assert "does not blend" in normalized or "not blend" in normalized
    # The docstring must name the actual trust-field behavior.
    assert "source-policy prior" in docstring


def test_production_paths_never_call_blend_memory_confidence() -> None:
    """Only the deprecated helper's own module may call it (call sites only)."""
    referencing_files: list[str] = []
    for path in (REPO_ROOT / "engram").rglob("*.py"):
        if path.name == "classification_trust.py" or "__pycache__" in path.parts:
            continue
        if "blend_memory_confidence(" in path.read_text(encoding="utf-8"):
            referencing_files.append(str(path.relative_to(REPO_ROOT)))
    assert not referencing_files, (
        "production code must not blend classifier confidence into "
        f"memory_confidence: {referencing_files}"
    )


def test_feedback_is_not_promotion_evidence() -> None:
    """Useful verdicts move importance (and a recall counter), nothing else."""
    from engram.feedback import FeedbackEffect, effect_for_feedback

    source = (REPO_ROOT / "engram" / "feedback.py").read_text(encoding="utf-8")
    assert "retention" not in source
    assert "promotion" not in source
    effect = effect_for_feedback(principal_type="agent", is_item_author=False, verdict="useful")
    assert isinstance(effect, FeedbackEffect)
    assert effect.importance_delta > 0.0
    assert list(effect.__dataclass_fields__) == [
        "importance_delta",
        "reset_startup_recall_count",
    ]


# --- Required-retention math (active policy version) ---------------------------


def test_required_retention_confidence_sync_turn_regression_fixture() -> None:
    """sync_turn prior 0.4 vs threshold 0.70 requires retention 0.775.

    Exact fixture from ENG-PROMOTION-003A verification item 5: under the
    active promotion-evidence-v1 weights (0.20 prior / 0.80 retention).
    """
    required = required_retention_confidence_v1(0.4, 0.70)
    assert required == pytest.approx(0.775)
    assert EVIDENCE_SOURCE_PRIOR_WEIGHT == 0.20
    assert EVIDENCE_RETENTION_WEIGHT == 0.80
    # Round trip: a 0.775 retention confidence on a 0.4 prior lands exactly on
    # the 0.70 threshold.
    assert evidence_score_v1(0.4, 0.775) == pytest.approx(0.70)


def test_required_retention_confidence_unreachable_cases() -> None:
    # Threshold above the score ceiling: no retention value can reach it.
    assert required_retention_confidence_v1(1.0, 0.86) is None
    # Required retention above the 0.95 classifier clamp.
    assert required_retention_confidence_v1(0.0, 0.80) is None
    # A high prior with a low threshold needs nothing.
    assert required_retention_confidence_v1(0.9, 0.18) == 0.0
    # Exactly reachable at the clamp.
    assert required_retention_confidence_v1(0.0, 0.76) == pytest.approx(0.95)


def test_required_retention_confidence_fails_closed_on_invalid_inputs() -> None:
    with pytest.raises(PromotionPolicyError):
        required_retention_confidence_v1(1.5, 0.70)
    with pytest.raises(PromotionPolicyError):
        required_retention_confidence_v1(float("nan"), 0.70)


def test_evidence_score_ceiling_unchanged() -> None:
    """No production policy change: v1 score shape is byte-identical."""
    assert EVIDENCE_SCORE_CEILING == 0.85
    assert evidence_score_v1(0.4, 0.95) == pytest.approx(0.84)


# --- Readiness-state vocabulary -------------------------------------------------


def test_evidence_state_vocabulary_is_canonical() -> None:
    assert EVIDENCE_STATE_NONE == "none"
    assert EVIDENCE_STATE_BOUND_QUALIFIED == "bound-qualified"
    assert EVIDENCE_STATE_BOUND_BELOW_THRESHOLD == "bound-below-threshold"
    assert EVIDENCE_STATE_MALFORMED_STALE == "malformed/stale"
    assert {JOB_STATE_SCHEDULED, JOB_STATE_OVERDUE, JOB_STATE_DEAD, JOB_STATE_MISSING} == {
        "scheduled",
        "overdue",
        "dead",
        "missing",
    }


@pytest.mark.parametrize(
    ("blockers", "expected"),
    [
        (["kind_policy"], READINESS_KIND_POLICY),
        (["review_policy"], READINESS_REVIEW_POLICY),
        (["conflict"], READINESS_CONFLICT_OR_DISPUTE),
        (["external_dispute"], READINESS_CONFLICT_OR_DISPUTE),
        (["evidence_disabled"], READINESS_EVIDENCE_DISABLED),
        (["no_retention_evidence"], READINESS_MISSING_EVIDENCE),
        (["missing_source_prior"], READINESS_MISSING_EVIDENCE),
        (["evidence_version"], READINESS_MALFORMED_EVIDENCE),
        (["evidence_inconsistent"], READINESS_MALFORMED_EVIDENCE),
        (["evidence_score"], READINESS_BELOW_THRESHOLD),
        (["taxonomy_confidence"], READINESS_BELOW_TAXONOMY),
        (["confidence"], READINESS_BELOW_LEGACY),
        (["age"], READINESS_COOLING),
        ([], READINESS_ELIGIBLE_NOW),
    ],
)
def test_readiness_state_precedence(blockers: list[str], expected: str) -> None:
    assert readiness_state_from_blockers(blockers) == expected


def test_readiness_state_policy_outranks_age_and_score() -> None:
    assert readiness_state_from_blockers(["kind_policy", "age", "evidence_score"]) == (
        READINESS_KIND_POLICY
    )
    assert readiness_state_from_blockers(["confidence", "age"]) == READINESS_BELOW_LEGACY


def test_classify_readiness_lane_aware_terminality() -> None:
    """Cooling is time-dependent even with other-lane blockers present."""
    # Legacy-qualified cooling item that also carries irrelevant evidence
    # blockers (evidence lane disabled, no receipt): time alone promotes it.
    cooling = classify_readiness(
        is_candidate=True,
        blockers=["age", "missing_source_prior", "no_retention_evidence",
                  "retention_disposition", "evidence_disabled"],
        legacy_trust_qualified=True,
        evidence_trust_qualified=False,
        selected_basis=None,
    )
    assert cooling == ReadinessClassification(
        readiness_state=READINESS_COOLING,
        terminal_under_current_policy=False,
        can_auto_promote_without_new_evidence_or_review=True,
    )
    # Neither lane trust-qualified: no amount of time helps.
    stuck = classify_readiness(
        is_candidate=True,
        blockers=["confidence", "no_retention_evidence", "age"],
        legacy_trust_qualified=False,
        evidence_trust_qualified=False,
        selected_basis=None,
    )
    assert stuck.terminal_under_current_policy is True
    assert stuck.can_auto_promote_without_new_evidence_or_review is False
    assert stuck.readiness_state == READINESS_MISSING_EVIDENCE
    # Kind policy blocks even a trust-qualified item.
    kind_blocked = classify_readiness(
        is_candidate=True,
        blockers=["kind_policy", "age"],
        legacy_trust_qualified=True,
        evidence_trust_qualified=False,
        selected_basis=None,
    )
    assert kind_blocked.readiness_state == READINESS_KIND_POLICY
    assert kind_blocked.terminal_under_current_policy is True
    # Selected basis with no residual gate is eligible now.
    eligible = classify_readiness(
        is_candidate=True,
        blockers=[],
        legacy_trust_qualified=True,
        evidence_trust_qualified=True,
        selected_basis="retention_evidence",
    )
    assert eligible.readiness_state == READINESS_ELIGIBLE_NOW
    assert eligible.terminal_under_current_policy is False
