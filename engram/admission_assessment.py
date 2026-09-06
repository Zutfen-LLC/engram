"""Durable, inspectable, versioned admission decisions (issue #159).

Every promotion/admission decision current Path A policy makes becomes an
append-only ``admission_assessments`` row plus a mutable one-row
``admission_assessment_current`` projection, so the system can answer — after
the fact, from durable state rather than a reconstruction — what policy
decided, from which exact inputs, under which policy identity, what blocked or
authorized the result, and what must happen next.

What this module is **not**:

* It is not a new promotion policy. ``assess_promotion_candidate()`` and
  ``auto_promote_proposed_memories()`` remain the sole production authority,
  and every threshold, weight, cooling period and lane rule is unchanged. This
  module observes and records that decision; it never makes one.
* It is not an evidence assessment. The #157 ``memory_assessments`` referenced
  in ``available_memory_assessment_refs`` are recorded as diagnostic
  references only: their epistemic/risk dimensions carry no admission
  authority in v1, and their identity never enters ``input_digest`` or
  ``policy_config_digest``.

Capture is gated by ``ENGRAM_ADMISSION_ASSESSMENT_CAPTURE_ENABLED`` (default
``false``). Disabled, production promotion behavior and audit JSON are
byte-for-byte unchanged. Enabled, the flag additionally buys a stronger audit
invariant: a ``proposed -> active`` mutation fails closed if its assessment,
linked audit event, and projection cannot commit atomically with it.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal

import rfc8785
from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from engram.models import (
    AdmissionAssessment,
    AdmissionAssessmentCurrent,
    ClassificationRun,
    MemoryAssessment,
    MemoryItem,
)
from engram.promotion_policy import (
    EVIDENCE_PROMOTION_POLICY_VERSION,
    EVIDENCE_RETENTION_WEIGHT,
    EVIDENCE_SCORE_CEILING,
    EVIDENCE_SOURCE_PRIOR_WEIGHT,
    EVIDENCE_TAXONOMY_MINIMUM,
    LEGACY_PROMOTION_POLICY_VERSION,
)

SCHEMA_VERSION: Final[Literal["engram.admission-assessment.v1"]] = (
    "engram.admission-assessment.v1"
)

# For this issue there is exactly one production policy profile: the current
# two-lane Path A behavior, named as what it is. The contract version does not
# encode any #158 candidate-policy semantics — #158 introduces its own profile
# rather than redefining this one.
POLICY_PROFILE_KEY: Final[Literal["path_a_compat"]] = "path_a_compat"
POLICY_CONTRACT_VERSION: Final[Literal["path-a-compat-v1"]] = "path-a-compat-v1"

# The classification receipt versions current Path A accepts. Duplicated from
# ``engram.promotion._supported`` deliberately: the digest must pin the exact
# accepted set, and a future widening there must be a deliberate, visible
# policy-digest change here rather than a silent one.
SUPPORTED_CLASSIFICATION_VERSION: Final[str] = "classification-v2"
SUPPORTED_RETENTION_POLICY_VERSION: Final[str] = "retention-v1"

AdmissionMode = Literal["authoritative", "shadow", "legacy_import"]
AdmissionOutcome = Literal[
    "admitted",
    "would_admit",
    "cooling",
    "review_required",
    "blocked",
    "insufficient_evidence",
    "unknown",
    "stale",
    "not_applicable",
]
AdmissionNextAction = Literal[
    "wait_until",
    "classification_required",
    "human_review_required",
    "conflict_resolution_required",
    "new_evidence_required",
    "policy_reconciliation_required",
    "none",
]
AdmissionProjectionStatus = Literal["current", "stale", "missing", "legacy_import"]

ADMISSION_OUTCOMES: Final[frozenset[str]] = frozenset(
    {
        "admitted",
        "would_admit",
        "cooling",
        "review_required",
        "blocked",
        "insufficient_evidence",
        "unknown",
        "stale",
        "not_applicable",
    }
)

# Deterministic ordering for the bounded next-action set. Multiple actions may
# be independently required; they are always emitted in this order so the
# decision hash is stable regardless of the order blockers were discovered.
NEXT_ACTION_ORDER: Final[tuple[AdmissionNextAction, ...]] = (
    "wait_until",
    "conflict_resolution_required",
    "human_review_required",
    "classification_required",
    "new_evidence_required",
    "policy_reconciliation_required",
    "none",
)

# Conflict-recheck vocabulary. ``not_run_preview`` is the exact v1 value for a
# shadow/preview evaluation: it never performs the promotion-time semantic
# recheck and never mutates state, and says so rather than borrowing the
# ordinary ``not_run``. ``unavailable_legacy`` marks a legacy-import row where
# no recheck fact was ever recorded — an honest gap, not a clear result.
CONFLICT_RECHECK_STATUSES: Final[frozenset[str]] = frozenset(
    {"clear", "blocked", "not_run", "not_run_preview", "unavailable_legacy"}
)

# --- Reason codes -----------------------------------------------------------
#
# A bounded, closed vocabulary explaining *why* the outcome is what it is,
# beyond the blocker codes (which say what current policy objected to). Reason
# codes are part of the hashed envelope, so this set is versioned with the
# schema: adding one is an ADR/schema change.
REASON_LANE_LEGACY: Final[str] = "lane_selected_legacy_confidence"
REASON_LANE_EVIDENCE: Final[str] = "lane_selected_retention_evidence"
REASON_MUTATION_COMMITTED: Final[str] = "mutation_committed"
REASON_SHADOW_PREVIEW: Final[str] = "shadow_preview"
REASON_LEGACY_IMPORT: Final[str] = "legacy_import_snapshot"
REASON_LANE_AWAITING_AGE: Final[str] = "lane_qualified_awaiting_age"
REASON_NO_LANE: Final[str] = "no_lane_qualified"
REASON_CONFLICT_UNRESOLVED: Final[str] = "conflict_unresolved"
REASON_CONFLICT_RECHECK_BLOCKED: Final[str] = "conflict_recheck_blocked"
REASON_KIND_NOT_PROMOTABLE: Final[str] = "kind_not_auto_promotable"
REASON_REVIEW_POLICY_DENIED: Final[str] = "review_policy_denied"
REASON_EXTERNAL_DISPUTE: Final[str] = "external_dispute_recorded"
REASON_EVIDENCE_UNINTERPRETABLE: Final[str] = "evidence_state_uninterpretable"
REASON_NOT_LIVE_PROPOSAL: Final[str] = "item_not_live_proposal"
REASON_POLICY_CHANGED: Final[str] = "policy_state_changed_during_evaluation"
REASON_MUTATION_RACE_LOST: Final[str] = "mutation_race_lost"
REASON_LEGACY_EVIDENCE_UNAVAILABLE: Final[str] = "historical_evidence_unavailable"

ADMISSION_REASON_CODES: Final[frozenset[str]] = frozenset(
    {
        REASON_LANE_LEGACY,
        REASON_LANE_EVIDENCE,
        REASON_MUTATION_COMMITTED,
        REASON_SHADOW_PREVIEW,
        REASON_LEGACY_IMPORT,
        REASON_LANE_AWAITING_AGE,
        REASON_NO_LANE,
        REASON_CONFLICT_UNRESOLVED,
        REASON_CONFLICT_RECHECK_BLOCKED,
        REASON_KIND_NOT_PROMOTABLE,
        REASON_REVIEW_POLICY_DENIED,
        REASON_EXTERNAL_DISPUTE,
        REASON_EVIDENCE_UNINTERPRETABLE,
        REASON_NOT_LIVE_PROPOSAL,
        REASON_POLICY_CHANGED,
        REASON_MUTATION_RACE_LOST,
        REASON_LEGACY_EVIDENCE_UNAVAILABLE,
    }
)

# Bounded diagnostic reference count. A #157 history can be long; the decision
# artifact records identity for at most this many effective/recent evidence
# assessments and never grows with it.
MAX_EVIDENCE_REFS: Final[int] = 8


class AdmissionAssessmentError(ValueError):
    """A decision envelope is not representable and must fail closed."""


def canonical_bytes(value: Any) -> bytes:
    """RFC 8785 (JCS) canonical UTF-8 bytes, via the same pinned library the
    context manifest and extraction receipts use."""
    return rfc8785.dumps(value)


def decision_hash(envelope: dict[str, Any]) -> str:
    """``sha256:<hex>`` over the RFC 8785 canonical bytes of ``envelope``."""
    return "sha256:" + hashlib.sha256(canonical_bytes(envelope)).hexdigest()


def digest(value: Any) -> str:
    """``sha256:<hex>`` over canonical bytes — used for both input and policy
    digests so a cross-runtime verifier needs exactly one algorithm."""
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


# --- Policy identity --------------------------------------------------------


def policy_config_payload(
    *,
    confidence_threshold: float,
    min_age_hours: int,
    evidence_enabled: bool,
    evidence_threshold: float,
    kind_auto_promote_allowed: bool,
) -> dict[str, Any]:
    """Every decision-affecting piece of current configuration and constants.

    Deliberately excludes timestamps, job/evaluation/request IDs and any other
    volatile invocation metadata: two evaluations of the same item under the
    same policy must produce the same digest, minutes or months apart. The
    kind auto-promotion eligibility fact *is* included — it is a per-item
    policy input, and a kind-registry change genuinely changes what policy
    would decide.
    """
    return {
        "policy_profile_key": POLICY_PROFILE_KEY,
        "policy_contract_version": POLICY_CONTRACT_VERSION,
        "legacy_confidence_threshold": confidence_threshold,
        "legacy_policy_version": LEGACY_PROMOTION_POLICY_VERSION,
        "min_age_hours": min_age_hours,
        "evidence_enabled": evidence_enabled,
        "evidence_threshold": evidence_threshold,
        "evidence_policy_version": EVIDENCE_PROMOTION_POLICY_VERSION,
        "evidence_score_ceiling": EVIDENCE_SCORE_CEILING,
        "evidence_source_prior_weight": EVIDENCE_SOURCE_PRIOR_WEIGHT,
        "evidence_retention_weight": EVIDENCE_RETENTION_WEIGHT,
        "evidence_taxonomy_minimum": EVIDENCE_TAXONOMY_MINIMUM,
        "supported_classification_version": SUPPORTED_CLASSIFICATION_VERSION,
        "supported_retention_policy_version": SUPPORTED_RETENTION_POLICY_VERSION,
        "kind_auto_promote_allowed": kind_auto_promote_allowed,
    }


def input_state_payload(item: MemoryItem, run: ClassificationRun | None) -> dict[str, Any]:
    """The item/governance/evidence state actually evaluated by current policy.

    Binds everything a current Path A input can change the result through:
    content identity, governed kind, review/conflict state, source type and
    prior, retention fields, the bound classification receipt's identity and
    versions, and the verification/authority/sensitivity facts policy reads.
    No memory content, no transcript, no extraction spans.

    #157 assessment identity is deliberately absent: those references are
    diagnostic in v1, and letting them move this digest would make an
    evidence assessment retroactively look like a promotion input.
    """
    return {
        "content_hash": item.content_hash,
        "kind": item.kind,
        "review_status": item.review_status,
        "valid_to": _iso(item.valid_to),
        "superseded_by": str(item.superseded_by) if item.superseded_by else None,
        "source_type": item.source_type,
        "source_trust": item.source_trust,
        "source_confidence_prior": item.source_confidence_prior,
        "memory_confidence": item.memory_confidence,
        "retention_confidence": item.retention_confidence,
        "retention_disposition": item.retention_disposition,
        "retention_evidence_at": _iso(item.retention_evidence_at),
        "conflict_resolution_status": item.conflict_resolution_status,
        "conflicts_with_item_id": (
            str(item.conflicts_with_item_id) if item.conflicts_with_item_id else None
        ),
        "authority": item.authority,
        "sensitivity": item.sensitivity,
        "human_verified": item.human_verified,
        "visibility": item.visibility,
        "created_at": _iso(item.created_at),
        "classification_run": (
            None
            if run is None
            else {
                "id": str(run.id),
                "bound_at": _iso(run.bound_at),
                "content_hash": run.content_hash,
                "source_type": run.source_type,
                "suggested_kind": run.suggested_kind,
                "taxonomy_confidence": run.taxonomy_confidence,
                "retention_confidence": run.retention_confidence,
                "retention_disposition": run.retention_disposition,
                "classification_version": run.classification_version,
                "retention_policy_version": run.retention_policy_version,
                "created_at": _iso(run.created_at),
            }
        ),
    }


# --- Outcome and next-action classification ---------------------------------

# Blocker -> outcome category. The categories themselves are ranked by the
# precedence rule below; a blocker never picks the outcome on its own.
_BLOCKED_BLOCKERS: Final[frozenset[str]] = frozenset({"conflict", "conflict_recheck"})
_REVIEW_BLOCKERS: Final[frozenset[str]] = frozenset(
    {"kind_policy", "review_policy", "external_dispute"}
)
_INSUFFICIENT_BLOCKERS: Final[frozenset[str]] = frozenset(
    {
        "confidence",
        "evidence_disabled",
        "no_retention_evidence",
        "missing_source_prior",
        "retention_disposition",
        "taxonomy_confidence",
        "evidence_score",
        "evidence_version",
        "evidence_inconsistent",
    }
)

# `stale > blocked > review_required > cooling > insufficient_evidence >
# unknown`, with `admitted` / `would_admit` handled separately: a successful
# mutation is always `admitted` and its non-mutating shadow equivalent is
# always `would_admit`, regardless of what else is true.
_PRECEDENCE: Final[tuple[AdmissionOutcome, ...]] = (
    "stale",
    "blocked",
    "review_required",
    "cooling",
    "insufficient_evidence",
    "unknown",
)


@dataclass(frozen=True)
class LaneQualification:
    """Whether each current Path A trust lane would otherwise qualify.

    ``cooling`` is never inferred from the presence of an ``age`` blocker
    alone — that would call an item cooling when no lane could ever admit it.
    A lane is "otherwise qualified" only when its trust test passed and its
    observation/age boundary is the single remaining gate.
    """

    legacy_trust_qualified: bool
    legacy_age_qualified: bool
    evidence_trust_qualified: bool
    evidence_age_qualified: bool

    @property
    def any_lane_awaiting_age(self) -> bool:
        return (self.legacy_trust_qualified and not self.legacy_age_qualified) or (
            self.evidence_trust_qualified and not self.evidence_age_qualified
        )


def classify_outcome(
    *,
    mode: AdmissionMode,
    mutated: bool,
    would_admit: bool,
    live_proposal: bool,
    blockers: list[str] | tuple[str, ...],
    lanes: LaneQualification,
    policy_changed: bool = False,
    uninterpretable: bool = False,
) -> AdmissionOutcome:
    """Map one current-policy evaluation onto the closed v1 outcome vocabulary.

    ``mutated`` means this assessment atomically authorized and completed
    ``proposed -> active``. ``would_admit`` means current policy would admit
    but no lifecycle mutation is permitted (a shadow/dry-run evaluation).
    ``policy_changed`` marks a pre-lock result whose policy/config digest no
    longer matches the locked authoritative state, which can neither become
    current nor authorize a mutation.
    """
    if mutated:
        return "admitted"
    if not live_proposal:
        return "not_applicable"
    if policy_changed:
        return "stale"
    if would_admit:
        # A non-mutating evaluation that current policy would admit. Only
        # shadow mode can legitimately reach here: an authoritative pass that
        # would admit performs the mutation, so if it did not, something in
        # the locked state stopped it and the caller reports that instead.
        return "would_admit" if mode == "shadow" else "unknown"
    codes = set(blockers)
    categories: set[AdmissionOutcome] = set()
    if codes & _BLOCKED_BLOCKERS:
        categories.add("blocked")
    if codes & _REVIEW_BLOCKERS:
        categories.add("review_required")
    if "age" in codes and lanes.any_lane_awaiting_age:
        categories.add("cooling")
    if codes & _INSUFFICIENT_BLOCKERS:
        categories.add("insufficient_evidence")
    if uninterpretable or not categories:
        # Never coerce an uninterpretable or unexplained state into
        # "insufficient evidence" (or a zero score): unknown is a distinct,
        # honest answer that operators can act on.
        categories.add("unknown")
    for outcome in _PRECEDENCE:
        if outcome in categories:
            return outcome
    return "unknown"


def next_actions_for(
    *, outcome: AdmissionOutcome, blockers: list[str] | tuple[str, ...]
) -> list[AdmissionNextAction]:
    """The bounded, deterministically ordered set of things that must happen.

    Multiple actions are returned when independently required — a cooling item
    that also carries an unresolved conflict needs both a wait and a
    resolution — and always in :data:`NEXT_ACTION_ORDER`.
    """
    if outcome in {"admitted", "would_admit"}:
        return ["none"]
    if outcome == "not_applicable":
        return ["none"]
    actions: set[AdmissionNextAction] = set()
    if outcome == "stale":
        actions.add("policy_reconciliation_required")
    if outcome == "cooling":
        actions.add("wait_until")
    codes = set(blockers)
    if codes & _BLOCKED_BLOCKERS:
        actions.add("conflict_resolution_required")
    if codes & _REVIEW_BLOCKERS:
        actions.add("human_review_required")
    # Missing or unsupported classification evidence is solved by producing a
    # receipt (or a supported one), not by finding better evidence.
    if codes & {"no_retention_evidence", "evidence_version", "evidence_inconsistent"}:
        actions.add("classification_required")
    # A deficiency that rerunning the same classifier over the same input
    # cannot fix: the score, disposition, taxonomy confidence or source
    # support itself has to change.
    if codes & {
        "evidence_score",
        "retention_disposition",
        "taxonomy_confidence",
        "missing_source_prior",
        "confidence",
    }:
        actions.add("new_evidence_required")
    # The evidence lane being switched off is a configuration fact; no amount
    # of new evidence resolves it.
    if "evidence_disabled" in codes:
        actions.add("policy_reconciliation_required")
    if outcome == "unknown" and not actions:
        actions.add("human_review_required")
    if not actions:
        actions.add("none")
    return [action for action in NEXT_ACTION_ORDER if action in actions]


def reason_codes_for(
    *,
    mode: AdmissionMode,
    outcome: AdmissionOutcome,
    selected_basis: str | None,
    blockers: list[str] | tuple[str, ...],
    lanes: LaneQualification,
    policy_changed: bool = False,
    race_lost: bool = False,
    legacy_evidence_unavailable: bool = False,
) -> list[str]:
    """Stable, bounded explanation codes, sorted for hash determinism."""
    codes = set(blockers)
    reasons: set[str] = set()
    if mode == "shadow":
        reasons.add(REASON_SHADOW_PREVIEW)
    if mode == "legacy_import":
        reasons.add(REASON_LEGACY_IMPORT)
    if legacy_evidence_unavailable:
        reasons.add(REASON_LEGACY_EVIDENCE_UNAVAILABLE)
    if selected_basis == "legacy_confidence":
        reasons.add(REASON_LANE_LEGACY)
    elif selected_basis == "retention_evidence":
        reasons.add(REASON_LANE_EVIDENCE)
    if outcome == "admitted":
        reasons.add(REASON_MUTATION_COMMITTED)
    if outcome == "not_applicable":
        reasons.add(REASON_NOT_LIVE_PROPOSAL)
    if race_lost:
        reasons.add(REASON_MUTATION_RACE_LOST)
    if policy_changed:
        reasons.add(REASON_POLICY_CHANGED)
    if outcome == "cooling":
        reasons.add(REASON_LANE_AWAITING_AGE)
    if selected_basis is None and outcome not in {"admitted", "not_applicable", "stale"}:
        reasons.add(REASON_NO_LANE)
    if "conflict" in codes:
        reasons.add(REASON_CONFLICT_UNRESOLVED)
    if "conflict_recheck" in codes:
        reasons.add(REASON_CONFLICT_RECHECK_BLOCKED)
    if "kind_policy" in codes:
        reasons.add(REASON_KIND_NOT_PROMOTABLE)
    if "review_policy" in codes:
        reasons.add(REASON_REVIEW_POLICY_DENIED)
    if "external_dispute" in codes:
        reasons.add(REASON_EXTERNAL_DISPUTE)
    if "evidence_inconsistent" in codes or outcome == "unknown":
        reasons.add(REASON_EVIDENCE_UNINTERPRETABLE)
    unknown = reasons - ADMISSION_REASON_CODES
    if unknown:
        raise AdmissionAssessmentError(f"unknown admission reason code(s): {sorted(unknown)!r}")
    return sorted(reasons)


# --- Canonical decision envelope --------------------------------------------


@dataclass(frozen=True)
class AdmissionDecision:
    """One fully-formed, hashable admission decision.

    Everything here is deterministic in the evaluated state and policy. The
    invocation identity (assessment id, evaluation/job/request ids, actor,
    ``created_at``) and the mutable projection deliberately live outside the
    hashed envelope, so the same decision over the same inputs verifies to the
    same hash across runtimes and across replays.
    """

    tenant_id: uuid.UUID
    memory_item_id: uuid.UUID
    mode: AdmissionMode
    item_content_hash: str
    input_digest: str
    policy_config_digest: str
    selected_basis: str | None
    outcome: AdmissionOutcome
    blocker_codes: tuple[str, ...]
    reason_codes: tuple[str, ...]
    decision_inputs: dict[str, Any]
    conflict_recheck_status: str
    cooling_period_start: datetime | None
    eligible_at: datetime | None
    next_evaluation_at: datetime | None
    next_actions: tuple[AdmissionNextAction, ...]

    def envelope(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "tenant_id": str(self.tenant_id),
            "memory_item_id": str(self.memory_item_id),
            "mode": self.mode,
            "item_content_hash": self.item_content_hash,
            "input_digest": self.input_digest,
            "policy_profile_key": POLICY_PROFILE_KEY,
            "policy_contract_version": POLICY_CONTRACT_VERSION,
            "policy_config_digest": self.policy_config_digest,
            "selected_basis": self.selected_basis,
            "outcome": self.outcome,
            "blocker_codes": list(self.blocker_codes),
            "reason_codes": list(self.reason_codes),
            "decision_inputs": self.decision_inputs,
            "conflict_recheck_status": self.conflict_recheck_status,
            "cooling_period_start": _iso(self.cooling_period_start),
            "eligible_at": _iso(self.eligible_at),
            "next_evaluation_at": _iso(self.next_evaluation_at),
            "next_actions": list(self.next_actions),
        }

    def hash(self) -> str:
        return decision_hash(self.envelope())


def canonical_blocker_order(blockers: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """De-duplicated blocker codes in canonical (sorted) order.

    The evaluator emits blockers in discovery order, which depends on which
    lane was examined first. Sorting makes the hashed envelope independent of
    that, so the same decision always hashes identically.
    """
    return tuple(sorted(set(blockers)))


def build_decision(
    *,
    item: MemoryItem,
    run: ClassificationRun | None,
    mode: AdmissionMode,
    mutated: bool,
    live_proposal: bool,
    blockers: list[str] | tuple[str, ...],
    selected_basis: str | None,
    lanes: LaneQualification,
    decision_inputs: dict[str, Any],
    policy_config: dict[str, Any],
    conflict_recheck_status: str,
    cooling_period_start: datetime | None,
    eligible_at: datetime | None,
    next_evaluation_at: datetime | None,
    policy_changed: bool = False,
    race_lost: bool = False,
    uninterpretable: bool = False,
    legacy_evidence_unavailable: bool = False,
    outcome_override: AdmissionOutcome | None = None,
) -> AdmissionDecision:
    """Assemble the canonical decision from one already-made policy evaluation.

    This function never re-decides anything: ``blockers``, ``selected_basis``
    and ``mutated`` all come from the production evaluator and mutation path.
    ``outcome_override`` exists for the legacy import, whose outcome reflects
    currently stored state rather than a reconstructed historical evaluation.
    """
    if conflict_recheck_status not in CONFLICT_RECHECK_STATUSES:
        raise AdmissionAssessmentError(
            f"unknown conflict_recheck_status: {conflict_recheck_status!r}"
        )
    if mode == "shadow" and mutated:
        raise AdmissionAssessmentError("a shadow assessment can never mutate item state")
    ordered_blockers = canonical_blocker_order(blockers)
    would_admit = not ordered_blockers and selected_basis is not None and live_proposal
    outcome = outcome_override or classify_outcome(
        mode=mode,
        mutated=mutated,
        would_admit=would_admit,
        live_proposal=live_proposal,
        blockers=ordered_blockers,
        lanes=lanes,
        policy_changed=policy_changed,
        uninterpretable=uninterpretable,
    )
    if outcome not in ADMISSION_OUTCOMES:
        raise AdmissionAssessmentError(f"unknown admission outcome: {outcome!r}")
    actions = next_actions_for(outcome=outcome, blockers=ordered_blockers)
    reasons = reason_codes_for(
        mode=mode,
        outcome=outcome,
        selected_basis=selected_basis,
        blockers=ordered_blockers,
        lanes=lanes,
        policy_changed=policy_changed,
        race_lost=race_lost,
        legacy_evidence_unavailable=legacy_evidence_unavailable,
    )
    # `wait_until` is only meaningful with a time to wait until; a cooling
    # outcome without one would be an unactionable next action.
    due = next_evaluation_at if "wait_until" in actions else None
    if "wait_until" in actions and due is None:
        raise AdmissionAssessmentError("a cooling decision must carry next_evaluation_at")
    return AdmissionDecision(
        tenant_id=item.tenant_id,
        memory_item_id=item.id,
        mode=mode,
        item_content_hash=item.content_hash,
        input_digest=digest(input_state_payload(item, run)),
        policy_config_digest=digest(policy_config),
        selected_basis=selected_basis,
        outcome=outcome,
        blocker_codes=ordered_blockers,
        reason_codes=tuple(reasons),
        decision_inputs=decision_inputs,
        conflict_recheck_status=conflict_recheck_status,
        cooling_period_start=cooling_period_start,
        eligible_at=eligible_at,
        next_evaluation_at=next_evaluation_at,
        next_actions=tuple(actions),
    )


# --- Persistence ------------------------------------------------------------


def _mode_rank(mode: str) -> int:
    return 1 if mode == "authoritative" else 0


async def evidence_assessment_refs(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    memory_item_id: uuid.UUID,
    limit: int = MAX_EVIDENCE_REFS,
) -> list[dict[str, Any]]:
    """Safe, bounded, diagnostic-only references to #157 assessments.

    Records identity — assessment ID, purpose, schema version, canonical hash,
    contract version — for the evidence assessments visible at evaluation
    time. It never copies dimensions, provider output, or reasoning, and it
    never fabricates a reference from item columns: an item with no #157
    assessment gets an empty list, which reads as "missing", not as "clean".

    These references are diagnostic in v1. They are recorded outside
    ``input_digest`` and ``policy_config_digest`` precisely so their presence
    cannot make epistemic/risk values authoritative for admission.
    """
    rows = (
        await session.scalars(
            select(MemoryAssessment)
            .where(
                MemoryAssessment.tenant_id == tenant_id,
                MemoryAssessment.memory_item_id == memory_item_id,
                MemoryAssessment.state.in_(("completed", "legacy")),
            )
            .order_by(MemoryAssessment.created_at.desc(), MemoryAssessment.id.desc())
            .limit(limit)
        )
    ).all()
    refs: list[dict[str, Any]] = []
    for row in rows:
        receipt = row.receipt if isinstance(row.receipt, dict) else {}
        refs.append(
            {
                "assessment_id": str(row.id),
                "purpose": row.purpose,
                "schema_version": receipt.get("schema_version"),
                "canonical_hash": row.canonical_hash or None,
                "contract_hash": row.contract_hash,
                "state": row.state,
            }
        )
    return refs


async def insert_assessment(
    session: AsyncSession,
    decision: AdmissionDecision,
    *,
    trigger_type: str,
    trigger_id: str,
    invocation_source: str,
    evaluated_at: datetime,
    evaluation_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    actor_principal_id: uuid.UUID | None = None,
    classification_run_id: uuid.UUID | None = None,
    evidence_refs: list[dict[str, Any]] | None = None,
    prior_assessment_id: uuid.UUID | None = None,
    linked_item_event_id: uuid.UUID | None = None,
    assessment_id: uuid.UUID | None = None,
) -> AdmissionAssessment:
    """Append one immutable assessment row inside the caller's transaction.

    Never commits: the caller owns the transaction, which is what makes the
    mutation/assessment/event/projection commit atomic (or fail closed
    together).
    """
    row = AdmissionAssessment(
        id=assessment_id or uuid.uuid4(),
        tenant_id=decision.tenant_id,
        memory_item_id=decision.memory_item_id,
        schema_version=SCHEMA_VERSION,
        mode=decision.mode,
        evaluation_id=evaluation_id,
        job_id=job_id,
        trigger_type=trigger_type,
        trigger_id=trigger_id,
        invocation_source=invocation_source,
        actor_principal_id=actor_principal_id,
        evaluated_at=evaluated_at,
        item_content_hash=decision.item_content_hash,
        input_digest=decision.input_digest,
        policy_profile_key=POLICY_PROFILE_KEY,
        policy_contract_version=POLICY_CONTRACT_VERSION,
        policy_config_digest=decision.policy_config_digest,
        selected_basis=decision.selected_basis,
        outcome=decision.outcome,
        blocker_codes=list(decision.blocker_codes),
        reason_codes=list(decision.reason_codes),
        decision_inputs=decision.decision_inputs,
        classification_run_id=classification_run_id,
        available_memory_assessment_refs=evidence_refs or [],
        conflict_recheck_status=decision.conflict_recheck_status,
        cooling_period_start=decision.cooling_period_start,
        eligible_at=decision.eligible_at,
        next_evaluation_at=decision.next_evaluation_at,
        next_actions=list(decision.next_actions),
        decision_hash=decision.hash(),
        prior_assessment_id=prior_assessment_id,
        linked_item_event_id=linked_item_event_id,
    )
    session.add(row)
    await session.flush()
    return row


async def project_current(session: AsyncSession, row: AdmissionAssessment) -> None:
    """Point the current projection at ``row`` unless something newer holds it.

    Shadow rows can never become current — enforced here and, independently,
    by the projection table's own CHECK constraint. A ``stale`` decision can
    never become current either: by definition its input or policy digest no
    longer matches the state it was computed against, so it cannot authorize
    anything and must not be what a reader resolves to. It is still kept as
    immutable history.

    Precedence is
    ``(mode_rank, evaluated_at, mutation_rank, assessment_id)``: an
    authoritative row always supersedes a ``legacy_import`` one regardless of
    time; within a mode the later evaluation wins; and a same-instant tie —
    which is exactly what a lost promotion race produces — resolves toward the
    evaluation that actually mutated item state, so the projection
    deterministically lands on the winner.

    Historical rows are never rewritten to mark them stale: freshness is a
    comparison against current state (see :func:`resolve_projection_status`),
    not a stored flag.
    """
    if row.mode == "shadow" or row.outcome == "stale":
        return
    rank = _mode_rank(row.mode)
    mutation_rank = 1 if row.outcome == "admitted" else 0
    table = AdmissionAssessmentCurrent
    values = {
        "tenant_id": row.tenant_id,
        "memory_item_id": row.memory_item_id,
        "policy_profile_key": row.policy_profile_key,
        "assessment_id": row.id,
        "mode": row.mode,
        "mode_rank": rank,
        "mutation_rank": mutation_rank,
        "evaluated_at": row.evaluated_at,
        "updated_at": row.evaluated_at,
    }
    stmt = pg_insert(table).values(**values)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[table.tenant_id, table.memory_item_id, table.policy_profile_key],
            set_={
                "assessment_id": stmt.excluded.assessment_id,
                "mode": stmt.excluded.mode,
                "mode_rank": stmt.excluded.mode_rank,
                "mutation_rank": stmt.excluded.mutation_rank,
                "evaluated_at": stmt.excluded.evaluated_at,
                "updated_at": stmt.excluded.updated_at,
            },
            where=tuple_(
                table.mode_rank, table.evaluated_at, table.mutation_rank, table.assessment_id
            )
            < tuple_(
                stmt.excluded.mode_rank,
                stmt.excluded.evaluated_at,
                stmt.excluded.mutation_rank,
                stmt.excluded.assessment_id,
            ),
        )
    )


# --- Resolution -------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedAdmission:
    """The current admission state of one item under one policy profile.

    ``status`` distinguishes the four operationally different situations the
    API must never blur together: ``missing`` (no decision has ever been
    recorded), ``current`` (the pointed decision still matches present state
    and policy), ``stale`` (it does not, so it cannot authorize anything), and
    ``legacy_import`` (a bounded snapshot of stored state, never a claim about
    a historical evaluation). An item whose current decision has outcome
    ``unknown`` is emphatically *not* ``missing``: policy looked and could not
    interpret the state safely.
    """

    status: AdmissionProjectionStatus
    assessment: AdmissionAssessment | None


def resolve_projection_status(
    assessment: AdmissionAssessment | None,
    *,
    current_input_digest: str,
    current_policy_config_digest: str,
) -> ResolvedAdmission:
    """Compare the pointed assessment's digests against current state."""
    if assessment is None:
        return ResolvedAdmission("missing", None)
    fresh = (
        assessment.input_digest == current_input_digest
        and assessment.policy_config_digest == current_policy_config_digest
    )
    if not fresh:
        return ResolvedAdmission("stale", assessment)
    if assessment.mode == "legacy_import":
        return ResolvedAdmission("legacy_import", assessment)
    return ResolvedAdmission("current", assessment)


