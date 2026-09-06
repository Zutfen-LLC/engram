"""Append-only assessment requests, evidence identity, and deterministic selection."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import literal, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from engram.assessment_schema import (
    AssessmentContract,
    AssessmentDimensions,
    AssessmentHistory,
    AssessmentView,
    JobStatus,
    ReassessRequest,
    ReassessResponse,
)
from engram.config import settings
from engram.extraction import digest
from engram.jobs import enqueue_job_in_transaction
from engram.memory_context import ResolvedMemoryContext, record_job_execution_context
from engram.models import (
    AssessmentRequest,
    Job,
    MemoryAssessment,
    MemoryItem,
)
from engram.provider_clients import resolve_classification_provider


def current_contract() -> AssessmentContract:
    """Identify the deployed inference contract without including credentials."""
    provider = resolve_classification_provider()
    from engram.assessment_calibration import load_profiles

    profiles = load_profiles(settings.assessment_calibration_profiles_path)
    return AssessmentContract(
        provider=provider.provider_adapter,
        model=provider.model,
        config_version=digest(
            {
                "host": provider.sanitized_provider_host,
                "endpoint_hash": digest(provider.base_url),
                "temperature": 0,
                "max_tokens": 1024,
                "input_limit": 16000,
            }
        ),
        calibration_version=settings.assessment_calibration_version,
        calibration_digest=digest([p.model_dump(mode="json") for p in profiles])
        if profiles
        else None,
    )


async def evidence_snapshot(
    session: AsyncSession,
    item: MemoryItem,
    context: ResolvedMemoryContext,
) -> dict[str, object]:
    """Hash authorized provenance and current governance state without copying text."""
    from engram.classification_evidence import hash_content

    rows = (
        (
            await session.execute(
                text("SELECT * FROM assessment_evidence_manifest(:item, :tenant)"),
                {"item": item.id, "tenant": item.tenant_id},
            )
        )
        .mappings()
        .all()
    )
    if len(rows) > 64:
        raise ValueError("assessment evidence exceeds 64 extraction links")
    roots: list[dict[str, object]] = []
    for row in rows:
        roots.append(
            {
                "run_id": str(row["run_id"]),
                "candidate_id": str(row["candidate_id"]),
                "receipt_hash": row["receipt_hash"],
                "evidence_root": row["evidence_root"],
                "assertion_mode": row["assertion_mode"] or "unknown",
                "origin": row["origin"] or "unknown",
                "asserting_principal_id": row["asserting_principal_id"],
            }
        )
    return {
        "content_hash": item.content_hash,
        "actual_content_hash": hash_content(item.content),
        "kind": item.kind,
        "source_type": item.source_type,
        "principal_id": str(item.principal_id),
        "workspace_id": str(item.workspace_id),
        "visibility": item.visibility,
        "review_status": item.review_status,
        "human_verified": item.human_verified,
        "verified_by": str(item.verified_by),
        "verified_at": item.verified_at.isoformat() if item.verified_at else None,
        "conflict_status": item.conflict_resolution_status,
        "conflicts_with": str(item.conflicts_with_item_id),
        "sensitivity": item.sensitivity,
        "authority": item.authority,
        "valid_to": item.valid_to.isoformat() if item.valid_to else None,
        "context_digest": digest(
            {
                "workspace_id": str(item.workspace_id),
                "evidence_contract": "assessment-evidence-manifest-v1",
                "version": context.version,
            }
        ),
        "evidence_roots": roots,
    }


def live_item(item: MemoryItem) -> bool:
    return (
        item.valid_to is None
        and item.review_status not in {"rejected", "archived"}
        and item.superseded_by is None
    )


async def request_assessment(
    session: AsyncSession,
    item: MemoryItem,
    context: ResolvedMemoryContext,
    request: ReassessRequest,
) -> AssessmentRequest:
    """Create one durable request per purpose, contract, and authorized input digest."""
    target = request.target or current_contract()
    contract_hash = digest(target.model_dump(mode="json"))
    evidence = await evidence_snapshot(session, item, context)
    input_digest = digest(evidence)
    identity = digest(
        [str(item.tenant_id), str(item.id), request.purpose, contract_hash, input_digest]
    )
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": identity}
    )
    existing = await session.scalar(
        select(AssessmentRequest).where(
            AssessmentRequest.tenant_id == item.tenant_id,
            AssessmentRequest.memory_item_id == item.id,
            AssessmentRequest.purpose == request.purpose,
            AssessmentRequest.contract_hash == contract_hash,
            AssessmentRequest.input_digest == input_digest,
        )
    )
    if existing is not None:
        return existing
    request_id = uuid4()
    execution_id = (
        await record_job_execution_context(session, context) if context.is_profile_bound else None
    )
    job_id = await enqueue_job_in_transaction(
        session,
        tenant_id=item.tenant_id,
        job_type="assessment.reassess",
        payload={
            "request_id": str(request_id),
            "memory_item_id": str(item.id),
            "principal_id": str(context.principal_id),
        },
        dedupe_key=identity,
    )
    result = AssessmentRequest(
        id=request_id,
        tenant_id=item.tenant_id,
        memory_item_id=item.id,
        principal_id=context.principal_id,
        execution_context_id=execution_id,
        purpose=request.purpose,
        reason=request.reason,
        target=target.model_dump(mode="json"),
        contract_hash=contract_hash,
        input_digest=input_digest,
        evidence=evidence,
        job_id=job_id,
    )
    session.add(result)
    await session.flush()
    return result


async def request_view(session: AsyncSession, request: AssessmentRequest) -> ReassessResponse:
    job = await session.get(Job, request.job_id)
    if job is None:
        raise ValueError("assessment job unavailable")
    return ReassessResponse(
        request_id=request.id,
        item_id=request.memory_item_id,
        job_id=request.job_id,
        job_status=cast(JobStatus, job.status),
        attempts=job.attempts,
        max_attempts=job.max_attempts,
        target=AssessmentContract.model_validate(request.target),
        input_digest=request.input_digest,
    )


def assessment_view(row: MemoryAssessment) -> AssessmentView:
    return AssessmentView.model_validate(
        {
            "assessment_id": row.id,
            "purpose": row.purpose,
            "schema_version": row.receipt["schema_version"],
            "contract_hash": row.contract_hash,
            "input_digest": row.input_digest,
            "created_at": row.created_at,
            "prior_assessment_id": row.prior_assessment_id,
            "state": row.state,
            "dimensions": AssessmentDimensions.model_validate(row.receipt["dimensions"]),
            "canonical_hash": row.canonical_hash,
        }
    )


async def assessment_history(
    session: AsyncSession,
    item: MemoryItem,
    context: ResolvedMemoryContext,
    *,
    limit: int = 50,
    before: UUID | None = None,
) -> AssessmentHistory:
    """Select only the operator-pinned contract and the current authorized input."""
    base = select(MemoryAssessment).where(
        MemoryAssessment.tenant_id == item.tenant_id,
        MemoryAssessment.memory_item_id == item.id,
    )
    history_query = base
    if before is not None:
        cursor = await session.scalar(base.where(MemoryAssessment.id == before))
        if cursor is None:
            raise ValueError("assessment cursor unavailable")
        from sqlalchemy import tuple_

        history_query = history_query.where(
            tuple_(MemoryAssessment.created_at, MemoryAssessment.id)
            < tuple_(literal(cursor.created_at), literal(cursor.id))
        )
    rows = list(
        (
            await session.scalars(
                history_query.order_by(
                    MemoryAssessment.created_at.desc(), MemoryAssessment.id.desc()
                ).limit(limit + 1)
            )
        ).all()
    )
    effective: dict[str, AssessmentView] = {}
    if (
        settings.assessment_selection_enabled
        and settings.assessment_effective_contract_hash
        and live_item(item)
    ):
        snapshot = await evidence_snapshot(session, item, context)
        eligible = (
            await session.scalars(
                base.where(
                    MemoryAssessment.contract_hash == settings.assessment_effective_contract_hash,
                    MemoryAssessment.input_digest == digest(snapshot),
                    MemoryAssessment.state == "completed",
                ).order_by(MemoryAssessment.created_at, MemoryAssessment.id)
            )
        ).all()
        for row in eligible:
            effective.setdefault(row.purpose, assessment_view(row))
    return AssessmentHistory(
        policy_version=settings.assessment_policy_version,
        effective=effective,
        assessments=[assessment_view(row) for row in rows[:limit]],
        next_before=rows[limit - 1].id if len(rows) > limit else None,
    )


async def effective_assessment_values(
    session: AsyncSession,
    item: MemoryItem,
    context: ResolvedMemoryContext,
) -> dict[str, Any]:
    """Use the same safe effective projection on detail and promotion preview."""
    if not settings.assessment_selection_enabled:
        return {}
    history = await assessment_history(session, item, context, limit=1)
    return {
        purpose: assessment.model_dump(mode="json")
        for purpose, assessment in history.effective.items()
    }


async def effective_assessment_selection(
    session: AsyncSession,
    item: MemoryItem,
    context: ResolvedMemoryContext,
    *,
    purpose: str = "combined",
) -> dict[str, Any]:
    """Expose the #157 selection result without collapsing rejected inputs.

    This helper is for policy simulation. The public effective projection stays
    limited to completed, exact assessments. The simulator also needs the
    reason that an assessment was not selected.
    """
    if not settings.assessment_selection_enabled:
        return {"selection_status": "disabled", "combined": None}
    rows = list(
        (
            await session.scalars(
                select(MemoryAssessment)
                .where(
                    MemoryAssessment.tenant_id == item.tenant_id,
                    MemoryAssessment.memory_item_id == item.id,
                    MemoryAssessment.purpose == purpose,
                )
                .order_by(MemoryAssessment.created_at.desc(), MemoryAssessment.id.desc())
            )
        ).all()
    )
    if not rows:
        return {"selection_status": "absent", "combined": None}
    if not settings.assessment_effective_contract_hash:
        return {"selection_status": "mismatched", "combined": None}
    snapshot = await evidence_snapshot(session, item, context)
    current_input_digest = digest(snapshot)
    exact_rows = [
        row
        for row in rows
        if row.contract_hash == settings.assessment_effective_contract_hash
        and row.input_digest == current_input_digest
    ]
    completed = next((row for row in exact_rows if row.state == "completed"), None)
    if completed is not None:
        return {
            "selection_status": "selected",
            "combined": assessment_view(completed).model_dump(mode="json"),
        }
    for state in ("failed", "disabled", "stale"):
        if any(row.state == state for row in exact_rows):
            return {"selection_status": state, "combined": None}
    if any(row.contract_hash == settings.assessment_effective_contract_hash for row in rows):
        return {"selection_status": "stale", "combined": None}
    return {"selection_status": "mismatched", "combined": None}
