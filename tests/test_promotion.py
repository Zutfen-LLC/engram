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
        from engram.db import apply_rls_context

        await apply_rls_context(
            session, tenant_id=row["tenant_id"], principal_id=row["principal_id"]
        )
        yield session


@pytest.fixture
def app():
    # Override the route handler's session (get_session) AND the auth path's
    # session factories. get_current_principal (engram/auth.py) resolves the
    # caller (and, with auth disabled, the default principal) through the OWNER
    # session factory (engram.db.owner_session_factory); the request session uses
    # async_session_factory; startup recall's bounded candidate selection
    # (ENG-AUD-011) uses read_session_factory. All three are real app engines,
    # which use a *connection pool* (pool_size=10). A pooled connection bound
    # to one test's event loop gets reused by a later test on a different loop
    # → asyncpg "Future attached to a different loop". Pointing all three at
    # the per-test NullPool factory keeps every connection on the current
    # test's loop.
    import engram.db as db_module

    app = create_app()
    app.dependency_overrides[get_session] = _get_test_session
    real_app_factory = db_module.async_session_factory
    real_owner_factory = db_module.owner_session_factory
    real_read_factory = db_module.read_session_factory
    db_module.async_session_factory = _test_session_factory
    db_module.owner_session_factory = _test_session_factory
    db_module.read_session_factory = _test_session_factory
    app.state._engram_real_session_factory = real_app_factory  # type: ignore[attr-defined]
    app.state._engram_real_owner_factory = real_owner_factory  # type: ignore[attr-defined]
    app.state._engram_real_read_factory = real_read_factory  # type: ignore[attr-defined]
    return app


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    # Restore the real factories so other test files see the app engines.
    import engram.db as db_module

    db_module.async_session_factory = app.state._engram_real_session_factory
    db_module.owner_session_factory = app.state._engram_real_owner_factory
    db_module.read_session_factory = app.state._engram_real_read_factory


@pytest.fixture(autouse=True)
async def _clean_db():
    if not await _db_ok():
        return
    # Remove any tenants created by tests (e.g. tenant B in isolation test),
    # cascading to their items/config/principals. Keep the seeded default tenant.
    # item_events must be deleted before tenants: item_events.actor_principal_id
    # -> principals has no ON DELETE CASCADE, and a single cascading
    # `DELETE FROM tenants` fires the principals cascade before the
    # memory_items -> item_events cascade in FK-creation order, so deleting
    # tenants first raises a spurious FK violation whenever any tenant (from
    # this file or a prior test module, since cleanup only runs before each
    # test) has an item with an event.
    async with _test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM item_events"))
        await conn.execute(text("DELETE FROM memory_items"))
        await conn.execute(
            text("DELETE FROM tenants WHERE slug != 'default'")
        )
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


async def test_startup_recall_lazily_promotes_eligible_item(client):
    """ENG-AUD-007 F11: an eligible proposed item is promoted by the bounded
    lazy pass inside POST /v1/recall (mode=startup) itself — no explicit
    promote call needed — and appears active in that same response."""
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

    resp = await client.post("/v1/recall", json={"mode": "startup"})
    assert resp.status_code == 200, resp.text
    ids = {item["id"] for item in resp.json()["items"]}
    assert item_id in ids

    assert await _status_of(item_id) == "active"


