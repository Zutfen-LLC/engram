"""Read-only promotion readiness diagnostics (ENG-PROMOTION-003A / issue #154).

Distinguishes, per live proposed item and without any provider call or
mutation:

* temporarily cooling (only the age gate remains);
* statically eligible but awaiting reconciliation or a delayed job;
* permanently blocked by kind/review policy under the current configuration;
* below the evidence threshold (with the exact required retention confidence);
* missing or malformed/stale classification evidence;
* explicitly review-required.

Everything here derives from the SAME pure policy module and evaluator used by
the mutation paths (:func:`engram.promotion.assess_promotion_candidate`,
:func:`engram.promotion_policy.choose_basis`,
:func:`engram.promotion_policy.required_retention_confidence_v1`) and from the
canonical closed blocker vocabulary (:data:`engram.promotion.PROMOTION_BLOCKER_CODES`).
This module implements NO promotion policy of its own — it only presents what
the shared evaluator already decided.

Terminology (canonical, see docs/design.md §3):

* ``source_confidence_prior`` — immutable source-policy prior written at
  capture time; production classification never blends it afterwards.
* ``taxonomy_confidence`` — classifier confidence in kind/placement.
* ``retention_confidence`` — classifier estimate that the candidate is
  durable/useful enough to keep. It is NOT epistemic/factual confidence: the
  retention receipt does not establish that the proposition is correct.
* ``promotion assessment`` — the deterministic policy decision over the
  available evidence computed by the shared evaluator.
* Path B (usage-validated quorum) — deferred and unimplemented; recalls and
  useful feedback do not accumulate promotion evidence.

These diagnostics never run the promotion-time semantic conflict recheck;
every response says so explicitly via ``conflict_recheck_status="not_run"``.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import ColumnElement, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from engram.models import ItemEvent, Job, MemoryItem
from engram.promotion import (
    BLOCK_AGE,
    BLOCK_CONFIDENCE,
    BLOCK_CONFLICT,
    BLOCK_DISPOSITION,
    BLOCK_DISPUTE,
    BLOCK_EVIDENCE_DISABLED,
    BLOCK_INCONSISTENT,
    BLOCK_KIND_POLICY,
    BLOCK_NO_EVIDENCE,
    BLOCK_REVIEW_POLICY,
    BLOCK_SCORE,
    BLOCK_SOURCE_PRIOR,
    BLOCK_TAXONOMY,
    BLOCK_VERSION,
    PROMOTION_BLOCKER_CODES,
    PromotionCandidate,
    PromotionSupport,
    _config,
    _config_values,
    _evidence_state,
    assess_promotion_candidate,
    load_promotion_support,
)
from engram.promotion_policy import (
    EVIDENCE_PROMOTION_POLICY_VERSION,
    LEGACY_PROMOTION_POLICY_VERSION,
    PromotionPolicyError,
    choose_basis,
    required_retention_confidence_v1,
)

__all__ = [
    "EVIDENCE_STATE_BOUND_BELOW_THRESHOLD",
    "EVIDENCE_STATE_BOUND_QUALIFIED",
    "EVIDENCE_STATE_MALFORMED_STALE",
    "EVIDENCE_STATE_NONE",
    "JOB_STATE_DEAD",
    "JOB_STATE_MISSING",
    "JOB_STATE_OVERDUE",
    "JOB_STATE_SCHEDULED",
    "JobObservation",
    "LAST_EVALUATION_UNKNOWN",
    "PromotionReadiness",
    "ReadinessClassification",
    "active_jobs_for_items",
    "build_promotion_readiness",
    "classify_readiness",
    "evidence_state_of",
    "promotion_readiness_aggregate",
    "readiness_state_from_blockers",
]

# --- Evidence states (doctor + per-item vocabulary) ---------------------------

EVIDENCE_STATE_NONE = "none"
EVIDENCE_STATE_BOUND_QUALIFIED = "bound-qualified"
EVIDENCE_STATE_BOUND_BELOW_THRESHOLD = "bound-below-threshold"
EVIDENCE_STATE_MALFORMED_STALE = "malformed/stale"

# --- Job states ---------------------------------------------------------------

JOB_STATE_SCHEDULED = "scheduled"
JOB_STATE_OVERDUE = "overdue"
JOB_STATE_DEAD = "dead"
JOB_STATE_MISSING = "missing"

# --- Derived per-item readiness states ----------------------------------------

READINESS_NOT_A_CANDIDATE = "not_a_promotion_candidate"
READINESS_KIND_POLICY = "blocked_by_kind_policy"
READINESS_REVIEW_POLICY = "blocked_by_review_policy"
READINESS_CONFLICT_OR_DISPUTE = "blocked_by_conflict_or_dispute"
READINESS_EVIDENCE_DISABLED = "evidence_lane_disabled"
READINESS_MISSING_EVIDENCE = "missing_evidence"
READINESS_MALFORMED_EVIDENCE = "malformed_or_stale_evidence"
READINESS_BELOW_THRESHOLD = "below_evidence_threshold"
READINESS_BELOW_TAXONOMY = "below_taxonomy_confidence_minimum"
READINESS_DISPOSITION = "retention_disposition_not_retain"
READINESS_BELOW_LEGACY = "below_legacy_confidence_threshold"
READINESS_COOLING = "cooling"
READINESS_ELIGIBLE_NOW = "eligible_now"

# Documented precedence for summarizing a blocker set as one state. Policy
# blockers (which need a config or review change) outrank evidence-presence
# blockers, which outrank score blockers, which outrank the age clock. The
# evidence-lane-disabled summary sits below the evidence-quality states so an
# item with no bound receipt is reported as missing evidence, never as merely
# "lane disabled" (issue #154: a receipt-less backfill is missing evidence,
# not organically accumulating evidence).
_READINESS_PRECEDENCE: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({BLOCK_KIND_POLICY}), READINESS_KIND_POLICY),
    (frozenset({BLOCK_REVIEW_POLICY}), READINESS_REVIEW_POLICY),
    (frozenset({BLOCK_CONFLICT, BLOCK_DISPUTE}), READINESS_CONFLICT_OR_DISPUTE),
    (frozenset({BLOCK_NO_EVIDENCE, BLOCK_SOURCE_PRIOR}), READINESS_MISSING_EVIDENCE),
    (frozenset({BLOCK_VERSION, BLOCK_INCONSISTENT}), READINESS_MALFORMED_EVIDENCE),
    (frozenset({BLOCK_SCORE}), READINESS_BELOW_THRESHOLD),
    (frozenset({BLOCK_TAXONOMY}), READINESS_BELOW_TAXONOMY),
    (frozenset({BLOCK_DISPOSITION}), READINESS_DISPOSITION),
    (frozenset({BLOCK_EVIDENCE_DISABLED}), READINESS_EVIDENCE_DISABLED),
    (frozenset({BLOCK_CONFIDENCE}), READINESS_BELOW_LEGACY),
    (frozenset({BLOCK_AGE}), READINESS_COOLING),
)

# Blockers no amount of time clears: each requires a configuration change, a
# review action, or conflict resolution.
_POLICY_BLOCKERS: frozenset[str] = frozenset(
    {BLOCK_KIND_POLICY, BLOCK_REVIEW_POLICY, BLOCK_CONFLICT, BLOCK_DISPUTE}
)

LAST_EVALUATION_UNKNOWN = "unknown"

# Job types whose presence/absence explains a proposed item's reconciliation
# state: the delayed targeted promotion job and the async classification job
# that produces retention evidence.
_DIAGNOSTIC_JOB_TYPES: tuple[str, ...] = ("promotion.path_a", "classification.refine")

# Hard bound on rows examined per job lookup so a pathological job history can
# never make a "bounded" diagnostic unbounded. Deterministic order keeps the
# bound repeatable.
_MAX_JOB_ROWS = 200


def readiness_state_from_blockers(blockers: list[str] | tuple[str, ...]) -> str:
    """Summarize canonical blocker codes into one readiness state."""
    present = set(blockers)
    for codes, state in _READINESS_PRECEDENCE:
        if present & codes:
            return state
    return READINESS_ELIGIBLE_NOW


@dataclass(frozen=True)
class ReadinessClassification:
    """Lane-aware readiness summary derived from shared-evaluator outputs."""

    readiness_state: str
    terminal_under_current_policy: bool
    can_auto_promote_without_new_evidence_or_review: bool


def classify_readiness(
    *,
    is_candidate: bool,
    blockers: list[str] | tuple[str, ...],
    legacy_trust_qualified: bool,
    evidence_trust_qualified: bool,
    selected_basis: str | None,
) -> ReadinessClassification:
    """Derive the presentation state from evaluator outputs, lane-aware.

    A lane-trust-qualified item whose age gate is still running is *cooling*
    and time-dependent even when the shared evaluator also lists blockers for
    the other, non-selected lane (e.g. missing evidence on a legacy-qualified
    item): the mutation path selects either lane independently, so those
    blockers never block promotion. Terminal means only new evidence, a
    configuration change, or review can unblock the item.
    """
    if not is_candidate:
        return ReadinessClassification(READINESS_NOT_A_CANDIDATE, True, False)
    policy_blocked = bool(_POLICY_BLOCKERS & set(blockers))
    if policy_blocked:
        return ReadinessClassification(
            readiness_state_from_blockers(blockers), True, False
        )
    if selected_basis is not None:
        # Trust and age both passed on a lane; only the promotion-time
        # conflict recheck — which this preview never runs — remains.
        return ReadinessClassification(
            readiness_state_from_blockers(blockers), False, True
        )
    if legacy_trust_qualified or evidence_trust_qualified:
        return ReadinessClassification(READINESS_COOLING, False, True)
    return ReadinessClassification(readiness_state_from_blockers(blockers), True, False)


def evidence_state_of(
    item: MemoryItem, run: Any | None, *, evidence_threshold: float
) -> str:
    """Classify the item's retention-evidence shape using the shared helper.

    ``none`` — no bound receipt or the item carries no stored retention
    fields/prior (nothing has assessed retention yet); ``malformed/stale`` — a
    receipt exists but fails the shared consistency/version checks;
    ``bound-below-threshold`` — a structurally valid receipt whose evidence
    does not qualify (score below threshold, disposition other than retain, or
    taxonomy below the minimum); ``bound-qualified`` — the shared evaluator
    computes a qualifying score from consistent bound evidence.
    """
    if (
        run is None
        or item.retention_confidence is None
        or item.retention_disposition is None
        or item.retention_evidence_at is None
        or item.source_confidence_prior is None
    ):
        return EVIDENCE_STATE_NONE
    blockers, score, _ = _evidence_state(item, run)
    if blockers and set(blockers) & {BLOCK_VERSION, BLOCK_INCONSISTENT, BLOCK_SOURCE_PRIOR}:
        return EVIDENCE_STATE_MALFORMED_STALE
    if blockers:
        # Structurally valid receipt that evaluated negatively (score,
        # disposition, or taxonomy gate).
        return EVIDENCE_STATE_BOUND_BELOW_THRESHOLD
    assert score is not None
    return (
        EVIDENCE_STATE_BOUND_QUALIFIED
        if score >= evidence_threshold
        else EVIDENCE_STATE_BOUND_BELOW_THRESHOLD
    )


# --- Job observation -----------------------------------------------------------


@dataclass(frozen=True)
class JobObservation:
    """One bounded, content-free fact about a diagnostic-relevant job."""

    job_id: uuid.UUID
    job_type: str
    status: str
    state: str
    run_after: datetime
    attempts: int
    max_attempts: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": str(self.job_id),
            "job_type": self.job_type,
            "status": self.status,
            "state": self.state,
            "run_after": self.run_after.isoformat(),
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
        }


def _job_row_state(job: Job, *, now: datetime) -> str:
    if job.status == "dead":
        return JOB_STATE_DEAD
    if job.run_after is not None and job.run_after <= now:
        return JOB_STATE_OVERDUE
    return JOB_STATE_SCHEDULED


async def active_jobs_for_items(
    session: AsyncSession,
    *,
    tenant_id: str | uuid.UUID,
    item_ids: list[uuid.UUID],
    now: datetime,
) -> dict[uuid.UUID, list[JobObservation]]:
    """Bounded, deterministic, tenant-scoped lookup of diagnostic jobs.

    Returns pending/running/dead ``promotion.path_a`` / ``classification.refine``
    jobs for the given items, ordered by ``(created_at, id)`` and capped at
    ``_MAX_JOB_ROWS`` rows total. Job payloads (which reference item ids only)
    are never returned.
    """
    if not item_ids:
        return {}
    clauses: list[ColumnElement[bool]] = [
        Job.tenant_id == str(tenant_id),
        Job.job_type.in_(_DIAGNOSTIC_JOB_TYPES),
        Job.status.in_(["pending", "running", "dead"]),
    ]
    item_param_names = [f"diag_item_{i}" for i in range(len(item_ids))]
    clauses.append(
        cast(
            "ColumnElement[bool]",
            text(
                "payload->>'memory_item_id' IN ("
                + ", ".join(f":{name}" for name in item_param_names)
                + ")"
            ),
        )
    )
    params = {name: str(item_id) for name, item_id in zip(item_param_names, item_ids, strict=True)}
    rows = (
        (
            await session.execute(
                select(Job)
                .where(*clauses)
                .order_by(Job.created_at.asc(), Job.id.asc())
                .limit(_MAX_JOB_ROWS)
                .params(**params)
            )
        )
        .scalars()
        .all()
    )
    result: dict[uuid.UUID, list[JobObservation]] = {item_id: [] for item_id in item_ids}
    for job in rows:
        payload_item = job.payload.get("memory_item_id") if job.payload else None
        if payload_item is None:
            continue
        try:
            item_id = uuid.UUID(str(payload_item))
        except ValueError:
            continue
        if item_id not in result:
            continue
        result[item_id].append(
            JobObservation(
                job_id=job.id,
                job_type=job.job_type,
                status=job.status,
                state=_job_row_state(job, now=now),
                run_after=job.run_after,
                attempts=int(job.attempts or 0),
                max_attempts=int(job.max_attempts or 0),
            )
        )
    return result


def item_job_state(jobs: list[JobObservation]) -> str:
    """Collapse one item's job observations to a single state.

    Precedence: dead > overdue > scheduled; no observed job → missing.
    """
    states = {job.state for job in jobs}
    if JOB_STATE_DEAD in states:
        return JOB_STATE_DEAD
    if JOB_STATE_OVERDUE in states:
        return JOB_STATE_OVERDUE
    if JOB_STATE_SCHEDULED in states:
        return JOB_STATE_SCHEDULED
    return JOB_STATE_MISSING


# --- Last evaluation -----------------------------------------------------------


async def last_evaluation_for_item(
    session: AsyncSession, item_id: uuid.UUID
) -> dict[str, Any]:
    """Best-available last evaluation trigger evidence, or explicit unknown.

    Per-assessment persistence is not implemented yet (deferred to
    ENG-PROMOTION-003D). Until then the honest available evidence is the most
    recent item event that triggered or performed a promotion-relevant
    evaluation: a ``classification`` event (worker evidence binding records
    its promotion-schedule diagnostics) or an ``auto-promotion`` audit event.
    Missing data stays explicit — it is never coerced to a time or version.
    """
    rows = (
        (
            await session.execute(
                select(ItemEvent)
                .where(
                    ItemEvent.item_id == item_id,
                    ItemEvent.event_type.in_(["classification", "review_change",
                                             "conflict_resolution"]),
                )
                .order_by(ItemEvent.created_at.desc(), ItemEvent.id.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    for event in rows:
        if event.event_type == "classification":
            return {
                "trigger": "classification.refine",
                "at": event.created_at.isoformat() if event.created_at else None,
                "policy_version": _policy_version_from_reason(event.reason),
                "basis": "evidence_binding",
                "detail": "worker classification event (promotion schedule diagnostics recorded)",
            }
        reason = event.reason or ""
        if "auto-promotion" in reason:
            return {
                "trigger": "auto_promotion",
                "at": event.created_at.isoformat() if event.created_at else None,
                "policy_version": _policy_version_from_reason(reason),
                "basis": _basis_from_reason(reason),
                "detail": "last auto-promotion audit event",
            }
    return {
        "trigger": LAST_EVALUATION_UNKNOWN,
        "at": None,
        "policy_version": None,
        "basis": None,
        "detail": "no persisted promotion assessment exists for this item yet",
    }


def _policy_version_from_reason(reason: str | None) -> str | None:
    if not reason:
        return None
    try:
        parsed = json.loads(reason)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    version = parsed.get("promotion_policy_version")
    return str(version) if version is not None else None


def _basis_from_reason(reason: str | None) -> str | None:
    if not reason:
        return None
    try:
        parsed = json.loads(reason)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    basis = parsed.get("basis") or parsed.get("selected_basis")
    return str(basis) if basis is not None else None


# --- Per-item readiness ---------------------------------------------------------


@dataclass(frozen=True)
class PromotionReadiness:
    """Everything an authorized reviewer can inspect about one item's readiness.

    Computed exclusively from the shared evaluator and canonical vocabulary;
    the expensive promotion-time conflict recheck is never run and the payload
    says so (``conflict_recheck_status``).
    """

    item_id: uuid.UUID
    source_type: str
    kind: str
    review_status: str
    created_at: datetime
    age_seconds: float
    is_promotion_candidate: bool

    memory_confidence: float
    source_confidence_prior: float | None
    legacy_threshold: float
    legacy_threshold_met: bool

    evidence_enabled: bool
    evidence_threshold: float
    evidence_score: float | None
    evidence_state: str
    required_retention_confidence: float | None
    required_retention_status: str

    classification_run_id: uuid.UUID | None
    classification_version: str | None
    retention_policy_version: str | None
    classification_model: str | None
    classification_provider: str | None
    taxonomy_confidence: float | None
    retention_confidence: float | None
    retention_disposition: str | None
    retention_evidence_at: datetime | None

    selected_basis: str | None
    promotion_policy_version: str | None
    blockers: list[str]
    readiness_state: str
    terminal_under_current_policy: bool
    can_auto_promote_without_new_evidence_or_review: bool

    legacy_trust_qualified: bool
    legacy_age_qualified: bool
    legacy_eligible_at: datetime
    evidence_trust_qualified: bool
    evidence_age_qualified: bool
    evidence_eligible_at: datetime | None
    cooling_period_start: datetime | None
    selected_eligible_at: datetime | None
    remaining_cooling_seconds: float | None

    promotion_job_state: str | None
    jobs: list[JobObservation]

    conflict_recheck_status: str
    last_evaluation: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": str(self.item_id),
            "source_type": self.source_type,
            "kind": self.kind,
            "review_status": self.review_status,
            "created_at": self.created_at.isoformat(),
            "age_seconds": self.age_seconds,
            "is_promotion_candidate": self.is_promotion_candidate,
            "memory_confidence": self.memory_confidence,
            "source_confidence_prior": self.source_confidence_prior,
            "legacy_threshold": self.legacy_threshold,
            "legacy_threshold_met": self.legacy_threshold_met,
            "evidence_enabled": self.evidence_enabled,
            "evidence_threshold": self.evidence_threshold,
            "evidence_score": self.evidence_score,
            "evidence_state": self.evidence_state,
            "required_retention_confidence": self.required_retention_confidence,
            "required_retention_status": self.required_retention_status,
            "classification_run_id": (
                str(self.classification_run_id) if self.classification_run_id else None
            ),
            "classification_version": self.classification_version,
            "retention_policy_version": self.retention_policy_version,
            "classification_model": self.classification_model,
            "classification_provider": self.classification_provider,
            "taxonomy_confidence": self.taxonomy_confidence,
            "retention_confidence": self.retention_confidence,
            "retention_disposition": self.retention_disposition,
            "retention_evidence_at": (
                self.retention_evidence_at.isoformat()
                if self.retention_evidence_at
                else None
            ),
            "selected_basis": self.selected_basis,
            "promotion_policy_version": self.promotion_policy_version,
            "blockers": list(self.blockers),
            "readiness_state": self.readiness_state,
            "terminal_under_current_policy": self.terminal_under_current_policy,
            "can_auto_promote_without_new_evidence_or_review": (
                self.can_auto_promote_without_new_evidence_or_review
            ),
            "legacy_trust_qualified": self.legacy_trust_qualified,
            "legacy_age_qualified": self.legacy_age_qualified,
            "legacy_eligible_at": self.legacy_eligible_at.isoformat(),
            "evidence_trust_qualified": self.evidence_trust_qualified,
            "evidence_age_qualified": self.evidence_age_qualified,
            "evidence_eligible_at": (
                self.evidence_eligible_at.isoformat() if self.evidence_eligible_at else None
            ),
            "cooling_period_start": (
                self.cooling_period_start.isoformat() if self.cooling_period_start else None
            ),
            "selected_eligible_at": (
                self.selected_eligible_at.isoformat() if self.selected_eligible_at else None
            ),
            "remaining_cooling_seconds": self.remaining_cooling_seconds,
            "promotion_job_state": self.promotion_job_state,
            "jobs": [job.as_dict() for job in self.jobs],
            "conflict_recheck_status": self.conflict_recheck_status,
            "last_evaluation": dict(self.last_evaluation),
        }


def _policy_version_for_basis(basis: str | None) -> str | None:
    if basis == "retention_evidence":
        return EVIDENCE_PROMOTION_POLICY_VERSION
    if basis == "legacy_confidence":
        return LEGACY_PROMOTION_POLICY_VERSION
    return None


def _required_retention(
    prior: float | None, evidence_threshold: float
) -> tuple[float | None, str]:
    # Computed under the active formula whenever the prior exists, regardless
    # of the tenant's lane toggle — the formula itself is versioned policy.
    if prior is None:
        # Missing prior stays explicit — never coerced to zero.
        return None, "unknown_no_source_prior"
    try:
        required = required_retention_confidence_v1(prior, evidence_threshold)
    except PromotionPolicyError:
        return None, "unknown_invalid_inputs"
    if required is None:
        return None, "unreachable"
    return required, "computable"


def _evidence_trust(
    item: MemoryItem,
    run: Any | None,
    *,
    evidence_enabled: bool,
    evidence_threshold: float,
) -> bool:
    """Lane trust exactly as the shared evaluator computes it."""
    evidence_blockers, score, _ = _evidence_state(item, run)
    if not evidence_enabled:
        evidence_blockers.append(BLOCK_EVIDENCE_DISABLED)
    return not evidence_blockers and score is not None and score >= evidence_threshold


async def build_promotion_readiness(
    session: AsyncSession,
    item: MemoryItem,
    *,
    now: datetime,
) -> PromotionReadiness:
    """Assess one item's promotion readiness through the shared evaluator."""
    config = await _config(session, str(item.tenant_id))
    (
        _enabled,
        confidence_threshold,
        min_age_hours,
        evidence_enabled,
        evidence_threshold,
    ) = _config_values(config)
    support_map = await load_promotion_support(session, [item])
    support: PromotionSupport = support_map[item.id]
    candidate: PromotionCandidate = assess_promotion_candidate(
        item,
        support,
        confidence_threshold=confidence_threshold,
        min_age_hours=min_age_hours,
        evidence_enabled=evidence_enabled,
        evidence_threshold=evidence_threshold,
        now=now,
    )
    # Lane-level detail (which lane would have qualified absent the age gate)
    # comes from the same pure policy the evaluator itself calls.
    _, _, cooling_start = _evidence_state(item, support.classification_run)
    evidence_trust = _evidence_trust(
        item,
        support.classification_run,
        evidence_enabled=evidence_enabled,
        evidence_threshold=evidence_threshold,
    )
    legacy_trust = item.memory_confidence >= confidence_threshold
    lanes = choose_basis(
        legacy_trust_qualified=legacy_trust,
        legacy_age_qualified=item.created_at is not None
        and candidate.legacy_eligible_at <= now,
        evidence_trust_qualified=evidence_trust,
        evidence_age_qualified=cooling_start is not None
        and candidate.evidence_eligible_at is not None
        and candidate.evidence_eligible_at <= now,
    )
    jobs = (
        await active_jobs_for_items(
            session, tenant_id=item.tenant_id, item_ids=[item.id], now=now
        )
    ).get(item.id, [])
    promotion_jobs = [job for job in jobs if job.job_type == "promotion.path_a"]
    is_candidate = (
        item.review_status == "proposed"
        and item.valid_to is None
        and item.superseded_by is None
    )
    blockers = list(candidate.blockers)
    classification = classify_readiness(
        is_candidate=is_candidate,
        blockers=blockers,
        legacy_trust_qualified=legacy_trust,
        evidence_trust_qualified=evidence_trust,
        selected_basis=candidate.selected_basis,
    )
    required_value, required_status = _required_retention(
        item.source_confidence_prior,
        evidence_threshold,
    )
    remaining: float | None = None
    # Prefer the selected lane's clock; for a cooling item no lane is selected
    # yet, so fall back to the first trust-qualified lane's eligibility time.
    effective_eligible_at = candidate.eligible_at
    if effective_eligible_at is None and evidence_trust:
        effective_eligible_at = candidate.evidence_eligible_at
    if effective_eligible_at is None and legacy_trust:
        effective_eligible_at = candidate.legacy_eligible_at
    if effective_eligible_at is not None:
        delta = (effective_eligible_at - now).total_seconds()
        remaining = delta if delta > 0 else 0.0
    run = support.classification_run
    provenance = run.provenance if run is not None else None
    provenance = provenance if isinstance(provenance, dict) else {}
    return PromotionReadiness(
        item_id=item.id,
        source_type=item.source_type,
        kind=item.kind,
        review_status=item.review_status,
        created_at=item.created_at,
        age_seconds=(now - item.created_at).total_seconds(),
        is_promotion_candidate=is_candidate,
        memory_confidence=item.memory_confidence,
        source_confidence_prior=item.source_confidence_prior,
        legacy_threshold=candidate.legacy_threshold,
        legacy_threshold_met=item.memory_confidence >= confidence_threshold,
        evidence_enabled=evidence_enabled,
        evidence_threshold=candidate.evidence_threshold,
        evidence_score=candidate.evidence_score,
        evidence_state=evidence_state_of(
            item, run, evidence_threshold=candidate.evidence_threshold
        ),
        required_retention_confidence=required_value,
        required_retention_status=required_status,
        classification_run_id=run.id if run is not None else None,
        classification_version=run.classification_version if run is not None else None,
        retention_policy_version=run.retention_policy_version if run is not None else None,
        classification_model=_optional_str(provenance.get("model")),
        classification_provider=_optional_str(provenance.get("provider")),
        taxonomy_confidence=candidate.taxonomy_confidence,
        retention_confidence=item.retention_confidence,
        retention_disposition=item.retention_disposition,
        retention_evidence_at=item.retention_evidence_at,
        selected_basis=candidate.selected_basis,
        promotion_policy_version=_policy_version_for_basis(candidate.selected_basis),
        blockers=blockers,
        readiness_state=classification.readiness_state,
        terminal_under_current_policy=classification.terminal_under_current_policy,
        can_auto_promote_without_new_evidence_or_review=(
            classification.can_auto_promote_without_new_evidence_or_review
        ),
        legacy_trust_qualified=lanes.legacy.trust_qualified,
        legacy_age_qualified=lanes.legacy.age_qualified,
        legacy_eligible_at=candidate.legacy_eligible_at,
        evidence_trust_qualified=lanes.evidence.trust_qualified,
        evidence_age_qualified=lanes.evidence.age_qualified,
        evidence_eligible_at=candidate.evidence_eligible_at,
        cooling_period_start=candidate.cooling_period_start,
        selected_eligible_at=candidate.eligible_at,
        remaining_cooling_seconds=remaining,
        promotion_job_state=item_job_state(promotion_jobs) if promotion_jobs else None,
        jobs=jobs,
        conflict_recheck_status="not_run",
        last_evaluation=await last_evaluation_for_item(session, item.id),
    )


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


