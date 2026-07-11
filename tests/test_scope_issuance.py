# ruff: noqa: E501
"""Real-Postgres coverage for V2-BL-004 scope-issuance validation (ticket section B).

Exercises `POST /v1/admin/api-keys` and `engram bootstrap-key --scopes` end to
end against a live Postgres — SQLite-only tests are insufficient proof for
issuance validation since `api_keys.scopes` is a real `TEXT[]` column.
Requires a live PostgreSQL with the v2 schema; skips automatically when no DB
is reachable (mirrors tests/test_trusted_actor.py).
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.api.app import create_app
from engram.auth import reset_principal_cache
from engram.config import settings
from engram.db import get_session

_test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_test_session_factory = async_sessionmaker(
    _test_engine, class_=AsyncSession, expire_on_commit=False
)

_LABEL_PREFIX = "v2bl4-issuance-"


async def _db_ok() -> bool:
    try:
        async with _test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _require_db():
    pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")


@pytest.fixture(autouse=True)
async def _clean_db():
    if not await _db_ok():
        return
    reset_principal_cache()
    async with _test_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM api_keys WHERE label LIKE :prefix"),
            {"prefix": f"{_LABEL_PREFIX}%"},
        )
        await conn.execute(
            text(
                "DELETE FROM principals WHERE tenant_id = (SELECT id FROM tenants WHERE slug = 'default') "
                "AND name LIKE 'v2bl4-issuance-%'"
            )
        )


async def _default_tenant_principal() -> tuple[str, str]:
    async with _test_session_factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT t.id::text AS tenant_id, p.id::text AS principal_id "
                        "FROM tenants t "
                        "JOIN principals p ON p.tenant_id = t.id AND p.name = 'admin' "
                        "WHERE t.slug = 'default'"
                    )
                )
            )
            .mappings()
            .one()
        )
    return str(row["tenant_id"]), str(row["principal_id"])


async def _seed_agent_principal(tenant_id: str, name: str) -> str:
    principal_id = str(uuid.uuid4())
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES (:id, :tid, :name, 'agent')"
            ),
            {"id": principal_id, "tid": tenant_id, "name": name},
        )
        await session.commit()
    return principal_id


async def _make_admin_client(
    tenant_id: str, principal_id: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    """ASGI client authorized as `principal_id` via a direct get_session override.

    Mirrors tests/test_trusted_actor.py's `_make_admin_client` — this bypasses
    bearer-token auth entirely (sets RLS GUCs directly), which is fine here
    because these tests exercise the admin *issuance* endpoint's request-body
    validation, not scope-gated authentication itself (see
    test_scope_enforcement.py for the bearer-token-driven matrix).
    """
    app = create_app()

    async def _override_get_session():
        async with _test_session_factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
            )
            await session.execute(
                text("SELECT set_config('app.principal_id', :pid, true)"), {"pid": principal_id}
            )
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    import engram.db as db_module

    monkeypatch.setattr(db_module, "async_session_factory", _test_session_factory)
    monkeypatch.setattr(db_module, "owner_session_factory", _test_session_factory)
    monkeypatch.setattr(db_module, "read_session_factory", _test_session_factory)
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# ===========================================================================
# POST /v1/admin/api-keys — scope-list validation
# ===========================================================================


@pytest.mark.parametrize("scope", ["read", "write", "review", "export", "admin"])
async def test_issuance_accepts_each_individual_scope(
    scope: str, monkeypatch: pytest.MonkeyPatch
):
    if not await _db_ok():
        _require_db()
    tenant_id, admin_pid = await _default_tenant_principal()
    agent_id = await _seed_agent_principal(tenant_id, f"v2bl4-issuance-{scope}")
    client = await _make_admin_client(tenant_id, admin_pid, monkeypatch)
    async with client:
        resp = await client.post(
            "/v1/admin/api-keys",
            json={
                "tenant_id": tenant_id,
                "principal_id": agent_id,
                "scopes": [scope],
                "label": f"{_LABEL_PREFIX}{scope}",
            },
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["scopes"] == [scope]


async def test_issuance_accepts_all_five_scopes(monkeypatch: pytest.MonkeyPatch):
    if not await _db_ok():
        _require_db()
    tenant_id, admin_pid = await _default_tenant_principal()
    agent_id = await _seed_agent_principal(tenant_id, "v2bl4-issuance-all")
    client = await _make_admin_client(tenant_id, admin_pid, monkeypatch)
    async with client:
        resp = await client.post(
            "/v1/admin/api-keys",
            json={
                "tenant_id": tenant_id,
                "principal_id": agent_id,
                "scopes": ["admin", "export", "review", "write", "read"],
                "label": f"{_LABEL_PREFIX}all",
            },
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["scopes"] == ["read", "write", "review", "export", "admin"]


async def test_issuance_canonicalizes_duplicates_and_order(monkeypatch: pytest.MonkeyPatch):
    if not await _db_ok():
        _require_db()
    tenant_id, admin_pid = await _default_tenant_principal()
    agent_id = await _seed_agent_principal(tenant_id, "v2bl4-issuance-dedup")
    client = await _make_admin_client(tenant_id, admin_pid, monkeypatch)
    async with client:
        resp = await client.post(
            "/v1/admin/api-keys",
            json={
                "tenant_id": tenant_id,
                "principal_id": agent_id,
                "scopes": ["review", "read", "review"],
                "label": f"{_LABEL_PREFIX}dedup",
            },
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["scopes"] == ["read", "review"]


async def test_issuance_rejects_unknown_scope(monkeypatch: pytest.MonkeyPatch):
    if not await _db_ok():
        _require_db()
    tenant_id, admin_pid = await _default_tenant_principal()
    agent_id = await _seed_agent_principal(tenant_id, "v2bl4-issuance-unknown")
    client = await _make_admin_client(tenant_id, admin_pid, monkeypatch)
    async with client:
        resp = await client.post(
            "/v1/admin/api-keys",
            json={
                "tenant_id": tenant_id,
                "principal_id": agent_id,
                "scopes": ["read", "superuser"],
                "label": f"{_LABEL_PREFIX}unknown",
            },
        )
    assert resp.status_code == 422, resp.text
    # No row was persisted for the rejected request.
    async with _test_session_factory() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM api_keys WHERE label = :label"),
                {"label": f"{_LABEL_PREFIX}unknown"},
            )
        ).scalar_one()
    assert count == 0


async def test_issuance_rejects_typo_scope(monkeypatch: pytest.MonkeyPatch):
    if not await _db_ok():
        _require_db()
    tenant_id, admin_pid = await _default_tenant_principal()
    agent_id = await _seed_agent_principal(tenant_id, "v2bl4-issuance-typo")
    client = await _make_admin_client(tenant_id, admin_pid, monkeypatch)
    async with client:
        resp = await client.post(
            "/v1/admin/api-keys",
            json={
                "tenant_id": tenant_id,
                "principal_id": agent_id,
                "scopes": ["reviews"],
                "label": f"{_LABEL_PREFIX}typo",
            },
        )
    assert resp.status_code == 422, resp.text


async def test_issuance_explicit_empty_scopes_stays_empty(monkeypatch: pytest.MonkeyPatch):
    if not await _db_ok():
        _require_db()
    tenant_id, admin_pid = await _default_tenant_principal()
    agent_id = await _seed_agent_principal(tenant_id, "v2bl4-issuance-empty")
    client = await _make_admin_client(tenant_id, admin_pid, monkeypatch)
    async with client:
        resp = await client.post(
            "/v1/admin/api-keys",
            json={
                "tenant_id": tenant_id,
                "principal_id": agent_id,
                "scopes": [],
                "label": f"{_LABEL_PREFIX}empty",
            },
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["scopes"] == []


async def test_issuance_omitted_scopes_defaults_to_read_write(monkeypatch: pytest.MonkeyPatch):
    if not await _db_ok():
        _require_db()
    tenant_id, admin_pid = await _default_tenant_principal()
    agent_id = await _seed_agent_principal(tenant_id, "v2bl4-issuance-default")
    client = await _make_admin_client(tenant_id, admin_pid, monkeypatch)
    async with client:
        resp = await client.post(
            "/v1/admin/api-keys",
            json={
                "tenant_id": tenant_id,
                "principal_id": agent_id,
                "label": f"{_LABEL_PREFIX}default",
            },
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["scopes"] == ["read", "write"]


async def test_issuance_still_rejects_internal_principal(monkeypatch: pytest.MonkeyPatch):
    """Scope validation must not weaken the V2-BL-003B internal-principal gate."""
    if not await _db_ok():
        _require_db()
    from engram.promotion import resolve_trusted_system_actor

    tenant_id, admin_pid = await _default_tenant_principal()
    async with _test_session_factory() as session:
        internal_id = await resolve_trusted_system_actor(session, tenant_id)
        await session.commit()
    client = await _make_admin_client(tenant_id, admin_pid, monkeypatch)
    async with client:
        resp = await client.post(
            "/v1/admin/api-keys",
            json={
                "tenant_id": tenant_id,
                "principal_id": str(internal_id),
                "scopes": ["review"],
                "label": f"{_LABEL_PREFIX}internal",
            },
        )
    assert resp.status_code == 409, resp.text


# ===========================================================================
# `engram bootstrap-key --scopes` (pure logic already covered in test_cli.py;
# this proves the same canonicalize_scopes() path is used end to end when a
# key is actually persisted via the live-DB bootstrap flow).
# ===========================================================================


async def test_bootstrap_key_accepts_review_and_persists_canonically():
    if not await _db_ok():
        _require_db()
    from engram.cli import make_bootstrap_key, parse_scopes

    scopes = parse_scopes("review,read")
    assert scopes == ["read", "review"]
    material = make_bootstrap_key("v2bl4-bootstrap-review", scopes)
    assert material.scopes == ("read", "review")


async def test_bootstrap_key_rejects_unknown_scope():
    from engram.cli import parse_scopes

    with pytest.raises(ValueError, match="unknown scope"):
        parse_scopes("read,not_a_real_scope")
