# ruff: noqa: E501
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from engram.api.app import create_app
from engram.auth import Principal, get_current_principal
from engram.db import get_session


# The mutation routes under test (PATCH/supersede/invalidate/review/verify)
# resolve the item_events actor from the RLS session context
# (current_setting('app.principal_id', ...)) rather than trusting a
# caller-supplied actor field (V2-BL-001) — the same mechanism
# tests/test_hygiene.py already emulates on SQLite via
# current_setting/set_config functions registered on connect.
def _register_pg_funcs(dbapi_conn, _record):
    store: dict[str, str] = {}

    def _current_setting(name, _local=None):
        return store.get(name)

    def _set_config(name, value, _local=False):
        store[name] = value
        return value

    dbapi_conn.create_function("current_setting", 2, _current_setting)
    dbapi_conn.create_function("set_config", 3, _set_config)


# Populated by _seed_base() before any request is made in a test, and read by
# the client fixture's session override at request time — lets each test seed
# its own dynamic tenant/principal ids (unlike test_hygiene.py's fixed
# module-level ids) while still giving the RLS-context routes something to
# resolve as the authenticated caller.
_rls_context: dict[str, str] = {}

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
        event_type TEXT NOT NULL,
        field_name TEXT,
        old_value TEXT,
        new_value TEXT,
        actor_principal_id TEXT,
        reason TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE kg_triples (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        workspace_id TEXT,
        principal_id TEXT,
        subject TEXT NOT NULL,
        predicate TEXT NOT NULL,
        object TEXT NOT NULL,
        valid_from TEXT NOT NULL,
        valid_to TEXT,
        source_item_id TEXT,
        confidence REAL NOT NULL,
        review_status TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
]


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


@pytest.fixture(autouse=True)
def _reset_rls_context():
    _rls_context.clear()
    yield
    _rls_context.clear()


@pytest.fixture()
async def client(session_factory):
    app = create_app()

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            if _rls_context:
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :tid, true)"),
                    {"tid": _rls_context["tenant_id"]},
                )
                await session.execute(
                    text("SELECT set_config('app.principal_id', :pid, true)"),
                    {"pid": _rls_context["principal_id"]},
                )
            yield session

    async def override_get_current_principal() -> Principal:
        # RLS identity here is driven by _rls_context/set_config(), not by
        # get_current_principal — this override only keeps V2-BL-004's
        # ScopeGuard dependencies from hitting the real (unreachable-in-this-
        # test) Postgres-backed default principal.
        return Principal(tenant_id="test-tenant", principal_id="test-principal", scopes=("admin",))

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_current_principal] = override_get_current_principal
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _seed_base(session: AsyncSession, *, created_at: str) -> dict[str, str]:
    tenant_id = str(uuid4())
    workspace_id = str(uuid4())
    principal_id = str(uuid4())
    _rls_context["tenant_id"] = tenant_id
    _rls_context["principal_id"] = principal_id
    await session.execute(
        text("INSERT INTO tenants (id, name, slug, created_at) VALUES (:id, :name, :slug, :created_at)"),
        {"id": tenant_id, "name": "Tenant", "slug": "tenant", "created_at": created_at},
    )
    await session.execute(
        text(
            "INSERT INTO workspaces (id, tenant_id, name, slug, created_at) "
            "VALUES (:id, :tenant_id, :name, :slug, :created_at)"
        ),
        {
            "id": workspace_id,
            "tenant_id": tenant_id,
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
            "id": principal_id,
            "tenant_id": tenant_id,
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
        {"workspace_id": workspace_id, "principal_id": principal_id},
    )
    return {"tenant_id": tenant_id, "workspace_id": workspace_id, "principal_id": principal_id}


async def _insert_item(
    session: AsyncSession,
    *,
    tenant_id: str,
    workspace_id: str,
    principal_id: str,
    item_id: str,
    content: str,
    kind: str = "fact",
    wing: str | None = None,
    room: str | None = None,
    review_status: str = "active",
    valid_to: str | None = None,
    superseded_by: str | None = None,
    created_at: str,
    source_type: str = "manual",
    importance: float = 0.5,
    pinned: bool = False,
    human_verified: bool = False,
) -> None:
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
            "tenant_id": tenant_id,
            "workspace_id": workspace_id,
            "principal_id": principal_id,
            "content": content,
            "content_hash": f"hash-{item_id}",
            "kind": kind,
            "wing": wing,
            "room": room,
            "subject_type": None,
            "subject_id": None,
            "subject_name": None,
            "visibility": "workspace",
            "review_status": review_status,
            "memory_confidence": 0.8,
            "source_trust": 0.7,
            "human_verified": human_verified,
            "verified_by": None,
            "verified_at": None,
            "review_notes": None,
            "importance": importance,
            "pinned": pinned,
            "last_recalled_at": None,
            "recall_count": 0,
            "startup_recall_count": 0,
            "last_verified_at": None,
            "source_type": source_type,
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
            "valid_from": created_at,
            "valid_to": valid_to,
            "superseded_by": superseded_by,
            "created_at": created_at,
        },
    )


