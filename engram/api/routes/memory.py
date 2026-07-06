"""Memory operations: remember, recall, search, item CRUD.

This is a skeleton — implementation in Phase 1 PR 4-5.
"""

from __future__ import annotations

from typing import Any, NoReturn
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter()


# ---- Request/response models ----


class RememberRequest(BaseModel):
    content: str
    kind: str = "fact"  # fact|preference|doctrine|decision|invariant|observation|diary_entry
    wing: str | None = None
    room: str | None = None
    workspace: str | None = None
    visibility: str = "workspace"
    source_type: str = "manual"
    source_session: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RememberResponse(BaseModel):
    id: UUID
    status: str  # created | deduped | superseded
    deduped_existing_id: UUID | None = None
    superseded_id: UUID | None = None


class RecallRequest(BaseModel):
    mode: str = "startup"  # startup | semantic
    query: str | None = None
    workspace: str | None = None
    byte_budget: int | None = None
    item_budget: int | None = None


class RecallResponse(BaseModel):
    working_set: str
    item_count: int
    byte_count: int
    omitted_count: int
    items: list[dict[str, Any]] = Field(default_factory=list)


class SearchRequest(BaseModel):
    query: str
    mode: str = "hybrid"  # keyword | semantic | hybrid
    limit: int = 10
    wing: str | None = None
    room: str | None = None
    kind: str | None = None


class SearchResponse(BaseModel):
    results: list[dict[str, Any]]
    total: int


# ---- Endpoints (stubs) ----


@router.post("/remember", response_model=None)
async def remember(req: RememberRequest) -> NoReturn:
    """Write a memory item with dedup and supersession."""
    # TODO: implement canonicalization, dedup, supersession, embedding
    raise NotImplementedError("remember not yet implemented")


@router.post("/recall", response_model=None)
async def recall(req: RecallRequest) -> NoReturn:
    """Bounded recall: deterministic startup set or semantic query."""
    # TODO: implement startup/semantic recall with budget enforcement
    raise NotImplementedError("recall not yet implemented")


@router.post("/search", response_model=None)
async def search(req: SearchRequest) -> NoReturn:
    """Keyword, semantic, or hybrid search."""
    # TODO: implement keyword (ILIKE), semantic (pgvector), hybrid search
    raise NotImplementedError("search not yet implemented")


@router.get("/items", response_model=None)
async def list_items(
    workspace: str | None = None,
    kind: str | None = None,
    wing: str | None = None,
    room: str | None = None,
    active_only: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> NoReturn:
    """List items with filters."""
    # TODO: implement filtered query
    raise NotImplementedError("list_items not yet implemented")


@router.get("/items/{item_id}", response_model=None)
async def get_item(item_id: UUID) -> NoReturn:
    """Full detail with provenance and linked KG facts."""
    # TODO: implement detail query
    raise NotImplementedError("get_item not yet implemented")


@router.patch("/items/{item_id}", response_model=None)
async def update_item_metadata(item_id: UUID) -> NoReturn:
    """Update metadata (wing, room, visibility) — not content."""
    raise NotImplementedError("update_item not yet implemented")


@router.post("/items/{item_id}/supersede", response_model=None)
async def supersede_item(item_id: UUID) -> NoReturn:
    """Mark superseded + write replacement."""
    raise NotImplementedError("supersede_item not yet implemented")


@router.post("/items/{item_id}/invalidate", response_model=None)
async def invalidate_item(item_id: UUID) -> NoReturn:
    """Mark invalid (set valid_to)."""
    raise NotImplementedError("invalidate_item not yet implemented")
