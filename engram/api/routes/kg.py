from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from engram.db import get_session
from engram.memory_access import apply_read_eligibility
from engram.models import KgTriple, MemoryItem

router = APIRouter()


class KgAddRequest(BaseModel):
    subject: str
    predicate: str
    object: str
    workspace: str | None = None
    valid_from: str | None = None
    source_item_id: UUID | None = None
    confidence: float = 0.5


class KgAddResponse(BaseModel):
    id: UUID
    triple: dict[str, Any]
    source_item_id: UUID | None = None
    memory_item: dict[str, Any] | None = None


class KgTripleOut(BaseModel):
    id: UUID
    subject: str
    predicate: str
    object: str
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    source_item_id: UUID | None = None
    confidence: float
    review_status: str
    created_at: datetime
    trust_annotation: str | None = None


class KgInvalidateRequest(BaseModel):
    subject: str
    predicate: str
    object: str


class KgInvalidateResponse(BaseModel):
    status: str
    count: int


class KgTimelineResponse(BaseModel):
    facts: list[KgTripleOut]
    total: int


async def _resolve_tenant_id(
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> UUID:
    tid_row = await session.execute(text("SELECT current_setting('app.tenant_id', true)"))
    tid_str = tid_row.scalar()
    if not tid_str:
        raise HTTPException(status_code=403, detail="no tenant context")
    return UUID(tid_str)


async def _resolve_principal(
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> tuple[UUID, str]:
    pid_row = await session.execute(text("SELECT current_setting('app.principal_id', true)"))
    pid_str = pid_row.scalar()
    if not pid_str:
        raise HTTPException(status_code=403, detail="no principal context")
    principal_id = UUID(pid_str)
    return principal_id, "agent"


async def _resolve_workspace_id(
    session: AsyncSession,
    tenant_id: UUID,
    workspace_slug: str | None,
) -> UUID | None:
    if workspace_slug is None:
        return None
    result = await session.execute(
        text("SELECT id FROM workspaces WHERE tenant_id = :tid AND slug = :slug"),
        {"tid": str(tenant_id), "slug": workspace_slug},
    )
    ws_id = result.scalar_one_or_none()
    if ws_id is None:
        raise HTTPException(status_code=422, detail=f"workspace '{workspace_slug}' not found")
    return UUID(str(ws_id))


def _row_to_triple_out(row: Any) -> KgTripleOut:
    trust = None
    if row.get("review_status") == "proposed":
        trust = "proposed"
    return KgTripleOut(
        id=row["id"],
        subject=row["subject"],
        predicate=row["predicate"],
        object=row["object"],
        valid_from=row.get("valid_from"),
        valid_to=row.get("valid_to"),
        source_item_id=row.get("source_item_id"),
        confidence=float(row.get("confidence", 0.5)),
        review_status=row.get("review_status", "proposed"),
        created_at=row.get("created_at"),
        trust_annotation=trust,
    )


@router.post("/kg", response_model=KgAddResponse, status_code=201)
async def add_triple(
    req: KgAddRequest,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    tenant_id: UUID = Depends(_resolve_tenant_id),  # noqa: B008
    principal: tuple[UUID, str] = Depends(_resolve_principal),  # noqa: B008
) -> KgAddResponse:
    principal_id = principal[0]
    workspace_id = await _resolve_workspace_id(session, tenant_id, req.workspace)

    source_item_id: UUID | None = req.source_item_id
    memory_item = None

    if source_item_id is not None:
        result = await session.execute(
            apply_read_eligibility(
                select(MemoryItem).where(MemoryItem.id == source_item_id),
                tenant_id=tenant_id,
                principal_id=principal_id,
            )
        )
        existing = result.scalar_one_or_none()
        if existing is None:
            raise HTTPException(status_code=404, detail="Item not found")
    else:
        item = MemoryItem(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            principal_id=principal_id,
            content=f"KG triple: {req.subject} {req.predicate} {req.object}",
            content_hash=f"kg-auto-{uuid.uuid4().hex}",
            kind="fact",
            visibility="workspace",
            review_status="proposed",
            memory_confidence=req.confidence,
            source_trust=0.5,
            importance=0.5,
            source_type="extraction",
            sensitivity="normal",
        )
        session.add(item)
        await session.flush()
        source_item_id = item.id
        memory_item = {
            "id": str(item.id),
            "content": item.content,
            "kind": item.kind,
            "review_status": item.review_status,
        }

    triple = KgTriple(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        principal_id=principal_id,
        subject=req.subject,
        predicate=req.predicate,
        object=req.object,
        source_item_id=source_item_id,
        confidence=req.confidence,
        review_status="proposed",
    )
    session.add(triple)
    await session.flush()
    await session.refresh(triple)

    triple_dict = {
        "id": str(triple.id),
        "subject": triple.subject,
        "predicate": triple.predicate,
        "object": triple.object,
        "valid_from": triple.valid_from.isoformat() if triple.valid_from else None,
        "valid_to": triple.valid_to.isoformat() if triple.valid_to else None,
        "source_item_id": str(triple.source_item_id) if triple.source_item_id else None,
        "confidence": triple.confidence,
        "review_status": triple.review_status,
        "created_at": triple.created_at.isoformat() if triple.created_at else None,
    }

    await session.commit()

    return KgAddResponse(
        id=triple.id,
        triple=triple_dict,
        source_item_id=source_item_id,
        memory_item=memory_item,
    )


@router.get("/kg/query", response_model=list[KgTripleOut])
async def query_kg(
    entity: str,
    direction: str = "both",
    as_of: str | None = None,
    predicate: str | None = None,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    tenant_id: UUID = Depends(_resolve_tenant_id),  # noqa: B008
    principal: tuple[UUID, str] = Depends(_resolve_principal),  # noqa: B008
) -> list[KgTripleOut]:
    principal_id = principal[0]

    conditions = [text("k.tenant_id = :tenant_id")]
    params: dict[str, Any] = {"tenant_id": str(tenant_id), "principal_id": str(principal_id)}

    if direction == "outbound":
        conditions.append(text("k.subject = :entity"))
    elif direction == "inbound":
        conditions.append(text("k.object = :entity"))
    else:
        conditions.append(text("(k.subject = :entity OR k.object = :entity)"))

    params["entity"] = entity

    if predicate is not None:
        conditions.append(text("k.predicate = :predicate"))
        params["predicate"] = predicate

    if as_of is not None:
        conditions.append(
            text(f"k.valid_from <= ({as_of}) ::timestamptz")
        )
        conditions.append(
            text(f"(k.valid_to IS NULL OR k.valid_to > ({as_of}) ::timestamptz)")
        )
    else:
        conditions.append(text("k.valid_to IS NULL"))

    where_clause = " AND ".join(str(c) for c in conditions)

    sql = f"""
        SELECT k.*, mi.visibility AS source_visibility, mi.principal_id AS source_principal_id
        FROM kg_triples k
        LEFT JOIN memory_items mi ON mi.id = k.source_item_id
        WHERE {where_clause}
        ORDER BY k.created_at DESC
    """

    rows = (await session.execute(text(sql), params)).mappings().all()

    results: list[KgTripleOut] = []
    for row in rows:
        source_visibility = row.get("source_visibility")
        source_principal_id = row.get("source_principal_id")
        is_private = source_visibility == "private"
        is_other = source_principal_id is not None and str(source_principal_id) != str(principal_id)
        if is_private and is_other:
            continue
        results.append(_row_to_triple_out(row))

    return results


@router.post("/kg/invalidate", response_model=KgInvalidateResponse)
async def invalidate_triple(
    req: KgInvalidateRequest,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    tenant_id: UUID = Depends(_resolve_tenant_id),  # noqa: B008
) -> KgInvalidateResponse:
    result = await session.execute(
        text(
            "UPDATE kg_triples SET valid_to = now() "
            "WHERE tenant_id = :tenant_id "
            "AND subject = :subject AND predicate = :predicate AND object = :object "
            "AND valid_to IS NULL"
        ),
        {
            "tenant_id": str(tenant_id),
            "subject": req.subject,
            "predicate": req.predicate,
            "object": req.object,
        },
    )
    count = result.rowcount  # type: ignore[attr-defined]
    await session.commit()

    return KgInvalidateResponse(status="invalidated" if count > 0 else "not_found", count=count)


@router.get("/kg/timeline", response_model=KgTimelineResponse)
async def kg_timeline(
    entity: str | None = None,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    tenant_id: UUID = Depends(_resolve_tenant_id),  # noqa: B008
    principal: tuple[UUID, str] = Depends(_resolve_principal),  # noqa: B008
) -> KgTimelineResponse:
    principal_id = principal[0]

    conditions = [text("k.tenant_id = :tenant_id")]
    params: dict[str, Any] = {
        "tenant_id": str(tenant_id),
        "principal_id": str(principal_id),
        "limit": limit,
    }

    if entity is not None:
        conditions.append(text("(k.subject = :entity OR k.object = :entity)"))
        params["entity"] = entity

    where_clause = " AND ".join(str(c) for c in conditions)

    sql = f"""
        SELECT k.*, mi.visibility AS source_visibility, mi.principal_id AS source_principal_id
        FROM kg_triples k
        LEFT JOIN memory_items mi ON mi.id = k.source_item_id
        WHERE {where_clause}
        ORDER BY k.valid_from ASC NULLS LAST, k.created_at ASC
        LIMIT :limit
    """

    rows = (await session.execute(text(sql), params)).mappings().all()

    facts: list[KgTripleOut] = []
    for row in rows:
        source_visibility = row.get("source_visibility")
        source_principal_id = row.get("source_principal_id")
        is_private = source_visibility == "private"
        is_other = source_principal_id is not None and str(source_principal_id) != str(principal_id)
        if is_private and is_other:
            continue
        facts.append(_row_to_triple_out(row))

    return KgTimelineResponse(facts=facts, total=len(facts))
