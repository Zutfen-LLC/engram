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

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from engram import semantic, trust_policy
from engram.auth import READ_SCOPE, WRITE_SCOPE
from engram.auth import Principal as AuthPrincipal
from engram.authority import authority_allows_supersession, authority_label, derive_memory_authority
from engram.candidate_ingests import (
    CandidateIdentity,
    create_ingest,
    get_ingest,
    identity_mismatches,
)
from engram.canonicalize import canonicalize, content_hash
from engram.classification import ClassificationResult, classify_rules_only
from engram.classification_evidence import bind_run, lock_run
from engram.classification_trust import narrow_visibility
from engram.config import settings
from engram.db import get_session
from engram.embeddings import create_embedding_placeholder, generate_embedding
from engram.feedback import (
    FeedbackRateLimitError,
    FeedbackResult,
    RecallLogItemMismatchError,
    RecallLogNotFoundError,
    record_feedback,
)
from engram.jobs import enqueue_job
from engram.memory_access import (
    principal_eligibility_sql,
    read_eligibility_sql,
    resolve_workspace_scope,
)
from engram.memory_context import ResolvedMemoryContext, resolve_memory_context
from engram.memory_kinds import UnknownMemoryKindError, require_enabled_memory_kind
from engram.memory_scope import resolve_write_scope
from engram.models import (
    CandidateIngest,
    ClassificationRun,
    ItemEvent,
    MemoryItem,
    Principal,
)
from engram.safety import has_secrets
from engram.source_types import SourceType
from engram.trust_policy import resolve_trust_defaults
from engram.usage import (
    EmbeddingOutcome,
    Timer,
    embedding_call_occurred_for,
    record_candidate_once,
    record_candidate_outcome,
    record_ingest_reuse_rejected,
    record_retrieval_request,
)

# Backward-compatible test/import alias; canonical implementation lives in trust_policy.
_resolve_trust_defaults = resolve_trust_defaults
_SOURCE_TRUST_KEYS = trust_policy._SOURCE_TRUST_KEYS
_TRUST_FALLBACKS = trust_policy._TRUST_FALLBACKS

router = APIRouter()

# source_type values that default to review_status='active'.
_ACTIVE_SOURCES = {"manual", "import", "migration"}


# ---- Request/response models ----

# Backward-compatible alias — canonical vocabulary now lives in
# engram.source_types (shared with /v1/classify so the two never drift).
SourceKind = SourceType
PrincipalKind = Literal["user", "agent", "system", "admin"]
SensitivityKind = Literal["normal", "sensitive", "restricted"]


class RememberRequest(BaseModel):
    content: str
    kind: str | None = None  # fact|preference|doctrine|decision|invariant|observation|diary_entry
    wing: str | None = None
    room: str | None = None
    workspace: str | None = None
    # Omitted/null resolves safely (ENG-SCOPE-001): private with no workspace,
    # workspace-shared when an authorized workspace is supplied. See
    # engram.memory_scope.resolve_write_scope for the full resolution table.
    visibility: str | None = None
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
    classification_run_id: UUID | None = None
    # Optional client trace shared with a preceding /v1/classify call. It is
    # never an identity or deduplication key; ingest_id owns those semantics.
    correlation_id: UUID | None = None
    ingest_id: UUID | None = None


class RememberResponse(BaseModel):
    id: UUID
    status: str  # created | deduped | superseded
    review_status: str
    memory_confidence: float
    deduped_existing_id: UUID | None = None
    superseded_id: UUID | None = None
    # Additive, backward-compatible: the effective correlation id (echoed back
    # if the caller supplied one, otherwise server-generated).
    correlation_id: UUID
    ingest_id: UUID
    attempt_id: UUID


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


