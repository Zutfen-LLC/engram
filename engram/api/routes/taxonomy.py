"""Taxonomy and tunnel endpoints.

Skeleton — implementation in Phase 1 PR 5+.
"""

from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class TunnelCreate(BaseModel):
    source_wing: str
    source_room: str
    target_wing: str
    target_room: str
    label: str | None = None


@router.get("/taxonomy", response_model=None)
async def get_taxonomy() -> NoReturn:
    """Wing → room → item count hierarchy."""
    raise NotImplementedError


@router.get("/tunnels", response_model=None)
async def list_tunnels(wing: str | None = None) -> NoReturn:
    """List cross-wing links."""
    raise NotImplementedError


@router.post("/tunnels", response_model=None)
async def create_tunnel(req: TunnelCreate) -> NoReturn:
    """Create a tunnel."""
    raise NotImplementedError
