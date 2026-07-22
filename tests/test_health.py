"""Test health and readiness endpoints.

The ``/health`` liveness probe needs no database and always runs. The
``/ready`` probe requires a live PostgreSQL with the v2 schema; it skips
automatically when no DB is reachable (e.g. CI runs no service containers). Run
it locally with ``docker compose up``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from engram.api.app import create_app
from engram.api.routes.health import whoami
from engram.auth import Principal
from engram.db import engine


async def _db_ok() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_health(client):
    """Liveness probe — always available, no DB needed."""
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("principal_type", ["agent", "user", "admin", "system"])
async def test_whoami_truthfully_exposes_principal_type(principal_type: str) -> None:
    session = AsyncMock()
    session.scalar.return_value = principal_type
    principal = Principal(
        tenant_id="11111111-1111-1111-1111-111111111111",
        principal_id="22222222-2222-2222-2222-222222222222",
        scopes=("read",),
    )
    response = await whoami(principal=principal, session=session)
    assert isinstance(response, dict)
    assert response["principal_type"] == principal_type
    session.scalar.assert_awaited_once()


async def test_ready_requires_db(client):
    """GET /ready confirms DB connected, RLS context set, and pgvector >= 0.8.

    Skipped when no DB is present (CI); run with ``docker compose up`` to
    exercise. The /ready dependency chain runs get_session, which SET LOCALs
    app.tenant_id/app.principal_id from seed data before the handler runs, so a
    200 here proves DB connectivity, a resolvable RLS context, AND a sufficient
    pgvector extension version.
    """
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    response = await client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["database"] == "connected"
    # pgvector version is reported and must satisfy the minimum (>= 0.8).
    assert "pgvector" in body
    from engram.api.routes.health import pgvector_version_satisfies

    assert pgvector_version_satisfies(body["pgvector"])
