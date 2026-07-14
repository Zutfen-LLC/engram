"""Guarded two-lane Promotion Path A service."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from engram.config import settings
from engram.conflicts import PromotionConflictCheck, check_promotion_conflict
from engram.feedback import current_feedback_predicate
from engram.internal_actors import (
    REVIEW_AUTOMATION_INTERNAL_KEY,
    InternalActorInvariantError,
    resolve_internal_system_actor,
)
from engram.models import (
    ClassificationRun,
    FeedbackEvent,
    ItemEvent,
    MemoryItem,
    MemoryKind,
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
    item: MemoryItem, candidate: PromotionCandidate, source: str, now: datetime, min_age_hours: int
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
    return json.dumps(reason, sort_keys=True)


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
) -> PromotionResult:
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
    if item_id is not None:
        base_stmt = base_stmt.where(MemoryItem.id == item_id)
    else:
        base_stmt = base_stmt.order_by(MemoryItem.created_at.asc())
        if limit is not None:
            base_stmt = base_stmt.limit(limit)
    if not enabled:
        items = list((await session.execute(base_stmt)).scalars())
        result.scanned = len(items)
        result.skipped_disabled = len(items)
        if dry_run:
            await session.rollback()
        else:
            await session.commit()
        return result
    stmt = base_stmt
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=item_id is None)
    items = list((await session.execute(stmt)).scalars())
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
            conflict = await check_promotion_conflict(session, item)
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
                event_type="review_change",
                field_name="review_status",
                old_value="proposed",
                new_value="active",
                actor_principal_id=actor,
                reason=_audit(item, candidate, source, moment, min_age),
            )
        )
        result.promoted += 1
        result.promoted_ids.append(item.id)
        if candidate.selected_basis == "retention_evidence":
            result.promoted_retention_evidence += 1
        else:
            result.promoted_legacy_confidence += 1
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
) -> PromotionResult:
    return await auto_promote_proposed_memories(
        session,
        tenant_id,
        now=now,
        source="worker",
        dry_run=dry_run,
        item_id=item_id,
        classification_run_id=classification_run_id,
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
    from engram.jobs import enqueue_job_in_transaction

    assert item.retention_evidence_at is not None
    run_after = max(item.created_at, item.retention_evidence_at) + timedelta(hours=min_age)
    job_id = await enqueue_job_in_transaction(
        session,
        tenant_id=item.tenant_id,
        job_type="promotion.path_a",
        payload={"memory_item_id": str(item.id), "classification_run_id": str(run.id)},
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
        session, tenant_id, now=now, limit=settings.startup_promotion_limit, source="startup_recall"
    )
