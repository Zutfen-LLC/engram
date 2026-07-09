"""Admin CRUD endpoints for tenant / workspace / principal / api-key management.

All endpoints require the ``admin`` scope when auth is enabled. When auth is
disabled the default principal already carries all scopes, so these endpoints
work in dev mode without a token.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from engram.auth import (
    DIGEST_ALGORITHM,
    Principal,
    digest_api_key_secret,
    generate_api_key,
    parse_api_key,
    require_scopes,
)
from engram.db import get_session
from engram.models import ApiKey, Tenant, Workspace
from engram.models import Principal as PrincipalModel
from engram.promotion import auto_promote_proposed_memories, summarize

router = APIRouter()


# --- Request / response schemas ----------------------------------------------


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=255)


class TenantOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str


class WorkspaceCreate(BaseModel):
    tenant_id: uuid.UUID
    name: str = Field(min_length=1)
    slug: str = Field(min_length=1, max_length=255)


class WorkspaceOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    slug: str


class PrincipalCreate(BaseModel):
    tenant_id: uuid.UUID
    name: str = Field(min_length=1)
    type: str = Field(default="agent", max_length=50)


class PrincipalOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    type: str


class ApiKeyCreate(BaseModel):
    tenant_id: uuid.UUID
    principal_id: uuid.UUID | None = None
    scopes: list[str] = Field(default=["read", "write"])
    label: str | None = None


class ApiKeyOut(BaseModel):
    """Returned only once at creation — includes the plaintext key."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    principal_id: uuid.UUID | None
    scopes: list[str]
    label: str | None
    key: str  # plaintext, shown once


class PromotionResponse(BaseModel):
    """Result of running auto-promotion Path A for the caller's tenant."""

    tenant_id: str
    enabled: bool
    confidence_threshold: float
    min_age_hours: int
    scanned: int = 0
    promoted: int = 0
    skipped_confidence: int = 0
    skipped_age: int = 0
    skipped_conflict: int = 0
    skipped_disabled: int = 0
    promoted_ids: list[uuid.UUID] = Field(default_factory=list)
    summary: str


# --- Endpoints ---------------------------------------------------------------


@router.post(
    "/admin/tenants",
    response_model=TenantOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scopes("admin"))],  # noqa: B008
)
async def create_tenant(
    body: TenantCreate,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> TenantOut:
    tenant = Tenant(name=body.name, slug=body.slug, created_at=datetime.now(UTC))
    session.add(tenant)
    await session.commit()
    await session.refresh(tenant)
    return TenantOut(id=tenant.id, name=tenant.name, slug=tenant.slug)


@router.post(
    "/admin/workspaces",
    response_model=WorkspaceOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scopes("admin"))],  # noqa: B008
)
async def create_workspace(
    body: WorkspaceCreate,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> WorkspaceOut:
    ws = Workspace(
        tenant_id=body.tenant_id, name=body.name, slug=body.slug,
        created_at=datetime.now(UTC),
    )
    session.add(ws)
    await session.commit()
    await session.refresh(ws)
    return WorkspaceOut(
        id=ws.id, tenant_id=ws.tenant_id, name=ws.name, slug=ws.slug
    )


@router.post(
    "/admin/principals",
    response_model=PrincipalOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scopes("admin"))],  # noqa: B008
)
async def create_principal(
    body: PrincipalCreate,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> PrincipalOut:
    principal = PrincipalModel(
        tenant_id=body.tenant_id, name=body.name, type=body.type,
        created_at=datetime.now(UTC),
    )
    session.add(principal)
    await session.commit()
    await session.refresh(principal)
    return PrincipalOut(
        id=principal.id,
        tenant_id=principal.tenant_id,
        name=principal.name,
        type=principal.type,
    )


@router.post(
    "/admin/api-keys",
    response_model=ApiKeyOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scopes("admin"))],  # noqa: B008
)
async def create_api_key(
    body: ApiKeyCreate,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ApiKeyOut:
    plaintext = generate_api_key()
    parsed = parse_api_key(plaintext)
    assert parsed.key_id is not None  # new-format keys always carry a key_id
    api_key = ApiKey(
        tenant_id=body.tenant_id,
        principal_id=body.principal_id,
        key_hash=None,
        key_id=parsed.key_id,
        secret_digest=digest_api_key_secret(parsed.secret),
        digest_algorithm=DIGEST_ALGORITHM,
        scopes=body.scopes,
        label=body.label,
        created_at=datetime.now(UTC),
    )
    session.add(api_key)
    await session.commit()
    await session.refresh(api_key)
    return ApiKeyOut(
        id=api_key.id,
        tenant_id=api_key.tenant_id,
        principal_id=api_key.principal_id,
        scopes=list(api_key.scopes),
        label=api_key.label,
        key=plaintext,
    )


# A read-only helper used by tests and tooling to list a tenant's principals.
@router.get(
    "/admin/principals",
    response_model=list[PrincipalOut],
    dependencies=[Depends(require_scopes("admin"))],  # noqa: B008
)
async def list_principals(
    tenant_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    _pr: Principal = Depends(require_scopes("admin")),  # noqa: B008
) -> list[PrincipalOut]:
    result = await session.execute(
        select(PrincipalModel).where(PrincipalModel.tenant_id == tenant_id)
    )
    return [
        PrincipalOut(
            id=p.id, tenant_id=p.tenant_id, name=p.name, type=p.type
        )
        for p in result.scalars()
    ]


async def _resolve_tenant_id(session: AsyncSession) -> str:
    """Read tenant_id from RLS session context (mirrors review.py)."""
    tid_str = (
        await session.execute(text("SELECT current_setting('app.tenant_id', true)"))
    ).scalar()
    if not tid_str:
        # With RLS configured every request sets this; reaching here means the
        # session was constructed without the dependency.
        raise HTTPException(status_code=403, detail="no tenant context")
    return str(tid_str)


@router.post(
    "/admin/promote",
    response_model=PromotionResponse,
    dependencies=[Depends(require_scopes("admin"))],  # noqa: B008
)
async def promote_proposed(
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> PromotionResponse:
    """Run auto-promotion Path A for the caller's tenant.

    Promotes ``proposed`` items whose ``memory_confidence`` meets the tenant
    threshold, whose age meets ``auto_promote_min_age_hours``, and which have
    no unresolved conflict. Each promotion writes an ``item_events`` audit row.
    Idempotent — safe to call repeatedly. Returns per-reason skip counts.
    """
    tenant_id = await _resolve_tenant_id(session)
    result = await auto_promote_proposed_memories(session, tenant_id)
    return PromotionResponse(
        tenant_id=result.tenant_id,
        enabled=result.enabled,
        confidence_threshold=result.confidence_threshold,
        min_age_hours=result.min_age_hours,
        scanned=result.scanned,
        promoted=result.promoted,
        skipped_confidence=result.skipped_confidence,
        skipped_age=result.skipped_age,
        skipped_conflict=result.skipped_conflict,
        skipped_disabled=result.skipped_disabled,
        promoted_ids=result.promoted_ids,
        summary=summarize(result),
    )
