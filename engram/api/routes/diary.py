"""Agent diary endpoints.

A diary is just a memory_item with locked defaults:
``kind='diary_entry'`` and ``visibility='private'``. Entries are scoped by
``principal_id`` so an agent's diary is only visible to that agent.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from engram.auth import READ_SCOPE, WRITE_SCOPE
from engram.canonicalize import canonicalize, content_hash
from engram.db import get_session
from engram.models import MemoryItem, Principal
from engram.safety import has_secrets

router = APIRouter()


# ---- Request/response models ----


class DiaryWrite(BaseModel):
    entry: str
    principal: str  # principal name (not UUID) — matches the AAAK convention
    topic: str | None = None


class DiaryEntry(BaseModel):
    id: UUID
    content: str
    topic: str | None
    created_at: datetime
    principal_id: UUID


class DiaryWriteResponse(BaseModel):
    id: UUID
    status: str  # created | deduped
    review_status: str
    principal_id: UUID


# ---- Helpers ----


async def _resolve_tenant_id(
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> UUID:
    row = await session.execute(text("SELECT current_setting('app.tenant_id', true)"))
    tid_str = row.scalar()
    if not tid_str:
        raise HTTPException(status_code=403, detail="no tenant context")
    return UUID(str(tid_str))


async def _resolve_principal_id_by_name(
    session: AsyncSession,
    tenant_id: UUID,
    name: str,
) -> UUID:
    """Look up a principal by its name within the current tenant.

    Diary writes are addressed to a principal name (AAAK convention). The
    tenant boundary is enforced via RLS.
    """
    stmt = select(Principal).where(
        Principal.tenant_id == tenant_id,
        Principal.name == name,
    )
    result = await session.execute(stmt)
    p = result.scalar_one_or_none()
    if p is None:
        raise HTTPException(status_code=422, detail=f"principal '{name}' not found in tenant")
    return p.id


async def _resolve_caller_principal_id(session: AsyncSession) -> UUID:
    """Read app.principal_id from RLS context — the authenticated caller."""
    row = await session.execute(text("SELECT current_setting('app.principal_id', true)"))
    pid_str = row.scalar()
    if not pid_str:
        raise HTTPException(status_code=403, detail="no principal context")
    return UUID(str(pid_str))


def _diary_to_entry(item: MemoryItem, topic: str | None) -> DiaryEntry:
    return DiaryEntry(
        id=item.id,
        content=item.content,
        topic=topic,
        created_at=item.created_at,
        principal_id=item.principal_id,
    )


# ---- Endpoints ----


@router.post(
    "/diary",
    response_model=DiaryWriteResponse,
    status_code=201,
    dependencies=[Depends(WRITE_SCOPE)],
)
async def write_diary(
    req: DiaryWrite,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> DiaryWriteResponse:
    """Write a diary entry. Locks kind='diary_entry' and visibility='private'."""
    if has_secrets(req.entry):
        raise HTTPException(
            status_code=422,
            detail="diary entry contains patterns matching secrets/credentials",
        )

    tenant_id = await _resolve_tenant_id(session)
    principal_id = await _resolve_principal_id_by_name(session, tenant_id, req.principal)

    canonical = canonicalize(req.entry)
    chash = content_hash(canonical)

    caller_id = await _resolve_caller_principal_id(session)
    caller_row = await session.execute(
        select(Principal.type).where(Principal.id == caller_id)
    )
    caller_type = caller_row.scalar_one_or_none() or "agent"
    review_status = "active" if caller_type in ("user", "admin", "system") else "proposed"

    # Topic is stored in subject_name (subject_type stays NULL — the DB CHECK
    # constraint limits subject_type to a fixed vocabulary and "topic" isn't
    # one of them).
    item = MemoryItem(
        tenant_id=tenant_id,
        workspace_id=None,
        principal_id=principal_id,
        content=req.entry,
        content_hash=chash,
        kind="diary_entry",
        wing=None,
        room=None,
        subject_type=None,
        subject_id=None,
        subject_name=req.topic,
        visibility="private",
        review_status=review_status,
        memory_confidence=0.7,
        source_trust=0.7 if caller_type in ("user", "admin", "system") else 0.5,
        importance=0.4,
        source_type="manual" if caller_type in ("user", "admin") else "extraction",
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
                    MemoryItem.principal_id == principal_id,
                    MemoryItem.workspace_id.is_(None),
                    MemoryItem.content_hash == chash,
                    MemoryItem.valid_to.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return DiaryWriteResponse(
                id=existing.id,
                status="deduped",
                review_status=existing.review_status,
                principal_id=existing.principal_id,
            )
        # Not a dedup — re-raise so the centralized DB-error handler
        # classifies the underlying constraint failure.
        raise

    await session.commit()
    await session.refresh(item)

    return DiaryWriteResponse(
        id=item.id,
        status="created",
        review_status=item.review_status,
        principal_id=item.principal_id,
    )


@router.get(
    "/diary/{principal}", response_model=list[DiaryEntry], dependencies=[Depends(READ_SCOPE)]
)
async def read_diary(
    principal: str,
    limit: int = 10,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[DiaryEntry]:
    """Read diary entries for a principal. Visibility-gated to that principal.

    The caller must be either the diary's owner or a user/admin principal.
    """
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 200")

    tenant_id = await _resolve_tenant_id(session)
    target_id = await _resolve_principal_id_by_name(session, tenant_id, principal)

    caller_id = await _resolve_caller_principal_id(session)
    caller_row = await session.execute(
        select(Principal.type).where(Principal.id == caller_id)
    )
    caller_type = caller_row.scalar_one_or_none() or "agent"
    if caller_id != target_id and caller_type not in ("user", "admin", "system"):
        raise HTTPException(
            status_code=403, detail="diary entries are visible only to their owning principal"
        )

    stmt = (
        select(MemoryItem)
        .where(
            MemoryItem.tenant_id == tenant_id,
            MemoryItem.principal_id == target_id,
            MemoryItem.kind == "diary_entry",
            MemoryItem.valid_to.is_(None),
        )
        .order_by(MemoryItem.created_at.desc())
        .limit(limit)
    )
    items = (await session.execute(stmt)).scalars().all()
    return [_diary_to_entry(item, item.subject_name) for item in items]
