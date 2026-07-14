from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import and_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from engram.api.routes.memory import _insert_item_event
from engram.auth import READ_SCOPE, WRITE_SCOPE
from engram.authority import MemoryAuthority
from engram.db import get_session
from engram.memory_access import apply_read_eligibility, eligibility_expression
from engram.models import KgTriple, MemoryItem, Principal
from engram.trust_policy import resolve_trust_defaults

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
    triple_id: UUID
    reason: str | None = None


class KgInvalidateResponse(BaseModel):
    status: Literal["invalidated", "unchanged"]
    count: int
    triple_id: UUID
    source_item_id: UUID
    valid_to: datetime
    event: dict[str, Any] | None


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
    result = await session.execute(
        select(Principal.type).where(
            Principal.id == principal_id,
            Principal.tenant_id == await _resolve_tenant_id(session),
        )
    )
    principal_type = result.scalar_one_or_none()
    if principal_type is None:
        raise HTTPException(status_code=403, detail="principal not found")
    return principal_id, principal_type


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


@router.post(
    "/kg", response_model=KgAddResponse, status_code=201, dependencies=[Depends(WRITE_SCOPE)]
)
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
        source_trust, source_prior, _ = await resolve_trust_defaults(
            session, tenant_id, "extraction", principal[1]
        )
        item = MemoryItem(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            principal_id=principal_id,
            content=f"KG triple: {req.subject} {req.predicate} {req.object}",
            content_hash=f"kg-auto-{uuid.uuid4().hex}",
            kind="fact",
            visibility="workspace",
            review_status="proposed",
            memory_confidence=source_prior,
            source_trust=source_trust,
            source_confidence_prior=source_prior,
            importance=0.5,
            source_type="extraction",
            authority=MemoryAuthority.INFERRED,
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


@router.get(
    "/kg/query", response_model=list[KgTripleOut], dependencies=[Depends(READ_SCOPE)]
)
async def query_kg(
    entity: str,
    direction: str = "both",
    as_of: datetime | None = None,
    predicate: str | None = None,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    tenant_id: UUID = Depends(_resolve_tenant_id),  # noqa: B008
    principal: tuple[UUID, str] = Depends(_resolve_principal),  # noqa: B008
) -> list[KgTripleOut]:
    principal_id = principal[0]

    conditions = [
        KgTriple.tenant_id == tenant_id,
        MemoryItem.tenant_id == tenant_id,
        eligibility_expression(principal_id),
    ]
    if direction == "outbound":
        conditions.append(KgTriple.subject == entity)
    elif direction == "inbound":
        conditions.append(KgTriple.object == entity)
    else:
        conditions.append(or_(KgTriple.subject == entity, KgTriple.object == entity))

    if predicate is not None:
        conditions.append(KgTriple.predicate == predicate)

    if as_of is not None:
        conditions.extend(
            [
                KgTriple.valid_from <= as_of,
                or_(KgTriple.valid_to.is_(None), KgTriple.valid_to > as_of),
            ]
        )
    else:
        conditions.append(KgTriple.valid_to.is_(None))

    stmt = (
        select(KgTriple)
        .join(
            MemoryItem,
            and_(
                MemoryItem.id == KgTriple.source_item_id,
                MemoryItem.tenant_id == KgTriple.tenant_id,
            ),
        )
        .where(*conditions)
        .order_by(KgTriple.created_at.desc(), KgTriple.id.desc())
    )
    triples = (await session.execute(stmt)).scalars().all()
    return [_row_to_triple_out(vars(triple)) for triple in triples]


@router.post(
    "/kg/invalidate", response_model=KgInvalidateResponse, dependencies=[Depends(WRITE_SCOPE)]
)
async def invalidate_triple(
    req: KgInvalidateRequest,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    tenant_id: UUID = Depends(_resolve_tenant_id),  # noqa: B008
    principal: tuple[UUID, str] = Depends(_resolve_principal),  # noqa: B008
) -> KgInvalidateResponse:
    principal_id, principal_type = principal
    stmt = (
        select(KgTriple, MemoryItem)
        .join(
            MemoryItem,
            and_(
                MemoryItem.id == KgTriple.source_item_id,
                MemoryItem.tenant_id == KgTriple.tenant_id,
            ),
        )
        .where(
            KgTriple.id == req.triple_id,
            KgTriple.tenant_id == tenant_id,
            MemoryItem.tenant_id == tenant_id,
            eligibility_expression(principal_id),
        )
        .with_for_update(of=KgTriple)
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="KG triple not found")
    triple, source_item = row

    if principal_type not in ("user", "admin") and triple.principal_id != principal_id:
        raise HTTPException(status_code=403, detail="not authorized to invalidate this KG triple")

    if triple.valid_to is not None:
        return KgInvalidateResponse(
            status="unchanged",
            count=0,
            triple_id=triple.id,
            source_item_id=source_item.id,
            valid_to=triple.valid_to,
            event=None,
        )

    invalidated_at = datetime.now(UTC)
    details = {
        "triple_id": str(triple.id),
        "previous_valid_to": None,
        "new_valid_to": invalidated_at.isoformat(),
        "subject": triple.subject,
        "predicate": triple.predicate,
        "object": triple.object,
    }
    triple.valid_to = invalidated_at
    event = await _insert_item_event(
        session,
        item_id=source_item.id,
        event_type="kg_invalidate",
        field_name="kg_triple.valid_to",
        old_value=None,
        new_value=json.dumps(details, sort_keys=True),
        actor_principal_id=principal_id,
        reason=req.reason,
    )
    await session.flush()
    await session.commit()

    return KgInvalidateResponse(
        status="invalidated",
        count=1,
        triple_id=triple.id,
        source_item_id=source_item.id,
        valid_to=invalidated_at,
        event=event,
    )


@router.get(
    "/kg/timeline", response_model=KgTimelineResponse, dependencies=[Depends(READ_SCOPE)]
)
async def kg_timeline(
    entity: str | None = None,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    tenant_id: UUID = Depends(_resolve_tenant_id),  # noqa: B008
    principal: tuple[UUID, str] = Depends(_resolve_principal),  # noqa: B008
) -> KgTimelineResponse:
    principal_id = principal[0]

    conditions = [
        KgTriple.tenant_id == tenant_id,
        MemoryItem.tenant_id == tenant_id,
        eligibility_expression(principal_id),
    ]

    if entity is not None:
        conditions.append(or_(KgTriple.subject == entity, KgTriple.object == entity))

    stmt = (
        select(KgTriple)
        .join(
            MemoryItem,
            and_(
                MemoryItem.id == KgTriple.source_item_id,
                MemoryItem.tenant_id == KgTriple.tenant_id,
            ),
        )
        .where(*conditions)
        .order_by(
            KgTriple.valid_from.asc().nulls_last(),
            KgTriple.created_at.asc(),
            KgTriple.id.asc(),
        )
        .limit(limit)
    )
    triples = (await session.execute(stmt)).scalars().all()
    facts = [_row_to_triple_out(vars(triple)) for triple in triples]

    return KgTimelineResponse(facts=facts, total=len(facts))
