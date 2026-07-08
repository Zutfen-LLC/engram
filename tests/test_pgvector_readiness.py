"""Tests for the pgvector version readiness gate in /ready.

The pure helper :func:`engram.api.routes.health.pgvector_version_satisfies`
gates whether the installed pgvector extension meets the minimum (>= 0.8)
required for ``iterative_scan`` in semantic recall/search. The end-to-end
behavior (a real /ready 503 on a too-old extension) is exercised against a live
Postgres by the Compose-backed CI path; here we cover the parsing logic and the
readiness JSON shape in all branches.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from engram.api.app import create_app
from engram.api.routes.health import PGVECTOR_MINIMUM, pgvector_version_satisfies


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.8.0", True),
        ("0.8", True),
        ("0.8.0-dev", True),
        ("0.9.1", True),
        ("1.0.0", True),
        ("0.7.4", False),
        ("0.5.0", False),
        (None, False),
        ("", False),
        ("garbage", False),
        ("dev", False),
    ],
)
def test_pgvector_version_satisfies(version: str | None, expected: bool):
    assert pgvector_version_satisfies(version) is expected


def test_pgvector_minimum_is_0_8():
    assert PGVECTOR_MINIMUM == (0, 8)
    assert pgvector_version_satisfies("0.7.9") is False
    assert pgvector_version_satisfies("0.8.0") is True


def _override_ready_session(app, *, tenant_id, pgvector_version):
    """Inject a fake session so /ready exercises the handler logic without a DB.

    The handler runs two queries against the session: the RLS tenant check and
    the pg_extension lookup. We make execute() return canned scalars in order.
    """
    from engram.db import get_session

    calls: list[str] = []

    async def fake_execute(stmt, *_args, **_kw):
        compiled = str(stmt)
        calls.append(compiled)
        result = MagicMock()
        if "current_setting" in compiled:
            result.scalar.return_value = tenant_id
        else:  # pg_extension lookup
            result.scalar.return_value = pgvector_version
        return result

    async def override_get_session():
        session = MagicMock()
        session.execute = AsyncMock(side_effect=fake_execute)
        yield session

    app.dependency_overrides[get_session] = override_get_session
    return calls


@pytest.fixture()
def app():
    return create_app()


async def test_ready_reports_pgvector_on_success(app):
    """200 response includes the pgvector version when all checks pass."""
    _override_ready_session(app, tenant_id="tenant-1", pgvector_version="0.8.0")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["database"] == "connected"
    assert body["pgvector"] == "0.8.0"


async def test_ready_503_when_pgvector_too_old(app):
    """Old pgvector -> 503 with the offending version and minimum required."""
    _override_ready_session(app, tenant_id="tenant-1", pgvector_version="0.7.1")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["pgvector"] == "0.7.1"
    assert body["minimum_required"] == "0.8.0"
    assert "iterative_scan" in body["reason"]


async def test_ready_503_when_pgvector_missing(app):
    """Missing pgvector extension -> 503 with 'missing'."""
    _override_ready_session(app, tenant_id="tenant-1", pgvector_version=None)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["pgvector"] == "missing"


async def test_ready_503_no_tenant_context_before_pgvector(app):
    """Missing RLS context short-circuits before the pgvector check."""
    _override_ready_session(app, tenant_id=None, pgvector_version="0.8.0")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["rls"] == "no_tenant_context"
    # pgvector key is absent because the RLS check failed first.
    assert "pgvector" not in body
