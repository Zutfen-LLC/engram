# ruff: noqa: E501
"""Real-Postgres coverage for V2-BL-003B: collision-safe internal review actors.

Proves that the trusted internal review actor is a durable, collision-safe,
server-owned identity keyed by ``internal_key = 'review_automation'`` — not by
the mutable principal name ``system``. Covers:

  A. Migration tests — internal_key schema, CHECK, partial unique index.
  B. Ordinary-name collision — agent/user/admin named ``system`` not adopted.
  C. Exact self-approval regression — agent named ``system`` promoted; event
     actor is the canonical internal principal, not the agent.
  D. Concurrent trusted actor creation — one canonical row per tenant.
  E. Reserved-name API tests — admin API rejects reserved prefix names.
  F. API-key issuance tests — internal principals are non-credentialable.
  G. Authentication fail-closed tests — manually inserted keys cannot auth.
  H. Promotion entry-point tests — all sources use the same canonical actor.
  I. Conflict-recheck attribution — conflict events use the canonical actor.
  J. Tenant isolation — distinct internal principals per tenant.

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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.api.app import create_app
from engram.auth import reset_principal_cache
from engram.config import settings
from engram.db import get_session
from engram.promotion import (
    TRUSTED_REVIEW_INTERNAL_KEY,
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
    reset_principal_cache()
    async with _test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM item_events"))
        await conn.execute(text("DELETE FROM memory_items"))
        # Delete api_keys first — principals has ON DELETE CASCADE from tenants
        # but api_keys.principal_id has no CASCADE, so deleting tenants/principals
        # with api_keys referencing them raises FK violation.
        await conn.execute(text("DELETE FROM api_keys WHERE label LIKE 'test-v2b3b-%'"))
        # Delete any api_keys referencing principals we're about to remove.
        await conn.execute(
            text(
                "DELETE FROM api_keys WHERE principal_id IN ("
                "  SELECT id FROM principals WHERE internal_key IS NOT NULL "
                "  OR (type = 'system' AND tenant_id = (SELECT id FROM tenants WHERE slug = 'default'))"
                ")"
            )
        )
        # Delete api_keys for non-default tenants (they'll be cascaded by
        # tenant deletion, but their principals may have api_keys without
        # CASCADE).
        await conn.execute(
            text(
                "DELETE FROM api_keys WHERE tenant_id IN ("
                "  SELECT id FROM tenants WHERE slug != 'default'"
                ")"
            )
        )
        # Also clean up api_keys and test-created principals in the default
        # tenant so subsequent test modules don't hit FK violations.
        await conn.execute(
            text(
                "DELETE FROM api_keys WHERE principal_id IN ("
                "  SELECT id FROM principals WHERE tenant_id = (SELECT id FROM tenants WHERE slug = 'default') "
                "  AND name != 'admin'"
                ")"
            )
        )
        await conn.execute(text("DELETE FROM tenants WHERE slug != 'default'"))
        await conn.execute(
            text(
                "DELETE FROM principals WHERE internal_key IS NOT NULL "
                "OR (type = 'system' AND tenant_id = (SELECT id FROM tenants WHERE slug = 'default')) "
                "OR (name = 'system' AND tenant_id = (SELECT id FROM tenants WHERE slug = 'default')) "
                "OR (name LIKE 'keytarget-%' AND tenant_id = (SELECT id FROM tenants WHERE slug = 'default')) "
                "OR (name LIKE 'src-%' AND tenant_id = (SELECT id FROM tenants WHERE slug = 'default')) "
                "OR (name LIKE 'admin-ep-%' AND tenant_id = (SELECT id FROM tenants WHERE slug = 'default')) "
                "OR (name LIKE 'conflict-author-%' AND tenant_id = (SELECT id FROM tenants WHERE slug = 'default')) "
                "OR (name LIKE 'proposer-%' AND tenant_id = (SELECT id FROM tenants WHERE slug = 'default'))"
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


# ===========================================================================
# Helpers
# ===========================================================================


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


async def _seed_principal(
    tenant_id: str, name: str, ptype: str, *, internal_key: str | None = None
) -> str:
    principal_id = str(uuid.uuid4())
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type, internal_key) "
                "VALUES (:id, :tid, :name, :type, :ik)"
            ),
            {"id": principal_id, "tid": tenant_id, "name": name, "type": ptype, "ik": internal_key},
        )
        await session.commit()
    return principal_id


async def _seed_agent_principal(tenant_id: str, name: str) -> str:
    return await _seed_principal(tenant_id, name, "agent")


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


async def _internal_principal_count(tenant_id: str) -> int:
    async with _test_session_factory() as session:
        return int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM principals WHERE tenant_id = :tid "
                        "AND internal_key = :ik"
                    ),
                    {"tid": tenant_id, "ik": TRUSTED_REVIEW_INTERNAL_KEY},
                )
            ).scalar_one()
        )


async def _insert_api_key_for_principal(
    *,
    tenant_id: str,
    principal_id: str,
    label: str,
) -> tuple[str, str]:
    """Manually insert a new-format API key for a principal (simulates owner-
    level SQL insertion). Returns (plaintext, key_id)."""
    from engram.auth import DIGEST_ALGORITHM, digest_api_key_secret, generate_api_key, parse_api_key

    plaintext = generate_api_key()
    parsed = parse_api_key(plaintext)
    assert parsed.key_id is not None
    digest = digest_api_key_secret(parsed.secret)
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO api_keys (id, tenant_id, principal_id, key_hash, key_id, "
                "secret_digest, digest_algorithm, scopes, label, created_at, revoked_at) "
                "VALUES (:id, :tid, :pid, NULL, :kid, :sd, :da, "
                "'{read,write,admin,export}', :lbl, now(), NULL)"
            ),
            {
                "id": str(uuid.uuid4()),
                "tid": tenant_id,
                "pid": principal_id,
                "kid": parsed.key_id,
                "sd": digest,
                "da": DIGEST_ALGORITHM,
                "lbl": label,
            },
        )
        await session.commit()
    return plaintext, parsed.key_id


async def _insert_legacy_api_key_for_principal(
    *,
    tenant_id: str,
    principal_id: str,
    label: str,
) -> str:
    """Manually insert a legacy bcrypt API key for a principal."""
    import bcrypt

    plaintext = "eng_" + uuid.uuid4().hex + uuid.uuid4().hex
    key_hash = bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt()).decode()
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO api_keys (id, tenant_id, principal_id, key_hash, key_id, "
                "secret_digest, digest_algorithm, scopes, label, created_at, revoked_at) "
                "VALUES (:id, :tid, :pid, :kh, NULL, NULL, NULL, "
                "'{read,write,admin,export}', :lbl, now(), NULL)"
            ),
            {
                "id": str(uuid.uuid4()),
                "tid": tenant_id,
                "pid": principal_id,
                "kh": key_hash,
                "lbl": label,
            },
        )
        await session.commit()
    return plaintext


# ===========================================================================
# A. Migration tests
# ===========================================================================


async def test_internal_key_column_exists_and_defaults_null():
    if not await _db_ok():
        _require_db()
    async with _test_session_factory() as session:
        col = (
            await session.execute(
                text(
                    "SELECT is_nullable, data_type FROM information_schema.columns "
                    "WHERE table_name = 'principals' AND column_name = 'internal_key'"
                )
            )
        ).one()
    assert col.is_nullable == "YES"
    assert col.data_type == "text"


async def test_internal_key_requires_system_type():
    if not await _db_ok():
        _require_db()
    tenant_id = await _seed_tenant("IkCheckSystem")
    # Inserting a non-system principal with internal_key should fail.
    with pytest.raises(IntegrityError):
        async with _test_session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO principals (id, tenant_id, name, type, internal_key) "
                    "VALUES (:id, :tid, 'bad', 'agent', 'some_key')"
                ),
                {"id": str(uuid.uuid4()), "tid": tenant_id},
            )
            await session.commit()


async def test_duplicate_internal_key_rejected():
    if not await _db_ok():
        _require_db()
    tenant_id = await _seed_tenant("IkDupKey")
    await _seed_principal(
        tenant_id, "first", "system", internal_key="some_role"
    )
    with pytest.raises(IntegrityError):
        async with _test_session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO principals (id, tenant_id, name, type, internal_key) "
                    "VALUES (:id, :tid, 'second', 'system', 'some_role')"
                ),
                {"id": str(uuid.uuid4()), "tid": tenant_id},
            )
            await session.commit()


async def test_same_internal_key_in_different_tenants():
    if not await _db_ok():
        _require_db()
    tenant_a = await _seed_tenant("IkTenantA")
    tenant_b = await _seed_tenant("IkTenantB")
    await _seed_principal(tenant_a, "actor-a", "system", internal_key="shared_role")
    # Same key in a different tenant must succeed.
    await _seed_principal(tenant_b, "actor-b", "system", internal_key="shared_role")


async def test_multiple_null_internal_key_principals_supported():
    if not await _db_ok():
        _require_db()
    tenant_id = await _seed_tenant("IkNullCoexist")
    await _seed_principal(tenant_id, "agent1", "agent")
    await _seed_principal(tenant_id, "agent2", "agent")
    await _seed_principal(tenant_id, "user1", "user")
    async with _test_session_factory() as session:
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM principals WHERE tenant_id = :tid "
                    "AND internal_key IS NULL"
                ),
                {"tid": tenant_id},
            )
        ).scalar_one()
    assert count >= 3


async def test_existing_principals_named_system_remain_null_internal_key():
    if not await _db_ok():
        _require_db()
    tenant_id = await _seed_tenant("IkExistingSystem")
    # Simulate an existing ordinary principal named "system" (any type).
    ordinary_id = await _seed_principal(tenant_id, "system", "agent")
    row = await _principal_row(ordinary_id)
    assert row["internal_key"] is None
    assert row["type"] == "agent"


# ===========================================================================
# B. Ordinary-name collision tests
# ===========================================================================


@pytest.mark.parametrize("ptype", ["agent", "user", "admin", "system"])
async def test_ordinary_principal_named_system_not_adopted(ptype: str):
    """An ordinary principal named 'system' (of any type, without internal_key)
    must NOT be adopted as the canonical trusted actor."""
    if not await _db_ok():
        _require_db()
    tenant_id = await _seed_tenant(f"Collision{ptype.title()}")
    ordinary_id = await _seed_principal(tenant_id, "system", ptype)

    async with _test_session_factory() as session:
        actor_id = await resolve_trusted_system_actor(session, tenant_id)
        await session.commit()

    assert str(actor_id) != ordinary_id, "ordinary principal must not be adopted"

    actor_row = await _principal_row(str(actor_id))
    assert actor_row["type"] == "system"
    assert actor_row["internal_key"] == TRUSTED_REVIEW_INTERNAL_KEY
    assert str(actor_row["tenant_id"]) == tenant_id

    # The ordinary principal is unchanged.
    ordinary_row = await _principal_row(ordinary_id)
    assert ordinary_row["internal_key"] is None
    assert ordinary_row["type"] == ptype


# ===========================================================================
# C. Exact self-approval regression
# ===========================================================================


async def test_agent_named_system_promotion_does_not_attribute_to_agent():
    """The exact regression: an agent principal named 'system' authors a
    proposal that auto-promotes. The promotion event actor must be the
    canonical internal principal, NOT the agent — even though the agent is
    named 'system'."""
    if not await _db_ok():
        _require_db()
    tenant_id, _ = await _default_tenant_principal()
    agent_author = await _seed_agent_principal(tenant_id, "system")
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=agent_author,
        content="agent-named-system authored fact eligible for promotion",
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
    assert actor != agent_author, "promotion event must not credit the agent named 'system'"

    actor_row = await _principal_row(actor)
    assert actor_row["type"] == "system"
    assert actor_row["internal_key"] == TRUSTED_REVIEW_INTERNAL_KEY

    # Item authorship remains the agent.
    async with _test_session_factory() as session:
        author_still = (
            await session.execute(
                text("SELECT principal_id::text FROM memory_items WHERE id = :id"),
                {"id": item_id},
            )
        ).scalar_one()
    assert author_still == agent_author

    # The ordinary agent named 'system' remains ordinary (internal_key NULL).
    agent_row = await _principal_row(agent_author)
    assert agent_row["internal_key"] is None
    assert agent_row["type"] == "agent"


# ===========================================================================
# D. Concurrent trusted actor creation
# ===========================================================================


async def test_concurrent_first_use_creates_exactly_one_internal_row():
    if not await _db_ok():
        _require_db()
    tenant_id = await _seed_tenant("ConcurrentInternal")

    async def _resolve() -> uuid.UUID:
        async with _test_session_factory() as session:
            actor_id = await resolve_trusted_system_actor(session, tenant_id)
            await session.commit()
            return actor_id

    results = await asyncio.gather(*(_resolve() for _ in range(8)))
    assert len(set(results)) == 1
    assert await _internal_principal_count(tenant_id) == 1


async def test_concurrent_two_tenants_distinct_actors():
    if not await _db_ok():
        _require_db()
    tenant_a = await _seed_tenant("ConcurrentTenantA")
    tenant_b = await _seed_tenant("ConcurrentTenantB")

    async def _resolve(tid: str) -> uuid.UUID:
        async with _test_session_factory() as session:
            actor_id = await resolve_trusted_system_actor(session, tid)
            await session.commit()
            return actor_id

    # Interleave concurrent calls for both tenants.
    tasks = [_resolve(tenant_a) for _ in range(4)] + [_resolve(tenant_b) for _ in range(4)]
    results = await asyncio.gather(*tasks)
    actor_a_set = set(results[:4])
    actor_b_set = set(results[4:])
    assert len(actor_a_set) == 1
    assert len(actor_b_set) == 1
    assert actor_a_set != actor_b_set


# ===========================================================================
# E. Reserved-name API tests
# ===========================================================================


async def test_admin_api_rejects_reserved_prefix_principal_name(monkeypatch: pytest.MonkeyPatch):
    if not await _db_ok():
        _require_db()
    tenant_id, admin_pid = await _default_tenant_principal()
    client = await _make_admin_client(tenant_id, admin_pid, monkeypatch)
    async with client:
        resp = await client.post(
            "/v1/admin/principals",
            json={
                "tenant_id": tenant_id,
                "name": "__engram_internal_review__:manual",
                "type": "system",
            },
        )
    assert resp.status_code == 422


async def test_admin_api_allows_ordinary_system_named_principal(monkeypatch: pytest.MonkeyPatch):
    if not await _db_ok():
        _require_db()
    tenant_id, admin_pid = await _default_tenant_principal()
    client = await _make_admin_client(tenant_id, admin_pid, monkeypatch)
    async with client:
        resp = await client.post(
            "/v1/admin/principals",
            json={"tenant_id": tenant_id, "name": "system", "type": "agent"},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["internal_key"] is None
    assert body["type"] == "agent"


async def test_admin_api_rejects_internal_key_in_request(monkeypatch: pytest.MonkeyPatch):
    if not await _db_ok():
        _require_db()
    tenant_id, admin_pid = await _default_tenant_principal()
    client = await _make_admin_client(tenant_id, admin_pid, monkeypatch)
    async with client:
        resp = await client.post(
            "/v1/admin/principals",
            json={
                "tenant_id": tenant_id,
                "name": "hacker",
                "type": "system",
                "internal_key": "review_automation",
            },
        )
    assert resp.status_code == 422


async def test_admin_api_rejects_invalid_principal_type(monkeypatch: pytest.MonkeyPatch):
    if not await _db_ok():
        _require_db()
    tenant_id, admin_pid = await _default_tenant_principal()
    client = await _make_admin_client(tenant_id, admin_pid, monkeypatch)
    async with client:
        resp = await client.post(
            "/v1/admin/principals",
            json={"tenant_id": tenant_id, "name": "badtype", "type": "superuser"},
        )
    assert resp.status_code == 422


# ===========================================================================
# F. API-key issuance tests
# ===========================================================================


async def _make_admin_client(
    tenant_id: str,
    principal_id: str,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> AsyncClient:
    """Build an ASGI client with the session override pointing at the test DB.

    If ``monkeypatch`` is provided, also patches the db module's session
    factories so the app shares the test event loop's engine (avoids asyncpg
    cross-loop connection errors).
    """
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
    if monkeypatch is not None:
        import engram.db as db_module

        monkeypatch.setattr(db_module, "async_session_factory", _test_session_factory)
        monkeypatch.setattr(db_module, "owner_session_factory", _test_session_factory)
        monkeypatch.setattr(db_module, "read_session_factory", _test_session_factory)
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_api_key_issuance_for_ordinary_agent_succeeds(monkeypatch: pytest.MonkeyPatch):
    if not await _db_ok():
        _require_db()
    tenant_id, admin_pid = await _default_tenant_principal()
    agent_id = await _seed_agent_principal(tenant_id, "keytarget-agent")
    client = await _make_admin_client(tenant_id, admin_pid, monkeypatch)
    async with client:
        resp = await client.post(
            "/v1/admin/api-keys",
            json={
                "tenant_id": tenant_id,
                "principal_id": agent_id,
                "scopes": ["read", "write"],
            },
        )
    assert resp.status_code == 201, resp.text


async def test_api_key_issuance_for_internal_principal_rejected(monkeypatch: pytest.MonkeyPatch):
    if not await _db_ok():
        _require_db()
    tenant_id, admin_pid = await _default_tenant_principal()
    # Create the canonical internal principal.
    async with _test_session_factory() as session:
        internal_id = await resolve_trusted_system_actor(session, tenant_id)
        await session.commit()
    client = await _make_admin_client(tenant_id, admin_pid, monkeypatch)
    async with client:
        resp = await client.post(
            "/v1/admin/api-keys",
            json={
                "tenant_id": tenant_id,
                "principal_id": str(internal_id),
                "scopes": ["read", "write"],
            },
        )
    assert resp.status_code == 409, resp.text
    # No api_keys row was created for the internal principal.
    async with _test_session_factory() as session:
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM api_keys WHERE principal_id = :pid"
                ),
                {"pid": str(internal_id)},
            )
        ).scalar_one()
    assert count == 0


# ===========================================================================
# G. Authentication fail-closed tests
# ===========================================================================


async def test_new_format_key_for_internal_principal_fails_auth(monkeypatch: pytest.MonkeyPatch):
    if not await _db_ok():
        _require_db()
    import engram.auth as auth_mod
    import engram.db as db_module

    tenant_id, _ = await _default_tenant_principal()
    async with _test_session_factory() as session:
        internal_id = await resolve_trusted_system_actor(session, tenant_id)
        await session.commit()
    plaintext, key_id = await _insert_api_key_for_principal(
        tenant_id=tenant_id, principal_id=str(internal_id), label="test-v2b3b-internal-new"
    )
    reset_principal_cache()

    # Point auth and the app session factories at the test DB so the ASGI
    # client shares the test event loop's engine.
    monkeypatch.setattr(auth_mod, "_get_session_factory", lambda: _test_session_factory)
    monkeypatch.setattr(db_module, "async_session_factory", _test_session_factory)
    monkeypatch.setattr(db_module, "owner_session_factory", _test_session_factory)
    monkeypatch.setattr(db_module, "read_session_factory", _test_session_factory)
    monkeypatch.setattr(settings, "auth_enabled", True)

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/v1/items",
            headers={"Authorization": f"Bearer {plaintext}"},
        )
    assert resp.status_code == 401
    reset_principal_cache()


async def test_legacy_key_for_internal_principal_fails_auth(monkeypatch: pytest.MonkeyPatch):
    if not await _db_ok():
        _require_db()
    import engram.auth as auth_mod
    import engram.db as db_module

    tenant_id, _ = await _default_tenant_principal()
    async with _test_session_factory() as session:
        internal_id = await resolve_trusted_system_actor(session, tenant_id)
        await session.commit()
    plaintext = await _insert_legacy_api_key_for_principal(
        tenant_id=tenant_id, principal_id=str(internal_id), label="test-v2b3b-internal-legacy"
    )
    reset_principal_cache()

    monkeypatch.setattr(auth_mod, "_get_session_factory", lambda: _test_session_factory)
    monkeypatch.setattr(db_module, "async_session_factory", _test_session_factory)
    monkeypatch.setattr(db_module, "owner_session_factory", _test_session_factory)
    monkeypatch.setattr(db_module, "read_session_factory", _test_session_factory)
    monkeypatch.setattr(settings, "auth_enabled", True)

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/v1/items",
            headers={"Authorization": f"Bearer {plaintext}"},
        )
    assert resp.status_code == 401
    reset_principal_cache()


# ===========================================================================
# H. Promotion entry-point tests
# ===========================================================================


@pytest.mark.parametrize("source", ["cli", "worker", "admin_endpoint", "startup_recall"])
async def test_every_promotion_entry_point_attributes_to_internal_actor(source: str):
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
    assert actor_row["internal_key"] == TRUSTED_REVIEW_INTERNAL_KEY


async def test_admin_endpoint_promotion_attributes_to_internal_actor(monkeypatch: pytest.MonkeyPatch):
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
    assert actor_row["internal_key"] == TRUSTED_REVIEW_INTERNAL_KEY


# ===========================================================================
# I. Conflict-recheck attribution
# ===========================================================================


async def test_conflict_recheck_event_uses_internal_actor():
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

    async def fake_classify(old_content, new_content, similarity, **_kwargs):
        return ConflictVerdict.CONTRADICT, 0.9, "forced contradiction", {}

    monkeypatch = pytest.MonkeyPatch()
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
    assert actor_row["internal_key"] == TRUSTED_REVIEW_INTERNAL_KEY


# ===========================================================================
# J. Tenant isolation
# ===========================================================================


async def test_tenant_isolation_distinct_internal_principals():
    if not await _db_ok():
        _require_db()
    tenant_a = await _seed_tenant("IsoTenantA")
    tenant_b = await _seed_tenant("IsoTenantB")
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
    assert row_a["internal_key"] == TRUSTED_REVIEW_INTERNAL_KEY
    assert row_b["internal_key"] == TRUSTED_REVIEW_INTERNAL_KEY


async def test_tenant_isolation_api_key_cross_tenant_internal_non_disclosing(
    monkeypatch: pytest.MonkeyPatch,
):
    if not await _db_ok():
        _require_db()
    tenant_a, admin_a = await _default_tenant_principal()
    tenant_b = await _seed_tenant("IsoKeyTenantB")
    # Create internal principal in tenant B.
    async with _test_session_factory() as session:
        internal_b_id = await resolve_trusted_system_actor(session, tenant_b)
        await session.commit()
    # Admin in tenant A tries to issue a key for tenant B's internal principal.
    # The validation resolves within the caller's tenant context (tenant A via
    # RLS), so the cross-tenant principal is not found — no disclosure that it
    # is internal. The key point: no 409 "internal principal" error that would
    # disclose the cross-tenant principal's internal status.
    client = await _make_admin_client(tenant_a, admin_a, monkeypatch)
    async with client:
        resp = await client.post(
            "/v1/admin/api-keys",
            json={
                "tenant_id": tenant_a,
                "principal_id": str(internal_b_id),
                "scopes": ["read"],
            },
        )
    # A 409 would disclose that the principal_id belongs to an internal
    # principal (even cross-tenant). The response must not be a 409 with an
    # internal-principal message. A 201 (key created with dangling principal_id)
    # or 422 is acceptable — neither discloses the internal status.
    if resp.status_code == 409:
        assert "internal" not in resp.text.lower(), (
            "cross-tenant internal principal status must not be disclosed"
        )