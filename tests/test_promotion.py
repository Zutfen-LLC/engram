"""Integration tests for auto-promotion Path A.

These tests require a live PostgreSQL with the v2 schema
(migrations/001_init.sql) and pgvector. They skip automatically when no DB is
reachable, mirroring tests/test_search.py and tests/test_semantic_recall.py.

The default tenant_config seeded by the migration has:
  auto_promote_enabled = TRUE
  auto_promote_confidence_threshold = 0.7
  auto_promote_min_age_hours = 72

Tests insert proposed items directly via SQL with controlled created_at /
memory_confidence / conflict fields, then call the promotion service function
(``engram.promotion.auto_promote_proposed_memories``) and the admin endpoint
and assert on the resulting review_status + audit rows.
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
from engram.promotion import auto_promote_proposed_memories

# Module-global engine/factory, recreated per test by the ``_fresh_engine`` autouse
# fixture. pytest-asyncio runs each test on its own event loop; asyncpg binds a
# connection's protocol to the loop where it was created. If a single engine
# spans many tests (and thus many loops), a protocol bound to an earlier loop can
# be reused by a later test on a different loop → "Future attached to a different
# loop". Recreating the engine per test sidesteps this entirely.
_test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_test_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    _test_engine, class_=AsyncSession, expire_on_commit=False
)


@pytest.fixture(autouse=True)
async def _fresh_engine():
    """Give each test a brand-new NullPool engine on its own loop.

    Disposes the previous engine (closing any lingering asyncpg protocols) and
    builds a fresh one + session factory. Without this, later tests in this
    module hit cross-loop asyncpg errors once the shared engine has been used
    across several per-function event loops.
    """
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


async def _get_test_session() -> AsyncSession:
    async with _test_session_factory() as session:
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


@pytest.fixture
def app():
    # Override the route handler's session (get_session) AND the auth path's
    # session factory. get_current_principal (engram/auth.py) opens its own
    # session from engram.db.async_session_factory — the real app engine, which
    # uses a *connection pool* (pool_size=10). A pooled connection bound to one
    # test's event loop gets reused by a later test on a different loop →
    # asyncpg "Future attached to a different loop". Pointing both at the
    # per-test NullPool factory keeps every connection on the current test's loop.
    import engram.db as db_module

    app = create_app()
    app.dependency_overrides[get_session] = _get_test_session
    real_factory = db_module.async_session_factory
    db_module.async_session_factory = _test_session_factory
    app.state._engram_real_session_factory = real_factory  # type: ignore[attr-defined]
    return app


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    # Restore the real factory so other test files see the app engine.
    import engram.db as db_module

    db_module.async_session_factory = app.state._engram_real_session_factory


@pytest.fixture(autouse=True)
async def _clean_db():
    if not await _db_ok():
        return
    # Remove any tenants created by tests (e.g. tenant B in isolation test),
    # cascading to their items/config/principals. Keep the seeded default tenant.
    async with _test_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM tenants WHERE slug != 'default'")
        )
        await conn.execute(text("DELETE FROM item_events"))
        await conn.execute(text("DELETE FROM memory_items"))
    # Reset the default tenant's config to migration defaults between tests.
    async with _test_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE tenant_config SET "
                "auto_promote_enabled = TRUE, "
                "auto_promote_confidence_threshold = 0.7, "
                "auto_promote_min_age_hours = 72 "
                "WHERE tenant_id = (SELECT id FROM tenants WHERE slug = 'default')"
            )
        )


@pytest.fixture(autouse=True)
def _reset_embedding_provider():
    """Restore embedding_provider/conflict settings mutated by individual tests."""
    original_provider = settings.embedding_provider
    original_conflict = settings.conflict_check_on_write
    yield
    settings.embedding_provider = original_provider
    settings.conflict_check_on_write = original_conflict


async def _default_tenant_principal() -> tuple[str, str]:
    """Return (tenant_id, principal_id) for the seeded default tenant/admin."""
    async with _test_session_factory() as session:
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


def _default_now() -> datetime:
    """A fixed 'now' for deterministic age math (72h+1h past for eligible)."""
    return datetime.now(UTC).replace(microsecond=0)


async def _insert_item(
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
        # Default: old enough to pass the 72h age gate.
        created_at = _default_now() - timedelta(hours=100)
    async with _test_session_factory() as session:
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


async def _status_of(item_id: str) -> str:
    async with _test_session_factory() as session:
        return str(
            (
                await session.execute(
                    text("SELECT review_status FROM memory_items WHERE id = :id"),
                    {"id": item_id},
                )
            ).scalar_one()
        )


async def _events_for(item_id: str) -> list[dict[str, Any]]:
    async with _test_session_factory() as session:
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


# ---- happy path ----


async def test_eligible_proposed_item_is_promoted():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="eligible proposed fact",
        memory_confidence=0.9,
        created_at=_default_now() - timedelta(hours=100),
    )

    async with _test_session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
        )
        result = await auto_promote_proposed_memories(session)

    assert result.promoted == 1
    assert result.scanned == 1
    assert result.skipped_confidence == 0
    assert result.skipped_age == 0
    assert result.skipped_conflict == 0
    assert result.enabled is True
    assert await _status_of(item_id) == "active"
    events = await _events_for(item_id)
    assert len(events) == 1
    ev = events[0]
    assert ev["event_type"] == "review_change"
    assert ev["field_name"] == "review_status"
    assert ev["old_value"] == "proposed"
    assert ev["new_value"] == "active"
    assert ev["reason"] is not None and "auto-promotion" in ev["reason"]


async def test_disabled_config_promotes_nothing():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id, principal_id = await _default_tenant_principal()
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "UPDATE tenant_config SET auto_promote_enabled = FALSE "
                "WHERE tenant_id = :tid"
            ),
            {"tid": tenant_id},
        )
        await session.commit()
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="disabled tenant fact"
    )

    async with _test_session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
        )
        result = await auto_promote_proposed_memories(session)

    assert result.promoted == 0
    assert result.enabled is False
    assert result.skipped_disabled == 1
    assert await _status_of(item_id) == "proposed"


async def test_too_new_item_not_promoted():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="too new fact",
        memory_confidence=0.9,
        created_at=_default_now() - timedelta(hours=1),
    )

    async with _test_session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
        )
        result = await auto_promote_proposed_memories(session)

    assert result.promoted == 0
    assert result.skipped_age == 1
    assert await _status_of(item_id) == "proposed"


async def test_low_confidence_item_not_promoted():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="low confidence fact",
        memory_confidence=0.4,
        created_at=_default_now() - timedelta(hours=100),
    )

    async with _test_session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
        )
        result = await auto_promote_proposed_memories(session)

    assert result.promoted == 0
    assert result.skipped_confidence == 1
    assert await _status_of(item_id) == "proposed"


async def test_confidence_threshold_boundary_inclusive():
    """memory_confidence == threshold (0.7) is promoted — the gate is >=."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="boundary confidence fact",
        memory_confidence=0.7,
        created_at=_default_now() - timedelta(hours=100),
    )

    async with _test_session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
        )
        result = await auto_promote_proposed_memories(session)

    assert result.promoted == 1
    assert await _status_of(item_id) == "active"


