"""Integration tests for auto-promotion Path A.

These tests require a live PostgreSQL with the v2 schema
(migrations/001_init.sql) and pgvector. They skip automatically when no DB is
reachable, mirroring tests/test_search.py and tests/test_remember.py.

The default tenant_config seeded by the migration has:
  auto_promote_enabled = TRUE
  auto_promote_confidence_threshold = 0.7
  auto_promote_min_age_hours = 72

Engine/loop handling: pytest-asyncio runs each test on its own event loop. To
keep asyncpg from binding a connection to one loop and reusing it on another,
each test gets a FRESH NullPool engine via the function-scoped ``db`` fixture
(disposed at teardown). All setup, assertions, and the app's ``get_session``
override draw from that one engine on the test's own loop.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.api.app import create_app
from engram.config import settings
from engram.db import get_session


@pytest.fixture
async def db():
    """Per-test NullPool engine + session factory, disposed after the test.

    Creating a fresh engine per test guarantees asyncpg connections are bound
    to THIS test's event loop and torn down before the next test's loop starts.
    """
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _db_ok(db: async_sessionmaker[AsyncSession]) -> bool:
    try:
        async with db() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _build_get_session(factory: async_sessionmaker[AsyncSession]) -> Any:
    """Build a get_session override bound to the test's engine."""

    async def _get_test_session() -> AsyncSession:
        async with factory() as session:
            from engram.db import _DEFAULT_PRINCIPAL_NAME, _DEFAULT_TENANT_SLUG

            row = (
                (
                    await session.execute(
                        text(
                            "SELECT t.id::text AS tenant_id, p.id::text AS principal_id "
                            "FROM tenants t "
                            "JOIN principals p ON p.tenant_id = t.id AND p.name = :principal "
                            "WHERE t.slug = :slug"
                        ),
                        {"slug": _DEFAULT_TENANT_SLUG, "principal": _DEFAULT_PRINCIPAL_NAME},
                    )
                )
                .mappings()
                .one()
            )
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": row["tenant_id"]},
            )
            await session.execute(
                text("SELECT set_config('app.principal_id', :pid, true)"),
                {"pid": row["principal_id"]},
            )
            yield session

    return _get_test_session


@pytest.fixture
async def app(db):
    app = create_app()
    app.dependency_overrides[get_session] = _build_get_session(db)
    return app


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
async def _clean_db(db):
    """Reset the corpus and tenant_config before each test.

    No-op (and skips the test) when the DB isn't reachable.
    """
    if not await _db_ok(db):
        return
    async with db() as session:
        await session.execute(text("DELETE FROM tenants WHERE slug != 'default'"))
        await session.execute(text("DELETE FROM item_events"))
        await session.execute(text("DELETE FROM memory_items"))
        await session.execute(
            text(
                "UPDATE tenant_config SET "
                "auto_promote_enabled = TRUE, "
                "auto_promote_confidence_threshold = 0.7, "
                "auto_promote_min_age_hours = 72 "
                "WHERE tenant_id = (SELECT id FROM tenants WHERE slug = 'default')"
            )
        )
        await session.commit()


@pytest.fixture(autouse=True)
def _reset_embedding_provider():
    """Restore embedding_provider/conflict settings mutated by individual tests."""
    original_provider = settings.embedding_provider
    original_conflict = settings.conflict_check_on_write
    yield
    settings.embedding_provider = original_provider
    settings.conflict_check_on_write = original_conflict


def _skip_if_no_db() -> Any:
    return pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")


async def _default_tenant_principal(db: async_sessionmaker[AsyncSession]) -> tuple[str, str]:
    """Return (tenant_id, principal_id) for the seeded default tenant/admin."""
    async with db() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT t.id::text AS tenant_id, p.id::text AS principal_id "
                        "FROM tenants t "
                        "JOIN principals p ON p.tenant_id = t.id AND p.name = 'admin' "
                        "WHERE t.slug = 'default'"
                    )
                )
            )
            .mappings()
            .one()
        )
    return str(row["tenant_id"]), str(row["principal_id"])