async def test_startup_recall_does_not_promote_ineligible_item(client):
    """A proposed item that fails Path A gates (too new) is not promoted or
    included by the lazy startup-recall pass."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="too new for lazy promotion",
        memory_confidence=0.9,
        created_at=_default_now() - timedelta(hours=1),
    )

    resp = await client.post("/v1/recall", json={"mode": "startup"})
    assert resp.status_code == 200, resp.text
    ids = {item["id"] for item in resp.json()["items"]}
    assert item_id not in ids
    assert await _status_of(item_id) == "proposed"


async def test_startup_recall_promotion_disabled_tenant_does_not_promote(client):
    """tenant_config.auto_promote_enabled = FALSE disables the lazy pass too."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id, principal_id = await _default_tenant_principal()
    async with _test_session_factory() as session:
        await session.execute(
            text("UPDATE tenant_config SET auto_promote_enabled = FALSE WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        )
        await session.commit()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="disabled tenant lazy fact",
        memory_confidence=0.9,
        created_at=_default_now() - timedelta(hours=100),
    )

    resp = await client.post("/v1/recall", json={"mode": "startup"})
    assert resp.status_code == 200, resp.text
    ids = {item["id"] for item in resp.json()["items"]}
    assert item_id not in ids
    assert await _status_of(item_id) == "proposed"


async def test_startup_recall_promotion_respects_limit(client, monkeypatch):
    """The lazy pass is bounded by settings.startup_promotion_limit: with the
    cap set to 1, only one of two eligible proposed items promotes per call."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    from engram.config import settings as engram_settings

    monkeypatch.setattr(engram_settings, "startup_promotion_limit", 1)

    tenant_id, principal_id = await _default_tenant_principal()
    old = _default_now() - timedelta(hours=100)
    item_a = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id,
        content="bounded lazy fact A", memory_confidence=0.9, created_at=old,
    )
    item_b = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id,
        content="bounded lazy fact B", memory_confidence=0.9, created_at=old,
    )

    resp = await client.post("/v1/recall", json={"mode": "startup"})
    assert resp.status_code == 200, resp.text

    statuses = {await _status_of(item_a), await _status_of(item_b)}
    # Exactly one promoted, one still proposed — never both under limit=1.
    assert statuses == {"active", "proposed"}


async def test_semantic_recall_does_not_trigger_lazy_promotion(client):
    """mode='semantic' does not invoke the lazy promotion pass in this slice —
    an eligible proposed item stays proposed after a semantic recall call."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="semantic no lazy promotion fact",
        memory_confidence=0.9,
        created_at=_default_now() - timedelta(hours=100),
    )

    resp = await client.post(
        "/v1/recall", json={"mode": "semantic", "query": "semantic no lazy promotion fact"}
    )
    assert resp.status_code == 200, resp.text
    assert await _status_of(item_id) == "proposed"


