"""Admin CRUD endpoints for tenant / workspace / principal / api-key management.

All endpoints require the ``admin`` scope when auth is enabled. When auth is
disabled the default principal already carries all scopes, so these endpoints
work in dev mode without a token.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engram.auth import (
    Principal,
    generate_api_key,
    hash_api_key,
    require_scopes,
)
from engram.db import get_session
from engram.models import ApiKey, Tenant, Workspace
from engram.models import Principal as PrincipalModel

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
    api_key = ApiKey(
        tenant_id=body.tenant_id,
        principal_id=body.principal_id,
        key_hash=hash_api_key(plaintext),
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
