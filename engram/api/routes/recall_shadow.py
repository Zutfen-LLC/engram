"""Authorized shadow comparison of candidate recall profiles (issue #160).

The only surface that can compute governed/exploratory packets before #162
certification. It is deliberately narrow:

* **Capability** — ``REVIEW_SCOPE`` (review-domain authority), consistent with
  the other privileged admission surfaces. Exploratory packets include
  proposals/unknown evidence that ordinary governed recall excludes; that is
  a broader trust surface and can never be reached with plain ``read``.
* **Tenant policy** — the tenant's explicit
  ``tenant_config.recall_profile_shadow_enabled`` allow. Capability is
  necessary but not sufficient: tenant denial wins even for a capable caller,
  and an absent config row fails closed.
* **Read-only** — the comparison writes no recall_logs row, no exposure
  counters, no receipts, and no promotion/evidence inputs; its results are
  non-authoritative by construction and can never change what
  ``POST /v1/recall`` serves (legacy until certification).

MCP does not expose this surface.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from engram.auth import REVIEW_SCOPE
from engram.db import get_session
from engram.memory_context import ResolvedMemoryContext, resolve_memory_context
from engram.recall_shadow import (
    RecallShadowProfileError,
    evaluate_recall_shadow_comparison,
    tenant_allows_candidate_profile_inspection,
)

router = APIRouter()

_TENANT_POLICY_DENIED_DETAIL = (
    "Tenant policy does not permit candidate recall-profile inspection "
    "(tenant_config.recall_profile_shadow_enabled)"
)


_ShadowProfile = Literal["governed", "exploratory"]


def _default_profiles() -> list[_ShadowProfile]:
    return ["governed"]


class RecallShadowCompareRequest(BaseModel):
    """One bounded legacy-vs-candidate comparison.

    ``profiles`` selects which uncertified candidate profiles to evaluate
    against the legacy packet; a certified profile can never appear here.
    """

    query: str = Field(min_length=1)
    workspace: str | None = None
    profiles: list[_ShadowProfile] = Field(
        default_factory=_default_profiles,
        min_length=1,
        max_length=2,
    )
    byte_budget: int | None = Field(default=None, ge=1)
    token_budget: int | None = Field(default=None, ge=1)
    item_budget: int | None = Field(default=None, ge=1)


class RecallShadowPacket(BaseModel):
    """One evaluated packet (authoritative legacy or a candidate).

    ``admission_diagnostics`` and ``v2_resolution`` are the issue #186
    binding: bounded, content-free per-candidate withhold diagnostics and the
    #158 V2 resolution summary. Legacy evaluates them to ``[]`` / ``None``.
    ``expansion`` is the issue #190 addition: the bounded admission-first
    relationship-expansion summary (contract version plus seed/neighbor/
    admission counts). Legacy evaluates it to ``None``.
    """

    profile: str
    scoring_version: str
    signals_version: str | None
    item_count: int
    byte_count: int
    candidate_count: int
    omitted_by_admission: dict[str, int]
    admission_diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    v2_resolution: dict[str, Any] | None = None
    expansion: dict[str, Any] | None = None
    effective_byte_budget: int | None
    effective_token_budget: int | None
    effective_item_budget: int | None
    items: list[dict[str, Any]]


class RecallShadowCandidate(RecallShadowPacket):
    """A candidate packet plus its id-level overlap with the legacy packet."""

    comparison: dict[str, list[str]]


class RecallShadowCompareResponse(BaseModel):
    """Non-authoritative comparison result — never a served packet."""

    shadow_comparison_version: str
    recall_profile_contract_version: str
    query: str
    authoritative_profile: str
    certified_serving_profiles: list[str]
    message: str | None
    workspace_id: str | None
    # Total eligible corpus across the compared profiles — nonzero exactly
    # when the comparison had anything to evaluate. Each packet (legacy and
    # candidates) carries its own precise candidate_count; legacy may be 0
    # while a candidate is non-empty.
    candidate_count: int
    embedding_outcome: str
    legacy: RecallShadowPacket | None
    candidates: list[RecallShadowCandidate]


@router.post(
    "/recall/shadow-compare",
    response_model=RecallShadowCompareResponse,
    dependencies=[Depends(REVIEW_SCOPE)],
)
async def recall_shadow_compare(
    req: RecallShadowCompareRequest,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    memory_context: ResolvedMemoryContext = Depends(resolve_memory_context),  # noqa: B008
) -> RecallShadowCompareResponse:
    """Compare the legacy packet with candidate profiles, changing nothing."""
    tenant_id = str(memory_context.tenant_id)
    if not await tenant_allows_candidate_profile_inspection(session, tenant_id):
        raise HTTPException(status_code=403, detail=_TENANT_POLICY_DENIED_DETAIL)
    try:
        payload = await evaluate_recall_shadow_comparison(
            session,
            memory_context=memory_context,
            workspace=req.workspace,
            query=req.query,
            candidate_profiles=list(req.profiles),
            byte_budget=req.byte_budget,
            token_budget=req.token_budget,
            item_budget=req.item_budget,
        )
    except RecallShadowProfileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RecallShadowCompareResponse.model_validate(payload)