async def test_semantic_recall_warning_changes_after_promotion(client, monkeypatch):
    """Proposed item shows warnings=['unreviewed']; after promotion (active) no warning."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")

    # Embeddings are needed for semantic recall. Use the deterministic fake so
    # the content matches the query vector.
    settings.embedding_provider = "openai"
    settings.conflict_check_on_write = False
    import engram.embeddings as embeddings_mod
    from engram import recall as recall_mod
    from engram.api.routes import memory as memory_routes

    target_vec = [1.0] + [0.0] * 1535

    async def fake_embedding(text_value: str) -> list[float] | None:
        if text_value.startswith("semantic target"):
            return target_vec
        return [0.0, 1.0] + [0.0] * 1534

    monkeypatch.setattr(recall_mod, "generate_embedding", fake_embedding)
    monkeypatch.setattr(memory_routes, "generate_embedding", fake_embedding)
    monkeypatch.setattr(embeddings_mod, "generate_embedding", fake_embedding)

    # Write the item via the client so the engine's first connection lands on the
    # request loop (mirrors test_semantic_recall). The write path also creates
    # the embedding via the monkeypatched fake.
    create = await client.post(
        "/v1/remember",
        json={"content": "semantic target unreviewed", "source_type": "extraction"},
    )
    assert create.status_code == 201, create.text
    item_id = create.json()["id"]
    # ENG-AUD-008: /v1/remember enqueues embedding.generate; drain it so the
    # embedding is ready before semantic recall.
    from engram.worker import process_one_job

    for _ in range(10):
        processed = await process_one_job(
            worker_id="test",
            session_factory=_test_session_factory,
            app_session_factory=_test_session_factory,
            job_types=["embedding.generate"],
        )
        if not processed:
            break
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


# ---- dispute gate (F12) ----


async def _insert_principal(tenant_id: str, name: str) -> str:
    """Insert a principal with a uuid-suffixed name (unique per call — the
    default tenant is shared and not reset between test runs, unlike
    tenant-B-style isolation tests which drop their own tenant)."""
    principal_id = str(uuid.uuid4())
    unique_name = f"{name}-{uuid.uuid4().hex[:8]}"
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES (:id, :tid, :name, 'agent')"
            ),
            {"id": principal_id, "tid": tenant_id, "name": unique_name},
        )
        await session.commit()
    return principal_id


async def _insert_review_change_event(
    *, item_id: str, new_value: str, actor_principal_id: str
) -> None:
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO item_events (id, item_id, event_type, field_name, "
                "old_value, new_value, actor_principal_id) VALUES "
                "(:id, :item_id, 'review_change', 'review_status', 'proposed', "
                ":new_value, :actor)"
            ),
            {
                "id": str(uuid.uuid4()),
                "item_id": item_id,
                "new_value": new_value,
                "actor": actor_principal_id,
            },
        )
        await session.commit()


async def _insert_feedback_event(
    *, tenant_id: str, item_id: str, principal_id: str, verdict: str
) -> None:
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO feedback_events (id, tenant_id, item_id, principal_id, verdict) "
                "VALUES (:id, :tid, :item_id, :pid, :verdict)"
            ),
            {
                "id": str(uuid.uuid4()),
                "tid": tenant_id,
                "item_id": item_id,
                "pid": principal_id,
                "verdict": verdict,
            },
        )
        await session.commit()


async def test_dispute_by_other_principal_blocks_promotion():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id, principal_id = await _default_tenant_principal()
    other_principal_id = await _insert_principal(tenant_id, "other-agent-disputer")
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="disputed by another principal",
        memory_confidence=0.9,
        created_at=_default_now() - timedelta(hours=100),
    )
    await _insert_review_change_event(
        item_id=item_id, new_value="disputed", actor_principal_id=other_principal_id
    )

    async with _test_session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
        )
        result = await auto_promote_proposed_memories(session)

    assert result.promoted == 0
    assert result.skipped_dispute == 1
    assert await _status_of(item_id) == "proposed"


async def test_dispute_by_creator_self_does_not_block():
    """The item's own creator disputing their own item is not an external
    dispute — Path A still promotes (design.md §3: only another principal's
    dispute blocks)."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="self-disputed by creator",
        memory_confidence=0.9,
        created_at=_default_now() - timedelta(hours=100),
    )
    await _insert_review_change_event(
        item_id=item_id, new_value="disputed", actor_principal_id=principal_id
    )

    async with _test_session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
        )
        result = await auto_promote_proposed_memories(session)

    assert result.promoted == 1
    assert result.skipped_dispute == 0
    assert await _status_of(item_id) == "active"


