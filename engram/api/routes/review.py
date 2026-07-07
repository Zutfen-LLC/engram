"""Review, verification, and conflict resolution endpoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
    _resolve_workspace_id,
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


class StaleItem(BaseModel):
    id: UUID
    content: str
    kind: str
    wing: str | None = None
    room: str | None = None
    review_status: str
    importance: float
    memory_confidence: float
    last_recalled_at: datetime | None = None
    recall_count: int
    created_at: datetime
    valid_from: datetime
    last_verified_at: datetime | None = None


class StaleListResponse(BaseModel):
    items: list[StaleItem]
    total: int
    days: int


class BulkArchiveRequest(BaseModel):
    item_ids: list[UUID]
    reason: str | None = None
    actor_principal_id: UUID | None = None


class BulkArchiveResponse(BaseModel):
    archived: list[UUID]
    archived_count: int
    skipped: list[UUID]
    skipped_count: int


class ReviewStatsResponse(BaseModel):
    by_review_status: dict[str, int]
    by_kind: dict[str, int]
    by_confidence: dict[str, int]
    total: int


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


@router.get("/review/stale", response_model=StaleListResponse)
async def stale_items(
    days: int = 90,
    workspace: str | None = None,
    kind: str | None = None,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> StaleListResponse:
    """Active items not recalled in N days.

    An item counts as stale when ``COALESCE(last_recalled_at, valid_from)`` is
    older than ``days`` ago. A never-recalled item is measured from
    ``valid_from`` — a NULL ``last_recalled_at`` does not exempt it. Only
    ``active`` items that are not invalidated or superseded are considered.
    """
    if days < 0:
        raise HTTPException(status_code=422, detail="days must be non-negative")
    tenant_id = await _resolve_tenant_id(session)
    limit = max(1, min(limit, 500))
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()

    clauses = [
        "tenant_id = :tenant_id",
        "review_status = 'active'",
        "valid_to IS NULL",
        "superseded_by IS NULL",
        "COALESCE(last_recalled_at, valid_from) < :cutoff",
    ]
    params: dict[str, Any] = {"tenant_id": str(tenant_id), "cutoff": cutoff, "limit": limit}
    if workspace is not None:
        ws_id = await _resolve_workspace_id(session, tenant_id, workspace)
        clauses.append("workspace_id = :workspace_id")
        params["workspace_id"] = str(ws_id)
    if kind is not None:
        clauses.append("kind = :kind")
        params["kind"] = kind
    sql = (
        "SELECT id, content, kind, wing, room, review_status, importance, "
        "memory_confidence, last_recalled_at, recall_count, created_at, "
        "valid_from, last_verified_at "
        "FROM memory_items WHERE "
        + " AND ".join(clauses)
        + " ORDER BY COALESCE(last_recalled_at, valid_from) ASC, created_at ASC "
        "LIMIT :limit"
    )
    rows = (await session.execute(text(sql), params)).mappings().all()
    items = [StaleItem(**dict(row)) for row in rows]
    return StaleListResponse(items=items, total=len(items), days=days)


@router.get("/review/stats", response_model=ReviewStatsResponse)
async def review_stats(
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ReviewStatsResponse:
    """Hygiene report: counts by review_status, kind, and confidence buckets.

    Counts only current memories (``valid_to IS NULL``). Confidence buckets:
    low (< 0.4), medium (0.4–< 0.7), high (>= 0.7).
    """
    tenant_id = await _resolve_tenant_id(session)
    params = {"tenant_id": str(tenant_id)}

    status_rows = (
        await session.execute(
            text(
                "SELECT review_status, count(*) FROM memory_items "
                "WHERE tenant_id = :tenant_id AND valid_to IS NULL "
                "GROUP BY review_status"
            ),
            params,
        )
    ).all()
    by_review_status: dict[str, int] = {str(row[0]): int(row[1]) for row in status_rows}

    kind_rows = (
        await session.execute(
            text(
                "SELECT kind, count(*) FROM memory_items "
                "WHERE tenant_id = :tenant_id AND valid_to IS NULL "
                "GROUP BY kind"
            ),
            params,
        )
    ).all()
    by_kind: dict[str, int] = {str(row[0]): int(row[1]) for row in kind_rows}

    conf_rows = (
        await session.execute(
            text(
                "SELECT CASE "
                "WHEN memory_confidence < 0.4 THEN 'low' "
                "WHEN memory_confidence < 0.7 THEN 'medium' "
                "ELSE 'high' END AS bucket, count(*) "
                "FROM memory_items "
                "WHERE tenant_id = :tenant_id AND valid_to IS NULL "
                "GROUP BY bucket"
            ),
            params,
        )
    ).all()
    by_confidence: dict[str, int] = {"low": 0, "medium": 0, "high": 0}
    for row in conf_rows:
        by_confidence[str(row[0])] = int(row[1])

    total = sum(by_review_status.values())
    return ReviewStatsResponse(
        by_review_status=by_review_status,
        by_kind=by_kind,
        by_confidence=by_confidence,
        total=total,
    )


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


@router.post("/items/bulk-archive", response_model=BulkArchiveResponse)
async def bulk_archive(
    req: BulkArchiveRequest,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> BulkArchiveResponse:
    """Archive multiple items: set review_status='archived'.

    Writes an ``item_events`` audit row per changed item, then updates the
    column. Items already in a terminal state (``archived``/``rejected``) and
    items not found in the caller's tenant are skipped. Archival excludes
    items from default recall without invalidating them (they're still true,
    just not actively useful).
    """
    tenant_id = await _resolve_tenant_id(session)
    requested = list(dict.fromkeys(req.item_ids))  # de-dup, preserve order
    if not requested:
        return BulkArchiveResponse(
            archived=[], archived_count=0, skipped=[], skipped_count=0
        )

    fetch_placeholders: list[str] = []
    fetch_params: dict[str, Any] = {"tenant_id": str(tenant_id)}
    for i, item_id in enumerate(requested):
        fetch_placeholders.append(f":id{i}")
        fetch_params[f"id{i}"] = str(item_id)
    fetch_sql = text(
        "SELECT id, review_status, principal_id FROM memory_items "
        "WHERE tenant_id = :tenant_id AND "
        f"CAST(id AS TEXT) IN ({', '.join(fetch_placeholders)})"
    )
    rows = (await session.execute(fetch_sql, fetch_params)).all()
    found: dict[str, tuple[str, object]] = {
        str(row[0]): (str(row[1]), row[2]) for row in rows
    }

    to_archive: list[UUID] = []
    skipped: list[UUID] = []
    for item_id in requested:
        entry = found.get(str(item_id))
        if entry is None or entry[0] in ("archived", "rejected"):
            skipped.append(item_id)
        else:
            to_archive.append(item_id)

    for item_id in to_archive:
        old_status, principal_id = found[str(item_id)]
        actor = req.actor_principal_id or UUID(str(principal_id))
        await _insert_item_event(
            session,
            item_id=item_id,
            event_type="review_change",
            field_name="review_status",
            old_value=old_status,
            new_value="archived",
            actor_principal_id=actor,
            reason=req.reason,
        )

    if to_archive:
        update_placeholders: list[str] = []
        update_params: dict[str, Any] = {"tenant_id": str(tenant_id)}
        for i, item_id in enumerate(to_archive):
            update_placeholders.append(f":uid{i}")
            update_params[f"uid{i}"] = str(item_id)
        await session.execute(
            text(
                "UPDATE memory_items SET review_status = 'archived' "
                "WHERE tenant_id = :tenant_id AND "
                f"CAST(id AS TEXT) IN ({', '.join(update_placeholders)})"
            ),
            update_params,
        )

    await session.commit()
    return BulkArchiveResponse(
        archived=to_archive,
        archived_count=len(to_archive),
        skipped=skipped,
        skipped_count=len(skipped),
    )

