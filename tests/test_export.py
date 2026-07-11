# ruff: noqa: E501
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from engram.api.app import create_app
from engram.auth import Principal, get_current_principal
from engram.canonicalize import canonicalize
from engram.canonicalize import content_hash as compute_hash
from engram.db import get_session

CREATE_STATEMENTS = [
    "CREATE TABLE tenants (id TEXT PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE workspaces (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, name TEXT NOT NULL, slug TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE principals (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, name TEXT NOT NULL, type TEXT NOT NULL, internal_key TEXT, created_at TEXT NOT NULL)",
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
        authority INTEGER NOT NULL DEFAULT 10,
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

    async def override_get_current_principal() -> Principal:
        # This SQLite harness drives tenant/principal identity via its own
        # set_config() calls inside override_get_session (RLS emulation), not
        # via get_current_principal — so this override exists solely to keep
        # V2-BL-004's ScopeGuard dependencies from resolving the real
        # (unreachable-in-this-test) Postgres-backed default principal.
        return Principal(tenant_id="test-tenant", principal_id="test-principal", scopes=("admin",))

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_current_principal] = override_get_current_principal
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _seed_base(session: AsyncSession) -> dict[str, str]:
    tenant_id = str(uuid4())
    workspace_id = str(uuid4())
    principal_id = str(uuid4())
    created_at = "2026-01-01T00:00:00"
    await session.execute(
        text("INSERT INTO tenants (id, name, slug, created_at) VALUES (:id, :name, :slug, :created_at)"),
        {"id": tenant_id, "name": "Tenant", "slug": "tenant", "created_at": created_at},
    )
    await session.execute(
        text("INSERT INTO workspaces (id, tenant_id, name, slug, created_at) VALUES (:id, :tenant_id, :name, :slug, :created_at)"),
        {"id": workspace_id, "tenant_id": tenant_id, "name": "Alpha", "slug": "alpha", "created_at": created_at},
    )
    await session.execute(
        text("INSERT INTO principals (id, tenant_id, name, type, created_at) VALUES (:id, :tenant_id, :name, :type, :created_at)"),
        {"id": principal_id, "tenant_id": tenant_id, "name": "Agent", "type": "agent", "created_at": created_at},
    )
    return {"tenant_id": tenant_id, "workspace_id": workspace_id, "principal_id": principal_id}


_FULL_COLS = (
    "id, tenant_id, workspace_id, principal_id, content, content_hash, kind, wing, room, "
    "subject_type, subject_id, subject_name, visibility, review_status, memory_confidence, "
    "source_trust, human_verified, verified_by, verified_at, review_notes, importance, pinned, "
    "last_recalled_at, recall_count, startup_recall_count, last_verified_at, source_type, "
    "source_session, source_uri, extracted_by_model, extraction_confidence, "
    "conflicts_with_item_id, conflict_type, conflict_resolution_status, conflict_resolved_by, "
    "conflict_resolved_at, sensitivity, external_id, external_source, valid_from, valid_to, "
    "superseded_by, created_at"
)
_FULL_PH = (
    ":id, :tenant_id, :workspace_id, :principal_id, :content, :content_hash, :kind, :wing, :room, "
    ":subject_type, :subject_id, :subject_name, :visibility, :review_status, :memory_confidence, "
    ":source_trust, :human_verified, :verified_by, :verified_at, :review_notes, :importance, :pinned, "
    ":last_recalled_at, :recall_count, :startup_recall_count, :last_verified_at, :source_type, "
    ":source_session, :source_uri, :extracted_by_model, :extraction_confidence, "
    ":conflicts_with_item_id, :conflict_type, :conflict_resolution_status, :conflict_resolved_by, "
    ":conflict_resolved_at, :sensitivity, :external_id, :external_source, :valid_from, :valid_to, "
    ":superseded_by, :created_at"
)


