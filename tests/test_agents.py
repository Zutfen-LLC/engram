# ruff: noqa: E501
"""Tests for self-service agent principal and API-key management.

These endpoints (/v1/agents) let any authenticated user with write scope
create agent principals and keys within their own tenant — no admin scope
required.

Uses the same SQLite test pattern as test_auth.py. The ApiKey.scopes column
uses Postgres ARRAY(String); for SQLite tests we intercept ORM flushes to
serialize scopes as a comma-separated TEXT value (the same encoding
auth._parse_scopes already handles).
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
    DIGEST_ALGORITHM,
    digest_api_key_secret,
    generate_api_key,
    parse_api_key,
    reset_principal_cache,
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
    CREATE TABLE principals (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        name TEXT NOT NULL,
        type TEXT NOT NULL,
        internal_key TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE api_keys (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        principal_id TEXT,
        key_hash TEXT,
        key_id TEXT,
        secret_digest TEXT,
        digest_algorithm TEXT,
        scopes TEXT NOT NULL,
        label TEXT,
        created_at TEXT NOT NULL,
        revoked_at TEXT
    )
    """,
    """
    CREATE TABLE memory_kinds (
        tenant_id TEXT NOT NULL,
        name TEXT NOT NULL,
        display_name TEXT NOT NULL,
        description TEXT,
        is_builtin INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        singleton INTEGER NOT NULL DEFAULT 0,
        stays_in_recall_when_disputed INTEGER NOT NULL DEFAULT 0,
        requires_review INTEGER NOT NULL DEFAULT 0,
        auto_promote_from_inferred INTEGER NOT NULL DEFAULT 0,
        default_importance REAL,
        sort_order INTEGER NOT NULL DEFAULT 100,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (tenant_id, name)
    )
    """,
    """
    CREATE TABLE tenant_config (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        config_version TEXT,
        weight_importance REAL,
        weight_source_trust REAL,
        weight_memory_confidence REAL,
        weight_recency REAL,
        weight_verified REAL,
        auto_promote_enabled INTEGER,
        auto_promote_confidence_threshold REAL,
        auto_promote_min_age_hours INTEGER,
        auto_promote_evidence_enabled INTEGER,
        auto_promote_evidence_threshold REAL,
        max_pinned_tokens INTEGER,
        stale_after_days INTEGER,
        startup_recall_penalty_threshold INTEGER,
        startup_recall_penalty_factor REAL,
        feedback_daily_limit INTEGER,
        trust_manual_user REAL,
        trust_manual_agent REAL,
        trust_import REAL,
        trust_extraction REAL,
        trust_sync_turn REAL,
        trust_pre_compress REAL,
        trust_session_end REAL,
        confidence_manual_user REAL,
        confidence_manual_agent REAL,
        confidence_import REAL,
        confidence_extraction REAL,
        confidence_sync_turn REAL,
        confidence_pre_compress REAL,
        confidence_session_end REAL,
        active INTEGER,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,

]


def _scopes_to_sql(scopes: list[str]) -> str:
    return ",".join(scopes)


def _sql_to_scopes(raw: str | list[str]) -> list[str]:
    if isinstance(raw, list):
        return raw
    return [s for s in raw.split(",") if s] if raw else []


@pytest.fixture()
async def session_factory(tmp_path: Path):
    db_path = tmp_path / "engram_agents.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        for stmt in CREATE_STATEMENTS:
            await conn.exec_driver_sql(stmt)

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield factory
    await engine.dispose()


@pytest.fixture(autouse=True)
def _reset_principal_cache():
    reset_principal_cache()


@pytest.fixture()
async def seeded(session_factory):
    """Seed a tenant and an admin principal."""
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
    import engram.auth as auth_mod

    monkeypatch.setattr(auth_mod, "_get_session_factory", lambda: seeded)
    return seeded


@pytest.fixture()
def make_client(patch_auth_factory):
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


async def _seed_new_api_key(
    factory,
    *,
    scopes: list[str],
    tenant_id: str = "00000000-0000-0000-0000-000000000001",
    principal_id: str = "00000000-0000-0000-0000-000000000002",
    label: str = "test-new",
) -> str:
    """Seed a NEW-format key (eng_<key_id>_<secret>) and return plaintext."""
    plaintext = generate_api_key()
    parsed = parse_api_key(plaintext)
    assert parsed.key_id is not None
    digest = digest_api_key_secret(parsed.secret)
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO api_keys "
                "  (id, tenant_id, principal_id, key_hash, key_id, secret_digest, "
                "   digest_algorithm, scopes, label, created_at, revoked_at) "
                "VALUES (:id, :tid, :pid, NULL, :kid, :sd, :da, :sc, :lbl, '2026-01-01', NULL)"
            ),
            {
                "id": f"kn-{label}",
                "tid": tenant_id,
                "pid": principal_id,
                "kid": parsed.key_id,
                "sd": digest,
                "da": DIGEST_ALGORITHM,
                "sc": _scopes_to_sql(scopes),
                "lbl": label,
            },
        )
        await session.commit()
    return plaintext


# === Create agent ===


async def test_create_agent_returns_key_and_principal(make_client, patch_auth_factory):
    """POST /v1/agents creates a principal + API key and returns plaintext once."""
    key = await _seed_new_api_key(patch_auth_factory, scopes=["read", "write"])
    c = make_client(auth_enabled=True)
    try:
        resp = await c.post(
            "/v1/agents",
            json={"name": "coding-agent"},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["name"] == "coding-agent"
        assert body["type"] == "agent"
        assert body["key"] is not None
        assert body["key"].startswith("eng_")
        assert body["key_id"] is not None
        assert body["scopes"] == ["read", "write"]

        # The returned key must authenticate — verify it resolves.
        reset_principal_cache()
        whoami = await c.get(
            "/whoami",
            headers={"Authorization": f"Bearer {body['key']}"},
        )
        assert whoami.status_code == 200
        assert whoami.json()["principal_id"] == body["id"]
    finally:
        await c.aclose()


async def test_create_agent_with_custom_scopes(make_client, patch_auth_factory):
    """Agent creation honors requested scopes in canonical order."""
    key = await _seed_new_api_key(patch_auth_factory, scopes=["read", "write", "admin"])
    c = make_client(auth_enabled=True)
    try:
        resp = await c.post(
            "/v1/agents",
            json={"name": "admin-agent", "scopes": ["write", "read"]},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["scopes"] == ["read", "write"]
    finally:
        await c.aclose()


async def test_create_agent_with_label(make_client, patch_auth_factory):
    """Custom labels are stored on the API key."""
    key = await _seed_new_api_key(patch_auth_factory, scopes=["read", "write"])
    c = make_client(auth_enabled=True)
    try:
        resp = await c.post(
            "/v1/agents",
            json={"name": "labeled-agent", "label": "production-deploy"},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["label"] == "production-deploy"
    finally:
        await c.aclose()


async def test_create_agent_rejects_internal_name_prefix(make_client, patch_auth_factory):
    """Names using the reserved internal prefix are rejected."""
    key = await _seed_new_api_key(patch_auth_factory, scopes=["read", "write"])
    c = make_client(auth_enabled=True)
    try:
        resp = await c.post(
            "/v1/agents",
            json={"name": "__engram_internal_review__evil"},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 422
    finally:
        await c.aclose()


async def test_create_agent_requires_write_scope(make_client, patch_auth_factory):
    """A read-only key cannot create agents (requires write scope)."""
    key = await _seed_new_api_key(patch_auth_factory, scopes=["read"], label="readonly")
    c = make_client(auth_enabled=True)
    try:
        resp = await c.post(
            "/v1/agents",
            json={"name": "should-fail"},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 403
    finally:
        await c.aclose()


# === List agents ===


async def test_list_agents_returns_only_agent_type(make_client, patch_auth_factory):
    """GET /v1/agents lists only agent-type principals, not users/admins."""
    key = await _seed_new_api_key(patch_auth_factory, scopes=["read", "write"])
    c = make_client(auth_enabled=True)
    try:
        # Create two agents.
        for name in ("agent-one", "agent-two"):
            resp = await c.post(
                "/v1/agents",
                json={"name": name},
                headers={"Authorization": f"Bearer {key}"},
            )
            assert resp.status_code == 201

        # List agents.
        resp = await c.get(
            "/v1/agents",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200
        agents = resp.json()
        agent_names = {a["name"] for a in agents}
        assert "agent-one" in agent_names
        assert "agent-two" in agent_names
        # The admin principal should NOT appear.
        assert "admin" not in agent_names
        # All entries should be type=agent.
        assert all(a["type"] == "agent" for a in agents)
    finally:
        await c.aclose()


async def test_list_agents_requires_read_scope(make_client, patch_auth_factory):
    """Without read scope, listing is denied."""
    key = await _seed_new_api_key(patch_auth_factory, scopes=["export"], label="export-only")
    c = make_client(auth_enabled=True)
    try:
        resp = await c.get(
            "/v1/agents",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 403
    finally:
        await c.aclose()


# === Delete/revoke agent ===


async def test_delete_agent_revokes_keys(make_client, patch_auth_factory):
    """DELETE /v1/agents/{id} revokes all API keys for the agent."""
    key = await _seed_new_api_key(patch_auth_factory, scopes=["read", "write"])
    c = make_client(auth_enabled=True)
    try:
        # Create an agent.
        resp = await c.post(
            "/v1/agents",
            json={"name": "to-revoke"},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 201
        agent = resp.json()
        agent_key = agent["key"]
        agent_id = agent["id"]

        # Verify the agent key works.
        reset_principal_cache()
        whoami = await c.get(
            "/whoami",
            headers={"Authorization": f"Bearer {agent_key}"},
        )
        assert whoami.status_code == 200

        # Revoke the agent.
        reset_principal_cache()
        resp = await c.delete(
            f"/v1/agents/{agent_id}",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 204

        # The agent's key should no longer work.
        reset_principal_cache()
        whoami = await c.get(
            "/whoami",
            headers={"Authorization": f"Bearer {agent_key}"},
        )
        assert whoami.status_code == 401
    finally:
        await c.aclose()


async def test_delete_nonexistent_agent_returns_404(make_client, patch_auth_factory):
    """Deleting a non-existent agent returns 404."""
    key = await _seed_new_api_key(patch_auth_factory, scopes=["read", "write"])
    c = make_client(auth_enabled=True)
    try:
        resp = await c.delete(
            "/v1/agents/00000000-0000-0000-0000-000000009999",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 404
    finally:
        await c.aclose()


async def test_delete_agent_requires_write_scope(make_client, patch_auth_factory):
    """A read-only key cannot revoke agents."""
    key = await _seed_new_api_key(patch_auth_factory, scopes=["read", "write"])
    read_only_key = await _seed_new_api_key(
        patch_auth_factory, scopes=["read"], label="readonly2"
    )
    c = make_client(auth_enabled=True)
    try:
        # Create an agent.
        resp = await c.post(
            "/v1/agents",
            json={"name": "target"},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 201
        agent_id = resp.json()["id"]

        # Try to revoke with read-only key.
        reset_principal_cache()
        resp = await c.delete(
            f"/v1/agents/{agent_id}",
            headers={"Authorization": f"Bearer {read_only_key}"},
        )
        assert resp.status_code == 403
    finally:
        await c.aclose()


# === Auth-disabled mode ===


async def test_create_agent_auth_disabled(make_client):
    """In auth-disabled mode, agents can be created without a token."""
    c = make_client(auth_enabled=False)
    try:
        resp = await c.post("/v1/agents", json={"name": "dev-agent"})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["name"] == "dev-agent"
        assert body["key"] is not None
    finally:
        await c.aclose()