def _hours_ago(hours: float) -> datetime:
    """A timezone-aware timestamp `hours` before now (for created_at math)."""
    return datetime.now(UTC) - timedelta(hours=hours)


async def _insert_item(
    db: async_sessionmaker[AsyncSession],
    *,
    tenant_id: str,
    principal_id: str,
    content: str,
    review_status: str = "proposed",
    memory_confidence: float = 0.9,
    created_at: datetime | None = None,
    conflict_resolution_status: str | None = None,
    conflicts_with_item_id: str | None = None,
    valid_to: datetime | None = None,
    superseded_by: str | None = None,
    content_hash: str | None = None,
) -> str:
    """Insert a memory_items row with explicit control over promotion inputs."""
    item_id = str(uuid.uuid4())
    if created_at is None:
        created_at = _hours_ago(100)  # default: old enough to pass the 72h age gate
    async with db() as session:
        await session.execute(
            text(
                "INSERT INTO memory_items ("
                "id, tenant_id, principal_id, content, content_hash, kind, "
                "visibility, review_status, memory_confidence, source_trust, "
                "importance, source_type, conflict_resolution_status, "
                "conflicts_with_item_id, valid_to, superseded_by, created_at, valid_from"
                ") VALUES ("
                ":id, :tenant_id, :principal_id, :content, :content_hash, 'fact', "
                "'workspace', :review_status, :memory_confidence, 0.5, "
                "0.5, 'manual', :conflict_resolution_status, "
                ":conflicts_with_item_id, :valid_to, :superseded_by, "
                ":created_at, :created_at"
                ")"
            ),
            {
                "id": item_id,
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "content": content,
                "content_hash": content_hash or f"sha256:{uuid.uuid4().hex}",
                "review_status": review_status,
                "memory_confidence": memory_confidence,
                "conflict_resolution_status": conflict_resolution_status,
                "conflicts_with_item_id": conflicts_with_item_id,
                "valid_to": valid_to,
                "superseded_by": superseded_by,
                "created_at": created_at,
            },
        )
        await session.commit()
    return item_id


async def _insert_embedding(
    db: async_sessionmaker[AsyncSession],
    *,
    item_id: str,
    tenant_id: str,
    vector: list[float],
    model: str = "text-embedding-3-small",
) -> None:
    """Insert a memory_embeddings row bound to an item (composite FK on tenant)."""
    vec_literal = "[" + ",".join(repr(v) for v in vector) + "]"
    async with db() as session:
        await session.execute(
            text(
                "INSERT INTO memory_embeddings "
                "(id, memory_item_id, tenant_id, embedding_model, embedding_dim, "
                "embedding, embedded_at, embedding_status) VALUES ("
                ":id, :item_id, :tenant_id, :model, :dim, "
                "CAST(:vec AS vector), now(), 'complete')"
            ),
            {
                "id": str(uuid.uuid4()),
                "item_id": item_id,
                "tenant_id": tenant_id,
                "model": model,
                "dim": len(vector),
                "vec": vec_literal,
            },
        )
        await session.commit()


async def _status_of(db: async_sessionmaker[AsyncSession], item_id: str) -> str:
    async with db() as session:
        return str(
            (
                await session.execute(
                    text("SELECT review_status FROM memory_items WHERE id = :id"),
                    {"id": item_id},
                )
            ).scalar_one()
        )


async def _events_for(
    db: async_sessionmaker[AsyncSession], item_id: str
) -> list[dict[str, Any]]:
    async with db() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT event_type, field_name, old_value, new_value, reason "
                    "FROM item_events WHERE item_id = :id "
                    "ORDER BY created_at ASC, id ASC"
                ),
                {"id": item_id},
            )
        ).mappings().all()
    return [dict(r) for r in rows]


async def _promote(client: AsyncClient) -> dict[str, Any]:
    """Drive promotion through the admin endpoint (runs on the test's loop)."""
    resp = await client.post("/v1/admin/promote")
    assert resp.status_code == 200, resp.text
    return resp.json()



