"""Taxonomy browser and tunnel CRUD endpoints.

The taxonomy view is a GROUP BY over active memory_items — wing → room → count.
Tunnels are cross-wing/room links stored in the ``tunnels`` table; they
navigate between rooms the way hallways navigate between chambers in a
MemPalace.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from engram.auth import READ_SCOPE, WRITE_SCOPE
from engram.db import get_session
from engram.memory_access import read_eligibility_sql
from engram.memory_context import ResolvedMemoryContext, resolve_memory_context
from engram.models import Tunnel

router = APIRouter()


# ---- Request/response models ----


class TunnelCreate(BaseModel):
    source_wing: str
    source_room: str | None = None
    target_wing: str
    target_room: str | None = None
    label: str | None = None


class TunnelOut(BaseModel):
    id: UUID
    source_wing: str
    source_room: str | None
    target_wing: str
    target_room: str | None
    label: str | None
    created_at: datetime


class TaxonomyRoom(BaseModel):
    name: str
    item_count: int


class TaxonomyWing(BaseModel):
    name: str
    item_count: int
    rooms: list[TaxonomyRoom]


class TaxonomyResponse(BaseModel):
    wings: list[TaxonomyWing]
    total_items: int
    wing_count: int


# ---- Helpers ----


async def _resolve_tenant_id(
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> UUID:
    row = await session.execute(text("SELECT current_setting('app.tenant_id', true)"))
    tid_str = row.scalar()
    if not tid_str:
        raise HTTPException(status_code=403, detail="no tenant context")
    return UUID(str(tid_str))


def _tunnel_to_out(t: Tunnel) -> TunnelOut:
    return TunnelOut(
        id=t.id,
        source_wing=t.source_wing,
        source_room=t.source_room,
        target_wing=t.target_wing,
        target_room=t.target_room,
        label=t.label,
        created_at=t.created_at,
    )


# ---- Endpoints ----


@router.get("/taxonomy", response_model=TaxonomyResponse, dependencies=[Depends(READ_SCOPE)])
async def get_taxonomy(
    session: AsyncSession = Depends(get_session),  # noqa: B008
    tenant_id: UUID = Depends(_resolve_tenant_id),  # noqa: B008
    memory_context: ResolvedMemoryContext = Depends(resolve_memory_context),  # noqa: B008
) -> TaxonomyResponse:
    """Return nested wing → room → item_count for active memory items.

    "Active" means ``review_status='active'`` and ``valid_to IS NULL`` — the same
    trust gate that startup recall uses. Items with a NULL wing are grouped
    under the synthetic wing ``"_(unassigned)"`` so they remain visible.
    """
    read_scope = read_eligibility_sql(memory_context, parameter_prefix="taxonomy_item")
    sql = text(
        f"""
        SELECT
            COALESCE(wing, '_(unassigned)') AS wing,
            COALESCE(room, '_(unassigned)') AS room,
            COUNT(*) AS item_count
        FROM memory_items
        WHERE {read_scope.clause}
          AND review_status = 'active'
          AND valid_to IS NULL
        GROUP BY wing, room
        ORDER BY wing ASC, room ASC
        """
    )
    rows = (await session.execute(sql, read_scope.params)).mappings().all()

    wings_map: dict[str, dict[str, Any]] = {}
    total = 0
    for row in rows:
        wing_name = row["wing"]
        room_name = row["room"]
        count = int(row["item_count"])
        if wing_name not in wings_map:
            wings_map[wing_name] = {"name": wing_name, "item_count": 0, "rooms": []}
        wings_map[wing_name]["item_count"] += count
        wings_map[wing_name]["rooms"].append(
            TaxonomyRoom(name=room_name, item_count=count)
        )
        total += count

    wings = [
        TaxonomyWing(
            name=w["name"],
            item_count=w["item_count"],
            rooms=[r for r in w["rooms"]],
        )
        for w in wings_map.values()
    ]

    return TaxonomyResponse(
        wings=wings,
        total_items=total,
        wing_count=len(wings),
    )


@router.get("/tunnels", response_model=list[TunnelOut], dependencies=[Depends(READ_SCOPE)])
async def list_tunnels(
    wing: str | None = None,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    tenant_id: UUID = Depends(_resolve_tenant_id),  # noqa: B008
) -> list[TunnelOut]:
    """List cross-wing links. Optional ``wing`` filter matches either endpoint."""
    stmt = select(Tunnel).where(Tunnel.tenant_id == tenant_id)
    if wing is not None:
        stmt = stmt.where((Tunnel.source_wing == wing) | (Tunnel.target_wing == wing))
    stmt = stmt.order_by(Tunnel.created_at.desc())
    rows = (await session.execute(stmt)).scalars().all()
    return [_tunnel_to_out(t) for t in rows]


@router.post(
    "/tunnels", response_model=TunnelOut, status_code=201, dependencies=[Depends(WRITE_SCOPE)]
)
async def create_tunnel(
    req: TunnelCreate,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    tenant_id: UUID = Depends(_resolve_tenant_id),  # noqa: B008
) -> TunnelOut:
    """Create a tunnel linking two wings (optionally specific rooms)."""
    if not req.source_wing.strip() or not req.target_wing.strip():
        raise HTTPException(status_code=422, detail="source_wing and target_wing are required")
    tunnel = Tunnel(
        id=uuid4(),
        tenant_id=tenant_id,
        source_wing=req.source_wing,
        source_room=req.source_room,
        target_wing=req.target_wing,
        target_room=req.target_room,
        label=req.label,
    )
    session.add(tunnel)
    await session.commit()
    await session.refresh(tunnel)
    return _tunnel_to_out(tunnel)
