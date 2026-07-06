"""Agent diary endpoints.

Diaries are memory_items with kind='diary_entry', scoped by principal.
Skeleton — implementation in Phase 1.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class DiaryWrite(BaseModel):
    entry: str  # AAAK format or free text
    principal: str
    topic: str | None = None


@router.post("/diary")
async def write_diary(req: DiaryWrite):
    """Write a diary entry."""
    raise NotImplementedError


@router.get("/diary/{principal}")
async def read_diary(principal: str, limit: int = 10):
    """Read diary entries for a principal."""
    raise NotImplementedError
