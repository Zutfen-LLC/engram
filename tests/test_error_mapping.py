"""Integration tests for centralized DB-error → HTTP-status mapping (BL-003).

These exercise real Postgres CHECK, FK, and unique-constraint failures end
to end — through the actual asyncpg driver, not a mock — so they verify the
SQLSTATE-based classification in ``engram.api.errors`` against the real
schema in migrations/001_init.sql. They require a live PostgreSQL with the
v2 schema and skip automatically when unreachable, matching the pattern in
test_remember.py. Run locally with ``docker compose up``.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.api.app import create_app
from engram.auth import Principal as AuthPrincipal
from engram.auth import get_current_principal
from engram.config import settings
from engram.db import _DEFAULT_PRINCIPAL_NAME, _DEFAULT_TENANT_SLUG, get_session

_test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_test_session_factory = async_sessionmaker(
    _test_engine, class_=AsyncSession, expire_on_commit=False
)


async def _db_ok() -> bool:
    try:
        async with _test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _get_test_session() -> AsyncSession:
    async with _test_session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT t.id::text AS tenant_id, p.id::text AS principal_id "
                    "FROM tenants t "
                    "JOIN principals p ON p.tenant_id = t.id AND p.name = :principal "
                    "WHERE t.slug = :slug"
                ),
                {"slug": _DEFAULT_TENANT_SLUG, "principal": _DEFAULT_PRINCIPAL_NAME},
            )
        ).mappings().one()
        from engram.db import apply_rls_context

        await apply_rls_context(
            session, tenant_id=row["tenant_id"], principal_id=row["principal_id"]
        )
        yield session


async def _override_get_current_principal() -> AuthPrincipal:
    """Resolve the seed default principal via our NullPool test engine.

    Admin routes go through ``require_scopes`` -> ``get_current_principal``,
    which by default opens its own session on the module-global, pooled
    ``engram.db.engine``. Pytest-asyncio gives each test function its own
    event loop, and asyncpg connections can't be reused across event loops —
    reusing that pooled engine across tests raises a spurious
    ``InterfaceError``. Overriding this dependency to use our per-test
    NullPool engine avoids that entirely, matching how ``get_session`` is
    already overridden below.
    """
    async with _test_session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT t.id::text AS tenant_id, p.id::text AS principal_id "
                    "FROM tenants t "
                    "JOIN principals p ON p.tenant_id = t.id AND p.name = :principal "
                    "WHERE t.slug = :slug"
                ),
                {"slug": _DEFAULT_TENANT_SLUG, "principal": _DEFAULT_PRINCIPAL_NAME},
            )
        ).mappings().one()
    return AuthPrincipal(
        tenant_id=row["tenant_id"],
        principal_id=row["principal_id"],
        scopes=("read", "write", "admin", "export"),
    )


@pytest.fixture
def app():
    app = create_app()
    app.dependency_overrides[get_session] = _get_test_session
    app.dependency_overrides[get_current_principal] = _override_get_current_principal
    return app


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
async def _clean_db():
    if not await _db_ok():
        return
    async with _test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM memory_embeddings"))
        await conn.execute(text("DELETE FROM memory_items"))


async def _default_tenant_id() -> str:
    async with _test_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT id::text FROM tenants WHERE slug = :slug"),
                {"slug": _DEFAULT_TENANT_SLUG},
            )
        ).one()
        return str(row[0])


# ---- CHECK constraint violations ----


async def test_remember_invalid_kind_returns_422(client):
    """`kind` isn't Pydantic-restricted — an unknown value must be rejected with
    a clear 422, not a 500. As of ENG-AUD-010, this is caught by application-level
    registry validation (engram.memory_kinds.require_enabled_memory_kind) before
    the DB is ever touched, replacing the old chk_kind CHECK constraint."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    response = await client.post(
        "/v1/remember",
        json={"content": "invalid kind probe content", "kind": "not_a_real_kind"},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "not_a_real_kind" in str(detail)


async def test_admin_principal_invalid_type_returns_422_check_violation(client):
    """PrincipalCreate.type is a bare str — the DB CHECK constraint on
    principals.type is the real gate against an invalid enum value."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id = await _default_tenant_id()
    response = await client.post(
        "/v1/admin/principals",
        json={"tenant_id": tenant_id, "name": f"bad-type-{uuid.uuid4().hex}", "type": "superadmin"},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["code"] == "check_violation"


# ---- Foreign-key violations ----


async def test_admin_workspace_unknown_tenant_returns_422_not_500(client):
    """A syntactically valid but nonexistent tenant_id must FK-fail as a
    client error (422), not surface as a raw 500."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    response = await client.post(
        "/v1/admin/workspaces",
        json={"tenant_id": str(uuid.uuid4()), "name": "Ghost", "slug": "ghost-ws"},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["code"] == "foreign_key_violation"


async def test_admin_principal_unknown_tenant_returns_422_not_500(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    response = await client.post(
        "/v1/admin/principals",
        json={
            "tenant_id": str(uuid.uuid4()),
            "name": f"orphan-{uuid.uuid4().hex}",
            "type": "agent",
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["code"] == "foreign_key_violation"


# ---- Unique-constraint conflicts (not the remember-dedup path) ----


async def test_admin_duplicate_tenant_slug_returns_409(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    slug = f"dup-{uuid.uuid4().hex[:12]}"
    first = await client.post("/v1/admin/tenants", json={"name": "Dup", "slug": slug})
    assert first.status_code == 201
    second = await client.post("/v1/admin/tenants", json={"name": "Dup2", "slug": slug})
    assert second.status_code == 409
    detail = second.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["code"] == "unique_violation"


async def test_admin_duplicate_principal_name_returns_409(client):
    """principals has UNIQUE(tenant_id, name) — a real conflict, not dedup."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id = await _default_tenant_id()
    name = f"conflict-{uuid.uuid4().hex[:12]}"
    first = await client.post(
        "/v1/admin/principals", json={"tenant_id": tenant_id, "name": name, "type": "agent"}
    )
    assert first.status_code == 201
    second = await client.post(
        "/v1/admin/principals", json={"tenant_id": tenant_id, "name": name, "type": "agent"}
    )
    assert second.status_code == 409
    detail = second.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["code"] == "unique_violation"


# ---- Remember's own dedup path stays a success, not an error ----


async def test_remember_dedup_is_not_reported_as_error(client):
    """The remember-dedup unique index (idx_memitems_dedup) is normal
    idempotent-retry behavior — it must not be classified as a 409/422."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    payload = {"content": f"dedup probe {uuid.uuid4().hex}", "source_type": "manual"}
    first = await client.post("/v1/remember", json=payload)
    assert first.status_code == 201
    second = await client.post("/v1/remember", json=payload)
    assert second.status_code == 201
    assert second.json()["status"] == "deduped"
