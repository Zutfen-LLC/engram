"""Operator and reviewer access to durable admission decisions (issue #159).

Read authority follows item read eligibility for the basic views; the full
normalized decision inputs and diagnostic evidence references require
review authority. Nothing here mutates promotion state: the reevaluation
endpoint enqueues the existing #155 ``promotion.evaluate`` job and returns.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import exists, literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from engram.admission_assessment import (
    POLICY_CONTRACT_VERSION,
    POLICY_PROFILE_KEY,
    ResolvedAdmission,
    digest,
    input_state_payload,
    load_current_assessment,
    policy_config_payload,
    resolve_projection_status,
    summary_payload,
)
from engram.admission_assessment_schema import (
    AdmissionAssessmentCurrentResponse,
    AdmissionAssessmentDetail,
    AdmissionAssessmentHistory,
    AdmissionAssessmentView,
    AdmissionReevaluateRequest,
    AdmissionReevaluateResponse,
)
from engram.auth import READ_SCOPE, REVIEW_SCOPE
from engram.config import settings
from engram.db import get_session
from engram.memory_access import read_eligibility_expression, write_eligibility_expression
from engram.memory_context import ResolvedMemoryContext, resolve_memory_context
from engram.models import AdmissionAssessment, AdmissionAssessmentCurrent, MemoryItem
from engram.promotion import (
    TRIGGER_MANUAL,
    TRIGGER_POLICY_CHANGED,
    TRIGGER_PROVENANCE_CHANGED,
    enqueue_promotion_evaluation,
)

router = APIRouter()

# What a reader sees for an item with no recorded decision — the ordinary
# state while capture is disabled. Deliberately not an empty dict: a caller
# must be able to tell "nothing has been recorded" from "a decision exists
# whose outcome is unknown", and both must be expressible in one shape.
MISSING_ADMISSION_SUMMARY: dict[str, Any] = summary_payload(ResolvedAdmission("missing", None))

# The reevaluation reasons an operator may express, mapped onto the existing
# closed #155 trigger vocabulary. No new trigger type and no new job type are
# introduced here.
_REASON_TRIGGERS = {
    "operator_request": TRIGGER_MANUAL,
    "policy_changed": TRIGGER_POLICY_CHANGED,
    "provenance_changed": TRIGGER_PROVENANCE_CHANGED,
}


async def _eligible_item(
    session: AsyncSession,
    item_id: UUID,
    context: ResolvedMemoryContext,
    *,
    write: bool = False,
) -> MemoryItem:
    eligibility = write_eligibility_expression if write else read_eligibility_expression
    item = await session.scalar(
        select(MemoryItem).where(MemoryItem.id == item_id, eligibility(context))
    )
    if item is None:
        raise HTTPException(404, "Item not found")
    return item


def _view_payload(row: AdmissionAssessment) -> dict[str, Any]:
    return {
        "assessment_id": row.id,
        "schema_version": row.schema_version,
        "mode": row.mode,
        "policy_profile_key": row.policy_profile_key,
        "policy_contract_version": row.policy_contract_version,
        "policy_config_digest": row.policy_config_digest,
        "input_digest": row.input_digest,
        "item_content_hash": row.item_content_hash,
        "selected_basis": row.selected_basis,
        "outcome": row.outcome,
        "blocker_codes": list(row.blocker_codes),
        "reason_codes": list(row.reason_codes),
        "next_actions": list(row.next_actions),
        "conflict_recheck_status": row.conflict_recheck_status,
        "cooling_period_start": row.cooling_period_start,
        "eligible_at": row.eligible_at,
        "next_evaluation_at": row.next_evaluation_at,
        "decision_hash": row.decision_hash,
        "evaluated_at": row.evaluated_at,
        "trigger_type": row.trigger_type,
        "trigger_id": row.trigger_id,
        "invocation_source": row.invocation_source,
        "evaluation_id": row.evaluation_id,
        "prior_assessment_id": row.prior_assessment_id,
        "linked_item_event_id": row.linked_item_event_id,
    }


def assessment_view(row: AdmissionAssessment) -> AdmissionAssessmentView:
    return AdmissionAssessmentView.model_validate(_view_payload(row))


def assessment_detail(row: AdmissionAssessment) -> AdmissionAssessmentDetail:
    return AdmissionAssessmentDetail.model_validate(
        {
            **_view_payload(row),
            "decision_inputs": dict(row.decision_inputs),
            "available_memory_assessment_refs": list(row.available_memory_assessment_refs),
            "classification_run_id": row.classification_run_id,
            "job_id": row.job_id,
            "actor_principal_id": row.actor_principal_id,
        }
    )


async def current_digests(session: AsyncSession, item: MemoryItem) -> tuple[str, str]:
    """Recompute this item's input and policy digests from present state.

    Freshness is decided by this comparison, so the resolver reads exactly the
    same policy configuration and item/evidence state the evaluator would.
    """
    from engram.promotion import _config, _config_values, load_promotion_support

    config = await _config(session, str(item.tenant_id))
    _, threshold, min_age, evidence_enabled, evidence_threshold = _config_values(config)
    support = (await load_promotion_support(session, [item]))[item.id]
    kind = support.kind
    return (
        digest(input_state_payload(item, support.classification_run)),
        digest(
            policy_config_payload(
                confidence_threshold=threshold,
                min_age_hours=min_age,
                evidence_enabled=evidence_enabled,
                evidence_threshold=evidence_threshold,
                kind_auto_promote_allowed=bool(
                    kind and kind.enabled and kind.auto_promote_from_inferred
                ),
            )
        ),
    )


@router.get(
    "/items/{item_id}/admission-assessment",
    response_model=AdmissionAssessmentCurrentResponse,
    dependencies=[Depends(READ_SCOPE)],
)
async def current_admission_assessment(
    item_id: UUID,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    context: ResolvedMemoryContext = Depends(resolve_memory_context),  # noqa: B008
) -> AdmissionAssessmentCurrentResponse:
    """Resolve the current admission decision for ``path_a_compat``.

    ``status='missing'`` means no decision has ever been recorded for this
    item — which is the expected state while capture is disabled — and is
    never conflated with a recorded decision whose outcome is ``unknown``.
    """
    item = await _eligible_item(session, item_id, context)
    current_input, current_policy = await current_digests(session, item)
    row = await load_current_assessment(
        session, tenant_id=item.tenant_id, memory_item_id=item.id
    )
    resolved = resolve_projection_status(
        row,
        current_input_digest=current_input,
        current_policy_config_digest=current_policy,
    )
    return AdmissionAssessmentCurrentResponse(
        item_id=item.id,
        policy_profile_key=POLICY_PROFILE_KEY,
        policy_contract_version=POLICY_CONTRACT_VERSION,
        status=resolved.status,
        capture_enabled=settings.admission_assessment_capture_enabled,
        current_input_digest=current_input,
        current_policy_config_digest=current_policy,
        assessment=(
            assessment_view(resolved.assessment) if resolved.assessment is not None else None
        ),
    )


@router.get(
    "/items/{item_id}/admission-assessments",
    response_model=AdmissionAssessmentHistory,
    dependencies=[Depends(READ_SCOPE)],
)
async def admission_assessment_history(
    item_id: UUID,
    limit: int = Query(default=50, ge=1, le=100),
    before: UUID | None = None,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    context: ResolvedMemoryContext = Depends(resolve_memory_context),  # noqa: B008
) -> AdmissionAssessmentHistory:
    """Paginated immutable history, newest first.

    Keyset paginated on ``(evaluated_at, id)`` so a long-lived item's history
    is read in bounded pages rather than scanned.
    """
    item = await _eligible_item(session, item_id, context)
    base = select(AdmissionAssessment).where(
        AdmissionAssessment.tenant_id == item.tenant_id,
        AdmissionAssessment.memory_item_id == item.id,
        AdmissionAssessment.policy_profile_key == POLICY_PROFILE_KEY,
    )
    query = base
    if before is not None:
        cursor = await session.scalar(base.where(AdmissionAssessment.id == before))
        if cursor is None:
            raise HTTPException(422, "Admission assessment cursor unavailable")
        query = query.where(
            tuple_(AdmissionAssessment.evaluated_at, AdmissionAssessment.id)
            < tuple_(literal(cursor.evaluated_at), literal(cursor.id))
        )
    rows = list(
        (
            await session.scalars(
                query.order_by(
                    AdmissionAssessment.evaluated_at.desc(), AdmissionAssessment.id.desc()
                ).limit(limit + 1)
            )
        ).all()
    )
    return AdmissionAssessmentHistory(
        item_id=item.id,
        policy_profile_key=POLICY_PROFILE_KEY,
        assessments=[assessment_view(row) for row in rows[:limit]],
        next_before=rows[limit - 1].id if len(rows) > limit else None,
    )


@router.get(
    "/items/{item_id}/admission-assessments/{assessment_id}",
    response_model=AdmissionAssessmentDetail,
    dependencies=[Depends(REVIEW_SCOPE)],
)
async def admission_assessment_detail(
    item_id: UUID,
    assessment_id: UUID,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    context: ResolvedMemoryContext = Depends(resolve_memory_context),  # noqa: B008
) -> AdmissionAssessmentDetail:
    """Bounded reviewer/debug detail: normalized inputs and evidence references.

    The evidence references are #157 identities only — ID, purpose, schema
    version, canonical hash — and were diagnostic at evaluation time. They
    never changed the outcome, and nothing here exposes provider output,
    transcripts, or conflict candidate identity.
    """
    item = await _eligible_item(session, item_id, context)
    row = await session.scalar(
        select(AdmissionAssessment).where(
            AdmissionAssessment.id == assessment_id,
            AdmissionAssessment.tenant_id == item.tenant_id,
            AdmissionAssessment.memory_item_id == item.id,
        )
    )
    if row is None:
        raise HTTPException(404, "Admission assessment not found")
    return assessment_detail(row)


@router.post(
    "/items/{item_id}/admission-assessments/reevaluate",
    response_model=AdmissionReevaluateResponse,
    dependencies=[Depends(REVIEW_SCOPE)],
)
async def reevaluate(
    item_id: UUID,
    request: AdmissionReevaluateRequest,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    context: ResolvedMemoryContext = Depends(resolve_memory_context),  # noqa: B008
) -> AdmissionReevaluateResponse:
    """Request one bounded reevaluation through existing ``promotion.evaluate``.

    No new job type and no synchronous scan: this enqueues the canonical #155
    item-scoped job, which evaluates whatever state is authoritative when it
    runs. Replaying the same ``trigger_id`` while a job is pending returns
    that job rather than queuing a second one.
    """
    item = await _eligible_item(session, item_id, context, write=True)
    job_id = await enqueue_promotion_evaluation(
        session,
        tenant_id=item.tenant_id,
        memory_item_id=item.id,
        trigger_type=_REASON_TRIGGERS[request.reason],
        trigger_id=request.trigger_id,
    )
    await session.commit()
    return AdmissionReevaluateResponse(
        item_id=item.id,
        job_id=job_id,
        trigger_type=_REASON_TRIGGERS[request.reason],
        trigger_id=request.trigger_id,
        policy_profile_key=POLICY_PROFILE_KEY,
    )


async def admission_summaries(
    session: AsyncSession, items: list[MemoryItem]
) -> dict[uuid.UUID, dict[str, Any]]:
    """Safe admission summaries for a bounded list of items, in one query pair.

    Used by the review queue so filtering and display never degrade into a
    per-item resolution loop. Items with no recorded decision are absent from
    the result; callers render them as ``missing``.
    """
    from engram.models import AdmissionAssessmentCurrent
    from engram.promotion import _config, _config_values, load_promotion_support

    if not items:
        return {}
    tenant_id = items[0].tenant_id
    rows = (
        await session.scalars(
            select(AdmissionAssessment)
            .join(
                AdmissionAssessmentCurrent,
                AdmissionAssessmentCurrent.assessment_id == AdmissionAssessment.id,
            )
            .where(
                AdmissionAssessmentCurrent.tenant_id == tenant_id,
                AdmissionAssessmentCurrent.memory_item_id.in_([item.id for item in items]),
                AdmissionAssessmentCurrent.policy_profile_key == POLICY_PROFILE_KEY,
            )
        )
    ).all()
    by_item = {row.memory_item_id: row for row in rows}
    config = await _config(session, str(tenant_id))
    _, threshold, min_age, evidence_enabled, evidence_threshold = _config_values(config)
    support_map = await load_promotion_support(session, items)
    summaries: dict[uuid.UUID, dict[str, Any]] = {}
    for item in items:
        row = by_item.get(item.id)
        if row is None:
            continue
        support = support_map[item.id]
        kind = support.kind
        resolved = resolve_projection_status(
            row,
            current_input_digest=digest(input_state_payload(item, support.classification_run)),
            current_policy_config_digest=digest(
                policy_config_payload(
                    confidence_threshold=threshold,
                    min_age_hours=min_age,
                    evidence_enabled=evidence_enabled,
                    evidence_threshold=evidence_threshold,
                    kind_auto_promote_allowed=bool(
                        kind and kind.enabled and kind.auto_promote_from_inferred
                    ),
                )
            ),
        )
        summaries[item.id] = {
            **summary_payload(resolved),
            "admission_blocker_codes": list(row.blocker_codes),
        }
    return summaries


# One filtered review-queue page never scans more than this many candidate
# rows. The computed current/stale state cannot be expressed as a SQL
# predicate (it is a digest comparison against live item and policy state), so
# that filter walks the queue in bounded batches instead of silently stopping
# at the first unfiltered window. Reaching this cap is an explicit bounded end,
# not a claim that nothing further matches.
MAX_ADMISSION_FILTER_SCAN = 2000

# Batch size for that walk.
_ADMISSION_SCAN_BATCH = 200

# States that are decided by comparing digests against current state, and so
# cannot be resolved in SQL.
_COMPUTED_STATES = frozenset({"current", "stale", "legacy_import"})


@dataclass(frozen=True)
class AdmissionQueueFilters:
    """The exact admission filter set this issue scopes to the review queue."""

    outcome: str | None = None
    blocker: str | None = None
    next_action: str | None = None
    state: str | None = None
    due_before: datetime | None = None

    @property
    def active(self) -> bool:
        return any(
            value is not None
            for value in (
                self.outcome,
                self.blocker,
                self.next_action,
                self.state,
                self.due_before,
            )
        )

    @property
    def needs_computed_state(self) -> bool:
        return self.state in _COMPUTED_STATES

    @property
    def incompatible_with_missing(self) -> tuple[str, ...]:
        """Return filters that cannot describe a missing assessment."""
        if self.state != "missing":
            return ()
        companions = (
            ("admission_outcome", self.outcome),
            ("admission_blocker", self.blocker),
            ("admission_next_action", self.next_action),
            ("admission_due_before", self.due_before),
        )
        return tuple(name for name, value in companions if value is not None)


class AdmissionFilterScanExhaustedError(RuntimeError):
    """A computed-state queue search has unexamined candidate rows."""

    def __init__(self, scanned: int) -> None:
        super().__init__(f"admission filter scan exhausted after {scanned} rows")
        self.scanned = scanned


def apply_admission_sql_filters(stmt: Select[Any], filters: AdmissionQueueFilters) -> Select[Any]:
    """Push every filter that is a stored fact down into SQL.

    Outcome, blocker code, next action and due time all live on the pointed
    assessment, so they select rows in the database rather than thinning an
    arbitrary page after the fact. ``missing`` is the absence of a projection
    row, which is also a SQL predicate. Only ``current`` / ``stale`` /
    ``legacy_import`` need the digest comparison the caller does afterwards,
    and even those are narrowed here first (a legacy_import row must at least
    be in legacy_import mode).
    """
    projection = (
        select(AdmissionAssessment)
        .join(
            AdmissionAssessmentCurrent,
            AdmissionAssessmentCurrent.assessment_id == AdmissionAssessment.id,
        )
        .where(
            AdmissionAssessmentCurrent.tenant_id == MemoryItem.tenant_id,
            AdmissionAssessmentCurrent.memory_item_id == MemoryItem.id,
            AdmissionAssessmentCurrent.policy_profile_key == POLICY_PROFILE_KEY,
        )
    )
    if filters.state == "missing":
        # No decision has ever been recorded. Nothing else can be asserted
        # about such an item, so no other filter may accompany this one.
        return stmt.where(~exists(projection))
    conditions = []
    if filters.outcome is not None:
        conditions.append(AdmissionAssessment.outcome == filters.outcome)
    if filters.blocker is not None:
        conditions.append(AdmissionAssessment.blocker_codes.contains([filters.blocker]))
    if filters.next_action is not None:
        conditions.append(AdmissionAssessment.next_actions.contains([filters.next_action]))
    if filters.due_before is not None:
        conditions.append(AdmissionAssessment.next_evaluation_at.is_not(None))
        conditions.append(AdmissionAssessment.next_evaluation_at < filters.due_before)
    if filters.state == "legacy_import":
        conditions.append(AdmissionAssessment.mode == "legacy_import")
    elif filters.state in {"current", "stale"}:
        # Both are authoritative-or-legacy rows; which one is decided by the
        # digest comparison the caller performs on the narrowed set.
        conditions.append(AdmissionAssessment.mode.in_(("authoritative", "legacy_import")))
    if not conditions:
        return stmt
    return stmt.where(exists(projection.where(*conditions)))


async def select_filtered_review_items(
    session: AsyncSession,
    *,
    base_stmt: Select[Any],
    filters: AdmissionQueueFilters,
    limit: int,
) -> list[MemoryItem]:
    """Return up to ``limit`` items matching ``filters``, bounded but honest.

    With only stored-fact filters, SQL selects the page directly and one query
    suffices. When the computed current/stale state is also requested, the
    remaining predicate cannot be a SQL expression, so this walks the
    SQL-narrowed queue in keyset batches and keeps going until the requested
    page is full or :data:`MAX_ADMISSION_FILTER_SCAN` rows have been examined.

    The distinction matters operationally: stopping at the first unfiltered
    window would report "nothing matches" whenever the matching item happened
    to sit past the caller's limit, which is exactly the false-negative answer
    #159 exists to eliminate.
    """
    filtered = apply_admission_sql_filters(base_stmt, filters)
    if not filters.needs_computed_state:
        return list((await session.execute(filtered.limit(limit))).scalars())

    matched: list[MemoryItem] = []
    scanned = 0
    cursor: tuple[datetime, uuid.UUID] | None = None
    while len(matched) < limit and scanned < MAX_ADMISSION_FILTER_SCAN:
        batch_stmt = filtered
        if cursor is not None:
            batch_stmt = batch_stmt.where(
                tuple_(MemoryItem.created_at, MemoryItem.id)
                < tuple_(literal(cursor[0]), literal(cursor[1]))
            )
        remaining = MAX_ADMISSION_FILTER_SCAN - scanned
        batch = list(
            (
                await session.execute(batch_stmt.limit(min(_ADMISSION_SCAN_BATCH, remaining)))
            ).scalars()
        )
        if not batch:
            break
        scanned += len(batch)
        summaries = await admission_summaries(session, batch)
        for item in batch:
            if len(matched) >= limit:
                break
            if matches_admission_filters(
                summaries.get(item.id),
                outcome=filters.outcome,
                blocker=filters.blocker,
                next_action=filters.next_action,
                state=filters.state,
                due_before=filters.due_before,
            ):
                matched.append(item)
        cursor = (batch[-1].created_at, batch[-1].id)
    if len(matched) < limit and scanned >= MAX_ADMISSION_FILTER_SCAN and cursor is not None:
        has_more = await session.scalar(
            select(
                exists(
                    filtered.where(
                        tuple_(MemoryItem.created_at, MemoryItem.id)
                        < tuple_(literal(cursor[0]), literal(cursor[1]))
                    )
                )
            )
        )
        if has_more:
            raise AdmissionFilterScanExhaustedError(scanned)
    return matched


def matches_admission_filters(
    summary: dict[str, Any] | None,
    *,
    outcome: str | None,
    blocker: str | None,
    next_action: str | None,
    state: str | None,
    due_before: datetime | None,
) -> bool:
    """Apply the exact filter set this issue scopes to the review queue.

    An item with no recorded decision matches only ``state='missing'``: it
    cannot honestly satisfy an outcome, blocker, next-action or due-time
    filter, and silently including it would misreport coverage while capture
    is being rolled out.
    """
    if summary is None:
        return state == "missing" and not any((outcome, blocker, next_action, due_before))
    if outcome is not None and summary["admission_outcome"] != outcome:
        return False
    if blocker is not None and blocker not in summary["admission_blocker_codes"]:
        return False
    if next_action is not None and next_action not in summary["admission_next_actions"]:
        return False
    if state is not None and summary["admission_assessment_status"] != state:
        return False
    if due_before is not None:
        due = summary["next_evaluation_at"]
        if due is None or datetime.fromisoformat(due) >= due_before:
            return False
    return True


def utcnow() -> datetime:
    return datetime.now(UTC)
