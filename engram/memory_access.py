"""Shared memory-item read eligibility.

Every read path that can return memory-item content (recall, search, item
list/detail) must apply the same predicate: the item belongs to the caller's
tenant, and its visibility permits the caller to see it.

Note on RLS: ``memory_items`` (and friends) have ``ENABLE ROW LEVEL
SECURITY`` policies (see migrations/001_init.sql) keyed off
``current_setting('app.tenant_id')``, but the tables are not
``FORCE ROW LEVEL SECURITY``. Policies do not apply to the table owner/role
the application connects as, so tenant scoping cannot be delegated to
Postgres alone — every read path here filters ``tenant_id`` explicitly in
the application layer.

Visibility rules (design.md):
    visibility = 'tenant'
    OR visibility = 'public'
    OR (visibility = 'private' AND principal_id = :caller_principal_id)
    OR (
        visibility = 'workspace'
        AND workspace_id IN (
            SELECT workspace_id FROM workspace_members
            WHERE principal_id = :caller_principal_id
        )
    )
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, text
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from engram.auth import check_workspace_membership
from engram.models import MemoryItem, WorkspaceMember


def eligibility_expression(principal_id: str | UUID) -> ColumnElement[bool]:
    """SQLAlchemy boolean expression: is a ``MemoryItem`` row visible to ``principal_id``?

    Does NOT check ``tenant_id`` — callers must additionally filter
    ``MemoryItem.tenant_id == <caller tenant>`` (see module docstring for why
    RLS cannot be relied on alone).

    A ``visibility='workspace'`` item with ``workspace_id IS NULL`` (the
    default for memories written without an explicit ``workspace``) isn't
    scoped to any workspace, so workspace membership doesn't apply to it —
    it's treated as tenant-wide, matching pre-existing default-write
    behavior. Only a ``workspace_id`` that names a real workspace restricts
    the item to that workspace's members.
    """
    member_workspaces = (
        sa_select(WorkspaceMember.workspace_id)
        .where(WorkspaceMember.principal_id == principal_id)
        .scalar_subquery()
    )
    return or_(
        MemoryItem.visibility == "tenant",
        MemoryItem.visibility == "public",
        and_(MemoryItem.visibility == "private", MemoryItem.principal_id == principal_id),
        and_(
            MemoryItem.visibility == "workspace",
            or_(
                MemoryItem.workspace_id.is_(None),
                MemoryItem.workspace_id.in_(member_workspaces),
            ),
        ),
    )


def apply_read_eligibility(
    stmt: Any, *, tenant_id: str | UUID, principal_id: str | UUID
) -> Any:
    """Apply tenant + visibility eligibility to a ``MemoryItem``-selecting statement."""
    return stmt.where(
        MemoryItem.tenant_id == tenant_id,
        eligibility_expression(principal_id),
    )


_ELIGIBILITY_SQL_TEMPLATE = """(
        {p}visibility = 'tenant'
        OR {p}visibility = 'public'
        OR ({p}visibility = 'private' AND {p}principal_id = :caller_principal_id)
        OR (
            {p}visibility = 'workspace'
            AND (
                {p}workspace_id IS NULL
                OR {p}workspace_id IN (
                    SELECT workspace_id FROM workspace_members
                    WHERE principal_id = :caller_principal_id
                )
            )
        )
    )"""


def eligibility_sql(alias: str = "") -> str:
    """Raw-SQL boolean fragment equivalent to :func:`eligibility_expression`.

    For use inside ``text(...)`` queries over ``memory_items``. Callers must
    bind ``caller_principal_id`` in their execute params, and should also
    include :func:`tenant_sql` (bind ``caller_tenant_id``) — this fragment
    alone does not scope by tenant.
    """
    prefix = f"{alias}." if alias else ""
    return _ELIGIBILITY_SQL_TEMPLATE.format(p=prefix)


def tenant_sql(alias: str = "") -> str:
    """Raw-SQL tenant fragment to pair with :func:`eligibility_sql`."""
    prefix = f"{alias}." if alias else ""
    return f"{prefix}tenant_id = :caller_tenant_id"


async def resolve_workspace_scope(
    session: AsyncSession,
    *,
    tenant_id: str | UUID,
    principal_id: str | UUID,
    workspace: str | None,
) -> tuple[str | None, bool]:
    """Resolve an optional workspace slug/name for a read request.

    Returns ``(workspace_id, accessible)``:

    - ``workspace`` is ``None`` -> ``(None, True)``: no workspace restriction.
    - ``workspace`` doesn't resolve within the caller's tenant, or resolves but
      the caller isn't a member -> ``(None, False)``: callers must treat this
      as zero accessible results rather than silently falling back to an
      unscoped read (an explicit workspace request must not bypass
      membership).
    - ``workspace`` resolves and the caller is a member -> ``(workspace_id, True)``.
    """
    if workspace is None:
        return None, True

    result = await session.execute(
        text(
            "SELECT id FROM workspaces WHERE tenant_id = :tid AND (slug = :ws OR name = :ws)"
        ),
        {"tid": str(tenant_id), "ws": workspace},
    )
    workspace_id = result.scalar_one_or_none()
    if workspace_id is None:
        return None, False

    is_member = await check_workspace_membership(
        session, principal_id=str(principal_id), workspace_id=str(workspace_id)
    )
    if not is_member:
        return None, False

    return str(workspace_id), True
