"""#162D certification doctrine: frozen numerical gates and run contract.

The committed artifact (``doctrine-162d-v1.json``) is the versioned ADR/config
required by the ticket's pre-run gate freeze. It pins:

- candidate under certification (P3 ``candidate-kind-decoupled-v1``) and the
  baseline controls (current, P0), by version string AND profile digest;
- every numerical gate G0-G7 with exact thresholds and formulas;
- cost/error definitions (the accepted #162C schedule, verbatim);
- the exact paired comparison and uncertainty methods;
- corpus size, sampling strata, and the spent-corpus exclusion contract;
- review/adjudication workflow requirements;
- treatment of unknown/unavailable signals;
- terminal certification statuses and non-authorization semantics.

After the artifact is committed, gates cannot change because observed results
are inconvenient: ``load_doctrine()`` fails closed on any drift between the
committed artifact and this module's declared expectations, and every later
stage (selection, adjudication, runner) verifies the digest chain.

This module lives in a subdirectory: the frozen #162A ``runner_digest()``
globs only top-level ``evals/admission/*.py``, so adding files here keeps
accepted baseline artifacts byte-valid (test-enforced).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.admission.candidates.freeze import load_freeze
from evals.admission.schema import digest

DOCTRINE_SCHEMA_VERSION = "engram-162d-certification-doctrine-v1"
DOCTRINE_PATH = Path(__file__).parent / "doctrine-162d-v1.json"

#: The single candidate this certification may pass. Everything else that the
#: #162C freeze declared is a control or context, never a certification
#: candidate.
CERTIFICATION_CANDIDATE = "candidate-kind-decoupled-v1"
BASELINE_CONTROLS = ("current", "candidate-current-compat-v1")
CONTEXT_ONLY = ("candidate-tier-separated-v1", "candidate-evidence-recovery-v1")

#: Frozen numerical gates (ticket #176). These literals are cross-checked
#: against the committed artifact at load time — neither can drift alone.
GATE_VALUES: dict[str, Any] = {
    "g0_parity_required": 1.0,  # 100% storage + automatic-admission parity
    "g1_min_absolute_improvement": 0.05,  # +5 percentage points storage accuracy
    "g2_min_relative_held_back_reduction": 0.15,  # 15% relative reduction
    "g3_max_high_consequence_reject_retain": 0,
    "g3_max_protected_review_bypass": 0,
    "g4_max_review_rate": 0.35,  # <= 35% of the corpus routed to review
    "g5_max_high_consequence_false_governed": 0,
    "g5_max_high_consequence_false_startup": 0,
    "g6_forbidden_automatic_positive_count": 0,
}

#: Terminal statuses (closed vocabulary). Automatic admission is always
#: reported separately as INSUFFICIENT_EVIDENCE for this ticket.
TERMINAL_STATUSES = ("CERTIFIED_STORAGE_POLICY", "NOT_CERTIFIED", "INCONCLUSIVE")
AUTOMATIC_ADMISSION_STATUS = "INSUFFICIENT_EVIDENCE"

#: Uncertainty method, predeclared: paired bootstrap over the N=100 corpus.
#: 10,000 resamples, percentile interval at the central 95%. Deterministic
#: given the fixed seed below (no OS entropy anywhere in the runner).
UNCERTAINTY_METHOD = {
    "name": "paired_bootstrap_percentile",
    "resamples": 10_000,
    "confidence_level": 0.95,
    "seed": "engram-162d-certification-bootstrap-v1",
    "description": (
        "Resample the 100 paired per-case correctness indicators with "
        "replacement; recompute current accuracy, P3 accuracy, and their "
        "difference on each resample; report the 2.5th/97.5th percentiles of "
        "the difference distribution."
    ),
}

#: Corpus doctrine: exactly 100 fresh cases unless the run is declared
#: invalid/inconclusive BEFORE evaluation (the runner refuses other N).
CORPUS_SIZE = 100


def _candidate_digests(freeze: dict[str, Any]) -> dict[str, str]:
    return {
        str(d["policy_version"]): digest(d)
        for d in freeze["candidate_declarations"]
    }


def doctrine_record(*, code_sha: str, frozen_at: datetime | None = None) -> dict[str, Any]:
    """Build the content-free certification doctrine artifact."""
    freeze = load_freeze()
    digests = _candidate_digests(freeze)
    missing = [v for v in (CERTIFICATION_CANDIDATE, *BASELINE_CONTROLS[1:]) if v not in digests]
    if missing:
        raise ValueError(f"candidate_missing_from_162c_freeze:{missing[0]}")
    when = (frozen_at or datetime.now(tz=UTC)).isoformat()
    record = {
        "doctrine_schema_version": DOCTRINE_SCHEMA_VERSION,
        "issue": "162D",
        "parent_issues": {"epic": 153, "parent": 162, "depends_on": "162C (PR #175, de55483)"},
        "frozen_at": when,
        "code_sha": code_sha,
        "candidate_under_certification": {
            "policy_version": CERTIFICATION_CANDIDATE,
            "declaration_digest": digests[CERTIFICATION_CANDIDATE],
            "source_freeze_digest": freeze["freeze_digest"],
        },
        "baseline_controls": [
            {
                "policy_version": version,
                "declaration_digest": digests.get(version),
                "role": "compatibility/baseline control",
            }
            for version in BASELINE_CONTROLS
        ],
        "context_only_candidates": list(CONTEXT_ONLY),
        "numerical_gates": dict(GATE_VALUES),
        "terminal_statuses": list(TERMINAL_STATUSES),
        "automatic_admission_status": AUTOMATIC_ADMISSION_STATUS,
        "uncertainty_method": dict(UNCERTAINTY_METHOD),
        "corpus": {
            "required_n": CORPUS_SIZE,
            "overlap_policy": {
                "162b_development_corpus": "excluded before selection (50 cases)",
                "162c_holdout_corpus": "excluded before selection (30 cases)",
                "proof": "derived set intersection is empty, recorded in the manifest",
            },
            "sampling": "rare-strata-first deterministic selection (blind_review.select_tranche)",
            "no_hand_curation": (
                "selection is a pure function of the snapshot, seed, and "
                "exclusion sets; candidate/current outputs are not inputs"
            ),
            "blindness": (
                "candidate/current outputs must not exist for certification "
                "cases before the final human corpus is frozen (reveal gate)"
            ),
            "privacy": (
                "raw content host-local outside Git; public artifacts carry "
                "digests, aggregates, and sanitized categories only"
            ),
        },
        "human_labeling": {
            "rubric": "clarified #162C rubric (ticket Human labeling section)",
            "reviewer_a": "one full independent blind reviewer over all 100 cases",
            "reviewer_b": (
                "a second independent blind reviewer for every "
                "high-consequence case and every substantive first-reviewer "
                "disagreement"
            ),
            "records": (
                "raw reviewer records preserved separately from adjudicated "
                "labels; no mechanical majority vote for substantive disagreement"
            ),
            "freeze_order": "adjudication freezes before policy reveal",
            "inter_rater": "agreement and disagreement reported by field",
        },
        "unknown_signal_doctrine": {
            "unavailable_stays_unavailable": True,
            "unknown_stays_unknown": True,
            "oracle_fields_in_candidate_inputs": "structurally impossible (typing)",
            "fabricated_fallback": "any fallback fabricating evidence/source prior/"
            "taxonomy certainty invalidates the run",
        },
        "cost_schedule": "admission-cost-weights-v1 (#162C frozen schedule, verbatim)",
        "authorization_scope": (
            "A CERTIFIED_STORAGE_POLICY pass authorizes ONLY accepting P3 as "
            "the evidence-backed design for the storage/kind-decoupling "
            "portion of future #158 implementation. It does not enable P3 in "
            "production, enable automatic admission, remove the 72h gate, "
            "change startup recall, authorize #160/#161, close #162, or "
            "constitute rollout/canary acceptance."
        ),
        "fail_closed_events": [
            "doctrine/gate drift after freeze",
            "corpus overlap with spent corpora",
            "candidate output revealed before label freeze",
            "N != 100 without a pre-declared invalid/inconclusive run",
            "P0/current parity failure",
            "label leakage into candidate decisions",
            "unfaithful gate computation",
        ],
    }
    return record


def doctrine_digest(record: dict[str, Any]) -> str:
    unsigned = {k: v for k, v in record.items() if k != "doctrine_digest"}
    return digest(unsigned)


def write_doctrine(
    path: Path = DOCTRINE_PATH, *, code_sha: str, frozen_at: datetime | None = None
) -> str:
    record = doctrine_record(code_sha=code_sha, frozen_at=frozen_at)
    value = doctrine_digest(record)
    record["doctrine_digest"] = value
    path.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n")
    return value


def load_doctrine(path: Path = DOCTRINE_PATH) -> dict[str, Any]:
    """Load and fully validate the committed doctrine artifact.

    Fails closed on: missing artifact, digest mismatch, schema mismatch,
    gate-value drift versus this module's frozen literals, candidate/freeze
    drift versus the committed #162C candidate freeze, or a terminal-status
    vocabulary change.
    """
    if not path.exists():
        raise ValueError("doctrine_artifact_missing")
    record: dict[str, Any] = json.loads(path.read_text())
    claimed = record.get("doctrine_digest")
    if claimed != doctrine_digest(record):
        raise ValueError("doctrine_digest_mismatch")
    if record.get("doctrine_schema_version") != DOCTRINE_SCHEMA_VERSION:
        raise ValueError("invalid_doctrine_schema")
    if record.get("numerical_gates") != GATE_VALUES:
        raise ValueError("doctrine_gate_drift")
    if tuple(record.get("terminal_statuses", ())) != TERMINAL_STATUSES:
        raise ValueError("doctrine_terminal_status_drift")
    candidate = record.get("candidate_under_certification", {})
    if candidate.get("policy_version") != CERTIFICATION_CANDIDATE:
        raise ValueError("doctrine_candidate_drift")
    freeze = load_freeze()
    if record.get("candidate_under_certification", {}).get("source_freeze_digest") != freeze.get(
        "freeze_digest"
    ):
        raise ValueError("doctrine_freeze_generation_mismatch")
    digests = _candidate_digests(freeze)
    pinned = record["candidate_under_certification"]["declaration_digest"]
    if pinned != digests[CERTIFICATION_CANDIDATE]:
        raise ValueError("doctrine_candidate_declaration_drift")
    return record
