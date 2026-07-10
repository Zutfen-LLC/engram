"""Bounded SQL candidate selection tests for startup recall (ENG-AUD-011 / F18).

Demonstrates that startup recall's Python-scored population is bounded by
``settings.startup_recall_candidate_limit`` regardless of corpus size, that
pinned items survive the candidate cap, that a small fixed number of queries
runs regardless of corpus size, and that the bounded pipeline matches the old
full-corpus pipeline (:func:`engram.recall._fetch_active_items` +
:func:`engram.recall.score_item`) for corpora at/under the limit.

Requires a live PostgreSQL with the v2 schema; skips automatically when no DB
is reachable, matching the other Postgres-backed suites.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.config import settings
from engram.models import MemoryItem
from engram.recall import _fetch_active_items, execute_startup_recall, score_item

_test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_test_session_factory = async_sessionmaker(
    _test_engine, class_=AsyncSession, expire_on_commit=False
)


@pytest.fixture(autouse=True)
async def _fresh_engine():
    """Per-test NullPool engine on its own loop (see test_promotion.py/test_jobs.py)."""
    global _test_engine, _test_session_factory
    _test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
    _test_session_factory = async_sessionmaker(
        _test_engine, class_=AsyncSession, expire_on_commit=False
    )
    yield
    await _test_engine.dispose()


@pytest.fixture(autouse=True)
def _use_test_read_session_factory():
    """Point engram.db.read_session_factory at this file's per-test factory.

    Lambda indirection so this respects whatever ``_test_session_factory``
    ``_fresh_engine`` has assigned by call time, regardless of autouse fixture
    ordering (see test_item_read_eligibility.py for the same pattern).
    """
    import engram.db as db_module

    original = db_module.read_session_factory
    db_module.read_session_factory = lambda: _test_session_factory()
    yield
    db_module.read_session_factory = original


async def _db_ok() -> bool:
    try:
        async with _test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.fixture(autouse=True)
async def _clean_db():
    if not await _db_ok():
        return
    async with _test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM jobs"))
        await conn.execute(text("DELETE FROM recall_logs"))
        await conn.execute(text("DELETE FROM item_events"))
        await conn.execute(text("DELETE FROM memory_embeddings"))
        await conn.execute(text("DELETE FROM memory_items"))


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


async def _apply_rls(session: AsyncSession, tenant_id: str, principal_id: str) -> None:
    from engram.db import apply_rls_context

    await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)


def _rows(
    tenant_id: str,
    principal_id: str,
    n: int,
    *,
    pinned: int = 0,
    importance: float = 0.5,
    content_prefix: str = "memory item",
) -> list[dict[str, object]]:
    now = datetime.now(UTC)
    rows: list[dict[str, object]] = []
    for i in range(n):
        rows.append(
            {
                "id": uuid.uuid4(),
                "tenant_id": uuid.UUID(tenant_id),
                "workspace_id": None,
                "principal_id": uuid.UUID(principal_id),
                "content": f"{content_prefix} {i}",
                "content_hash": f"sha256:{uuid.uuid4().hex}",
                "kind": "fact",
                "visibility": "tenant",
                "review_status": "active",
                "memory_confidence": 0.5 + (i % 10) / 100,
                "source_trust": 0.5,
                "importance": importance,
                "pinned": i < pinned,
                "human_verified": False,
                "created_at": now - timedelta(days=i % 60),
                "valid_from": now - timedelta(days=i % 60),
            }
        )
    return rows


async def _bulk_insert(rows: list[dict[str, object]]) -> None:
    async with _test_session_factory() as session:
        await session.execute(insert(MemoryItem), rows)
        await session.commit()


async def _run_startup_recall(tenant_id: str, principal_id: str, **kwargs: object) -> dict:
    async with _test_session_factory() as session:
        await _apply_rls(session, tenant_id, principal_id)
        return await execute_startup_recall(
            session=session,
            tenant_id=tenant_id,
            principal_id=principal_id,
            workspace=None,
            byte_budget=kwargs.get("byte_budget", 10_000_000),
            token_budget=kwargs.get("token_budget"),
        )


async def test_below_candidate_limit_matches_full_corpus_scoring():
    """Corpus under the candidate limit: bounded pipeline == old full-corpus pipeline."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id, principal_id = await _default_tenant_principal()
    rows = _rows(tenant_id, principal_id, 40, importance=0.5)
    await _bulk_insert(rows)

    result = await _run_startup_recall(tenant_id, principal_id)
    bounded_ids = [item["id"] for item in result["items"]]

    async with _test_session_factory() as session:
        await _apply_rls(session, tenant_id, principal_id)
        full_corpus = await _fetch_active_items(session, tenant_id, principal_id, None)
    now = datetime.now(UTC)
    reference = sorted(
        ((i, score_item(i, None, now).score) for i in full_corpus),
        key=lambda pair: pair[1],
        reverse=True,
    )
    reference_ids = [str(i.id) for i, _ in reference]

    assert len(full_corpus) == 40
    assert result["candidate_count"] == 40
    assert bounded_ids == reference_ids


