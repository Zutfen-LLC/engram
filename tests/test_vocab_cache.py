"""Tests for the classification vocabulary cache (ENG-AUD-008 / F20).

F20: each unclassified ``remember`` ran six DISTINCT scans for vocabulary on
every call. The cache (engram.classification) serves vocab at most once per TTL
window per tenant.

Part 1 (no DB): the cache mechanism — repeated calls hit the loader once per
TTL; different tenants do not share; TTL expiry reloads; invalidation works.
Part 2 (DB): the default taxonomy is always present in cached vocab.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.classification import (
    _DEFAULT_KIND_TAXONOMY,
    _load_rules_cached,
    _load_vocab_cached,
    invalidate_vocab_cache,
)
from engram.config import settings

_test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_test_session_factory = async_sessionmaker(
    _test_engine, class_=AsyncSession, expire_on_commit=False
)


@pytest.fixture(autouse=True)
async def _fresh_engine():
    global _test_engine, _test_session_factory
    _test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
    _test_session_factory = async_sessionmaker(
        _test_engine, class_=AsyncSession, expire_on_commit=False
    )
    yield
    await _test_engine.dispose()


@pytest.fixture(autouse=True)
def _reset_cache_and_settings(monkeypatch):
    """Start each test with a clean cache and the default TTL."""
    invalidate_vocab_cache()
    monkeypatch.setattr(settings, "vocab_cache_ttl_seconds", 120)
    yield
    invalidate_vocab_cache()


# ---- Part 1: cache mechanism (no DB) ----


async def test_repeated_calls_load_vocab_once_per_ttl(monkeypatch):
    """Two classify() calls for the same tenant execute the loader once."""
    import engram.classification as classification_mod

    load_count = 0

    async def canned_loader(session, tenant_id):
        nonlocal load_count
        load_count += 1
        return ["fact", "decision"], ["wing-a"], ["room-1"]

    monkeypatch.setattr(classification_mod, "_load_vocab", canned_loader)

    class _DummySession:
        pass

    tenant = uuid4()
    await _load_vocab_cached(_DummySession(), tenant)  # type: ignore[arg-type]
    await _load_vocab_cached(_DummySession(), tenant)  # type: ignore[arg-type]
    await _load_vocab_cached(_DummySession(), tenant)  # type: ignore[arg-type]
    assert load_count == 1, "vocab should be loaded once and served from cache"


async def test_different_tenants_do_not_share_vocab(monkeypatch):
    import engram.classification as classification_mod

    seen_tenants: list[object] = []

    async def recording_loader(session, tenant_id):
        seen_tenants.append(tenant_id)
        return ["fact"], [], []

    monkeypatch.setattr(classification_mod, "_load_vocab", recording_loader)

    a, b = uuid4(), uuid4()
    class _DummySession:
        pass

    await _load_vocab_cached(_DummySession(), a)  # type: ignore[arg-type]
    await _load_vocab_cached(_DummySession(), b)  # type: ignore[arg-type]
    await _load_vocab_cached(_DummySession(), a)  # type: ignore[arg-type]
    await _load_vocab_cached(_DummySession(), b)  # type: ignore[arg-type]
    # Two distinct tenants loaded exactly once each (no cross-tenant sharing).
    assert seen_tenants.count(a) == 1
    assert seen_tenants.count(b) == 1


async def test_ttl_expiry_reloads_vocab(monkeypatch):
    import engram.classification as classification_mod

    load_count = 0

    async def canned_loader(session, tenant_id):
        nonlocal load_count
        load_count += 1
        return ["fact"], [], []

    monkeypatch.setattr(classification_mod, "_load_vocab", canned_loader)
    monkeypatch.setattr(settings, "vocab_cache_ttl_seconds", 0)  # disable caching

    class _DummySession:
        pass

    tenant = uuid4()
    await _load_vocab_cached(_DummySession(), tenant)  # type: ignore[arg-type]
    await _load_vocab_cached(_DummySession(), tenant)  # type: ignore[arg-type]
    assert load_count == 2, "with TTL=0 every call reloads"


async def test_invalidate_forces_reload(monkeypatch):
    import engram.classification as classification_mod

    load_count = 0

    async def canned_loader(session, tenant_id):
        nonlocal load_count
        load_count += 1
        return ["fact"], [], []

    monkeypatch.setattr(classification_mod, "_load_vocab", canned_loader)

    class _DummySession:
        pass

    tenant = uuid4()
    await _load_vocab_cached(_DummySession(), tenant)  # type: ignore[arg-type]
    invalidate_vocab_cache(tenant)
    await _load_vocab_cached(_DummySession(), tenant)  # type: ignore[arg-type]
    assert load_count == 2, "invalidation forces a reload"


async def test_rules_cached_the_same_way(monkeypatch):
    import engram.classification as classification_mod

    load_count = 0

    async def canned_loader(session, tenant_id):
        nonlocal load_count
        load_count += 1
        return []

    monkeypatch.setattr(classification_mod, "_load_rules", canned_loader)

    class _DummySession:
        pass

    tenant = uuid4()
    await _load_rules_cached(_DummySession(), tenant)  # type: ignore[arg-type]
    await _load_rules_cached(_DummySession(), tenant)  # type: ignore[arg-type]
    assert load_count == 1


# ---- Part 2: default taxonomy present (DB) ----


async def _db_ok() -> bool:
    try:
        async with _test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def test_default_taxonomy_present_in_cached_vocab():
    """The cached vocab always includes the default kind taxonomy."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    # Resolve the seeded default tenant.
    async with _test_session_factory() as session:
        tenant_id = (
            await session.execute(text("SELECT id::text FROM tenants WHERE slug = 'default'"))
        ).scalar_one()
        taxonomy, _wings, _rooms = await _load_vocab_cached(session, tenant_id)
        for kind in _DEFAULT_KIND_TAXONOMY:
            assert kind in taxonomy
