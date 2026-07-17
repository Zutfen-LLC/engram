"""Request-scoped memory-profile read and write policy.

``ResolvedMemoryContext`` is the immutable data-plane boundary pinned during
authentication and resolved on the primary request session.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID

from fastapi import Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from engram.auth import Principal, get_current_principal
from engram.config import settings
from engram.db import get_session

if TYPE_CHECKING:
    from engram.models import CandidateIngest

MEMORY_CONTEXT_VERSION = "memory-context-v2"
LEGACY_MEMORY_CONTEXT_VERSION = "legacy-unprofiled-v0"
INTERNAL_MEMORY_CONTEXT_VERSION = "internal-system-v1"
WriteVisibility = Literal["private", "workspace", "tenant", "public"]
_VALID_VISIBILITIES = frozenset({"private", "workspace", "tenant", "public"})


@dataclass(frozen=True)
class ResolvedMemoryContext:
    """One caller's pinned memory read/write boundary.

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
    allow_tenant_write: bool
    allow_public_write: bool
    default_write_visibility: WriteVisibility
    default_write_workspace_id: UUID | None
    writable_workspace_ids: frozenset[UUID] | None
    admin_workspace_bypass: bool = False

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

    def allows_workspace_read(self, workspace_id: UUID | str | None) -> bool:
        """Whether profile narrowing admits a workspace association.

        NULL-workspace items are governed only by their visibility category.
        Unprofiled callers retain compatibility behavior.
        """
        if workspace_id is None or self.readable_workspace_ids is None:
            return True
        return UUID(str(workspace_id)) in self.readable_workspace_ids

    def allows_workspace_write(self, workspace_id: UUID | str | None) -> bool:
        """Whether write policy admits a workspace association."""
        if workspace_id is None or self.writable_workspace_ids is None:
            return True
        return UUID(str(workspace_id)) in self.writable_workspace_ids

    def allows_workspace(self, workspace_id: UUID | str | None) -> bool:
        """Compatibility alias for the read boundary."""
        return self.allows_workspace_read(workspace_id)

    def allows_new_write_scope(
        self, visibility: str, workspace_id: UUID | str | None
    ) -> bool:
        """Apply profile write policy to a prospective item scope."""
        if visibility not in _VALID_VISIBILITIES:
            return False
        if not self.allows_workspace_write(workspace_id):
            return False
        if visibility == "private":
            return True
        if visibility == "workspace":
            return workspace_id is not None and self.allows_workspace_write(workspace_id)
        if visibility == "tenant":
            return self.allow_tenant_write
        return self.allow_public_write

    def allows_existing_item_scope(
        self, visibility: str, workspace_id: UUID | str | None
    ) -> bool:
        """Apply the profile read/write intersection to an existing item."""
        if not self.is_profile_bound:
            return True
        if not self.allows_workspace_read(workspace_id):
            return False
        readable = (
            (visibility == "private" and self.include_private)
            or (visibility == "workspace" and workspace_id is not None)
            or (visibility == "tenant" and self.include_tenant)
            or (visibility == "public" and self.include_public)
        )
        return readable and self.allows_new_write_scope(visibility, workspace_id)


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
        allow_tenant_write=True,
        allow_public_write=True,
        default_write_visibility="private",
        default_write_workspace_id=None,
        writable_workspace_ids=None,
        admin_workspace_bypass=principal.has_scope("admin"),
    )


