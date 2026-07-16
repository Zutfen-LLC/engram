"""Domain service for immutable, tenant-scoped memory profiles.

Profiles are deliberately control-plane only in ENG-SCOPE-002A.  This module
does not participate in memory access decisions; later slices intersect a
resolved profile policy with the principal's existing eligibility.
"""
# ruff: noqa: E501

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

ProfileVisibility = Literal["private", "workspace", "tenant", "public"]
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class ProfileValidationError(ValueError):
    """A deterministic request validation error."""


class ProfileNotFoundError(LookupError):
    """Use a non-disclosing 404 for absent and cross-tenant profiles."""


class ProfileConflictError(RuntimeError):
    """A stale revision pointer or disabled profile transition conflict."""


@dataclass(frozen=True)
class WorkspaceGrantInput:
    workspace_id: UUID
    can_read: bool
    can_write: bool


@dataclass(frozen=True)
class ProfilePolicyInput:
    include_private: bool = True
    include_tenant: bool = False
    include_public: bool = False
    allow_tenant_write: bool = False
    allow_public_write: bool = False
    default_write_visibility: ProfileVisibility = "private"
    default_write_workspace_id: UUID | None = None
    workspace_grants: tuple[WorkspaceGrantInput, ...] = ()


@dataclass(frozen=True)
class ResolvedWorkspaceGrant:
    workspace_id: UUID
    workspace_slug: str
    can_read: bool
    can_write: bool


@dataclass(frozen=True)
class ActiveMemoryProfile:
    id: UUID
    slug: str
    revision_id: UUID
    version: int


def validate_slug(slug: str) -> str:
    if not _SLUG_RE.fullmatch(slug):
        raise ProfileValidationError(
            "slug must use lowercase ASCII letters, digits, and single hyphens"
        )
    return slug


async def _resolve_grants(
    session: AsyncSession, tenant_id: UUID, policy: ProfilePolicyInput
) -> list[ResolvedWorkspaceGrant]:
    seen: set[UUID] = set()
    for grant in policy.workspace_grants:
        if grant.workspace_id in seen:
            raise ProfileValidationError("workspace grants must be unique")
        seen.add(grant.workspace_id)
        if grant.can_write and not grant.can_read:
            raise ProfileValidationError("workspace grant can_write requires can_read")
        if not grant.can_read and not grant.can_write:
            raise ProfileValidationError("workspace grant must allow read or write")
    if not seen:
        return []
    rows = (
        await session.execute(
            text(
                "SELECT id, slug FROM workspaces WHERE tenant_id = :tenant_id "
                "AND id = ANY(CAST(:workspace_ids AS uuid[]))"
            ),
            {"tenant_id": str(tenant_id), "workspace_ids": [str(value) for value in seen]},
        )
    ).mappings().all()
    resolved = {UUID(str(row["id"])): str(row["slug"]) for row in rows}
    if len(resolved) != len(seen):
        raise ProfileValidationError("workspace was not found in this tenant")
    return [
        ResolvedWorkspaceGrant(
            workspace_id=grant.workspace_id,
            workspace_slug=resolved[grant.workspace_id],
            can_read=grant.can_read,
            can_write=grant.can_write,
        )
        for grant in policy.workspace_grants
    ]


async def validate_policy(
    session: AsyncSession, tenant_id: UUID, policy: ProfilePolicyInput
) -> list[ResolvedWorkspaceGrant]:
    grants = await _resolve_grants(session, tenant_id, policy)
    visibility = policy.default_write_visibility
    workspace_id = policy.default_write_workspace_id
    if visibility == "private" and workspace_id is not None:
        raise ProfileValidationError("private default write cannot specify a workspace")
    if visibility == "workspace":
        if workspace_id is None:
            raise ProfileValidationError("workspace default write requires a workspace")
        if not any(g.workspace_id == workspace_id and g.can_write for g in grants):
            raise ProfileValidationError("workspace default write requires a writable workspace grant")
    elif workspace_id is not None:
        raise ProfileValidationError("only workspace default write may specify a workspace")
    if visibility == "tenant" and not policy.allow_tenant_write:
        raise ProfileValidationError("tenant default write requires allow_tenant_write")
    if visibility == "public" and not policy.allow_public_write:
        raise ProfileValidationError("public default write requires allow_public_write")
    return grants


def _policy_params(policy: ProfilePolicyInput) -> dict[str, object]:
    return {
        "include_private": policy.include_private,
        "include_tenant": policy.include_tenant,
        "include_public": policy.include_public,
        "allow_tenant_write": policy.allow_tenant_write,
        "allow_public_write": policy.allow_public_write,
        "default_write_visibility": policy.default_write_visibility,
        "default_write_workspace_id": str(policy.default_write_workspace_id)
        if policy.default_write_workspace_id
        else None,
    }


