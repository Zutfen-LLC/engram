# ruff: noqa: E501
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from engram.api.app import create_app
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
        created_at TEXT NOT NULL
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
            yield session

    app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _seed_base(session: AsyncSession, *, created_at: str) -> dict[str, str]:
    tenant_id = str(uuid4())
    workspace_id = str(uuid4())
    principal_id = str(uuid4())
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
            "type": "agent",
            "created_at": created_at,
        },
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


async def _insert_event(
    session: AsyncSession,
    *,
    item_id: str,
    event_type: str,
    field_name: str | None,
    old_value: str | None,
    new_value: str | None,
    actor_principal_id: str | None,
    reason: str | None,
    created_at: str,
) -> None:
    await session.execute(
        text(
            "INSERT INTO item_events (id, item_id, event_type, field_name, old_value, new_value, "
            "actor_principal_id, reason, created_at) VALUES ("
            ":id, :item_id, :event_type, :field_name, :old_value, :new_value, :actor_principal_id, "
            ":reason, :created_at)"
        ),
        {
            "id": str(uuid4()),
            "item_id": item_id,
            "event_type": event_type,
            "field_name": field_name,
            "old_value": old_value,
            "new_value": new_value,
            "actor_principal_id": actor_principal_id,
            "reason": reason,
            "created_at": created_at,
        },
    )


@pytest.mark.asyncio
async def test_items_cursor_pagination_and_filters(client, session_factory):
    async with session_factory() as session:
        base = await _seed_base(session, created_at="2026-01-01T00:00:00+00:00")
        beta_workspace_id = str(uuid4())
        await session.execute(
            text(
                "INSERT INTO workspaces (id, tenant_id, name, slug, created_at) "
                "VALUES (:id, :tenant_id, :name, :slug, :created_at)"
            ),
            {
                "id": beta_workspace_id,
                "tenant_id": base["tenant_id"],
                "name": "Beta",
                "slug": "beta",
                "created_at": "2026-01-01T00:00:00+00:00",
            },
        )
        await _insert_item(
            session,
            tenant_id=base["tenant_id"],
            workspace_id=base["workspace_id"],
            principal_id=base["principal_id"],
            item_id=str(uuid4()),
            content="older",
            wing="west",
            room="hall",
            created_at="2026-01-01T01:00:00+00:00",
        )
        await _insert_item(
            session,
            tenant_id=base["tenant_id"],
            workspace_id=base["workspace_id"],
            principal_id=base["principal_id"],
            item_id=str(uuid4()),
            content="middle",
            wing="east",
            room="lobby",
            created_at="2026-01-01T02:00:00+00:00",
        )
        await _insert_item(
            session,
            tenant_id=base["tenant_id"],
            workspace_id=base["workspace_id"],
            principal_id=base["principal_id"],
            item_id=str(uuid4()),
            content="newest",
            wing="east",
            room="lobby",
            importance=0.9,
            pinned=True,
            created_at="2026-01-01T03:00:00+00:00",
        )
        await _insert_item(
            session,
            tenant_id=base["tenant_id"],
            workspace_id=base["workspace_id"],
            principal_id=base["principal_id"],
            item_id=str(uuid4()),
            content="proposed",
            review_status="proposed",
            created_at="2026-01-01T04:00:00+00:00",
        )
        await _insert_item(
            session,
            tenant_id=base["tenant_id"],
            workspace_id=beta_workspace_id,
            principal_id=base["principal_id"],
            item_id=str(uuid4()),
            content="beta item",
            wing="east",
            room="lobby",
            review_status="proposed",
            created_at="2026-01-01T05:00:00+00:00",
        )
        await session.commit()

    first = await client.get("/v1/items", params={"limit": 2})
    assert first.status_code == 200
    first_payload = first.json()
    assert [item["content"] for item in first_payload["items"]] == ["newest", "middle"]
    assert first_payload["next_cursor"]

    async with session_factory() as session:
        later_base = await _seed_base(session, created_at='2026-01-01T00:00:00+00:00')
        await _insert_item(
            session,
            tenant_id=later_base['tenant_id'],
            workspace_id=later_base['workspace_id'],
            principal_id=later_base['principal_id'],
            item_id=str(uuid4()),
            content="inserted later",
            wing="east",
            room="lobby",
            created_at="2026-01-01T06:00:00+00:00",
        )
        await session.commit()

    second = await client.get("/v1/items", params={"limit": 2, "cursor": first_payload["next_cursor"]})
    assert second.status_code == 200
    assert [item["content"] for item in second.json()["items"]] == ["older"]

    filtered = await client.get(
        "/v1/items",
        params={"workspace": "alpha", "kind": "fact", "wing": "east", "room": "lobby", "limit": 10},
    )
    assert filtered.status_code == 200
    contents = [item["content"] for item in filtered.json()["items"]]
    assert "proposed" not in contents
    assert all(item['workspace_id'] != beta_workspace_id for item in filtered.json()['items'])
    assert all(item["wing"] == "east" and item["room"] == "lobby" for item in filtered.json()["items"])