async def test_age_threshold_boundary_inclusive():
    """created_at exactly min_age_hours ago is promoted — the gate is >=."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id, principal_id = await _default_tenant_principal()
    now = _default_now()
    # exactly 72 hours old (boundary). Use a known `now` passed explicitly so the
    # boundary math is deterministic.
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="boundary age fact",
        memory_confidence=0.9,
        created_at=now - timedelta(hours=72),
    )

    async with _test_session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
        )
        result = await auto_promote_proposed_memories(session, now=now)

    assert result.promoted == 1
    assert await _status_of(item_id) == "active"


async def test_unresolved_conflict_excluded():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id, principal_id = await _default_tenant_principal()
    # Need a real conflicts_with_item_id target for the FK.
    target_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="conflict target"
    )
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="conflicted fact",
        memory_confidence=0.9,
        created_at=_default_now() - timedelta(hours=100),
        conflict_resolution_status="unresolved",
        conflicts_with_item_id=target_id,
    )

    async with _test_session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
        )
        result = await auto_promote_proposed_memories(session)

    # The conflicted item is skipped; the target it points to has no conflict
    # marker of its own and is eligible, so it promotes.
    assert result.skipped_conflict == 1
    assert await _status_of(item_id) == "proposed"
    assert await _status_of(target_id) == "active"


async def test_accepted_conflict_is_promotable():
    """conflict_resolution_status = 'accepted' is NOT a blocker (design §3)."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id, principal_id = await _default_tenant_principal()
    target_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="accepted target"
    )
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="accepted-conflict fact",
        memory_confidence=0.9,
        created_at=_default_now() - timedelta(hours=100),
        conflict_resolution_status="accepted",
        conflicts_with_item_id=target_id,
    )

    async with _test_session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
        )
        result = await auto_promote_proposed_memories(session)

    # The accepted-conflict item AND its conflict target are both eligible
    # (neither has an 'unresolved' marker), so both promote.
    assert result.promoted == 2
    assert await _status_of(item_id) == "active"
    assert await _status_of(target_id) == "active"