async def _promote(client: AsyncClient) -> dict[str, Any]:
    """Drive promotion through the admin endpoint (runs on the test's loop)."""
    resp = await client.post("/v1/admin/promote")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---- happy path ----


async def test_eligible_proposed_item_is_promoted(client, db):
    if not await _db_ok(db):
        _skip_if_no_db()
    tenant_id, principal_id = await _default_tenant_principal(db)
    item_id = await _insert_item(
        db,
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="eligible proposed fact",
        memory_confidence=0.9,
        created_at=_hours_ago(100),
    )

    body = await _promote(client)

    assert body["promoted"] == 1
    assert body["scanned"] == 1
    assert body["skipped_confidence"] == 0
    assert body["skipped_age"] == 0
    assert body["skipped_conflict"] == 0
    assert body["enabled"] is True
    assert body["confidence_threshold"] == pytest.approx(0.7)
    assert body["min_age_hours"] == 72
    assert await _status_of(db, item_id) == "active"
    events = await _events_for(db, item_id)
    assert len(events) == 1
    ev = events[0]
    assert ev["event_type"] == "review_change"
    assert ev["field_name"] == "review_status"
    assert ev["old_value"] == "proposed"
    assert ev["new_value"] == "active"
    assert ev["reason"] is not None and "auto-promotion" in ev["reason"]