@pytest.mark.asyncio
async def test_get_item_detail_includes_events_and_kg(client, session_factory):
    async with session_factory() as session:
        base = await _seed_base(session, created_at="2026-01-01T00:00:00+00:00")
        item_id = str(uuid4())
        await _insert_item(
            session,
            tenant_id=base["tenant_id"],
            workspace_id=base["workspace_id"],
            principal_id=base["principal_id"],
            item_id=item_id,
            content="detail",
            wing="north",
            room="shelf",
            created_at="2026-01-01T01:00:00+00:00",
        )
        await _insert_event(
            session,
            item_id=item_id,
            event_type="metadata_patch",
            field_name="wing",
            old_value="south",
            new_value="north",
            actor_principal_id=base["principal_id"],
            reason="retag",
            created_at="2026-01-01T01:30:00+00:00",
        )
        await session.execute(
            text(
                "INSERT INTO kg_triples (id, tenant_id, workspace_id, principal_id, subject, predicate, object, "
                "valid_from, valid_to, source_item_id, confidence, review_status, created_at) VALUES ("
                ":id, :tenant_id, :workspace_id, :principal_id, :subject, :predicate, :object, "
                ":valid_from, :valid_to, :source_item_id, :confidence, :review_status, :created_at)"
            ),
            {
                "id": str(uuid4()),
                "tenant_id": base["tenant_id"],
                "workspace_id": base["workspace_id"],
                "principal_id": base["principal_id"],
                "subject": "engram",
                "predicate": "relates_to",
                "object": "memory",
                "valid_from": "2026-01-01T02:00:00+00:00",
                "valid_to": None,
                "source_item_id": item_id,
                "confidence": 0.9,
                "review_status": "active",
                "created_at": "2026-01-01T02:00:00+00:00",
            },
        )
        await session.commit()

    response = await client.get(f"/v1/items/{item_id}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["item"]["content"] == "detail"
    assert len(payload["item_events"]) == 1
    assert len(payload["linked_kg_facts"]) == 1


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

    supersede_response = await client.post(
        f"/v1/items/{item_id}/supersede",
        json={"reason": "replace", "actor_principal_id": base["principal_id"]},
    )
    assert supersede_response.status_code == 200
    supersede_payload = supersede_response.json()
    assert UUID(supersede_payload["old_item"]["superseded_by"]) == UUID(supersede_payload["new_item"]["id"])
    assert supersede_payload["old_item"]["valid_to"] is not None

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
    for method, path, body in [
        ("get", f"/v1/items/{missing}", None),
        ("patch", f"/v1/items/{missing}", {"wing": "x"}),
        ("post", f"/v1/items/{missing}/supersede", {}),
        ("post", f"/v1/items/{missing}/invalidate", {}),
        ("post", f"/v1/items/{missing}/review", {"review_status": "active"}),
        ("post", f"/v1/items/{missing}/verify", {}),
    ]:
        response = await getattr(client, method)(path, json=body) if body is not None else await getattr(client, method)(path)
        assert response.status_code == 404
