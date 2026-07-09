"""Neutral semantic retrieval core.

Shared by ``/v1/search`` (semantic/hybrid modes) and semantic recall
(``POST /v1/recall mode=semantic``). Both callers need the same vector
machinery — pgvector cosine distance over ``memory_embeddings`` joined to
``memory_items``, ordered by distance — but differ in which review states are
eligible. Search is active-only by default; semantic recall also includes
proposed items (design.md §3) so agents can rediscover their own observations.

Keeping this here (rather than in ``engram/api/routes/memory.py``) lets the
recall engine depend on it without importing from the FastAPI route layer.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from engram.embeddings import EMBEDDING_MODEL as _EMBEDDING_MODEL
from engram.memory_access import eligibility_expression
from engram.models import MemoryEmbedding, MemoryItem


async def candidate_count(
    session: AsyncSession,
    *,
    tenant_id: str | UUID,
    principal_id: str | UUID,
    workspace_id: str | None = None,
    review_statuses: tuple[str, ...] = ("active",),
) -> int:
    """Count recall/search-eligible embeddings currently in the corpus.

    An embedding is a candidate when its model matches, the vector is
    populated, the parent item is in one of ``review_statuses`` with
    ``valid_to IS NULL``, and the parent item belongs to ``tenant_id`` and is
    eligible for ``principal_id`` under the shared visibility predicate
    (see ``engram.memory_access``).
    """
    stmt = (
        select(func.count())
        .select_from(MemoryEmbedding)
        .join(
            MemoryItem,
            and_(
                MemoryItem.id == MemoryEmbedding.memory_item_id,
                MemoryItem.tenant_id == MemoryEmbedding.tenant_id,
            ),
        )
        .where(
            MemoryEmbedding.embedding_model == _EMBEDDING_MODEL,
            MemoryEmbedding.embedding.is_not(None),
            MemoryItem.review_status.in_(review_statuses),
            MemoryItem.valid_to.is_(None),
            MemoryItem.tenant_id == tenant_id,
            eligibility_expression(principal_id),
        )
    )
    if workspace_id is not None:
        stmt = stmt.where(MemoryItem.workspace_id == workspace_id)
    return int((await session.execute(stmt)).scalar_one())


async def search(
    session: AsyncSession,
    query_embedding: list[float],
    limit: int,
    *,
    tenant_id: str | UUID,
    principal_id: str | UUID,
    workspace_id: str | None = None,
    review_statuses: tuple[str, ...] = ("active",),
) -> list[dict[str, Any]]:
    """Return the top-``limit`` items nearest to ``query_embedding`` by cosine distance.

    Sets ``hnsw.iterative_scan = strict_order`` (requires pgvector 0.8+) so
    tenant-filtered queries don't suffer recall degradation. Results are
    ordered by distance ascending (most similar first), with ``created_at``
    descending as a stable tiebreaker.

    Candidates are scoped to ``tenant_id`` and filtered through the shared
    visibility predicate for ``principal_id`` (see ``engram.memory_access``)
    before the distance ordering/limit is applied, so ineligible rows never
    displace eligible ones out of the top-``limit`` window.

    Each row is returned as a dict with: ``id``, ``content``, ``kind``,
    ``review_status``, ``valid_to``, ``embedding_model``, ``embedding_dim``,
    ``distance`` (cosine), and ``score`` (``1 - distance``).
    """
    # strict_order handles tenant-filtered queries without recall degradation.
    await session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
    distance = MemoryEmbedding.embedding.cosine_distance(query_embedding)
    stmt = (
        select(
            MemoryItem.id.label("id"),
            MemoryItem.content.label("content"),
            MemoryItem.kind.label("kind"),
            MemoryItem.review_status.label("review_status"),
            MemoryItem.valid_to.label("valid_to"),
            MemoryEmbedding.embedding_model.label("embedding_model"),
            MemoryEmbedding.embedding_dim.label("embedding_dim"),
            distance.label("distance"),
        )
        .select_from(MemoryEmbedding)
        .join(
            MemoryItem,
            and_(
                MemoryItem.id == MemoryEmbedding.memory_item_id,
                MemoryItem.tenant_id == MemoryEmbedding.tenant_id,
            ),
        )
        .where(
            MemoryEmbedding.embedding_model == _EMBEDDING_MODEL,
            MemoryEmbedding.embedding.is_not(None),
            MemoryItem.review_status.in_(review_statuses),
            MemoryItem.valid_to.is_(None),
            MemoryItem.tenant_id == tenant_id,
            eligibility_expression(principal_id),
        )
        .order_by(distance.asc(), MemoryItem.created_at.desc())
        .limit(limit)
    )
    if workspace_id is not None:
        stmt = stmt.where(MemoryItem.workspace_id == workspace_id)
    rows = (await session.execute(stmt)).mappings().all()
    results: list[dict[str, Any]] = []
    for row in rows:
        distance_value = float(row["distance"] or 0.0)
        results.append(
            {
                "id": str(row["id"]),
                "content": row["content"],
                "kind": row["kind"],
                "review_status": row["review_status"],
                "valid_to": row["valid_to"],
                "distance": distance_value,
                "score": 1.0 - distance_value,
                "embedding_model": row["embedding_model"],
                "embedding_dim": row["embedding_dim"],
            }
        )
    return results
