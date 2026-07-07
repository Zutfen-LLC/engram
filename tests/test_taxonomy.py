# ruff: noqa: E501
"""Tests for taxonomy, tunnel, and diary endpoints (T14).

These tests require a live PostgreSQL with the v2 schema (migrations/001_init.sql).
They skip automatically when no DB is reachable, matching the pattern in
test_health.py / test_kg.py. Run locally with ``docker compose up``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.api.app import create_app
from engram.config import settings
from engram.db import get_session

_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


async def _db_ok() -> bool:
    try:
        async with _engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _get_test_session() -> AsyncSession:
    async with _session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT t.id::text AS tenant_id, p.id::text AS principal_id "
                    "FROM tenants t "
                    "JOIN principals p ON p.tenant_id = t.id AND p.name = :principal "
                    "WHERE t.slug = :slug"
                ),
                {"slug": "default", "principal": "admin"},
            )
        ).mappings().one()
        rls_tenant_id = row["tenant_id"]
        rls_principal_id = row["principal_id"]
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": rls_tenant_id},
        )
        await session.execute(
            text("SELECT set_config('app.principal_id', :pid, true)"),
            {"pid": rls_principal_id},
        )
        yield session


@pytest.fixture
def app():
    app = create_app()
    app.dependency_overrides[get_session] = _get_test_session
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
    async with _engine.begin() as conn:
        await conn.execute(text("DELETE FROM tunnels"))
        await conn.execute(text("DELETE FROM item_events"))
        await conn.execute(text("DELETE FROM memory_items"))


async def _seed_item(
    session: AsyncSession,
    *,
    wing: str | None,
    room: str | None,
    review_status: str = "active",
    kind: str = "fact",
    principal_id: str | None = None,
    content: str | None = None,
) -> str:
    """Insert a memory_item with the given taxonomy coordinates. Returns the item id."""
    if principal_id is None:
        pid_row = await session.execute(text("SELECT current_setting('app.principal_id', true)"))
        principal_id = pid_row.scalar()
    ws_row = await session.execute(text("SELECT id FROM workspaces LIMIT 1"))
    workspace_id = ws_row.scalar()
    tid_row = await session.execute(text("SELECT current_setting('app.tenant_id', true)"))
    tenant_id = tid_row.scalar()
    item_id = str(uuid4())
    body = content or f"taxonomy-test-{item_id}"
    await session.execute(
        text("""
            INSERT INTO memory_items (
                id, tenant_id, workspace_id, principal_id, content, content_hash, kind,
                wing, room, visibility, review_status, memory_confidence, source_trust,
                human_verified, importance, pinned, recall_count, startup_recall_count,
                source_type, sensitivity, valid_from, created_at
            ) VALUES (
                :id, CAST(:tid AS uuid), :ws, :pid, :content, :chash, :kind,
                :wing, :room, :vis, :rs, :mc, :st,
                :hv, :imp, :pin, :rc, :src, :sotype, :sens, now(), now()
            )
        """),
        {
            "id": item_id, "tid": tenant_id, "ws": workspace_id, "pid": principal_id,
            "content": body, "chash": f"hash-{item_id}", "kind": kind,
            "wing": wing, "room": room, "vis": "workspace", "rs": review_status,
            "mc": 0.8, "st": 0.7, "hv": False, "imp": 0.5, "pin": False,
            "rc": 0, "src": 0, "sotype": "manual", "sens": "normal",
        },
    )
    return item_id


# ---- Taxonomy tests ----


@pytest.mark.asyncio
async def test_taxonomy_groups_by_wing_and_room(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', (SELECT id::text FROM tenants WHERE slug='default'), true)")
        )
        await session.execute(
            text("SELECT set_config('app.principal_id', (SELECT id::text FROM principals WHERE name='admin'), true)")
        )
        await _seed_item(session, wing="infrastructure", room="postgres")
        await _seed_item(session, wing="infrastructure", room="postgres")
        await _seed_item(session, wing="infrastructure", room="redis")
        await _seed_item(session, wing="agents", room="hermes")
        await session.commit()

    response = await client.get("/v1/taxonomy")
    assert response.status_code == 200
    payload = response.json()
    by_wing = {w["name"]: w for w in payload["wings"]}

    assert "infrastructure" in by_wing
    assert "agents" in by_wing
    assert by_wing["infrastructure"]["item_count"] == 3
    assert by_wing["agents"]["item_count"] == 1

    infra_rooms = {r["name"]: r["item_count"] for r in by_wing["infrastructure"]["rooms"]}
    assert infra_rooms == {"postgres": 2, "redis": 1}
    assert payload["total_items"] == 4
    assert payload["wing_count"] == 2


@pytest.mark.asyncio
async def test_taxonomy_excludes_non_active_items(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', (SELECT id::text FROM tenants WHERE slug='default'), true)")
        )
        await session.execute(
            text("SELECT set_config('app.principal_id', (SELECT id::text FROM principals WHERE name='admin'), true)")
        )
        active_id = await _seed_item(session, wing="x", room="y", review_status="active")
        await _seed_item(session, wing="x", room="y", review_status="proposed")
        await _seed_item(session, wing="x", room="y", review_status="archived")
        invalidated_id = await _seed_item(session, wing="x", room="y", review_status="active")
        await session.execute(
            text("UPDATE memory_items SET valid_to = now() WHERE id = CAST(:id AS uuid)"),
            {"id": invalidated_id},
        )
        await session.commit()
        _ = active_id  # silence unused

    response = await client.get("/v1/taxonomy")
    assert response.status_code == 200
    payload = response.json()
    by_wing = {w["name"]: w for w in payload["wings"]}
    assert by_wing["x"]["item_count"] == 1


@pytest.mark.asyncio
async def test_taxonomy_handles_unassigned_wing(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', (SELECT id::text FROM tenants WHERE slug='default'), true)")
        )
        await session.execute(
            text("SELECT set_config('app.principal_id', (SELECT id::text FROM principals WHERE name='admin'), true)")
        )
        await _seed_item(session, wing=None, room=None)
        await session.commit()

    response = await client.get("/v1/taxonomy")
    assert response.status_code == 200
    payload = response.json()
    by_wing = {w["name"]: w for w in payload["wings"]}
    assert "_(unassigned)" in by_wing
    assert by_wing["_(unassigned)"]["item_count"] == 1


# ---- Tunnel tests ----


@pytest.mark.asyncio
async def test_tunnel_create_and_list(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")

    create_resp = await client.post(
        "/v1/tunnels",
        json={
            "source_wing": "agents",
            "source_room": "hermes",
            "target_wing": "infrastructure",
            "target_room": "postgres",
            "label": "hermes uses postgres",
        },
    )
    assert create_resp.status_code == 201
    tunnel = create_resp.json()
    assert tunnel["source_wing"] == "agents"
    assert tunnel["target_wing"] == "infrastructure"
    assert tunnel["label"] == "hermes uses postgres"
    assert "id" in tunnel

    list_resp = await client.get("/v1/tunnels")
    assert list_resp.status_code == 200
    tunnels = list_resp.json()
    assert any(t["id"] == tunnel["id"] for t in tunnels)


@pytest.mark.asyncio
async def test_tunnel_filter_by_wing(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")

    for src, tgt in [("a", "b"), ("b", "c"), ("c", "d")]:
        resp = await client.post(
            "/v1/tunnels",
            json={"source_wing": src, "target_wing": tgt},
        )
        assert resp.status_code == 201

    resp = await client.get("/v1/tunnels", params={"wing": "b"})
    assert resp.status_code == 200
    matched = resp.json()
    wings = {(t["source_wing"], t["target_wing"]) for t in matched}
    assert wings == {("a", "b"), ("b", "c")}


@pytest.mark.asyncio
async def test_tunnel_create_rejects_blank_wings(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")

    resp = await client.post(
        "/v1/tunnels",
        json={"source_wing": "", "target_wing": "x"},
    )
    assert resp.status_code == 422


# ---- Diary tests ----


@pytest.mark.asyncio
async def test_diary_write_creates_diary_entry(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")

    resp = await client.post(
        "/v1/diary",
        json={"entry": "Today I learned about pgvector indexes.", "principal": "admin", "topic": "postgres"},
    )
    assert resp.status_code == 201
    payload = resp.json()
    assert payload["status"] == "created"
    assert "id" in payload
    assert "principal_id" in payload


@pytest.mark.asyncio
async def test_diary_entry_is_private_diary_kind(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")

    write_resp = await client.post(
        "/v1/diary",
        json={"entry": "Wrote a hook today.", "principal": "admin"},
    )
    assert write_resp.status_code == 201
    diary_id = write_resp.json()["id"]

    async with _session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', (SELECT id::text FROM tenants WHERE slug='default'), true)")
        )
        await session.execute(
            text("SELECT set_config('app.principal_id', (SELECT id::text FROM principals WHERE name='admin'), true)")
        )
        row = (
            await session.execute(
                text("SELECT kind, visibility FROM memory_items WHERE id = CAST(:id AS uuid)"),
                {"id": diary_id},
            )
        ).one()
        assert row.kind == "diary_entry"
        assert row.visibility == "private"


@pytest.mark.asyncio
async def test_diary_write_dedupes(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")

    body = "Same diary line, repeated."
    first = await client.post(
        "/v1/diary",
        json={"entry": body, "principal": "admin"},
    )
    assert first.status_code == 201
    second = await client.post(
        "/v1/diary",
        json={"entry": body, "principal": "admin"},
    )
    assert second.status_code == 201
    assert second.json()["status"] == "deduped"
    assert second.json()["id"] == first.json()["id"]


@pytest.mark.asyncio
async def test_diary_read_returns_owner_entries(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")

    for body in ["alpha", "beta", "gamma"]:
        resp = await client.post(
            "/v1/diary",
            json={"entry": body, "principal": "admin"},
        )
        assert resp.status_code == 201

    read_resp = await client.get("/v1/diary/admin", params={"limit": 10})
    assert read_resp.status_code == 200
    entries = read_resp.json()
    assert len(entries) >= 3
    assert all(e["content"] in {"alpha", "beta", "gamma"} for e in entries)
    # Newest first.
    assert entries[0]["content"] == "gamma"


@pytest.mark.asyncio
async def test_diary_read_unknown_principal_returns_422(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")

    resp = await client.get("/v1/diary/no-such-principal-xyz")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_diary_write_blocks_secrets(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")

    resp = await client.post(
        "/v1/diary",
        json={"entry": "My AWS key is AKIAIOSFODNN7EXAMPLE", "principal": "admin"},
    )
    assert resp.status_code == 422
