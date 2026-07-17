"""Central write-scope resolution for memory-item visibility and workspace.

ENG-SCOPE-001: the foundational memory-scope invariant is that missing or
ambiguous scope must never widen memory access. This module is the single
place that turns a caller's raw ``(visibility, workspace)`` write request into
a truthful, authorized scope — every memory-producing write path
(``/v1/remember``, ``/v1/classify``, and auto-backed ``/v1/kg`` writes) must
resolve through :func:`resolve_write_scope` rather than
reimplementing default/authorization logic locally.

Resolution rules:

* visibility omitted, workspace omitted -> private, no workspace.
* visibility omitted, workspace supplied and authorized -> workspace-shared.
* ``visibility="workspace"`` always requires a real, authorized workspace.
* ``visibility`` explicitly private/tenant/public never requires a workspace,
  but a supplied workspace must still be authorized (it remains an
  organizational scope association for dedup/conflict/provenance purposes).
* an unknown or unauthorized workspace returns the same outward 404 in both
  cases, so workspace existence is never disclosed to a non-member.
* an authenticated key with effective ``admin`` scope may reference any
  workspace inside its own tenant without a workspace-membership row. This is
  evaluated strictly from the caller's granted API-key scopes (never from
  principal name or principal ``type``), and never crosses the tenant
  boundary (the workspace lookup is always scoped to ``tenant_id``).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from engram.auth import check_workspace_membership
from engram.memory_context import ResolvedMemoryContext

VALID_VISIBILITIES: frozenset[str] = frozenset({"private", "workspace", "tenant", "public"})

# Deliberately identical for "workspace does not exist in this tenant" and
# "workspace exists but the caller is not authorized for it" — an unauthorized
# caller must not be able to distinguish the two by response content.
_WORKSPACE_NOT_FOUND_DETAIL = "workspace not found"


@dataclass(frozen=True)
class ResolvedWriteScope:
    """The fully resolved, authorized scope for one memory write.

    Immutable and side-effect-free: :func:`resolve_write_scope` never commits
    or creates application records — callers use this result to build their
    own candidate/receipt/item state.
    """

    visibility: str
    workspace_id: UUID | None
    workspace_slug: str | None
    visibility_was_defaulted: bool
    visibility_from_profile_default: bool
    workspace_from_profile_default: bool
    request_scope_was_explicit: bool


async def authorize_workspace(
    session: AsyncSession,
    *,
    tenant_id: UUID | str,
    principal_id: UUID | str,
    caller_has_admin_scope: bool,
    workspace_slug: str,
) -> UUID:
    """Resolve ``workspace_slug`` within ``tenant_id`` and authorize the caller.

    Raises a non-disclosing 404 for both an unknown workspace and a workspace
    the caller may not use — admin-scoped callers bypass the membership check
    (never the tenant check) per the ENG-SCOPE-001 admin bypass.
    """
    result = await session.execute(
        text("SELECT id FROM workspaces WHERE tenant_id = :tid AND slug = :slug"),
        {"tid": str(tenant_id), "slug": workspace_slug},
    )
    workspace_id = result.scalar_one_or_none()
    if workspace_id is None:
        raise HTTPException(status_code=404, detail=_WORKSPACE_NOT_FOUND_DETAIL)

    if not caller_has_admin_scope:
        is_member = await check_workspace_membership(
            session, principal_id=str(principal_id), workspace_id=str(workspace_id)
        )
        if not is_member:
            raise HTTPException(status_code=404, detail=_WORKSPACE_NOT_FOUND_DETAIL)

    return UUID(str(workspace_id))


async def _authorize_workspace_id(
    session: AsyncSession,
    *,
    memory_context: ResolvedMemoryContext,
    caller_has_admin_scope: bool,
    workspace_id: UUID,
) -> tuple[UUID, str]:
    row = (
        await session.execute(
            text("SELECT id, slug FROM workspaces WHERE tenant_id = :tid AND id = :wid"),
            {"tid": str(memory_context.tenant_id), "wid": str(workspace_id)},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail=_WORKSPACE_NOT_FOUND_DETAIL)
    if not caller_has_admin_scope and not await check_workspace_membership(
        session,
        principal_id=str(memory_context.principal_id),
        workspace_id=str(workspace_id),
    ):
        raise HTTPException(status_code=404, detail=_WORKSPACE_NOT_FOUND_DETAIL)
    return UUID(str(row["id"])), str(row["slug"])


def assert_write_scope_allowed(
    memory_context: ResolvedMemoryContext,
    *,
    visibility: str,
    workspace_id: UUID | None,
) -> None:
    """Revalidate a final/classifier-narrowed scope against profile policy."""
    if visibility not in VALID_VISIBILITIES:
        raise HTTPException(status_code=422, detail=f"invalid visibility: {visibility!r}")
    if visibility == "workspace" and workspace_id is None:
        raise HTTPException(
            status_code=422,
            detail="visibility='workspace' requires an authorized workspace",
        )
    if not memory_context.allows_new_write_scope(visibility, workspace_id):
        if workspace_id is not None and not memory_context.allows_workspace_write(workspace_id):
            raise HTTPException(status_code=404, detail=_WORKSPACE_NOT_FOUND_DETAIL)
        raise HTTPException(status_code=403, detail="memory write scope is not permitted")


async def resolve_write_scope(
    session: AsyncSession,
    *,
    memory_context: ResolvedMemoryContext,
    caller_has_admin_scope: bool,
    requested_visibility: str | None,
    requested_workspace: str | None,
) -> ResolvedWriteScope:
    """Resolve and authorize the effective write scope for one candidate.

    ``requested_visibility`` is the raw, possibly-omitted request field.
    ``requested_workspace`` is the raw workspace slug, or ``None``.

    Raises:
        HTTPException(422): invalid visibility value, or explicit
            ``visibility="workspace"`` with no workspace supplied.
        HTTPException(404): the supplied workspace does not exist in the
            caller's tenant, or the caller is not authorized to use it
            (identical response for both, and for a non-admin caller who is
            not a member).
    """
    if requested_visibility is not None and requested_visibility not in VALID_VISIBILITIES:
        raise HTTPException(
            status_code=422, detail=f"invalid visibility: {requested_visibility!r}"
        )

    workspace_id: UUID | None = None
    workspace_slug: str | None = None
    resolved_visibility: str
    from_profile_default = (
        memory_context.is_profile_bound
        and requested_visibility is None
        and requested_workspace is None
    )
    if from_profile_default:
        resolved_visibility = memory_context.default_write_visibility
        if memory_context.default_write_workspace_id is not None:
            workspace_id, workspace_slug = await _authorize_workspace_id(
                session,
                memory_context=memory_context,
                caller_has_admin_scope=caller_has_admin_scope,
                workspace_id=memory_context.default_write_workspace_id,
            )
        assert_write_scope_allowed(
            memory_context, visibility=resolved_visibility, workspace_id=workspace_id
        )
        return ResolvedWriteScope(
            visibility=resolved_visibility,
            workspace_id=workspace_id,
            workspace_slug=workspace_slug,
            visibility_was_defaulted=True,
            visibility_from_profile_default=True,
            workspace_from_profile_default=workspace_id is not None,
            request_scope_was_explicit=False,
        )

    if requested_workspace is not None:
        workspace_id = await authorize_workspace(
            session,
            tenant_id=memory_context.tenant_id,
            principal_id=memory_context.principal_id,
            caller_has_admin_scope=caller_has_admin_scope,
            workspace_slug=requested_workspace,
        )
        workspace_slug = requested_workspace

    visibility_was_defaulted = requested_visibility is None
    if requested_visibility is None:
        resolved_visibility = "workspace" if workspace_id is not None else "private"
    else:
        resolved_visibility = requested_visibility
        if resolved_visibility == "workspace" and workspace_id is None:
            raise HTTPException(
                status_code=422,
                detail="visibility='workspace' requires an authorized workspace",
            )

    assert_write_scope_allowed(
        memory_context, visibility=resolved_visibility, workspace_id=workspace_id
    )
    return ResolvedWriteScope(
        visibility=resolved_visibility,
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        visibility_was_defaulted=visibility_was_defaulted,
        visibility_from_profile_default=False,
        workspace_from_profile_default=False,
        request_scope_was_explicit=(
            requested_visibility is not None or requested_workspace is not None
        ),
    )