# NOTE: GET /v1/items and GET /v1/items/{item_id} now resolve tenant/principal
# from RLS context (`current_setting('app.tenant_id'/'app.principal_id')`) to
# enforce read eligibility (engram.memory_access) — a function this SQLite
# fixture doesn't provide. Their pagination/filter/detail/404 coverage lives in
# tests/test_item_read_eligibility.py (Postgres-backed) alongside the broader
# eligibility test matrix. The mutation endpoints below don't call that
# resolver, so they're unaffected and stay on this lightweight SQLite fixture.


@pytest.mark.asyncio
async def test_patch_supersede_review_verify_and_404s(client, session_factory):
    async with session_factory() as session:
        base = await _seed_base(session, created_at="2026-01-01T00:00:00+00:00")
        item_id = str(uuid4())
        await _insert_item(
            session,
            tenant_id=base["tenant_id"],
            workspace_id=base["workspace_id"],
            principal_id=base["principal_id"],
            item_id=item_id,
            content="patch me",
            wing="old",
            room="old-room",
            created_at="2026-01-01T01:00:00+00:00",
        )
        await session.commit()

    patch_response = await client.patch(
        f"/v1/items/{item_id}",
        json={
            "wing": "new",
            "room": "new-room",
            "visibility": "private",
            "importance": 0.8,
            "pinned": True,
            "reason": "retag",
            "actor_principal_id": base["principal_id"],
        },
    )
    assert patch_response.status_code == 200
    patch_payload = patch_response.json()
    assert patch_payload["item"]["wing"] == "new"
    assert patch_payload["event"]["field_name"] == "wing"
    assert len(patch_payload["events"]) == 5

    # NOTE: the supersede *happy path* (expire + insert + linkage) is NOT
    # exercised here. It depends on the real idx_memitems_dedup partial unique
    # index (migrations/001_init.sql), which this hand-rolled SQLite schema does
    # not enforce — so a SQLite run would pass spuriously even with the F6
    # ordering bug present. Full supersede invariant coverage (unique-index
    # safety, atomic rollback, dedup interaction, provenance linkage, authority,
    # cross-tenant RLS) lives in tests/test_supersede.py against real Postgres.
    # Only the supersede 404 case (which returns before any constraint) is
    # covered below.

    review_id = str(uuid4())
    verify_id = str(uuid4())
    async with session_factory() as session:
        await _insert_item(
            session,
            tenant_id=base["tenant_id"],
            workspace_id=base["workspace_id"],
            principal_id=base["principal_id"],
            item_id=review_id,
            content="review me",
            review_status="proposed",
            created_at="2026-01-01T02:00:00+00:00",
        )
        await _insert_item(
            session,
            tenant_id=base["tenant_id"],
            workspace_id=base["workspace_id"],
            principal_id=base["principal_id"],
            item_id=verify_id,
            content="verify me",
            created_at="2026-01-01T03:00:00+00:00",
        )
        await session.commit()

    review_response = await client.post(
        f"/v1/items/{review_id}/review",
        json={
            "review_status": "active",
            "reason": "approved",
            "review_notes": "good",
            "actor_principal_id": base["principal_id"],
        },
    )
    assert review_response.status_code == 200
    review_payload = review_response.json()
    assert review_payload["item"]["review_status"] == "active"
    assert review_payload["event"]["event_type"] == "review_change"

    verify_response = await client.post(
        f"/v1/items/{verify_id}/verify",
        json={"verified_by": base["principal_id"], "reason": "checked"},
    )
    assert verify_response.status_code == 200
    verify_payload = verify_response.json()
    assert verify_payload["item"]["human_verified"] in (1, True)
    assert verify_payload["item"]["verified_by"] == base["principal_id"]
    assert verify_payload["event"]["event_type"] == "verify"

    missing = str(uuid4())
    # GET is excluded here — it now requires RLS tenant/principal context
    # (engram.memory_access), covered in test_item_read_eligibility.py instead.
    for method, path, body in [
        ("patch", f"/v1/items/{missing}", {"wing": "x"}),
        ("post", f"/v1/items/{missing}/supersede", {}),
        ("post", f"/v1/items/{missing}/invalidate", {}),
        ("post", f"/v1/items/{missing}/review", {"review_status": "active"}),
        ("post", f"/v1/items/{missing}/verify", {}),
    ]:
        response = await getattr(client, method)(path, json=body) if body is not None else await getattr(client, method)(path)
        assert response.status_code == 404
