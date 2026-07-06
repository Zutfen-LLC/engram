"""Knowledge graph endpoints: add, query, invalidate, timeline.

Skeleton — implementation in Phase 1 PR 5+.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class KgAddRequest(BaseModel):
    subject: str
    predicate: str
    object: str
    workspace: str | None = None
    valid_from: str | None = None


@router.post("/kg")
async def add_triple(req: KgAddRequest):
    """Add a knowledge graph triple."""
    raise NotImplementedError


@router.get("/kg/query")
async def query_kg(
    entity: str,
    direction: str = "both",
    as_of: str | None = None,
    predicate: str | None = None,
):
    """Query an entity's relationships."""
    raise NotImplementedError


@router.post("/kg/invalidate")
async def invalidate_triple(subject: str, predicate: str, object: str):
    """Mark a triple as no longer true."""
    raise NotImplementedError


@router.get("/kg/timeline")
async def kg_timeline(entity: str | None = None):
    """Chronological timeline of facts."""
    raise NotImplementedError