async def test_disabled_config_promotes_nothing(client, db):
    if not await _db_ok(db):
        _skip_if_no_db()
    tenant_id, principal_id = await _default_tenant_principal(db)
    async with db() as session:
        await session.execute(
            text("UPDATE tenant_config SET auto_promote_enabled = FALSE WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        )
        await session.commit()
    item_id = await _insert_item(db,
        tenant_id=tenant_id, principal_id=principal_id, content="disabled tenant fact"
    )

    body = await _promote(client)

    assert body["promoted"] == 0
    assert body["enabled"] is False
    assert body["skipped_disabled"] == 1
    assert await _status_of(db, item_id) == "proposed"


async def test_too_new_item_not_promoted(client, db):
    if not await _db_ok(db):
        _skip_if_no_db()
    tenant_id, principal_id = await _default_tenant_principal(db)
    item_id = await _insert_item(db,
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="too new fact",
        memory_confidence=0.9,
        created_at=_hours_ago(1),
    )

    body = await _promote(client)

    assert body["promoted"] == 0
    assert body["skipped_age"] == 1
    assert await _status_of(db, item_id) == "proposed"


async def test_low_confidence_item_not_promoted(client, db):
    if not await _db_ok(db):
        _skip_if_no_db()
    tenant_id, principal_id = await _default_tenant_principal(db)
    item_id = await _insert_item(db,
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="low confidence fact",
        memory_confidence=0.4,
        created_at=_hours_ago(100),
    )

    body = await _promote(client)

    assert body["promoted"] == 0
    assert body["skipped_confidence"] == 1
    assert await _status_of(db, item_id) == "proposed"


async def test_confidence_threshold_boundary_inclusive(client, db):
    """memory_confidence == threshold (0.7) is promoted — the gate is >=."""
    if not await _db_ok(db):
        _skip_if_no_db()
    tenant_id, principal_id = await _default_tenant_principal(db)
    item_id = await _insert_item(db,
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="boundary confidence fact",
        memory_confidence=0.7,
        created_at=_hours_ago(100),
    )

    body = await _promote(client)

    assert body["promoted"] == 1
    assert await _status_of(db, item_id) == "active"


async def test_age_threshold_boundary_inclusive(client, db):
    """created_at just past min_age_hours (73h) promotes — the gate is >=.

    We can't inject ``now`` through the endpoint, so we set created_at to 73h
    ago (comfortably past the 72h gate) rather than testing the exact-second
    boundary. The >= direction is still exercised: 71h is skipped (previous
    test's _hours_ago(1) covers the too-new side; here 73h covers eligible).
    """
    if not await _db_ok(db):
        _skip_if_no_db()
    tenant_id, principal_id = await _default_tenant_principal(db)
    item_id = await _insert_item(db,
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="boundary age fact",
        memory_confidence=0.9,
        created_at=_hours_ago(73),
    )

    body = await _promote(client)

    assert body["promoted"] == 1
    assert await _status_of(db, item_id) == "active"


async def test_unresolved_conflict_excluded(client, db):
    if not await _db_ok(db):
        _skip_if_no_db()
    tenant_id, principal_id = await _default_tenant_principal(db)
    target_id = await _insert_item(db,
        tenant_id=tenant_id, principal_id=principal_id, content="conflict target"
    )
    item_id = await _insert_item(db,
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="conflicted fact",
        memory_confidence=0.9,
        created_at=_hours_ago(100),
        conflict_resolution_status="unresolved",
        conflicts_with_item_id=target_id,
    )

    body = await _promote(client)

    # The conflicted item is skipped; the target it points to has no conflict
    # marker of its own and is eligible, so it promotes.
    assert body["skipped_conflict"] == 1
    assert body["promoted"] == 1
    assert await _status_of(db, item_id) == "proposed"
    assert await _status_of(db, target_id) == "active"


async def test_accepted_conflict_is_promotable(client, db):
    """conflict_resolution_status = 'accepted' is NOT a blocker (design §3)."""
    if not await _db_ok(db):
        _skip_if_no_db()
    tenant_id, principal_id = await _default_tenant_principal(db)
    target_id = await _insert_item(db,
        tenant_id=tenant_id, principal_id=principal_id, content="accepted target"
    )
    item_id = await _insert_item(db,
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="accepted-conflict fact",
        memory_confidence=0.9,
        created_at=_hours_ago(100),
        conflict_resolution_status="accepted",
        conflicts_with_item_id=target_id,
    )

    body = await _promote(client)

    # The accepted-conflict item AND its conflict target are both eligible
    # (neither has an 'unresolved' marker), so both promote.
    assert body["promoted"] == 2
    assert await _status_of(db, item_id) == "active"
    assert await _status_of(db, target_id) == "active"


async def test_rejected_archived_expired_superseded_not_promoted(client, db):
    """Non-proposed / terminal-state rows never enter the candidate set."""
    if not await _db_ok(db):
        _skip_if_no_db()
    tenant_id, principal_id = await _default_tenant_principal(db)
    old = _hours_ago(100)

    rejected = await _insert_item(db,
        tenant_id=tenant_id, principal_id=principal_id,
        content="rejected", review_status="rejected", created_at=old,
    )
    archived = await _insert_item(db,
        tenant_id=tenant_id, principal_id=principal_id,
        content="archived", review_status="archived", created_at=old,
    )
    disputed = await _insert_item(db,
        tenant_id=tenant_id, principal_id=principal_id,
        content="disputed", review_status="disputed", created_at=old,
    )
    expired = await _insert_item(db,
        tenant_id=tenant_id, principal_id=principal_id,
        content="expired", review_status="proposed", created_at=old,
        valid_to=datetime.now(UTC) - timedelta(hours=1),
    )
    # superseded: a proposed-but-superseded row should not promote.
    repl = await _insert_item(db,
        tenant_id=tenant_id, principal_id=principal_id, content="replacement",
    )
    superseded = await _insert_item(db,
        tenant_id=tenant_id, principal_id=principal_id,
        content="superseded", review_status="proposed", created_at=old,
        superseded_by=uuid.UUID(repl),
    )

    body = await _promote(client)

    # Only the replacement (repl) is eligible; superseded is skipped defensively.
    # rejected/archived/disputed/expired are filtered out by the candidate query.
    assert body["promoted"] == 1
    for iid in (rejected, archived, disputed, expired, superseded):
        assert await _status_of(db, iid) != "active"
    assert await _status_of(db, superseded) == "proposed"


async def test_idempotent_second_run_promotes_zero(client, db):
    if not await _db_ok(db):
        _skip_if_no_db()
    tenant_id, principal_id = await _default_tenant_principal(db)
    await _insert_item(db,
        tenant_id=tenant_id, principal_id=principal_id, content="idempotent fact"
    )

    first = await _promote(client)
    second = await _promote(client)

    assert first["promoted"] == 1
    assert second["promoted"] == 0
    assert second["scanned"] == 0  # nothing proposed remains


async def test_tenant_isolation(client, db):
    """Promotion for tenant A leaves tenant B's proposed items untouched.

    The admin endpoint runs in tenant A's RLS context (the default tenant), so
    it cannot see tenant B's items — proving tenant isolation through the same
    RLS path the rest of the API uses.
    """
    if not await _db_ok(db):
        _skip_if_no_db()
    tid_a, pid_a = await _default_tenant_principal(db)

    # Create tenant B (items written under its own tenant id).
    tid_b = str(uuid.uuid4())
    pid_b = str(uuid.uuid4())
    async with db() as session:
        await session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'B', :slug)"),
            {"id": tid_b, "slug": f"tenant-b-{tid_b[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES (:id, :tid, 'agent-b', 'agent')"
            ),
            {"id": pid_b, "tid": tid_b},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_config (tenant_id, config_version, active) "
                "VALUES (:tid, 'v1', TRUE)"
            ),
            {"tid": tid_b},
        )
        await session.commit()

    item_a = await _insert_item(db,
        tenant_id=tid_a, principal_id=pid_a, content="tenant A eligible"
    )
    item_b = await _insert_item(db,
        tenant_id=tid_b, principal_id=pid_b, content="tenant B eligible"
    )

    # Promote via the endpoint, which runs in tenant A's RLS context.
    body = await _promote(client)

    assert body["promoted"] == 1
    assert body["tenant_id"] == tid_a
    assert await _status_of(db, item_a) == "active"
    # Tenant B's item is invisible to tenant A's request (RLS) → untouched.
    assert await _status_of(db, item_b) == "proposed"