async def _insert_revision(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    profile_id: UUID,
    version: int,
    policy: ProfilePolicyInput,
    grants: list[ResolvedWorkspaceGrant],
    actor_principal_id: UUID,
    reason: str,
) -> UUID:
    revision_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO memory_profile_revisions "
            "(id, tenant_id, profile_id, version, include_private, include_tenant, include_public, "
            "allow_tenant_write, allow_public_write, default_write_visibility, "
            "default_write_workspace_id, created_by_principal_id, reason) "
            "VALUES (:id, :tenant_id, :profile_id, :version, :include_private, :include_tenant, "
            ":include_public, :allow_tenant_write, :allow_public_write, :default_write_visibility, "
            ":default_write_workspace_id, :actor_principal_id, :reason)"
        ),
        {
            "id": str(revision_id), "tenant_id": str(tenant_id), "profile_id": str(profile_id),
            "version": version, "actor_principal_id": str(actor_principal_id), "reason": reason,
            **_policy_params(policy),
        },
    )
    for grant in grants:
        await session.execute(
            text(
                "INSERT INTO memory_profile_workspace_grants "
                "(tenant_id, revision_id, workspace_id, can_read, can_write) "
                "VALUES (:tenant_id, :revision_id, :workspace_id, :can_read, :can_write)"
            ),
            {"tenant_id": str(tenant_id), "revision_id": str(revision_id),
             "workspace_id": str(grant.workspace_id), "can_read": grant.can_read,
             "can_write": grant.can_write},
        )
    return revision_id


async def _event(
    session: AsyncSession, *, tenant_id: UUID, profile_id: UUID, revision_id: UUID | None,
    actor_principal_id: UUID, event_type: str, reason: str, details: str = "{}"
) -> None:
    await session.execute(
        text(
            "INSERT INTO memory_profile_events "
            "(tenant_id, profile_id, revision_id, actor_principal_id, event_type, reason, details) "
            "VALUES (:tenant_id, :profile_id, :revision_id, :actor_principal_id, :event_type, "
            ":reason, CAST(:details AS jsonb))"
        ),
        {"tenant_id": str(tenant_id), "profile_id": str(profile_id),
         "revision_id": str(revision_id) if revision_id else None,
         "actor_principal_id": str(actor_principal_id), "event_type": event_type,
         "reason": reason, "details": details},
    )


async def create_profile(
    session: AsyncSession, *, tenant_id: UUID, actor_principal_id: UUID, name: str, slug: str,
    description: str | None, policy: ProfilePolicyInput, reason: str
) -> UUID:
    validate_slug(slug)
    if not reason.strip():
        raise ProfileValidationError("reason must not be empty")
    grants = await validate_policy(session, tenant_id, policy)
    profile_id = uuid4()
    try:
        await session.execute(
            text(
                "INSERT INTO memory_profiles "
                "(id, tenant_id, name, slug, description, created_by_principal_id) "
                "VALUES (:id, :tenant_id, :name, :slug, :description, :actor_principal_id)"
            ),
            {"id": str(profile_id), "tenant_id": str(tenant_id), "name": name, "slug": slug,
             "description": description, "actor_principal_id": str(actor_principal_id)},
        )
        revision_id = await _insert_revision(
            session, tenant_id=tenant_id, profile_id=profile_id, version=1, policy=policy,
            grants=grants, actor_principal_id=actor_principal_id, reason=reason,
        )
        await session.execute(
            text("UPDATE memory_profiles SET active_revision_id = :revision_id, updated_at = now() "
                 "WHERE id = :profile_id AND tenant_id = :tenant_id"),
            {"revision_id": str(revision_id), "profile_id": str(profile_id), "tenant_id": str(tenant_id)},
        )
        await _event(session, tenant_id=tenant_id, profile_id=profile_id, revision_id=revision_id,
                     actor_principal_id=actor_principal_id, event_type="profile_created", reason=reason)
    except Exception:
        # Caller owns the transaction and will roll it back; retain duplicate-slug context.
        raise
    return profile_id


async def get_profile_row(
    session: AsyncSession, tenant_id: UUID, profile_id: UUID, *, lock: bool = False
) -> RowMapping:
    statement = (
        "SELECT id, tenant_id, name, slug, description, active_revision_id, disabled_at, "
        "created_by_principal_id, created_at, updated_at FROM memory_profiles "
        "WHERE id = :profile_id AND tenant_id = :tenant_id"
    )
    if lock:
        statement += " FOR UPDATE"
    row = (await session.execute(text(statement), {"profile_id": str(profile_id), "tenant_id": str(tenant_id)})).mappings().first()
    if row is None:
        raise ProfileNotFoundError("memory profile not found")
    return row


