"""Taxonomy and tunnel endpoints.

Skeleton — implementation in Phase 1 PR 5+.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class TunnelCreate(BaseModel):
    source_wing: str
    source_room: str
    target_wing: str
    target_room: str
    label: str | None = None


@router.get("/taxonomy")
async def get_taxonomy():
    """Wing → room → item count hierarchy."""
    raise NotImplementedError


@router.get("/tunnels")
async def list_tunnels(wing: str | None = None):
    """List cross-wing links."""
    raise NotImplementedError


@router.post("/tunnels")
async def create_tunnel(req: TunnelCreate):
    """Create a tunnel."""
    raise NotImplementedError