def context_provenance(memory_context: ResolvedMemoryContext) -> dict[str, object]:
    """Relational provenance values for candidate and event inserts."""
    return {
        "tenant_id": memory_context.tenant_id,
        "api_key_id": memory_context.api_key_id,
        "memory_profile_id": memory_context.memory_profile_id,
        "memory_profile_revision_id": memory_context.memory_profile_revision_id,
        "memory_context_version": memory_context.version,
    }


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
    """Resolve one immutable read/write policy on the primary request session.

    Authentication has already pinned the exact active revision on
    ``principal``.  This dependency loads that revision and all readable
    grants in one query, without consulting a read replica or a cross-request
    policy cache. A revision that became inactive after authentication may
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
                "r.include_tenant, r.include_public, r.allow_tenant_write, "
                "r.allow_public_write, r.default_write_visibility, "
                "r.default_write_workspace_id, "
                "COALESCE(array_agg(g.workspace_id) FILTER (WHERE g.can_read), "
                "ARRAY[]::uuid[]) AS readable_workspace_ids, "
                "COALESCE(array_agg(g.workspace_id) FILTER (WHERE g.can_write), "
                "ARRAY[]::uuid[]) AS writable_workspace_ids "
                "FROM memory_profiles p "
                "JOIN memory_profile_revisions r "
                "ON r.tenant_id = p.tenant_id AND r.profile_id = p.id "
                "AND r.id = :revision_id "
                "LEFT JOIN memory_profile_workspace_grants g "
                "ON g.tenant_id = r.tenant_id AND g.revision_id = r.id "
                "WHERE p.tenant_id = :tenant_id AND p.id = :profile_id "
                "GROUP BY p.id, p.slug, p.disabled_at, r.id, r.version, "
                "r.include_private, r.include_tenant, r.include_public, "
                "r.allow_tenant_write, r.allow_public_write, "
                "r.default_write_visibility, r.default_write_workspace_id"
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

    default_visibility = str(row["default_write_visibility"])
    readable_workspace_ids = frozenset(
        UUID(str(value)) for value in row["readable_workspace_ids"]
    )
    writable_workspace_ids = frozenset(
        UUID(str(value)) for value in row["writable_workspace_ids"]
    )
    default_workspace_id = (
        UUID(str(row["default_write_workspace_id"]))
        if row["default_write_workspace_id"] is not None
        else None
    )
    allow_tenant_write = bool(row["allow_tenant_write"])
    allow_public_write = bool(row["allow_public_write"])
    coherent_default = (
        (default_visibility == "private" and default_workspace_id is None)
        or (
            default_visibility == "workspace"
            and default_workspace_id is not None
            and default_workspace_id in writable_workspace_ids
        )
        or (default_visibility == "tenant" and allow_tenant_write)
        or (default_visibility == "public" and allow_public_write)
    )
    if (
        default_visibility not in _VALID_VISIBILITIES
        or not writable_workspace_ids.issubset(readable_workspace_ids)
        or not coherent_default
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
        readable_workspace_ids=readable_workspace_ids,
        allow_tenant_write=allow_tenant_write,
        allow_public_write=allow_public_write,
        default_write_visibility=cast(WriteVisibility, default_visibility),
        default_write_workspace_id=default_workspace_id,
        writable_workspace_ids=writable_workspace_ids,
        admin_workspace_bypass=principal.has_scope("admin"),
    )


async def memory_context_from_ingest(
    session: AsyncSession, ingest: CandidateIngest
) -> ResolvedMemoryContext | None:
    """Recover the exact accepted v2 context; legacy work returns ``None``."""
    if ingest.memory_context_version == LEGACY_MEMORY_CONTEXT_VERSION:
        return None
    if ingest.memory_context_version != MEMORY_CONTEXT_VERSION:
        raise ValueError("unsupported candidate memory context")
    # API-key deletion truthfully nulls the historical key reference.  Without
    # durable evidence of an admin scope, worker reconstruction must not infer
    # the membership bypass from that NULL and accidentally widen cross-item
    # effects.
    admin_workspace_bypass = False
    if ingest.api_key_id is not None:
        scopes = await session.scalar(
            text(
                "SELECT scopes FROM api_keys WHERE tenant_id = :tenant_id "
                "AND id = :api_key_id"
            ),
            {
                "tenant_id": str(ingest.tenant_id),
                "api_key_id": str(ingest.api_key_id),
            },
        )
        admin_workspace_bypass = scopes is not None and "admin" in scopes
    if ingest.memory_profile_id is None:
        if ingest.memory_profile_revision_id is not None:
            raise ValueError("candidate memory profile provenance is incoherent")
        return ResolvedMemoryContext(
            version=MEMORY_CONTEXT_VERSION,
            tenant_id=ingest.tenant_id,
            principal_id=ingest.principal_id,
            api_key_id=ingest.api_key_id,
            memory_profile_id=None,
            memory_profile_revision_id=None,
            memory_profile_slug=None,
            memory_profile_version=None,
            include_private=True,
            include_tenant=True,
            include_public=True,
            readable_workspace_ids=None,
            allow_tenant_write=True,
            allow_public_write=True,
            default_write_visibility="private",
            default_write_workspace_id=None,
            writable_workspace_ids=None,
            admin_workspace_bypass=admin_workspace_bypass,
        )
    if ingest.memory_profile_revision_id is None:
        raise ValueError("candidate memory profile provenance is incoherent")
    row = (
        await session.execute(
            text(
                "SELECT p.slug, r.version, r.include_private, r.include_tenant, "
                "r.include_public, r.allow_tenant_write, r.allow_public_write, "
                "r.default_write_visibility, r.default_write_workspace_id, "
                "COALESCE(array_agg(g.workspace_id) FILTER (WHERE g.can_read), "
                "ARRAY[]::uuid[]) AS readable_workspace_ids, "
                "COALESCE(array_agg(g.workspace_id) FILTER (WHERE g.can_write), "
                "ARRAY[]::uuid[]) AS writable_workspace_ids "
                "FROM memory_profiles p JOIN memory_profile_revisions r "
                "ON r.tenant_id = p.tenant_id AND r.profile_id = p.id "
                "LEFT JOIN memory_profile_workspace_grants g "
                "ON g.tenant_id = r.tenant_id AND g.revision_id = r.id "
                "WHERE p.tenant_id = :tenant_id AND p.id = :profile_id "
                "AND r.id = :revision_id "
                "GROUP BY p.slug, r.version, r.include_private, r.include_tenant, "
                "r.include_public, r.allow_tenant_write, r.allow_public_write, "
                "r.default_write_visibility, r.default_write_workspace_id"
            ),
            {
                "tenant_id": str(ingest.tenant_id),
                "profile_id": str(ingest.memory_profile_id),
                "revision_id": str(ingest.memory_profile_revision_id),
            },
        )
    ).mappings().first()
    if row is None:
        raise ValueError("candidate memory profile revision is unavailable")
    readable = frozenset(UUID(str(value)) for value in row["readable_workspace_ids"])
    writable = frozenset(UUID(str(value)) for value in row["writable_workspace_ids"])
    default_visibility = str(row["default_write_visibility"])
    default_workspace = (
        UUID(str(row["default_write_workspace_id"]))
        if row["default_write_workspace_id"] is not None
        else None
    )
    context = ResolvedMemoryContext(
        version=MEMORY_CONTEXT_VERSION,
        tenant_id=ingest.tenant_id,
        principal_id=ingest.principal_id,
        api_key_id=ingest.api_key_id,
        memory_profile_id=ingest.memory_profile_id,
        memory_profile_revision_id=ingest.memory_profile_revision_id,
        memory_profile_slug=str(row["slug"]),
        memory_profile_version=int(row["version"]),
        include_private=bool(row["include_private"]),
        include_tenant=bool(row["include_tenant"]),
        include_public=bool(row["include_public"]),
        readable_workspace_ids=readable,
        allow_tenant_write=bool(row["allow_tenant_write"]),
        allow_public_write=bool(row["allow_public_write"]),
        default_write_visibility=cast(WriteVisibility, default_visibility),
        default_write_workspace_id=default_workspace,
        writable_workspace_ids=writable,
        admin_workspace_bypass=admin_workspace_bypass,
    )
    coherent_default = (
        (default_visibility == "private" and default_workspace is None)
        or (
            default_visibility == "workspace"
            and default_workspace is not None
            and default_workspace in writable
        )
        or (default_visibility == "tenant" and context.allow_tenant_write)
        or (default_visibility == "public" and context.allow_public_write)
    )
    if (
        default_visibility not in _VALID_VISIBILITIES
        or not writable.issubset(readable)
        or not coherent_default
    ):
        raise ValueError("candidate memory profile revision is incoherent")
    return context