async def test_scored_population_bounded_for_large_corpus(monkeypatch):
    """Corpus far above the candidate limit: Python-scored count stays bounded."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    monkeypatch.setattr(settings, "startup_recall_candidate_limit", 60)
    tenant_id, principal_id = await _default_tenant_principal()
    total_corpus = 900
    await _bulk_insert(_rows(tenant_id, principal_id, total_corpus))

    result = await _run_startup_recall(tenant_id, principal_id)

    assert result["candidate_count"] <= 60
    assert result["scored_count"] <= 60
    assert result["candidate_count"] < total_corpus
    assert result["candidate_strategy_version"] == "startup-candidates-v1"


async def test_query_count_bounded_regardless_of_corpus_size(monkeypatch):
    """The number of candidate-selection queries does not grow with corpus size."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    monkeypatch.setattr(settings, "startup_recall_candidate_limit", 60)
    tenant_id, principal_id = await _default_tenant_principal()

    await _bulk_insert(_rows(tenant_id, principal_id, 120, content_prefix="small corpus"))
    small_result = await _run_startup_recall(tenant_id, principal_id)
    small_queries = small_result["candidate_stats"]["query_count"]

    async with _test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM recall_logs"))
        await conn.execute(text("DELETE FROM memory_items"))
    await _bulk_insert(_rows(tenant_id, principal_id, 2000, content_prefix="large corpus"))
    large_result = await _run_startup_recall(tenant_id, principal_id)
    large_queries = large_result["candidate_stats"]["query_count"]

    assert small_queries == large_queries
    # 1 disputed-kind lookup + 1 pinned + 4 sub-pools = 6, fixed regardless of N.
    assert large_queries <= 10


async def test_pinned_items_never_displaced_by_candidate_cap(monkeypatch):
    """Pinned items survive even a very small candidate limit."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    monkeypatch.setattr(settings, "startup_recall_candidate_limit", 25)
    tenant_id, principal_id = await _default_tenant_principal()
    pinned_rows = _rows(
        tenant_id, principal_id, 15, pinned=15, importance=0.9, content_prefix="pinned"
    )
    unpinned_rows = _rows(
        tenant_id, principal_id, 600, importance=0.95, content_prefix="unpinned-high-importance"
    )
    await _bulk_insert(pinned_rows + unpinned_rows)

    # Generous budget so pinned packing doesn't drop any for byte-budget reasons.
    result = await _run_startup_recall(tenant_id, principal_id, byte_budget=10_000_000)

    returned_ids = {item["id"] for item in result["items"]}
    pinned_ids = {str(r["id"]) for r in pinned_rows}
    assert pinned_ids <= returned_ids
    assert result["pinned_omitted_count"] == 0


async def test_candidates_diversify_beyond_coarse_score(monkeypatch):
    """A never-recalled, high-importance item outside the coarse top-N still survives.

    Sets up a corpus where the coarse-score sub-pool alone (bounded to a small
    fraction of the tiny candidate limit) would not reach a deliberately
    "buried" high-importance item — but the importance sub-pool should still
    pick it up, per the diversified candidate-pool strategy (requirement 6).
    """
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    monkeypatch.setattr(settings, "startup_recall_candidate_limit", 20)
    tenant_id, principal_id = await _default_tenant_principal()

    # Many mediocre-but-slightly-higher-coarse-score items to fill the coarse
    # sub-pool (60% of 20 = 12 slots) ahead of the buried item.
    filler_rows = _rows(
        tenant_id, principal_id, 200, importance=0.6, content_prefix="filler"
    )
    buried = _rows(tenant_id, principal_id, 1, importance=0.99, content_prefix="buried-important")
    await _bulk_insert(filler_rows + buried)

    result = await _run_startup_recall(tenant_id, principal_id, byte_budget=10_000_000)
    returned_ids = {item["id"] for item in result["items"]}
    assert str(buried[0]["id"]) in returned_ids