# --- Aggregate (doctor) ----------------------------------------------------------


async def promotion_readiness_aggregate(
    session: AsyncSession,
    *,
    tenant_id: str | uuid.UUID,
    now: datetime,
    window_limit: int,
) -> dict[str, Any]:
    """Bounded, content-free promotion-readiness aggregate for one tenant.

    ``window_limit`` mirrors the bounded startup-recall promotion scan
    (``settings.startup_promotion_limit``): the first ``window_limit`` live
    proposed items by ``created_at ASC`` — exactly the rows the lazy sweep
    would examine — are assessed through the shared evaluator. The exact
    by-source-type / by-kind / age-bucket counts cover the full live proposed
    set because they are cheap categorical GROUP BYs. No content, prompt,
    provider credential, principal id, or cross-tenant row ever enters the
    result.
    """
    tenant = str(tenant_id)
    limit = max(1, min(int(window_limit), 500))
    base_where = (
        "tenant_id = :tenant_id AND review_status = 'proposed' AND valid_to IS NULL"
    )
    base_params: dict[str, Any] = {"tenant_id": tenant}

    source_rows = (
        (
            await session.execute(
                text(
                    "SELECT source_type, count(*) AS n FROM memory_items "
                    f"WHERE {base_where} GROUP BY source_type"
                ),
                base_params,
            )
        )
        .mappings()
        .all()
    )
    by_source_type: Counter[str] = Counter()
    for row in source_rows:
        by_source_type[str(row["source_type"])] += int(row["n"])

    kind_rows = (
        (
            await session.execute(
                text(
                    "SELECT kind, count(*) AS n FROM memory_items "
                    f"WHERE {base_where} GROUP BY kind"
                ),
                base_params,
            )
        )
        .mappings()
        .all()
    )
    by_kind: Counter[str] = Counter()
    for row in kind_rows:
        by_kind[str(row["kind"])] += int(row["n"])

    bucket_rows = (
        (
            await session.execute(
                text(
                    "SELECT CASE "
                    "WHEN :now - created_at < interval '24 hours' THEN 'lt_24h' "
                    "WHEN :now - created_at < interval '72 hours' THEN '24h_to_72h' "
                    "WHEN :now - created_at < interval '7 days' THEN '72h_to_7d' "
                    "WHEN :now - created_at < interval '30 days' THEN '7d_to_30d' "
                    "ELSE 'gt_30d' END AS bucket, count(*) AS n "
                    f"FROM memory_items WHERE {base_where} GROUP BY bucket"
                ),
                {**base_params, "now": now},
            )
        )
        .mappings()
        .all()
    )
    age_buckets: Counter[str] = Counter()
    for row in bucket_rows:
        age_buckets[str(row["bucket"])] += int(row["n"])
    total = sum(by_source_type.values())

    items = list(
        (
            await session.execute(
                select(MemoryItem)
                .where(
                    MemoryItem.tenant_id == tenant,
                    MemoryItem.review_status == "proposed",
                    MemoryItem.valid_to.is_(None),
                )
                .order_by(MemoryItem.created_at.asc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    config = await _config(session, tenant)
    (
        enabled,
        confidence_threshold,
        min_age_hours,
        evidence_enabled,
        evidence_threshold,
    ) = _config_values(config)
    support_map = await load_promotion_support(session, items)
    jobs_map = await active_jobs_for_items(
        session, tenant_id=tenant, item_ids=[item.id for item in items], now=now
    )

    blocker_counts: Counter[str] = Counter()
    evidence_states: Counter[str] = Counter()
    job_states: Counter[str] = Counter()
    terminal_count = 0
    time_dependent_count = 0
    for item in items:
        if item.superseded_by is not None:
            # Same skip the mutation sweep applies.
            continue
        candidate = assess_promotion_candidate(
            item,
            support_map[item.id],
            confidence_threshold=confidence_threshold,
            min_age_hours=min_age_hours,
            evidence_enabled=evidence_enabled,
            evidence_threshold=evidence_threshold,
            now=now,
        )
        blockers = candidate.blockers
        for blocker in blockers:
            assert blocker in PROMOTION_BLOCKER_CODES
            blocker_counts[blocker] += 1
        evidence_states[
            evidence_state_of(
                item, support_map[item.id].classification_run, evidence_threshold=evidence_threshold
            )
        ] += 1
        job_states[item_job_state(jobs_map.get(item.id, []))] += 1
        classification = classify_readiness(
            is_candidate=True,
            blockers=blockers,
            legacy_trust_qualified=item.memory_confidence >= confidence_threshold,
            evidence_trust_qualified=_evidence_trust(
                item,
                support_map[item.id].classification_run,
                evidence_enabled=evidence_enabled,
                evidence_threshold=evidence_threshold,
            ),
            selected_basis=candidate.selected_basis,
        )
        if classification.terminal_under_current_policy:
            terminal_count += 1
        else:
            time_dependent_count += 1

    return {
        "auto_promote_enabled": enabled,
        "evidence_lane_enabled": evidence_enabled,
        "proposed_total": total,
        "by_source_type": dict(sorted(by_source_type.items())),
        "by_kind": dict(sorted(by_kind.items())),
        "age_buckets": dict(sorted(age_buckets.items())),
        "startup_window": {
            "limit": limit,
            "size": len(items),
            "terminal_under_current_policy": terminal_count,
            "time_dependent": time_dependent_count,
            "blocker_counts": [
                {"code": code, "count": count}
                for code, count in sorted(
                    blocker_counts.items(), key=lambda kv: (-kv[1], kv[0])
                )
            ],
            "evidence_states": dict(sorted(evidence_states.items())),
            "job_states": dict(sorted(job_states.items())),
        },
        "starvation_risk": bool(items) and terminal_count * 2 > len(items),
        "evidence_note": (
            "retention_confidence is the classifier's durability/usefulness "
            "estimate, not epistemic/factual confidence; recalls and useful "
            "feedback accumulate no promotion evidence (Path B deferred)"
        ),
    }