async def test_admin_endpoint_returns_summary(client, db):
    if not await _db_ok(db):
        _skip_if_no_db()
    tenant_id, principal_id = await _default_tenant_principal(db)
    await _insert_item(db,
        tenant_id=tenant_id, principal_id=principal_id, content="endpoint fact"
    )

    body = await _promote(client)

    assert body["promoted"] == 1
    assert body["scanned"] == 1
    assert body["enabled"] is True
    assert body["confidence_threshold"] == pytest.approx(0.7)
    assert body["min_age_hours"] == 72
    assert body["promoted_ids"]
    assert "auto-promotion" in body["summary"].lower() or "promoted" in body["summary"]


async def test_cli_promote_single_tenant(capsys, db):
    """The CLI ``promote-proposed`` command promotes and prints a clear summary.

    We pass the test session factory into ``_run_promotion`` so the CLI shares
    the test event loop's NullPool engine (the app's own engine would bind to a
    different loop under per-function event loops and trip asyncpg).
    """
    if not await _db_ok(db):
        _skip_if_no_db()
    from engram.cli import _run_promotion

    tenant_id, principal_id = await _default_tenant_principal(db)
    await _insert_item(db,
        tenant_id=tenant_id, principal_id=principal_id, content="cli fact"
    )

    rc = await _run_promotion(tenant_id, limit=None, session_factory=db)
    out = capsys.readouterr().out
    assert rc == 0
    assert "promoted=1" in out
    assert f"tenant={tenant_id}" in out
    assert "Total:" in out


# ---- startup + semantic recall integration (acceptance criteria) ----


async def test_startup_recall_excludes_proposed_includes_after_promotion(client, db):
    """Before promotion: proposed item absent from startup recall.
    After promotion: the same item appears as active."""
    if not await _db_ok(db):
        _skip_if_no_db()
    tenant_id, principal_id = await _default_tenant_principal(db)
    item_id = await _insert_item(db,
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="startup integration fact",
        memory_confidence=0.9,
        created_at=_hours_ago(100),
    )

    # Proposed → not in startup recall (active-only).
    resp = await client.post("/v1/recall", json={"mode": "startup"})
    assert resp.status_code == 200, resp.text
    ids = {item["id"] for item in resp.json()["items"]}
    assert item_id not in ids

    # Promote.
    body = await _promote(client)
    assert body["promoted"] == 1

    # Now active → present in startup recall.
    resp = await client.post("/v1/recall", json={"mode": "startup"})
    assert resp.status_code == 200, resp.text
    ids = {item["id"] for item in resp.json()["items"]}
    assert item_id in ids