async def _insert_item(
    session: AsyncSession,
    *,
    ids: dict[str, str],
    item_id: str,
    content: str,
    kind: str,
    valid_to: str | None = None,
    review_status: str = "active",
    created_at: str = "2026-01-01T00:00:00",
    content_hash: str | None = None,
    source_type: str = "manual",
    human_verified: bool = False,
    memory_confidence: float = 0.8,
    source_trust: float = 0.7,
    wing: str | None = None,
    room: str | None = None,
) -> None:
    await session.execute(
        text(f"INSERT INTO memory_items ({_FULL_COLS}) VALUES ({_FULL_PH})"),
        {
            "id": item_id,
            "tenant_id": ids["tenant_id"],
            "workspace_id": ids["workspace_id"],
            "principal_id": ids["principal_id"],
            "content": content,
            "content_hash": content_hash or compute_hash(canonicalize(content)),
            "kind": kind,
            "wing": wing,
            "room": room,
            "subject_type": None,
            "subject_id": None,
            "subject_name": None,
            "visibility": "workspace",
            "review_status": review_status,
            "memory_confidence": memory_confidence,
            "source_trust": source_trust,
            "human_verified": human_verified,
            "verified_by": None,
            "verified_at": None,
            "review_notes": None,
            "importance": 0.5,
            "pinned": False,
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
            "superseded_by": None,
            "created_at": created_at,
        },
    )


@pytest.fixture()
async def seeded(session_factory):
    """Seed base rows + one item of each CCA kind + excluded items."""
    async with session_factory() as session:
        ids = await _seed_base(session)
        await _insert_item(session, ids=ids, item_id="d1", content="Doctrine one", kind="doctrine")
        await _insert_item(session, ids=ids, item_id="d2", content="Decision one", kind="decision")
        await _insert_item(session, ids=ids, item_id="d3", content="Invariant one", kind="invariant")
        await _insert_item(session, ids=ids, item_id="d4", content="Preference one", kind="preference")
        # Excluded kinds
        await _insert_item(session, ids=ids, item_id="f1", content="Fact one", kind="fact")
        await _insert_item(session, ids=ids, item_id="o1", content="Obs one", kind="observation")
        # Inactive (valid_to set) — should be excluded
        await _insert_item(
            session, ids=ids, item_id="x1", content="Old doctrine", kind="doctrine", valid_to="2026-02-01T00:00:00"
        )
        await session.commit()
    return session_factory


async def test_export_returns_cca_packet_format(seeded, client):
    """Export returns the cca_lite_memory_packet@v1 envelope with entries."""
    resp = await client.get("/v1/export/cca")
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "cca_lite_memory_packet@v1"
    assert "meta" in body
    assert "entries" in body
    assert isinstance(body["entries"], list)


async def test_export_active_only_filter(seeded, client):
    """Items with valid_to IS NULL are included; invalidated items excluded."""
    resp = await client.get("/v1/export/cca")
    entries = resp.json()["entries"]
    ids = {e["id"] for e in entries}
    assert "d1" in ids  # active doctrine
    assert "x1" not in ids  # invalidated (valid_to set)


async def test_export_kind_filter(seeded, client):
    """Only doctrine/decision/invariant/preference kinds are included."""
    resp = await client.get("/v1/export/cca")
    kinds = {e["kind"] for e in resp.json()["entries"]}
    assert kinds == {"doctrine", "decision", "invariant", "preference"}
    assert "fact" not in kinds
    assert "observation" not in kinds


async def test_export_trust_fields_present(seeded, client):
    """Each entry carries Engram trust fields."""
    resp = await client.get("/v1/export/cca")
    for entry in resp.json()["entries"]:
        assert "review_status" in entry
        assert "memory_confidence" in entry
        assert "source_trust" in entry
        assert "human_verified" in entry
        assert isinstance(entry["human_verified"], bool)


async def test_export_entry_has_cca_fields(seeded, client):
    """Entries carry the baseline CCA packet fields (id, kind, text, content_hash, ...)."""
    resp = await client.get("/v1/export/cca")
    entry = resp.json()["entries"][0]
    for field in ("id", "kind", "text", "source", "session_id", "captured_at", "canonical_text", "content_hash"):
        assert field in entry, f"missing field: {field}"


async def test_export_canonical_text_matches_hash(seeded, client):
    """canonical_text is the lowercased-collapsed content; content_hash present."""
    resp = await client.get("/v1/export/cca")
    entry = next(e for e in resp.json()["entries"] if e["id"] == "d1")
    assert entry["canonical_text"] == "doctrine one"
    assert entry["content_hash"].startswith("sha256:")


async def test_export_empty_when_no_items(session_factory, client):
    """Export works on an empty ledger (no seeded rows)."""
    resp = await client.get("/v1/export/cca")
    assert resp.status_code == 200
    body = resp.json()
    assert body["entries"] == []
    assert body["meta"]["entry_count"] == 0