async def load_current_assessment(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID | str,
    memory_item_id: uuid.UUID,
    policy_profile_key: str = POLICY_PROFILE_KEY,
) -> AdmissionAssessment | None:
    """Read the assessment the current projection points at, or ``None``."""
    row = await session.scalar(
        select(AdmissionAssessment)
        .join(
            AdmissionAssessmentCurrent,
            AdmissionAssessmentCurrent.assessment_id == AdmissionAssessment.id,
        )
        .where(
            AdmissionAssessmentCurrent.tenant_id == uuid.UUID(str(tenant_id)),
            AdmissionAssessmentCurrent.memory_item_id == memory_item_id,
            AdmissionAssessmentCurrent.policy_profile_key == policy_profile_key,
        )
    )
    return row


def summary_payload(resolved: ResolvedAdmission) -> dict[str, Any]:
    """The safe item-detail / promotion-readiness summary (issue #159 §API).

    Carries no provider internals, no conflict candidate identity and no
    normalized decision inputs — those need review/admin authority and a
    separate detail call. ``missing`` stays distinguishable from an
    ``unknown`` outcome.
    """
    row = resolved.assessment
    if row is None:
        return {
            "admission_assessment_id": None,
            "admission_assessment_status": "missing",
            "admission_outcome": None,
            "admission_policy_profile": POLICY_PROFILE_KEY,
            "admission_policy_version": POLICY_CONTRACT_VERSION,
            "admission_reason_codes": [],
            "admission_next_actions": [],
            "next_evaluation_at": None,
        }
    return {
        "admission_assessment_id": str(row.id),
        "admission_assessment_status": resolved.status,
        "admission_outcome": row.outcome,
        "admission_policy_profile": row.policy_profile_key,
        "admission_policy_version": row.policy_contract_version,
        "admission_reason_codes": list(row.reason_codes),
        "admission_next_actions": list(row.next_actions),
        "next_evaluation_at": _iso(row.next_evaluation_at),
    }


