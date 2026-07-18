# ruff: noqa: E501
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from engram.api.app import create_app
from engram.auth import Principal, get_current_principal
from engram.db import get_session

CREATE_STATEMENTS = [
    """
    CREATE TABLE tenants (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        slug TEXT NOT NULL,
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
        internal_key TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE workspace_members (
        workspace_id TEXT NOT NULL,
        principal_id TEXT NOT NULL,
        role TEXT NOT NULL,
        PRIMARY KEY (workspace_id, principal_id)
    )
    """,
    """
    CREATE TABLE memory_items (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        workspace_id TEXT,
        principal_id TEXT NOT NULL,
        content TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        kind TEXT NOT NULL,
        wing TEXT,
        room TEXT,
        subject_type TEXT,
        subject_id TEXT,
        subject_name TEXT,
        visibility TEXT NOT NULL,
        review_status TEXT NOT NULL,
        memory_confidence REAL NOT NULL,
        source_trust REAL NOT NULL,
        human_verified INTEGER NOT NULL,
        verified_by TEXT,
        verified_at TEXT,
        review_notes TEXT,
        importance REAL NOT NULL,
        pinned INTEGER NOT NULL,
        last_recalled_at TEXT,
        recall_count INTEGER NOT NULL,
        startup_recall_count INTEGER NOT NULL,
        last_verified_at TEXT,
        source_type TEXT NOT NULL,
        source_session TEXT,
        source_uri TEXT,
        extracted_by_model TEXT,
        extraction_confidence REAL,
        conflicts_with_item_id TEXT,
        conflict_type TEXT,
        conflict_resolution_status TEXT,
        conflict_resolved_by TEXT,
        conflict_resolved_at TEXT,
        sensitivity TEXT NOT NULL,
        external_id TEXT,
        external_source TEXT,
        valid_from TEXT NOT NULL,
        valid_to TEXT,
        superseded_by TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE item_events (
        id TEXT PRIMARY KEY,
        item_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        api_key_id TEXT,
        memory_profile_id TEXT,
        memory_profile_revision_id TEXT,
        memory_context_version TEXT NOT NULL,
        event_type TEXT NOT NULL,
        field_name TEXT,
        old_value TEXT,
        new_value TEXT,
        actor_principal_id TEXT,
        reason TEXT,
        created_at TEXT NOT NULL
    )
    """,
]

TENANT_ID = str(uuid4())
WORKSPACE_ID = str(uuid4())
PRINCIPAL_ID = str(uuid4())


def _register_pg_funcs(dbapi_conn, _record):
    """Emulate PG's current_setting/set_config on SQLite so the real
    _resolve_tenant_id path runs against an in-memory per-connection store."""
    store: dict[str, str] = {}

    def _current_setting(name, _local=None):
        return store.get(name)

    def _set_config(name, value, _local=False):
        store[name] = value
        return value

    dbapi_conn.create_function("current_setting", 2, _current_setting)
    dbapi_conn.create_function("set_config", 3, _set_config)


@pytest.fixture()
async def session_factory(tmp_path: Path):
    db_path = tmp_path / "engram.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    event.listen(engine.sync_engine, "connect", _register_pg_funcs)
    async with engine.begin() as conn:
        for stmt in CREATE_STATEMENTS:
            await conn.exec_driver_sql(stmt)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield factory
    await engine.dispose()


