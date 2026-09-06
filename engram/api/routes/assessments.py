"""Authorized assessment history and bounded reassessment requests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engram.assessment_schema import (
    AssessmentHistory,
    ReassessBatchRequest,
    ReassessRequest,
    ReassessResponse,
)
from engram.assessments import (
    assessment_history,
    current_contract,
    evidence_snapshot,
    live_item,
    request_assessment,
    request_view,
)
from engram.auth import READ_SCOPE, REVIEW_SCOPE
from engram.config import settings
from engram.db import get_session
from engram.memory_access import read_eligibility_expression, write_eligibility_expression
from engram.memory_context import ResolvedMemoryContext, resolve_memory_context
from engram.models import AssessmentRequest, Job, MemoryAssessment, MemoryItem

router = APIRouter()


async def eligible_item(
    session: AsyncSession,
    item_id: UUID,
    context: ResolvedMemoryContext,
    *,
    write: bool = False,
) -> MemoryItem:
    eligibility = write_eligibility_expression if write else read_eligibility_expression
    item = await session.scalar(
        select(MemoryItem).where(
            MemoryItem.id == item_id,
            eligibility(context),
        )
    )
    if item is None:
        raise HTTPException(404, "Item not found")
    return item


def validate_target(request: ReassessRequest) -> None:
    if not settings.assessment_reassessment_enabled:
        raise HTTPException(409, "Reassessment is disabled")
    if request.target is not None and request.target != current_contract():
        raise HTTPException(422, "Target contract is not deployed")


@router.post(
    "/items/{item_id}/reassess",
    response_model=ReassessResponse,
    dependencies=[Depends(REVIEW_SCOPE)],
)
async def reassess(
    item_id: UUID,
    request: ReassessRequest,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    context: ResolvedMemoryContext = Depends(resolve_memory_context),  # noqa: B008
) -> ReassessResponse:
    """Request assessment. Retention does not establish truth or startup eligibility."""
    item = await eligible_item(session, item_id, context, write=True)
    validate_target(request)
    if not live_item(item):
        raise HTTPException(409, "Item is not live")
    try:
        row = await request_assessment(session, item, context, request)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    result = await request_view(session, row)
    await session.commit()
    return result


@router.post(
    "/assessments/reassess",
    response_model=list[ReassessResponse],
    dependencies=[Depends(REVIEW_SCOPE)],
)
async def reassess_batch(
    request: ReassessBatchRequest,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    context: ResolvedMemoryContext = Depends(resolve_memory_context),  # noqa: B008
) -> list[ReassessResponse]:
    """Request at most 100 eligible items. Continue with the last returned item ID."""
    validate_target(request)
    query = select(MemoryItem).where(
        write_eligibility_expression(context),
        MemoryItem.valid_to.is_(None),
        MemoryItem.review_status.not_in(("rejected", "archived")),
        MemoryItem.superseded_by.is_(None),
    )
    if request.workspace_id is not None:
        query = query.where(MemoryItem.workspace_id == request.workspace_id)
    if request.after_item_id is not None:
        query = query.where(MemoryItem.id > request.after_item_id)
    items = (await session.scalars(query.order_by(MemoryItem.id).limit(request.limit))).all()
    result = []
    try:
        for item in items:
            if live_item(item):
                row = await request_assessment(session, item, context, request)
                result.append(await request_view(session, row))
    except ValueError as exc:
        # Evidence validation errors must roll back earlier batch work. The
        # batch therefore has one explicit, controlled failure result.
        await session.rollback()
        raise HTTPException(422, str(exc)) from exc
    await session.commit()
    return result


@router.get(
    "/items/{item_id}/assessments",
    response_model=AssessmentHistory,
    dependencies=[Depends(READ_SCOPE)],
)
async def history(
    item_id: UUID,
    limit: int = Query(default=50, ge=1, le=100),
    before: UUID | None = None,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    context: ResolvedMemoryContext = Depends(resolve_memory_context),  # noqa: B008
) -> AssessmentHistory:
    """Read safe normalized dimensions. Missing scores remain null."""
    item = await eligible_item(session, item_id, context)
    try:
        return await assessment_history(session, item, context, limit=limit, before=before)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get(
    "/items/{item_id}/reassessments/{request_id}",
    response_model=ReassessResponse,
    dependencies=[Depends(REVIEW_SCOPE)],
)
async def request_status(
    item_id: UUID,
    request_id: UUID,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    context: ResolvedMemoryContext = Depends(resolve_memory_context),  # noqa: B008
) -> ReassessResponse:
    """Read retry and dead-letter state without provider exception text."""
    await eligible_item(session, item_id, context)
    row = await session.scalar(
        select(AssessmentRequest).where(
            AssessmentRequest.id == request_id,
            AssessmentRequest.memory_item_id == item_id,
        )
    )
    if row is None:
        raise HTTPException(404, "Assessment request not found")
    return await request_view(session, row)


@router.post(
    "/items/{item_id}/reassessments/{request_id}/retry",
    response_model=ReassessResponse,
    dependencies=[Depends(REVIEW_SCOPE)],
)
async def retry_request(
    item_id: UUID,
    request_id: UUID,
    body: ReassessRequest,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    context: ResolvedMemoryContext = Depends(resolve_memory_context),  # noqa: B008
) -> ReassessResponse:
    """Retry a dead provider job. Preserve its identity, attempts, and historical receipts."""
    from engram.api.routes.memory import _insert_item_event
    from engram.extraction import digest

    item = await eligible_item(session, item_id, context, write=True)
    validate_target(body)
    row = await session.scalar(
        select(AssessmentRequest).where(
            AssessmentRequest.id == request_id,
            AssessmentRequest.memory_item_id == item_id,
        )
    )
    if row is None:
        raise HTTPException(404, "Assessment request not found")
    if (
        not live_item(item)
        or row.target != current_contract().model_dump(mode="json")
        or row.input_digest != digest(await evidence_snapshot(session, item, context))
    ):
        raise HTTPException(409, "Input or target changed; request a new assessment")
    job = await session.scalar(select(Job).where(Job.id == row.job_id).with_for_update())
    if job is None:
        raise HTTPException(404, "Assessment job not found")
    if job.status == "dead":
        await _insert_item_event(
            session,
            item_id=item.id,
            event_type="classification",
            field_name="assessment_retry",
            old_value="dead",
            new_value={
                "request_id": str(row.id),
                "job_id": str(job.id),
                "target_contract": row.contract_hash,
            },
            actor_principal_id=context.principal_id,
            reason=body.reason,
            memory_context=context,
        )
        job.status = "pending"
        job.max_attempts = job.attempts + settings.job_max_attempts
        job.run_after = datetime.now(UTC)
        job.completed_at = None
        job.last_error = None
    elif job.status not in {"pending", "running"}:
        raise HTTPException(409, "Assessment job is terminal")
    result = await request_view(session, row)
    await session.commit()
    return result


@router.get(
    "/items/{item_id}/assessments/{assessment_id}/debug", dependencies=[Depends(REVIEW_SCOPE)]
)
async def debug_receipt(
    item_id: UUID,
    assessment_id: UUID,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    context: ResolvedMemoryContext = Depends(resolve_memory_context),  # noqa: B008
) -> dict[str, object]:
    """Read bounded provider fields and evidence references with review authority."""
    await eligible_item(session, item_id, context)
    row = await session.scalar(
        select(MemoryAssessment).where(
            MemoryAssessment.id == assessment_id,
            MemoryAssessment.memory_item_id == item_id,
        )
    )
    if row is None:
        raise HTTPException(404, "Assessment not found")
    return {
        "receipt": row.receipt,
        "canonical_hash": row.canonical_hash,
        "canonicalization_version": "pg-jsonb-v1",
    }
