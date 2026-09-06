"""Guarded two-lane Promotion Path A service."""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import Exists, Select, and_, exists, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from engram.admission_assessment import (
    AdmissionDecision,
    LaneQualification,
    build_decision,
    evidence_assessment_refs,
    input_state_payload,
    insert_assessment,
    policy_config_payload,
    project_current,
)
from engram.config import settings
from engram.conflicts import PromotionConflictCheck, check_promotion_conflict
from engram.feedback import current_feedback_predicate
from engram.internal_actors import (
    REVIEW_AUTOMATION_INTERNAL_KEY,
    InternalActorInvariantError,
    resolve_internal_system_actor,
)
from engram.memory_access import write_eligibility_expression
from engram.memory_context import (
    INTERNAL_MEMORY_CONTEXT_VERSION,
    ResolvedMemoryContext,
    context_provenance,
)
from engram.models import (
    ClassificationRun,
    FeedbackEvent,
    ItemEvent,
    MemoryItem,
    MemoryKind,
    PromotionReconciliationState,
    TenantConfig,
)
from engram.promotion_policy import (
    DEFAULT_EVIDENCE_THRESHOLD,
    EVIDENCE_PROMOTION_POLICY_VERSION,
    EVIDENCE_RETENTION_WEIGHT,
    EVIDENCE_SCORE_CEILING,
    EVIDENCE_SOURCE_PRIOR_WEIGHT,
    EVIDENCE_TAXONOMY_MINIMUM,
    LEGACY_PROMOTION_POLICY_VERSION,
    PromotionBasis,
    PromotionPolicyError,
    choose_basis,
    evidence_score_v1,
)
from engram.review_policy import TrustedReviewOperation, evaluate_transition

_FALLBACK_CONFIDENCE_THRESHOLD = 0.7
_FALLBACK_MIN_AGE_HOURS = 72
TRUSTED_REVIEW_INTERNAL_KEY = REVIEW_AUTOMATION_INTERNAL_KEY
TrustedActorInvariantError = InternalActorInvariantError

BLOCK_KIND_POLICY = "kind_policy"
BLOCK_EVIDENCE_DISABLED = "evidence_disabled"
BLOCK_NO_EVIDENCE = "no_retention_evidence"
BLOCK_SOURCE_PRIOR = "missing_source_prior"
BLOCK_DISPOSITION = "retention_disposition"
BLOCK_TAXONOMY = "taxonomy_confidence"
BLOCK_SCORE = "evidence_score"
BLOCK_VERSION = "evidence_version"
BLOCK_INCONSISTENT = "evidence_inconsistent"
BLOCK_CONFIDENCE = "confidence"
BLOCK_AGE = "age"
BLOCK_CONFLICT = "conflict"
BLOCK_DISPUTE = "external_dispute"
BLOCK_RECHECK = "conflict_recheck"
BLOCK_REVIEW_POLICY = "review_policy"

# The canonical, closed set of promotion-blocker codes this module can ever
# produce. Exported so other read-only consumers (e.g. `engram.doctor`'s
# review.backlog check) can allow-list untrusted remote blocker strings
# against the single real source of truth, rather than maintaining a
# separate, driftable copy of this vocabulary (ENG-LOOP-001A-FIX2 / FIX2-3).
PROMOTION_BLOCKER_CODES: frozenset[str] = frozenset(
    {
        BLOCK_KIND_POLICY,
        BLOCK_EVIDENCE_DISABLED,
        BLOCK_NO_EVIDENCE,
        BLOCK_SOURCE_PRIOR,
        BLOCK_DISPOSITION,
        BLOCK_TAXONOMY,
        BLOCK_SCORE,
        BLOCK_VERSION,
        BLOCK_INCONSISTENT,
        BLOCK_CONFIDENCE,
        BLOCK_AGE,
        BLOCK_CONFLICT,
        BLOCK_DISPUTE,
        BLOCK_RECHECK,
        BLOCK_REVIEW_POLICY,
    }
)


async def resolve_trusted_system_actor(session: AsyncSession, tenant_id: str) -> uuid.UUID:
    return await resolve_internal_system_actor(
        session, tenant_id=tenant_id, internal_key=REVIEW_AUTOMATION_INTERNAL_KEY
    )


@dataclass
class PromotionCandidate:
    item_id: uuid.UUID
    would_promote: bool
    selected_basis: PromotionBasis | None
    blockers: list[str]
    legacy_confidence: float
    legacy_threshold: float
    evidence_score: float | None
    evidence_threshold: float
    taxonomy_confidence: float | None
    retention_disposition: str | None
    classification_run_id: uuid.UUID | None
    cooling_period_start: datetime | None
    eligible_at: datetime | None
    legacy_eligible_at: datetime
    evidence_cooling_period_start: datetime | None
    evidence_eligible_at: datetime | None
    kind: str
    kind_auto_promote_allowed: bool
    conflict_recheck_status: str


@dataclass(frozen=True)
class PromotionSupport:
    """Preloaded, database-independent support for one candidate assessment."""

    kind: MemoryKind | None
    classification_run: ClassificationRun | None
    has_external_dispute: bool = False
    has_external_noise_feedback: bool = False


@dataclass
class PromotionResult:
    tenant_id: str
    enabled: bool
    confidence_threshold: float
    min_age_hours: int
    evidence_enabled: bool = False
    evidence_threshold: float = DEFAULT_EVIDENCE_THRESHOLD
    dry_run: bool = False
    rotation_wrapped: bool = False
    scanned: int = 0
    promoted: int = 0
    promoted_legacy_confidence: int = 0
    promoted_retention_evidence: int = 0
    would_promote: int = 0
    would_promote_legacy_confidence: int = 0
    would_promote_retention_evidence: int = 0
    skipped_confidence: int = 0
    skipped_age: int = 0
    skipped_conflict: int = 0
    skipped_disabled: int = 0
    skipped_dispute: int = 0
    skipped_conflict_recheck: int = 0
    skipped_kind_policy: int = 0
    skipped_evidence_disabled: int = 0
    skipped_no_retention_evidence: int = 0
    skipped_missing_source_prior: int = 0
    skipped_retention_disposition: int = 0
    skipped_taxonomy_confidence: int = 0
    skipped_evidence_score: int = 0
    skipped_evidence_version: int = 0
    skipped_evidence_inconsistent: int = 0
    skipped_review_policy: int = 0
    promoted_ids: list[uuid.UUID] = field(default_factory=list)
    would_promote_ids: list[uuid.UUID] = field(default_factory=list)
    candidates: list[PromotionCandidate] = field(default_factory=list)


# The startup compatibility caller may inspect the exact rows that its
# ``FOR UPDATE SKIP LOCKED`` query acquired before this function performs any
# lifecycle mutation.  This is deliberately a window hand-off, not a second
# evaluator or an authority hook.
@dataclass(frozen=True)
class PromotionObservedWindow:
    """Exact locked selection and wrap fact from the authoritative pass."""

    items: Sequence[MemoryItem]
    rotation_wrapped: bool


PromotionWindowObserver = Callable[[PromotionObservedWindow], Awaitable[None]]


def summarize(result: PromotionResult) -> str:
    action = "would_promote" if result.dry_run else "promoted"
    lane_legacy = (
        result.would_promote_legacy_confidence
        if result.dry_run
        else result.promoted_legacy_confidence
    )
    lane_evidence = (
        result.would_promote_retention_evidence
        if result.dry_run
        else result.promoted_retention_evidence
    )
    action_count = result.would_promote if result.dry_run else result.promoted
    return (
        f"tenant={result.tenant_id} threshold={result.confidence_threshold} "
        f"evidence_enabled={result.evidence_enabled} "
        f"evidence_threshold={result.evidence_threshold} "
        f"min_age_hours={result.min_age_hours} scanned={result.scanned} {action}="
        f"{action_count} legacy={lane_legacy} evidence={lane_evidence}"
    )


