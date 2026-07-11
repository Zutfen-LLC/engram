# ruff: noqa: E501
"""Real-Postgres coverage for V2-BL-003A trusted-actor attribution.

Proves Problem 1's fix: trusted internal review operations (Path A promotion,
promotion-time conflict recheck) are attributed to a durable, tenant-scoped
system principal — never the memory author, never a random/nullable actor —
and that this holds across every promotion entry point (direct service call
with each ``source``, the admin endpoint, and startup recall). Also covers
``engram.promotion.resolve_trusted_system_actor`` itself: idempotent
lookup-or-create, concurrency safety, and tenant isolation.

Requires a live PostgreSQL with the v2 schema; skips automatically when no DB
is reachable (mirrors tests/test_promotion.py).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.api.app import create_app
from engram.config import settings
from engram.db import get_session
from engram.promotion import (
    TRUSTED_SYSTEM_PRINCIPAL_NAME,
    auto_promote_proposed_memories,
    resolve_trusted_system_actor,
)

_test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_test_session_factory = async_sessionmaker(
    _test_engine, class_=AsyncSession, expire_on_commit=False
)


async def _db_ok() -> bool:
    try:
        async with _test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _require_db():
    pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")


@pytest.fixture(autouse=True)
async def _clean_db():
    if not await _db_ok():
        return
    # item_events/memory_items must be deleted before tenants — see the note
    # in tests/test_promotion.py's _clean_db for why tenants-first raises a
    # spurious FK violation.
    async with _test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM item_events"))
        await conn.execute(text("DELETE FROM memory_items"))
        await conn.execute(text("DELETE FROM tenants WHERE slug != 'default'"))
        await conn.execute(
            text(
                "DELETE FROM principals WHERE type = 'system' "
                "AND tenant_id = (SELECT id FROM tenants WHERE slug = 'default')"
            )
        )
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


async def _default_tenant_principal() -> tuple[str, str]:
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


async def _seed_tenant(name: str) -> str:
    tenant_id = str(uuid.uuid4())
    async with _test_session_factory() as session:
        await session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
            {"id": tenant_id, "name": name, "slug": f"{name.lower()}-{tenant_id[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_config (tenant_id, config_version, active) "
                "VALUES (:tid, 'v1', TRUE)"
            ),
            {"tid": tenant_id},
        )
        await session.commit()
    return tenant_id


async def _seed_agent_principal(tenant_id: str, name: str) -> str:
    principal_id = str(uuid.uuid4())
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES (:id, :tid, :name, 'agent')"
            ),
            {"id": principal_id, "tid": tenant_id, "name": name},
        )
        await session.commit()
    return principal_id


def _default_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


async def _insert_item(
    *,
    tenant_id: str,
    principal_id: str,
    content: str,
    review_status: str = "proposed",
    memory_confidence: float = 0.9,
    created_at: datetime | None = None,
) -> str:
    item_id = str(uuid.uuid4())
    if created_at is None:
        created_at = _default_now() - timedelta(hours=100)
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO memory_items ("
                "id, tenant_id, principal_id, content, content_hash, kind, "
                "visibility, review_status, memory_confidence, source_trust, "
                "importance, source_type, created_at, valid_from"
                ") VALUES ("
                ":id, :tenant_id, :principal_id, :content, :content_hash, 'fact', "
                "'workspace', :review_status, :memory_confidence, 0.5, "
                "0.5, 'manual', :created_at, :created_at"
                ")"
            ),
            {
                "id": item_id,
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "content": content,
                "content_hash": f"sha256:{uuid.uuid4().hex}",
                "review_status": review_status,
                "memory_confidence": memory_confidence,
                "created_at": created_at,
            },
        )
        await session.commit()
    return item_id


async def _events_for(item_id: str) -> list[dict[str, object]]:
    async with _test_session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT event_type, field_name, old_value, new_value, reason, "
                    "actor_principal_id::text AS actor_principal_id "
                    "FROM item_events WHERE item_id = :id ORDER BY created_at ASC, id ASC"
                ),
                {"id": item_id},
            )
        ).mappings().all()
    return [dict(r) for r in rows]


async def _principal_row(principal_id: str) -> dict[str, object]:
    async with _test_session_factory() as session:
        return dict(
            (
                await session.execute(
                    text("SELECT * FROM principals WHERE id = :id"), {"id": principal_id}
                )
            )
            .mappings()
            .one()
        )


async def _system_principal_count(tenant_id: str) -> int:
    async with _test_session_factory() as session:
        return int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM principals WHERE tenant_id = :tid "
                        "AND type = 'system' AND name = :name"
                    ),
                    {"tid": tenant_id, "name": TRUSTED_SYSTEM_PRINCIPAL_NAME},
                )
            ).scalar_one()
        )


# ===========================================================================
# H. resolve_trusted_system_actor: idempotent lookup-or-create
# ===========================================================================


async def test_resolves_and_creates_system_principal():
    if not await _db_ok():
        _require_db()
    tenant_id = await _seed_tenant("TrustedActorCreate")
    async with _test_session_factory() as session:
        actor_id = await resolve_trusted_system_actor(session, tenant_id)
        await session.commit()
    row = await _principal_row(str(actor_id))
    assert row["type"] == "system"
    assert row["name"] == TRUSTED_SYSTEM_PRINCIPAL_NAME
    assert str(row["tenant_id"]) == tenant_id


async def test_repeated_lookup_is_idempotent_same_id_no_duplicate():
    if not await _db_ok():
        _require_db()
    tenant_id = await _seed_tenant("TrustedActorIdempotent")
    async with _test_session_factory() as session:
        first = await resolve_trusted_system_actor(session, tenant_id)
        await session.commit()
    async with _test_session_factory() as session:
        second = await resolve_trusted_system_actor(session, tenant_id)
        await session.commit()
    assert first == second
    assert await _system_principal_count(tenant_id) == 1


async def test_concurrent_first_use_creates_exactly_one_row():
    if not await _db_ok():
        _require_db()
    tenant_id = await _seed_tenant("TrustedActorConcurrent")

    async def _resolve() -> uuid.UUID:
        async with _test_session_factory() as session:
            actor_id = await resolve_trusted_system_actor(session, tenant_id)
            await session.commit()
            return actor_id

    results = await asyncio.gather(*(_resolve() for _ in range(8)))
    assert len(set(results)) == 1
    assert await _system_principal_count(tenant_id) == 1


async def test_tenant_isolation_distinct_system_principals():
    if not await _db_ok():
        _require_db()
    tenant_a = await _seed_tenant("TrustedActorTenantA")
    tenant_b = await _seed_tenant("TrustedActorTenantB")
    async with _test_session_factory() as session:
        actor_a = await resolve_trusted_system_actor(session, tenant_a)
        await session.commit()
    async with _test_session_factory() as session:
        actor_b = await resolve_trusted_system_actor(session, tenant_b)
        await session.commit()
    assert actor_a != actor_b
    row_a = await _principal_row(str(actor_a))
    row_b = await _principal_row(str(actor_b))
    assert str(row_a["tenant_id"]) == tenant_a
    assert str(row_b["tenant_id"]) == tenant_b


# ===========================================================================
# C. Automatic promotion event attribution — never the item author
# ===========================================================================


async def test_agent_authored_promotion_does_not_attribute_to_agent_author():
    """The false self-approval case: an agent-authored proposal is
    auto-promoted, and the resulting event must not appear to have been
    approved by the agent that wrote it."""
    if not await _db_ok():
        _require_db()
    tenant_id, _ = await _default_tenant_principal()
    agent_author = await _seed_agent_principal(tenant_id, f"proposer-{uuid.uuid4().hex[:8]}")
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=agent_author,
        content="agent-authored fact eligible for promotion",
    )

    async with _test_session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
        )
        result = await auto_promote_proposed_memories(session, source="cli")

    assert result.promoted == 1
    events = await _events_for(item_id)
    promo_events = [
        e for e in events if e["event_type"] == "review_change" and e["new_value"] == "active"
    ]
    assert len(promo_events) == 1
    actor = promo_events[0]["actor_principal_id"]
    assert actor != agent_author, "promotion event must not credit the agent author"
    actor_row = await _principal_row(actor)
    assert actor_row["type"] == "system"
    assert actor_row["name"] == TRUSTED_SYSTEM_PRINCIPAL_NAME

    # The item's own authorship is unchanged.
    async with _test_session_factory() as session:
        author_still = (
            await session.execute(
                text("SELECT principal_id::text FROM memory_items WHERE id = :id"),
                {"id": item_id},
            )
        ).scalar_one()
    assert author_still == agent_author


async def test_promotion_conflict_recheck_event_uses_system_actor():
    if not await _db_ok():
        _require_db()
    from engram.conflicts import ConflictVerdict

    tenant_id, principal_id = await _default_tenant_principal()
    agent_author = await _seed_agent_principal(tenant_id, f"conflict-author-{uuid.uuid4().hex[:8]}")
    proposed_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=agent_author,
        content="conflict recheck candidate",
    )
    active_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="conflicting active memory",
        review_status="active",
    )
    from engram.embeddings import EMBEDDING_MODEL

    async def _insert_embedding(item_id: str, angle: float) -> None:
        import math

        vec = [0.0] * 1536
        vec[0] = math.cos(math.radians(angle))
        vec[1] = math.sin(math.radians(angle))
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
                    "dim": len(vec),
                    "embedding": str(vec),
                },
            )
            await session.commit()

    await _insert_embedding(proposed_id, 0)
    await _insert_embedding(active_id, 5)

    import engram.conflicts as conflicts_mod

    async def fake_classify(old_content, new_content, similarity):
        return ConflictVerdict.CONTRADICT, 0.9, "forced contradiction", {}

    import pytest as _pytest

    monkeypatch = _pytest.MonkeyPatch()
    monkeypatch.setattr(conflicts_mod, "_classify_relationship", fake_classify)
    try:
        async with _test_session_factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
            )
            result = await auto_promote_proposed_memories(session, source="cli")
    finally:
        monkeypatch.undo()

    assert result.promoted == 0
    assert result.skipped_conflict_recheck == 1
    events = await _events_for(proposed_id)
    conflict_events = [e for e in events if e["event_type"] == "conflict_resolution"]
    assert len(conflict_events) == 1
    actor = conflict_events[0]["actor_principal_id"]
    assert actor != agent_author
    actor_row = await _principal_row(actor)
    assert actor_row["type"] == "system"


@pytest.mark.parametrize("source", ["cli", "worker", "admin_endpoint", "startup_recall"])
async def test_every_promotion_entry_point_attributes_to_system_actor(source: str):
    """CLI, worker, startup-recall, and admin-triggered Path A promotion all
    funnel through the same service function and the same trusted-actor
    resolution — proven here by exercising each ``source`` label directly."""
    if not await _db_ok():
        _require_db()
    tenant_id, _ = await _default_tenant_principal()
    agent_author = await _seed_agent_principal(tenant_id, f"src-{source}-{uuid.uuid4().hex[:8]}")
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=agent_author, content=f"eligible via {source}"
    )

    async with _test_session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
        )
        result = await auto_promote_proposed_memories(session, source=source)

    assert result.promoted == 1
    events = await _events_for(item_id)
    assert len(events) == 1
    assert events[0]["actor_principal_id"] != agent_author
    assert source in events[0]["reason"]
    actor_row = await _principal_row(events[0]["actor_principal_id"])
    assert actor_row["type"] == "system"


async def test_admin_endpoint_promotion_attributes_to_system_actor(monkeypatch: pytest.MonkeyPatch):
    if not await _db_ok():
        _require_db()
    import engram.db as db_module

    tenant_id, principal_id = await _default_tenant_principal()
    agent_author = await _seed_agent_principal(tenant_id, f"admin-ep-{uuid.uuid4().hex[:8]}")
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=agent_author, content="admin endpoint eligible"
    )

    app = create_app()

    async def _override_get_session():
        async with _test_session_factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
            )
            await session.execute(
                text("SELECT set_config('app.principal_id', :pid, true)"), {"pid": principal_id}
            )
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    monkeypatch.setattr(db_module, "async_session_factory", _test_session_factory)
    monkeypatch.setattr(db_module, "owner_session_factory", _test_session_factory)
    monkeypatch.setattr(db_module, "read_session_factory", _test_session_factory)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/v1/admin/promote")
    assert resp.status_code == 200, resp.text
    assert resp.json()["promoted"] == 1

    events = await _events_for(item_id)
    assert len(events) == 1
    assert events[0]["actor_principal_id"] != agent_author
    actor_row = await _principal_row(events[0]["actor_principal_id"])
    assert actor_row["type"] == "system"