async def test_negative_feedback_from_other_principal_blocks_promotion():
    """A 'noise' feedback_events row from another principal also counts as an
    external dispute signal (design.md §3 dispute-event definition)."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id, principal_id = await _default_tenant_principal()
    other_principal_id = await _insert_principal(tenant_id, "other-agent-feedback")
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="noise feedback from another principal",
        memory_confidence=0.9,
        created_at=_default_now() - timedelta(hours=100),
    )
    await _insert_feedback_event(
        tenant_id=tenant_id, item_id=item_id, principal_id=other_principal_id, verdict="noise"
    )

    async with _test_session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
        )
        result = await auto_promote_proposed_memories(session)

    assert result.promoted == 0
    assert result.skipped_dispute == 1
    assert await _status_of(item_id) == "proposed"


async def test_own_noise_feedback_does_not_block():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="own noise feedback",
        memory_confidence=0.9,
        created_at=_default_now() - timedelta(hours=100),
    )
    await _insert_feedback_event(
        tenant_id=tenant_id, item_id=item_id, principal_id=principal_id, verdict="noise"
    )

    async with _test_session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
        )
        result = await auto_promote_proposed_memories(session)

    assert result.promoted == 1
    assert result.skipped_dispute == 0


@pytest.mark.parametrize(
    ("historical", "current", "blocked"),
    [("noise", "useful", False), ("useful", "noise", True)],
)
async def test_path_a_uses_only_current_canonical_feedback(
    historical: str, current: str, blocked: bool
):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id, principal_id = await _default_tenant_principal()
    other_id = await _insert_principal(
        tenant_id, f"canonical-feedback-{historical}-{current}"
    )
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content=f"historical {historical} current {current}",
        memory_confidence=0.9,
        created_at=_default_now() - timedelta(hours=100),
    )
    old_id, new_id = str(uuid.uuid4()), str(uuid.uuid4())
    replacement_time = _default_now() - timedelta(minutes=1)
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO feedback_events "
                "(id, tenant_id, item_id, principal_id, verdict, created_at, superseded_at) "
                "VALUES (:id, :tid, :iid, :pid, :verdict, :created, :superseded)"
            ),
            {
                "id": old_id,
                "tid": tenant_id,
                "iid": item_id,
                "pid": other_id,
                "verdict": historical,
                "created": replacement_time - timedelta(minutes=1),
                "superseded": replacement_time,
            },
        )
        await session.execute(
            text(
                "INSERT INTO feedback_events "
                "(id, tenant_id, item_id, principal_id, verdict, created_at, "
                "replaces_feedback_event_id) "
                "VALUES (:id, :tid, :iid, :pid, :verdict, :created, :old_id)"
            ),
            {
                "id": new_id,
                "tid": tenant_id,
                "iid": item_id,
                "pid": other_id,
                "verdict": current,
                "created": replacement_time,
                "old_id": old_id,
            },
        )
        await session.commit()

    async with _test_session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
        )
        result = await auto_promote_proposed_memories(session)

    assert result.skipped_dispute == int(blocked)
    assert result.promoted == int(not blocked)
    assert await _status_of(item_id) == ("proposed" if blocked else "active")


# ---- promotion-time conflict recheck + top-k candidates (F13) ----


async def _insert_embedding(
    *, item_id: str, tenant_id: str, vector: list[float]
) -> None:
    from engram.embeddings import EMBEDDING_MODEL

    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO memory_embeddings (id, memory_item_id, tenant_id, "
                "embedding_model, embedding_dim, embedding, embedding_status) "
                "VALUES (:id, :item_id, :tid, :model, :dim, :embedding, 'ready')"
            ),
            {
                "id": str(uuid.uuid4()),
                "item_id": item_id,
                "tid": tenant_id,
                "model": EMBEDDING_MODEL,
                "dim": len(vector),
                "embedding": str(vector),
            },
        )
        await session.commit()


def _unit_vector_2d(angle_degrees: float, dim: int = 1536) -> list[float]:
    import math

    radians = math.radians(angle_degrees)
    vec = [0.0] * dim
    vec[0] = math.cos(radians)
    vec[1] = math.sin(radians)
    return vec


async def test_conflict_recheck_blocks_when_active_item_conflicts_later(monkeypatch):
    """A candidate clean at write time (conflict_resolution_status IS NULL)
    but that now conflicts with a *later* active memory does not promote."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    from engram.conflicts import ConflictVerdict

    tenant_id, principal_id = await _default_tenant_principal()
    proposed_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="recheck candidate",
        memory_confidence=0.9,
        created_at=_default_now() - timedelta(hours=100),
    )
    active_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="later active conflicting memory",
        review_status="active",
    )
    await _insert_embedding(
        item_id=proposed_id, tenant_id=tenant_id, vector=_unit_vector_2d(0)
    )
    await _insert_embedding(
        item_id=active_id, tenant_id=tenant_id, vector=_unit_vector_2d(5)
    )

    import engram.conflicts as conflicts_mod

    async def fake_classify(old_content, new_content, similarity):
        return ConflictVerdict.CONTRADICT, 0.9, "forced contradiction", {}

    monkeypatch.setattr(conflicts_mod, "_classify_relationship", fake_classify)

    async with _test_session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
        )
        result = await auto_promote_proposed_memories(session)

    assert result.promoted == 0
    assert result.skipped_conflict_recheck == 1
    assert await _status_of(proposed_id) == "proposed"

    row = await _fetch_conflict_fields(proposed_id)
    assert row["conflict_resolution_status"] == "unresolved"
    assert str(row["conflicts_with_item_id"]) == active_id

    events = await _events_for(proposed_id)
    assert any(e["event_type"] == "conflict_resolution" for e in events)


async def _fetch_conflict_fields(item_id: str) -> dict[str, Any]:
    async with _test_session_factory() as session:
        return dict(
            (
                await session.execute(
                    text(
                        "SELECT conflict_resolution_status, conflicts_with_item_id "
                        "FROM memory_items WHERE id = :id"
                    ),
                    {"id": item_id},
                )
            )
            .mappings()
            .one()
        )


