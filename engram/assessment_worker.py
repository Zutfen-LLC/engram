"""Recoverable assessment jobs. Results never change memory content or governed kind."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from engram.assessment_calibration import CalibrationProfile, calibrate, load_profiles
from engram.assessment_schema import AssessmentContract, AssessmentDimensions
from engram.assessments import current_contract, evidence_snapshot, live_item
from engram.auth import Principal
from engram.config import settings
from engram.db import apply_rls_context
from engram.extraction import digest
from engram.memory_access import write_eligibility_expression
from engram.memory_context import (
    memory_context_from_execution_context,
    unrestricted_memory_context,
)
from engram.models import AssessmentRequest, Job, MemoryAssessment, MemoryItem


def normalize_evidence(
    dimensions: AssessmentDimensions,
    item: MemoryItem,
    evidence: dict[str, Any],
    target: AssessmentContract,
    provider_model: object,
    profiles: list[CalibrationProfile],
) -> None:
    """Derive factual support from recorded evidence, independently of retention."""
    roots = evidence.get("evidence_roots", [])
    modes = {r["assertion_mode"] for r in roots}
    origins = {r["origin"] for r in roots}
    dimensions.assertion_mode = next(iter(modes)) if len(modes) == 1 else "unknown"
    dimensions.origin = next(iter(origins)) if len(origins) == 1 else "unknown"
    if item.sensitivity != "normal" or item.authority >= 40:
        dimensions.risk = "high"
    # Verification is an observed binary feature. It is not a probability.
    verified = bool(item.human_verified and item.verified_by and item.verified_at)
    if item.review_status == "disputed" or item.conflict_resolution_status == "unresolved":
        dimensions.epistemic_state = "contested"
    elif item.kind == "diary":
        dimensions.epistemic_state = "not_applicable"
    elif roots or verified:
        dimensions.epistemic_state = "insufficient_evidence"
        if verified:
            dimensions.epistemic.raw_value = 1
    else:
        dimensions.epistemic_state = "unknown"
    profiles = profiles if provider_model == target.model else []
    for dimension in ("taxonomy", "retention", "epistemic"):
        score = getattr(dimensions, dimension)
        for profile in profiles:
            calibrated = calibrate(
                score.raw_value,
                profile=profile,
                contract=target,
                dimension=dimension,
                source_type=item.source_type,
                assertion_mode=dimensions.assertion_mode,
                kind=item.kind,
                risk=dimensions.risk,
            )
            if calibrated.status == "calibrated":
                setattr(dimensions, dimension, calibrated)
                break
    if (
        verified
        and dimensions.epistemic.status == "calibrated"
        and dimensions.epistemic_state == "insufficient_evidence"
    ):
        dimensions.epistemic_state = "supported"
    dimensions.reason_codes = [
        dimensions.epistemic_state,
        "risk_unknown" if dimensions.risk == "unknown" else "governed_risk",
        "independent_corroboration_unknown",
    ]


async def handle_assessment_reassess(session: AsyncSession, job: Job) -> None:
    """Serialize duplicate work and recheck item/evidence identity after inference."""
    if not settings.assessment_reassessment_enabled:
        raise RuntimeError("assessment rollout disabled")
    request_id = UUID(str(job.payload["request_id"]))
    principal_id = UUID(str(job.payload["principal_id"]))
    await apply_rls_context(session, tenant_id=job.tenant_id, principal_id=principal_id)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"assessment-worker:{request_id}"},
    )
    request = await session.scalar(
        select(AssessmentRequest).where(
            AssessmentRequest.id == request_id,
            AssessmentRequest.tenant_id == job.tenant_id,
            AssessmentRequest.principal_id == principal_id,
            AssessmentRequest.job_id == job.id,
        )
    )
    if request is None:
        raise RuntimeError("assessment request unavailable")
    completed = await session.scalar(
        select(MemoryAssessment.id).where(
            MemoryAssessment.request_id == request.id,
            MemoryAssessment.state.in_(("completed", "stale")),
        )
    )
    if completed is not None:
        await session.commit()
        return
    previous_attempt = await session.scalar(
        select(MemoryAssessment.id).where(
            MemoryAssessment.request_id == request.id,
            MemoryAssessment.attempt == job.attempts,
        )
    )
    if previous_attempt is not None:
        raise RuntimeError("assessment provider unavailable")
    context = (
        await memory_context_from_execution_context(
            session,
            request.execution_context_id,
            tenant_id=job.tenant_id,
        )
        if request.execution_context_id
        else unrestricted_memory_context(
            Principal(str(job.tenant_id), str(principal_id), ("read", "review")),
        )
    )
    query = (
        select(MemoryItem)
        .where(
            MemoryItem.id == request.memory_item_id,
            write_eligibility_expression(context),
        )
        .execution_options(populate_existing=True)
    )
    item = await session.scalar(query)
    if item is None:
        raise RuntimeError("assessment item unavailable")
    target = AssessmentContract.model_validate(request.target)
    if target != current_contract():
        raise RuntimeError("assessment target contract unavailable on this worker")
    profiles = load_profiles(settings.assessment_calibration_profiles_path)
    profile_digest = digest([p.model_dump(mode="json") for p in profiles]) if profiles else None
    if profile_digest != target.calibration_digest:
        raise RuntimeError("assessment calibration artifact changed")
    before = await evidence_snapshot(session, item, context)
    stale = not live_item(item) or digest(before) != request.input_digest
    dimensions = AssessmentDimensions(reason_codes=["provider_disabled"])
    state = "disabled"
    provider_details: dict[str, object] = {"status": "disabled", "model": None}
    if not stale and target.provider == "openai":
        from engram.assessment_provider import assess_content

        try:
            result = await assess_content(item.content, item.kind)
            dimensions.taxonomy.raw_value = result.values.taxonomy_value
            dimensions.suggested_kind = result.values.suggested_kind
            dimensions.retention.raw_value = result.values.retention_value
            dimensions.retention_disposition = result.values.retention_disposition
            dimensions.reason_codes = ["uncalibrated"]
            state = "completed"
            provider_details = {
                "status": "succeeded",
                "model": result.model,
                "raw_output": result.values.model_dump(mode="json"),
                "reliability": "uncalibrated",
            }
        except Exception as exc:
            state = "failed"
            dimensions = AssessmentDimensions(reason_codes=["provider_failed"])
            provider_details = {"status": "failed", "error_type": type(exc).__name__}

    # The item stays unlocked during provider work. Human review can proceed.
    item = await session.scalar(query.with_for_update())
    if item is None:
        raise RuntimeError("assessment item unavailable")
    after = await evidence_snapshot(session, item, context)
    if stale or not live_item(item) or digest(after) != request.input_digest:
        state = "stale"
        dimensions = AssessmentDimensions(reason_codes=["stale_input"])
    elif state == "completed":
        normalize_evidence(
            dimensions,
            item,
            request.evidence,
            target,
            provider_details.get("model"),
            profiles,
        )
    prior = await session.scalar(
        select(MemoryAssessment.id)
        .where(
            MemoryAssessment.memory_item_id == item.id,
            MemoryAssessment.purpose == request.purpose,
        )
        .order_by(MemoryAssessment.created_at.desc(), MemoryAssessment.id.desc())
        .limit(1)
    )
    assessment = MemoryAssessment(
        id=uuid4(),
        tenant_id=item.tenant_id,
        memory_item_id=item.id,
        request_id=request.id,
        attempt=job.attempts,
        purpose=request.purpose,
        contract_hash=request.contract_hash,
        input_digest=request.input_digest,
        state=state,
        prior_assessment_id=prior,
        receipt={
            "schema_version": target.schema_version,
            "target": request.target,
            "input_content_hash": request.evidence["content_hash"],
            "evidence": request.evidence,
            "actor_principal_id": str(request.principal_id),
            "reason": request.reason,
            "request_id": str(request.id),
            "dimensions": dimensions.model_dump(mode="json"),
            "provider_details": provider_details,
        },
    )
    session.add(assessment)
    await session.flush()
    if state == "completed":
        from engram.promotion import enqueue_promotion_evaluation

        # This durable follow-up uses the unchanged legacy promotion evaluator.
        await enqueue_promotion_evaluation(
            session,
            tenant_id=item.tenant_id,
            memory_item_id=item.id,
            trigger_type="classification_reassessed",
            trigger_id=str(assessment.id),
            execution_context_id=request.execution_context_id,
        )
    await session.commit()
    if state in {"failed", "disabled"}:
        # Preserve the failed receipt before the queue schedules its next attempt.
        raise RuntimeError("assessment provider unavailable")
