"""Memory operations: remember, recall, search, item CRUD.

This is a skeleton — implementation in Phase 1 PR 4-5.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from engram import semantic
from engram.canonicalize import canonicalize, content_hash
from engram.classification import ClassificationResult
from engram.classification import classify as classify_memory
from engram.config import settings
from engram.conflicts import ConflictAction, ConflictResult, detect_conflicts
from engram.db import get_session
from engram.embeddings import create_embedding_placeholder, generate_embedding
from engram.memory_access import eligibility_sql, resolve_workspace_scope, tenant_sql
from engram.models import (
    FeedbackEvent,
    ItemEvent,
    MemoryItem,
    Principal,
    TenantConfig,
    Workspace,
)
from engram.safety import has_secrets

router = APIRouter()

# Kinds with singleton semantics — writing a new item with the same family
# key supersedes the old one instead of creating a duplicate.
_SINGLETON_KINDS = {"preference", "invariant"}

# source_type values that default to review_status='active'.
_ACTIVE_SOURCES = {"manual", "import", "migration"}


# ---- Request/response models ----

SourceKind = Literal["manual", "import", "migration", "extraction", "sync_turn", "pre_compress"]
PrincipalKind = Literal["user", "agent", "system", "admin"]
SensitivityKind = Literal["normal", "sensitive", "restricted"]


class RememberRequest(BaseModel):
    content: str
    kind: str | None = None  # fact|preference|doctrine|decision|invariant|observation|diary_entry
    wing: str | None = None
    room: str | None = None
    workspace: str | None = None
    visibility: str = "workspace"
    source_type: SourceKind = "manual"
    source_session: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Subject / entity (what this memory is ABOUT)
    subject_type: str | None = None
    subject_id: str | None = None
    subject_name: str | None = None

    # Trust overrides (optional — caller can override tenant_config defaults)
    importance: float = 0.5
    sensitivity: SensitivityKind = "normal"

    # External linkage (for imports)
    external_id: str | None = None
    external_source: str | None = None


class RememberResponse(BaseModel):
    id: UUID
    status: str  # created | deduped | superseded
    review_status: str
    memory_confidence: float
    deduped_existing_id: UUID | None = None
    superseded_id: UUID | None = None


class RecallRequest(BaseModel):
    mode: str = "startup"  # startup | semantic
    query: str | None = None
    workspace: str | None = None
    byte_budget: int | None = None
    token_budget: int | None = None
    item_budget: int | None = None


class RecallResponse(BaseModel):
    working_set: str
    item_count: int
    byte_count: int
    pinned_omitted_count: int = 0
    omitted_count: int
    items: list[dict[str, Any]] = Field(default_factory=list)
    scoring_version: str = "v1"
    config_version: str = "v1"
    recall_log_id: str | None = None
    message: str | None = None


class SearchRequest(BaseModel):
    query: str
    mode: Literal["keyword", "semantic", "hybrid"] = "hybrid"
    limit: int = Field(default=10, ge=1, le=100)
    wing: str | None = None
    room: str | None = None
    kind: str | None = None


class SearchResponse(BaseModel):
    results: list[dict[str, Any]]
    total: int
    message: str | None = None


class FeedbackRequest(BaseModel):
    item_id: UUID
    feedback: Literal["useful", "noise"]
    recall_log_id: UUID | None = None


# ---- Trust model helpers ----


def _trust_confidence_key(source_type: str, principal_type: str) -> tuple[str, str]:
    """Return the (trust_col, confidence_col) suffix pair for a source/principal combo.

    manual + user/admin → manual_user; manual + agent → manual_agent;
    import/migration → import; extraction → extraction; etc.
    """
    if source_type == "manual":
        if principal_type in ("user", "admin"):
            return "manual_user", "manual_user"
        return "manual_agent", "manual_agent"
    if source_type in ("import", "migration"):
        return "import", "import"
    return source_type, source_type


# Fallback defaults from design.md Section 4 — used only when no tenant_config row exists.
_TRUST_FALLBACKS: dict[str, tuple[float, float]] = {
    "manual_user": (0.9, 0.9),
    "manual_agent": (0.6, 0.5),
    "import": (0.8, 0.8),
    "extraction": (0.5, 0.5),
    "sync_turn": (0.4, 0.4),
    "pre_compress": (0.3, 0.3),
}


async def _resolve_trust_defaults(
    session: AsyncSession,
    tenant_id: UUID,
    source_type: str,
    principal_type: str,
) -> tuple[float, float, str]:
    """Read the active tenant_config and return (source_trust, memory_confidence, review_status).

    Values come from the tenant_config table, not hardcoded constants. Falls
    back to design.md Section 4 defaults if no config row is found.
    """
    result = await session.execute(
        select(TenantConfig).where(
            TenantConfig.tenant_id == tenant_id,
            TenantConfig.active.is_(True),
        )
    )
    config = result.scalar_one_or_none()

    trust_key, conf_key = _trust_confidence_key(source_type, principal_type)

    if config is not None:
        source_trust = float(getattr(config, f"trust_{trust_key}"))
        memory_confidence = float(getattr(config, f"confidence_{conf_key}"))
    else:
        source_trust, memory_confidence = _TRUST_FALLBACKS.get(trust_key, (0.5, 0.5))

    # Review status: active for user/admin manual writes and system imports;
    # proposed for agent-sourced writes and all inferred sources.
    if source_type in _ACTIVE_SOURCES and principal_type in ("user", "admin", "system"):
        review_status = "active"
    else:
        review_status = "proposed"

    return source_trust, memory_confidence, review_status


async def _resolve_workspace_id(
    session: AsyncSession,
    tenant_id: UUID,
    workspace_slug: str | None,
) -> UUID | None:
    """Resolve workspace slug to UUID. Returns None for tenant-level memories."""
    if workspace_slug is None:
        return None
    result = await session.execute(
        select(Workspace.id).where(
            Workspace.tenant_id == tenant_id,
            Workspace.slug == workspace_slug,
        )
    )
    ws_id = result.scalar_one_or_none()
    if ws_id is None:
        raise HTTPException(status_code=422, detail=f"workspace '{workspace_slug}' not found")
    return ws_id


async def _resolve_principal(
    session: AsyncSession,
    tenant_id: UUID,
) -> tuple[UUID, str]:
    """Return (principal_id, principal_type) from the RLS session context.

    When auth is disabled (Phase 1A), get_session sets app.principal_id to the
    seed admin principal. We read that principal's type from the DB.
    """
    pid_row = await session.execute(text("SELECT current_setting('app.principal_id', true)"))
    pid_str = pid_row.scalar()
    if not pid_str:
        raise HTTPException(status_code=403, detail="no principal context")
    principal_id = UUID(pid_str)

    result = await session.execute(select(Principal.type).where(Principal.id == principal_id))
    ptype = result.scalar_one_or_none()
    if ptype is None:
        raise HTTPException(status_code=403, detail="principal not found")
    return principal_id, ptype


async def _resolve_tenant_id(session: AsyncSession) -> UUID:
    """Read tenant_id from RLS session context."""
    tid_row = await session.execute(text("SELECT current_setting('app.tenant_id', true)"))
    tid_str = tid_row.scalar()
    if not tid_str:
        raise HTTPException(status_code=403, detail="no tenant context")
    return UUID(tid_str)


async def _check_supersession(
    session: AsyncSession,
    tenant_id: UUID,
    workspace_id: UUID | None,
    principal_id: UUID,
    kind: str,
    subject_type: str | None,
    subject_id: str | None,
) -> UUID | None:
    """For singleton kinds (preference/invariant), find an existing active item
    with the same family key and return its ID for supersession.

    Family key = (tenant, workspace, principal, subject_type, subject_id, kind).
    """
    if kind not in _SINGLETON_KINDS:
        return None

    stmt = (
        select(MemoryItem.id)
        .where(
            MemoryItem.tenant_id == tenant_id,
            MemoryItem.principal_id == principal_id,
            MemoryItem.kind == kind,
            MemoryItem.valid_to.is_(None),
            MemoryItem.review_status != "rejected",
        )
        .order_by(MemoryItem.created_at.desc())
        .limit(1)
    )
    if workspace_id is None:
        stmt = stmt.where(MemoryItem.workspace_id.is_(None))
    else:
        stmt = stmt.where(MemoryItem.workspace_id == workspace_id)
    if subject_type is not None:
        stmt = stmt.where(MemoryItem.subject_type == subject_type)
    if subject_id is not None:
        stmt = stmt.where(MemoryItem.subject_id == subject_id)

    result = await session.execute(stmt)
    return result.scalar_one_or_none()


_SEARCH_HELPFUL_MESSAGE = (
    "No embeddings are available yet. Write memories with embedding_provider != 'none' "
    "to enable semantic search."
)
_RRF_K = 60


def _search_result_row(
    row: Any,
    *,
    mode: str,
    score: float,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": str(row["id"]),
        "content": row["content"],
        "kind": row["kind"],
        "review_status": row["review_status"],
        "valid_to": row["valid_to"],
        "score": float(score),
        "mode": mode,
    }
    if extra is not None:
        result.update(extra)
    return result


async def _keyword_search(
    session: AsyncSession,
    query: str,
    limit: int,
    *,
    tenant_id: UUID | str,
    principal_id: UUID | str,
) -> list[dict[str, Any]]:
    """Keyword (full-text) search, scoped to the caller's tenant + visibility.

    Uses the shared raw-SQL eligibility fragment (``engram.memory_access``)
    since this query is over raw ``text(...)`` SQL, not the ORM.
    """
    stmt = text(
        f"""
        SELECT
            mi.id,
            mi.content,
            mi.kind,
            mi.review_status,
            mi.valid_to,
            ts_rank_cd(mi.content_tsv, plainto_tsquery('english', :query)) AS score
        FROM memory_items mi
        WHERE mi.review_status = 'active'
          AND mi.valid_to IS NULL
          AND {tenant_sql("mi")}
          AND {eligibility_sql("mi")}
          AND mi.content_tsv @@ plainto_tsquery('english', :query)
        ORDER BY score DESC, mi.created_at DESC
        LIMIT :limit
        """
    )
    rows = (
        await session.execute(
            stmt,
            {
                "query": query,
                "limit": limit,
                "caller_tenant_id": str(tenant_id),
                "caller_principal_id": str(principal_id),
            },
        )
    ).mappings().all()
    return [
        _search_result_row(row, mode="keyword", score=float(row["score"] or 0.0)) for row in rows
    ]


def _format_semantic_result(row: dict[str, Any]) -> dict[str, Any]:
    """Shape a semantic.search() row into the /v1/search response format."""
    return _search_result_row(
        row,
        mode="semantic",
        score=float(row.get("score", 0.0)),
        extra={
            "distance": row.get("distance"),
            "embedding_model": row.get("embedding_model"),
            "embedding_dim": row.get("embedding_dim"),
        },
    )


def _rrf_fuse(
    keyword_results: list[dict[str, Any]],
    semantic_results: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    fused: dict[str, dict[str, Any]] = {}
    for rank, result in enumerate(keyword_results, start=1):
        entry = fused.setdefault(result["id"], dict(result))
        entry["score"] = float(entry.get("score", 0.0)) + 1.0 / (_RRF_K + rank)
        entry["keyword_rank"] = rank
        entry.setdefault("semantic_rank", None)
        entry["mode"] = "hybrid"
    for rank, result in enumerate(semantic_results, start=1):
        entry = fused.setdefault(result["id"], dict(result))
        entry["score"] = float(entry.get("score", 0.0)) + 1.0 / (_RRF_K + rank)
        entry["semantic_rank"] = rank
        entry.setdefault("keyword_rank", None)
        entry["mode"] = "hybrid"
        if "distance" not in entry and "distance" in result:
            entry["distance"] = result["distance"]
        if "embedding_model" not in entry and "embedding_model" in result:
            entry["embedding_model"] = result["embedding_model"]
        if "embedding_dim" not in entry and "embedding_dim" in result:
            entry["embedding_dim"] = result["embedding_dim"]
    return sorted(
        fused.values(),
        key=lambda item: (
            -float(item["score"]),
            item.get("keyword_rank") or 1000000000,
            item.get("semantic_rank") or 1000000000,
            item["content"],
        ),
    )[:limit]


# ---- Endpoints ----


@router.post("/remember", response_model=RememberResponse, status_code=201)
async def remember(
    req: RememberRequest,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> RememberResponse:
    """Write a memory item with dedup, trust defaults, and supersession."""
    # 1. Secret check — wire existing safety.py denylist.
    if has_secrets(req.content):
        raise HTTPException(
            status_code=422,
            detail="content contains patterns matching secrets/credentials",
        )

    # 2. Canonicalize + hash.
    canonical = canonicalize(req.content)
    chash = content_hash(canonical)

    # 3. Resolve RLS context.
    tenant_id = await _resolve_tenant_id(session)
    principal_id, principal_type = await _resolve_principal(session, tenant_id)
    workspace_id = await _resolve_workspace_id(session, tenant_id, req.workspace)

    kind = req.kind
    wing = req.wing
    room = req.room
    classification_result: ClassificationResult | None = None
    if kind is None:
        classification_result = await classify_memory(req.content, tenant_id, session)
        kind = classification_result.suggested_kind
        if wing is None:
            wing = classification_result.suggested_wing
        if room is None:
            room = classification_result.suggested_room

    # 4. Trust defaults from tenant_config.
    source_trust, memory_confidence, review_status = await _resolve_trust_defaults(
        session, tenant_id, req.source_type, principal_type
    )
    if classification_result is not None and classification_result.suggested_kind == "decision":
        review_status = "proposed"

    # 5. Supersession check for singleton kinds.
    superseded_id = await _check_supersession(
        session,
        tenant_id,
        workspace_id,
        principal_id,
        kind,
        req.subject_type,
        req.subject_id,
    )

    # 6. Build the memory item.
    item = MemoryItem(
        tenant_id=str(tenant_id),
        workspace_id=workspace_id,
        principal_id=str(principal_id),
        content=req.content,
        content_hash=chash,
        kind=kind,
        wing=wing,
        room=room,
        subject_type=req.subject_type,
        subject_id=req.subject_id,
        subject_name=req.subject_name,
        visibility=req.visibility,
        review_status=review_status,
        memory_confidence=memory_confidence,
        source_trust=source_trust,
        importance=req.importance,
        source_type=req.source_type,
        source_session=req.source_session,
        sensitivity=req.sensitivity,
        external_id=req.external_id,
        external_source=req.external_source,
    )

    session.add(item)

    # 7. Attempt flush — catches dedup via unique index idx_memitems_dedup.
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        # Re-query for the existing item to verify this was a dedup, not
        # a CHECK constraint violation or other integrity error.
        dedup_stmt = select(MemoryItem.id).where(
            MemoryItem.tenant_id == tenant_id,
            MemoryItem.content_hash == chash,
            MemoryItem.principal_id == principal_id,
            MemoryItem.valid_to.is_(None),
            MemoryItem.review_status != "rejected",
        )
        if workspace_id is None:
            dedup_stmt = dedup_stmt.where(MemoryItem.workspace_id.is_(None))
        else:
            dedup_stmt = dedup_stmt.where(MemoryItem.workspace_id == workspace_id)
        existing_id = (await session.execute(dedup_stmt)).scalar_one_or_none()
        if existing_id is None:
            # Not a dedup — some other constraint rejected the request shape
            # (e.g. a CHECK/enum value or FK reference that slipped past
            # Pydantic). Re-raise so the centralized DB-error handler
            # classifies it by SQLSTATE into the right 4xx/5xx.
            raise
        return RememberResponse(
            id=existing_id,
            status="deduped",
            review_status=review_status,
            memory_confidence=memory_confidence,
            deduped_existing_id=existing_id,
        )

    provider = "caller" if classification_result is None else classification_result.provenance.get(
        "provider", "rule"
    )
    provenance_payload: dict[str, Any] = {
        "source": "explicit_kind" if classification_result is None else "auto_classified",
        "kind": kind,
        "wing": wing,
        "room": room,
        "provider": provider,
    }
    if classification_result is not None:
        classification_dump = classification_result.model_dump(exclude={"provenance"})
        provenance_payload["classification"] = classification_dump
        provenance_payload["classification_provenance"] = classification_result.provenance
        provenance_payload["reason"] = classification_result.reason
    if classification_result is None:
        reason = "explicit kind override"
    else:
        reason = classification_result.reason
    session.add(
        ItemEvent(
            item_id=item.id,
            event_type="classification",
            field_name="kind",
            old_value=None,
            new_value=json.dumps(provenance_payload, sort_keys=True),
            actor_principal_id=principal_id,
            reason=reason,
        )
    )

    # 8. If supersession applies, mark the old item.
    if superseded_id is not None:
        await session.execute(
            update(MemoryItem)
            .where(MemoryItem.id == superseded_id)
            .values(
                valid_to=func.now(),
                superseded_by=item.id,
            )
        )

    # 9. Create embedding row when embeddings are enabled.
    if settings.embedding_provider != "none":
        embedding_row = await create_embedding_placeholder(session, item.id, tenant_id)
        embedding = await generate_embedding(req.content)
        if embedding is not None:
            embedding_row.embedding = embedding
            embedding_row.embedding_dim = len(embedding)
            embedding_row.embedding_status = "ready"

    # 10. Conflict detection (semantic similarity + classifier).
    # Only runs when embeddings are available — skipped cleanly otherwise.
    conflict_result: ConflictResult | None = None
    if settings.embedding_provider != "none" and settings.conflict_check_on_write:
        await session.flush()
        conflict_result = await detect_conflicts(item, session)

    if conflict_result is not None:
        action = conflict_result.action
        if action is ConflictAction.DEDUP:
            await session.rollback()
            return RememberResponse(
                id=conflict_result.existing_item_id,
                status="deduped",
                review_status=review_status,
                memory_confidence=memory_confidence,
                deduped_existing_id=conflict_result.existing_item_id,
            )
        if action is ConflictAction.AUTO_SUPERSEDE:
            await session.execute(
                update(MemoryItem)
                .where(MemoryItem.id == conflict_result.existing_item_id)
                .values(
                    valid_to=func.now(),
                    superseded_by=item.id,
                )
            )
            session.add(
                ItemEvent(
                    item_id=item.id,
                    event_type="conflict_detected",
                    field_name="conflicts_with_item_id",
                    old_value=None,
                    new_value=json.dumps(
                        {
                            "verdict": conflict_result.verdict.value,
                            "action": action.value,
                            "similarity": conflict_result.similarity,
                            "existing_item_id": str(conflict_result.existing_item_id),
                            "reason": conflict_result.reason,
                        },
                        sort_keys=True,
                    ),
                    actor_principal_id=principal_id,
                    reason=conflict_result.reason,
                )
            )
            await session.commit()
            return RememberResponse(
                id=item.id,
                status="superseded",
                review_status=review_status,
                memory_confidence=memory_confidence,
                superseded_id=conflict_result.existing_item_id,
            )
        # FLAG_CONTRADICTION, PROPOSED_SUPERSEDE, FLAG_SCOPE_OVERLAP:
        # mark the item with conflict metadata and write an event.
        item.conflicts_with_item_id = conflict_result.existing_item_id
        item.conflict_type = conflict_result.conflict_type
        item.conflict_resolution_status = "unresolved"
        item.review_status = "proposed"
        session.add(
            ItemEvent(
                item_id=item.id,
                event_type="conflict_detected",
                field_name="conflicts_with_item_id",
                old_value=None,
                new_value=json.dumps(
                    {
                        "verdict": conflict_result.verdict.value,
                        "action": action.value,
                        "conflict_type": conflict_result.conflict_type,
                        "similarity": conflict_result.similarity,
                        "existing_item_id": str(conflict_result.existing_item_id),
                        "reason": conflict_result.reason,
                    },
                    sort_keys=True,
                ),
                actor_principal_id=principal_id,
                reason=conflict_result.reason,
            )
        )

    await session.commit()

    return RememberResponse(
        id=item.id,
        status="superseded" if superseded_id is not None else "created",
        review_status=review_status,
        memory_confidence=memory_confidence,
        superseded_id=superseded_id,
    )


@router.post("/recall", response_model=RecallResponse)
async def recall(
    req: RecallRequest,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> RecallResponse:
    """Bounded recall: deterministic startup set or semantic query."""
    from engram.recall import execute_semantic_recall, execute_startup_recall

    mode = req.mode
    if mode not in ("startup", "semantic"):
        raise HTTPException(
            status_code=422,
            detail=f"mode={mode!r} not supported (use 'startup' or 'semantic')",
        )

    if mode == "semantic" and (req.query is None or not req.query.strip()):
        raise HTTPException(
            status_code=422,
            detail="mode='semantic' requires a non-empty query",
        )

    # Resolve RLS context
    tenant_id = await _resolve_tenant_id(session)
    principal_id, _ = await _resolve_principal(session, tenant_id)

    if mode == "semantic":
        # execute_semantic_recall owns the query-embedding generation flow.
        result = await execute_semantic_recall(
            session=session,
            tenant_id=str(tenant_id),
            principal_id=str(principal_id),
            workspace=req.workspace,
            query=req.query or "",
            byte_budget=req.byte_budget,
            token_budget=req.token_budget,
            item_budget=req.item_budget,
        )
    else:
        result = await execute_startup_recall(
            session=session,
            tenant_id=str(tenant_id),
            principal_id=str(principal_id),
            workspace=req.workspace,
            byte_budget=req.byte_budget,
            token_budget=req.token_budget,
        )

    return RecallResponse(
        working_set=result["working_set"],
        item_count=result["item_count"],
        byte_count=result["byte_count"],
        pinned_omitted_count=result["pinned_omitted_count"],
        omitted_count=result["omitted_count"],
        items=result["items"],
        scoring_version=result.get("scoring_version", "v1"),
        config_version=result.get("config_version", "v1"),
        recall_log_id=result.get("recall_log_id"),
        message=result.get("message"),
    )


@router.post("/search", response_model=SearchResponse)
async def search(
    req: SearchRequest,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> SearchResponse:
    """Keyword, semantic, or hybrid search.

    All three modes resolve the caller's tenant/principal and apply the
    shared read-eligibility predicate (``engram.memory_access``) so a caller
    never sees another tenant's memory, another principal's private memory,
    or workspace memory from a workspace they aren't a member of.
    """
    limit = req.limit
    mode = req.mode

    tenant_id = await _resolve_tenant_id(session)
    principal_id, _ = await _resolve_principal(session, tenant_id)

    if mode == "keyword":
        results = await _keyword_search(
            session, req.query, limit, tenant_id=tenant_id, principal_id=principal_id
        )
        return SearchResponse(results=results, total=len(results))

    query_embedding = await generate_embedding(req.query)
    semantic_count = await semantic.candidate_count(
        session, tenant_id=tenant_id, principal_id=principal_id
    )

    if mode == "semantic":
        if query_embedding is None or semantic_count == 0:
            return SearchResponse(results=[], total=0, message=_SEARCH_HELPFUL_MESSAGE)
        raw = await semantic.search(
            session, query_embedding, limit, tenant_id=tenant_id, principal_id=principal_id
        )
        if not raw:
            return SearchResponse(results=[], total=0, message=_SEARCH_HELPFUL_MESSAGE)
        results = [_format_semantic_result(row) for row in raw]
        return SearchResponse(results=results, total=len(results))

    keyword_results = await _keyword_search(
        session,
        req.query,
        max(limit * 5, limit),
        tenant_id=tenant_id,
        principal_id=principal_id,
    )
    if query_embedding is None or semantic_count == 0:
        return SearchResponse(
            results=keyword_results[:limit],
            total=min(len(keyword_results), limit),
            message=_SEARCH_HELPFUL_MESSAGE,
        )

    raw_semantic = await semantic.search(
        session,
        query_embedding,
        max(limit * 5, limit),
        tenant_id=tenant_id,
        principal_id=principal_id,
    )
    semantic_results = [_format_semantic_result(row) for row in raw_semantic]
    results = _rrf_fuse(keyword_results, semantic_results, limit=limit)
    return SearchResponse(results=results, total=len(results))


@router.post("/feedback", response_model=None, status_code=201)
async def feedback(
    req: FeedbackRequest,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    """Record feedback on a recalled item.

    Useful feedback incrementally raises importance (capped at 0.95);
    noise lowers it (floor at 0.1). Feedback authority weighting:
    - user/admin: full weight (resets penalty counter, adjusts importance)
    - agent on own memories: zero penalty-reset weight
    - agent on other agent's memories: partial weight (0.5x) on importance
    """
    tenant_id = await _resolve_tenant_id(session)
    principal_id, principal_type = await _resolve_principal(session, tenant_id)

    # Verify the item exists
    item = await _fetch_item(session, req.item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")

    # Determine authority
    is_own_memory = str(item["principal_id"]) == str(principal_id)
    is_user_or_admin = principal_type in ("user", "admin")

    # Log the feedback event
    session.add(
        FeedbackEvent(
            tenant_id=tenant_id,
            item_id=req.item_id,
            principal_id=principal_id,
            verdict=req.feedback,
            recall_log_id=req.recall_log_id,
        )
    )

    if req.feedback == "useful":
        if is_user_or_admin:
            # Full weight: reset penalty counter, raise importance
            await session.execute(
                update(MemoryItem)
                .where(MemoryItem.id == req.item_id)
                .values(
                    startup_recall_count=0,
                    importance=func.least(
                        func.greatest(item["importance"] + 0.05, 0.1), 0.95
                    ),
                )
            )
        elif not is_own_memory:
            # Agent on another agent's memory: partial weight (0.5x)
            await session.execute(
                update(MemoryItem)
                .where(MemoryItem.id == req.item_id)
                .values(
                    importance=func.least(
                        func.greatest(item["importance"] + 0.025, 0.1), 0.95
                    ),
                )
            )
        # Agent on own memory: no-op for importance/penalty
    else:  # noise
        if is_user_or_admin:
            # Full weight: lower importance
            await session.execute(
                update(MemoryItem)
                .where(MemoryItem.id == req.item_id)
                .values(
                    importance=func.least(
                        func.greatest(item["importance"] - 0.1, 0.1), 0.95
                    ),
                )
            )
        elif not is_own_memory:
            # Agent on another agent's memory: partial weight
            await session.execute(
                update(MemoryItem)
                .where(MemoryItem.id == req.item_id)
                .values(
                    importance=func.least(
                        func.greatest(item["importance"] - 0.05, 0.1), 0.95
                    ),
                )
            )
        # Agent on own memory: no-op for importance

    await session.commit()

    return {"status": "recorded", "feedback": req.feedback, "item_id": str(req.item_id)}


class ItemMetadataPatchRequest(BaseModel):
    wing: str | None = None
    room: str | None = None
    visibility: str | None = None
    importance: float | None = None
    pinned: bool | None = None
    actor_principal_id: UUID | None = None
    reason: str | None = None


class MutationAuditRequest(BaseModel):
    actor_principal_id: UUID | None = None
    reason: str | None = None


MUTATION_FIELDS = ("wing", "room", "visibility", "importance", "pinned")


def _now_dt() -> datetime:
    return datetime.now(UTC)


def _row_to_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    mapping = getattr(row, "_mapping", row)
    data = dict(mapping)
    for key in ("human_verified", "pinned"):
        value = data.get(key)
        if isinstance(value, int):
            data[key] = bool(value)
    return data


def _stringify(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


def _encode_cursor(item: dict[str, Any]) -> str:
    payload = {
        "created_at": _stringify(item["created_at"]),
        "id": _stringify(item["id"]),
    }
    data = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    padding = "=" * (-len(cursor) % 4)
    data = base64.urlsafe_b64decode((cursor + padding).encode()).decode()
    payload = json.loads(data)
    created_at = payload.get("created_at")
    item_id = payload.get("id")
    if not isinstance(created_at, str) or not isinstance(item_id, str):
        raise ValueError("invalid cursor")
    return created_at, item_id


async def _fetch_item(session: AsyncSession, item_id: UUID) -> dict[str, Any] | None:
    """Fetch a memory item by id, with no tenant/visibility scoping.

    Used only by mutation endpoints (patch/supersede/invalidate/review/verify)
    that operate on an item an internal caller already has a reference to —
    not a caller-facing read path. Caller-facing reads must use
    :func:`_fetch_readable_item` instead, which applies the shared
    eligibility predicate.
    """
    result = await session.execute(
        text("SELECT * FROM memory_items WHERE id = :item_id OR id = :item_id_hex"),
        {"item_id": str(item_id), "item_id_hex": item_id.hex},
    )
    row = result.mappings().first()
    return _row_to_dict(row) if row else None


async def _fetch_readable_item(
    session: AsyncSession,
    item_id: UUID,
    *,
    tenant_id: UUID | str,
    principal_id: UUID | str,
) -> dict[str, Any] | None:
    """Fetch a memory item for a caller-facing read, applying the shared
    tenant + visibility eligibility predicate (``engram.memory_access``).

    Returns ``None`` both when the item doesn't exist and when it exists but
    the caller is ineligible to read it — callers should map both cases to a
    404, never a 403, to avoid disclosing item existence.
    """
    stmt = text(
        "SELECT * FROM memory_items WHERE (id = :item_id OR id = :item_id_hex) "
        f"AND {tenant_sql()} AND {eligibility_sql()}"
    )
    result = await session.execute(
        stmt,
        {
            "item_id": str(item_id),
            "item_id_hex": item_id.hex,
            "caller_tenant_id": str(tenant_id),
            "caller_principal_id": str(principal_id),
        },
    )
    row = result.mappings().first()
    return _row_to_dict(row) if row else None


async def _fetch_events(session: AsyncSession, item_id: UUID) -> list[dict[str, Any]]:
    result = await session.execute(
        text("SELECT * FROM item_events WHERE item_id = :item_id ORDER BY created_at ASC, id ASC"),
        {"item_id": str(item_id)},
    )
    return [dict(row) for row in result.mappings().all()]


async def _fetch_kg_facts(session: AsyncSession, item_id: UUID) -> list[dict[str, Any]]:
    result = await session.execute(
        text(
            "SELECT * FROM kg_triples WHERE source_item_id = :item_id "
            "ORDER BY created_at ASC, id ASC"
        ),
        {"item_id": str(item_id)},
    )
    return [dict(row) for row in result.mappings().all()]


async def _insert_item_event(
    session: AsyncSession,
    *,
    item_id: UUID,
    event_type: str,
    field_name: str | None,
    old_value: Any,
    new_value: Any,
    actor_principal_id: UUID | None,
    reason: str | None,
) -> dict[str, Any]:
    event = {
        "id": uuid.uuid4(),
        "item_id": item_id,
        "event_type": event_type,
        "field_name": field_name,
        "old_value": _stringify(old_value),
        "new_value": _stringify(new_value),
        "actor_principal_id": actor_principal_id,
        "reason": reason,
        "created_at": _now_dt(),
    }
    await session.execute(insert(ItemEvent).values(**event))
    return event


async def _require_item(session: AsyncSession, item_id: UUID) -> dict[str, Any]:
    item = await _fetch_item(session, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return item


@router.get("/items", response_model=None)
async def list_items(
    workspace: str | None = None,
    kind: str | None = None,
    wing: str | None = None,
    room: str | None = None,
    active_only: bool = True,
    limit: int = 50,
    cursor: str | None = None,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    """List items with stable cursor pagination, scoped to the caller's
    tenant and read eligibility (``engram.memory_access``)."""
    limit = max(1, min(limit, 100))
    tenant_id = await _resolve_tenant_id(session)
    principal_id, _ = await _resolve_principal(session, tenant_id)

    workspace_id, workspace_accessible = await resolve_workspace_scope(
        session, tenant_id=tenant_id, principal_id=principal_id, workspace=workspace
    )
    if workspace is not None and not workspace_accessible:
        return {"items": [], "count": 0, "next_cursor": None, "cursor": None}

    clauses: list[str] = [tenant_sql(), eligibility_sql()]
    params: dict[str, Any] = {
        "limit": limit + 1,
        "caller_tenant_id": str(tenant_id),
        "caller_principal_id": str(principal_id),
    }
    if workspace_id is not None:
        clauses.append("workspace_id = :workspace_id")
        params["workspace_id"] = workspace_id
    if kind is not None:
        clauses.append("kind = :kind")
        params["kind"] = kind
    if wing is not None:
        clauses.append("wing = :wing")
        params["wing"] = wing
    if room is not None:
        clauses.append("room = :room")
        params["room"] = room
    if active_only:
        clauses.append("review_status = 'active' AND valid_to IS NULL AND superseded_by IS NULL")
    if cursor is not None:
        try:
            created_at, item_id = _decode_cursor(cursor)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid cursor") from exc
        clauses.append(
            "(created_at < :cursor_created_at OR "
            "(created_at = :cursor_created_at AND id < :cursor_id))"
        )
        params["cursor_created_at"] = created_at
        params["cursor_id"] = item_id
    sql = "SELECT * FROM memory_items"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC, id DESC LIMIT :limit"
    result = await session.execute(text(sql), params)
    rows = [_row_to_dict(row) for row in result.mappings().all()]
    page = rows[:limit]
    next_cursor = _encode_cursor(page[-1]) if len(rows) > limit and page else None
    return {
        "items": page,
        "count": len(page),
        "next_cursor": next_cursor,
        "cursor": next_cursor,
    }


@router.get("/items/{item_id}", response_model=None)
async def get_item(
    item_id: UUID,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    """Full detail with provenance and linked KG facts.

    Scoped to the caller's tenant + read eligibility (``engram.memory_access``);
    an ineligible item is indistinguishable from a nonexistent one (404, not
    403) so its existence is never disclosed.
    """
    tenant_id = await _resolve_tenant_id(session)
    principal_id, _ = await _resolve_principal(session, tenant_id)
    item = await _fetch_readable_item(
        session, item_id, tenant_id=tenant_id, principal_id=principal_id
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    events = await _fetch_events(session, item_id)
    kg_facts = await _fetch_kg_facts(session, item_id)
    return {
        "item": item,
        "events": events,
        "item_events": events,
        "kg_facts": kg_facts,
        "linked_kg_facts": kg_facts,
    }


@router.patch("/items/{item_id}", response_model=None)
async def update_item_metadata(
    item_id: UUID,
    req: ItemMetadataPatchRequest,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    """Update metadata (wing, room, visibility, importance, pinned) — not content."""
    item = await _require_item(session, item_id)
    actor = req.actor_principal_id or UUID(str(item["principal_id"]))
    changes: list[dict[str, Any]] = []
    for field in MUTATION_FIELDS:
        new_value = getattr(req, field)
        if new_value is None:
            continue
        old_value = item.get(field)
        if old_value == new_value:
            continue
        changes.append({"field": field, "old": old_value, "new": new_value})

    events: list[dict[str, Any]] = []
    for change in changes:
        event = await _insert_item_event(
            session,
            item_id=item_id,
            event_type="metadata_patch",
            field_name=change["field"],
            old_value=change["old"],
            new_value=change["new"],
            actor_principal_id=actor,
            reason=req.reason,
        )
        await session.execute(
            text(f"UPDATE memory_items SET {change['field']} = :value WHERE id = :item_id"),
            {"value": change["new"], "item_id": str(item_id)},
        )
        events.append(event)
    updated = await _require_item(session, item_id)
    await session.commit()
    return {"item": updated, "event": events[0] if events else None, "events": events}


@router.post("/items/{item_id}/supersede", response_model=None)
async def supersede_item(
    item_id: UUID,
    req: MutationAuditRequest | None = None,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    """Mark superseded + write replacement."""
    item = await _require_item(session, item_id)
    now = _now_dt()
    new_id = uuid.uuid4()
    replacement = dict(item)
    replacement.update(
        {
            "id": new_id,
            "valid_from": now,
            "valid_to": None,
            "superseded_by": None,
            "created_at": now,
        }
    )
    for key in (
        "tenant_id",
        "workspace_id",
        "principal_id",
        "verified_by",
        "conflicts_with_item_id",
        "conflict_resolved_by",
    ):
        if replacement.get(key) is not None:
            replacement[key] = UUID(str(replacement[key]))
    actor = req.actor_principal_id if req and req.actor_principal_id else UUID(
        str(item["principal_id"])
    )
    reason = req.reason if req else None
    await session.execute(insert(MemoryItem).values(**replacement))
    event = await _insert_item_event(
        session,
        item_id=item_id,
        event_type="supersede",
        field_name="superseded_by",
        old_value=item.get("superseded_by"),
        new_value=new_id,
        actor_principal_id=actor,
        reason=reason,
    )
    await session.execute(
        text(
            "UPDATE memory_items SET valid_to = :valid_to, superseded_by = :superseded_by "
            "WHERE id = :item_id"
        ),
        {"valid_to": now.isoformat(), "superseded_by": str(new_id), "item_id": str(item_id)},
    )
    old_item = await _require_item(session, item_id)
    new_item = await _require_item(session, new_id)
    await session.commit()
    return {"old_item": old_item, "new_item": new_item, "event": event}


@router.post("/items/{item_id}/invalidate", response_model=None)
async def invalidate_item(
    item_id: UUID,
    req: MutationAuditRequest | None = None,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    """Mark invalid (set valid_to)."""
    item = await _require_item(session, item_id)
    now = _now_dt()
    actor = req.actor_principal_id if req and req.actor_principal_id else UUID(
        str(item["principal_id"])
    )
    reason = req.reason if req else None
    event = await _insert_item_event(
        session,
        item_id=item_id,
        event_type="invalidate",
        field_name="valid_to",
        old_value=item.get("valid_to"),
        new_value=now,
        actor_principal_id=actor,
        reason=reason,
    )
    await session.execute(
        text("UPDATE memory_items SET valid_to = :valid_to WHERE id = :item_id"),
        {"valid_to": now.isoformat(), "item_id": str(item_id)},
    )
    updated = await _require_item(session, item_id)
    await session.commit()
    return {"item": updated, "event": event}