async def test_topk_conflict_candidate_third_nearest_detected(monkeypatch):
    """The conflicting candidate is only the 3rd-nearest by embedding
    distance. A top-1-only implementation (checking only the nearest active
    item) would never see it and would wrongly promote."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    from engram.conflicts import ConflictVerdict

    tenant_id, principal_id = await _default_tenant_principal()
    proposed_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="topk candidate",
        memory_confidence=0.9,
        created_at=_default_now() - timedelta(hours=100),
    )
    # Three active items, all above the 0.85 similarity threshold, ordered
    # nearest (cand1) -> farthest (cand3) by embedding angle.
    cand1 = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id,
        content="cand1 nearest duplicate", review_status="active",
    )
    cand2 = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id,
        content="cand2 middle refine", review_status="active",
    )
    cand3 = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id,
        content="cand3 farthest contradiction", review_status="active",
    )
    await _insert_embedding(item_id=proposed_id, tenant_id=tenant_id, vector=_unit_vector_2d(0))
    await _insert_embedding(item_id=cand1, tenant_id=tenant_id, vector=_unit_vector_2d(2))
    await _insert_embedding(item_id=cand2, tenant_id=tenant_id, vector=_unit_vector_2d(15))
    await _insert_embedding(item_id=cand3, tenant_id=tenant_id, vector=_unit_vector_2d(28))

    import engram.conflicts as conflicts_mod

    async def fake_classify(old_content, new_content, similarity):
        if "cand1" in old_content:
            return ConflictVerdict.DUPLICATE, 0.9, "cand1 dup", {}
        if "cand2" in old_content:
            return ConflictVerdict.REFINE, 0.9, "cand2 refine", {}
        return ConflictVerdict.CONTRADICT, 0.9, "cand3 contradict", {}

    monkeypatch.setattr(conflicts_mod, "_classify_relationship", fake_classify)

    async with _test_session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
        )
        result = await auto_promote_proposed_memories(session)

    assert result.promoted == 0
    assert result.skipped_conflict_recheck == 1
    assert await _status_of(proposed_id) == "proposed"
    row = await _fetch_conflict_fields(proposed_id)
    assert str(row["conflicts_with_item_id"]) == cand3


async def test_topk_candidate_count_bounded(monkeypatch):
    """find_promotion_conflict_candidates never returns more than k rows."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    from engram.conflicts import find_promotion_conflict_candidates
    from engram.models import MemoryItem

    tenant_id, principal_id = await _default_tenant_principal()
    proposed_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="bounded topk candidate",
        memory_confidence=0.9,
        created_at=_default_now() - timedelta(hours=100),
    )
    await _insert_embedding(item_id=proposed_id, tenant_id=tenant_id, vector=_unit_vector_2d(0))
    # 8 active candidates, all within the similarity threshold — more than k.
    for i in range(8):
        cand_id = await _insert_item(
            tenant_id=tenant_id, principal_id=principal_id,
            content=f"bounded candidate {i}", review_status="active",
        )
        await _insert_embedding(
            item_id=cand_id, tenant_id=tenant_id, vector=_unit_vector_2d(1 + i)
        )

    async with _test_session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
        )
        from sqlalchemy import select

        item = (
            await session.execute(select(MemoryItem).where(MemoryItem.id == proposed_id))
        ).scalar_one()
        candidates = await find_promotion_conflict_candidates(session, item, k=3)

    assert len(candidates) <= 3


# ---- embeddings-off fallback (F13) ----


