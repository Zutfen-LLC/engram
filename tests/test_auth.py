# ruff: noqa: E501
"""Tests for API key auth, scope enforcement, workspace membership, and admin endpoints.

Uses an in-memory SQLite with manually-created tables (the same pattern as
test_items.py) so the full dependency chain — including get_session RLS context
and get_current_principal — runs without a live Postgres.

The auth module resolves the caller via a lazy ``_get_session_factory()``
accessor. Tests monkeypatch that accessor so ``get_current_principal`` reads
from the test SQLite DB, not the module-level Postgres engine.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from engram.api.app import create_app
from engram.auth import (
    check_workspace_membership,
    generate_api_key,
    hash_api_key,
    verify_api_key,
)
from engram.db import get_session

CREATE_STATEMENTS = [
    """
    CREATE TABLE tenants (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        slug TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE workspaces (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        name TEXT NOT NULL,
        slug TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE principals (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        name TEXT NOT NULL,
        type TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE workspace_members (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        principal_id TEXT NOT NULL,
        role TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE api_keys (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        principal_id TEXT,
        key_hash TEXT NOT NULL,
        scopes TEXT NOT NULL,
        label TEXT,
        created_at TEXT NOT NULL,
        revoked_at TEXT
    )
    """,
]


def _scopes_to_sql(scopes: list[str]) -> str:
    return ",".join(scopes)


@pytest.fixture()
async def session_factory(tmp_path: Path):
    db_path = tmp_path / "engram_auth.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        for stmt in CREATE_STATEMENTS:
            await conn.exec_driver_sql(stmt)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield factory
    await engine.dispose()


@pytest.fixture()
async def seeded(session_factory):
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO tenants (id, name, slug, created_at) "
                "VALUES ('00000000-0000-0000-0000-000000000001', 'Default', 'default', '2026-01-01')"
            )
        )
        await session.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type, created_at) "
                "VALUES ('00000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000001', 'admin', 'admin', '2026-01-01')"
            )
        )
        await session.commit()
    return session_factory


@pytest.fixture()
def patch_auth_factory(seeded, monkeypatch):
    """Point auth._get_session_factory at the test DB so the dependency
    chain resolves principals from SQLite, not the module Postgres engine."""
    import engram.auth as auth_mod

    monkeypatch.setattr(auth_mod, "_get_session_factory", lambda: seeded)
    return seeded


@pytest.fixture()
def make_client(patch_auth_factory):
    """Factory: builds a client with a given auth_enabled setting."""

    def _build(*, auth_enabled: bool) -> AsyncClient:
        from engram.config import settings as _settings

        _settings.auth_enabled = auth_enabled
        app = create_app()

        async def override_get_session() -> AsyncIterator[AsyncSession]:
            async with patch_auth_factory() as session:
                yield session

        app.dependency_overrides[get_session] = override_get_session
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")

    return _build


async def _seed_api_key(
    factory, *, scopes: list[str], tenant_id: str = "00000000-0000-0000-0000-000000000001"
) -> str:
    plaintext = generate_api_key()
    key_hash = hash_api_key(plaintext)
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO api_keys (id, tenant_id, principal_id, key_hash, scopes, label, created_at, revoked_at) "
                "VALUES (:id, :tid, :pid, :kh, :sc, :lbl, '2026-01-01', NULL)"
            ),
            {
                "id": f"k-{scopes[0]}",
                "tid": tenant_id,
                "pid": "p-admin",
                "kh": key_hash,
                "sc": _scopes_to_sql(scopes),
                "lbl": "test",
            },
        )
        await session.commit()
    return plaintext


# === Unit tests for key gen/hash/verify ===


def test_generate_api_key_format():
    key = generate_api_key()
    assert key.startswith("eng_")
    assert len(key) > 20


def test_hash_and_verify_api_key_roundtrip():
    key = generate_api_key()
    h = hash_api_key(key)
    assert verify_api_key(key, h) is True
    assert verify_api_key("eng_wrong", h) is False


def test_verify_api_key_garbage_hash():
    assert verify_api_key("eng_x", "not-a-real-hash") is False


# === Health exempt + auth-disabled flow ===


async def test_health_exempt_no_auth(make_client):
    c = make_client(auth_enabled=True)
    try:
        resp = await c.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
    finally:
        await c.aclose()


async def test_health_works_with_auth_enabled(make_client):
    c = make_client(auth_enabled=True)
    try:
        resp = await c.get("/health")
        assert resp.status_code == 200
    finally:
        await c.aclose()


async def test_admin_works_when_auth_disabled(make_client):
    c = make_client(auth_enabled=False)
    try:
        resp = await c.post(
            "/v1/admin/tenants", json={"name": "Acme", "slug": "acme"}
        )
        assert resp.status_code == 201
        assert resp.json()["slug"] == "acme"
    finally:
        await c.aclose()


async def test_create_principal_when_auth_disabled(make_client):
    c = make_client(auth_enabled=False)
    try:
        resp = await c.post(
            "/v1/admin/principals",
            json={"tenant_id": "00000000-0000-0000-0000-000000000001", "name": "bot-1", "type": "agent"},
        )
        assert resp.status_code == 201
        assert resp.json()["name"] == "bot-1"
    finally:
        await c.aclose()


# === Auth enabled ===


async def test_missing_token_returns_401(make_client):
    c = make_client(auth_enabled=True)
    try:
        resp = await c.post(
            "/v1/admin/tenants", json={"name": "X", "slug": "x"}
        )
        assert resp.status_code == 401
    finally:
        await c.aclose()


async def test_invalid_token_returns_401(make_client):
    c = make_client(auth_enabled=True)
    try:
        resp = await c.post(
            "/v1/admin/tenants",
            json={"name": "X", "slug": "x"},
            headers={"Authorization": "Bearer eng_totallybogus"},
        )
        assert resp.status_code == 401
    finally:
        await c.aclose()


async def test_non_bearer_scheme_rejected(make_client):
    c = make_client(auth_enabled=True)
    try:
        resp = await c.post(
            "/v1/admin/tenants",
            json={"name": "X", "slug": "x"},
            headers={"Authorization": "Basic abc123"},
        )
        assert resp.status_code == 401
    finally:
        await c.aclose()


async def test_valid_token_admin_scope(make_client, patch_auth_factory):
    key = await _seed_api_key(patch_auth_factory, scopes=["read", "write", "admin"])
    c = make_client(auth_enabled=True)
    try:
        resp = await c.post(
            "/v1/admin/tenants",
            json={"name": "ViaKey", "slug": "viakey"},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 201
        assert resp.json()["name"] == "ViaKey"
    finally:
        await c.aclose()


async def test_valid_token_missing_scope_403(make_client, patch_auth_factory):
    key = await _seed_api_key(patch_auth_factory, scopes=["read"])
    c = make_client(auth_enabled=True)
    try:
        resp = await c.post(
            "/v1/admin/tenants",
            json={"name": "NoScope", "slug": "noscope"},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 403
    finally:
        await c.aclose()


async def test_revoked_key_rejected(make_client, patch_auth_factory):
    key = await _seed_api_key(patch_auth_factory, scopes=["admin"])
    async with patch_auth_factory() as session:
        await session.execute(
            text("UPDATE api_keys SET revoked_at = '2026-01-02' WHERE label = 'test'")
        )
        await session.commit()
    c = make_client(auth_enabled=True)
    try:
        resp = await c.post(
            "/v1/admin/tenants",
            json={"name": "Revoked", "slug": "revoked"},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 401
    finally:
        await c.aclose()


# === Admin CRUD round-trip ===


async def test_create_workspace_and_api_key(make_client):
    c = make_client(auth_enabled=False)
    try:
        t = await c.post(
            "/v1/admin/tenants", json={"name": "Org", "slug": "org"}
        )
        assert t.status_code == 201
        tenant_id = t.json()["id"]

        w = await c.post(
            "/v1/admin/workspaces",
            json={"tenant_id": tenant_id, "name": "Eng", "slug": "eng"},
        )
        assert w.status_code == 201
        assert w.json()["slug"] == "eng"

        p = await c.post(
            "/v1/admin/principals",
            json={"tenant_id": tenant_id, "name": "ci-bot", "type": "agent"},
        )
        assert p.status_code == 201

        # API-key creation uses ARRAY(String) which requires Postgres.
        # The key generation + hashing logic is unit-tested above;
        # here we verify tenant/workspace/principal ORM CRUD works end-to-end.
    finally:
        await c.aclose()


async def test_duplicate_tenant_slug_conflict(make_client):
    c = make_client(auth_enabled=False)
    try:
        r1 = await c.post(
            "/v1/admin/tenants", json={"name": "Dup", "slug": "dupslug"}
        )
        assert r1.status_code == 201
        r2 = await c.post(
            "/v1/admin/tenants", json={"name": "Dup2", "slug": "dupslug"}
        )
        assert r2.status_code == 409
    finally:
        await c.aclose()


# === Workspace membership ===


async def test_check_workspace_membership_true(seeded):
    async with seeded() as session:
        await session.execute(
            text(
                "INSERT INTO workspaces (id, tenant_id, name, slug, created_at) "
                "VALUES ('ws-1', '00000000-0000-0000-0000-000000000001', 'W', 'w', '2026-01-01')"
            )
        )
        await session.execute(
            text(
                "INSERT INTO workspace_members (id, workspace_id, principal_id, role, created_at) "
                "VALUES ('wm-1', 'ws-1', 'p-admin', 'owner', '2026-01-01')"
            )
        )
        await session.commit()

    async with seeded() as session:
        is_member = await check_workspace_membership(
            session, principal_id="p-admin", workspace_id="ws-1"
        )
    assert is_member is True


async def test_check_workspace_membership_false(seeded):
    async with seeded() as session:
        await session.execute(
            text(
                "INSERT INTO workspaces (id, tenant_id, name, slug, created_at) "
                "VALUES ('ws-2', '00000000-0000-0000-0000-000000000001', 'W2', 'w2', '2026-01-01')"
            )
        )
        await session.commit()
    async with seeded() as session:
        is_member = await check_workspace_membership(
            session, principal_id="p-admin", workspace_id="ws-2"
        )
    assert is_member is False
