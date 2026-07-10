"""Neutral semantic retrieval core.

Shared by ``/v1/search`` (semantic/hybrid modes) and semantic recall
(``POST /v1/recall mode=semantic``). Both callers need the same vector
machinery — pgvector cosine distance over ``memory_embeddings`` joined to
``memory_items`` — but differ in which review states are eligible. Search is
active-only by default; semantic recall also includes proposed items
(design.md §3) so agents can rediscover their own observations.

Keeping this here (rather than in ``engram/api/routes/memory.py``) lets the
recall engine depend on it without importing from the FastAPI route layer.

Ranking (semantic-v2): candidates are pulled from the HNSW index ordered by
cosine distance, then re-ranked by a deterministic trust-weighted score so a
slightly-closer low-trust item cannot outrank a high-trust memory. See
:func:`compute_semantic_trust_score`.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import and_, cast, func, literal_column, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from engram.memory_access import eligibility_expression
from engram.models import EmbeddingProfile, MemoryEmbedding, MemoryItem

# Scoring version exposed in search/recall results and recall_logs so the
# ranking that produced a given working set is auditable and reproducible.
SEMANTIC_SCORING_VERSION = "semantic-v2"

# ---- Trust-weighted blend constants ----
#
# trust_score =
#     0.30 * source_trust
#   + 0.30 * memory_confidence
#   + 0.25 * importance
#   + 0.10 * human_verified_bonus
#   + 0.05 * review_status_factor
#
# then multiplicative penalties for unresolved conflicts and proposed items.
# Clamped to [TRUST_MIN, 1.0] so low-trust items never fully dominate while
# cosine similarity still matters (semantic_score = similarity * trust_score).
_TRUST_W_SOURCE = 0.30
_TRUST_W_CONFIDENCE = 0.30
_TRUST_W_IMPORTANCE = 0.25
_TRUST_W_VERIFIED = 0.10
_TRUST_W_REVIEW = 0.05
_TRUST_MIN = 0.05
_TRUST_MAX = 1.0
_UNRESOLVED_CONFLICT_FACTOR = 0.75
_PROPOSED_REVIEW_FACTOR = 0.85


def compute_semantic_trust_score(
    *,
    source_trust: float,
    memory_confidence: float,
    importance: float,
    human_verified: bool,
    review_status: str,
    conflict_resolution_status: str | None,
) -> float:
    """Blend an item's trust signals into a ``[TRUST_MIN, 1.0]`` multiplier.

    Pure function — unit-testable without a DB. Applied to cosine similarity
    so the final ordering is similarity-aware *and* trust-aware. A proposed or
    unverified item that happens to sit slightly closer in embedding space
    therefore cannot outrank a higher-trust memory (design.md §4 trust model).
    """
    human_verified_bonus = 1.0 if human_verified else 0.0
    # Proposed items contribute nothing from this term; they additionally take
    # the _PROPOSED_REVIEW_FACTOR multiplier below.
    review_status_factor = 1.0 if review_status == "active" else 0.0
    trust = (
        _TRUST_W_SOURCE * source_trust
        + _TRUST_W_CONFIDENCE * memory_confidence
        + _TRUST_W_IMPORTANCE * importance
        + _TRUST_W_VERIFIED * human_verified_bonus
        + _TRUST_W_REVIEW * review_status_factor
    )
    if conflict_resolution_status == "unresolved":
        trust *= _UNRESOLVED_CONFLICT_FACTOR
    if review_status == "proposed":
        trust *= _PROPOSED_REVIEW_FACTOR
    return max(_TRUST_MIN, min(_TRUST_MAX, trust))


async def candidate_count(
    session: AsyncSession,
    *,
    tenant_id: str | UUID,
    principal_id: str | UUID,
    workspace_id: str | None = None,
    review_statuses: tuple[str, ...] = ("active",),
    kind: str | None = None,
    wing: str | None = None,
    room: str | None = None,
    profile: EmbeddingProfile | None = None,
) -> int:
    """Count recall/search-eligible embeddings currently in the corpus.

    An embedding is a candidate when its model matches, the vector is
    populated, the parent item is in one of ``review_statuses`` with
    ``valid_to IS NULL``, and the parent item belongs to ``tenant_id`` and is
    eligible for ``principal_id`` under the shared visibility predicate
    (see ``engram.memory_access``). Optional ``kind``/``wing``/``room``
    filters further narrow the candidate set (matching :func:`search`).
    """
    if profile is None:
        from engram.embedding_profiles import get_active_profile

        profile = await get_active_profile(session)
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
            MemoryEmbedding.profile_id == profile.id,
            MemoryEmbedding.embedding_dim == profile.dimensions,
            MemoryEmbedding.embedding_status == "ready",
            MemoryEmbedding.embedding.is_not(None),
            MemoryItem.review_status.in_(review_statuses),
            MemoryItem.valid_to.is_(None),
            MemoryItem.tenant_id == tenant_id,
            eligibility_expression(principal_id),
        )
    )
    if workspace_id is not None:
        stmt = stmt.where(MemoryItem.workspace_id == workspace_id)
    if kind is not None:
        stmt = stmt.where(MemoryItem.kind == kind)
    if wing is not None:
        stmt = stmt.where(MemoryItem.wing == wing)
    if room is not None:
        stmt = stmt.where(MemoryItem.room == room)
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
    kind: str | None = None,
    wing: str | None = None,
    room: str | None = None,
    profile: EmbeddingProfile | None = None,
) -> list[dict[str, Any]]:
    """Return the top-``limit`` items for ``query_embedding``, trust-ranked.

    Sets ``hnsw.iterative_scan = strict_order`` (requires pgvector 0.8+) so
    tenant-filtered queries don't suffer recall degradation. The HNSW index
    pulls the ``limit`` nearest candidates by cosine distance, then each is
    re-ranked by a deterministic trust-weighted score
    (:func:`compute_semantic_trust_score`), so ordering is similarity-aware
    *and* trust-aware.

    Filters (``kind``/``wing``/``room``) and tenant/visibility eligibility are
    applied before the distance ordering/limit, so ineligible rows never
    displace eligible ones out of the candidate window.

    Each row is returned as a dict with: ``id``, ``content``, ``kind``,
    ``review_status``, ``valid_to``, ``embedding_model``, ``embedding_dim``,
    ``distance`` (cosine), ``similarity_score`` (``1 - distance``),
    ``trust_score`` (the trust blend), and ``score`` (the final
    trust-weighted semantic score = similarity * trust_score). Results are
    ordered by ``score`` descending, then distance ascending, then
    ``created_at`` descending.
    """
    if profile is None:
        from engram.embedding_profiles import get_active_profile

        profile = await get_active_profile(session)
    if len(query_embedding) != profile.dimensions:
        raise ValueError(
            f"query vector dimension {len(query_embedding)} does not match active "
            f"profile {profile.profile_key} ({profile.dimensions})"
        )
    # strict_order handles tenant-filtered queries without recall degradation.
    await session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
    typed_embedding = cast(MemoryEmbedding.embedding, Vector(profile.dimensions))
    distance = typed_embedding.cosine_distance(query_embedding)
    profile_id_sql: Any = literal_column(f"'{profile.id}'::uuid")
    dimensions_sql: Any = literal_column(str(int(profile.dimensions)))
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
            MemoryItem.source_trust.label("source_trust"),
            MemoryItem.memory_confidence.label("memory_confidence"),
            MemoryItem.importance.label("importance"),
            MemoryItem.human_verified.label("human_verified"),
            MemoryItem.conflict_resolution_status.label("conflict_resolution_status"),
            MemoryItem.created_at.label("created_at"),
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
            MemoryEmbedding.profile_id == profile_id_sql,
            MemoryEmbedding.embedding_dim == dimensions_sql,
            MemoryEmbedding.embedding_status == "ready",
            MemoryEmbedding.embedding.is_not(None),
            MemoryItem.review_status.in_(review_statuses),
            MemoryItem.valid_to.is_(None),
            MemoryItem.tenant_id == tenant_id,
            eligibility_expression(principal_id),
        )
        # Fetch the nearest candidates from the HNSW index; trust re-ranking
        # happens in Python below. Callers over-fetch (search route + recall)
        # so trust re-ranking has room to reorder within the window.
        .order_by(distance.asc(), MemoryItem.created_at.desc())
        .limit(limit)
    )
    if workspace_id is not None:
        stmt = stmt.where(MemoryItem.workspace_id == workspace_id)
    if kind is not None:
        stmt = stmt.where(MemoryItem.kind == kind)
    if wing is not None:
        stmt = stmt.where(MemoryItem.wing == wing)
    if room is not None:
        stmt = stmt.where(MemoryItem.room == room)
    rows = (await session.execute(stmt)).mappings().all()

    results: list[dict[str, Any]] = []
    for row in rows:
        distance_value = float(row["distance"] or 0.0)
        similarity = max(0.0, min(1.0, 1.0 - distance_value))
        trust_score = compute_semantic_trust_score(
            source_trust=float(row["source_trust"] or 0.0),
            memory_confidence=float(row["memory_confidence"] or 0.0),
            importance=float(row["importance"] or 0.0),
            human_verified=bool(row["human_verified"]),
            review_status=row["review_status"],
            conflict_resolution_status=row["conflict_resolution_status"],
        )
        semantic_score = similarity * trust_score
        results.append(
            {
                "id": str(row["id"]),
                "content": row["content"],
                "kind": row["kind"],
                "review_status": row["review_status"],
                "valid_to": row["valid_to"],
                "distance": distance_value,
                "similarity_score": similarity,
                "trust_score": trust_score,
                "score": semantic_score,
                "embedding_model": row["embedding_model"],
                "embedding_dim": row["embedding_dim"],
                "embedding_profile": profile.profile_key,
                "created_at": row["created_at"],
            }
        )

    # Trust-weighted re-ranking: final score desc, then distance asc (closer
    # first among equal trust-weighted scores), then newest first as a stable
    # tiebreaker.
    results.sort(key=lambda r: (-r["score"], r["distance"], _sort_created_desc(r["created_at"])))
    return results


def _sort_created_desc(created_at: Any) -> float:
    """Return a sort key that orders newer ``created_at`` first ascending-ly.

    ``list.sort`` is ascending; negating the timestamp makes newer items sort
    before older ones among equal scores/distances. Missing timestamps fall to
    the end.
    """
    if created_at is None:
        return float("inf")
    try:
        ts = created_at.timestamp()
    except (AttributeError, ValueError, OSError):
        return float("inf")
    # Negate so that, in an ascending key, a larger (newer) timestamp sorts first.
    return float(-ts)
