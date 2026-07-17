"""Admin control-plane API for revisioned memory profiles (ENG-SCOPE-002A)."""
# ruff: noqa: B008, E501

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from engram.auth import ADMIN_SCOPE, Principal, get_current_principal
from engram.db import get_session
from engram.memory_profiles import (
    ProfileConflictError,
    ProfileNotFoundError,
    ProfilePolicyInput,
    ProfileValidationError,
    WorkspaceGrantInput,
    create_profile,
    create_revision,
    get_profile_row,
    set_enabled,
)

router = APIRouter()


class WorkspaceGrantBody(BaseModel):
    workspace_id: uuid.UUID
    can_read: bool
    can_write: bool


class ProfilePolicyBody(BaseModel):
    include_private: bool = True
    include_tenant: bool = False
    include_public: bool = False
    allow_tenant_write: bool = False
    allow_public_write: bool = False
    default_write_visibility: Literal["private", "workspace", "tenant", "public"] = "private"
    default_write_workspace_id: uuid.UUID | None = None
    workspace_grants: list[WorkspaceGrantBody] = Field(default_factory=list)

    def domain(self) -> ProfilePolicyInput:
        return ProfilePolicyInput(
            include_private=self.include_private, include_tenant=self.include_tenant,
            include_public=self.include_public, allow_tenant_write=self.allow_tenant_write,
            allow_public_write=self.allow_public_write,
            default_write_visibility=self.default_write_visibility,
            default_write_workspace_id=self.default_write_workspace_id,
            workspace_grants=tuple(
                WorkspaceGrantInput(g.workspace_id, g.can_read, g.can_write)
                for g in self.workspace_grants
            ),
        )


class ProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=255)
    description: str | None = None
    policy: ProfilePolicyBody = Field(default_factory=ProfilePolicyBody)
    reason: str = Field(min_length=1, max_length=1000)


class RevisionCreate(BaseModel):
    expected_active_revision_id: uuid.UUID
    policy: ProfilePolicyBody
    reason: str = Field(min_length=1, max_length=1000)


class LifecycleBody(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class WorkspaceGrantOut(BaseModel):
    workspace_id: uuid.UUID
    workspace_slug: str
    can_read: bool
    can_write: bool


class RevisionOut(BaseModel):
    id: uuid.UUID
    version: int
    include_private: bool
    include_tenant: bool
    include_public: bool
    allow_tenant_write: bool
    allow_public_write: bool
    default_write_visibility: str
    default_write_workspace_id: uuid.UUID | None
    created_by_principal_id: uuid.UUID | None
    reason: str
    created_at: datetime
    workspace_grants: list[WorkspaceGrantOut]


class ProfileOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    description: str | None
    enabled: bool
    active_revision_id: uuid.UUID
    active_revision: RevisionOut
    created_at: datetime
    updated_at: datetime


class ProfileSummary(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    description: str | None
    enabled: bool
    active_revision_id: uuid.UUID | None
    active_revision_version: int | None
    created_at: datetime
    updated_at: datetime


def _tenant(caller: Principal) -> uuid.UUID:
    return uuid.UUID(caller.tenant_id)


def _as_http(exc: Exception) -> HTTPException:
    if isinstance(exc, ProfileNotFoundError):
        return HTTPException(status_code=404, detail="memory profile not found")
    if isinstance(exc, ProfileConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ProfileValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=409, detail="memory profile conflict")


async def _revision(session: AsyncSession, tenant_id: uuid.UUID, revision_id: uuid.UUID) -> RevisionOut:
    row = (await session.execute(text(
        "SELECT id, version, include_private, include_tenant, include_public, allow_tenant_write, "
        "allow_public_write, default_write_visibility, default_write_workspace_id, "
        "created_by_principal_id, reason, created_at FROM memory_profile_revisions "
        "WHERE tenant_id = :tenant_id AND id = :revision_id"
    ), {"tenant_id": str(tenant_id), "revision_id": str(revision_id)})).mappings().one()
    grants = (await session.execute(text(
        "SELECT g.workspace_id, w.slug AS workspace_slug, g.can_read, g.can_write "
        "FROM memory_profile_workspace_grants g JOIN workspaces w ON w.id = g.workspace_id "
        "AND w.tenant_id = g.tenant_id WHERE g.tenant_id = :tenant_id "
        "AND g.revision_id = :revision_id ORDER BY w.slug"
    ), {"tenant_id": str(tenant_id), "revision_id": str(revision_id)})).mappings().all()
    return RevisionOut(
        **dict(row), workspace_grants=[WorkspaceGrantOut(**dict(grant)) for grant in grants]
    )


async def _profile_out(session: AsyncSession, tenant_id: uuid.UUID, profile_id: uuid.UUID) -> ProfileOut:
    profile = await get_profile_row(session, tenant_id, profile_id)
    active = profile["active_revision_id"]
    if active is None:
        raise HTTPException(status_code=409, detail="memory profile has no active revision")
    return ProfileOut(
        id=profile["id"], name=profile["name"], slug=profile["slug"], description=profile["description"],
        enabled=profile["disabled_at"] is None, active_revision_id=active,
        active_revision=await _revision(session, tenant_id, active),
        created_at=profile["created_at"], updated_at=profile["updated_at"],
    )


@router.post("/memory-profiles", response_model=ProfileOut, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(ADMIN_SCOPE)])
async def create_memory_profile(
    body: ProfileCreate, session: AsyncSession = Depends(get_session),
    caller: Principal = Depends(get_current_principal),
) -> ProfileOut:
    try:
        profile_id = await create_profile(
            session, tenant_id=_tenant(caller), actor_principal_id=uuid.UUID(caller.principal_id),
            name=body.name, slug=body.slug, description=body.description, policy=body.policy.domain(),
            reason=body.reason,
        )
        await session.commit()
    except (ProfileValidationError, ProfileConflictError, ProfileNotFoundError) as exc:
        await session.rollback()
        raise _as_http(exc) from exc
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="memory profile conflict") from exc
    return await _profile_out(session, _tenant(caller), profile_id)


