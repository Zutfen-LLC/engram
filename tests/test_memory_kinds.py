"""Postgres-backed tests for the governed memory-kind registry (ENG-AUD-010 / F17).

Covers: migration/registry seeding, FK enforcement, classification vocabulary
sourcing, behavior flags (singleton / requires_review / stays_in_recall_when_disputed),
the admin CRUD surface, and backward compatibility with existing builtin kinds.

Requires a live PostgreSQL with migrations 001-007 applied. Skips automatically
when no DB is reachable, matching the pattern in test_remember.py.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.api.app import create_app
from engram.auth import Principal as AuthPrincipal
from engram.auth import get_current_principal
from engram.classification import _load_vocab_cached, invalidate_vocab_cache
from engram.config import settings
from engram.db import _DEFAULT_PRINCIPAL_NAME, _DEFAULT_TENANT_SLUG, apply_rls_context, get_session
from engram.memory_kinds import (
    BUILTIN_KIND_NAMES,
    UnknownMemoryKindError,
    get_enabled_memory_kinds,
    invalidate_memory_kind_cache,
    require_enabled_memory_kind,
    seed_builtin_kinds,
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


async def _default_tenant_id() -> str:
    async with _test_session_factory() as session:
        return (
            await session.execute(
                text("SELECT id::text FROM tenants WHERE slug = :slug"),
                {"slug": _DEFAULT_TENANT_SLUG},
            )
        ).scalar_one()


async def _get_test_session():
    async with _test_session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT t.id::text AS tenant_id, p.id::text AS principal_id "
                    "FROM tenants t "
                    "JOIN principals p ON p.tenant_id = t.id AND p.name = :principal "
                    "WHERE t.slug = :slug"
                ),
                {"slug": _DEFAULT_TENANT_SLUG, "principal": _DEFAULT_PRINCIPAL_NAME},
            )
        ).mappings().one()
        await apply_rls_context(
            session, tenant_id=row["tenant_id"], principal_id=row["principal_id"]
        )
        yield session


async def _override_get_current_principal() -> AuthPrincipal:
    """Resolve the seed default principal via our NullPool test engine.

    Admin routes go through ``require_scopes`` -> ``get_current_principal``,
    which by default opens its own session on the module-global, pooled
    ``engram.db.engine``. Pytest-asyncio gives each test function its own
    event loop, and asyncpg connections can't be reused across event loops —
    reusing that pooled engine across tests raises a spurious
    ``InterfaceError``. Overriding this dependency to use our per-test
    NullPool engine avoids that (mirrors test_error_mapping.py).
    """
    async with _test_session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT t.id::text AS tenant_id, p.id::text AS principal_id "
                    "FROM tenants t "
                    "JOIN principals p ON p.tenant_id = t.id AND p.name = :principal "
                    "WHERE t.slug = :slug"
                ),
                {"slug": _DEFAULT_TENANT_SLUG, "principal": _DEFAULT_PRINCIPAL_NAME},
            )
        ).mappings().one()
    return AuthPrincipal(
        tenant_id=row["tenant_id"],
        principal_id=row["principal_id"],
        scopes=("read", "write", "admin", "export"),
    )


@pytest.fixture
def app():
    application = create_app()
    application.dependency_overrides[get_session] = _get_test_session
    application.dependency_overrides[get_current_principal] = _override_get_current_principal
    return application


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
async def _clean_state():
    invalidate_vocab_cache()
    invalidate_memory_kind_cache()
    if await _db_ok():
        async with _test_engine.begin() as conn:
            await conn.execute(text("DELETE FROM memory_embeddings"))
            await conn.execute(text("DELETE FROM memory_items"))
            await conn.execute(
                text("DELETE FROM memory_kinds WHERE is_builtin = FALSE AND name LIKE 'test_%'")
            )
    yield
    invalidate_vocab_cache()
    invalidate_memory_kind_cache()


def _require_db():
    pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")


# ---- Migration / registry ----------------------------------------------------


async def test_default_tenant_has_all_builtin_kinds():
    if not await _db_ok():
        _require_db()
    tenant_id = await _default_tenant_id()
    async with _test_session_factory() as session:
        rows = await get_enabled_memory_kinds(session, tenant_id)
    names = {r.name for r in rows}
    assert names >= BUILTIN_KIND_NAMES


async def test_procedure_and_summary_are_registered_and_writable(client):
    if not await _db_ok():
        _require_db()
    async with _test_session_factory() as session:
        tenant_id = await _default_tenant_id()
        kind_names = {k.name for k in await get_enabled_memory_kinds(session, tenant_id)}
    assert "procedure" in kind_names
    assert "summary" in kind_names

    for kind in ("procedure", "summary"):
        resp = await client.post(
            "/v1/remember",
            json={"content": f"A {kind} memory example", "kind": kind, "source_type": "manual"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["status"] == "created"


async def test_unknown_kind_rejected_with_clear_422(client):
    if not await _db_ok():
        _require_db()
    resp = await client.post(
        "/v1/remember",
        json={"content": "content with a bogus kind", "kind": "not_a_registered_kind"},
    )
    assert resp.status_code == 422
    assert "not_a_registered_kind" in str(resp.json()["detail"])


async def test_require_enabled_memory_kind_raises_for_unknown():
    if not await _db_ok():
        _require_db()
    tenant_id = await _default_tenant_id()
    async with _test_session_factory() as session:
        with pytest.raises(UnknownMemoryKindError):
            await require_enabled_memory_kind(session, tenant_id, "totally_bogus_kind")


async def test_seed_builtin_kinds_is_idempotent():
    if not await _db_ok():
        _require_db()
    async with _test_session_factory() as session:
        new_tenant_id = uuid.uuid4()
        await session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'Seed Test', :slug)"),
            {"id": str(new_tenant_id), "slug": f"seed-test-{new_tenant_id.hex[:8]}"},
        )
        await session.commit()
        await seed_builtin_kinds(session, new_tenant_id)
        await seed_builtin_kinds(session, new_tenant_id)  # must not raise / duplicate
        await session.commit()
        count = (
            await session.execute(
                text("SELECT count(*) FROM memory_kinds WHERE tenant_id = :tid"),
                {"tid": str(new_tenant_id)},
            )
        ).scalar_one()
        assert count == len(BUILTIN_KIND_NAMES)
        await session.execute(
            text("DELETE FROM tenants WHERE id = :id"), {"id": str(new_tenant_id)}
        )
        await session.commit()


# ---- Classification vocabulary ------------------------------------------------


async def test_classifier_taxonomy_sourced_from_registry():
    if not await _db_ok():
        _require_db()
    tenant_id = await _default_tenant_id()
    async with _test_session_factory() as session:
        taxonomy, _wings, _rooms = await _load_vocab_cached(session, tenant_id)
    assert "procedure" in taxonomy
    assert "summary" in taxonomy
    for name in BUILTIN_KIND_NAMES:
        assert name in taxonomy


async def test_disabled_kind_excluded_from_classification_vocab():
    if not await _db_ok():
        _require_db()
    tenant_id = await _default_tenant_id()
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "UPDATE memory_kinds SET enabled = FALSE "
                "WHERE tenant_id = :tid AND name = 'observation'"
            ),
            {"tid": str(tenant_id)},
        )
        await session.commit()
    invalidate_vocab_cache(tenant_id)
    invalidate_memory_kind_cache(tenant_id)
    try:
        async with _test_session_factory() as session:
            taxonomy, _wings, _rooms = await _load_vocab_cached(session, tenant_id)
        assert "observation" not in taxonomy
    finally:
        async with _test_session_factory() as session:
            await session.execute(
                text(
                    "UPDATE memory_kinds SET enabled = TRUE "
                    "WHERE tenant_id = :tid AND name = 'observation'"
                ),
                {"tid": str(tenant_id)},
            )
            await session.commit()
        invalidate_vocab_cache(tenant_id)
        invalidate_memory_kind_cache(tenant_id)


# ---- Behavior flags ------------------------------------------------------------


async def test_custom_singleton_kind_supersedes(client):
    if not await _db_ok():
        _require_db()
    resp = await client.post(
        "/v1/admin/memory-kinds",
        json={
            "name": "test_singleton_kind",
            "display_name": "Test Singleton Kind",
            "singleton": True,
        },
    )
    assert resp.status_code == 201, resp.text

    first = await client.post(
        "/v1/remember",
        json={"content": "first singleton value", "kind": "test_singleton_kind"},
    )
    assert first.status_code == 201
    second = await client.post(
        "/v1/remember",
        json={"content": "second singleton value", "kind": "test_singleton_kind"},
    )
    assert second.status_code == 201
    body = second.json()
    assert body["status"] == "superseded"
    assert body["superseded_id"] == first.json()["id"]


async def test_custom_non_singleton_kind_allows_multiple(client):
    if not await _db_ok():
        _require_db()
    resp = await client.post(
        "/v1/admin/memory-kinds",
        json={
            "name": "test_nonsingleton_kind",
            "display_name": "Test Non-Singleton Kind",
            "singleton": False,
        },
    )
    assert resp.status_code == 201

    first = await client.post(
        "/v1/remember",
        json={"content": "first non-singleton value", "kind": "test_nonsingleton_kind"},
    )
    second = await client.post(
        "/v1/remember",
        json={"content": "second non-singleton value", "kind": "test_nonsingleton_kind"},
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["status"] == "created"
    assert second.json()["superseded_id"] is None


async def test_requires_review_true_starts_proposed(client):
    if not await _db_ok():
        _require_db()
    resp = await client.post(
        "/v1/admin/memory-kinds",
        json={
            "name": "test_requires_review_kind",
            "display_name": "Test Requires Review Kind",
            "requires_review": True,
        },
    )
    assert resp.status_code == 201

    # Manual + admin writes normally default to review_status='active' —
    # requires_review=True must override that.
    write = await client.post(
        "/v1/remember",
        json={
            "content": "high stakes governed content",
            "kind": "test_requires_review_kind",
            "source_type": "manual",
        },
    )
    assert write.status_code == 201
    assert write.json()["review_status"] == "proposed"


async def test_requires_review_false_preserves_default_behavior(client):
    if not await _db_ok():
        _require_db()
    resp = await client.post(
        "/v1/admin/memory-kinds",
        json={
            "name": "test_no_review_kind",
            "display_name": "Test No Review Kind",
            "requires_review": False,
        },
    )
    assert resp.status_code == 201

    write = await client.post(
        "/v1/remember",
        json={
            "content": "normal manual content",
            "kind": "test_no_review_kind",
            "source_type": "manual",
        },
    )
    assert write.status_code == 201
    assert write.json()["review_status"] == "active"


async def test_stays_in_recall_when_disputed_true_preserves_inclusion(client):
    if not await _db_ok():
        _require_db()
    resp = await client.post(
        "/v1/admin/memory-kinds",
        json={
            "name": "test_disputed_stays_kind",
            "display_name": "Test Disputed Stays Kind",
            "stays_in_recall_when_disputed": True,
        },
    )
    assert resp.status_code == 201

    write = await client.post(
        "/v1/remember",
        json={
            "content": "high stakes disputed content",
            "kind": "test_disputed_stays_kind",
            "source_type": "manual",
        },
    )
    assert write.status_code == 201
    item_id = write.json()["id"]

    async with _test_engine.begin() as conn:
        await conn.execute(
            text("UPDATE memory_items SET review_status = 'disputed' WHERE id = :id"),
            {"id": item_id},
        )

    recall = await client.post("/v1/recall", json={"mode": "startup"})
    assert recall.status_code == 200
    body = recall.json()
    ids = {item["id"] for item in body["items"]}
    assert item_id in ids
    matched = next(item for item in body["items"] if item["id"] == item_id)
    assert "disputed — pending resolution" in matched["warnings"]


async def test_stays_in_recall_when_disputed_false_excludes(client):
    if not await _db_ok():
        _require_db()
    resp = await client.post(
        "/v1/admin/memory-kinds",
        json={
            "name": "test_disputed_excluded_kind",
            "display_name": "Test Disputed Excluded Kind",
            "stays_in_recall_when_disputed": False,
        },
    )
    assert resp.status_code == 201

    write = await client.post(
        "/v1/remember",
        json={
            "content": "ordinary disputed content",
            "kind": "test_disputed_excluded_kind",
            "source_type": "manual",
        },
    )
    assert write.status_code == 201
    item_id = write.json()["id"]

    async with _test_engine.begin() as conn:
        await conn.execute(
            text("UPDATE memory_items SET review_status = 'disputed' WHERE id = :id"),
            {"id": item_id},
        )

    recall = await client.post("/v1/recall", json={"mode": "startup"})
    assert recall.status_code == 200
    ids = {item["id"] for item in recall.json()["items"]}
    assert item_id not in ids


# ---- Administration ------------------------------------------------------------


async def test_admin_can_create_custom_kind(client):
    if not await _db_ok():
        _require_db()
    resp = await client.post(
        "/v1/admin/memory-kinds",
        json={"name": "test_admin_created_kind", "display_name": "Test Admin Created Kind"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "test_admin_created_kind"
    assert body["is_builtin"] is False
    assert body["enabled"] is True


async def test_invalid_name_rejected(client):
    if not await _db_ok():
        _require_db()
    for bad_name in ("Test-Bad", "UPPERCASE", "has space", "has-dash", "", "9startswithdigit"):
        resp = await client.post(
            "/v1/admin/memory-kinds",
            json={"name": bad_name, "display_name": "Bad name"},
        )
        assert resp.status_code == 422, f"{bad_name!r} should have been rejected"


async def test_reserved_builtin_name_rejected(client):
    if not await _db_ok():
        _require_db()
    resp = await client.post(
        "/v1/admin/memory-kinds",
        json={"name": "fact", "display_name": "Shadowing fact"},
    )
    assert resp.status_code == 422


async def test_kind_name_is_immutable_no_rename_field(client):
    """PATCH has no ``name`` field at all — built-ins (and custom kinds) can
    never be renamed through the admin API."""
    if not await _db_ok():
        _require_db()
    create = await client.post(
        "/v1/admin/memory-kinds",
        json={"name": "test_immutable_name_kind", "display_name": "Original"},
    )
    assert create.status_code == 201

    resp = await client.patch(
        "/v1/admin/memory-kinds/test_immutable_name_kind",
        json={"name": "renamed_kind", "display_name": "Renamed"},
    )
    # The unknown "name" field is ignored by MemoryKindPatch; display_name applies.
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "test_immutable_name_kind"
    assert body["display_name"] == "Renamed"


async def test_disabling_kind_prevents_new_writes_but_keeps_existing_readable(client):
    if not await _db_ok():
        _require_db()
    create = await client.post(
        "/v1/admin/memory-kinds",
        json={"name": "test_disable_kind", "display_name": "Test Disable Kind"},
    )
    assert create.status_code == 201

    write = await client.post(
        "/v1/remember",
        json={"content": "content written while enabled", "kind": "test_disable_kind"},
    )
    assert write.status_code == 201
    item_id = write.json()["id"]

    disable = await client.patch(
        "/v1/admin/memory-kinds/test_disable_kind", json={"enabled": False}
    )
    assert disable.status_code == 200
    assert disable.json()["enabled"] is False

    blocked = await client.post(
        "/v1/remember",
        json={"content": "content written while disabled", "kind": "test_disable_kind"},
    )
    assert blocked.status_code == 422

    # Existing memory of the now-disabled kind remains readable.
    read = await client.get(f"/v1/items/{item_id}")
    assert read.status_code == 200
    assert read.json()["item"]["kind"] == "test_disable_kind"


async def test_registry_update_invalidates_vocabulary_cache():
    if not await _db_ok():
        _require_db()
    tenant_id = await _default_tenant_id()
    async with _test_session_factory() as session:
        await seed_builtin_kinds(session, tenant_id)  # warm/populate, no-op if seeded
        await session.commit()

    async with _test_session_factory() as session:
        taxonomy_before, _, _ = await _load_vocab_cached(session, tenant_id)
    assert "test_cache_invalidation_kind" not in taxonomy_before

    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO memory_kinds (tenant_id, name, display_name, is_builtin, enabled) "
                "VALUES (:tid, 'test_cache_invalidation_kind', 'Cache Invalidation', FALSE, TRUE)"
            ),
            {"tid": str(tenant_id)},
        )
        await session.commit()

    # Without invalidation the stale cached taxonomy would still be served.
    invalidate_memory_kind_cache(tenant_id)
    invalidate_vocab_cache(tenant_id)
    async with _test_session_factory() as session:
        taxonomy_after, _, _ = await _load_vocab_cached(session, tenant_id)
    assert "test_cache_invalidation_kind" in taxonomy_after

    async with _test_session_factory() as session:
        await session.execute(
            text(
                "DELETE FROM memory_kinds WHERE tenant_id = :tid "
                "AND name = 'test_cache_invalidation_kind'"
            ),
            {"tid": str(tenant_id)},
        )
        await session.commit()
    invalidate_memory_kind_cache(tenant_id)
    invalidate_vocab_cache(tenant_id)


# ---- Compatibility ---------------------------------------------------------


async def test_all_builtin_kinds_still_work(client):
    if not await _db_ok():
        _require_db()
    for kind in sorted(BUILTIN_KIND_NAMES):
        resp = await client.post(
            "/v1/remember",
            json={"content": f"builtin compatibility check for {kind}", "kind": kind},
        )
        assert resp.status_code == 201, f"{kind}: {resp.text}"
