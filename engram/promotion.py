"""Guarded two-lane Promotion Path A service."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Exists, Select, and_, exists, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

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
            if conflict and not dry_run:
                actor = await resolve_trusted_system_actor(session, str(tenant_id))
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
            continue
        result.would_promote += 1
        result.would_promote_ids.append(item.id)
        if candidate.selected_basis == "retention_evidence":
            result.would_promote_retention_evidence += 1
        else:
            result.would_promote_legacy_confidence += 1
        if dry_run:
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
            continue
        actor = await resolve_trusted_system_actor(session, str(tenant_id))
        session.add(
            ItemEvent(
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
        )
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
    session: AsyncSession, tenant_id: str, *, now: datetime | None = None
) -> PromotionResult:
    return await auto_promote_proposed_memories(
        session,
        tenant_id,
        now=now,
        limit=settings.startup_promotion_limit,
        source="startup_recall",
        rotation=True,
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
    """A parsed, validated ``promotion-evaluate-v1`` job payload.

    ``memory_item_id`` is the stable evaluation target. ``trigger_type`` /
    ``trigger_id`` are audit provenance only. ``requested_policy_version`` is
    what the producer expected/requested at enqueue time — descriptive, never
    mutation authority (the evaluator always applies whatever policy is
    currently configured). ``ingest_id`` is consumed only to reconstruct
    execution authority identically to the legacy worker paths.
    """

    contract_version: str
    memory_item_id: uuid.UUID
    trigger_type: str
    trigger_id: str
    requested_policy_version: str
    ingest_id: uuid.UUID | None
    correlation_id: uuid.UUID | None
    dedupe_key: str


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
) -> dict[str, object]:
    """Construct one canonical, exact-field ``promotion-evaluate-v1`` payload.

    This is the single producer-side half of the contract: it runs the same
    runtime validation :func:`parse_promotion_evaluate_payload` re-verifies on
    the worker side (never trusting Python type annotations alone) —
    ``memory_item_id`` is a real UUID, ``trigger_type`` is in the closed
    vocabulary, ``trigger_id`` / ``requested_policy_version`` are non-empty
    strings, and optional ``ingest_id`` / ``correlation_id`` are valid UUIDs
    when supplied — and it always computes the dedupe key itself from the
    validated identity fields. Callers can never supply their own
    ``dedupe_key``, so enqueue-time construction and worker parse-time
    validation cannot drift apart. The returned dict contains exactly
    :data:`PROMOTION_EVALUATE_ALLOWED_FIELDS`, nothing more.
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
    return {
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


def parse_promotion_evaluate_payload(payload: dict[str, object]) -> PromotionEvaluatePayload:
    """Parse and validate a ``promotion.evaluate`` job payload (v1 contract).

    Fails closed on every axis that would let a malformed or dishonest
    payload masquerade as a canonical evaluation job:

    * any field outside :data:`PROMOTION_EVALUATE_ALLOWED_FIELDS` (unknown
      mutable decision state, memory content, credentials, or anything else)
      is rejected outright — the v1 envelope is exact/closed, not a bag with
      tolerated extras;
    * an unknown/missing ``contract_version`` or an unrecognized
      ``trigger_type`` raises :class:`PromotionEvaluateContractError` rather
      than guessing at a compatible interpretation;
    * structurally malformed fields (missing/wrong-typed ``memory_item_id``,
      ``trigger_id``, etc.) raise the same error;
    * the stored ``dedupe_key`` must equal the canonical key recomputed from
      the parsed ``(memory_item_id, trigger_type, trigger_id)`` identity — a
      wrong-but-nonempty ``dedupe_key`` is rejected exactly like an
      unsupported contract version, so the payload's claimed identity is
      independently verified rather than trusted from a generic queue
      producer or the database's unique-index behavior alone.
    """
    if not isinstance(payload, dict):
        raise PromotionEvaluateContractError("promotion.evaluate payload must be an object")
    unknown_fields = set(payload) - PROMOTION_EVALUATE_ALLOWED_FIELDS
    if unknown_fields:
        raise PromotionEvaluateContractError(
            "promotion.evaluate payload carries unsupported field(s): "
            f"{sorted(unknown_fields)!r}"
        )
    contract_version = payload.get("contract_version")
    if contract_version != PROMOTION_EVALUATE_CONTRACT_VERSION:
        raise PromotionEvaluateContractError(
            f"unsupported promotion.evaluate contract_version: {contract_version!r}"
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
        ingest_id=_optional_uuid(payload.get("ingest_id"), field="ingest_id"),
        correlation_id=_optional_uuid(payload.get("correlation_id"), field="correlation_id"),
        dedupe_key=dedupe_key,
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
    run_after: datetime | None = None,
) -> uuid.UUID:
    """Canonically enqueue one ``promotion.evaluate`` job (issue #155).

    Delegates all field validation and dedupe-key construction to
    :func:`build_promotion_evaluate_payload` — the exact same rules the
    worker re-verifies at parse time — so enqueue-time construction and
    execution-time validation cannot independently drift. Callers cannot
    supply their own ``dedupe_key``. Uses
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
