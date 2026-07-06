"""Review, verification, and conflict resolution endpoints.

Skeleton — implementation in Phase 1A/1B.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class ReviewChangeRequest(BaseModel):
    review_status: str  # proposed | active | disputed | rejected | archived
    reason: str | None = None
    review_notes: str | None = None


class ConflictResolution(BaseModel):
    resolution: str  # accepted | rejected | merged
    reason: str | None = None


@router.get("/review/queue")
async def review_queue(
    kind: str | None = None,
    workspace: str | None = None,
    limit: int = 50,
):
    """Items awaiting review (review_status='proposed')."""
    raise NotImplementedError


@router.get("/review/conflicts")
async def conflict_queue():
    """Items with unresolved conflicts."""
    raise NotImplementedError


@router.get("/review/stale")
async def stale_items(days: int = 90):
    """Active items not recalled in N days."""
    raise NotImplementedError


@router.get("/review/stats")
async def review_stats():
    """Counts by review_status, kind, confidence buckets."""
    raise NotImplementedError


@router.post("/items/{item_id}/review")
async def change_review_status(item_id: UUID, req: ReviewChangeRequest):
    """Change review_status (proposed → active, dispute, etc.). Writes item_event."""
    raise NotImplementedError


@router.post("/items/{item_id}/verify")
async def verify_item(item_id: UUID):
    """Mark item as human-verified."""
    raise NotImplementedError


@router.post("/items/{item_id}/resolve-conflict")
async def resolve_conflict(item_id: UUID, req: ConflictResolution):
    """Resolve a conflict (accept/reject/merge)."""
    raise NotImplementedError


@router.post("/items/bulk-archive")
async def bulk_archive(item_ids: list[UUID]):
    """Archive multiple items (set review_status='archived')."""
    raise NotImplementedError
