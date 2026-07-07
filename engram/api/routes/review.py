"""Review, verification, and conflict resolution endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from engram.api.routes.memory import (
    _insert_item_event,
    _now_dt,
    _require_item,
)
from engram.db import get_session
from engram.models import ItemEvent, MemoryItem

router = APIRouter()


class ReviewChangeRequest(BaseModel):
    review_status: str  # proposed | active | disputed | rejected | archived
    reason: str | None = None
    review_notes: str | None = None
    actor_principal_id: UUID | None = None


class VerifyRequest(BaseModel):
    verified_by: UUID | None = None
    reason: str | None = None


class ConflictResolution(BaseModel):
    resolution: Literal["accepted", "rejected", "merged"]
    reason: str | None = None


class ConflictItem(BaseModel):
    id: UUID
    content: str
    kind: str
    conflict_type: str
    conflicts_with_item_id: UUID
    conflict_resolution_status: str
    review_status: str
    created_at: datetime


class ConflictListResponse(BaseModel):
    items: list[ConflictItem]
    total: int


class ConflictResolutionResponse(BaseModel):
    id: UUID
    conflict_resolution_status: str
    resolved_at: datetime | None = None


async def _resolve_tenant_id(session: AsyncSession) -> UUID:
    row = await session.execute(text("SELECT current_setting('app.tenant_id', true)"))
    tid_str = row.scalar()
    if not tid_str:
        raise HTTPException(status_code=403, detail="no tenant context")
    return UUID(str(tid_str))


@router.get("/review/queue", response_model=None)
async def review_queue(
    kind: str | None = None,
    workspace: str | None = None,
    limit: int = 50,
) -> NoReturn:
    """Items awaiting review (review_status='proposed')."""
    raise NotImplementedError


@router.get("/review/conflicts", response_model=ConflictListResponse)
async def conflict_queue(
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ConflictListResponse:
    """Items with unresolved conflicts (conflict_resolution_status='unresolved')."""
    tenant_id = await _resolve_tenant_id(session)
    stmt = (
        select(
            MemoryItem.id,
            MemoryItem.content,
            MemoryItem.kind,
            MemoryItem.conflict_type,
            MemoryItem.conflicts_with_item_id,
            MemoryItem.conflict_resolution_status,
            MemoryItem.review_status,
            MemoryItem.created_at,
        )
        .where(
            MemoryItem.tenant_id == tenant_id,
            MemoryItem.conflict_resolution_status == "unresolved",
            MemoryItem.conflicts_with_item_id.is_not(None),
        )
        .order_by(MemoryItem.created_at.desc())
    )
    rows = (await session.execute(stmt)).mappings().all()
    items = [
        ConflictItem(
            id=row["id"],
            content=row["content"],
            kind=row["kind"],
            conflict_type=row["conflict_type"],
            conflicts_with_item_id=row["conflicts_with_item_id"],
            conflict_resolution_status=row["conflict_resolution_status"],
            review_status=row["review_status"],
            created_at=row["created_at"],
        )
        for row in rows
    ]
    return ConflictListResponse(items=items, total=len(items))


@router.get("/review/stale", response_model=None)
async def stale_items(days: int = 90) -> NoReturn:
    """Active items not recalled in N days."""
    raise NotImplementedError


@router.get("/review/stats", response_model=None)
async def review_stats() -> NoReturn:
    """Counts by review_status, kind, confidence buckets."""
    raise NotImplementedError


@router.post("/items/{item_id}/review", response_model=None)
async def change_review_status(
    item_id: UUID,
    req: ReviewChangeRequest,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    """Change review_status (proposed -> active, dispute, etc.). Writes item_event."""
    item = await _require_item(session, item_id)
    actor = req.actor_principal_id or UUID(str(item["principal_id"]))
    event = await _insert_item_event(
        session,
        item_id=item_id,
        event_type="review_change",
        field_name="review_status",
        old_value=item.get("review_status"),
        new_value=req.review_status,
        actor_principal_id=actor,
        reason=req.reason,
    )
    assignments = ["review_status = :review_status"]
    params: dict[str, Any] = {"review_status": req.review_status, "item_id": str(item_id)}
    if req.review_notes is not None:
        assignments.append("review_notes = :review_notes")
        params["review_notes"] = req.review_notes
    await session.execute(
        text(f"UPDATE memory_items SET {', '.join(assignments)} WHERE id = :item_id"),
        params,
    )
    updated = await _require_item(session, item_id)
    await session.commit()
    return {"item": updated, "event": event}


@router.post("/items/{item_id}/verify", response_model=None)
async def verify_item(
    item_id: UUID,
    req: VerifyRequest | None = None,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    """Mark item as human-verified."""
    item = await _require_item(session, item_id)
    now = _now_dt()
    verified_by = req.verified_by if req and req.verified_by else UUID(str(item["principal_id"]))
    reason = req.reason if req else None
    event = await _insert_item_event(
        session,
        item_id=item_id,
        event_type="verify",
        field_name="human_verified",
        old_value=item.get("human_verified"),
        new_value=True,
        actor_principal_id=verified_by,
        reason=reason,
    )
    await session.execute(
        text(
            "UPDATE memory_items SET human_verified = 1, verified_by = :verified_by, "
            "verified_at = :verified_at, last_verified_at = :last_verified_at WHERE id = :item_id"
        ),
        {
            "verified_by": str(verified_by),
            "verified_at": now.isoformat(),
            "last_verified_at": now.isoformat(),
            "item_id": str(item_id),
        },
    )
    updated = await _require_item(session, item_id)
    await session.commit()
    return {"item": updated, "event": event}


@router.post("/items/{item_id}/resolve-conflict", response_model=ConflictResolutionResponse)
async def resolve_conflict(
    item_id: UUID,
    req: ConflictResolution,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ConflictResolutionResponse:
    """Resolve a conflict (accept/reject/merge).

    Sets ``conflict_resolution_status`` and writes an ``item_event`` audit row.
    Returns 422 if the item has no unresolved conflict.
    """
    await _resolve_tenant_id(session)

    result = await session.execute(
        select(MemoryItem).where(MemoryItem.id == item_id)
    )
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    if item.conflict_resolution_status != "unresolved" or item.conflicts_with_item_id is None:
        raise HTTPException(
            status_code=422, detail="item has no unresolved conflict to resolve"
        )

    old_status = item.conflict_resolution_status
    item.conflict_resolution_status = req.resolution
    resolved_at = func.now()

    await session.execute(
        update(MemoryItem)
        .where(MemoryItem.id == item_id)
        .values(
            conflict_resolution_status=req.resolution,
            conflict_resolved_at=resolved_at,
        )
    )
    session.add(
        ItemEvent(
            item_id=item_id,
            event_type="conflict_resolution",
            field_name="conflict_resolution_status",
            old_value=old_status,
            new_value=req.resolution,
            reason=req.reason,
        )
    )
    await session.commit()

    refreshed = await session.execute(
        select(MemoryItem.conflict_resolved_at).where(MemoryItem.id == item_id)
    )
    return ConflictResolutionResponse(
        id=item_id,
        conflict_resolution_status=req.resolution,
        resolved_at=refreshed.scalar_one(),
    )


@router.post("/items/bulk-archive", response_model=None)
async def bulk_archive(item_ids: list[UUID]) -> NoReturn:
    """Archive multiple items (set review_status='archived')."""
    raise NotImplementedError

