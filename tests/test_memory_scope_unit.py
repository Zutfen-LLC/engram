"""Fast, DB-independent coverage for ``engram.memory_scope.resolve_write_scope``.

Uses an in-memory SQLite database with just the two tables the resolver reads
(``workspaces``, ``workspace_members``) — no RLS, no API keys, no live
Postgres required, so these always run (never skip). Real-Postgres coverage
of the full end-to-end wiring (a genuine admin-scoped API key reaching the
resolver through ``/v1/remember``/``/v1/classify``) lives in
``tests/test_scope_write_defaults.py``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from engram.memory_scope import authorize_workspace, resolve_write_scope

pytestmark = pytest.mark.asyncio


@pytest.fixture()
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE workspaces "
            "(id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, slug TEXT NOT NULL)"
        )
        await conn.exec_driver_sql(
            "CREATE TABLE workspace_members "
            "(workspace_id TEXT NOT NULL, principal_id TEXT NOT NULL)"
        )
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _seed_workspace(session: AsyncSession, *, tenant_id: str, slug: str) -> str:
    workspace_id = str(uuid4())
    await session.execute(
        text(
            "INSERT INTO workspaces (id, tenant_id, slug) VALUES (:id, :tid, :slug)"
        ),
        {"id": workspace_id, "tid": tenant_id, "slug": slug},
    )
    await session.commit()
    return workspace_id


async def _add_member(session: AsyncSession, *, workspace_id: str, principal_id: str) -> None:
    await session.execute(
        text(
            "INSERT INTO workspace_members (workspace_id, principal_id) VALUES (:wid, :pid)"
        ),
        {"wid": workspace_id, "pid": principal_id},
    )
    await session.commit()


# ---- Safe defaults ----


async def test_omitted_visibility_and_workspace_resolves_private(session):
    tenant_id, principal_id = str(uuid4()), str(uuid4())
    scope = await resolve_write_scope(
        session,
        tenant_id=tenant_id,
        principal_id=principal_id,
        caller_has_admin_scope=False,
        requested_visibility=None,
        requested_workspace=None,
    )
    assert scope.visibility == "private"
    assert scope.workspace_id is None
    assert scope.visibility_was_defaulted is True


async def test_omitted_visibility_with_authorized_workspace_resolves_workspace(session):
    tenant_id, principal_id = str(uuid4()), str(uuid4())
    workspace_id = await _seed_workspace(session, tenant_id=tenant_id, slug="alpha")
    await _add_member(session, workspace_id=workspace_id, principal_id=principal_id)

    scope = await resolve_write_scope(
        session,
        tenant_id=tenant_id,
        principal_id=principal_id,
        caller_has_admin_scope=False,
        requested_visibility=None,
        requested_workspace="alpha",
    )
    assert scope.visibility == "workspace"
    assert str(scope.workspace_id) == workspace_id
    assert scope.visibility_was_defaulted is True


@pytest.mark.parametrize("visibility", ["private", "tenant", "public"])
async def test_explicit_non_workspace_visibility_needs_no_workspace(session, visibility):
    tenant_id, principal_id = str(uuid4()), str(uuid4())
    scope = await resolve_write_scope(
        session,
        tenant_id=tenant_id,
        principal_id=principal_id,
        caller_has_admin_scope=False,
        requested_visibility=visibility,
        requested_workspace=None,
    )
    assert scope.visibility == visibility
    assert scope.workspace_id is None
    assert scope.visibility_was_defaulted is False


async def test_explicit_private_with_authorized_workspace_stays_private(session):
    """Rule D: a supplied workspace remains an association, audience stays private."""
    tenant_id, principal_id = str(uuid4()), str(uuid4())
    workspace_id = await _seed_workspace(session, tenant_id=tenant_id, slug="alpha")
    await _add_member(session, workspace_id=workspace_id, principal_id=principal_id)

    scope = await resolve_write_scope(
        session,
        tenant_id=tenant_id,
        principal_id=principal_id,
        caller_has_admin_scope=False,
        requested_visibility="private",
        requested_workspace="alpha",
    )
    assert scope.visibility == "private"
    assert str(scope.workspace_id) == workspace_id


async def test_invalid_visibility_is_422(session):
    tenant_id, principal_id = str(uuid4()), str(uuid4())
    with pytest.raises(HTTPException) as exc_info:
        await resolve_write_scope(
            session,
            tenant_id=tenant_id,
            principal_id=principal_id,
            caller_has_admin_scope=False,
            requested_visibility="nonsense",
            requested_workspace=None,
        )
    assert exc_info.value.status_code == 422


async def test_explicit_workspace_visibility_without_workspace_is_422(session):
    tenant_id, principal_id = str(uuid4()), str(uuid4())
    with pytest.raises(HTTPException) as exc_info:
        await resolve_write_scope(
            session,
            tenant_id=tenant_id,
            principal_id=principal_id,
            caller_has_admin_scope=False,
            requested_visibility="workspace",
            requested_workspace=None,
        )
    assert exc_info.value.status_code == 422


# ---- Workspace authorization ----


async def test_unknown_workspace_is_404(session):
    tenant_id, principal_id = str(uuid4()), str(uuid4())
    with pytest.raises(HTTPException) as exc_info:
        await resolve_write_scope(
            session,
            tenant_id=tenant_id,
            principal_id=principal_id,
            caller_has_admin_scope=False,
            requested_visibility=None,
            requested_workspace="does-not-exist",
        )
    assert exc_info.value.status_code == 404


async def test_non_member_gets_same_404_as_unknown_workspace(session):
    tenant_id = str(uuid4())
    owner_id, outsider_id = str(uuid4()), str(uuid4())
    workspace_id = await _seed_workspace(session, tenant_id=tenant_id, slug="alpha")
    await _add_member(session, workspace_id=workspace_id, principal_id=owner_id)

    unknown_exc = None
    member_exc = None
    try:
        await resolve_write_scope(
            session,
            tenant_id=tenant_id,
            principal_id=outsider_id,
            caller_has_admin_scope=False,
            requested_visibility=None,
            requested_workspace="does-not-exist",
        )
    except HTTPException as exc:
        unknown_exc = exc
    try:
        await resolve_write_scope(
            session,
            tenant_id=tenant_id,
            principal_id=outsider_id,
            caller_has_admin_scope=False,
            requested_visibility=None,
            requested_workspace="alpha",
        )
    except HTTPException as exc:
        member_exc = exc

    assert unknown_exc is not None and member_exc is not None
    assert unknown_exc.status_code == member_exc.status_code == 404
    assert unknown_exc.detail == member_exc.detail


async def test_admin_scope_bypasses_membership_same_tenant(session):
    tenant_id = str(uuid4())
    admin_id = str(uuid4())
    workspace_id = await _seed_workspace(session, tenant_id=tenant_id, slug="alpha")
    # No membership row for admin_id.

    scope = await resolve_write_scope(
        session,
        tenant_id=tenant_id,
        principal_id=admin_id,
        caller_has_admin_scope=True,
        requested_visibility=None,
        requested_workspace="alpha",
    )
    assert str(scope.workspace_id) == workspace_id
    assert scope.visibility == "workspace"


async def test_admin_scope_cannot_cross_tenant(session):
    tenant_a, tenant_b = str(uuid4()), str(uuid4())
    admin_id = str(uuid4())
    await _seed_workspace(session, tenant_id=tenant_b, slug="alpha")

    with pytest.raises(HTTPException) as exc_info:
        await resolve_write_scope(
            session,
            tenant_id=tenant_a,
            principal_id=admin_id,
            caller_has_admin_scope=True,
            requested_visibility=None,
            requested_workspace="alpha",
        )
    assert exc_info.value.status_code == 404


async def test_direct_authorize_workspace_helper_matches_resolver(session):
    """/v1/classify uses authorize_workspace() directly (no visibility to resolve)."""
    tenant_id, principal_id = str(uuid4()), str(uuid4())
    workspace_id = await _seed_workspace(session, tenant_id=tenant_id, slug="alpha")
    await _add_member(session, workspace_id=workspace_id, principal_id=principal_id)

    resolved = await authorize_workspace(
        session,
        tenant_id=tenant_id,
        principal_id=principal_id,
        caller_has_admin_scope=False,
        workspace_slug="alpha",
    )
    assert str(resolved) == workspace_id