@pytest.fixture()
async def client(session_factory):
    app = create_app()

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": TENANT_ID},
            )
            await session.execute(
                text("SELECT set_config('app.principal_id', :pid, true)"),
                {"pid": PRINCIPAL_ID},
            )
            yield session

    async def override_get_current_principal() -> Principal:
        # RLS identity here is driven by the set_config() calls above, not by
        # get_current_principal — this override only keeps V2-BL-004's
        # ScopeGuard dependencies from hitting the real (unreachable-in-this-
        # test) Postgres-backed default principal.
        return Principal(tenant_id=TENANT_ID, principal_id=PRINCIPAL_ID, scopes=("admin",))

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_current_principal] = override_get_current_principal
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _seed_base(session: AsyncSession, *, created_at: str) -> None:
    await session.execute(
        text(
            "INSERT INTO tenants (id, name, slug, created_at) VALUES (:id, :name, :slug, :created_at)"
        ),
        {"id": TENANT_ID, "name": "Tenant", "slug": "default", "created_at": created_at},
    )
    await session.execute(
        text(
            "INSERT INTO workspaces (id, tenant_id, name, slug, created_at) "
            "VALUES (:id, :tenant_id, :name, :slug, :created_at)"
        ),
        {
            "id": WORKSPACE_ID,
            "tenant_id": TENANT_ID,
            "name": "Alpha",
            "slug": "alpha",
            "created_at": created_at,
        },
    )
    await session.execute(
        text(
            "INSERT INTO principals (id, tenant_id, name, type, created_at) "
            "VALUES (:id, :tenant_id, :name, :type, :created_at)"
        ),
        {
            "id": PRINCIPAL_ID,
            "tenant_id": TENANT_ID,
            "name": "Agent",
            "type": "user",
            "created_at": created_at,
        },
    )
    await session.execute(
        text(
            "INSERT INTO workspace_members (workspace_id, principal_id, role) "
            "VALUES (:workspace_id, :principal_id, 'member')"
        ),
        {"workspace_id": WORKSPACE_ID, "principal_id": PRINCIPAL_ID},
    )


async def _insert_item(
    session: AsyncSession,
    *,
    item_id: str,
    content: str,
    kind: str = "fact",
    review_status: str = "active",
    memory_confidence: float = 0.5,
    importance: float = 0.5,
    valid_from: str | None = None,
    valid_to: str | None = None,
    last_recalled_at: str | None = None,
    superseded_by: str | None = None,
    created_at: str | None = None,
) -> None:
    created_at = created_at or datetime.now(UTC).isoformat()
    valid_from = valid_from or created_at
    await session.execute(
        text(
            "INSERT INTO memory_items ("
            "id, tenant_id, workspace_id, principal_id, content, content_hash, kind, wing, room, "
            "subject_type, subject_id, subject_name, visibility, review_status, memory_confidence, "
            "source_trust, human_verified, verified_by, verified_at, review_notes, importance, pinned, "
            "last_recalled_at, recall_count, startup_recall_count, last_verified_at, source_type, "
            "source_session, source_uri, extracted_by_model, extraction_confidence, "
            "conflicts_with_item_id, conflict_type, conflict_resolution_status, conflict_resolved_by, "
            "conflict_resolved_at, sensitivity, external_id, external_source, valid_from, valid_to, "
            "superseded_by, created_at"
            ") VALUES ("
            ":id, :tenant_id, :workspace_id, :principal_id, :content, :content_hash, :kind, :wing, :room, "
            ":subject_type, :subject_id, :subject_name, :visibility, :review_status, :memory_confidence, "
            ":source_trust, :human_verified, :verified_by, :verified_at, :review_notes, :importance, :pinned, "
            ":last_recalled_at, :recall_count, :startup_recall_count, :last_verified_at, :source_type, "
            ":source_session, :source_uri, :extracted_by_model, :extraction_confidence, "
            ":conflicts_with_item_id, :conflict_type, :conflict_resolution_status, :conflict_resolved_by, "
            ":conflict_resolved_at, :sensitivity, :external_id, :external_source, :valid_from, :valid_to, "
            ":superseded_by, :created_at"
            ")"
        ),
        {
            "id": item_id,
            "tenant_id": TENANT_ID,
            "workspace_id": WORKSPACE_ID,
            "principal_id": PRINCIPAL_ID,
            "content": content,
            "content_hash": f"hash-{item_id}",
            "kind": kind,
            "wing": None,
            "room": None,
            "subject_type": None,
            "subject_id": None,
            "subject_name": None,
            "visibility": "workspace",
            "review_status": review_status,
            "memory_confidence": memory_confidence,
            "source_trust": 0.7,
            "human_verified": 0,
            "verified_by": None,
            "verified_at": None,
            "review_notes": None,
            "importance": importance,
            "pinned": 0,
            "last_recalled_at": last_recalled_at,
            "recall_count": 0,
            "startup_recall_count": 0,
            "last_verified_at": None,
            "source_type": "manual",
            "source_session": None,
            "source_uri": None,
            "extracted_by_model": None,
            "extraction_confidence": None,
            "conflicts_with_item_id": None,
            "conflict_type": None,
            "conflict_resolution_status": None,
            "conflict_resolved_by": None,
            "conflict_resolved_at": None,
            "sensitivity": "normal",
            "external_id": None,
            "external_source": None,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "superseded_by": superseded_by,
            "created_at": created_at,
        },
    )


