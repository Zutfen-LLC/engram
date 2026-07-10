# ruff: noqa: E501
"""Tests for KG endpoints — add_triple, query, invalidate, timeline.

These tests require a live PostgreSQL with the v2 schema (migrations/001_init.sql).
They skip automatically when no DB is reachable, matching the pattern in
test_health.py. Run locally with ``docker compose up``.
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
        from sqlalchemy import text as sa_text

        from engram.db import _DEFAULT_PRINCIPAL_NAME, _DEFAULT_TENANT_SLUG

        row = (
            await session.execute(
                sa_text(
                    "SELECT t.id::text AS tenant_id, p.id::text AS principal_id "
                    "FROM tenants t "
                    "JOIN principals p ON p.tenant_id = t.id AND p.name = :principal "
                    "WHERE t.slug = :slug"
                ),
                {"slug": _DEFAULT_TENANT_SLUG, "principal": _DEFAULT_PRINCIPAL_NAME},
            )
        ).mappings().one()
        rls_tenant_id = row["tenant_id"]
        rls_principal_id = row["principal_id"]
        from engram.db import apply_rls_context

        await apply_rls_context(session, tenant_id=rls_tenant_id, principal_id=rls_principal_id)
        yield session


async def _seed_rls(session: AsyncSession) -> tuple[str, str]:
    """Set RLS context on an existing session, return (tenant_id, principal_id)."""
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
    tid = row["tenant_id"]
    pid = row["principal_id"]
    await session.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tid})
    await session.execute(text("SELECT set_config('app.principal_id', :pid, true)"), {"pid": pid})
    return tid, pid


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
        await conn.execute(text("DELETE FROM kg_triples"))
        await conn.execute(text("DELETE FROM memory_embeddings"))
        await conn.execute(text("DELETE FROM memory_items"))
        await conn.execute(text("DELETE FROM deletion_events"))
        await conn.execute(text("DELETE FROM item_events"))


async def _seed_test_item(session: AsyncSession, tenant_id: str | None = None) -> dict[str, str]:
    pid_row = await session.execute(text("SELECT current_setting('app.principal_id', true)"))
    principal_id = pid_row.scalar()
    ws_row = await session.execute(text("SELECT id FROM workspaces LIMIT 1"))
    workspace_id = ws_row.scalar()
    if tenant_id is None:
        tid_row = await session.execute(text("SELECT current_setting('app.tenant_id', true)"))
        tenant_id = tid_row.scalar()
    await session.execute(
        text(
            "INSERT INTO workspace_members (workspace_id, principal_id, role) "
            "VALUES (:ws, :pid, 'member') ON CONFLICT DO NOTHING"
        ),
        {"ws": workspace_id, "pid": principal_id},
    )
    item_id = str(uuid4())
    await session.execute(
        text("""
            INSERT INTO memory_items (
                id, tenant_id, workspace_id, principal_id, content, content_hash, kind,
                visibility, review_status, memory_confidence, source_trust, human_verified,
                importance, pinned, recall_count, startup_recall_count, source_type, sensitivity,
                valid_from, created_at
            ) VALUES (
                :id, CAST(:tid AS uuid), :ws, :pid,
                :content, :chash, :kind,
                :vis, :rs, :mc, :st, :hv,
                :imp, :pin, :rc, :src, :sotype, :sens,
                now(), now()
            )
        """),
        {
            "id": item_id, "tid": tenant_id, "ws": workspace_id, "pid": principal_id,
            "content": "test memory item", "chash": f"hash-{item_id}",
            "kind": "fact", "vis": "workspace", "rs": "active",
            "mc": 0.8, "st": 0.7, "hv": False, "imp": 0.5, "pin": False,
            "rc": 0, "src": 0, "sotype": "manual", "sens": "normal",
        },
    )
    return {"item_id": item_id, "workspace_id": str(workspace_id), "principal_id": str(principal_id)}


async def _seed_private_item(session: AsyncSession, tenant_id: str | None = None) -> dict[str, str]:
    if tenant_id is None:
        tid_row = await session.execute(text("SELECT current_setting('app.tenant_id', true)"))
        tenant_id = tid_row.scalar()
    other_principal_id = str(uuid4())
    name = f"OtherAgent-{uuid4().hex[:8]}"
    await session.execute(
        text("""
            INSERT INTO principals (id, tenant_id, name, type, created_at)
            VALUES (:id, CAST(:tid AS uuid), :name, :type, now())
        """),
        {"id": other_principal_id, "tid": tenant_id, "name": name, "type": "agent"},
    )
    ws_row = await session.execute(text("SELECT id FROM workspaces LIMIT 1"))
    workspace_id = ws_row.scalar()
    item_id = str(uuid4())
    await session.execute(
        text("""
            INSERT INTO memory_items (
                id, tenant_id, workspace_id, principal_id, content, content_hash, kind,
                visibility, review_status, memory_confidence, source_trust, human_verified,
                importance, pinned, recall_count, startup_recall_count, source_type, sensitivity,
                valid_from, created_at
            ) VALUES (
                :id, CAST(:tid AS uuid), :ws, :pid,
                :content, :chash, :kind,
                :vis, :rs, :mc, :st, :hv,
                :imp, :pin, :rc, :src, :sotype, :sens,
                now(), now()
            )
        """),
        {
            "id": item_id, "tid": tenant_id, "ws": workspace_id, "pid": other_principal_id,
            "content": "private item", "chash": f"hash-{item_id}",
            "kind": "fact", "vis": "private", "rs": "active",
            "mc": 0.8, "st": 0.7, "hv": False, "imp": 0.5, "pin": False,
            "rc": 0, "src": 0, "sotype": "manual", "sens": "normal",
        },
    )
    return {"item_id": item_id, "other_principal_id": other_principal_id}


async def _insert_triple(
    session: AsyncSession,
    *,
    ws_id: str,
    pid: str,
    subject: str,
    predicate: str,
    object: str,
    source_item_id: str | None = None,
    valid_from: str = "now()",
    valid_to: str | None = None,
    confidence: float = 0.5,
    review_status: str = "proposed",
    tenant_id: str | None = None,
) -> None:
    vf = f"({valid_from})" if valid_from else "now()"
    vt = f"({valid_to})" if valid_to else "NULL"
    if tenant_id is None:
        tid_row = await session.execute(text("SELECT current_setting('app.tenant_id', true)"))
        tenant_id = tid_row.scalar()
    await session.execute(
        text(f"""
            INSERT INTO kg_triples (
                id, tenant_id, workspace_id, principal_id, subject, predicate, object,
                valid_from, valid_to, source_item_id, confidence, review_status, created_at
            ) VALUES (
                :id, CAST(:tid AS uuid), :ws, :pid,
                :subj, :pred, :obj, {vf}, {vt}, :sid, :conf, :rs, now()
            )
        """),
        {
            "id": str(uuid4()), "tid": tenant_id, "ws": ws_id, "pid": pid,
            "subj": subject, "pred": predicate, "obj": object,
            "sid": source_item_id, "conf": confidence, "rs": review_status,
        },
    )


# ---- Tests ----


@pytest.mark.asyncio
async def test_kg_add_triple_with_source_item(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _session_factory() as session:
        await _seed_rls(session)
        ids = await _seed_test_item(session)
        await session.commit()

    response = await client.post(
        "/v1/kg",
        json={
            "subject": "alice", "predicate": "knows", "object": "bob",
            "source_item_id": ids["item_id"], "confidence": 0.9,
        },
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["triple"]["subject"] == "alice"
    assert payload["triple"]["predicate"] == "knows"
    assert payload["triple"]["object"] == "bob"
    assert payload["triple"]["source_item_id"] == ids["item_id"]
    assert payload["triple"]["confidence"] == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_kg_add_triple_without_source_item_creates_backing_item(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")

    response = await client.post(
        "/v1/kg",
        json={"subject": "alice", "predicate": "works_at", "object": "acme"},
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["triple"]["subject"] == "alice"
    assert payload["source_item_id"] is not None
    assert payload["memory_item"] is not None
    assert payload["memory_item"]["kind"] == "fact"
    assert payload["memory_item"]["review_status"] == "proposed"


@pytest.mark.asyncio
async def test_kg_add_triple_nonexistent_source_item(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")

    response = await client.post(
        "/v1/kg",
        json={
            "subject": "alice", "predicate": "knows", "object": "bob",
            "source_item_id": str(uuid4()),
        },
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_kg_query_outbound(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _session_factory() as session:
        await _seed_rls(session)
        ids = await _seed_test_item(session)
        for subj, pred, obj in [
            ("alice", "knows", "bob"),
            ("alice", "works_at", "acme"),
            ("bob", "knows", "alice"),
        ]:
            await _insert_triple(
                session, ws_id=ids["workspace_id"], pid=ids["principal_id"],
                subject=subj, predicate=pred, object=obj,
                source_item_id=ids["item_id"],
            )
        await session.commit()

    response = await client.get("/v1/kg/query", params={"entity": "alice", "direction": "outbound"})
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 2
    assert all(t["subject"] == "alice" for t in payload)


@pytest.mark.asyncio
async def test_kg_query_inbound(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _session_factory() as session:
        await _seed_rls(session)
        ids = await _seed_test_item(session)
        await _insert_triple(
            session, ws_id=ids["workspace_id"], pid=ids["principal_id"],
            subject="bob", predicate="knows", object="alice",
            source_item_id=ids["item_id"],
        )
        await session.commit()

    response = await client.get("/v1/kg/query", params={"entity": "alice", "direction": "inbound"})
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["object"] == "alice"


@pytest.mark.asyncio
async def test_kg_query_predicate_filter(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _session_factory() as session:
        await _seed_rls(session)
        ids = await _seed_test_item(session)
        for subj, pred, obj in [
            ("alice", "knows", "bob"),
            ("alice", "works_at", "acme"),
        ]:
            await _insert_triple(
                session, ws_id=ids["workspace_id"], pid=ids["principal_id"],
                subject=subj, predicate=pred, object=obj,
                source_item_id=ids["item_id"],
            )
        await session.commit()

    response = await client.get(
        "/v1/kg/query", params={"entity": "alice", "predicate": "works_at"}
    )
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["predicate"] == "works_at"


@pytest.mark.asyncio
async def test_kg_query_visibility_private_hides_from_others(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _session_factory() as session:
        await _seed_rls(session)
        ids = await _seed_test_item(session)
        private = await _seed_private_item(session)
        await _insert_triple(
            session, ws_id=ids["workspace_id"], pid=ids["principal_id"],
            subject="alice", predicate="knows", object="bob",
            source_item_id=private["item_id"],
        )
        await session.commit()

    response = await client.get("/v1/kg/query", params={"entity": "alice"})
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 0


@pytest.mark.asyncio
async def test_kg_invalidate(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _session_factory() as session:
        await _seed_rls(session)
        ids = await _seed_test_item(session)
        await _insert_triple(
            session, ws_id=ids["workspace_id"], pid=ids["principal_id"],
            subject="alice", predicate="knows", object="bob",
            source_item_id=ids["item_id"],
        )
        await session.commit()

    response = await client.post(
        "/v1/kg/invalidate",
        json={"subject": "alice", "predicate": "knows", "object": "bob"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "invalidated"
    assert payload["count"] == 1

    response = await client.get("/v1/kg/query", params={"entity": "alice"})
    assert response.status_code == 200
    assert len(response.json()) == 0


@pytest.mark.asyncio
async def test_kg_invalidate_not_found(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")

    response = await client.post(
        "/v1/kg/invalidate",
        json={"subject": "alice", "predicate": "knows", "object": "bob"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "not_found"


@pytest.mark.asyncio
async def test_kg_timeline(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _session_factory() as session:
        await _seed_rls(session)
        ids = await _seed_test_item(session)
        for subj, pred, obj, offset in [
            ("alice", "born", "1990", "now() - interval '3 minutes'"),
            ("alice", "knows", "bob", "now() - interval '2 minutes'"),
            ("alice", "works_at", "acme", "now() - interval '1 minute'"),
        ]:
            await _insert_triple(
                session, ws_id=ids["workspace_id"], pid=ids["principal_id"],
                subject=subj, predicate=pred, object=obj,
                source_item_id=ids["item_id"],
                valid_from=offset,
            )
        await session.commit()

    response = await client.get("/v1/kg/timeline", params={"entity": "alice"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    timestamps = [t["valid_from"] for t in payload["facts"]]
    assert timestamps == sorted(timestamps)


@pytest.mark.asyncio
async def test_kg_timeline_all(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _session_factory() as session:
        await _seed_rls(session)
        ids = await _seed_test_item(session)
        for subj, pred, obj in [
            ("alice", "knows", "bob"),
            ("carol", "knows", "dave"),
        ]:
            await _insert_triple(
                session, ws_id=ids["workspace_id"], pid=ids["principal_id"],
                subject=subj, predicate=pred, object=obj,
                source_item_id=ids["item_id"],
            )
        await session.commit()

    response = await client.get("/v1/kg/timeline")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2


@pytest.mark.asyncio
async def test_kg_proposed_triple_has_trust_annotation(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _session_factory() as session:
        await _seed_rls(session)
        ids = await _seed_test_item(session)
        await _insert_triple(
            session, ws_id=ids["workspace_id"], pid=ids["principal_id"],
            subject="alice", predicate="knows", object="bob",
            source_item_id=ids["item_id"],
        )
        await session.commit()

    response = await client.get("/v1/kg/query", params={"entity": "alice"})
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["trust_annotation"] == "proposed"


@pytest.mark.asyncio
async def test_kg_query_as_of(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _session_factory() as session:
        await _seed_rls(session)
        ids = await _seed_test_item(session)
        await _insert_triple(
            session, ws_id=ids["workspace_id"], pid=ids["principal_id"],
            subject="alice", predicate="knows", object="bob",
            source_item_id=ids["item_id"],
            valid_from="now() - interval '1 hour'",
            valid_to="now() - interval '30 minutes'",
        )
        await session.commit()

    response = await client.get(
        "/v1/kg/query", params={"entity": "alice", "as_of": "now() - interval '45 minutes'"}
    )
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1

    response = await client.get(
        "/v1/kg/query", params={"entity": "alice", "as_of": "now() - interval '15 minutes'"}
    )
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 0