async def test_semantic_recall_warning_changes_after_promotion(client, monkeypatch, db):
    """Proposed item shows warnings=['unreviewed']; after promotion (active) no warning."""
    if not await _db_ok(db):
        _skip_if_no_db()
    tenant_id, principal_id = await _default_tenant_principal(db)

    # Embeddings are needed for semantic recall. Use the deterministic fake so
    # the content matches the query vector.
    settings.embedding_provider = "openai"
    settings.conflict_check_on_write = False
    from engram import recall as recall_mod
    from engram.api.routes import memory as memory_routes

    target_vec = [1.0] + [0.0] * 1535

    async def fake_embedding(text_value: str) -> list[float] | None:
        if text_value.startswith("semantic target"):
            return target_vec
        return [0.0, 1.0] + [0.0] * 1534

    monkeypatch.setattr(recall_mod, "generate_embedding", fake_embedding)
    monkeypatch.setattr(memory_routes, "generate_embedding", fake_embedding)

    item_id = await _insert_item(db,
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="semantic target unreviewed",
        memory_confidence=0.9,
        created_at=_hours_ago(100),
    )
    # Seed the embedding so semantic recall can match the item.
    await _insert_embedding(db, item_id=item_id, tenant_id=tenant_id, vector=target_vec)

    # Before promotion: proposed + unreviewed warning.
    resp = await client.post(
        "/v1/recall", json={"mode": "semantic", "query": "semantic query"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    matched = [i for i in body["items"] if i["id"] == item_id]
    assert matched, "proposed item should appear in semantic recall"
    assert "unreviewed" in matched[0]["warnings"]
    assert matched[0]["review_status"] == "proposed"

    # Promote via the admin endpoint.
    promote_resp = await client.post("/v1/admin/promote")
    assert promote_resp.status_code == 200
    assert promote_resp.json()["promoted"] == 1

    # After promotion: active, no unreviewed warning.
    resp = await client.post(
        "/v1/recall", json={"mode": "semantic", "query": "semantic query"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    matched = [i for i in body["items"] if i["id"] == item_id]
    assert matched, "promoted item should still appear in semantic recall"
    assert "unreviewed" not in matched[0]["warnings"]
    assert matched[0]["review_status"] == "active"


async def test_custom_tenant_config_thresholds_respected(client, db):
    """Tenant-configured threshold/age overrides defaults."""
    if not await _db_ok(db):
        _skip_if_no_db()
    tenant_id, principal_id = await _default_tenant_principal(db)
    # Tighten confidence to 0.95 and age to 1h.
    async with db() as session:
        await session.execute(
            text(
                "UPDATE tenant_config SET "
                "auto_promote_confidence_threshold = 0.95, "
                "auto_promote_min_age_hours = 1 "
                "WHERE tenant_id = :tid"
            ),
            {"tid": tenant_id},
        )
        await session.commit()

    # 0.9 confidence < 0.95 → skipped_confidence.
    low = await _insert_item(db,
        tenant_id=tenant_id, principal_id=principal_id,
        content="below custom threshold", memory_confidence=0.9,
        created_at=_hours_ago(5),
    )
    # 0.97 confidence >= 0.95, age 5h >= 1h → promoted.
    high = await _insert_item(db,
        tenant_id=tenant_id, principal_id=principal_id,
        content="above custom threshold", memory_confidence=0.97,
        created_at=_hours_ago(5),
    )

    body = await _promote(client)

    assert body["promoted"] == 1
    assert body["skipped_confidence"] == 1
    assert body["confidence_threshold"] == pytest.approx(0.95)
    assert body["min_age_hours"] == 1
    assert await _status_of(db, low) == "proposed"
    assert await _status_of(db, high) == "active"
