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
from engram.auth import READ_SCOPE, WRITE_SCOPE
from engram.canonicalize import canonicalize, content_hash
from engram.classification import ClassificationResult, classify_rules_only
from engram.classification_trust import blend_memory_confidence, narrow_visibility
from engram.config import settings
from engram.conflicts import authority_allows_supersession
from engram.db import get_session
from engram.embeddings import create_embedding_placeholder, generate_embedding
from engram.jobs import enqueue_job
from engram.memory_access import eligibility_sql, resolve_workspace_scope, tenant_sql
from engram.memory_kinds import UnknownMemoryKindError, require_enabled_memory_kind
from engram.models import (
    FeedbackEvent,
    ItemEvent,
    MemoryItem,
    TenantConfig,
    Workspace,
)
from engram.safety import has_secrets

router = APIRouter()

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

    # Raw SQL (not the ORM UUID column) so this matches regardless of how a
    # test fixture's SQLite schema happens to store the id string — the ORM's
    # generic UUID type binds a hex literal on non-Postgres dialects, which
    # would never match a plain ``str(uuid4())`` seed row. Mirrors the
    # dual-format lookup already used by ``_fetch_item``.
    result = await session.execute(
        text("SELECT type FROM principals WHERE id = :pid OR id = :pid_hex"),
        {"pid": str(principal_id), "pid_hex": principal_id.hex},
    )
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
    *,
    singleton: bool,
) -> UUID | None:
    """For singleton kinds (memory_kinds.singleton), find an existing active
    item with the same family key and return its ID for supersession.

    Family key = (tenant, workspace, principal, subject_type, subject_id, kind).
    ``singleton`` comes from the tenant's kind registry (ENG-AUD-010 / F17)
    rather than a hard-coded kind-name set.
    """
    if not singleton:
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

