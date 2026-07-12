"""Private diary routes with explicit ownership and truthful actor provenance."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from engram.auth import READ_SCOPE, WRITE_SCOPE, get_current_principal
from engram.auth import Principal as AuthPrincipal
from engram.authority import authority_label, derive_memory_authority
from engram.canonicalize import canonicalize, content_hash
from engram.db import get_session
from engram.models import ItemEvent, MemoryItem, Principal
from engram.safety import has_secrets
from engram.trust_policy import resolve_trust_defaults

router = APIRouter()


class DiaryWrite(BaseModel):
    entry: str
    topic: str | None = None
    principal: str | None = Field(
        default=None,
        deprecated=True,
        description="Legacy self-target hint only; cannot represent another principal.",
    )
    on_behalf_of_principal_id: UUID | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def targeting_is_unambiguous(self) -> DiaryWrite:
        if self.principal is not None and self.on_behalf_of_principal_id is not None:
            raise ValueError("principal cannot be combined with on_behalf_of_principal_id")
        return self


class DiaryEntry(BaseModel):
    id: UUID
    content: str
    topic: str | None
    created_at: datetime
    principal_id: UUID


class DiaryWriteResponse(BaseModel):
    id: UUID
    status: Literal["created", "deduped"]
    review_status: str
    principal_id: UUID
    actor_principal_id: UUID
    represented: bool
    authority: int
    authority_label: str


async def _tenant_id(session: AsyncSession) -> UUID:
    value = (
        await session.execute(text("SELECT current_setting('app.tenant_id', true)"))
    ).scalar()
    if not value:
        raise HTTPException(status_code=403, detail="no tenant context")
    return UUID(str(value))


async def _caller_row(session: AsyncSession, caller: AuthPrincipal) -> Principal:
    row = (
        await session.execute(
            select(Principal).where(
                Principal.id == UUID(caller.principal_id),
                Principal.tenant_id == UUID(caller.tenant_id),
                Principal.internal_key.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=403, detail="caller principal not found")
    return row


async def _principal_by_name(session: AsyncSession, tenant_id: UUID, name: str) -> Principal:
    row = (
        await session.execute(
            select(Principal).where(
                Principal.tenant_id == tenant_id,
                Principal.name == name,
                Principal.internal_key.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="principal not found")
    return row


def _response(
    item: MemoryItem, *, status: Literal["created", "deduped"], actor_id: UUID, represented: bool
) -> DiaryWriteResponse:
    return DiaryWriteResponse(
        id=item.id,
        status=status,
        review_status=item.review_status,
        principal_id=item.principal_id,
        actor_principal_id=actor_id,
        represented=represented,
        authority=item.authority,
        authority_label=authority_label(item.authority),
    )


async def _existing_response(session: AsyncSession, item: MemoryItem) -> DiaryWriteResponse:
    event = (
        await session.execute(
            select(ItemEvent).where(
                ItemEvent.item_id == item.id, ItemEvent.event_type == "diary_create"
            )
        )
    ).scalar_one()
    details = json.loads(event.new_value or "{}")
    if event.actor_principal_id is None:
        raise RuntimeError("diary_create event is missing its actor")
    return _response(
        item,
        status="deduped",
        actor_id=event.actor_principal_id,
        represented=bool(details.get("represented", False)),
    )


@router.post(
    "/diary",
    response_model=DiaryWriteResponse,
    status_code=201,
    dependencies=[Depends(WRITE_SCOPE)],
)
async def write_diary(
    req: DiaryWrite,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    caller: AuthPrincipal = Depends(get_current_principal),  # noqa: B008
) -> DiaryWriteResponse:
    if has_secrets(req.entry):
        raise HTTPException(
            status_code=422, detail="diary entry contains patterns matching secrets/credentials"
        )
    tenant_id = await _tenant_id(session)
    actor = await _caller_row(session, caller)
    owner = actor
    represented = req.on_behalf_of_principal_id is not None

    if req.principal is not None and req.principal != actor.name:
        raise HTTPException(status_code=422, detail="legacy principal must identify the caller")
    if represented:
        if actor.type != "admin" or not caller.has_scope("admin"):
            raise HTTPException(status_code=403, detail="represented diary writes require admin")
        target = (
            await session.execute(
                select(Principal).where(
                    Principal.id == req.on_behalf_of_principal_id,
                    Principal.tenant_id == tenant_id,
                    Principal.internal_key.is_(None),
                )
            )
        ).scalar_one_or_none()
        if target is None:
            raise HTTPException(status_code=404, detail="principal not found")
        owner = target

    source_type = "manual" if actor.type in {"user", "admin"} else "extraction"
    source_trust, confidence, review_status = await resolve_trust_defaults(
        session, tenant_id, source_type, actor.type
    )
    authority = derive_memory_authority(source_type=source_type, principal_type=actor.type)
    owner_id = owner.id
    chash = content_hash(canonicalize(req.entry))
    item = MemoryItem(
        tenant_id=tenant_id,
        workspace_id=None,
        principal_id=owner_id,
        content=req.entry,
        content_hash=chash,
        kind="diary_entry",
        subject_name=req.topic,
        visibility="private",
        review_status=review_status,
        memory_confidence=confidence,
        source_trust=source_trust,
        importance=0.4,
        source_type=source_type,
        authority=authority,
        sensitivity="normal",
    )
    session.add(item)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = (
            await session.execute(
                select(MemoryItem).where(
                    MemoryItem.tenant_id == tenant_id,
                    MemoryItem.principal_id == owner_id,
                    MemoryItem.workspace_id.is_(None),
                    MemoryItem.content_hash == chash,
                    MemoryItem.valid_to.is_(None),
                    MemoryItem.review_status != "rejected",
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise
        return await _existing_response(session, existing)

    details = {
        "owner_principal_id": str(owner.id),
        "actor_principal_id": str(actor.id),
        "represented": represented,
        "on_behalf_of_principal_id": str(owner.id) if represented else None,
        "source_type": source_type,
        "source_trust": source_trust,
        "memory_confidence": confidence,
        "authority": authority,
        "authority_label": authority_label(authority),
        "review_status": review_status,
        "topic": req.topic,
    }
    session.add(
        ItemEvent(
            item_id=item.id,
            event_type="diary_create",
            field_name="principal_id",
            old_value=None,
            new_value=json.dumps(details, sort_keys=True),
            actor_principal_id=actor.id,
            reason=req.reason,
        )
    )
    await session.commit()
    await session.refresh(item)
    return _response(item, status="created", actor_id=actor.id, represented=represented)


@router.get(
    "/diary/{principal}", response_model=list[DiaryEntry], dependencies=[Depends(READ_SCOPE)]
)
async def read_diary(
    principal: str,
    limit: int = 10,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    caller: AuthPrincipal = Depends(get_current_principal),  # noqa: B008
) -> list[DiaryEntry]:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 200")
    tenant_id = await _tenant_id(session)
    actor = await _caller_row(session, caller)
    target = await _principal_by_name(session, tenant_id, principal)
    if actor.id != target.id and actor.type not in {"user", "admin"}:
        raise HTTPException(
            status_code=403, detail="diary entries are visible only to their owning principal"
        )
    items = (
        await session.execute(
            select(MemoryItem)
            .where(
                MemoryItem.tenant_id == tenant_id,
                MemoryItem.principal_id == target.id,
                MemoryItem.kind == "diary_entry",
                MemoryItem.valid_to.is_(None),
            )
            .order_by(MemoryItem.created_at.desc(), MemoryItem.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        DiaryEntry(
            id=item.id,
            content=item.content,
            topic=item.subject_name,
            created_at=item.created_at,
            principal_id=item.principal_id,
        )
        for item in items
    ]
