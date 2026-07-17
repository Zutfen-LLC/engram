"""Canonical principal eligibility and profile read narrowing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import and_, false, literal, or_, select, text, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from engram.auth import check_workspace_membership
from engram.memory_context import ResolvedMemoryContext
from engram.models import MemoryItem, WorkspaceMember

_SQL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class SqlPredicate:
    """A parameterized raw-SQL predicate and its bind values."""

    clause: str
    params: dict[str, object]


def principal_eligibility_expression(
    principal_id: str | UUID,
    *,
    item_entity: Any = MemoryItem,
    workspace_membership_bypass: bool = False,
) -> ColumnElement[bool]:
    """Existing visibility/membership rule, without profile narrowing."""
    member_workspaces = (
        select(WorkspaceMember.workspace_id)
        .where(WorkspaceMember.principal_id == principal_id)
        .scalar_subquery()
    )
    return or_(
        item_entity.visibility == "tenant",
        item_entity.visibility == "public",
        and_(item_entity.visibility == "private", item_entity.principal_id == principal_id),
        and_(
            item_entity.visibility == "workspace",
            or_(
                literal(workspace_membership_bypass),
                item_entity.workspace_id.in_(member_workspaces),
            ),
        ),
    )


def profile_read_scope_expression(
    memory_context: ResolvedMemoryContext,
    *,
    item_entity: Any = MemoryItem,
) -> ColumnElement[bool]:
    """Profile visibility flags plus workspace-association narrowing."""
    workspace_ids = memory_context.readable_workspace_ids
    if workspace_ids is None:
        return true()
    if not memory_context.may_read_anything:
        return false()

    readable_workspace = item_entity.workspace_id.in_(workspace_ids)
    visibility = or_(
        and_(item_entity.visibility == "private", literal(memory_context.include_private)),
        and_(item_entity.visibility == "tenant", literal(memory_context.include_tenant)),
        and_(item_entity.visibility == "public", literal(memory_context.include_public)),
        and_(item_entity.visibility == "workspace", readable_workspace),
    )
    association = or_(item_entity.workspace_id.is_(None), readable_workspace)
    return and_(visibility, association)


def read_eligibility_expression(
    memory_context: ResolvedMemoryContext,
    *,
    item_entity: Any = MemoryItem,
) -> ColumnElement[bool]:
    """Tenant ∩ principal eligibility ∩ profile read scope."""
    return and_(
        item_entity.tenant_id == memory_context.tenant_id,
        principal_eligibility_expression(
            memory_context.principal_id, item_entity=item_entity
        ),
        profile_read_scope_expression(memory_context, item_entity=item_entity),
    )


def profile_write_scope_expression(
    memory_context: ResolvedMemoryContext,
    *,
    item_entity: Any = MemoryItem,
) -> ColumnElement[bool]:
    """Profile write scope for an existing, already-readable item."""
    workspace_ids = memory_context.writable_workspace_ids
    if workspace_ids is None:
        return true()
    writable_workspace = (
        item_entity.workspace_id.in_(workspace_ids) if workspace_ids else false()
    )
    association = or_(item_entity.workspace_id.is_(None), writable_workspace)
    visibility = or_(
        item_entity.visibility == "private",
        and_(item_entity.visibility == "workspace", writable_workspace),
        and_(item_entity.visibility == "tenant", literal(memory_context.allow_tenant_write)),
        and_(item_entity.visibility == "public", literal(memory_context.allow_public_write)),
    )
    return and_(visibility, association)


def write_eligibility_expression(
    memory_context: ResolvedMemoryContext,
    *,
    item_entity: Any = MemoryItem,
) -> ColumnElement[bool]:
    """Tenant ∩ principal ∩ profile read ∩ profile write eligibility."""
    return and_(
        item_entity.tenant_id == memory_context.tenant_id,
        principal_eligibility_expression(
            memory_context.principal_id,
            item_entity=item_entity,
            workspace_membership_bypass=memory_context.admin_workspace_bypass,
        ),
        profile_read_scope_expression(memory_context, item_entity=item_entity),
        profile_write_scope_expression(memory_context, item_entity=item_entity),
    )


def apply_read_eligibility(
    stmt: Any,
    memory_context: ResolvedMemoryContext,
    *,
    item_entity: Any = MemoryItem,
) -> Any:
    return stmt.where(read_eligibility_expression(memory_context, item_entity=item_entity))


def apply_write_eligibility(
    stmt: Any,
    memory_context: ResolvedMemoryContext,
    *,
    item_entity: Any = MemoryItem,
) -> Any:
    return stmt.where(write_eligibility_expression(memory_context, item_entity=item_entity))


def apply_principal_eligibility(
    stmt: Any,
    *,
    tenant_id: str | UUID,
    principal_id: str | UUID,
    item_entity: Any = MemoryItem,
) -> Any:
    """Compatibility helper for non-mutation callers needing principal-only scope."""
    return stmt.where(
        item_entity.tenant_id == tenant_id,
        principal_eligibility_expression(principal_id, item_entity=item_entity),
    )


def _sql_names(alias: str, parameter_prefix: str) -> tuple[str, str]:
    if alias and not _SQL_IDENTIFIER.fullmatch(alias):
        raise ValueError("alias must be a SQL identifier")
    if not _SQL_IDENTIFIER.fullmatch(parameter_prefix):
        raise ValueError("parameter_prefix must be a SQL identifier")
    return (f"{alias}." if alias else "", parameter_prefix)


def principal_eligibility_sql(
    principal_id: str | UUID,
    *,
    alias: str = "",
    parameter_prefix: str = "memory",
    workspace_membership_bypass: bool = False,
) -> SqlPredicate:
    p, key = _sql_names(alias, parameter_prefix)
    principal_param = f"{key}_principal_id"
    membership = (
        f"{p}workspace_id IN (SELECT workspace_id FROM workspace_members "
        f"WHERE principal_id = :{principal_param})"
    )
    params: dict[str, object] = {principal_param: str(principal_id)}
    if workspace_membership_bypass:
        admin_param = f"{key}_admin_bypass"
        membership = f"(:{admin_param} OR {membership})"
        params[admin_param] = True
    return SqlPredicate(
        clause=(
            f"({p}visibility = 'tenant' OR {p}visibility = 'public' "
            f"OR ({p}visibility = 'private' AND {p}principal_id = :{principal_param}) "
            f"OR ({p}visibility = 'workspace' AND {membership}))"
        ),
        params=params,
    )


def profile_read_scope_sql(
    memory_context: ResolvedMemoryContext,
    *,
    alias: str = "",
    parameter_prefix: str = "memory",
) -> SqlPredicate:
    p, key = _sql_names(alias, parameter_prefix)
    workspace_ids = memory_context.readable_workspace_ids
    if workspace_ids is None:
        return SqlPredicate("TRUE", {})
    if not memory_context.may_read_anything:
        return SqlPredicate("FALSE", {})

    params: dict[str, object] = {}
    placeholders: list[str] = []
    for index, workspace_id in enumerate(sorted(workspace_ids, key=str)):
        name = f"{key}_workspace_{index}"
        placeholders.append(f":{name}")
        params[name] = str(workspace_id)
    workspace_clause = (
        f"{p}workspace_id IN ({', '.join(placeholders)})" if placeholders else "FALSE"
    )
    visibility_parts: list[str] = []
    if memory_context.include_private:
        visibility_parts.append(f"{p}visibility = 'private'")
    if memory_context.include_tenant:
        visibility_parts.append(f"{p}visibility = 'tenant'")
    if memory_context.include_public:
        visibility_parts.append(f"{p}visibility = 'public'")
    if placeholders:
        visibility_parts.append(f"({p}visibility = 'workspace' AND {workspace_clause})")
    visibility_clause = " OR ".join(visibility_parts) if visibility_parts else "FALSE"
    return SqlPredicate(
        f"(({visibility_clause}) AND ({p}workspace_id IS NULL OR {workspace_clause}))",
        params,
    )


def read_eligibility_sql(
    memory_context: ResolvedMemoryContext,
    *,
    alias: str = "",
    parameter_prefix: str = "memory",
) -> SqlPredicate:
    p, key = _sql_names(alias, parameter_prefix)
    principal = principal_eligibility_sql(
        memory_context.principal_id, alias=alias, parameter_prefix=parameter_prefix
    )
    profile = profile_read_scope_sql(
        memory_context, alias=alias, parameter_prefix=parameter_prefix
    )
    tenant_param = f"{key}_tenant_id"
    return SqlPredicate(
        f"({p}tenant_id = :{tenant_param} AND {principal.clause} AND {profile.clause})",
        {
            tenant_param: str(memory_context.tenant_id),
            **principal.params,
            **profile.params,
        },
    )


def profile_write_scope_sql(
    memory_context: ResolvedMemoryContext,
    *,
    alias: str = "",
    parameter_prefix: str = "memory_write",
) -> SqlPredicate:
    p, key = _sql_names(alias, parameter_prefix)
    workspace_ids = memory_context.writable_workspace_ids
    if workspace_ids is None:
        return SqlPredicate("TRUE", {})

    params: dict[str, object] = {}
    placeholders: list[str] = []
    for index, workspace_id in enumerate(sorted(workspace_ids, key=str)):
        name = f"{key}_workspace_{index}"
        placeholders.append(f":{name}")
        params[name] = str(workspace_id)
    workspace_clause = (
        f"{p}workspace_id IN ({', '.join(placeholders)})" if placeholders else "FALSE"
    )
    visibility_parts = [f"{p}visibility = 'private'"]
    if placeholders:
        visibility_parts.append(f"({p}visibility = 'workspace' AND {workspace_clause})")
    if memory_context.allow_tenant_write:
        visibility_parts.append(f"{p}visibility = 'tenant'")
    if memory_context.allow_public_write:
        visibility_parts.append(f"{p}visibility = 'public'")
    return SqlPredicate(
        f"(({' OR '.join(visibility_parts)}) "
        f"AND ({p}workspace_id IS NULL OR {workspace_clause}))",
        params,
    )


def write_eligibility_sql(
    memory_context: ResolvedMemoryContext,
    *,
    alias: str = "",
    parameter_prefix: str = "memory_write",
) -> SqlPredicate:
    p, key = _sql_names(alias, parameter_prefix)
    principal = principal_eligibility_sql(
        memory_context.principal_id,
        alias=alias,
        parameter_prefix=f"{parameter_prefix}_principal",
        workspace_membership_bypass=memory_context.admin_workspace_bypass,
    )
    read_scope = profile_read_scope_sql(
        memory_context, alias=alias, parameter_prefix=f"{parameter_prefix}_read"
    )
    write = profile_write_scope_sql(
        memory_context, alias=alias, parameter_prefix=f"{parameter_prefix}_scope"
    )
    return SqlPredicate(
        f"({p}tenant_id = :{key}_tenant_id AND {principal.clause} "
        f"AND {read_scope.clause} AND {write.clause})",
        {
            f"{key}_tenant_id": str(memory_context.tenant_id),
            **principal.params,
            **read_scope.params,
            **write.params,
        },
    )


# Compatibility aliases for external imports and pre-002B tests. Production
# caller-facing reads and mutations use the explicit complete helpers above.
eligibility_expression = principal_eligibility_expression


def eligibility_sql(alias: str = "") -> str:
    return principal_eligibility_sql(
        UUID(int=0), alias=alias, parameter_prefix="caller"
    ).clause


def tenant_sql(alias: str = "") -> str:
    p, _ = _sql_names(alias, "caller")
    return f"{p}tenant_id = :caller_tenant_id"


async def resolve_workspace_scope(
    session: AsyncSession,
    *,
    workspace: str | None,
    memory_context: ResolvedMemoryContext | None = None,
    tenant_id: str | UUID | None = None,
    principal_id: str | UUID | None = None,
) -> tuple[str | None, bool]:
    """Resolve explicit workspace narrowing without disclosure or fallback."""
    if workspace is None:
        return None, True
    if memory_context is not None:
        tenant_id = memory_context.tenant_id
        principal_id = memory_context.principal_id
    if tenant_id is None or principal_id is None:
        raise ValueError("memory_context or tenant_id/principal_id is required")

    workspace_id = (
        await session.execute(
            text(
                "SELECT id FROM workspaces WHERE tenant_id = :tid "
                "AND (slug = :ws OR name = :ws OR CAST(id AS TEXT) = :ws)"
            ),
            {"tid": str(tenant_id), "ws": workspace},
        )
    ).scalar_one_or_none()
    if workspace_id is None:
        return None, False
    if not await check_workspace_membership(
        session, principal_id=str(principal_id), workspace_id=str(workspace_id)
    ):
        return None, False
    if memory_context is not None and not memory_context.allows_workspace(workspace_id):
        return None, False
    return str(workspace_id), True
