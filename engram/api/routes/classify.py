"""Classification endpoint: suggest kind, wing, room for raw text."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from engram.classification import ClassificationResult
from engram.classification import classify as classify_content
from engram.db import get_session

router = APIRouter()


class ClassifyRequest(BaseModel):
    content: str
    context: str | None = None  # optional conversation excerpt or source_type hint
    workspace: str | None = None


class ClassifyResponse(BaseModel):
    suggested_kind: str
    suggested_wing: str | None = None
    suggested_room: str | None = None
    # Advisory only. ``/v1/classify`` returns the suggestion; the actual
    # downward-only narrowing happens on ``/v1/remember``. ``None`` means the
    # classifier has no opinion and the caller's visibility should be preserved.
    suggested_visibility: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    rules_matched: list[str] = Field(default_factory=list)


class RuleCreate(BaseModel):
    name: str
    rule_type: str  # keyword_kind | keyword_wing | regex_skip | llm_hint
    pattern: str
    target_kind: str | None = None
    target_wing: str | None = None
    target_room: str | None = None
    priority: int = 100


async def _resolve_tenant_id(session: AsyncSession) -> UUID:
    row = await session.execute(text("SELECT current_setting('app.tenant_id', true)"))
    tenant_id = row.scalar()
    if not tenant_id:
        raise HTTPException(status_code=403, detail="no tenant context")
    return UUID(str(tenant_id))


@router.post("/classify", response_model=ClassifyResponse)
async def classify(
    req: ClassifyRequest,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ClassifyResponse:
    """Classify raw text: suggest kind, wing, room, visibility."""

    tenant_id = await _resolve_tenant_id(session)
    result: ClassificationResult = await classify_content(
        req.content, tenant_id, session, context=req.context
    )
    return ClassifyResponse(**result.model_dump(exclude={"provenance"}))


@router.get("/classification/rules", response_model=None)
async def list_rules() -> None:
    """List tenant classification rules."""
    raise NotImplementedError


@router.post("/classification/rules", response_model=None)
async def create_rule(req: RuleCreate) -> None:
    """Create or update a classification rule."""
    raise NotImplementedError


@router.delete("/classification/rules/{rule_id}", response_model=None)
async def delete_rule(rule_id: str) -> None:
    """Delete a classification rule."""
    raise NotImplementedError
