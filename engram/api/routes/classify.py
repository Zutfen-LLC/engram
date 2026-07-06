"""Classification endpoint: suggest kind, wing, room for raw text.

Skeleton — implementation in Phase 1 PR (T18).
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter()


class ClassifyRequest(BaseModel):
    content: str
    context: str | None = None  # optional conversation excerpt or source_type hint
    workspace: str | None = None


class ClassifyResponse(BaseModel):
    suggested_kind: str
    suggested_wing: str | None = None
    suggested_room: str | None = None
    suggested_visibility: str = "workspace"
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


@router.post("/classify", response_model=ClassifyResponse)
async def classify(req: ClassifyRequest):
    """Classify raw text: suggest kind, wing, room, visibility.

    Uses LLM if configured, otherwise falls back to tenant's rule-based classification.
    """
    # TODO (T18): implement rule-based classification + optional LLM call
    raise NotImplementedError("classify not yet implemented")


@router.get("/classification/rules")
async def list_rules():
    """List tenant's classification rules."""
    raise NotImplementedError


@router.post("/classification/rules")
async def create_rule(req: RuleCreate):
    """Create or update a classification rule."""
    raise NotImplementedError


@router.delete("/classification/rules/{rule_id}")
async def delete_rule(rule_id: str):
    """Delete a classification rule."""
    raise NotImplementedError