@pytest.mark.asyncio
async def test_stale_returns_active_items_not_recalled_in_n_days(client, session_factory):
    now = datetime.now(UTC)
    old = (now - timedelta(days=100)).isoformat()
    recent = (now - timedelta(days=10)).isoformat()
    async with session_factory() as session:
        await _seed_base(session, created_at=old)
        # stale: recalled long ago
        await _insert_item(
            session,
            item_id=str(uuid4()),
            content="recalled-old",
            review_status="active",
            last_recalled_at=old,
            valid_from=old,
            created_at=old,
        )
        # not stale: recalled recently
        await _insert_item(
            session,
            item_id=str(uuid4()),
            content="recalled-recent",
            review_status="active",
            last_recalled_at=recent,
            valid_from=old,
            created_at=old,
        )
        # stale: never recalled, old valid_from (NULL does not exempt)
        await _insert_item(
            session,
            item_id=str(uuid4()),
            content="never-recalled-old",
            review_status="active",
            last_recalled_at=None,
            valid_from=old,
            created_at=old,
        )
        # not stale: never recalled, recent valid_from
        await _insert_item(
            session,
            item_id=str(uuid4()),
            content="never-recalled-recent",
            review_status="active",
            last_recalled_at=None,
            valid_from=recent,
            created_at=recent,
        )
        # not stale: proposed (not active) even though recalled long ago
        await _insert_item(
            session,
            item_id=str(uuid4()),
            content="proposed-old",
            review_status="proposed",
            last_recalled_at=old,
            valid_from=old,
            created_at=old,
        )
        # not stale: invalidated (valid_to set)
        await _insert_item(
            session,
            item_id=str(uuid4()),
            content="invalidated",
            review_status="active",
            last_recalled_at=old,
            valid_from=old,
            valid_to=recent,
            created_at=old,
        )
        await session.commit()

    resp = await client.get("/v1/review/stale", params={"days": 90})
    assert resp.status_code == 200
    body = resp.json()
    assert body["days"] == 90
    assert body["total"] == 2
    contents = {item["content"] for item in body["items"]}
    assert contents == {"recalled-old", "never-recalled-old"}


