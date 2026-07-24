"""Health and readiness endpoint tests."""

from __future__ import annotations

from httpx import AsyncClient

from engram_control_plane import __version__


async def test_health_is_stable_and_database_free(client: AsyncClient) -> None:
    """Health is a stable liveness response and never touches the database."""
    r1 = await client.get("/health")
    r2 = await client.get("/health")
    assert r1.status_code == 200
    assert r1.json() == r2.json()
    body = r1.json()
    assert body["status"] == "ok"
    assert body["service"] == "engram-control-plane"
    assert body["version"] == __version__
    assert "build_sha" in body


async def test_ready_returns_503_when_no_database_configured(
    client: AsyncClient, fresh_settings
) -> None:
    """In a database-free dev configuration, readiness reports an honest 503."""
    assert fresh_settings.database_url is None
    r = await client.get("/ready")
    assert r.status_code == 503
    assert r.json()["error"] == "not_ready"
    # Sanitized: no DB URL or credential in the body.
    assert "postgresql" not in r.text.lower()


async def test_ready_sanitizes_connection_failure(
    client: AsyncClient, monkeypatch
) -> None:
    """A DB connection failure yields a sanitized 503 with no credential leak."""
    import engram_control_plane.api.routes.health as health_mod

    async def _boom(_session):
        # Simulate a library whose str() includes a credential-laden URL.
        raise OperationalError(
            "connection failed: postgresql+asyncpg://user:hunter2@host/db"
        )

    class OperationalError(Exception):
        pass

    monkeypatch.setattr(health_mod, "check_readiness", _boom)
    # Force a database_url so the endpoint attempts the DB path.
    monkeypatch.setattr(health_mod.settings, "database_url", "postgresql+asyncpg://x")
    # Provide a session factory that yields a context manager returning a dummy.
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_session():
        yield object()

    monkeypatch.setattr(health_mod, "_ready_session", _fake_session)

    r = await client.get("/ready")
    assert r.status_code == 503
    # The credential URL must NOT appear in the response body.
    assert "hunter2" not in r.text
    assert "postgresql" not in r.text.lower()


async def test_ready_at_head_returns_200(client: AsyncClient, monkeypatch) -> None:
    """When the DB reports migration at head, readiness returns 200."""
    from contextlib import asynccontextmanager

    import engram_control_plane.api.routes.health as health_mod

    async def _ok(_session):
        return {"database": "connected", "migration": "at_head",
                "current": "0001_initial", "head": "0001_initial"}

    monkeypatch.setattr(health_mod, "check_readiness", _ok)
    monkeypatch.setattr(health_mod.settings, "database_url", "postgresql+asyncpg://x")

    @asynccontextmanager
    async def _fake_session():
        yield object()

    monkeypatch.setattr(health_mod, "_ready_session", _fake_session)

    r = await client.get("/ready")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"
    assert r.json()["migration"] == "at_head"


async def test_ready_behind_returns_503(client: AsyncClient, monkeypatch) -> None:
    """When the DB reports migration behind, readiness returns 503."""
    from contextlib import asynccontextmanager

    import engram_control_plane.api.routes.health as health_mod

    async def _behind(_session):
        return {"database": "connected", "migration": "behind",
                "current": None, "head": "0001_initial"}

    monkeypatch.setattr(health_mod, "check_readiness", _behind)
    monkeypatch.setattr(health_mod.settings, "database_url", "postgresql+asyncpg://x")

    @asynccontextmanager
    async def _fake_session():
        yield object()

    monkeypatch.setattr(health_mod, "_ready_session", _fake_session)

    r = await client.get("/ready")
    assert r.status_code == 503
    assert "behind" in r.json()["detail"]


async def test_request_id_header_propagated(client: AsyncClient) -> None:
    """An inbound X-Request-ID is echoed back on the response."""
    r = await client.get("/health", headers={"x-request-id": "test-rid-123"})
    assert r.headers["x-request-id"] == "test-rid-123"
