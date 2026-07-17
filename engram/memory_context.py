"""Request-scoped memory-profile read policy.

``ResolvedMemoryContext`` is the immutable data-plane boundary pinned during
authentication and resolved on the primary request session.  It deliberately
does not contain any write-policy fields: profile-enforced writes are
ENG-SCOPE-002C work.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from uuid import UUID

from fastapi import Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from engram.auth import Principal, get_current_principal
from engram.config import settings
from engram.db import get_session

MEMORY_CONTEXT_VERSION = "memory-context-v1"


@dataclass(frozen=True)
class ResolvedMemoryContext:
    """One caller's pinned memory read boundary.

    ``readable_workspace_ids=None`` is the compatibility/unprofiled state.
    An empty frozenset is a bound profile with no readable workspace grants.
    """

    version: str
    tenant_id: UUID
    principal_id: UUID
    api_key_id: UUID | None
    memory_profile_id: UUID | None
    memory_profile_revision_id: UUID | None
    memory_profile_slug: str | None
    memory_profile_version: int | None
    include_private: bool
    include_tenant: bool
    include_public: bool
    readable_workspace_ids: frozenset[UUID] | None

    @property
    def is_profile_bound(self) -> bool:
        return self.memory_profile_id is not None

    @property
    def may_read_anything(self) -> bool:
        if not self.is_profile_bound:
            return True
        return bool(
            self.include_private
            or self.include_tenant
            or self.include_public
            or self.readable_workspace_ids
        )

    def allows_workspace(self, workspace_id: UUID | str | None) -> bool:
        """Whether profile narrowing admits a workspace association.

        NULL-workspace items are governed only by their visibility category.
        Unprofiled callers retain compatibility behavior.
        """
        if workspace_id is None or self.readable_workspace_ids is None:
            return True
        return UUID(str(workspace_id)) in self.readable_workspace_ids


def unrestricted_memory_context(principal: Principal) -> ResolvedMemoryContext:
    """Build the compatibility context for an unprofiled/auth-disabled caller."""
    return ResolvedMemoryContext(
        version=MEMORY_CONTEXT_VERSION,
        tenant_id=UUID(principal.tenant_id),
        principal_id=UUID(principal.principal_id),
        api_key_id=UUID(principal.api_key_id) if principal.api_key_id else None,
        memory_profile_id=None,
        memory_profile_revision_id=None,
        memory_profile_slug=None,
        memory_profile_version=None,
        include_private=True,
        include_tenant=True,
        include_public=True,
        readable_workspace_ids=None,
    )


def _invalid_key() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or revoked API key",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _request_identity(
    session: AsyncSession, principal: Principal
) -> tuple[UUID, UUID]:
    """Return the request-session identity, preserving test/dev overrides.

    Production authentication and ``get_session`` always agree.  Auth-disabled
    development and test dependency overrides may deliberately install a
    different RLS identity, so use the session's pinned GUCs on PostgreSQL in
    that compatibility mode.
    """
    if settings.auth_enabled or session.bind is None or session.bind.dialect.name != "postgresql":
        return UUID(principal.tenant_id), UUID(principal.principal_id)
    row = (
        await session.execute(
            text(
                "SELECT current_setting('app.tenant_id', true) AS tenant_id, "
                "current_setting('app.principal_id', true) AS principal_id"
            )
        )
    ).mappings().one()
    return UUID(str(row["tenant_id"])), UUID(str(row["principal_id"]))


async def resolve_memory_context(
    session: AsyncSession = Depends(get_session),  # noqa: B008
    principal: Principal = Depends(get_current_principal),  # noqa: B008
) -> ResolvedMemoryContext:
    """Resolve one immutable read policy on the primary request session.

    Authentication has already pinned the exact active revision on
    ``principal``.  This dependency loads that revision and all readable
    grants in one query, without consulting a read replica or a cross-request
    policy cache.  A revision that became inactive after authentication may
    finish the in-flight request; tenant/profile/revision incoherence and a
    disabled profile fail closed as the same generic 401 used for revoked
    keys.
    """
    tenant_id, principal_id = await _request_identity(session, principal)
    if principal.memory_profile_id is None:
        context = unrestricted_memory_context(principal)
        if context.tenant_id == tenant_id and context.principal_id == principal_id:
            return context
        return replace(context, tenant_id=tenant_id, principal_id=principal_id)

    if (
        principal.memory_profile_revision_id is None
        or principal.memory_profile_slug is None
        or principal.memory_profile_version is None
        or tenant_id != UUID(principal.tenant_id)
        or principal_id != UUID(principal.principal_id)
    ):
        raise _invalid_key()

    row = (
        await session.execute(
            text(
                "SELECT p.id AS profile_id, p.slug, p.disabled_at, "
                "r.id AS revision_id, r.version, r.include_private, "
                "r.include_tenant, r.include_public, "
                "COALESCE(array_agg(g.workspace_id) FILTER (WHERE g.can_read), "
                "ARRAY[]::uuid[]) AS readable_workspace_ids "
                "FROM memory_profiles p "
                "JOIN memory_profile_revisions r "
                "ON r.tenant_id = p.tenant_id AND r.profile_id = p.id "
                "AND r.id = :revision_id "
                "LEFT JOIN memory_profile_workspace_grants g "
                "ON g.tenant_id = r.tenant_id AND g.revision_id = r.id "
                "WHERE p.tenant_id = :tenant_id AND p.id = :profile_id "
                "GROUP BY p.id, p.slug, p.disabled_at, r.id, r.version, "
                "r.include_private, r.include_tenant, r.include_public"
            ),
            {
                "tenant_id": str(tenant_id),
                "profile_id": principal.memory_profile_id,
                "revision_id": principal.memory_profile_revision_id,
            },
        )
    ).mappings().first()
    if (
        row is None
        or row["disabled_at"] is not None
        or str(row["profile_id"]) != principal.memory_profile_id
        or str(row["revision_id"]) != principal.memory_profile_revision_id
        or str(row["slug"]) != principal.memory_profile_slug
        or int(row["version"]) != principal.memory_profile_version
    ):
        raise _invalid_key()

    return ResolvedMemoryContext(
        version=MEMORY_CONTEXT_VERSION,
        tenant_id=tenant_id,
        principal_id=principal_id,
        api_key_id=UUID(principal.api_key_id) if principal.api_key_id else None,
        memory_profile_id=UUID(str(row["profile_id"])),
        memory_profile_revision_id=UUID(str(row["revision_id"])),
        memory_profile_slug=str(row["slug"]),
        memory_profile_version=int(row["version"]),
        include_private=bool(row["include_private"]),
        include_tenant=bool(row["include_tenant"]),
        include_public=bool(row["include_public"]),
        readable_workspace_ids=frozenset(
            UUID(str(value)) for value in row["readable_workspace_ids"]
        ),
    )