async def test_embeddings_off_same_subject_conflict_blocks_promotion():
    """With embeddings disabled (no stored embedding for the candidate item),
    a same-subject/same-kind active memory with different content blocks
    promotion via the structural fallback."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id, principal_id = await _default_tenant_principal()
    proposed_id = str(uuid.uuid4())
    active_id = str(uuid.uuid4())
    async with _test_session_factory() as session:
        for item_id, content, review_status, content_hash in (
            (active_id, "Alice's role is engineer", "active", "sha256:active-hash"),
            (proposed_id, "Alice's role is manager", "proposed", "sha256:proposed-hash"),
        ):
            await session.execute(
                text(
                    "INSERT INTO memory_items (id, tenant_id, principal_id, content, "
                    "content_hash, kind, visibility, review_status, memory_confidence, "
                    "source_trust, importance, source_type, subject_type, subject_id, "
                    "created_at, valid_from) VALUES (:id, :tid, :pid, :content, :hash, "
                    "'fact', 'workspace', :status, 0.9, 0.5, 0.5, 'manual', 'domain_entity', "
                    "'alice', :created_at, :created_at)"
                ),
                {
                    "id": item_id,
                    "tid": tenant_id,
                    "pid": principal_id,
                    "content": content,
                    "hash": content_hash,
                    "status": review_status,
                    "created_at": _default_now() - timedelta(hours=100),
                },
            )
        await session.commit()

    async with _test_session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
        )
        result = await auto_promote_proposed_memories(session)

    assert result.promoted == 0
    assert result.skipped_conflict_recheck == 1
    assert await _status_of(proposed_id) == "proposed"
    row = await _fetch_conflict_fields(proposed_id)
    assert row["conflict_resolution_status"] == "unresolved"
    assert str(row["conflicts_with_item_id"]) == active_id


async def test_embeddings_off_exact_same_subject_content_still_promotes():
    """Same subject + same content_hash (an exact match, not a conflict) is
    not flagged by the structural fallback and promotes normally.

    Uses two distinct principals for the active/proposed rows: the dedup
    unique index (tenant_id, workspace_id, principal_id, content_hash) forbids
    two *live* rows from the same principal sharing a content_hash, but two
    different principals independently writing identical content is a valid
    scenario and exercises exactly the "exact match, not a conflict" case.
    """
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id, principal_id = await _default_tenant_principal()
    other_principal_id = await _insert_principal(tenant_id, "exact-match-other-writer")
    shared_hash = f"sha256:{uuid.uuid4().hex}"
    active_id = str(uuid.uuid4())
    proposed_id = str(uuid.uuid4())
    async with _test_session_factory() as session:
        for item_id, review_status, pid in (
            (active_id, "active", other_principal_id),
            (proposed_id, "proposed", principal_id),
        ):
            await session.execute(
                text(
                    "INSERT INTO memory_items (id, tenant_id, principal_id, content, "
                    "content_hash, kind, visibility, review_status, memory_confidence, "
                    "source_trust, importance, source_type, subject_type, subject_id, "
                    "created_at, valid_from) VALUES (:id, :tid, :pid, 'Bob is an engineer', "
                    ":hash, 'fact', 'workspace', :status, 0.9, 0.5, 0.5, 'manual', "
                    "'domain_entity', 'bob', :created_at, :created_at)"
                ),
                {
                    "id": item_id,
                    "tid": tenant_id,
                    "pid": pid,
                    "hash": shared_hash,
                    "status": review_status,
                    "created_at": _default_now() - timedelta(hours=100),
                },
            )
        await session.commit()

    async with _test_session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
        )
        result = await auto_promote_proposed_memories(session)

    assert result.promoted == 1
    assert result.skipped_conflict_recheck == 0
    assert await _status_of(proposed_id) == "active"


# ---- explicit promotion still uses the same gates (CLI / admin) ----


async def test_admin_endpoint_uses_dispute_gate():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id, principal_id = await _default_tenant_principal()
    other_principal_id = await _insert_principal(tenant_id, "admin-endpoint-disputer")
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="admin endpoint disputed fact",
        memory_confidence=0.9,
        created_at=_default_now() - timedelta(hours=100),
    )
    await _insert_review_change_event(
        item_id=item_id, new_value="disputed", actor_principal_id=other_principal_id
    )

    # Exercises the same shared gate the route (POST /v1/admin/promote) calls
    # with source="admin_endpoint" — the route itself is covered end-to-end by
    # test_admin_endpoint_returns_summary.
    async with _test_session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
        )
        result = await auto_promote_proposed_memories(session, tenant_id, source="admin_endpoint")

    assert result.promoted == 0
    assert result.skipped_dispute == 1
    assert await _status_of(item_id) == "proposed"