def next_evaluation_for(
    *,
    outcome_eligible_at: datetime | None,
    now: datetime,
    fallback_hours: int | None = None,
) -> datetime | None:
    """When this decision should next be reconsidered.

    A cooling decision is due when its lane's observation boundary passes. A
    decision that needs new evidence, human review or a conflict resolution
    has no deterministic due time — an external event, not the clock, changes
    it — so it gets ``None`` rather than an invented schedule, unless the
    caller supplies an explicit ``fallback_hours`` re-check cadence.
    """
    if outcome_eligible_at is not None and outcome_eligible_at > now:
        return outcome_eligible_at
    if fallback_hours is not None:
        return now + timedelta(hours=fallback_hours)
    return None


__all__ = [
    "ADMISSION_OUTCOMES",
    "ADMISSION_REASON_CODES",
    "CONFLICT_RECHECK_STATUSES",
    "MAX_EVIDENCE_REFS",
    "NEXT_ACTION_ORDER",
    "POLICY_CONTRACT_VERSION",
    "POLICY_PROFILE_KEY",
    "SCHEMA_VERSION",
    "AdmissionAssessmentError",
    "AdmissionDecision",
    "AdmissionMode",
    "AdmissionNextAction",
    "AdmissionOutcome",
    "AdmissionProjectionStatus",
    "LaneQualification",
    "ResolvedAdmission",
    "build_decision",
    "canonical_blocker_order",
    "canonical_bytes",
    "classify_outcome",
    "decision_hash",
    "digest",
    "evidence_assessment_refs",
    "input_state_payload",
    "insert_assessment",
    "load_current_assessment",
    "next_actions_for",
    "next_evaluation_for",
    "policy_config_payload",
    "project_current",
    "reason_codes_for",
    "resolve_projection_status",
    "summary_payload",
]