async def test_rejected_archived_expired_superseded_not_promoted():
    """Non-proposed / terminal-state rows never enter the candidate set."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id, principal_id = await _default_tenant_principal()
    old = _default_now() - timedelta(hours=100)

    rejected = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id,
        content="rejected", review_status="rejected", created_at=old,
    )
    archived = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id,
        content="archived", review_status="archived", created_at=old,
    )
    disputed = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id,
        content="disputed", review_status="disputed", created_at=old,
    )
    expired = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id,
        content="expired", review_status="proposed", created_at=old,
        valid_to=_default_now() - timedelta(hours=1),
    )
    # superseded: a proposed-but-superseded row should not promote.
    repl = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="replacement",
    )
    superseded = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id,
        content="superseded", review_status="proposed", created_at=old,
        superseded_by=uuid.UUID(repl),
    )

    async with _test_session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
        )
        result = await auto_promote_proposed_memories(session)

    # Only the replacement (and nothing terminal) is eligible; superseded is
    # skipped defensively.
    assert result.promoted == 1
    for iid in (rejected, archived, disputed, expired, superseded):
        assert await _status_of(iid) != "active"
    assert await _status_of(superseded) == "proposed"


async def test_idempotent_second_run_promotes_zero():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id, principal_id = await _default_tenant_principal()
    await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="idempotent fact"
    )

    async with _test_session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
        )
        first = await auto_promote_proposed_memories(session)
    async with _test_session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
        )
        second = await auto_promote_proposed_memories(session)

    assert first.promoted == 1
    assert second.promoted == 0
    assert second.scanned == 0  # nothing proposed remains


async def test_tenant_isolation():
    """Promotion for tenant A leaves tenant B's proposed items untouched."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tid_a, pid_a = await _default_tenant_principal()

    # Create tenant B.
    tid_b = str(uuid.uuid4())
    pid_b = str(uuid.uuid4())
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO tenants (id, name, slug) VALUES (:id, 'B', :slug)"
            ),
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

    item_a = await _insert_item(
        tenant_id=tid_a, principal_id=pid_a, content="tenant A eligible"
    )
    item_b = await _insert_item(
        tenant_id=tid_b, principal_id=pid_b, content="tenant B eligible"
    )

    # Promote only tenant A.
    async with _test_session_factory() as session:
        result = await auto_promote_proposed_memories(session, tid_a)

    assert result.promoted == 1
    assert result.tenant_id == tid_a
    assert await _status_of(item_a) == "active"
    # Tenant B's item is untouched.
    assert await _status_of(item_b) == "proposed"