async def create_revision(
    session: AsyncSession, *, tenant_id: UUID, actor_principal_id: UUID, profile_id: UUID,
    expected_active_revision_id: UUID, policy: ProfilePolicyInput, reason: str
) -> UUID:
    if not reason.strip():
        raise ProfileValidationError("reason must not be empty")
    profile = await get_profile_row(session, tenant_id, profile_id, lock=True)
    if profile["disabled_at"] is not None:
        raise ProfileConflictError("memory profile is disabled")
    current = UUID(str(profile["active_revision_id"])) if profile["active_revision_id"] else None
    if current != expected_active_revision_id:
        raise ProfileConflictError("active revision has changed")
    grants = await validate_policy(session, tenant_id, policy)
    version = int((await session.scalar(text(
        "SELECT COALESCE(MAX(version), 0) + 1 FROM memory_profile_revisions "
        "WHERE tenant_id = :tenant_id AND profile_id = :profile_id"
    ), {"tenant_id": str(tenant_id), "profile_id": str(profile_id)})) or 1)
    revision_id = await _insert_revision(
        session, tenant_id=tenant_id, profile_id=profile_id, version=version, policy=policy,
        grants=grants, actor_principal_id=actor_principal_id, reason=reason,
    )
    await session.execute(text(
        "UPDATE memory_profiles SET active_revision_id = :revision_id, updated_at = now() "
        "WHERE id = :profile_id AND tenant_id = :tenant_id"
    ), {"revision_id": str(revision_id), "profile_id": str(profile_id), "tenant_id": str(tenant_id)})
    details = (
        f'{{"previous_active_revision_id":"{current}","new_active_revision_id":"{revision_id}",'
        f'"version":{version},"workspace_grant_count":{len(grants)},'
        f'"default_write_visibility":"{policy.default_write_visibility}"}}'
    )
    await _event(session, tenant_id=tenant_id, profile_id=profile_id, revision_id=revision_id,
                 actor_principal_id=actor_principal_id, event_type="revision_activated",
                 reason=reason, details=details)
    return revision_id


async def set_enabled(
    session: AsyncSession, *, tenant_id: UUID, actor_principal_id: UUID, profile_id: UUID,
    enabled: bool, reason: str
) -> bool:
    if not reason.strip():
        raise ProfileValidationError("reason must not be empty")
    profile = await get_profile_row(session, tenant_id, profile_id, lock=True)
    currently_enabled = profile["disabled_at"] is None
    if currently_enabled == enabled:
        return False
    if enabled:
        revision = profile["active_revision_id"]
        if revision is None:
            raise ProfileConflictError("memory profile has no active revision")
        valid = await session.scalar(text(
            "SELECT 1 FROM memory_profile_revisions WHERE id = :revision_id "
            "AND profile_id = :profile_id AND tenant_id = :tenant_id"
        ), {"revision_id": str(revision), "profile_id": str(profile_id), "tenant_id": str(tenant_id)})
        if valid is None:
            raise ProfileConflictError("memory profile has an invalid active revision")
    await session.execute(text(
        "UPDATE memory_profiles SET disabled_at = :disabled_at, updated_at = now() "
        "WHERE id = :profile_id AND tenant_id = :tenant_id"
    ), {"disabled_at": None if enabled else datetime.now(UTC), "profile_id": str(profile_id),
        "tenant_id": str(tenant_id)})
    await _event(session, tenant_id=tenant_id, profile_id=profile_id,
                 revision_id=UUID(str(profile["active_revision_id"])) if profile["active_revision_id"] else None,
                 actor_principal_id=actor_principal_id,
                 event_type="profile_enabled" if enabled else "profile_disabled", reason=reason)
    return True


async def resolve_active_profile(
    session: AsyncSession, tenant_id: str, profile_id: str
) -> ActiveMemoryProfile | None:
    """Resolve current profile state on every authenticated profile-bound request."""
    row = (await session.execute(text(
        "SELECT p.id, p.slug, p.active_revision_id, r.version FROM memory_profiles p "
        "JOIN memory_profile_revisions r ON r.id = p.active_revision_id "
        "AND r.profile_id = p.id AND r.tenant_id = p.tenant_id "
        "WHERE p.tenant_id = :tenant_id AND p.id = :profile_id AND p.disabled_at IS NULL"
    ), {"tenant_id": tenant_id, "profile_id": profile_id})).mappings().first()
    if row is None:
        return None
    return ActiveMemoryProfile(UUID(str(row["id"])), str(row["slug"]),
                               UUID(str(row["active_revision_id"])), int(row["version"]))


async def validate_key_binding(
    session: AsyncSession, tenant_id: UUID, profile_id: UUID | None
) -> ActiveMemoryProfile | None:
    if profile_id is None:
        return None
    resolved = await resolve_active_profile(session, str(tenant_id), str(profile_id))
    if resolved is None:
        raise ProfileNotFoundError("memory profile not found")
    return resolved