@router.get("/memory-profiles", response_model=list[ProfileSummary], dependencies=[Depends(ADMIN_SCOPE)])
async def list_memory_profiles(
    include_disabled: bool = False, session: AsyncSession = Depends(get_session),
    caller: Principal = Depends(get_current_principal),
) -> list[ProfileSummary]:
    rows = (await session.execute(text(
        "SELECT p.id, p.name, p.slug, p.description, p.disabled_at, p.active_revision_id, "
        "r.version AS active_revision_version, p.created_at, p.updated_at "
        "FROM memory_profiles p LEFT JOIN memory_profile_revisions r "
        "ON r.id = p.active_revision_id AND r.profile_id = p.id AND r.tenant_id = p.tenant_id "
        "WHERE p.tenant_id = :tenant_id AND (:include_disabled OR p.disabled_at IS NULL) ORDER BY p.slug"
    ), {"tenant_id": str(_tenant(caller)), "include_disabled": include_disabled})).mappings().all()
    return [ProfileSummary(**dict(row), enabled=row["disabled_at"] is None) for row in rows]


@router.get("/memory-profiles/{profile_id}", response_model=ProfileOut, dependencies=[Depends(ADMIN_SCOPE)])
async def get_memory_profile(
    profile_id: uuid.UUID, session: AsyncSession = Depends(get_session),
    caller: Principal = Depends(get_current_principal),
) -> ProfileOut:
    try:
        return await _profile_out(session, _tenant(caller), profile_id)
    except ProfileNotFoundError as exc:
        raise _as_http(exc) from exc


@router.get("/memory-profiles/{profile_id}/revisions", response_model=list[RevisionOut],
            dependencies=[Depends(ADMIN_SCOPE)])
async def list_memory_profile_revisions(
    profile_id: uuid.UUID, session: AsyncSession = Depends(get_session),
    caller: Principal = Depends(get_current_principal),
) -> list[RevisionOut]:
    try:
        await get_profile_row(session, _tenant(caller), profile_id)
    except ProfileNotFoundError as exc:
        raise _as_http(exc) from exc
    ids = (await session.execute(text(
        "SELECT id FROM memory_profile_revisions WHERE tenant_id = :tenant_id "
        "AND profile_id = :profile_id ORDER BY version DESC"
    ), {"tenant_id": str(_tenant(caller)), "profile_id": str(profile_id)})).scalars().all()
    return [await _revision(session, _tenant(caller), revision_id) for revision_id in ids]


@router.post("/memory-profiles/{profile_id}/revisions", response_model=ProfileOut,
             dependencies=[Depends(ADMIN_SCOPE)])
async def activate_memory_profile_revision(
    profile_id: uuid.UUID, body: RevisionCreate, session: AsyncSession = Depends(get_session),
    caller: Principal = Depends(get_current_principal),
) -> ProfileOut:
    try:
        await create_revision(
            session, tenant_id=_tenant(caller), actor_principal_id=uuid.UUID(caller.principal_id),
            profile_id=profile_id, expected_active_revision_id=body.expected_active_revision_id,
            policy=body.policy.domain(), reason=body.reason,
        )
        await session.commit()
    except (ProfileValidationError, ProfileConflictError, ProfileNotFoundError) as exc:
        await session.rollback()
        raise _as_http(exc) from exc
    return await _profile_out(session, _tenant(caller), profile_id)


async def _set_profile_enabled(
    profile_id: uuid.UUID, body: LifecycleBody, enabled: bool, session: AsyncSession, caller: Principal
) -> ProfileOut:
    try:
        await set_enabled(session, tenant_id=_tenant(caller), actor_principal_id=uuid.UUID(caller.principal_id),
                          profile_id=profile_id, enabled=enabled, reason=body.reason)
        await session.commit()
    except (ProfileValidationError, ProfileConflictError, ProfileNotFoundError) as exc:
        await session.rollback()
        raise _as_http(exc) from exc
    return await _profile_out(session, _tenant(caller), profile_id)


@router.post("/memory-profiles/{profile_id}/disable", response_model=ProfileOut,
             dependencies=[Depends(ADMIN_SCOPE)])
async def disable_memory_profile(
    profile_id: uuid.UUID, body: LifecycleBody, session: AsyncSession = Depends(get_session),
    caller: Principal = Depends(get_current_principal),
) -> ProfileOut:
    return await _set_profile_enabled(profile_id, body, False, session, caller)


@router.post("/memory-profiles/{profile_id}/enable", response_model=ProfileOut,
             dependencies=[Depends(ADMIN_SCOPE)])
async def enable_memory_profile(
    profile_id: uuid.UUID, body: LifecycleBody, session: AsyncSession = Depends(get_session),
    caller: Principal = Depends(get_current_principal),
) -> ProfileOut:
    return await _set_profile_enabled(profile_id, body, True, session, caller)
