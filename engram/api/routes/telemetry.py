"""Client lifecycle-summary telemetry (ENG-METER-001).

A narrow, authenticated, diagnostic endpoint. The server cannot see
locally guard-rejected candidates or candidates parked by a hooks adapter —
this endpoint lets a client report an aggregate summary of one lifecycle
invocation so the dogfood usage report can approximate the full funnel.

Client-reported summaries are diagnostic and untrusted: they are recorded with
``metadata.authoritative = false`` and must never be treated as an
authoritative billing record or used to gate anything.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from engram.auth import WRITE_SCOPE, Principal, get_current_principal
from engram.usage import record_client_lifecycle_summary

router = APIRouter()

LifecycleEvent = Literal["sync_turn", "pre_compress", "session_end"]


class LifecycleSummaryRequest(BaseModel):
    invocation_id: UUID
    event: LifecycleEvent
    extracted: int = Field(default=0, ge=0)
    guard_rejected: int = Field(default=0, ge=0)
    classified: int = Field(default=0, ge=0)
    promoted: int = Field(default=0, ge=0)
    parked: int = Field(default=0, ge=0)
    errors: int = Field(default=0, ge=0)
    candidate_bytes: int = Field(default=0, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)
    adapter_version: str | None = None


class LifecycleSummaryResponse(BaseModel):
    status: Literal["succeeded", "partial"]
    invocation_id: UUID


@router.post(
    "/telemetry/lifecycle",
    response_model=LifecycleSummaryResponse,
    status_code=202,
    dependencies=[Depends(WRITE_SCOPE)],
)
async def report_lifecycle_summary(
    req: LifecycleSummaryRequest,
    principal: Principal = Depends(get_current_principal),  # noqa: B008
) -> LifecycleSummaryResponse:
    """Record one client-reported lifecycle summary.

    Tenant and principal come from authentication only — never from the
    request body — so a caller can never spoof another tenant/principal's
    summary. Best-effort: a telemetry insert failure is swallowed inside
    :mod:`engram.usage` and never surfaces here (this endpoint is itself pure
    telemetry, so there is no business operation to protect from it).
    """
    status: Literal["succeeded", "partial"] = "partial" if req.errors > 0 else "succeeded"
    await record_client_lifecycle_summary(
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        operation=req.event,
        status=status,
        invocation_id=req.invocation_id,
        extracted=req.extracted,
        guard_rejected=req.guard_rejected,
        classified=req.classified,
        promoted=req.promoted,
        parked=req.parked,
        errors=req.errors,
        candidate_bytes=req.candidate_bytes,
        latency_ms=req.latency_ms,
        adapter_version=req.adapter_version,
    )
    return LifecycleSummaryResponse(status=status, invocation_id=req.invocation_id)