async def test_admin_endpoint_returns_summary(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    # Setup via helper sessions, then a single client call to the endpoint —
    # mirrors the passing test_startup_recall_* structure (helper-then-client).
    tenant_id, principal_id = await _default_tenant_principal()
    await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="endpoint fact"
    )

    resp = await client.post("/v1/admin/promote")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["promoted"] == 1
    assert body["scanned"] == 1
    assert body["enabled"] is True
    assert body["confidence_threshold"] == pytest.approx(0.7)
    assert body["min_age_hours"] == 72
    assert body["promoted_ids"]
    assert "auto-promotion" in body["summary"].lower() or "promoted" in body["summary"]


async def test_cli_promote_single_tenant(capsys):
    """The CLI ``promote-proposed`` command promotes and prints a clear summary."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    from engram.cli import _run_promotion

    tenant_id, principal_id = await _default_tenant_principal()
    await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="cli fact"
    )

    rc = await _run_promotion(
        tenant_id, limit=None, session_factory=_test_session_factory
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "promoted=1" in out
    assert f"tenant={tenant_id}" in out
    assert "Total:" in out

# ---- startup + semantic recall integration (acceptance criteria) ----


async def test_startup_recall_excludes_proposed_includes_after_promotion(client, monkeypatch):
    """Before promotion: proposed item absent from startup recall.
    After promotion: the same item appears as active."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="startup integration fact",
        memory_confidence=0.9,
        created_at=_default_now() - timedelta(hours=100),
    )

    # Proposed → not in startup recall (active-only).
    resp = await client.post("/v1/recall", json={"mode": "startup"})
    assert resp.status_code == 200, resp.text
    ids = {item["id"] for item in resp.json()["items"]}
    assert item_id not in ids

    # Promote.
    async with _test_session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
        )
        result = await auto_promote_proposed_memories(session)
    assert result.promoted == 1

    # Now active → present in startup recall.
    resp = await client.post("/v1/recall", json={"mode": "startup"})
    assert resp.status_code == 200, resp.text
    ids = {item["id"] for item in resp.json()["items"]}
    assert item_id in ids


async def test_semantic_recall_warning_changes_after_promotion(client, monkeypatch):
    """Proposed item shows warnings=['unreviewed']; after promotion (active) no warning."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")

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

    # Write the item via the client so the engine's first connection lands on the
    # request loop (mirrors test_semantic_recall). The write path also creates
    # the embedding via the monkeypatched fake.
    create = await client.post(
        "/v1/remember",
        json={"content": "semantic target unreviewed", "source_type": "extraction"},
    )
    assert create.status_code == 201, create.text
    item_id = create.json()["id"]
    # Tweak to promotion-eligible (session-after-client is the proven-safe order).
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "UPDATE memory_items SET memory_confidence = 0.9, "
                "created_at = :old WHERE id = :id"
            ),
            {"old": _default_now() - timedelta(hours=100), "id": item_id},
        )
        await session.commit()

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


async def test_custom_tenant_config_thresholds_respected():
    """Tenant-configured threshold/age overrides defaults."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id, principal_id = await _default_tenant_principal()
    # Tighten confidence to 0.95 and age to 1h.
    async with _test_session_factory() as session:
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
    low = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id,
        content="below custom threshold", memory_confidence=0.9,
        created_at=_default_now() - timedelta(hours=5),
    )
    # 0.97 confidence >= 0.95, age 5h >= 1h → promoted.
    high = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id,
        content="above custom threshold", memory_confidence=0.97,
        created_at=_default_now() - timedelta(hours=5),
    )

    async with _test_session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
        )
        result = await auto_promote_proposed_memories(session)

    assert result.promoted == 1
    assert result.skipped_confidence == 1
    assert result.confidence_threshold == pytest.approx(0.95)
    assert result.min_age_hours == 1
    assert await _status_of(low) == "proposed"
    assert await _status_of(high) == "active"
