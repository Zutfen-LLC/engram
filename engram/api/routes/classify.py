"""Classification endpoint: suggest kind, wing, room for raw text."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from engram.auth import ADMIN_SCOPE, READ_SCOPE
from engram.auth import Principal as AuthPrincipal
from engram.candidate_ingests import CandidateIdentity, create_ingest
from engram.canonicalize import canonicalize, content_hash
from engram.classification import ClassificationResult, RetentionDisposition
from engram.classification import classify as classify_content
from engram.classification_evidence import new_run
from engram.db import get_session
from engram.memory_context import ResolvedMemoryContext, resolve_memory_context
from engram.memory_scope import resolve_write_scope
from engram.models import Principal
from engram.source_types import SourceType
from engram.usage import record_candidate_once

router = APIRouter()


class ClassifyRequest(BaseModel):
    content: str
    context: str | None = None  # optional conversation excerpt or source_type hint
    workspace: str | None = None
    visibility: str | None = None
    source_type: SourceType = "manual"
    # Optional client trace shared with /v1/remember. It does not control
    # candidate uniqueness; the server-issued ingest_id is authoritative.
    correlation_id: UUID | None = None


class ClassifyResponse(BaseModel):
    classification_run_id: UUID
    expires_at: datetime
    # Additive, backward-compatible: the effective correlation id (echoed back
    # if the caller supplied one, otherwise server-generated).
    correlation_id: UUID
    ingest_id: UUID
    suggested_kind: str
    suggested_wing: str | None = None
    suggested_room: str | None = None
    # Advisory only. ``/v1/classify`` returns the suggestion; the actual
    # downward-only narrowing happens on ``/v1/remember``. ``None`` means the
    # classifier has no opinion and the caller's visibility should be preserved.
    suggested_visibility: str | None = None
    taxonomy_confidence: float = Field(ge=0.0, le=0.95)
    confidence: float = Field(ge=0.0, le=0.95)
    retention_confidence: float = Field(ge=0.0, le=0.95)
    retention_disposition: RetentionDisposition
    reason: str
    rules_matched: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def keep_legacy_confidence_alias_equal(self) -> ClassifyResponse:
        self.confidence = self.taxonomy_confidence
        return self


class RuleCreate(BaseModel):
    name: str
    rule_type: str  # keyword_kind | keyword_wing | regex_skip | llm_hint
    pattern: str
    target_kind: str | None = None
    target_wing: str | None = None
    target_room: str | None = None
    priority: int = 100


async def _resolve_tenant_id(session: AsyncSession) -> UUID:
    row = await session.execute(text("SELECT current_setting('app.tenant_id', true)"))
    tenant_id = row.scalar()
    if not tenant_id:
        raise HTTPException(status_code=403, detail="no tenant context")
    return UUID(str(tenant_id))


async def _resolve_principal_id(session: AsyncSession, tenant_id: UUID) -> UUID:
    row = await session.execute(text("SELECT current_setting('app.principal_id', true)"))
    value = row.scalar()
    if not value:
        raise HTTPException(status_code=403, detail="no principal context")
    principal_id = UUID(str(value))
    exists = await session.scalar(
        select(Principal.id).where(
            Principal.id == principal_id, Principal.tenant_id == tenant_id
        )
    )
    if exists is None:
        raise HTTPException(status_code=403, detail="invalid principal context")
    return principal_id


@router.post("/classify", response_model=ClassifyResponse)
async def classify(
    req: ClassifyRequest,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    caller: AuthPrincipal = Depends(READ_SCOPE),  # noqa: B008
    memory_context: ResolvedMemoryContext = Depends(resolve_memory_context),  # noqa: B008
) -> ClassifyResponse:
    """Classify raw text: suggest kind, wing, room, visibility.

    A classification receipt bound to a workspace must already be authorized
    at creation time (ENG-SCOPE-001) — the workspace is resolved and the
    caller's membership verified (or admin scope, tenant-bounded) before any
    candidate_ingest or classification_runs row is created. /v1/remember
    re-checks authorization when the receipt is consumed, so a membership
    revoked after classify() still blocks the later workspace write.
    """

    tenant_id = memory_context.tenant_id
    principal_id = memory_context.principal_id
    scope = await resolve_write_scope(
        session,
        memory_context=memory_context,
        caller_has_admin_scope=caller.has_scope("admin"),
        requested_visibility=req.visibility,
        requested_workspace=req.workspace,
    )
    workspace_id = scope.workspace_id
    correlation_id = req.correlation_id or uuid4()

    identity = CandidateIdentity(
        tenant_id=tenant_id,
        principal_id=principal_id,
        workspace_id=workspace_id,
        source_type=req.source_type,
        content_hash=content_hash(canonicalize(req.content)),
    )
    ingest = create_ingest(
        identity=identity,
        client_correlation_id=req.correlation_id,
        memory_context=memory_context,
    )
    session.add(ingest)
    # The ingest is authoritative business state, not best-effort telemetry.
    # Commit it before provider execution so fallback/failure still has the
    # same durable server-issued identity.
    await session.commit()

    # candidate.observed is idempotent per server-issued ingest. Best-effort: a
    # telemetry failure here must never affect classification.
    await record_candidate_once(
        tenant_id=tenant_id,
        principal_id=principal_id,
        workspace_id=workspace_id,
        correlation_id=correlation_id,
        ingest_id=ingest.id,
        candidate_utf8_bytes=len(req.content.encode("utf-8")),
        source_type=req.source_type,
    )

    result: ClassificationResult = await classify_content(
        req.content,
        tenant_id,
        session,
        context=req.context,
        principal_id=principal_id,
        workspace_id=workspace_id,
        correlation_id=correlation_id,
        ingest_id=ingest.id,
        source_type=req.source_type,
        usage_class="request",
    )
    run = new_run(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content=req.content,
        source_type=req.source_type,
        workspace_id=workspace_id,
        context=req.context,
        result=result,
        ingest_id=ingest.id,
    )
    session.add(run)
    await session.commit()
    return ClassifyResponse(
        classification_run_id=run.id,
        expires_at=run.expires_at,
        correlation_id=correlation_id,
        ingest_id=ingest.id,
        **result.model_dump(exclude={"provenance"}),
    )


@router.get(
    "/classification/rules", response_model=None, dependencies=[Depends(ADMIN_SCOPE)]
)
async def list_rules() -> None:
    """List tenant classification rules."""
    raise NotImplementedError


@router.post(
    "/classification/rules", response_model=None, dependencies=[Depends(ADMIN_SCOPE)]
)
async def create_rule(req: RuleCreate) -> None:
    """Create or update a classification rule."""
    raise NotImplementedError


@router.delete(
    "/classification/rules/{rule_id}", response_model=None, dependencies=[Depends(ADMIN_SCOPE)]
)
async def delete_rule(rule_id: str) -> None:
    """Delete a classification rule."""
    raise NotImplementedError