# Semantic search pulls more candidates than the requested limit from the HNSW
# index so trust-weighted re-ranking (engram.semantic) has room to reorder
# within the window before trimming to the caller's limit.
_SEMANTIC_SEARCH_OVERFETCH = 3
_SEMANTIC_SEARCH_OVERFETCH_CAP = 200


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
    kind: str | None = None,
    wing: str | None = None,
    room: str | None = None,
) -> list[dict[str, Any]]:
    """Keyword (full-text) search, scoped to the caller's tenant + visibility.

    Uses the shared raw-SQL eligibility fragment (``engram.memory_access``)
    since this query is over raw ``text(...)`` SQL, not the ORM. Optional
    ``kind``/``wing``/``room`` filters apply with AND semantics before the
    rank/limit so ineligible rows don't displace matches.
    """
    clauses: list[str] = [
        "mi.review_status = 'active'",
        "mi.valid_to IS NULL",
        tenant_sql("mi"),
        eligibility_sql("mi"),
        "mi.content_tsv @@ plainto_tsquery('english', :query)",
    ]
    params: dict[str, Any] = {
        "query": query,
        "limit": limit,
        "caller_tenant_id": str(tenant_id),
        "caller_principal_id": str(principal_id),
    }
    if kind is not None:
        clauses.append("mi.kind = :kind")
        params["kind"] = kind
    if wing is not None:
        clauses.append("mi.wing = :wing")
        params["wing"] = wing
    if room is not None:
        clauses.append("mi.room = :room")
        params["room"] = room
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
        WHERE {" AND ".join(clauses)}
        ORDER BY score DESC, mi.created_at DESC
        LIMIT :limit
        """
    )
    rows = (await session.execute(stmt, params)).mappings().all()
    return [
        _search_result_row(row, mode="keyword", score=float(row["score"] or 0.0)) for row in rows
    ]


def _format_semantic_result(row: dict[str, Any]) -> dict[str, Any]:
    """Shape a semantic.search() row into the /v1/search response format.

    ``score`` is the final trust-weighted semantic score; ``distance``,
    ``similarity_score`` and ``trust_score`` are exposed for transparency so
    callers can explain the ordering.
    """
    return _search_result_row(
        row,
        mode="semantic",
        score=float(row.get("score", 0.0)),
        extra={
            "distance": row.get("distance"),
            "similarity_score": row.get("similarity_score"),
            "trust_score": row.get("trust_score"),
            "embedding_model": row.get("embedding_model"),
            "embedding_dim": row.get("embedding_dim"),
            "embedding_profile": row.get("embedding_profile"),
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
        if "embedding_profile" not in entry and "embedding_profile" in result:
            entry["embedding_profile"] = result["embedding_profile"]
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


@router.post(
    "/remember",
    response_model=RememberResponse,
    status_code=201,
    dependencies=[Depends(WRITE_SCOPE)],
)
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
        # Synchronous rule-based classification only (ENG-AUD-008 / F20): the
        # OpenAI LLM refinement runs later via an async classification.refine
        # job, so the request path never blocks on a provider call. The
        # classifier's taxonomy is already the tenant's enabled registry
        # (engram.memory_kinds), so suggested_kind is always registry-valid.
        classification_result = await classify_rules_only(req.content, tenant_id, session)
        kind = classification_result.suggested_kind
        if wing is None:
            wing = classification_result.suggested_wing
        if room is None:
            room = classification_result.suggested_room

    # 3b. Validate kind against the tenant's governed registry (ENG-AUD-010 /
    # F17). An explicit caller-supplied kind that is unknown or disabled fails
    # with a clear 4xx — it is never silently coerced to a default.
    try:
        kind_row = await require_enabled_memory_kind(session, tenant_id, kind)
    except UnknownMemoryKindError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # 4. Trust defaults from tenant_config.
    source_trust, memory_confidence, review_status = await _resolve_trust_defaults(
        session, tenant_id, req.source_type, principal_type
    )
    # The kind's governance flag decides whether a write must begin as
    # proposed — regardless of whether the kind came from an explicit request
    # or the classifier (generalizes the old classifier-only
    # suggested_kind == "decision" special case, ENG-AUD-010 / F17).
    if kind_row.requires_review:
        review_status = "proposed"

    # 4b. Classification → trust/visibility wiring (only when classification ran).
    # The classifier may refine memory_confidence (capped by source authority so
    # weak automated sources can't self-promote) and narrow — never widen — the
    # requested visibility. Explicit-kind writes skip classification entirely, so
    # their confidence/visibility come straight from the request/defaults.
    default_confidence = memory_confidence
    final_visibility = req.visibility
    suggested_visibility: str | None = None
    visibility_narrowed = False
    memory_confidence_blended = False
    if classification_result is not None:
        suggested_visibility = classification_result.suggested_visibility
        final_visibility = narrow_visibility(req.visibility, suggested_visibility)
        visibility_narrowed = final_visibility != req.visibility
        memory_confidence, memory_confidence_blended = blend_memory_confidence(
            source_default_confidence=default_confidence,
            classifier_confidence=classification_result.confidence,
            source_trust=source_trust,
            source_type=req.source_type,
        )

    # 5. Supersession check for singleton kinds.
    superseded_id = await _check_supersession(
        session,
        tenant_id,
        workspace_id,
        principal_id,
        kind,
        req.subject_type,
        req.subject_id,
        singleton=kind_row.singleton,
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
        visibility=final_visibility,
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

    provider = (
        "caller"
        if classification_result is None
        else classification_result.provenance.get("provider", "rule")
    )
    provenance_payload: dict[str, Any] = {
        "source": "explicit_kind" if classification_result is None else "auto_classified",
        "kind": kind,
        "wing": wing,
        "room": room,
        "provider": provider,
        # Trust/visibility audit — present on every classification event so the
        # record is self-describing even for explicit-kind writes (where the
        # classifier did not run and these are the untouched request/defaults).
        "source_type": req.source_type,
        "source_trust": source_trust,
        "default_memory_confidence": default_confidence,
        "final_memory_confidence": memory_confidence,
        "memory_confidence_blended": memory_confidence_blended,
        "requested_visibility": req.visibility,
        "suggested_visibility": suggested_visibility,
        "final_visibility": final_visibility,
        "visibility_narrowed": visibility_narrowed,
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

    # 9. Embeddings are generated OFF the request path (ENG-AUD-008 / F20).
    # Create the pending placeholder synchronously so the row exists, then
    # enqueue an embedding.generate job. The provider is never called inline,
    # so /v1/remember returns without waiting on OpenAI. The worker fills in
    # the vector later and (when conflict_check_on_write is set) enqueues a
    # conflict.check job that runs the now-embedding-dependent semantic dedup /
    # auto-supersede / contradiction detection as eventual state transitions.
    if settings.embedding_provider != "none":
        from engram.embedding_profiles import get_writable_profiles

        profiles = await get_writable_profiles(session)
        for profile in profiles:
            await create_embedding_placeholder(session, item.id, tenant_id, profile)
        await session.flush()
        for profile in profiles:
            await enqueue_job(
                session,
                tenant_id=tenant_id,
                job_type="embedding.generate",
                payload={
                    "memory_item_id": str(item.id),
                    "profile_id": str(profile.id),
                    "profile_key": profile.profile_key,
                },
                dedupe_key=f"embedding.generate:{item.id}:{profile.id}",
            )

    await session.commit()

    return RememberResponse(
        id=item.id,
        status="superseded" if superseded_id is not None else "created",
        review_status=review_status,
        memory_confidence=memory_confidence,
        superseded_id=superseded_id,
    )


@router.post("/recall", response_model=RecallResponse, dependencies=[Depends(READ_SCOPE)])
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


@router.post("/search", response_model=SearchResponse, dependencies=[Depends(READ_SCOPE)])
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
    # kind/wing/room filters apply (AND semantics) to all three search modes.
    kind = req.kind
    wing = req.wing
    room = req.room

    tenant_id = await _resolve_tenant_id(session)
    principal_id, _ = await _resolve_principal(session, tenant_id)

    if mode == "keyword":
        results = await _keyword_search(
            session,
            req.query,
            limit,
            tenant_id=tenant_id,
            principal_id=principal_id,
            kind=kind,
            wing=wing,
            room=room,
        )
        return SearchResponse(results=results, total=len(results))

    import inspect

    from engram.embedding_profiles import get_active_profile

    active_profile = await get_active_profile(session)
    if len(inspect.signature(generate_embedding).parameters) >= 2:
        query_embedding = await generate_embedding(req.query, active_profile)
    else:
        query_embedding = await generate_embedding(req.query)
    semantic_count = await semantic.candidate_count(
        session,
        tenant_id=tenant_id,
        principal_id=principal_id,
        kind=kind,
        wing=wing,
        room=room,
        profile=active_profile,
    )

    if mode == "semantic":
        if query_embedding is None or semantic_count == 0:
            return SearchResponse(results=[], total=0, message=_SEARCH_HELPFUL_MESSAGE)
        # Over-fetch so trust-weighted re-ranking can reorder within the window
        # before trimming to the caller's requested limit.
        fetch_limit = min(limit * _SEMANTIC_SEARCH_OVERFETCH, _SEMANTIC_SEARCH_OVERFETCH_CAP)
        raw = await semantic.search(
            session,
            query_embedding,
            fetch_limit,
            tenant_id=tenant_id,
            principal_id=principal_id,
            kind=kind,
            wing=wing,
            room=room,
            profile=active_profile,
        )
        if not raw:
            return SearchResponse(results=[], total=0, message=_SEARCH_HELPFUL_MESSAGE)
        results = [_format_semantic_result(row) for row in raw[:limit]]
        return SearchResponse(results=results, total=len(results))

    keyword_results = await _keyword_search(
        session,
        req.query,
        max(limit * 5, limit),
        tenant_id=tenant_id,
        principal_id=principal_id,
        kind=kind,
        wing=wing,
        room=room,
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
        kind=kind,
        wing=wing,
        room=room,
        profile=active_profile,
    )
    semantic_results = [_format_semantic_result(row) for row in raw_semantic]
    results = _rrf_fuse(keyword_results, semantic_results, limit=limit)
    return SearchResponse(results=results, total=len(results))


@router.post(
    "/feedback", response_model=None, status_code=201, dependencies=[Depends(WRITE_SCOPE)]
)
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

    # Missing and caller-ineligible items deliberately share the same response.
    item = await _fetch_readable_item(
        session, req.item_id, tenant_id=tenant_id, principal_id=principal_id
    )
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
                    importance=func.least(func.greatest(item["importance"] + 0.05, 0.1), 0.95),
                )
            )
        elif not is_own_memory:
            # Agent on another agent's memory: partial weight (0.5x)
            await session.execute(
                update(MemoryItem)
                .where(MemoryItem.id == req.item_id)
                .values(
                    importance=func.least(func.greatest(item["importance"] + 0.025, 0.1), 0.95),
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
                    importance=func.least(func.greatest(item["importance"] - 0.1, 0.1), 0.95),
                )
            )
        elif not is_own_memory:
            # Agent on another agent's memory: partial weight
            await session.execute(
                update(MemoryItem)
                .where(MemoryItem.id == req.item_id)
                .values(
                    importance=func.least(func.greatest(item["importance"] - 0.05, 0.1), 0.95),
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
    actor_principal_id: UUID | None = Field(
        default=None,
        deprecated=True,
        description=(
            "Deprecated and ignored — the event actor is always the "
            "authenticated caller. Use on_behalf_of_principal_id for "
            "admin-scoped delegation."
        ),
    )
    on_behalf_of_principal_id: UUID | None = Field(
        default=None,
        description=(
            "Admin-only. Records this principal as the represented party in "
            "the event's audit metadata. Does not change who the event "
            "actor is — the authenticated caller remains the actor."
        ),
    )
    reason: str | None = None


class MutationAuditRequest(BaseModel):
    actor_principal_id: UUID | None = Field(
        default=None,
        deprecated=True,
        description=(
            "Deprecated and ignored — the event actor is always the "
            "authenticated caller. Use on_behalf_of_principal_id for "
            "admin-scoped delegation."
        ),
    )
    on_behalf_of_principal_id: UUID | None = Field(
        default=None,
        description=(
            "Admin-only. Records this principal as the represented party in "
            "the event's audit metadata. Does not change who the event "
            "actor is — the authenticated caller remains the actor."
        ),
    )
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


async def _fetch_readable_item(
    session: AsyncSession,
    item_id: UUID,
    *,
    tenant_id: UUID | str,
    principal_id: UUID | str,
    for_update: bool = False,
) -> dict[str, Any] | None:
    """Fetch a memory item for a caller-facing read, applying the shared
    tenant + visibility eligibility predicate (``engram.memory_access``).

    Returns ``None`` both when the item doesn't exist and when it exists but
    the caller is ineligible to read it — callers should map both cases to a
    404, never a 403, to avoid disclosing item existence.
    """
    dialect_name = session.bind.dialect.name if session.bind is not None else None
    lock_clause = " FOR UPDATE" if for_update and dialect_name == "postgresql" else ""
    stmt = text(
        "SELECT * FROM memory_items WHERE (id = :item_id OR id = :item_id_hex) "
        f"AND {tenant_sql()} AND {eligibility_sql()}{lock_clause}"
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


def _encode_delegation_reason(
    reason: str | None, on_behalf_of_principal_id: UUID | None
) -> str | None:
    """Fold admin delegation into the event's ``reason`` column.

    ``item_events`` has no dedicated structured-details column and this slice
    must not add one (see V2-BL-001), so delegation is carried as a small JSON
    envelope in ``reason`` — but only when delegation is actually present.
    With no delegation, ``reason`` is stored exactly as given: full backward
    compatibility for every existing caller/test. The caller-supplied
    ``reason`` text is nested under the ``"reason"`` key, so it can never
    collide with or overwrite the server-set ``on_behalf_of_principal_id`` key
    at the envelope's top level.
    """
    if on_behalf_of_principal_id is None:
        return reason
    return json.dumps(
        {"reason": reason, "on_behalf_of_principal_id": str(on_behalf_of_principal_id)},
        sort_keys=True,
    )


async def _resolve_actor_and_delegation(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    requested_on_behalf_of: UUID | None,
) -> tuple[UUID, UUID | None]:
    """Resolve the item_events actor and validate any requested delegation.

    The actor is always the authenticated caller (read from the RLS session
    context established by ``get_session``/``apply_rls_context`` off the
    resolved auth ``Principal``) — never a request-supplied value, and never
    the item's own author. This is the single choke point every caller-facing
    mutation route must go through so a spoofed ``actor_principal_id`` in a
    request body can never reach an event-write.

    ``requested_on_behalf_of`` is the caller's *explicit* delegation request
    (``on_behalf_of_principal_id``, distinct from the deprecated/ignored
    ``actor_principal_id``). Only a caller whose principal ``type='admin'`` —
    this codebase's existing authority marker for elevated actions (see
    ``_resolve_trust_defaults`` and ``feedback``'s authority weighting) — may
    supply one; it must resolve to a principal in the caller's own tenant.
    Raises 403 (non-admin) or a non-disclosing 404 (missing/cross-tenant
    principal) *before* any mutation is attempted, so a rejected delegation
    request never leaves a partial mutation or event committed.
    """
    actor_id, actor_type = await _resolve_principal(session, tenant_id)
    if requested_on_behalf_of is None:
        return actor_id, None
    if actor_type != "admin":
        raise HTTPException(
            status_code=403, detail="on_behalf_of_principal_id requires admin authority"
        )
    represented = await session.execute(
        text(
            "SELECT 1 FROM principals WHERE (id = :pid OR id = :pid_hex) AND tenant_id = :tid"
        ),
        {
            "pid": str(requested_on_behalf_of),
            "pid_hex": requested_on_behalf_of.hex,
            "tid": str(tenant_id),
        },
    )
    if represented.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="principal not found")
    return actor_id, requested_on_behalf_of


async def _insert_item_event(
    session: AsyncSession,
    *,
    item_id: UUID,
    event_type: str,
    field_name: str | None,
    old_value: Any,
    new_value: Any,
    actor_principal_id: UUID,
    reason: str | None,
    on_behalf_of_principal_id: UUID | None = None,
) -> dict[str, Any]:
    """Write an ``item_events`` audit row. ``actor_principal_id`` is required —
    always the authenticated caller (see :func:`_resolve_actor_and_delegation`),
    never a caller-facing request model. Delegation, when present, is folded
    into ``reason`` (see :func:`_encode_delegation_reason`); the returned dict
    reflects the same encoding stored on the row.
    """
    event = {
        "id": uuid.uuid4(),
        "item_id": item_id,
        "event_type": event_type,
        "field_name": field_name,
        "old_value": _stringify(old_value),
        "new_value": _stringify(new_value),
        "actor_principal_id": actor_principal_id,
        "reason": _encode_delegation_reason(reason, on_behalf_of_principal_id),
        "created_at": _now_dt(),
    }
    await session.execute(insert(ItemEvent).values(**event))
    return event


async def _require_eligible_item(
    session: AsyncSession,
    item_id: UUID,
    *,
    tenant_id: UUID | str,
    principal_id: UUID | str,
    for_update: bool = False,
) -> dict[str, Any]:
    """Resolve a caller-supplied mutation target through read eligibility."""
    item = await _fetch_readable_item(
        session,
        item_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        for_update=for_update,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return item


@router.get("/items", response_model=None, dependencies=[Depends(READ_SCOPE)])
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


@router.get("/items/{item_id}", response_model=None, dependencies=[Depends(READ_SCOPE)])
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


@router.patch("/items/{item_id}", response_model=None, dependencies=[Depends(WRITE_SCOPE)])
async def update_item_metadata(
    item_id: UUID,
    req: ItemMetadataPatchRequest,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    """Update metadata (wing, room, visibility, importance, pinned) — not content."""
    tenant_id = await _resolve_tenant_id(session)
    principal_id, _ = await _resolve_principal(session, tenant_id)
    item = await _require_eligible_item(
        session, item_id, tenant_id=tenant_id, principal_id=principal_id
    )
    actor, on_behalf_of = await _resolve_actor_and_delegation(
        session, tenant_id=tenant_id, requested_on_behalf_of=req.on_behalf_of_principal_id
    )
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
            on_behalf_of_principal_id=on_behalf_of,
            reason=req.reason,
        )
        await session.execute(
            text(f"UPDATE memory_items SET {change['field']} = :value WHERE id = :item_id"),
            {"value": change["new"], "item_id": str(item_id)},
        )
        events.append(event)
    updated = await _require_eligible_item(
        session, item_id, tenant_id=tenant_id, principal_id=principal_id
    )
    await session.commit()
    return {"item": updated, "event": events[0] if events else None, "events": events}


@router.post(
    "/items/{item_id}/supersede", response_model=None, dependencies=[Depends(WRITE_SCOPE)]
)
async def supersede_item(
    item_id: UUID,
    req: MutationAuditRequest | None = None,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    """Atomically expire an item and write its replacement.

    Ordering is load-bearing for the partial unique index ``idx_memitems_dedup``
    (``WHERE valid_to IS NULL AND review_status != 'rejected'``): the original
    is expired *before* the replacement is inserted, so the two never satisfy
    the index predicate with identical ``(tenant, workspace, principal,
    content_hash)`` keys at the same time. The whole operation runs in one
    transaction — a failure after expiring the original but before inserting
    the replacement rolls the expiration back.
    """
    tenant_id = await _resolve_tenant_id(session)
    principal_id, _ = await _resolve_principal(session, tenant_id)
    item = await _require_eligible_item(
        session,
        item_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        for_update=True,
    )
    actor, on_behalf_of = await _resolve_actor_and_delegation(
        session,
        tenant_id=tenant_id,
        requested_on_behalf_of=req.on_behalf_of_principal_id if req else None,
    )

    # The eligible lookup also locks the original row, avoiding a policy-check/lock gap.

    # 2. Eligibility: cannot supersede an item that is already expired or
    #    rejected — it has left the active set the replacement would target.
    if item.get("valid_to") is not None or item.get("review_status") == "rejected":
        raise HTTPException(
            status_code=409,
            detail="Item is already expired or rejected and cannot be superseded",
        )

    # 3. Authority: a lower-authority source must not silently replace a
    #    higher-authority memory (design §4). The replacement is a clone, so
    #    by default authority is equal and the gate passes — but it is present
    #    structurally so a future divergent-authority supersede is enforced.
    if not authority_allows_supersession(
        new_trust=float(item["source_trust"]),
        old_trust=float(item["source_trust"]),
    ):
        raise HTTPException(
            status_code=403,
            detail="Lower-authority source may not supersede a higher-authority memory",
        )

    now = _now_dt()
    new_id = uuid.uuid4()
    # Build the replacement from only the ORM-mapped columns. ``item`` comes
    # from ``SELECT * FROM memory_items``, which includes the generated
    # ``content_tsv`` column (STORED GENERATED ALWAYS AS) — inserting it
    # explicitly is rejected by Postgres, and passing it to insert(MemoryItem)
    # raises "Unconsumed column names" because it isn't an ORM attribute.
    mapped_keys = {c.name for c in MemoryItem.__table__.columns}
    replacement = {k: v for k, v in item.items() if k in mapped_keys}
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
    reason = req.reason if req else None

    # 4. Expire the original BEFORE inserting the replacement. Setting
    #    ``valid_to`` removes the original from the idx_memitems_dedup partial
    #    index (predicate: ``valid_to IS NULL AND review_status != 'rejected'``)
    #    so the replacement insert — which reuses the original's content_hash —
    #    cannot violate uniqueness.
    #
    #    The ``superseded_by`` link is set in a SEPARATE update AFTER the
    #    replacement exists, because ``superseded_by`` is a self-FK to
    #    memory_items(id): setting it before the replacement row exists would
    #    violate memory_items_superseded_by_fkey. (``valid_to`` alone carries
    #    no FK, so it is safe to set first.)
    #
    #    Pass ``valid_to`` as a datetime (not an isoformat string) so the
    #    asyncpg driver binds it to the TIMESTAMPTZ column natively.
    await session.execute(
        text("UPDATE memory_items SET valid_to = :valid_to WHERE id = :item_id"),
        {"valid_to": now, "item_id": str(item_id)},
    )

    # 5. Insert the replacement. With the original now expired, the two rows
    #    never both satisfy the dedup index predicate simultaneously.
    await session.execute(insert(MemoryItem).values(**replacement))

    # 6. Link the original forward to its replacement. The replacement row now
    #    exists, so the superseded_by self-FK is satisfied.
    await session.execute(
        text("UPDATE memory_items SET superseded_by = :superseded_by WHERE id = :item_id"),
        {"superseded_by": str(new_id), "item_id": str(item_id)},
    )

    # 7. Record provenance both ways: the original points forward to its
    #    replacement, and the replacement points back at what it replaced.
    event = await _insert_item_event(
        session,
        item_id=item_id,
        event_type="supersede",
        field_name="superseded_by",
        old_value=item.get("superseded_by"),
        new_value=new_id,
        actor_principal_id=actor,
        on_behalf_of_principal_id=on_behalf_of,
        reason=reason,
    )
    replacement_event = await _insert_item_event(
        session,
        item_id=new_id,
        event_type="supersede",
        field_name="replaces",
        old_value=None,
        new_value=item_id,
        actor_principal_id=actor,
        on_behalf_of_principal_id=on_behalf_of,
        reason=reason,
    )
    old_item = await _require_eligible_item(
        session, item_id, tenant_id=tenant_id, principal_id=principal_id
    )
    new_item = await _require_eligible_item(
        session, new_id, tenant_id=tenant_id, principal_id=principal_id
    )
    await session.commit()
    return {
        "old_item": old_item,
        "new_item": new_item,
        "event": event,
        "replacement_event": replacement_event,
    }


@router.post(
    "/items/{item_id}/invalidate", response_model=None, dependencies=[Depends(WRITE_SCOPE)]
)
async def invalidate_item(
    item_id: UUID,
    req: MutationAuditRequest | None = None,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    """Mark invalid (set valid_to)."""
    tenant_id = await _resolve_tenant_id(session)
    principal_id, _ = await _resolve_principal(session, tenant_id)
    item = await _require_eligible_item(
        session, item_id, tenant_id=tenant_id, principal_id=principal_id
    )
    actor, on_behalf_of = await _resolve_actor_and_delegation(
        session,
        tenant_id=tenant_id,
        requested_on_behalf_of=req.on_behalf_of_principal_id if req else None,
    )
    now = _now_dt()
    reason = req.reason if req else None
    event = await _insert_item_event(
        session,
        item_id=item_id,
        event_type="invalidate",
        field_name="valid_to",
        old_value=item.get("valid_to"),
        new_value=now,
        actor_principal_id=actor,
        on_behalf_of_principal_id=on_behalf_of,
        reason=reason,
    )
    await session.execute(
        text("UPDATE memory_items SET valid_to = :valid_to WHERE id = :item_id"),
        {"valid_to": now, "item_id": str(item_id)},
    )
    updated = await _require_eligible_item(
        session, item_id, tenant_id=tenant_id, principal_id=principal_id
    )
    await session.commit()
    return {"item": updated, "event": event}