@pytest.mark.asyncio
async def test_stale_days_window_and_limit(client, session_factory):
    now = datetime.now(UTC)
    base = (now - timedelta(days=50)).isoformat()
    fresh = (now - timedelta(days=5)).isoformat()
    async with session_factory() as session:
        await _seed_base(session, created_at=base)
        await _insert_item(
            session,
            item_id=str(uuid4()),
            content="stale-at-30",
            review_status="active",
            last_recalled_at=base,
            valid_from=base,
            created_at=base,
        )
        await _insert_item(
            session,
            item_id=str(uuid4()),
            content="fresh",
            review_status="active",
            last_recalled_at=fresh,
            valid_from=base,
            created_at=base,
        )
        await session.commit()

    # With a 30-day window, the 50-day-old item is stale; the fresh one is not.
    resp = await client.get("/v1/review/stale", params={"days": 30})
    assert resp.status_code == 200
    contents = {item["content"] for item in resp.json()["items"]}
    assert contents == {"stale-at-30"}

    # With a 100-day window, nothing is stale.
    resp = await client.get("/v1/review/stale", params={"days": 100})
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_bulk_archive_sets_status_and_writes_events(client, session_factory):
    created_at = datetime.now(UTC).isoformat()
    active_a = str(uuid4())
    active_b = str(uuid4())
    already_archived = str(uuid4())
    rejected = str(uuid4())
    unknown = str(uuid4())
    async with session_factory() as session:
        await _seed_base(session, created_at=created_at)
        for item_id, status in [
            (active_a, "active"),
            (active_b, "active"),
            (already_archived, "archived"),
            (rejected, "rejected"),
        ]:
            await _insert_item(
                session,
                item_id=item_id,
                content=f"item-{item_id[:8]}",
                review_status=status,
                created_at=created_at,
            )
        await session.commit()

    resp = await client.post(
        "/v1/items/bulk-archive",
        json={
            "item_ids": [active_a, active_b, already_archived, unknown],
            "reason": "hygiene cleanup",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["archived"]) == {active_a, active_b}
    assert body["archived_count"] == 2
    assert set(body["skipped"]) == {already_archived, unknown}
    assert body["skipped_count"] == 2

    async with session_factory() as session:
        for item_id in (active_a, active_b):
            status = (
                await session.execute(
                    text("SELECT review_status FROM memory_items WHERE id = :id"),
                    {"id": item_id},
                )
            ).scalar_one()
            assert status == "archived"
        event_count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM item_events "
                    "WHERE event_type = 'review_change' AND new_value = 'archived'"
                )
            )
        ).scalar_one()
        assert event_count == 2


@pytest.mark.asyncio
async def test_bulk_archive_empty_list_is_noop(client, session_factory):
    created_at = datetime.now(UTC).isoformat()
    async with session_factory() as session:
        await _seed_base(session, created_at=created_at)
        await session.commit()

    resp = await client.post("/v1/items/bulk-archive", json={"item_ids": []})
    assert resp.status_code == 200
    body = resp.json()
    assert body["archived"] == []
    assert body["archived_count"] == 0
    assert body["skipped"] == []


@pytest.mark.asyncio
async def test_review_stats_counts_by_status_kind_and_confidence(client, session_factory):
    created_at = datetime.now(UTC).isoformat()
    async with session_factory() as session:
        await _seed_base(session, created_at=created_at)
        await _insert_item(
            session,
            item_id=str(uuid4()),
            content="active-fact-high",
            kind="fact",
            review_status="active",
            memory_confidence=0.9,
            created_at=created_at,
        )
        await _insert_item(
            session,
            item_id=str(uuid4()),
            content="active-preference-medium",
            kind="preference",
            review_status="active",
            memory_confidence=0.5,
            created_at=created_at,
        )
        await _insert_item(
            session,
            item_id=str(uuid4()),
            content="proposed-fact-low",
            kind="fact",
            review_status="proposed",
            memory_confidence=0.2,
            created_at=created_at,
        )
        await _insert_item(
            session,
            item_id=str(uuid4()),
            content="archived-observation-high",
            kind="observation",
            review_status="archived",
            memory_confidence=0.95,
            created_at=created_at,
        )
        # Excluded: invalidated (valid_to set)
        await _insert_item(
            session,
            item_id=str(uuid4()),
            content="invalidated-active",
            kind="fact",
            review_status="active",
            memory_confidence=0.9,
            valid_to=created_at,
            created_at=created_at,
        )
        await session.commit()

    resp = await client.get("/v1/review/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["by_review_status"] == {"active": 2, "proposed": 1, "archived": 1}
    assert body["by_kind"] == {"fact": 2, "preference": 1, "observation": 1}
    assert body["by_confidence"] == {"low": 1, "medium": 1, "high": 2}
    assert body["total"] == 4
