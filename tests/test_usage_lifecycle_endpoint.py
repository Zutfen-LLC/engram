"""Tests for POST /v1/telemetry/lifecycle (ENG-METER-001, Deliverable 7).

Requires a live PostgreSQL with the v2 schema. Skips automatically when no DB
is reachable.

Covers: tenant/principal always come from authentication (never the request
body, even when a caller tries to smuggle them), retries with the same
invocation_id do not double-count, the recorded event is marked
non-authoritative, and status becomes "partial" when errors > 0.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.api.app import create_app
from engram.config import settings
from engram.db import get_session

_test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_test_session_factory = async_sessionmaker(
    _test_engine, class_=AsyncSession, expire_on_commit=False
)


@pytest.fixture(autouse=True)
async def _fresh_engine():
    global _test_engine, _test_session_factory
    _test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
    _test_session_factory = async_sessionmaker(
        _test_engine, class_=AsyncSession, expire_on_commit=False
    )
    yield
    await _test_engine.dispose()


async def _db_ok() -> bool:
    try:
        async with _test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _default_principal() -> tuple[str, str]:
    from engram.db import _DEFAULT_PRINCIPAL_NAME, _DEFAULT_TENANT_SLUG

    async with _test_session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT t.id::text AS tenant_id, p.id::text AS principal_id "
                    "FROM tenants t JOIN principals p ON p.tenant_id = t.id AND p.name = :pn "
                    "WHERE t.slug = :slug"
                ),
                {"slug": _DEFAULT_TENANT_SLUG, "pn": _DEFAULT_PRINCIPAL_NAME},
            )
        ).mappings().one()
        return row["tenant_id"], row["principal_id"]


async def _get_test_session() -> AsyncSession:
    async with _test_session_factory() as session:
        from engram.db import apply_rls_context

        tenant_id, principal_id = await _default_principal()
        await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
        yield session


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
async def client(app):
    app.dependency_overrides[get_session] = _get_test_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
async def _clean_db():
    if not await _db_ok():
        return
    async with _test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM usage_events"))


@pytest.fixture(autouse=True)
def _enable_telemetry(monkeypatch):
    monkeypatch.setattr(settings, "usage_telemetry_enabled", True)


async def test_tenant_and_principal_come_from_auth_not_body(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    real_tenant_id, real_principal_id = await _default_principal()
    spoofed_tenant = str(uuid.uuid4())
    spoofed_principal = str(uuid.uuid4())
    invocation_id = str(uuid.uuid4())

    resp = await client.post(
        "/v1/telemetry/lifecycle",
        json={
            "invocation_id": invocation_id,
            "event": "sync_turn",
            "extracted": 3,
            "guard_rejected": 1,
            "classified": 2,
            "promoted": 1,
            "parked": 1,
            "errors": 0,
            "candidate_bytes": 42,
            # Attempted spoofing — these are not real request fields.
            "tenant_id": spoofed_tenant,
            "principal_id": spoofed_principal,
        },
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "succeeded"
    assert body["invocation_id"] == invocation_id

    async with _test_session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT tenant_id::text, principal_id::text, metadata "
                    "FROM usage_events WHERE event_type = 'client.lifecycle_summary' "
                    "AND dedupe_key = :dk"
                ),
                {"dk": invocation_id},
            )
        ).mappings().one()
    assert row["tenant_id"] == real_tenant_id
    assert row["principal_id"] == real_principal_id
    assert row["tenant_id"] != spoofed_tenant
    assert row["principal_id"] != spoofed_principal


async def test_retry_with_same_invocation_id_does_not_double_count(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    invocation_id = str(uuid.uuid4())
    payload = {
        "invocation_id": invocation_id,
        "event": "session_end",
        "extracted": 5,
    }
    first = await client.post("/v1/telemetry/lifecycle", json=payload)
    second = await client.post("/v1/telemetry/lifecycle", json=payload)
    assert first.status_code == 202
    assert second.status_code == 202

    async with _test_session_factory() as session:
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM usage_events "
                    "WHERE event_type = 'client.lifecycle_summary' AND dedupe_key = :dk"
                ),
                {"dk": invocation_id},
            )
        ).scalar_one()
    assert count == 1


async def test_recorded_as_non_authoritative(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    invocation_id = str(uuid.uuid4())
    await client.post(
        "/v1/telemetry/lifecycle",
        json={"invocation_id": invocation_id, "event": "pre_compress", "extracted": 1},
    )
    async with _test_session_factory() as session:
        metadata = (
            await session.execute(
                text(
                    "SELECT metadata FROM usage_events "
                    "WHERE event_type = 'client.lifecycle_summary' AND dedupe_key = :dk"
                ),
                {"dk": invocation_id},
            )
        ).scalar_one()
    if isinstance(metadata, str):
        import json

        metadata = json.loads(metadata)
    assert metadata["authoritative"] is False


async def test_errors_yield_partial_status(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    resp = await client.post(
        "/v1/telemetry/lifecycle",
        json={
            "invocation_id": str(uuid.uuid4()),
            "event": "sync_turn",
            "extracted": 2,
            "errors": 1,
        },
    )
    assert resp.status_code == 202
    assert resp.json()["status"] == "partial"