async def has_external_dispute_event(session: AsyncSession, item: MemoryItem) -> bool:
    dispute = (
        await session.execute(
            select(ItemEvent.id)
            .where(
                ItemEvent.item_id == item.id,
                ItemEvent.event_type == "review_change",
                ItemEvent.field_name == "review_status",
                ItemEvent.new_value == "disputed",
                ItemEvent.actor_principal_id.is_not(None),
                ItemEvent.actor_principal_id != item.principal_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if dispute is not None:
        return True
    noise = (
        await session.execute(
            select(FeedbackEvent.id)
            .where(
                FeedbackEvent.item_id == item.id,
                FeedbackEvent.verdict == "noise",
                current_feedback_predicate(),
                FeedbackEvent.principal_id != item.principal_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return noise is not None


async def _config(session: AsyncSession, tenant_id: str) -> TenantConfig | None:
    return (
        await session.execute(
            select(TenantConfig).where(
                TenantConfig.tenant_id == tenant_id, TenantConfig.active.is_(True)
            )
        )
    ).scalar_one_or_none()


def _config_values(config: TenantConfig | None) -> tuple[bool, float, int, bool, float]:
    if config is None:
        return (
            True,
            _FALLBACK_CONFIDENCE_THRESHOLD,
            _FALLBACK_MIN_AGE_HOURS,
            False,
            DEFAULT_EVIDENCE_THRESHOLD,
        )
    return (
        bool(config.auto_promote_enabled),
        config.auto_promote_confidence_threshold,
        config.auto_promote_min_age_hours,
        bool(config.auto_promote_evidence_enabled),
        config.auto_promote_evidence_threshold,
    )


def _supported(run: ClassificationRun) -> bool:
    # These are the only currently supported receipt versions; unknown values
    # intentionally fail closed rather than assuming compatibility.
    return (
        run.classification_version == "classification-v2"
        and run.retention_policy_version == "retention-v1"
    )


async def load_promotion_support(
    session: AsyncSession, items: list[MemoryItem]
) -> dict[uuid.UUID, PromotionSupport]:
    """Load kind, receipt, dispute, and noise support in four bounded queries."""
    if not items:
        return {}
    tenant_id = items[0].tenant_id
    item_ids = [item.id for item in items]
    kinds = {
        row.name: row
        for row in (
            await session.execute(
                select(MemoryKind).where(
                    MemoryKind.tenant_id == tenant_id,
                    MemoryKind.name.in_({item.kind for item in items}),
                )
            )
        ).scalars()
    }
    runs = {
        row.memory_item_id: row
        for row in (
            await session.execute(
                select(ClassificationRun).where(ClassificationRun.memory_item_id.in_(item_ids))
            )
        ).scalars()
        if row.memory_item_id is not None
    }
    dispute_rows = (
        await session.execute(
            select(ItemEvent.item_id, ItemEvent.actor_principal_id).where(
                ItemEvent.item_id.in_(item_ids),
                ItemEvent.event_type == "review_change",
                ItemEvent.field_name == "review_status",
                ItemEvent.new_value == "disputed",
                ItemEvent.actor_principal_id.is_not(None),
            )
        )
    ).all()
    noise_rows = (
        await session.execute(
            select(FeedbackEvent.item_id, FeedbackEvent.principal_id).where(
                FeedbackEvent.item_id.in_(item_ids),
                FeedbackEvent.verdict == "noise",
                current_feedback_predicate(),
            )
        )
    ).all()
    authors = {item.id: item.principal_id for item in items}
    disputed = {item_id for item_id, actor in dispute_rows if actor != authors[item_id]}
    noisy = {item_id for item_id, actor in noise_rows if actor != authors[item_id]}
    return {
        item.id: PromotionSupport(
            kind=kinds.get(item.kind),
            classification_run=runs.get(item.id),
            has_external_dispute=item.id in disputed,
            has_external_noise_feedback=item.id in noisy,
        )
        for item in items
    }


def _evidence_state(
    item: MemoryItem, run: ClassificationRun | None
) -> tuple[list[str], float | None, datetime | None]:
    blockers: list[str] = []
    if item.source_confidence_prior is None:
        blockers.append(BLOCK_SOURCE_PRIOR)
    if (
        item.retention_confidence is None
        or item.retention_disposition is None
        or item.retention_evidence_at is None
    ):
        blockers.append(BLOCK_NO_EVIDENCE)
    if item.retention_disposition != "retain":
        blockers.append(BLOCK_DISPOSITION)
    if run is None:
        blockers.append(BLOCK_NO_EVIDENCE)
        return blockers, None, None
    if not _supported(run):
        blockers.append(BLOCK_VERSION)
    if (
        run.tenant_id != item.tenant_id
        or run.memory_item_id != item.id
        or run.bound_at is None
        or run.content_hash != item.content_hash
        or run.source_type != item.source_type
        or run.suggested_kind != item.kind
        or run.retention_confidence != item.retention_confidence
        or run.retention_disposition != item.retention_disposition
        or run.created_at != item.retention_evidence_at
    ):
        blockers.append(BLOCK_INCONSISTENT)
    if run.taxonomy_confidence < EVIDENCE_TAXONOMY_MINIMUM:
        blockers.append(BLOCK_TAXONOMY)
    if blockers:
        return blockers, None, None
    assert (
        item.source_confidence_prior is not None
        and item.retention_confidence is not None
        and item.retention_evidence_at is not None
    )
    try:
        score = evidence_score_v1(item.source_confidence_prior, item.retention_confidence)
    except PromotionPolicyError:
        return [BLOCK_INCONSISTENT], None, None
    return [], score, max(item.created_at, item.retention_evidence_at, run.created_at)


def _count_blockers(result: PromotionResult, blockers: list[str]) -> None:
    mapping = {
        BLOCK_KIND_POLICY: "skipped_kind_policy",
        BLOCK_EVIDENCE_DISABLED: "skipped_evidence_disabled",
        BLOCK_NO_EVIDENCE: "skipped_no_retention_evidence",
        BLOCK_SOURCE_PRIOR: "skipped_missing_source_prior",
        BLOCK_DISPOSITION: "skipped_retention_disposition",
        BLOCK_TAXONOMY: "skipped_taxonomy_confidence",
        BLOCK_SCORE: "skipped_evidence_score",
        BLOCK_VERSION: "skipped_evidence_version",
        BLOCK_INCONSISTENT: "skipped_evidence_inconsistent",
        BLOCK_CONFIDENCE: "skipped_confidence",
        BLOCK_AGE: "skipped_age",
        BLOCK_CONFLICT: "skipped_conflict",
        BLOCK_DISPUTE: "skipped_dispute",
        BLOCK_RECHECK: "skipped_conflict_recheck",
        BLOCK_REVIEW_POLICY: "skipped_review_policy",
    }
    for blocker in set(blockers):
        attr = mapping.get(blocker)
        if attr:
            setattr(result, attr, getattr(result, attr) + 1)


def assess_promotion_candidate(
    item: MemoryItem,
    support: PromotionSupport,
    *,
    confidence_threshold: float,
    min_age_hours: int,
    evidence_enabled: bool,
    evidence_threshold: float,
    now: datetime,
    conflict_recheck_status: str = "not_run",
) -> PromotionCandidate:
    kind = support.kind
    run = support.classification_run
    allowed_kind = bool(kind and kind.enabled and kind.auto_promote_from_inferred)
    blockers: list[str] = [] if allowed_kind else [BLOCK_KIND_POLICY]
    evidence_blockers, score, cooling_start = _evidence_state(item, run)
    if not evidence_enabled:
        evidence_blockers.append(BLOCK_EVIDENCE_DISABLED)
    evidence_trust = not evidence_blockers and score is not None and score >= evidence_threshold
    if not evidence_blockers and score is not None and score < evidence_threshold:
        evidence_blockers.append(BLOCK_SCORE)
    legacy_trust = item.memory_confidence >= confidence_threshold
    legacy_age = item.created_at + timedelta(hours=min_age_hours) <= now
    evidence_age = (
        cooling_start is not None and cooling_start + timedelta(hours=min_age_hours) <= now
    )
    selected = (
        choose_basis(
            legacy_trust_qualified=legacy_trust,
            legacy_age_qualified=legacy_age,
            evidence_trust_qualified=evidence_trust,
            evidence_age_qualified=evidence_age,
            legacy_score=item.memory_confidence,
            legacy_threshold=confidence_threshold,
            evidence_score=score,
            evidence_threshold=evidence_threshold,
        ).selected_basis
        if allowed_kind
        else None
    )
    if selected is None:
        if not legacy_trust:
            blockers.append(BLOCK_CONFIDENCE)
        if legacy_trust and not legacy_age:
            blockers.append(BLOCK_AGE)
        blockers.extend(evidence_blockers)
        if evidence_trust and not evidence_age:
            blockers.append(BLOCK_AGE)
    if selected is not None:
        if item.conflict_resolution_status == "unresolved":
            blockers.append(BLOCK_CONFLICT)
        elif support.has_external_dispute or support.has_external_noise_feedback:
            blockers.append(BLOCK_DISPUTE)
        else:
            decision = evaluate_transition(
                principal_id=item.principal_id,
                principal_type="system",
                item_author_principal_id=item.principal_id,
                current_status=item.review_status,
                requested_status="active",
                trusted_operation=TrustedReviewOperation.PROMOTION,
            )
            if not decision.allowed:
                blockers.append(BLOCK_REVIEW_POLICY)
    legacy_eligible_at = item.created_at + timedelta(hours=min_age_hours)
    evidence_eligible_at = cooling_start + timedelta(hours=min_age_hours) if cooling_start else None
    selected_start = (
        cooling_start
        if selected == "retention_evidence"
        else item.created_at
        if selected == "legacy_confidence"
        else None
    )
    selected_eligible = (
        evidence_eligible_at
        if selected == "retention_evidence"
        else legacy_eligible_at
        if selected == "legacy_confidence"
        else None
    )
    return PromotionCandidate(
        item.id,
        selected is not None and not blockers,
        selected,
        list(dict.fromkeys(blockers)),
        item.memory_confidence,
        confidence_threshold,
        score,
        evidence_threshold,
        run.taxonomy_confidence if run else None,
        item.retention_disposition,
        run.id if run else None,
        selected_start,
        selected_eligible,
        legacy_eligible_at,
        cooling_start,
        evidence_eligible_at,
        item.kind,
        allowed_kind,
        conflict_recheck_status,
    )


# --- Admission assessment capture (issue #159) --------------------------------
#
# Everything below observes and records the decision the evaluator above
# already made. None of it changes a threshold, a lane rule, a cooling period
# or a blocker: with ENGRAM_ADMISSION_ASSESSMENT_CAPTURE_ENABLED=false (the
# default) not one line of it runs, and current promotion behavior and audit
# JSON are byte-for-byte unchanged.


def _lane_qualification(
    item: MemoryItem,
    support: PromotionSupport,
    *,
    confidence_threshold: float,
    min_age_hours: int,
    evidence_enabled: bool,
    evidence_threshold: float,
    now: datetime,
) -> LaneQualification:
    """Recompute both lanes' trust/age qualification from the same pure policy.

    ``cooling`` may never be inferred from an ``age`` blocker alone, so the
    assessment has to persist enough lane facts to prove a lane would
    otherwise qualify. These are recomputed here through exactly the helpers
    ``assess_promotion_candidate`` itself uses — no second interpretation of
    the policy, just the lane detail the candidate does not carry.
    """
    evidence_blockers, score, cooling_start = _evidence_state(item, support.classification_run)
    if not evidence_enabled:
        evidence_blockers.append(BLOCK_EVIDENCE_DISABLED)
    evidence_trust = not evidence_blockers and score is not None and score >= evidence_threshold
    return LaneQualification(
        legacy_trust_qualified=item.memory_confidence >= confidence_threshold,
        legacy_age_qualified=item.created_at + timedelta(hours=min_age_hours) <= now,
        evidence_trust_qualified=evidence_trust,
        evidence_age_qualified=(
            cooling_start is not None and cooling_start + timedelta(hours=min_age_hours) <= now
        ),
    )


def _admission_timing(
    item: MemoryItem, candidate: PromotionCandidate, lanes: LaneQualification
) -> tuple[datetime | None, datetime | None, datetime | None]:
    """``(cooling_period_start, eligible_at, next_evaluation_at)`` for one decision.

    With a lane selected, the candidate's own clock is authoritative and there
    is nothing to wait for. With no lane selected but a lane trust-qualified
    and merely waiting on its observation boundary, the earliest such boundary
    is both the eligibility time and the due time — that is exactly what makes
    the outcome ``cooling`` rather than ``insufficient_evidence``. Otherwise
    no deterministic due time exists and none is invented.
    """
    if candidate.selected_basis is not None:
        return candidate.cooling_period_start, candidate.eligible_at, None
    waiting: list[tuple[datetime, datetime]] = []
    if lanes.legacy_trust_qualified and not lanes.legacy_age_qualified:
        waiting.append((item.created_at, candidate.legacy_eligible_at))
    if (
        lanes.evidence_trust_qualified
        and not lanes.evidence_age_qualified
        and candidate.evidence_cooling_period_start is not None
        and candidate.evidence_eligible_at is not None
    ):
        waiting.append(
            (candidate.evidence_cooling_period_start, candidate.evidence_eligible_at)
        )
    if not waiting:
        return None, None, None
    start, eligible = min(waiting, key=lambda pair: pair[1])
    return start, eligible, eligible


def _admission_decision_inputs(
    item: MemoryItem,
    candidate: PromotionCandidate,
    support: PromotionSupport,
    lanes: LaneQualification,
    *,
    min_age_hours: int,
    evidence_enabled: bool,
) -> dict[str, Any]:
    """Only safe normalized values current Path A actually used.

    No memory content, no transcript, no extraction spans, no provider output
    text, no credentials, no unrestricted conflict candidate identity, no
    human-evaluation labels. Every field here is something the evaluator read
    to reach this decision, so a reviewer can see why without re-running it.
    """
    run = support.classification_run
    return {
        "kind": item.kind,
        "kind_auto_promote_allowed": candidate.kind_auto_promote_allowed,
        "review_status": item.review_status,
        "source_type": item.source_type,
        "source_trust": item.source_trust,
        "authority": item.authority,
        "sensitivity": item.sensitivity,
        "human_verified": item.human_verified,
        "min_age_hours": min_age_hours,
        "memory_confidence": candidate.legacy_confidence,
        "legacy_threshold": candidate.legacy_threshold,
        "legacy_trust_qualified": lanes.legacy_trust_qualified,
        "legacy_age_qualified": lanes.legacy_age_qualified,
        "legacy_eligible_at": candidate.legacy_eligible_at.isoformat(),
        "evidence_enabled": evidence_enabled,
        "evidence_threshold": candidate.evidence_threshold,
        "evidence_score": candidate.evidence_score,
        "evidence_trust_qualified": lanes.evidence_trust_qualified,
        "evidence_age_qualified": lanes.evidence_age_qualified,
        "evidence_cooling_period_start": (
            candidate.evidence_cooling_period_start.isoformat()
            if candidate.evidence_cooling_period_start
            else None
        ),
        "evidence_eligible_at": (
            candidate.evidence_eligible_at.isoformat()
            if candidate.evidence_eligible_at
            else None
        ),
        "source_confidence_prior": item.source_confidence_prior,
        "retention_confidence": item.retention_confidence,
        "retention_disposition": candidate.retention_disposition,
        "taxonomy_confidence": candidate.taxonomy_confidence,
        "classification_version": run.classification_version if run else None,
        "retention_policy_version": run.retention_policy_version if run else None,
        "conflict_resolution_status": item.conflict_resolution_status,
        "external_dispute": support.has_external_dispute,
        "external_noise_feedback": support.has_external_noise_feedback,
    }


@dataclass(frozen=True)
class _AdmissionCaptureContext:
    """Invocation provenance for one pass's assessments.

    The canonical ``promotion.evaluate`` handler supplies real evaluation/job/
    trigger provenance through ``evaluation_context`` (issue #155). Legacy
    compatibility callers have none, and are labelled ``legacy_caller`` rather
    than being dressed up as a canonical trigger they never had.
    """

    enabled: bool
    invocation_source: str
    trigger_type: str
    trigger_id: str
    evaluation_id: uuid.UUID | None
    job_id: uuid.UUID | None

    @classmethod
    def build(
        cls, *, source: str, evaluation_context: dict[str, object] | None, enabled: bool
    ) -> _AdmissionCaptureContext:
        context = evaluation_context or {}
        return cls(
            enabled=enabled,
            invocation_source=source,
            trigger_type=str(context.get("trigger_type") or "legacy_caller"),
            trigger_id=str(context.get("trigger_id") or source),
            evaluation_id=_optional_uuid(context.get("evaluation_id"), field="evaluation_id"),
            job_id=_optional_uuid(context.get("job_id"), field="job_id"),
        )


async def _capture_admission_assessment(
    session: AsyncSession,
    item: MemoryItem,
    candidate: PromotionCandidate,
    snapshot: _EvaluatedItemState,
    lanes: LaneQualification,
    capture: _AdmissionCaptureContext,
    *,
    mode: str,
    mutated: bool,
    moment: datetime,
    policy_config: dict[str, Any],
    conflict_recheck_status: str,
    resulting_state: dict[str, Any] | None = None,
    live_proposal: bool = True,
    policy_changed: bool = False,
    race_lost: bool = False,
    claim_evaluation_id: bool = True,
    actor_principal_id: uuid.UUID | None = None,
    linked_item_event_id: uuid.UUID | None = None,
    assessment_id: uuid.UUID | None = None,
) -> Any:
    """Persist one decision and move the current projection, in this transaction.

    Never commits and never rolls back: the caller owns the transaction, which
    is what makes the mutation, its audit event, this assessment and the
    projection commit atomically or fail closed together.
    """
    decision = _build_admission_decision(
        item,
        candidate,
        snapshot,
        lanes,
        mode=mode,
        mutated=mutated,
        live_proposal=live_proposal,
        policy_config=policy_config,
        conflict_recheck_status=conflict_recheck_status,
        resulting_state=resulting_state,
        policy_changed=policy_changed,
        race_lost=race_lost,
    )
    row = await insert_assessment(
        session,
        decision,
        trigger_type=capture.trigger_type,
        trigger_id=capture.trigger_id,
        invocation_source=capture.invocation_source,
        evaluated_at=moment,
        # A retried canonical evaluation carries the same evaluation_id, and
        # insert_assessment resolves it back to the decision already bound to
        # that identity instead of appending a second one. Only the first
        # item of a pass may claim it; a multi-item sweep records the rest
        # without one rather than colliding on a shared identity.
        #
        # `claim_evaluation_id=False` marks a row that is *not* the outcome of
        # the canonical execution — a superseded pre-lock result recorded
        # alongside the reevaluation that replaces it. Binding the execution
        # identity there would point the canonical evaluation at the wrong
        # historical row and leave the authoritative decision unaddressable.
        evaluation_id=capture.evaluation_id if claim_evaluation_id else None,
        job_id=capture.job_id,
        actor_principal_id=actor_principal_id,
        classification_run_id=candidate.classification_run_id,
        evidence_refs=await evidence_assessment_refs(
            session, tenant_id=decision.tenant_id, memory_item_id=decision.memory_item_id
        ),
        linked_item_event_id=linked_item_event_id,
        assessment_id=assessment_id,
    )
    await project_current(session, row)
    return row


@dataclass(frozen=True)
class _EvaluatedItemState:
    """The exact pre-mutation state one decision was made against.

    Taken before any lifecycle mutation in this pass, and never re-read from
    the ORM object afterwards. That ordering is the whole point: the guarded
    ``proposed -> active`` UPDATE and the conflict-recheck marking both
    synchronize back onto the live object, so a decision built from that
    object after the fact would record state its own mutation produced — an
    admitted decision claiming it evaluated an already-active item.
    """

    tenant_id: uuid.UUID
    memory_item_id: uuid.UUID
    content_hash: str
    input_state: dict[str, Any]
    decision_inputs: dict[str, Any]

    def promoted_state(self) -> dict[str, Any]:
        """The state a successful ``proposed -> active`` admission produces."""
        return {**self.input_state, "review_status": "active"}

    def conflict_marked_state(self, conflicting_item_id: uuid.UUID) -> dict[str, Any]:
        """The state a promotion-time conflict block produces.

        The recheck writes ``conflict_resolution_status`` and
        ``conflicts_with_item_id``, and both participate in the evaluated
        input, so without this the blocked decision would be stale against the
        item the instant its own transaction committed.
        """
        return {
            **self.input_state,
            "conflict_resolution_status": "unresolved",
            "conflicts_with_item_id": str(conflicting_item_id),
        }


def _snapshot_evaluated_state(
    item: MemoryItem,
    candidate: PromotionCandidate,
    support: PromotionSupport,
    lanes: LaneQualification,
    *,
    min_age_hours: int,
    evidence_enabled: bool,
) -> _EvaluatedItemState:
    """Freeze everything the decision record needs, before anything mutates."""
    return _EvaluatedItemState(
        tenant_id=item.tenant_id,
        memory_item_id=item.id,
        content_hash=item.content_hash,
        input_state=input_state_payload(item, support.classification_run),
        decision_inputs=_admission_decision_inputs(
            item,
            candidate,
            support,
            lanes,
            min_age_hours=min_age_hours,
            evidence_enabled=evidence_enabled,
        ),
    )


def _build_admission_decision(
    item: MemoryItem,
    candidate: PromotionCandidate,
    snapshot: _EvaluatedItemState,
    lanes: LaneQualification,
    *,
    mode: str,
    mutated: bool,
    policy_config: dict[str, Any],
    conflict_recheck_status: str,
    resulting_state: dict[str, Any] | None = None,
    live_proposal: bool = True,
    policy_changed: bool = False,
    race_lost: bool = False,
) -> AdmissionDecision:
    """Assemble the canonical decision from an evaluation already performed.

    Pure and database-free by design: every value it reads was produced by the
    production evaluator or frozen into ``snapshot`` before any mutation, so
    this can be called while the evaluating transaction is still open (the
    authoritative path) or captured in memory and persisted after that
    transaction has been rolled back (the dry-run preview path), with
    identical results.

    ``item`` is used only for the eligibility clock, which no mutation in this
    pass touches; every hashed value comes from ``snapshot``.
    """
    cooling_start, eligible_at, next_evaluation_at = _admission_timing(item, candidate, lanes)
    return build_decision(
        tenant_id=snapshot.tenant_id,
        memory_item_id=snapshot.memory_item_id,
        item_content_hash=snapshot.content_hash,
        input_state=snapshot.input_state,
        resulting_state=resulting_state,
        mode=cast(Any, mode),
        mutated=mutated,
        live_proposal=live_proposal,
        blockers=candidate.blockers,
        selected_basis=candidate.selected_basis,
        lanes=lanes,
        decision_inputs=snapshot.decision_inputs,
        policy_config=policy_config,
        conflict_recheck_status=conflict_recheck_status,
        cooling_period_start=cooling_start,
        eligible_at=eligible_at,
        next_evaluation_at=next_evaluation_at,
        policy_changed=policy_changed,
        race_lost=race_lost,
    )


async def _persist_shadow_decisions(
    session: AsyncSession,
    pending: list[tuple[AdmissionDecision, uuid.UUID | None]],
    capture: _AdmissionCaptureContext,
    *,
    moment: datetime,
) -> None:
    """Record dry-run/preview decisions after the evaluating pass rolled back.

    Running in its own transaction is the guarantee, not a convenience: the
    preview pass has already discarded everything it touched, so a shadow row
    written here provably cannot carry a lifecycle mutation with it. Shadow
    rows never become current — :func:`project_current` refuses them, and the
    projection table's own CHECK constraint refuses them again.
    """
    if not capture.enabled or not pending:
        return
    for decision, classification_run_id in pending:
        await insert_assessment(
            session,
            decision,
            trigger_type=capture.trigger_type,
            trigger_id=capture.trigger_id,
            invocation_source=capture.invocation_source,
            evaluated_at=moment,
            evaluation_id=capture.evaluation_id,
            job_id=capture.job_id,
            classification_run_id=classification_run_id,
            evidence_refs=await evidence_assessment_refs(
                session,
                tenant_id=decision.tenant_id,
                memory_item_id=decision.memory_item_id,
            ),
        )
        capture = replace(capture, evaluation_id=None)
    await session.commit()


def _audit(
    item: MemoryItem,
    candidate: PromotionCandidate,
    source: str,
    now: datetime,
    min_age_hours: int,
    *,
    evaluation_context: dict[str, object] | None = None,
) -> str:
    basis = candidate.selected_basis
    assert basis is not None
    reason: dict[str, object] = {
        "operation": "auto-promotion",
        "invocation_source": source,
        "basis": basis,
        "promotion_policy_version": EVIDENCE_PROMOTION_POLICY_VERSION
        if basis == "retention_evidence"
        else LEGACY_PROMOTION_POLICY_VERSION,
        "min_age_hours": min_age_hours,
        "cooling_period_start": candidate.cooling_period_start.isoformat()
        if candidate.cooling_period_start
        else None,
        "eligible_at": candidate.eligible_at.isoformat() if candidate.eligible_at else None,
        "promoted_at": now.isoformat(),
        "kind": item.kind,
        "kind_auto_promote_allowed": True,
        "conflict_status": item.conflict_resolution_status,
        "external_dispute": False,
        "external_noise_feedback": False,
        "conflict_recheck": "clear",
        "source_type": item.source_type,
        "source_trust": item.source_trust,
        "authority": item.authority,
        "human_verified": item.human_verified,
    }
    if basis == "legacy_confidence":
        reason.update(
            memory_confidence=item.memory_confidence,
            legacy_confidence_threshold=candidate.legacy_threshold,
        )
    else:
        reason.update(
            classification_run_id=str(candidate.classification_run_id),
            classification_version="classification-v2",
            retention_policy_version="retention-v1",
            source_confidence_prior=item.source_confidence_prior,
            retention_confidence=item.retention_confidence,
            retention_disposition=item.retention_disposition,
            taxonomy_confidence=candidate.taxonomy_confidence,
            evidence_score=candidate.evidence_score,
            evidence_threshold=candidate.evidence_threshold,
            evidence_score_ceiling=EVIDENCE_SCORE_CEILING,
            evidence_weights={
                "source_confidence_prior": EVIDENCE_SOURCE_PRIOR_WEIGHT,
                "retention_confidence": EVIDENCE_RETENTION_WEIGHT,
            },
        )
    # Optional invocation/audit context (evaluation_id, job_id, contract
    # version, trigger provenance): populated only by the canonical
    # promotion.evaluate handler (issue #155). Legacy callers pass None, so
    # their existing audit-event JSON shape is byte-for-byte unchanged.
    if evaluation_context is not None:
        reason.update(evaluation_context)
    return json.dumps(reason, sort_keys=True)


def _kind_promotion_allowed() -> Exists:
    """Correlated EXISTS mirroring the mutation UPDATE's kind guard.

    A live proposal whose kind has no enabled ``auto_promote_from_inferred``
    row in the tenant's registry can never be admitted under current policy,
    so the rotating window never spends scan budget on it. A kind-policy
    change immediately makes affected rows eligible for future rotation
    windows again; it does not guarantee they are reached by the very next
    pass — a row whose ``(created_at, id)`` is behind the persisted cursor
    still waits until the rotation wraps around to it.
    """
    return exists(
        select(MemoryKind.name).where(
            MemoryKind.tenant_id == MemoryItem.tenant_id,
            MemoryKind.name == MemoryItem.kind,
            MemoryKind.enabled.is_(True),
            MemoryKind.auto_promote_from_inferred.is_(True),
        )
    )


def _rotation_window(
    base_stmt: Select[tuple[MemoryItem]],
    cursor: PromotionReconciliationState | None,
    limit: int,
) -> Select[tuple[MemoryItem]]:
    """Bound one fair rotation window over live proposals (issue #155, first slice).

    Applies the kind-policy exclusion, the strictly-after keyset predicate on
    the persisted cursor (``cursor=None`` reads from the head of the queue —
    the wrapped continuation reuses the same call), a deterministic
    ``(created_at, id)`` order, the per-pass row limit, and FOR UPDATE SKIP
    LOCKED so concurrent startup passes partition the window instead of
    blocking each other, without raising the per-pass bound or loading the
    whole backlog.

    Eventual-evaluation bound: from an arbitrary persisted cursor position, a
    rotation can begin partway through the live, kind-eligible set — a short
    partial page to the tail, then wraparound pages covering the rest. So a
    stable set of ``N`` live kind-eligible proposals is fully covered within
    at most ``ceil(N / limit) + 1`` passes (one for the possible partial tail
    page, plus a full rotation of the remainder), not the tighter
    ``ceil(N / limit)`` a cursor starting at the head would give. This bound
    holds only while the eligible set is stable during the rotation;
    concurrent inserts or kind-policy changes can change the set mid-rotation
    and are not counted against it.
    """
    stmt = base_stmt.where(_kind_promotion_allowed())
    if cursor is not None:
        stmt = stmt.where(
            or_(
                MemoryItem.created_at > cursor.cursor_created_at,
                and_(
                    MemoryItem.created_at == cursor.cursor_created_at,
                    MemoryItem.id > cursor.cursor_item_id,
                ),
            )
        )
    return (
        stmt.order_by(MemoryItem.created_at.asc(), MemoryItem.id.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )


async def _advance_reconciliation_cursor(
    session: AsyncSession, tenant_id: str, item: MemoryItem, moment: datetime
) -> None:
    """Persist the rotation cursor to the last row this pass examined.

    Writes are last-writer-wins: whichever pass commits last sets the
    persisted position, so a slower concurrent pass can move the cursor
    backwards relative to a faster one that started later. FOR UPDATE SKIP
    LOCKED means two concurrent passes never examine the same row, so
    neither pass's own write can regress past rows *it* already examined.
    This first slice does not prove a general "never skips a row" guarantee
    across arbitrary concurrent startup/admin/review/worker interleavings —
    that full concurrency proof is deferred to later #155 work. What is
    established here: any row a pass reads is re-reachable by a later
    rotation (nothing is permanently excluded), because a backwards cursor
    move only widens, never narrows, the next pass's keyset window.
    """
    values = {
        "cursor_created_at": item.created_at,
        "cursor_item_id": item.id,
        "updated_at": moment,
    }
    await session.execute(
        pg_insert(PromotionReconciliationState)
        .values(tenant_id=uuid.UUID(tenant_id), **values)
        .on_conflict_do_update(
            index_elements=[PromotionReconciliationState.tenant_id], set_=values
        )
    )


async def _mark_promotion_conflict(
    session: AsyncSession,
    item: MemoryItem,
    candidate: PromotionCandidate,
    conflict: PromotionConflictCheck,
    *,
    tenant_id: str,
    source: str,
    event_provenance: dict[str, Any],
    evaluation_context: dict[str, object] | None,
) -> bool:
    """Mark a promotion-time conflict block and write its audit event.

    Returns whether the guarded marking actually applied; it does not when
    the row left the live-proposal state under a concurrent writer.

    Extracted verbatim from the promotion loop so the admission-capture
    path can run it *before* recording its decision — the marking writes
    conflict metadata that is itself part of the evaluated input — without
    forking a second implementation. The emitted audit JSON is unchanged.
    """
    actor = await resolve_trusted_system_actor(session, tenant_id)
    marked = await session.execute(
        update(MemoryItem)
        .where(
            MemoryItem.id == item.id,
            MemoryItem.review_status == "proposed",
            MemoryItem.valid_to.is_(None),
            MemoryItem.superseded_by.is_(None),
        )
        .values(
            conflict_resolution_status="unresolved",
            conflicts_with_item_id=conflict.conflicting_item_id,
        )
        .returning(MemoryItem.id)
    )
    if marked.scalar_one_or_none() is not None:
        session.add(
            ItemEvent(
                item_id=item.id,
                **event_provenance,
                event_type="conflict_resolution",
                field_name="conflict_resolution_status",
                old_value=item.conflict_resolution_status,
                new_value="unresolved",
                actor_principal_id=actor,
                reason=json.dumps(
                    {
                        "operation": "auto-promotion",
                        "invocation_source": source,
                        "selected_basis": candidate.selected_basis,
                        "promotion_policy_version": (
                            EVIDENCE_PROMOTION_POLICY_VERSION
                            if candidate.selected_basis == "retention_evidence"
                            else LEGACY_PROMOTION_POLICY_VERSION
                        ),
                        "conflict_recheck": "blocked",
                        "conflicting_item_id": str(conflict.conflicting_item_id),
                        "conflict_verdict": conflict.verdict,
                        "conflict_reason": conflict.reason,
                        "conflict_detection_mode": (
                            "embedding"
                            if conflict.used_embeddings
                            else "heuristic_fallback"
                        ),
                        "source_item_id": str(item.id),
                        "kind": item.kind,
                        "source_type": item.source_type,
                        "classification_run_id": (
                            str(candidate.classification_run_id)
                            if candidate.classification_run_id
                            else None
                        ),
                        "evidence_score": candidate.evidence_score,
                        "legacy_confidence": candidate.legacy_confidence,
                        "evidence_threshold": candidate.evidence_threshold,
                        "legacy_threshold": candidate.legacy_threshold,
                        **(evaluation_context or {}),
                    },
                    sort_keys=True,
                ),
            )
        )
        return True
    return False


async def auto_promote_proposed_memories(
    session: AsyncSession,
    tenant_id: str | None = None,
    *,
    now: datetime | None = None,
    limit: int | None = None,
    source: str = "cli",
    dry_run: bool = False,
    item_id: uuid.UUID | None = None,
    classification_run_id: uuid.UUID | None = None,
    memory_context: ResolvedMemoryContext | None = None,
    rotation: bool = False,
    evaluation_context: dict[str, object] | None = None,
    selected_window_observer: PromotionWindowObserver | None = None,
) -> PromotionResult:
    """Evaluate live proposals for Path A promotion.

    ``rotation=True`` selects the scan window fairly instead of oldest-first:
    the per-tenant reconciliation cursor records the last row examined, each
    pass reads the next ``limit`` rows strictly after it (wrapping to the head
    when the page is empty), and kind-terminal rows are excluded outright.
    This keeps a bounded pass from re-selecting the same permanently blocked
    rows indefinitely (issue #155) without raising the limit or loading the
    full backlog. Only the enabled, non-preview, untargeted, bounded case
    rotates — targeted jobs, CLI/admin/worker sweeps, and dry-run previews
    keep their existing selection semantics.

    ``evaluation_context``, when provided, is merged into state-changing
    audit-event reasons only (successful promotions and conflict-recheck
    blocks) — issue #155's canonical ``promotion.evaluate`` handler is the
    only caller that passes it, carrying ``evaluation_id``, ``job_id``,
    ``job_contract_version``, and trigger provenance. ``None`` (the default,
    used by every legacy caller) leaves the existing audit-event JSON shape
    byte-for-byte unchanged.
    """
    moment = now or datetime.now(UTC)
    if tenant_id is None:
        tenant_id = (
            await session.execute(text("SELECT current_setting('app.tenant_id', true)::text"))
        ).scalar_one_or_none()
        if not tenant_id:
            result = PromotionResult(
                "", False, _FALLBACK_CONFIDENCE_THRESHOLD, _FALLBACK_MIN_AGE_HOURS
            )
            if dry_run:
                await session.rollback()
            return result
    config = await _config(session, str(tenant_id))
    enabled, threshold, min_age, evidence_enabled, evidence_threshold = _config_values(config)
    result = PromotionResult(
        str(tenant_id), enabled, threshold, min_age, evidence_enabled, evidence_threshold, dry_run
    )
    base_stmt = select(MemoryItem).where(
        MemoryItem.tenant_id == tenant_id,
        MemoryItem.review_status == "proposed",
        MemoryItem.valid_to.is_(None),
    )
    if memory_context is not None and memory_context.is_profile_bound:
        base_stmt = base_stmt.where(write_eligibility_expression(memory_context))
    event_provenance = (
        context_provenance(memory_context)
        if memory_context is not None
        else {
            "tenant_id": uuid.UUID(str(tenant_id)),
            "memory_context_version": INTERNAL_MEMORY_CONTEXT_VERSION,
        }
    )
    is_postgres = session.bind is not None and session.bind.dialect.name == "postgresql"
    # Fair rotation applies only to the enabled, non-preview, untargeted,
    # bounded pass — i.e. the lazy startup-recall sweep whose callers pass
    # rotation=True. Targeted jobs, CLI/admin/worker sweeps, and dry-run
    # previews keep their existing selection semantics.
    rotation_active = (
        rotation
        and enabled
        and not dry_run
        and item_id is None
        and limit is not None
        and is_postgres
    )
    cursor_state: PromotionReconciliationState | None = None
    if rotation_active:
        cursor_state = (
            await session.execute(
                select(PromotionReconciliationState).where(
                    PromotionReconciliationState.tenant_id == uuid.UUID(str(tenant_id))
                )
            )
        ).scalar_one_or_none()
    # The ordinary targeted/untargeted selection shape is established first,
    # independent of whether promotion is enabled, so a disabled tenant's
    # targeted call still scans exactly the requested item and an untargeted
    # call still respects the caller's existing ordering/limit — neither
    # falls back to a tenant-wide scan. Rotation only ever replaces the
    # untargeted shape, and only for the actual active rotation case
    # (which itself requires enabled=True, so it can never apply here).
    if item_id is not None:
        stmt = base_stmt.where(MemoryItem.id == item_id)
    elif rotation_active:
        assert limit is not None
        stmt = _rotation_window(base_stmt, cursor_state, limit)
    else:
        stmt = base_stmt.order_by(MemoryItem.created_at.asc())
        if limit is not None:
            stmt = stmt.limit(limit)
    if not enabled:
        items = list((await session.execute(stmt)).scalars())
        result.scanned = len(items)
        result.skipped_disabled = len(items)
        if dry_run:
            await session.rollback()
        else:
            await session.commit()
        return result
    if is_postgres and not rotation_active:
        # Rotation's own locking (FOR UPDATE SKIP LOCKED) is already baked
        # into the statement _rotation_window() built above.
        stmt = stmt.with_for_update(skip_locked=item_id is None)
    items = list((await session.execute(stmt)).scalars())
    if rotation_active and not items and cursor_state is not None:
        # The keyset page after the cursor is empty: the rotation reached the
        # tail of the live proposed set and wraps to the oldest rows still
        # eligible under current kind policy, restarting coverage at the head.
        result.rotation_wrapped = True
        assert limit is not None
        items = list(
            (await session.execute(_rotation_window(base_stmt, None, limit))).scalars()
        )
    capture = _AdmissionCaptureContext.build(
        source=source,
        evaluation_context=evaluation_context,
        enabled=settings.admission_assessment_capture_enabled,
    )
    policy_changed_under_lock = False
    # Dry-run/preview decisions are held in memory and written only after the
    # pass has rolled its own transaction back, so a shadow assessment can
    # never ride along with an accidental state mutation.
    shadow_decisions: list[tuple[AdmissionDecision, uuid.UUID | None]] = []
    if capture.enabled and items and not dry_run:
        # Step 4 of the #159 transaction: revalidate policy/config against the
        # state we now hold the item lock on. ``config`` above was read before
        # the lock, so a tenant_config change committed in between would make
        # the pre-lock policy — and any decision derived from it — stale. When
        # that happens the pre-lock result is recorded as immutable ``stale``
        # history (it can neither become current nor authorize a mutation) and
        # the pass continues on the reloaded policy, which is the current-state
        # reevaluation the mutation must be based on.
        if config is not None:
            session.expire(config)
        relocked = await _config(session, str(tenant_id))
        (
            reloaded_enabled,
            reloaded_threshold,
            reloaded_min_age,
            reloaded_evidence_enabled,
            reloaded_evidence_threshold,
        ) = _config_values(relocked)
        # Only the values the policy digest actually covers can make a
        # pre-lock decision stale. ``auto_promote_enabled`` is not one of
        # them — it gates whether the pass runs at all, not what policy would
        # decide — so it is handled separately below rather than producing a
        # "stale" row whose digests match the new policy exactly.
        if (
            reloaded_threshold,
            reloaded_min_age,
            reloaded_evidence_enabled,
            reloaded_evidence_threshold,
        ) != (threshold, min_age, evidence_enabled, evidence_threshold):
            policy_changed_under_lock = True
            stale_threshold, stale_min_age = threshold, min_age
            stale_evidence_enabled, stale_evidence_threshold = (
                evidence_enabled,
                evidence_threshold,
            )
            threshold, min_age = reloaded_threshold, reloaded_min_age
            evidence_enabled = reloaded_evidence_enabled
            evidence_threshold = reloaded_evidence_threshold
            result.confidence_threshold = threshold
            result.min_age_hours = min_age
            result.evidence_enabled = evidence_enabled
            result.evidence_threshold = evidence_threshold
        if not reloaded_enabled:
            # The tenant switched auto-promotion off between the pre-lock read
            # and the lock. Fail closed: the rest of this pass records
            # decisions but promotes nothing, rather than committing a
            # mutation under a policy the tenant has just withdrawn.
            enabled = False
            result.enabled = False

    # The compatibility observer must see the exact window selected by the
    # authoritative locked query.  In particular, it cannot reconstruct this
    # window from the cursor: another transaction may have made SKIP LOCKED
    # advance this pass into later rows.  Invoke the caller's diagnostic hook
    # before any compatibility mutation, in this same transaction.
    if selected_window_observer is not None:
        await selected_window_observer(
            PromotionObservedWindow(tuple(items), result.rotation_wrapped)
        )
    result.scanned = len(items)
    support_map = await load_promotion_support(session, items)
    for item in items:
        if classification_run_id is not None:
            bound_run = support_map[item.id].classification_run
            if bound_run is None or bound_run.id != classification_run_id:
                continue
        if item.superseded_by is not None:
            continue
        candidate = assess_promotion_candidate(
            item,
            support_map[item.id],
            confidence_threshold=threshold,
            min_age_hours=min_age,
            evidence_enabled=evidence_enabled,
            evidence_threshold=evidence_threshold,
            now=moment,
        )
        lanes = (
            _lane_qualification(
                item,
                support_map[item.id],
                confidence_threshold=threshold,
                min_age_hours=min_age,
                evidence_enabled=evidence_enabled,
                evidence_threshold=evidence_threshold,
                now=moment,
            )
            if capture.enabled
            else None
        )
        policy_config = (
            policy_config_payload(
                confidence_threshold=threshold,
                min_age_hours=min_age,
                evidence_enabled=evidence_enabled,
                evidence_threshold=evidence_threshold,
                kind_auto_promote_allowed=candidate.kind_auto_promote_allowed,
            )
            if capture.enabled
            else {}
        )
        # Freeze the evaluated state here — before the guarded promotion UPDATE
        # or the conflict-recheck marking below, either of which synchronizes
        # back onto `item` and would otherwise be hashed as though it were the
        # input policy read.
        snapshot = (
            _snapshot_evaluated_state(
                item,
                candidate,
                support_map[item.id],
                lanes,
                min_age_hours=min_age,
                evidence_enabled=evidence_enabled,
            )
            if capture.enabled and lanes is not None
            else None
        )
        if capture.enabled and policy_changed_under_lock:
            # Truthful, non-current history for the superseded pre-lock policy.
            # It is deliberately built from the pre-change configuration: this
            # row records what the evaluation *was* about to conclude, and its
            # own digests are what make it resolvable as stale later.
            stale_candidate = assess_promotion_candidate(
                item,
                support_map[item.id],
                confidence_threshold=stale_threshold,
                min_age_hours=stale_min_age,
                evidence_enabled=stale_evidence_enabled,
                evidence_threshold=stale_evidence_threshold,
                now=moment,
            )
            stale_lanes = _lane_qualification(
                item,
                support_map[item.id],
                confidence_threshold=stale_threshold,
                min_age_hours=stale_min_age,
                evidence_enabled=stale_evidence_enabled,
                evidence_threshold=stale_evidence_threshold,
                now=moment,
            )
            await _capture_admission_assessment(
                session,
                item,
                stale_candidate,
                _snapshot_evaluated_state(
                    item,
                    stale_candidate,
                    support_map[item.id],
                    stale_lanes,
                    min_age_hours=stale_min_age,
                    evidence_enabled=stale_evidence_enabled,
                ),
                stale_lanes,
                capture,
                mode="authoritative",
                mutated=False,
                moment=moment,
                # This row is history for a policy that no longer applies; the
                # reevaluation below is what this execution actually decided,
                # so that is what the evaluation identity must resolve to.
                claim_evaluation_id=False,
                policy_config=policy_config_payload(
                    confidence_threshold=stale_threshold,
                    min_age_hours=stale_min_age,
                    evidence_enabled=stale_evidence_enabled,
                    evidence_threshold=stale_evidence_threshold,
                    kind_auto_promote_allowed=stale_candidate.kind_auto_promote_allowed,
                ),
                conflict_recheck_status="not_run",
                policy_changed=True,
            )
            # Deliberately no `replace(capture, evaluation_id=None)` here: the
            # identity is still unclaimed and belongs to the reevaluation this
            # pass is about to record for the same item.
        conflict: PromotionConflictCheck | None = None
        if candidate.would_promote:
            conflict = await check_promotion_conflict(
                session, item, memory_context=memory_context
            )
            candidate.conflict_recheck_status = "blocked" if conflict else "clear"
            if conflict is not None:
                candidate.blockers.append(BLOCK_RECHECK)
                candidate.would_promote = False
        result.candidates.append(candidate)
        _count_blockers(result, candidate.blockers)
        if not candidate.would_promote:
            # A blocking conflict recheck writes conflict metadata that is
            # itself part of the evaluated input. The marking therefore runs
            # before the decision is recorded, so the decision can declare the
            # state its own recheck produced instead of being stale against it
            # the moment the transaction commits.
            conflict_marked = False
            if capture.enabled and not dry_run:
                assert lanes is not None and snapshot is not None
                if conflict is not None:
                    conflict_marked = await _mark_promotion_conflict(
                        session,
                        item,
                        candidate,
                        conflict,
                        tenant_id=str(tenant_id),
                        source=source,
                        event_provenance=event_provenance,
                        evaluation_context=evaluation_context,
                    )
                await _capture_admission_assessment(
                    session,
                    item,
                    candidate,
                    snapshot,
                    lanes,
                    capture,
                    mode="authoritative",
                    mutated=False,
                    moment=moment,
                    policy_config=policy_config,
                    conflict_recheck_status=candidate.conflict_recheck_status,
                    resulting_state=(
                        snapshot.conflict_marked_state(conflict.conflicting_item_id)
                        if conflict_marked and conflict is not None
                        else None
                    ),
                )
                capture = replace(capture, evaluation_id=None)
                continue
            elif capture.enabled and dry_run:
                assert lanes is not None and snapshot is not None
                shadow_decisions.append(
                    (
                        _build_admission_decision(
                            item,
                            candidate,
                            snapshot,
                            lanes,
                            mode="shadow",
                            mutated=False,
                            policy_config=policy_config,
                            # A preview never runs the promotion-time semantic
                            # conflict recheck, and says exactly that rather
                            # than borrowing the ordinary "not_run" an
                            # authoritative pass uses before its own recheck.
                            conflict_recheck_status="not_run_preview",
                        ),
                        candidate.classification_run_id,
                    )
                )
            if conflict and not dry_run:
                await _mark_promotion_conflict(
                    session,
                    item,
                    candidate,
                    conflict,
                    tenant_id=str(tenant_id),
                    source=source,
                    event_provenance=event_provenance,
                    evaluation_context=evaluation_context,
                )
            continue
        result.would_promote += 1
        result.would_promote_ids.append(item.id)
        if candidate.selected_basis == "retention_evidence":
            result.would_promote_retention_evidence += 1
        else:
            result.would_promote_legacy_confidence += 1
        if not enabled:
            # Auto-promotion was withdrawn under the lock (see the post-lock
            # revalidation above). Current policy would have admitted this
            # item, but the authority to act on that was taken away
            # mid-evaluation, so the result cannot authorize a mutation and
            # cannot become current: that is exactly `stale`, and it asks an
            # operator to reconcile policy rather than silently doing nothing.
            result.skipped_disabled += 1
            if capture.enabled and not dry_run:
                assert lanes is not None and snapshot is not None
                await _capture_admission_assessment(
                    session,
                    item,
                    candidate,
                    snapshot,
                    lanes,
                    capture,
                    mode="authoritative",
                    mutated=False,
                    moment=moment,
                    policy_config=policy_config,
                    conflict_recheck_status=candidate.conflict_recheck_status,
                    policy_changed=True,
                )
                capture = replace(capture, evaluation_id=None)
            continue
        if dry_run:
            if capture.enabled:
                assert lanes is not None and snapshot is not None
                shadow_decisions.append(
                    (
                        _build_admission_decision(
                            item,
                            candidate,
                            snapshot,
                            lanes,
                            mode="shadow",
                            mutated=False,
                            policy_config=policy_config,
                            # A preview never runs the promotion-time semantic
                            # conflict recheck, and says exactly that rather
                            # than borrowing the ordinary "not_run" an
                            # authoritative pass uses before its own recheck.
                            conflict_recheck_status="not_run_preview",
                        ),
                        candidate.classification_run_id,
                    )
                )
            continue
        kind_allowed = exists(
            select(MemoryKind.name).where(
                MemoryKind.tenant_id == tenant_id,
                MemoryKind.name == item.kind,
                MemoryKind.enabled.is_(True),
                MemoryKind.auto_promote_from_inferred.is_(True),
            )
        )
        changed = await session.execute(
            update(MemoryItem)
            .where(
                MemoryItem.id == item.id,
                MemoryItem.tenant_id == tenant_id,
                MemoryItem.review_status == "proposed",
                MemoryItem.valid_to.is_(None),
                MemoryItem.superseded_by.is_(None),
                kind_allowed,
            )
            .values(review_status="active")
            .returning(MemoryItem.id)
        )
        if changed.scalar_one_or_none() is None:
            # Lost the guarded race: another worker already moved this row out
            # of `proposed`. Never write a false `admitted` assessment or a
            # second mutation event for a transition this pass did not make.
            # The winner's assessment stays authoritative; this pass appends a
            # truthful non-mutating result, and the projection's precedence
            # rule keeps it from displacing the winner.
            if capture.enabled:
                assert lanes is not None and snapshot is not None
                await _capture_admission_assessment(
                    session,
                    item,
                    candidate,
                    snapshot,
                    lanes,
                    capture,
                    mode="authoritative",
                    mutated=False,
                    live_proposal=False,
                    race_lost=True,
                    moment=moment,
                    policy_config=policy_config,
                    conflict_recheck_status=candidate.conflict_recheck_status,
                )
                capture = replace(capture, evaluation_id=None)
            continue
        actor = await resolve_trusted_system_actor(session, str(tenant_id))
        # The audit event's ID is preallocated so the assessment can name the
        # event it authorized while the event names the assessment that
        # authorized it. Both, the state mutation above and the projection
        # below live in this one transaction: they commit together or the
        # promotion fails closed (issue #159, capture enabled).
        event_id = uuid.uuid4()
        event = ItemEvent(
            id=event_id,
            item_id=item.id,
            **event_provenance,
            event_type="review_change",
            field_name="review_status",
            old_value="proposed",
            new_value="active",
            actor_principal_id=actor,
            reason=_audit(
                item, candidate, source, moment, min_age, evaluation_context=evaluation_context
            ),
        )
        session.add(event)
        if capture.enabled:
            assert lanes is not None and snapshot is not None
            await session.flush()
            assessment_row = await _capture_admission_assessment(
                session,
                item,
                candidate,
                snapshot,
                lanes,
                capture,
                mode="authoritative",
                mutated=True,
                moment=moment,
                policy_config=policy_config,
                conflict_recheck_status=candidate.conflict_recheck_status,
                # The evaluated input stays `proposed` — that is what policy
                # read and authorized this transition out of. The resulting
                # state records what the mutation produced, so the decision
                # resolves `current` immediately after commit instead of being
                # stale against its own effect.
                resulting_state=snapshot.promoted_state(),
                actor_principal_id=actor,
                linked_item_event_id=event_id,
            )
            event.admission_assessment_id = assessment_row.id
            capture = replace(capture, evaluation_id=None)
        result.promoted += 1
        result.promoted_ids.append(item.id)
        if candidate.selected_basis == "retention_evidence":
            result.promoted_retention_evidence += 1
        else:
            result.promoted_legacy_confidence += 1
    if rotation_active and items:
        await _advance_reconciliation_cursor(session, str(tenant_id), items[-1], moment)
    if not dry_run:
        await session.commit()
    else:
        await session.rollback()
        await _persist_shadow_decisions(session, shadow_decisions, capture, moment=moment)
    return result


async def auto_promote_item(
    session: AsyncSession,
    tenant_id: str,
    item_id: uuid.UUID,
    classification_run_id: uuid.UUID,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    memory_context: ResolvedMemoryContext | None = None,
) -> PromotionResult:
    return await auto_promote_proposed_memories(
        session,
        tenant_id,
        now=now,
        source="worker",
        dry_run=dry_run,
        item_id=item_id,
        classification_run_id=classification_run_id,
        memory_context=memory_context,
    )


async def schedule_evidence_promotion_if_qualified(
    session: AsyncSession,
    item: MemoryItem,
    run: ClassificationRun,
    *,
    diagnostics: dict[str, str] | None = None,
) -> uuid.UUID | None:
    """Atomically enqueue the delayed targeted job for statically qualified evidence."""
    def blocked(reason: str) -> None:
        if diagnostics is not None:
            diagnostics["blocker"] = reason

    if (
        item.review_status != "proposed"
        or item.valid_to is not None
        or item.superseded_by is not None
    ):
        blocked("item_state")
        return None
    config = await _config(session, str(item.tenant_id))
    enabled, _, min_age, evidence_enabled, evidence_threshold = _config_values(config)
    if not enabled:
        blocked("promotion_disabled")
        return None
    if not evidence_enabled:
        blocked("evidence_disabled")
        return None
    if item.conflict_resolution_status == "unresolved":
        blocked("conflict")
        return None
    support = (await load_promotion_support(session, [item]))[item.id]
    kind = support.kind
    bound_run = support.classification_run
    if (
        kind is None
        or not kind.enabled
        or not kind.auto_promote_from_inferred
        or bound_run is None
        or bound_run.id != run.id
    ):
        blocked("kind_or_receipt")
        return None
    evidence_blockers, score, _ = _evidence_state(item, run)
    if evidence_blockers:
        blocked(",".join(evidence_blockers))
        return None
    if score is None or score < evidence_threshold:
        blocked("evidence_score")
        return None
    # Do not test the cooling clock here: the point of this delayed job is to
    # wake at its end. Dynamic dispute and semantic-conflict gates remain the
    # target job's responsibility.
    if item.retention_disposition != "retain":
        blocked("retention_disposition")
        return None
    assert item.retention_evidence_at is not None
    # The scheduling boundary (run_after) is identical regardless of which job
    # contract is scheduled — issue #155 requires this slice not to change
    # when an otherwise-equivalent delayed evaluation becomes due.
    run_after = max(item.created_at, item.retention_evidence_at) + timedelta(hours=min_age)

    if settings.promotion_evaluate_jobs_enabled:
        # Rollout-flag path (ENG-PROMOTION-003B2): schedule the canonical,
        # current-state promotion.evaluate job instead of a new legacy
        # promotion.path_a job. Already-queued legacy jobs remain valid and
        # executable (mixed-version coexistence) — this branch only changes
        # what *new* schedule events from this producer create.
        job_id = await enqueue_promotion_evaluation(
            session,
            tenant_id=item.tenant_id,
            memory_item_id=item.id,
            trigger_type=TRIGGER_CLASSIFICATION_BOUND,
            trigger_id=str(run.id),
            requested_policy_version=EVIDENCE_PROMOTION_POLICY_VERSION,
            ingest_id=run.ingest_id,
            run_after=run_after,
        )
    else:
        from engram.jobs import enqueue_job_in_transaction

        job_id = await enqueue_job_in_transaction(
            session,
            tenant_id=item.tenant_id,
            job_type="promotion.path_a",
            payload={
                "memory_item_id": str(item.id),
                "classification_run_id": str(run.id),
                **({"ingest_id": str(run.ingest_id)} if run.ingest_id is not None else {}),
            },
            run_after=run_after,
            dedupe_key=f"promotion.path_a:{item.id}:{run.id}",
        )
    if diagnostics is not None:
        diagnostics["status"] = "scheduled"
    return job_id


async def maybe_auto_promote_for_startup_recall(
    session: AsyncSession,
    tenant_id: str,
    *,
    now: datetime | None = None,
    selected_window_observer: PromotionWindowObserver | None = None,
) -> PromotionResult:
    return await auto_promote_proposed_memories(
        session,
        tenant_id,
        now=now,
        limit=settings.startup_promotion_limit,
        source="startup_recall",
        rotation=True,
        selected_window_observer=selected_window_observer,
    )


# ---------------------------------------------------------------------------
# Canonical promotion.evaluate job contract (issue #155, ENG-PROMOTION-003B2)
#
# One versioned, item-scoped evaluation contract that always evaluates the
# item's *current* authoritative state at execution time — never the
# enqueue-time observation that triggered it. This is a fundamental
# difference from the legacy promotion.path_a classification-run binding
# (auto_promote_item / handle_promotion_path_a), which filters to a specific
# classification_run_id and treats a mismatch as "nothing to do". The
# canonical contract carries trigger_type/trigger_id as audit provenance
# only — never as a filter, never as authorization, never as decision input.
#
# This slice wires exactly one producer (classification.refine's delayed
# evidence-promotion schedule, behind ENGRAM_PROMOTION_EVALUATE_JOBS_ENABLED)
# and proves the contract is safe alongside the legacy job path. Full trigger
# coverage (item_created, feedback, conflict_changed, review_changed,
# provenance_changed, kind_changed, policy_changed, provider_recovery,
# reconcile, manual) remains #155 follow-up work — the vocabulary below is
# declared now, closed and versioned, so later slices only need to start
# calling enqueue_promotion_evaluation from new call sites, never redefine
# the contract.
# ---------------------------------------------------------------------------

PROMOTION_EVALUATE_JOB_TYPE = "promotion.evaluate"
PROMOTION_EVALUATE_CONTRACT_VERSION = "promotion-evaluate-v1"
# v2 (issue #155 correction) exists ONLY for the non-ingest pinned
# execution-authority form: a valid v2 payload always carries
# ``execution_context_id`` — the durable job_execution_contexts row pinning
# the producer request's execution authority (e.g. the manual admin trigger)
# — and never ``ingest_id``. v1 remains the contract for both pre-v2
# authority forms (ingest-bound via ``ingest_id``, unprofiled compatibility
# via neither field) and stays fully supported during mixed-version rollout.
PROMOTION_EVALUATE_CONTRACT_VERSION_V2 = "promotion-evaluate-v2"

TRIGGER_ITEM_CREATED = "item_created"
TRIGGER_CLASSIFICATION_BOUND = "classification_bound"
TRIGGER_CLASSIFICATION_REASSESSED = "classification_reassessed"
TRIGGER_FEEDBACK = "feedback"
TRIGGER_CONFLICT_CHANGED = "conflict_changed"
TRIGGER_REVIEW_CHANGED = "review_changed"
TRIGGER_PROVENANCE_CHANGED = "provenance_changed"
TRIGGER_KIND_CHANGED = "kind_changed"
TRIGGER_POLICY_CHANGED = "policy_changed"
TRIGGER_PROVIDER_RECOVERY = "provider_recovery"
TRIGGER_RECONCILE = "reconcile"
TRIGGER_MANUAL = "manual"

# The closed, versioned trigger vocabulary. Sized for the full #155
# destination (every lifecycle event that should eventually be able to
# schedule an evaluation), even though only TRIGGER_CLASSIFICATION_BOUND has
# a wired producer in this slice. An unrecognized trigger_type fails closed
# (see parse_promotion_evaluate_payload / enqueue_promotion_evaluation)
# rather than being silently accepted as a no-op label.
PROMOTION_EVALUATE_TRIGGER_TYPES: frozenset[str] = frozenset(
    {
        TRIGGER_ITEM_CREATED,
        TRIGGER_CLASSIFICATION_BOUND,
        TRIGGER_CLASSIFICATION_REASSESSED,
        TRIGGER_FEEDBACK,
        TRIGGER_CONFLICT_CHANGED,
        TRIGGER_REVIEW_CHANGED,
        TRIGGER_PROVENANCE_CHANGED,
        TRIGGER_KIND_CHANGED,
        TRIGGER_POLICY_CHANGED,
        TRIGGER_PROVIDER_RECOVERY,
        TRIGGER_RECONCILE,
        TRIGGER_MANUAL,
    }
)

# The exact, closed field set of a ``promotion-evaluate-v1`` payload. Every
# field is identifier/provenance-only: no mutable decision state (e.g.
# ``retention_confidence``, ``review_status``), no memory content, and no
# credentials ever belong in this contract. A generic ``metadata``/``extra``
# bag is deliberately not offered — an unrecognized field always fails
# closed (see ``parse_promotion_evaluate_payload``) rather than being
# silently tolerated or namespaced away.
PROMOTION_EVALUATE_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "contract_version",
        "memory_item_id",
        "trigger_type",
        "trigger_id",
        "requested_policy_version",
        "ingest_id",
        "correlation_id",
        "dedupe_key",
    }
)

# v2 extends the closed set by exactly one mandatory identifier field:
# ``execution_context_id`` references the immutable ``job_execution_contexts``
# row recording the pinned execution authority of the producer request (a
# reference to durable authorization state, never a copy of it). The field is
# what v2 *is* — a v2 payload without it is damaged and fails closed — and it
# is mutually exclusive with ``ingest_id`` by construction. Everything else
# about the v1 envelope — exact field set, closed vocabulary, centrally
# computed dedupe key — is unchanged.
PROMOTION_EVALUATE_ALLOWED_FIELDS_V2: frozenset[str] = PROMOTION_EVALUATE_ALLOWED_FIELDS | {
    "execution_context_id"
}


class PromotionEvaluateContractError(ValueError):
    """A ``promotion.evaluate`` payload is malformed, or carries an unknown/
    unsupported contract version or trigger type.

    Deliberately a ``ValueError`` subclass so it participates in the worker's
    ordinary retry/dead-letter machinery (issue #155 §10: malformed
    contracts fail/retry like any other unrecoverable job error) rather than
    being swallowed as a silent no-op.
    """


@dataclass(frozen=True)
class PromotionEvaluatePayload:
    """A parsed, validated ``promotion-evaluate-v1``/``-v2`` job payload.

    ``memory_item_id`` is the stable evaluation target. ``trigger_type`` /
    ``trigger_id`` are audit provenance only. ``requested_policy_version`` is
    what the producer expected/requested at enqueue time — descriptive, never
    mutation authority (the evaluator always applies whatever policy is
    currently configured). The execution-authority representation is named by
    the contract version itself: v1 carries either ``ingest_id`` (ingest-bound
    authority, consumed only to reconstruct execution authority identically
    to the legacy worker paths) or neither field (established unprofiled
    compatibility); v2 always carries a non-null ``execution_context_id``
    referencing the durable ``job_execution_contexts`` row pinning a
    non-ingest producer request's execution authority, and never
    ``ingest_id``. A successfully parsed v2 therefore can never resolve to
    the unprofiled ``memory_context=None`` path.
    """

    contract_version: str
    memory_item_id: uuid.UUID
    trigger_type: str
    trigger_id: str
    requested_policy_version: str
    ingest_id: uuid.UUID | None
    correlation_id: uuid.UUID | None
    dedupe_key: str
    execution_context_id: uuid.UUID | None = None


def promotion_evaluate_dedupe_key(
    memory_item_id: uuid.UUID | str, trigger_type: str, trigger_id: str
) -> str:
    """The canonical dedupe key for one (item, trigger identity) evaluation.

    Computed centrally — never accepted verbatim from a caller — so at most
    one pending/running job can ever exist for the same
    ``(tenant_id, memory_item_id, trigger_type, trigger_id)`` identity, while
    a different ``trigger_id`` (even for the same item and trigger_type)
    remains an independently representable, distinct evaluation.
    """
    return f"{PROMOTION_EVALUATE_JOB_TYPE}:{memory_item_id}:{trigger_type}:{trigger_id}"


def _require_uuid(value: object, *, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError as exc:
            raise PromotionEvaluateContractError(f"invalid {field}: {value!r}") from exc
    raise PromotionEvaluateContractError(f"invalid {field}: {value!r}")


def _optional_uuid(value: object, *, field: str) -> uuid.UUID | None:
    if value is None:
        return None
    return _require_uuid(value, field=field)


def _require_nonempty_str(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise PromotionEvaluateContractError(f"promotion.evaluate payload missing {field}")
    return value


def build_promotion_evaluate_payload(
    *,
    memory_item_id: uuid.UUID | str,
    trigger_type: str,
    trigger_id: str,
    requested_policy_version: str = EVIDENCE_PROMOTION_POLICY_VERSION,
    ingest_id: uuid.UUID | str | None = None,
    correlation_id: uuid.UUID | str | None = None,
    execution_context_id: uuid.UUID | str | None = None,
) -> dict[str, object]:
    """Construct one canonical, exact-field promotion-evaluate payload.

    This is the single producer-side half of the contract: it runs the same
    runtime validation :func:`parse_promotion_evaluate_payload` re-verifies on
    the worker side (never trusting Python type annotations alone) —
    ``memory_item_id`` is a real UUID, ``trigger_type`` is in the closed
    vocabulary, ``trigger_id`` / ``requested_policy_version`` are non-empty
    strings, and optional ``ingest_id`` / ``correlation_id`` /
    ``execution_context_id`` are valid UUIDs when supplied — and it always
    computes the dedupe key itself from the validated identity fields. Callers
    can never supply their own ``dedupe_key``, so enqueue-time construction
    and worker parse-time validation cannot drift apart. The returned dict
    contains exactly the closed field set of its contract version, nothing
    more: v1 without ``execution_context_id`` (covering both legacy authority
    forms — ingest-bound and unprofiled compatibility), v2 with one (the
    non-ingest pinned execution-authority form, which may never also carry
    ``ingest_id``).
    """
    item_id = _require_uuid(memory_item_id, field="memory_item_id")
    if trigger_type not in PROMOTION_EVALUATE_TRIGGER_TYPES:
        raise PromotionEvaluateContractError(
            f"unknown promotion.evaluate trigger_type: {trigger_type!r}"
        )
    validated_trigger_id = _require_nonempty_str(trigger_id, field="trigger_id")
    validated_policy_version = _require_nonempty_str(
        requested_policy_version, field="requested_policy_version"
    )
    resolved_ingest_id = _optional_uuid(ingest_id, field="ingest_id")
    resolved_correlation_id = _optional_uuid(correlation_id, field="correlation_id")
    resolved_execution_context_id = _optional_uuid(
        execution_context_id, field="execution_context_id"
    )
    if resolved_ingest_id is not None and resolved_execution_context_id is not None:
        raise PromotionEvaluateContractError(
            "promotion.evaluate payload cannot carry both ingest_id and "
            "execution_context_id: a job has exactly one execution-authority source"
        )
    payload: dict[str, object] = {
        "contract_version": PROMOTION_EVALUATE_CONTRACT_VERSION,
        "memory_item_id": str(item_id),
        "trigger_type": trigger_type,
        "trigger_id": validated_trigger_id,
        "requested_policy_version": validated_policy_version,
        "ingest_id": str(resolved_ingest_id) if resolved_ingest_id is not None else None,
        "correlation_id": (
            str(resolved_correlation_id) if resolved_correlation_id is not None else None
        ),
        "dedupe_key": promotion_evaluate_dedupe_key(item_id, trigger_type, validated_trigger_id),
    }
    if resolved_execution_context_id is not None:
        payload["contract_version"] = PROMOTION_EVALUATE_CONTRACT_VERSION_V2
        payload["execution_context_id"] = str(resolved_execution_context_id)
    return payload


def parse_promotion_evaluate_payload(payload: dict[str, object]) -> PromotionEvaluatePayload:
    """Parse and validate a ``promotion.evaluate`` job payload (v1/v2 contracts).

    Fails closed on every axis that would let a malformed or dishonest
    payload masquerade as a canonical evaluation job:

    * any field outside the closed set of the payload's declared
      ``contract_version`` (v1: :data:`PROMOTION_EVALUATE_ALLOWED_FIELDS`;
      v2: :data:`PROMOTION_EVALUATE_ALLOWED_FIELDS_V2`) — unknown mutable
      decision state, memory content, credentials, or anything else — is
      rejected outright: the envelope stays exact/closed per version, not a
      bag with tolerated extras. This includes a v1 payload carrying
      ``execution_context_id``, which v1's field set does not know;
    * an unknown/missing ``contract_version`` or an unrecognized
      ``trigger_type`` raises :class:`PromotionEvaluateContractError` rather
      than guessing at a compatible interpretation;
    * structurally malformed fields (missing/wrong-typed ``memory_item_id``,
      ``trigger_id``, etc.) raise the same error;
    * the contract version names the mandatory execution-authority
      representation: a v2 payload must carry a valid, non-null
      ``execution_context_id`` and must NOT carry ``ingest_id``. An
      authority-less v2 (the reference missing, null, or malformed) or an
      ingest-authorized v2 is damaged — unprofiled compatibility and
      ingest-bound authority are both v1's job — and fails closed here, so a
      successfully parsed v2 can never be executed under the unprofiled
      ``memory_context=None`` compatibility path;
    * the stored ``dedupe_key`` must equal the canonical key recomputed from
      the parsed ``(memory_item_id, trigger_type, trigger_id)`` identity — a
      wrong-but-nonempty ``dedupe_key`` is rejected exactly like an
      unsupported contract version, so the payload's claimed identity is
      independently verified rather than trusted from a generic queue
      producer or the database's unique-index behavior alone.
    """
    if not isinstance(payload, dict):
        raise PromotionEvaluateContractError("promotion.evaluate payload must be an object")
    contract_version = payload.get("contract_version")
    if contract_version == PROMOTION_EVALUATE_CONTRACT_VERSION:
        allowed_fields = PROMOTION_EVALUATE_ALLOWED_FIELDS
    elif contract_version == PROMOTION_EVALUATE_CONTRACT_VERSION_V2:
        allowed_fields = PROMOTION_EVALUATE_ALLOWED_FIELDS_V2
    else:
        raise PromotionEvaluateContractError(
            f"unsupported promotion.evaluate contract_version: {contract_version!r}"
        )
    unknown_fields = set(payload) - allowed_fields
    if unknown_fields:
        raise PromotionEvaluateContractError(
            "promotion.evaluate payload carries unsupported field(s): "
            f"{sorted(unknown_fields)!r}"
        )
    memory_item_id = _require_uuid(payload.get("memory_item_id"), field="memory_item_id")
    trigger_type = payload.get("trigger_type")
    if trigger_type not in PROMOTION_EVALUATE_TRIGGER_TYPES:
        raise PromotionEvaluateContractError(
            f"unknown promotion.evaluate trigger_type: {trigger_type!r}"
        )
    assert isinstance(trigger_type, str)
    trigger_id = _require_nonempty_str(payload.get("trigger_id"), field="trigger_id")
    requested_policy_version = _require_nonempty_str(
        payload.get("requested_policy_version"), field="requested_policy_version"
    )
    ingest_id = _optional_uuid(payload.get("ingest_id"), field="ingest_id")
    execution_context_id: uuid.UUID | None = None
    if contract_version == PROMOTION_EVALUATE_CONTRACT_VERSION_V2:
        # The version itself names the mandatory authority representation. A
        # v2 envelope whose reference is missing/null/malformed is damaged —
        # never a valid "unprofiled" v2 (that compatibility form is v1's) —
        # and one carrying an ingest_id is equally invalid (ingest-bound
        # authority is also v1's). Both fail closed here so the worker can
        # never resolve a parsed v2 to the unprofiled memory_context=None
        # path; the canonical producer never emits either shape, but worker
        # validation must not rely on producer correctness alone.
        execution_context_id = _optional_uuid(
            payload.get("execution_context_id"), field="execution_context_id"
        )
        if execution_context_id is None:
            raise PromotionEvaluateContractError(
                "promotion-evaluate-v2 requires a non-null execution_context_id "
                "(unprofiled compatibility is the v1 contract)"
            )
        if ingest_id is not None:
            raise PromotionEvaluateContractError(
                "promotion-evaluate-v2 cannot carry ingest_id "
                "(ingest-bound authority is the v1 contract)"
            )
    dedupe_key = _require_nonempty_str(payload.get("dedupe_key"), field="dedupe_key")
    expected_dedupe_key = promotion_evaluate_dedupe_key(memory_item_id, trigger_type, trigger_id)
    if dedupe_key != expected_dedupe_key:
        raise PromotionEvaluateContractError(
            f"promotion.evaluate dedupe_key {dedupe_key!r} does not match the canonical "
            f"identity key {expected_dedupe_key!r}"
        )
    return PromotionEvaluatePayload(
        contract_version=contract_version,
        memory_item_id=memory_item_id,
        trigger_type=trigger_type,
        trigger_id=trigger_id,
        requested_policy_version=requested_policy_version,
        ingest_id=ingest_id,
        correlation_id=_optional_uuid(payload.get("correlation_id"), field="correlation_id"),
        dedupe_key=dedupe_key,
        execution_context_id=execution_context_id,
    )


async def enqueue_promotion_evaluation(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID | str,
    memory_item_id: uuid.UUID,
    trigger_type: str,
    trigger_id: str,
    requested_policy_version: str = EVIDENCE_PROMOTION_POLICY_VERSION,
    ingest_id: uuid.UUID | None = None,
    correlation_id: uuid.UUID | None = None,
    execution_context_id: uuid.UUID | None = None,
    run_after: datetime | None = None,
) -> uuid.UUID:
    """Canonically enqueue one ``promotion.evaluate`` job (issue #155).

    Delegates all field validation and dedupe-key construction to
    :func:`build_promotion_evaluate_payload` — the exact same rules the
    worker re-verifies at parse time — so enqueue-time construction and
    execution-time validation cannot independently drift. Callers cannot
    supply their own ``dedupe_key``. ``execution_context_id`` (when supplied)
    references the durable ``job_execution_contexts`` row pinning a
    non-ingest producer request's execution authority, and selects the v2
    contract. Uses
    :func:`engram.jobs.enqueue_job_in_transaction`, so this preserves the
    caller's outer transaction (e.g. a classification-binding transaction)
    rather than committing it prematurely; the caller commits. Idempotent:
    enqueuing the same ``(item, trigger_type, trigger_id)`` identity again
    while a pending/running job exists returns that job's id instead of
    creating a duplicate. A different ``trigger_id`` always produces a
    distinct job, even for the same item.
    """
    from engram.jobs import enqueue_job_in_transaction

    payload = build_promotion_evaluate_payload(
        memory_item_id=memory_item_id,
        trigger_type=trigger_type,
        trigger_id=trigger_id,
        requested_policy_version=requested_policy_version,
        ingest_id=ingest_id,
        correlation_id=correlation_id,
        execution_context_id=execution_context_id,
    )
    return await enqueue_job_in_transaction(
        session,
        tenant_id=tenant_id,
        job_type=PROMOTION_EVALUATE_JOB_TYPE,
        payload=payload,
        run_after=run_after,
        dedupe_key=str(payload["dedupe_key"]),
    )


def _item_state_field(item: MemoryItem | Mapping[str, Any], name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


def is_live_proposal(item: MemoryItem | Mapping[str, Any]) -> bool:
    """Whether this item state is still a promotion candidate right now.

    Tolerates both ORM rows and the ``SELECT *`` mappings the API routes
    hold, so every producer can gate on the same in-transaction state. An
    item that is expired, superseded, or no longer ``proposed`` can only be
    a guaranteed no-op for the evaluator, so producers skip it.
    """
    return (
        _item_state_field(item, "review_status") == "proposed"
        and _item_state_field(item, "valid_to") is None
        and _item_state_field(item, "superseded_by") is None
    )


async def maybe_enqueue_promotion_evaluation(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID | str,
    item: MemoryItem | Mapping[str, Any],
    trigger_type: str,
    trigger_id: str,
    requested_policy_version: str = EVIDENCE_PROMOTION_POLICY_VERSION,
    ingest_id: uuid.UUID | None = None,
    correlation_id: uuid.UUID | None = None,
    execution_context_id: uuid.UUID | None = None,
    run_after: datetime | None = None,
) -> uuid.UUID | None:
    """Gate and canonically enqueue one evaluation for a committed event.

    The producer-side half of issue #155's trigger coverage. Every wired
    producer calls this with the item's in-transaction state and the stable
    id of the event that just committed (or will commit in the same
    transaction — enqueue uses ``enqueue_job_in_transaction``, so the job
    row exists iff the event does). The gates, in order:

    * ``ENGRAM_PROMOTION_EVALUATE_JOBS_ENABLED`` off → no job (rollout
      flag; default false, so all producers are inert by default);
    * the item is not a live proposal → no job (evaluation would be a
      guaranteed no-op);
    * the tenant's ``auto_promote_enabled`` is off → no job (the evaluator
      would only record ``skipped_disabled``; re-coverage after a policy
      re-enable belongs to the reconciliation backstop, not to producers).

    ``TRIGGER_ITEM_CREATED`` is the one purely time-dependent trigger: with
    no caller-supplied ``run_after`` it schedules at the exact legacy
    cooling boundary (``created_at + auto_promote_min_age_hours``) instead
    of immediately. Every other wired trigger runs immediately — the
    committed event itself is the reevaluation reason, and the evaluator
    no-ops safely if the item is still cooling.

    Trigger matrix for the producers wired through this helper (issue #155
    §2 requires each trigger to document its decision effect):

    * ``item_created`` — can newly admit (legacy lane at the cooling
      boundary); the baseline future path for explicit-kind writes and
      below-threshold receipts that bind no classification job.
    * ``feedback`` — can block (a new external ``noise`` verdict) or newly
      admit (a replacement verdict lifting an existing noise block). A
      first-time ``useful`` verdict is consumed by no current gate and so
      merely refreshes diagnostics; it is enqueued anyway because the
      transition is cheap, flag-gated, and forward-compatible with future
      usefulness lanes. Importance changes are *not* promotion evidence and
      are not read by the evaluator.
    * ``conflict_changed`` — can newly admit (resolution clears
      ``conflict_resolution_status='unresolved'``). Conflict *creation* is
      intentionally not a producer: a new conflict can only block, and the
      blocked state is re-derived from current state by any later
      evaluation rather than needing one scheduled at creation time.
    * ``review_changed`` — refreshes diagnostics only today: human
      verification does not feed any current promotion gate, and no
      committed review transition can land an item back on ``proposed``.
    * ``manual`` — can newly admit or block; runs the exact same evaluator
      as every other path, under explicit admin authority.
    """
    if not settings.promotion_evaluate_jobs_enabled:
        return None
    if not is_live_proposal(item):
        return None
    config = await _config(session, str(tenant_id))
    enabled, _, min_age_hours, _, _ = _config_values(config)
    if not enabled:
        return None
    item_id = _require_uuid(_item_state_field(item, "id"), field="memory_item_id")
    effective_run_after = run_after
    if effective_run_after is None and trigger_type == TRIGGER_ITEM_CREATED:
        created_at = _item_state_field(item, "created_at")
        if not isinstance(created_at, datetime):
            raise PromotionEvaluateContractError(
                f"item_created trigger requires a datetime created_at, got {created_at!r}"
            )
        effective_run_after = created_at + timedelta(hours=min_age_hours)
    return await enqueue_promotion_evaluation(
        session,
        tenant_id=tenant_id,
        memory_item_id=item_id,
        trigger_type=trigger_type,
        trigger_id=trigger_id,
        requested_policy_version=requested_policy_version,
        ingest_id=ingest_id,
        correlation_id=correlation_id,
        execution_context_id=execution_context_id,
        run_after=effective_run_after,
    )


async def evaluate_promotion_item_current_state(
    session: AsyncSession,
    tenant_id: str,
    item_id: uuid.UUID,
    *,
    evaluation_context: dict[str, object],
    now: datetime | None = None,
    memory_context: ResolvedMemoryContext | None = None,
) -> PromotionResult:
    """Evaluate one item's *current* authoritative state (canonical handler).

    Reuses the exact shared evaluator and mutation machinery every other
    promotion path uses (:func:`auto_promote_proposed_memories`), targeted at
    a single item and with no ``classification_run_id`` filter — so whatever
    classification run is *currently* bound to the item (if any) governs the
    evidence lane, never whichever run the enqueue-time trigger named. A
    trigger enqueued for a since-superseded observation is not an error and
    is not forced back into the decision: the item's current row is
    authoritative.

    No promotion policy differs from :func:`auto_promote_item` (legacy
    ``promotion.path_a``) — same thresholds, same evidence weights, same
    conflict recheck, same review-policy gate. Only the selection semantics
    (current state vs. a specific bound run) differ.
    """
    return await auto_promote_proposed_memories(
        session,
        tenant_id,
        now=now,
        source=PROMOTION_EVALUATE_JOB_TYPE,
        item_id=item_id,
        memory_context=memory_context,
        evaluation_context=evaluation_context,
    )