class FeedbackResponse(BaseModel):
    status: Literal["recorded", "updated", "unchanged"]
    item_id: UUID
    feedback: Literal["useful", "noise"]
    previous_feedback: Literal["useful", "noise"] | None = None
    feedback_event_id: UUID
    importance: float
    startup_recall_count: int


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
    new_authority: int,
) -> tuple[UUID | None, dict[str, Any] | None]:
    """For singleton kinds (memory_kinds.singleton), find an existing active
    item with the same family key and return its ID for supersession.

    Family key = (tenant, workspace, principal, subject_type, subject_id, kind).
    ``singleton`` comes from the tenant's kind registry (ENG-AUD-010 / F17)
    rather than a hard-coded kind-name set.
    """
    if not singleton:
        return None, None

    stmt = (
        select(MemoryItem.id, MemoryItem.authority)
        .where(
            MemoryItem.tenant_id == tenant_id,
            MemoryItem.principal_id == principal_id,
            MemoryItem.kind == kind,
            MemoryItem.valid_to.is_(None),
            MemoryItem.review_status != "rejected",
        )
        .order_by(MemoryItem.created_at.desc())
        .limit(1)
        .with_for_update()
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
    row = result.mappings().one_or_none()
    if row is None:
        return None, None
    existing = {"id": row["id"], "authority": int(row["authority"])}
    if authority_allows_supersession(
        new_authority=new_authority, old_authority=existing["authority"]
    ):
        return UUID(str(existing["id"])), None
    return None, existing


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
    memory_context: ResolvedMemoryContext,
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
    read_scope = read_eligibility_sql(
        memory_context, alias="mi", parameter_prefix="keyword_item"
    )
    clauses: list[str] = [
        "mi.review_status = 'active'",
        "mi.valid_to IS NULL",
        read_scope.clause,
        "mi.content_tsv @@ plainto_tsquery('english', :query)",
    ]
    params: dict[str, Any] = {
        "query": query,
        "limit": limit,
        **read_scope.params,
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
    """Reciprocal Rank Fusion (pure rank-based).

    Uses only rank positions — raw per-modality scores (ts_rank, cosine
    distance) do NOT bleed into the fused score.  This is critical because
    keyword ts_rank scores (~0.01) and semantic similarity scores (~0.5)
    operate on completely different scales; letting them carry through via
    ``setdefault`` would let semantic-only items dominate items that matched
    in both modalities.
    """
    fused: dict[str, dict[str, Any]] = {}
    for rank, result in enumerate(keyword_results, start=1):
        entry = fused.setdefault(result["id"], dict(result))
        # Overwrite raw score with pure RRF score — do NOT accumulate on top
        # of the raw per-modality score that came from the result dict.
        if entry.get("_rrf_score") is None:
            entry["_rrf_score"] = 0.0
        entry["_rrf_score"] += 1.0 / (_RRF_K + rank)
        entry["score"] = entry["_rrf_score"]
        entry["keyword_rank"] = rank
        entry.setdefault("semantic_rank", None)
        entry["mode"] = "hybrid"
    for rank, result in enumerate(semantic_results, start=1):
        entry = fused.setdefault(result["id"], dict(result))
        if entry.get("_rrf_score") is None:
            entry["_rrf_score"] = 0.0
        entry["_rrf_score"] += 1.0 / (_RRF_K + rank)
        entry["score"] = entry["_rrf_score"]
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
)
async def remember(
    req: RememberRequest,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    caller: AuthPrincipal = Depends(WRITE_SCOPE),  # noqa: B008
) -> RememberResponse:
    """Write a memory item with dedup, trust defaults, and supersession.

    Thin wrapper around :func:`_remember_impl` that owns usage-telemetry
    attribution: it resolves the effective correlation id and
    guarantees exactly one ``candidate.outcome`` event is recorded — status
    ``created``/``deduped``/``superseded`` on success, ``failed`` for every
    other early-return/raised path (inventoried in ``_remember_impl``) —
    without altering the response or re-raising differently. Telemetry
    failures are swallowed inside :mod:`engram.usage` and never reach here.

    Tenant/principal identity is resolved HERE (before ``_remember_impl``) so
    that an outcome is recorded even for early failures the impl raises before
    it would have populated ``outcome_ctx`` — notably the secret-denylist
    rejection and an invalid workspace slug. Identity resolution reads only the
    RLS session context (never request content), so no secret/content can leak
    into telemetry via this path.
    """
    attempt_id = uuid.uuid4()
    correlation_id = req.correlation_id or uuid.uuid4()
    outcome_ctx: dict[str, Any] = {"classification_mode": "explicit" if req.kind else "automatic"}
    # Resolve authenticated identity up front so every failure path (including
    # the secret-denylist rejection inside _remember_impl) is attributable.
    # A 403 here (no tenant/principal context) is an auth failure, not a
    # candidate outcome — it propagates without recording, matching the
    # pre-existing behavior for unauthenticated requests.
    tenant_id = await _resolve_tenant_id(session)
    principal_id, _principal_type = await _resolve_principal(session, tenant_id)
    outcome_ctx["tenant_id"] = tenant_id
    outcome_ctx["principal_id"] = principal_id
    outcome_status = "failed"
    try:
        result = await _remember_impl(
            req, session, correlation_id=correlation_id, outcome_ctx=outcome_ctx,
            tenant_id=tenant_id, principal_id=principal_id,
            principal_type=_principal_type,
            attempt_id=attempt_id,
            caller_has_admin_scope=caller.has_scope("admin"),
        )
        outcome_status = result.status
        return result
    finally:
        await record_candidate_outcome(
            tenant_id=outcome_ctx["tenant_id"],
            principal_id=outcome_ctx.get("principal_id"),
            workspace_id=outcome_ctx.get("workspace_id"),
            correlation_id=correlation_id,
            ingest_id=outcome_ctx.get("ingest_id"),
            attempt_id=attempt_id,
            status=outcome_status,
            source_type=req.source_type,
            final_kind=outcome_ctx.get("final_kind"),
            final_review_status=outcome_ctx.get("final_review_status"),
            final_visibility=outcome_ctx.get("final_visibility"),
            classification_mode=outcome_ctx.get("classification_mode"),
        )


async def _remember_impl(
    req: RememberRequest,
    session: AsyncSession,
    *,
    correlation_id: UUID,
    outcome_ctx: dict[str, Any],
    tenant_id: UUID,
    principal_id: UUID,
    principal_type: str,
    attempt_id: UUID,
    caller_has_admin_scope: bool,
) -> RememberResponse:
    """The actual write logic. See :func:`remember` for the telemetry wrapper.

    ``outcome_ctx`` is mutated in place with tenant/principal/workspace (as
    soon as they are resolved) and final outcome dimensions (right before each
    return), so the wrapper can record a ``candidate.outcome`` event even when
    this function raises partway through.

    ``tenant_id``/``principal_id``/``principal_type`` are resolved by the
    wrapper before this function runs (so an outcome is recorded even for the
    secret-denylist rejection), and are NOT re-resolved here.
    """
    # 1. Secret check — wire existing safety.py denylist.
    if has_secrets(req.content):
        raise HTTPException(
            status_code=422,
            detail="content contains patterns matching secrets/credentials",
        )

    # 2. Canonicalize + hash.
    canonical = canonicalize(req.content)
    chash = content_hash(canonical)

    # 3. Resolve and authorize the write scope (ENG-SCOPE-001): safe default
    # visibility, workspace existence + membership (or admin bypass), and the
    # workspace-visibility-requires-workspace invariant. The resolved values —
    # not req.visibility/req.workspace — are authoritative for everything
    # below (identity, receipt compatibility, item fields, dedup/supersession
    # scope, provenance, telemetry).
    scope = await resolve_write_scope(
        session,
        tenant_id=tenant_id,
        principal_id=principal_id,
        caller_has_admin_scope=caller_has_admin_scope,
        requested_visibility=req.visibility,
        requested_workspace=req.workspace,
    )
    workspace_id = scope.workspace_id
    caller_visibility = scope.visibility
    outcome_ctx["workspace_id"] = workspace_id

    identity = CandidateIdentity(
        tenant_id=tenant_id,
        principal_id=principal_id,
        workspace_id=workspace_id,
        source_type=req.source_type,
        content_hash=chash,
    )
    receipt: ClassificationRun | None = None
    ingest: CandidateIngest | None
    if req.classification_run_id is not None:
        receipt = await lock_run(session, req.classification_run_id)
        if (
            receipt is None
            or receipt.tenant_id != tenant_id
            or receipt.principal_id != principal_id
        ):
            raise HTTPException(status_code=404, detail="classification run not found")
        if (
            receipt.expires_at <= datetime.now(UTC)
            or receipt.content_hash != chash
            or receipt.source_type != req.source_type
            or receipt.workspace_id != workspace_id
        ) and receipt.bound_at is None:
            raise HTTPException(status_code=422, detail="classification run does not match request")

    # The locked receipt is authoritative. Historical pre-migration receipts
    # acquire a newly issued ingest here; a body-supplied id never chooses it.
    if receipt is not None and receipt.ingest_id is None:
        ingest = create_ingest(identity=identity, client_correlation_id=req.correlation_id)
        session.add(ingest)
        receipt.ingest_id = ingest.id
        await session.commit()
        receipt = await lock_run(session, receipt.id)
        if receipt is None:
            raise HTTPException(status_code=404, detail="classification run not found")

    if receipt is not None:
        if req.ingest_id is not None and req.ingest_id != receipt.ingest_id:
            await record_ingest_reuse_rejected(
                tenant_id=tenant_id,
                principal_id=principal_id,
                workspace_id=workspace_id,
                correlation_id=req.correlation_id,
                ingest_id=None,
                mismatches=(),
            )
            raise HTTPException(
                status_code=409, detail="ingest_id does not match classification run"
            )
        if receipt.ingest_id is None:  # pragma: no cover - guarded above
            raise RuntimeError("classification run has no ingest identity")
        ingest = await get_ingest(session, receipt.ingest_id)
    elif req.ingest_id is not None:
        ingest = await get_ingest(session, req.ingest_id)
        if ingest is None:
            await record_ingest_reuse_rejected(
                tenant_id=tenant_id,
                principal_id=principal_id,
                workspace_id=workspace_id,
                correlation_id=req.correlation_id,
                ingest_id=None,
                mismatches=("tenant_mismatch",),
            )
            raise HTTPException(status_code=404, detail="candidate ingest not found")
    else:
        ingest = create_ingest(identity=identity, client_correlation_id=req.correlation_id)
        session.add(ingest)
        # Authoritative identity must be durable before the rest of remember.
        await session.commit()

    if ingest is None:
        raise HTTPException(status_code=404, detail="candidate ingest not found")
    mismatches = identity_mismatches(ingest, identity)
    if mismatches:
        await record_ingest_reuse_rejected(
            tenant_id=tenant_id,
            principal_id=principal_id,
            workspace_id=workspace_id,
            correlation_id=req.correlation_id,
            ingest_id=ingest.id,
            mismatches=mismatches,
        )
        raise HTTPException(status_code=409, detail="candidate ingest does not match request")
    outcome_ctx["ingest_id"] = ingest.id

    await record_candidate_once(
        tenant_id=tenant_id,
        principal_id=principal_id,
        workspace_id=workspace_id,
        correlation_id=correlation_id,
        ingest_id=ingest.id,
        candidate_utf8_bytes=len(req.content.encode("utf-8")),
        source_type=req.source_type,
    )

    if receipt is not None:
        if receipt.bound_at is None and receipt.memory_item_id is not None:
            raise HTTPException(status_code=409, detail="classification run has invalid state")
        if receipt.bound_at is not None:
            if receipt.memory_item_id is None:
                raise HTTPException(status_code=409, detail="classification run is already bound")
            bound_item = await session.scalar(
                select(MemoryItem).where(MemoryItem.id == receipt.memory_item_id)
            )
            if bound_item is None:
                raise HTTPException(status_code=409, detail="classification run is already bound")
            if (
                bound_item.tenant_id != receipt.tenant_id
                or bound_item.principal_id != receipt.principal_id
                or bound_item.workspace_id != receipt.workspace_id
                or bound_item.content_hash != chash
                or bound_item.source_type != req.source_type
                or bound_item.workspace_id != workspace_id
                or bound_item.principal_id != principal_id
                or bound_item.kind != receipt.suggested_kind
                or receipt.content_hash != chash
                or receipt.source_type != req.source_type
                or receipt.workspace_id != workspace_id
                or (req.kind is not None and req.kind != receipt.suggested_kind)
            ):
                raise HTTPException(status_code=409, detail="classification run is already bound")
            outcome_ctx["final_kind"] = bound_item.kind
            outcome_ctx["final_review_status"] = bound_item.review_status
            outcome_ctx["final_visibility"] = bound_item.visibility
            return RememberResponse(
                id=bound_item.id,
                status="deduped",
                review_status=bound_item.review_status,
                memory_confidence=bound_item.memory_confidence,
                deduped_existing_id=bound_item.id,
                correlation_id=correlation_id,
                ingest_id=ingest.id,
                attempt_id=attempt_id,
            )

    kind = req.kind
    wing = req.wing
    room = req.room
    classification_result: ClassificationResult | None = None
    if receipt is not None:
        if kind is not None and kind != receipt.suggested_kind:
            raise HTTPException(status_code=422, detail="kind does not match classification run")
        kind = receipt.suggested_kind
        wing = wing or receipt.suggested_wing
        room = room or receipt.suggested_room
    elif kind is None:
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
    source_trust, memory_confidence, review_status = await resolve_trust_defaults(
        session, tenant_id, req.source_type, principal_type
    )
    authority = derive_memory_authority(source_type=req.source_type, principal_type=principal_type)
    # The kind's governance flag decides whether a write must begin as
    # proposed — regardless of whether the kind came from an explicit request
    # or the classifier (generalizes the old classifier-only
    # suggested_kind == "decision" special case, ENG-AUD-010 / F17).
    if kind_row.requires_review:
        review_status = "proposed"

    # Taxonomy confidence never mutates overall memory confidence. Classification
    # may only narrow visibility; source policy remains the immutable write prior.
    default_confidence = memory_confidence
    final_visibility = caller_visibility
    suggested_visibility: str | None = None
    visibility_narrowed = False
    workspace_available = workspace_id is not None
    if receipt is not None:
        suggested_visibility = receipt.suggested_visibility
        final_visibility = narrow_visibility(
            caller_visibility, suggested_visibility, workspace_available=workspace_available
        )
        visibility_narrowed = final_visibility != caller_visibility
    elif classification_result is not None:
        suggested_visibility = classification_result.suggested_visibility
        final_visibility = narrow_visibility(
            caller_visibility, suggested_visibility, workspace_available=workspace_available
        )
        visibility_narrowed = final_visibility != caller_visibility

    # 5. Supersession check for singleton kinds.
    superseded_id, withheld_singleton = await _check_supersession(
        session,
        tenant_id,
        workspace_id,
        principal_id,
        kind,
        req.subject_type,
        req.subject_id,
        singleton=kind_row.singleton,
        new_authority=authority,
    )
    if withheld_singleton is not None:
        review_status = "proposed"

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
        source_confidence_prior=default_confidence,
        authority=authority,
        importance=req.importance,
        source_type=req.source_type,
        source_session=req.source_session,
        sensitivity=req.sensitivity,
        external_id=req.external_id,
        external_source=req.external_source,
        conflicts_with_item_id=(
            withheld_singleton["id"] if withheld_singleton is not None else None
        ),
        conflict_type="scope_overlap" if withheld_singleton is not None else None,
        conflict_resolution_status="unresolved" if withheld_singleton is not None else None,
    )

    # 7. Attempt flush — catches dedup via unique index idx_memitems_dedup.
    try:
        async with session.begin_nested():
            session.add(item)
            await session.flush()
            if receipt is not None:
                bind_run(receipt, item)
                await session.flush()
    except IntegrityError:
        # Re-query for the existing item to verify this was a dedup, not
        # a CHECK constraint violation or other integrity error.
        dedup_stmt = select(MemoryItem).where(
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
        existing = (await session.execute(dedup_stmt.with_for_update())).scalar_one_or_none()
        if existing is None:
            # Not a dedup — some other constraint rejected the request shape
            # (e.g. a CHECK/enum value or FK reference that slipped past
            # Pydantic). Re-raise so the centralized DB-error handler
            # classifies it by SQLSTATE into the right 4xx/5xx.
            raise
        if receipt is not None:
            if (
                existing.tenant_id != receipt.tenant_id
                or existing.principal_id != receipt.principal_id
                or existing.workspace_id != receipt.workspace_id
                or existing.content_hash != receipt.content_hash
                or existing.source_type != receipt.source_type
                or existing.kind != receipt.suggested_kind
            ):
                raise HTTPException(
                    status_code=409,
                    detail="existing memory is incompatible with classification run",
                ) from None
            competing = await session.scalar(
                select(ClassificationRun).where(ClassificationRun.memory_item_id == existing.id)
            )
            if competing is not None and competing.id != receipt.id:
                raise HTTPException(
                    status_code=409, detail="memory item already has classification evidence"
                ) from None

            previous_visibility = existing.visibility
            receipt_requested_visibility = narrow_visibility(
                caller_visibility,
                receipt.suggested_visibility,
                workspace_available=workspace_available,
            )
            dedup_final_visibility = narrow_visibility(
                previous_visibility,
                receipt_requested_visibility,
                workspace_available=workspace_available,
            )
            if dedup_final_visibility != previous_visibility:
                visibility_guard = (
                    update(MemoryItem)
                    .where(
                        MemoryItem.id == existing.id,
                        MemoryItem.tenant_id == tenant_id,
                        MemoryItem.principal_id == principal_id,
                        MemoryItem.workspace_id == workspace_id,
                        MemoryItem.content_hash == chash,
                        MemoryItem.source_type == receipt.source_type,
                        MemoryItem.kind == receipt.suggested_kind,
                        MemoryItem.visibility == previous_visibility,
                        MemoryItem.valid_to.is_(None),
                        MemoryItem.superseded_by.is_(None),
                        MemoryItem.review_status != "rejected",
                    )
                    .values(visibility=dedup_final_visibility)
                    .returning(MemoryItem.id)
                )
                changed_id = (
                    await session.execute(
                        visibility_guard.execution_options(synchronize_session=False)
                    )
                ).scalar_one_or_none()
                if changed_id is None:
                    raise HTTPException(
                        status_code=409,
                        detail="existing memory changed during receipt binding",
                    ) from None
                session.add(
                    ItemEvent(
                        item_id=existing.id,
                        event_type="metadata_patch",
                        field_name="visibility",
                        old_value=previous_visibility,
                        new_value=dedup_final_visibility,
                        actor_principal_id=principal_id,
                        reason="classification receipt narrowed visibility",
                    )
                )

            bind_run(receipt, existing)
            from engram.promotion import schedule_evidence_promotion_if_qualified

            await schedule_evidence_promotion_if_qualified(session, existing, receipt)
            session.add(
                ItemEvent(
                    item_id=existing.id,
                    event_type="classification",
                    field_name="kind",
                    old_value=None,
                    new_value=json.dumps(
                        {
                            "source": "classification_receipt_dedup",
                            "classification_run_id": str(receipt.id),
                            "classification_version": receipt.classification_version,
                            "retention_policy_version": receipt.retention_policy_version,
                            "source_type": req.source_type,
                            "source_trust": existing.source_trust,
                            "source_confidence_prior": existing.source_confidence_prior,
                            "default_memory_confidence": existing.source_confidence_prior,
                            "final_memory_confidence": existing.memory_confidence,
                            "taxonomy_confidence": receipt.taxonomy_confidence,
                            "confidence": receipt.taxonomy_confidence,
                            "retention_confidence": receipt.retention_confidence,
                            "retention_disposition": receipt.retention_disposition,
                            "requested_visibility": caller_visibility,
                            "suggested_visibility": receipt.suggested_visibility,
                            "previous_visibility": previous_visibility,
                            "final_visibility": dedup_final_visibility,
                            "visibility_narrowed": (dedup_final_visibility != previous_visibility),
                            "reason": receipt.reason,
                            "classification_provenance": receipt.provenance,
                        },
                        sort_keys=True,
                    ),
                    actor_principal_id=principal_id,
                    reason=receipt.reason,
                )
            )
            await session.commit()
        outcome_ctx["final_kind"] = existing.kind
        outcome_ctx["final_review_status"] = existing.review_status
        outcome_ctx["final_visibility"] = existing.visibility
        return RememberResponse(
            id=existing.id,
            status="deduped",
            review_status=existing.review_status,
            memory_confidence=existing.memory_confidence,
            deduped_existing_id=existing.id,
            correlation_id=correlation_id,
            ingest_id=ingest.id,
            attempt_id=attempt_id,
        )

    provider = "caller"
    if receipt is not None:
        provider = str(receipt.provenance.get("provider", "rule"))
    elif classification_result is not None:
        provider = str(classification_result.provenance.get("provider", "rule"))
    provenance_payload: dict[str, Any] = {
        "source": (
            "classification_receipt"
            if receipt is not None
            else "explicit_kind"
            if classification_result is None
            else "auto_classified"
        ),
        "kind": kind,
        "wing": wing,
        "room": room,
        "provider": provider,
        # Trust/visibility audit — present on every classification event so the
        # record is self-describing even for explicit-kind writes (where the
        # classifier did not run and these are the untouched request/defaults).
        "source_type": req.source_type,
        "source_trust": source_trust,
        "source_confidence_prior": default_confidence,
        "authority": int(authority),
        "authority_label": authority_label(authority),
        "default_memory_confidence": default_confidence,
        "final_memory_confidence": memory_confidence,
        "requested_visibility": caller_visibility,
        "suggested_visibility": suggested_visibility,
        "previous_visibility": caller_visibility,
        "final_visibility": final_visibility,
        "visibility_narrowed": visibility_narrowed,
    }
    if receipt is not None:
        provenance_payload.update(
            {
                "classification_run_id": str(receipt.id),
                "classification_version": receipt.classification_version,
                "retention_policy_version": receipt.retention_policy_version,
                "taxonomy_confidence": receipt.taxonomy_confidence,
                "confidence": receipt.taxonomy_confidence,
                "retention_confidence": receipt.retention_confidence,
                "retention_disposition": receipt.retention_disposition,
                "classification_provenance": receipt.provenance,
                "reason": receipt.reason,
            }
        )
    if classification_result is not None:
        classification_dump = classification_result.model_dump(exclude={"provenance"})
        provenance_payload["classification"] = classification_dump
        provenance_payload["classification_provenance"] = classification_result.provenance
        provenance_payload["reason"] = classification_result.reason
    if receipt is not None:
        reason = receipt.reason
    elif classification_result is None:
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
    if withheld_singleton is not None:
        old_authority = int(withheld_singleton["authority"])
        session.add(
            ItemEvent(
                item_id=item.id,
                event_type="conflict_detected",
                field_name="conflicts_with_item_id",
                old_value=None,
                new_value=json.dumps(
                    {
                        "existing_item_id": str(withheld_singleton["id"]),
                        "existing_authority": old_authority,
                        "existing_authority_label": authority_label(old_authority),
                        "new_authority": int(authority),
                        "new_authority_label": authority_label(authority),
                        "reason": "singleton supersession withheld by authority",
                    },
                    sort_keys=True,
                ),
                actor_principal_id=principal_id,
                reason="lower-authority singleton candidate preserved for review",
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

    if receipt is not None:
        from engram.promotion import schedule_evidence_promotion_if_qualified

        await schedule_evidence_promotion_if_qualified(session, item, receipt)

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
                    # ENG-METER-001: thread the candidate correlation id so the
                    # worker-originated embedding provider call is attributed to
                    # the original candidate, and propagated into conflict.check.
                    "correlation_id": str(correlation_id),
                    "ingest_id": str(ingest.id),
                },
                dedupe_key=f"embedding.generate:{item.id}:{profile.id}",
            )

    await session.commit()

    outcome_ctx["final_kind"] = kind
    outcome_ctx["final_review_status"] = review_status
    outcome_ctx["final_visibility"] = final_visibility
    return RememberResponse(
        id=item.id,
        status="superseded" if superseded_id is not None else "created",
        review_status=review_status,
        memory_confidence=memory_confidence,
        superseded_id=superseded_id,
        correlation_id=correlation_id,
        ingest_id=ingest.id,
        attempt_id=attempt_id,
    )


@router.post("/recall", response_model=RecallResponse, dependencies=[Depends(READ_SCOPE)])
async def recall(
    req: RecallRequest,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    memory_context: ResolvedMemoryContext = Depends(resolve_memory_context),  # noqa: B008
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
    tenant_id = memory_context.tenant_id
    principal_id = memory_context.principal_id

    timer = Timer()
    operation = "semantic_recall" if mode == "semantic" else "startup_recall"
    try:
        if mode == "semantic":
            # execute_semantic_recall owns the query-embedding generation flow.
            result = await execute_semantic_recall(
                session=session,
                memory_context=memory_context,
                workspace=req.workspace,
                query=req.query or "",
                byte_budget=req.byte_budget,
                token_budget=req.token_budget,
                item_budget=req.item_budget,
            )
        else:
            result = await execute_startup_recall(
                session=session,
                memory_context=memory_context,
                workspace=req.workspace,
                byte_budget=req.byte_budget,
                token_budget=req.token_budget,
            )
    except Exception:
        # Record the failed retrieval request (previously no row was written on
        # exception, hiding retrieval errors — ENG-METER-001 correction). The
        # embedding stage is NOT known on an opaque failure: the exception may
        # have occurred before, during, or after the embedding call. Record
        # "unknown" rather than asserting an outcome that may be false, and do
        # NOT set embedding_call_occurred=True based solely on the mode.
        await record_retrieval_request(
            tenant_id=tenant_id,
            principal_id=principal_id,
            workspace_id=None,
            operation=operation,
            status="failed",
            latency_ms=timer.elapsed_ms(),
            embedding_call_occurred=None,
            embedding_outcome="unknown" if mode == "semantic" else "not_required",
            memory_context_version=memory_context.version,
            memory_profile_id=memory_context.memory_profile_id,
            memory_profile_revision_id=memory_context.memory_profile_revision_id,
            memory_profile_version=memory_context.memory_profile_version,
        )
        raise

    # On success, the resolved embedding_outcome comes from the recall engine
    # (it owns the embedding call and knows whether it happened). The Boolean
    # embedding_call_occurred is derived from that actual outcome, not the mode.
    resolved_embedding_outcome: EmbeddingOutcome = result.get(
        "embedding_outcome", "not_required" if mode != "semantic" else "unknown"
    )
    await record_retrieval_request(
        tenant_id=tenant_id,
        principal_id=principal_id,
        workspace_id=result.get("workspace_id"),
        operation=operation,
        status="succeeded",
        item_count=result.get("item_count", 0),
        byte_count=result.get("byte_count", 0),
        candidate_count=result.get("candidate_count"),
        latency_ms=timer.elapsed_ms(),
        scoring_version=result.get("scoring_version"),
        config_version=result.get("config_version"),
        embedding_call_occurred=embedding_call_occurred_for(resolved_embedding_outcome),
        embedding_outcome=resolved_embedding_outcome,
        memory_context_version=memory_context.version,
        memory_profile_id=memory_context.memory_profile_id,
        memory_profile_revision_id=memory_context.memory_profile_revision_id,
        memory_profile_version=memory_context.memory_profile_version,
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
    memory_context: ResolvedMemoryContext = Depends(resolve_memory_context),  # noqa: B008
) -> SearchResponse:
    """Keyword, semantic, or hybrid search.

    All three modes resolve the caller's tenant/principal and apply the
    shared read-eligibility predicate (``engram.memory_access``) so a caller
    never sees another tenant's memory, another principal's private memory,
    or workspace memory from a workspace they aren't a member of.
    """
    mode = req.mode
    tenant_id = memory_context.tenant_id
    principal_id = memory_context.principal_id
    timer = Timer()

    # Tracks the actual embedding stage as _search_impl progresses, so the
    # except handler records the truth rather than guessing from the mode.
    # Values: not_attempted | disabled | succeeded | failed.
    embedding_stage: dict[str, EmbeddingOutcome] = {"value": "not_attempted"}

    async def _record_search(
        operation: str,
        results: list[dict[str, Any]],
        *,
        status: str = "succeeded",
        embedding_outcome: EmbeddingOutcome | None = None,
    ) -> None:
        # Default embedding_outcome to the tracked stage when not overridden.
        outcome = embedding_outcome if embedding_outcome is not None else embedding_stage["value"]
        byte_count = sum(len(str(r.get("content", "")).encode("utf-8")) for r in results)
        await record_retrieval_request(
            tenant_id=tenant_id,
            principal_id=principal_id,
            workspace_id=None,
            operation=operation,
            status=status,
            item_count=len(results),
            byte_count=byte_count,
            latency_ms=timer.elapsed_ms(),
            embedding_call_occurred=embedding_call_occurred_for(outcome),
            embedding_outcome=outcome,
            memory_context_version=memory_context.version,
            memory_profile_id=memory_context.memory_profile_id,
            memory_profile_revision_id=memory_context.memory_profile_revision_id,
            memory_profile_version=memory_context.memory_profile_version,
        )

    try:
        return await _search_impl(
            req, session, memory_context, timer, _record_search, embedding_stage
        )
    except Exception:
        # Record the failed search request before re-raising — previously no
        # row was written on exception, hiding search errors (ENG-METER-001).
        # The embedding stage is whatever _search_impl actually reached (tracked
        # in embedding_stage), NOT a guess from the requested mode.
        failed_op = {
            "keyword": "keyword_search",
            "semantic": "semantic_search",
            "hybrid": "hybrid_search",
        }.get(mode, "hybrid_search")
        await _record_search(failed_op, [], status="failed")
        raise


async def _search_impl(
    req: SearchRequest,
    session: AsyncSession,
    memory_context: ResolvedMemoryContext,
    timer: Any,
    _record_search: Any,
    embedding_stage: dict[str, EmbeddingOutcome],
) -> SearchResponse:
    """Keyword/semantic/hybrid search body, split out so the route can wrap it
    in failure-recording telemetry (ENG-METER-001).

    ``embedding_stage`` is mutated in place to reflect the actual embedding
    provider-call state (``not_attempted`` → ``succeeded``/``disabled``/
    ``failed``) so the outer wrapper records the truth on exception, not a
    guess from the requested mode.
    """
    limit = req.limit
    mode = req.mode
    kind = req.kind
    wing = req.wing
    room = req.room
    tenant_id = memory_context.tenant_id
    principal_id = memory_context.principal_id

    if mode == "keyword":
        # Keyword search never calls an embedding provider.
        embedding_stage["value"] = "not_required"
        results = await _keyword_search(
            session,
            req.query,
            limit,
            memory_context=memory_context,
            kind=kind,
            wing=wing,
            room=room,
        )
        await _record_search("keyword_search", results)
        return SearchResponse(results=results, total=len(results))

    import inspect

    from engram.embedding_profiles import get_active_profile

    embedding_profile = await get_active_profile(session)
    semantic_count = (
        0
        if not memory_context.may_read_anything
        else await semantic.candidate_count(
            session,
            memory_context=memory_context,
            kind=kind,
            wing=wing,
            room=room,
            embedding_profile=embedding_profile,
        )
    )
    if semantic_count == 0:
        if mode == "semantic":
            await _record_search("semantic_search", [])
            return SearchResponse(results=[], total=0, message=_SEARCH_HELPFUL_MESSAGE)
        keyword_results = await _keyword_search(
            session,
            req.query,
            max(limit * 5, limit),
            memory_context=memory_context,
            kind=kind,
            wing=wing,
            room=room,
        )
        limited = keyword_results[:limit]
        await _record_search("hybrid_search", limited)
        return SearchResponse(
            results=limited,
            total=min(len(keyword_results), limit),
            message=_SEARCH_HELPFUL_MESSAGE,
        )
    try:
        if len(inspect.signature(generate_embedding).parameters) >= 2:
            query_embedding = await generate_embedding(
                req.query,
                embedding_profile,
                tenant_id=tenant_id,
                principal_id=principal_id,
                operation="embedding_query_search",
                usage_class="request",
            )
        else:
            query_embedding = await generate_embedding(req.query)
    except Exception:
        # The embedding provider call itself failed.
        embedding_stage["value"] = "failed"
        raise
    # An embedding provider WAS attempted; whether it produced a vector
    # (succeeded) or came back disabled/empty (disabled) is the signal.
    embedding_stage["value"] = "succeeded" if query_embedding is not None else "disabled"

    if mode == "semantic":
        if query_embedding is None or semantic_count == 0:
            await _record_search("semantic_search", [])
            return SearchResponse(results=[], total=0, message=_SEARCH_HELPFUL_MESSAGE)
        # Over-fetch so trust-weighted re-ranking can reorder within the window
        # before trimming to the caller's requested limit.
        fetch_limit = min(limit * _SEMANTIC_SEARCH_OVERFETCH, _SEMANTIC_SEARCH_OVERFETCH_CAP)
        raw = await semantic.search(
            session,
            query_embedding,
            fetch_limit,
            memory_context=memory_context,
            kind=kind,
            wing=wing,
            room=room,
            embedding_profile=embedding_profile,
        )
        if not raw:
            await _record_search("semantic_search", [])
            return SearchResponse(results=[], total=0, message=_SEARCH_HELPFUL_MESSAGE)
        results = [_format_semantic_result(row) for row in raw[:limit]]
        await _record_search("semantic_search", results)
        return SearchResponse(results=results, total=len(results))

    keyword_results = await _keyword_search(
        session,
        req.query,
        max(limit * 5, limit),
        memory_context=memory_context,
        kind=kind,
        wing=wing,
        room=room,
    )
    if query_embedding is None or semantic_count == 0:
        limited = keyword_results[:limit]
        await _record_search("hybrid_search", limited)
        return SearchResponse(
            results=limited,
            total=min(len(keyword_results), limit),
            message=_SEARCH_HELPFUL_MESSAGE,
        )

    raw_semantic = await semantic.search(
        session,
        query_embedding,
        max(limit * 5, limit),
        memory_context=memory_context,
        kind=kind,
        wing=wing,
        room=room,
        embedding_profile=embedding_profile,
    )
    semantic_results = [_format_semantic_result(row) for row in raw_semantic]
    results = _rrf_fuse(keyword_results, semantic_results, limit=limit)
    await _record_search("hybrid_search", results)
    return SearchResponse(results=results, total=len(results))


@router.post(
    "/feedback",
    response_model=FeedbackResponse,
    status_code=201,
    responses={
        200: {"model": FeedbackResponse, "description": "Verdict updated or unchanged"},
        429: {"description": "Daily feedback limit exceeded"},
    },
    dependencies=[Depends(WRITE_SCOPE)],
)
async def feedback(
    req: FeedbackRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> FeedbackResponse | JSONResponse:
    """Record or replace the caller's canonical verdict on an eligible item."""
    tenant_id = await _resolve_tenant_id(session)
    principal_id, principal_type = await _resolve_principal(session, tenant_id)

    # Missing and caller-ineligible items deliberately share the same response.
    item = await _require_eligible_item(
        session,
        req.item_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        for_update=True,
    )
    try:
        result: FeedbackResult = await record_feedback(
            session,
            tenant_id=UUID(str(tenant_id)),
            principal_id=UUID(str(principal_id)),
            principal_type=principal_type,
            item=item,
            verdict=req.feedback,
            recall_log_id=req.recall_log_id,
        )
    except RecallLogNotFoundError as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail="Recall log not found") from exc
    except RecallLogItemMismatchError as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail="Recall log did not contain item") from exc
    except FeedbackRateLimitError as exc:
        await session.rollback()
        seconds = max(1, int((exc.reset_at - datetime.now(UTC)).total_seconds()))
        reset_at = exc.reset_at.isoformat().replace("+00:00", "Z")
        return JSONResponse(
            status_code=429,
            content={
                "detail": "Daily feedback limit exceeded",
                "limit": exc.limit,
                "reset_at": reset_at,
            },
            headers={"Retry-After": str(seconds)},
        )
    if result.status != "recorded":
        response.status_code = 200
    return FeedbackResponse(**result.__dict__)


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


async def _fetch_profile_readable_item(
    session: AsyncSession,
    item_id: UUID,
    *,
    memory_context: ResolvedMemoryContext,
) -> dict[str, Any] | None:
    """Fetch a caller-facing item through principal + profile eligibility.

    Returns ``None`` both when the item doesn't exist and when it exists but
    the caller is ineligible to read it — callers should map both cases to a
    404, never a 403, to avoid disclosing item existence.
    """
    read_scope = read_eligibility_sql(memory_context, parameter_prefix="item_detail")
    stmt = text(
        "SELECT * FROM memory_items WHERE (id = :item_id OR id = :item_id_hex) "
        f"AND {read_scope.clause}"
    )
    result = await session.execute(
        stmt,
        {
            "item_id": str(item_id),
            "item_id_hex": item_id.hex,
            **read_scope.params,
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
        text("SELECT 1 FROM principals WHERE (id = :pid OR id = :pid_hex) AND tenant_id = :tid"),
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
    """Resolve a mutation target through pre-002C principal authorization.

    Profile write policy is intentionally not enforced in this slice.
    """
    dialect_name = session.bind.dialect.name if session.bind is not None else None
    lock_clause = " FOR UPDATE" if for_update and dialect_name == "postgresql" else ""
    principal_scope = principal_eligibility_sql(
        principal_id, parameter_prefix="mutation_item"
    )
    stmt = text(
        "SELECT * FROM memory_items WHERE (id = :item_id OR id = :item_id_hex) "
        f"AND tenant_id = :mutation_tenant_id AND {principal_scope.clause}{lock_clause}"
    )
    result = await session.execute(
        stmt,
        {
            "item_id": str(item_id),
            "item_id_hex": item_id.hex,
            "mutation_tenant_id": str(tenant_id),
            **principal_scope.params,
        },
    )
    row = result.mappings().first()
    item = _row_to_dict(row) if row else None
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
    memory_context: ResolvedMemoryContext = Depends(resolve_memory_context),  # noqa: B008
) -> dict[str, Any]:
    """List items with stable cursor pagination, scoped to the caller's
    tenant and read eligibility (``engram.memory_access``)."""
    limit = max(1, min(limit, 100))
    workspace_id, workspace_accessible = await resolve_workspace_scope(
        session, memory_context=memory_context, workspace=workspace
    )
    if not memory_context.may_read_anything or (
        workspace is not None and not workspace_accessible
    ):
        return {"items": [], "count": 0, "next_cursor": None, "cursor": None}

    read_scope = read_eligibility_sql(memory_context, parameter_prefix="item_list")
    clauses: list[str] = [read_scope.clause]
    params: dict[str, Any] = {
        "limit": limit + 1,
        **read_scope.params,
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
    memory_context: ResolvedMemoryContext = Depends(resolve_memory_context),  # noqa: B008
) -> dict[str, Any]:
    """Full detail with provenance and linked KG facts.

    Scoped to the caller's tenant + read eligibility (``engram.memory_access``);
    an ineligible item is indistinguishable from a nonexistent one (404, not
    403) so its existence is never disclosed.
    """
    item = await _fetch_profile_readable_item(
        session, item_id, memory_context=memory_context
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    item["authority_label"] = authority_label(int(item["authority"]))
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
    """Update metadata (wing, room, visibility, importance, pinned) — not content.

    Serialized mutation: the target row is locked via SELECT ... FOR UPDATE
    through the eligibility fetch. Each field change uses a guarded UPDATE
    whose WHERE clause re-checks the old value, so a concurrent writer that
    changed the field between the lock-time read and the UPDATE produces a
    zero-row result and is skipped (no stale event). Events are written only
    after RETURNING confirms the mutation, in the same transaction.
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
        if field == "visibility" and new_value == "workspace" and item.get("workspace_id") is None:
            # ENG-SCOPE-001: a metadata PATCH cannot change workspace_id, so
            # "workspace" is only a valid target when the item already has
            # one. Never silently coerce — fail the same way remember() does.
            raise HTTPException(
                status_code=422,
                detail="visibility='workspace' requires the item to already have a workspace",
            )
        changes.append({"field": field, "old": old_value, "new": new_value})

    events: list[dict[str, Any]] = []
    for change in changes:
        field = change["field"]
        old_val = change["old"]
        new_val = change["new"]

        # Guarded mutation: re-check the old value in the WHERE clause so a
        # concurrent writer that changed this field between the lock-time read
        # and the UPDATE is caught. RETURNING confirms the row was actually
        # mutated before we write the event.
        # Use IS NOT DISTINCT FROM (not =) so NULL old values match correctly
        # (wing/room are nullable; = NULL always returns NULL, not true).
        guard_stmt = text(
            f"UPDATE memory_items SET {field} = :new_value "
            "WHERE (id = :item_id OR id = :item_id_hex) "
            "AND tenant_id = :tenant_id "
            f"AND {field} IS NOT DISTINCT FROM :old_value "
            "RETURNING id"
        )
        guard_result = await session.execute(
            guard_stmt,
            {
                "new_value": new_val,
                "item_id": str(item_id),
                "item_id_hex": item_id.hex,
                "tenant_id": str(tenant_id),
                "old_value": old_val,
            },
        )
        if guard_result.scalar_one_or_none() is None:
            # A concurrent writer changed this field between our read and the
            # UPDATE. Skip this field — no event, no stale mutation.
            continue

        event = await _insert_item_event(
            session,
            item_id=item_id,
            event_type="metadata_patch",
            field_name=field,
            old_value=old_val,
            new_value=new_val,
            actor_principal_id=actor,
            on_behalf_of_principal_id=on_behalf_of,
            reason=req.reason,
        )
        events.append(event)

    updated = await _require_eligible_item(
        session, item_id, tenant_id=tenant_id, principal_id=principal_id
    )
    await session.commit()
    return {"item": updated, "event": events[0] if events else None, "events": events}


@router.post("/items/{item_id}/supersede", response_model=None, dependencies=[Depends(WRITE_SCOPE)])
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
        new_authority=int(item["authority"]),
        old_authority=int(item["authority"]),
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
    author_type = await session.scalar(
        select(Principal.type).where(Principal.id == UUID(str(item["principal_id"])))
    )
    if author_type is None:
        raise HTTPException(status_code=409, detail="Memory author no longer exists")
    _, replacement_prior, _ = await resolve_trust_defaults(
        session, tenant_id, str(item["source_type"]), author_type
    )
    replacement.update(
        {
            "source_confidence_prior": replacement_prior,
            "retention_confidence": None,
            "retention_disposition": None,
            "retention_evidence_at": None,
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
    """Mark invalid (set valid_to).

    Serialized mutation: the target row is locked via SELECT ... FOR UPDATE
    through the eligibility fetch, revalidated for active-live state, and
    mutated with a guarded UPDATE ... RETURNING whose WHERE clause repeats the
    mutation-authoritative predicates (tenant, id, valid_to IS NULL,
    superseded_by IS NULL). The audit event is written only after RETURNING
    confirms the row was actually mutated, in the same transaction. A
    concurrent writer that committed a terminal state (supersession, prior
    invalidation, review rejection/archival) wins; this route returns 409 and
    writes no event.
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

    # Under-lock revalidation: if the item is already invalidated or
    # superseded, it has left the active-live set. A concurrent terminal
    # writer wins; this route is a 409 and writes no event.
    if item.get("valid_to") is not None or item.get("superseded_by") is not None:
        raise HTTPException(
            status_code=409,
            detail="Item is already invalidated or superseded and cannot be invalidated",
        )

    now = _now_dt()
    reason = req.reason if req else None
    old_valid_to = item.get("valid_to")

    # Guarded mutation: the WHERE clause repeats every mutation-authoritative
    # fact so a concurrent change between revalidation and the write is still
    # caught. A zero-row RETURNING means a concurrent writer committed a
    # terminal state between the lock-holding revalidation and the UPDATE —
    # which can only happen if the competing writer was already holding a
    # conflicting lock that our FOR UPDATE waited for. Return 409 with no
    # event.
    guard_stmt = text(
        "UPDATE memory_items SET valid_to = :valid_to "
        "WHERE (id = :item_id OR id = :item_id_hex) "
        "AND tenant_id = :tenant_id "
        "AND valid_to IS NULL "
        "AND superseded_by IS NULL "
        "RETURNING id"
    )
    guard_result = await session.execute(
        guard_stmt,
        {
            "valid_to": now,
            "item_id": str(item_id),
            "item_id_hex": item_id.hex,
            "tenant_id": str(tenant_id),
        },
    )
    if guard_result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=409,
            detail="Item was concurrently modified and cannot be invalidated",
        )

    # Truthful event only after RETURNING confirms the transition, in the same
    # transaction. If the event INSERT fails, the transaction rolls back both
    # the valid_to mutation and the event.
    event = await _insert_item_event(
        session,
        item_id=item_id,
        event_type="invalidate",
        field_name="valid_to",
        old_value=old_valid_to,
        new_value=now,
        actor_principal_id=actor,
        on_behalf_of_principal_id=on_behalf_of,
        reason=reason,
    )
    updated = await _require_eligible_item(
        session, item_id, tenant_id=tenant_id, principal_id=principal_id
    )
    await session.commit()
    return {"item": updated, "event": event}
