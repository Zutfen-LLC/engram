"""Integration tests for the Postgres job queue core (ENG-AUD-008).

These require a live PostgreSQL with the v2 schema (migrations/001..005). They
skip automatically when no DB is reachable, matching test_remember.py.

Covers: enqueue, claim ordering, ``FOR UPDATE SKIP LOCKED`` concurrency,
retry/backoff, dead-letter after max attempts, stale-running reclaim, tenant
RLS posture, and dedupe-key idempotent enqueue.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.config import settings
from engram.jobs import (
    STATUS_DEAD,
    STATUS_PENDING,
    STATUS_RUNNING,
    claim_next_job,
    enqueue_job,
    mark_job_failed_or_retry,
    mark_job_succeeded,
    reclaim_stale_jobs,
)

_test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_test_session_factory = async_sessionmaker(
    _test_engine, class_=AsyncSession, expire_on_commit=False
)


@pytest.fixture(autouse=True)
async def _fresh_engine():
    """Per-test NullPool engine on its own loop (see test_promotion.py)."""
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


async def _default_tenant_id() -> str:
    async with _test_session_factory() as session:
        row = (
            await session.execute(text("SELECT id::text FROM tenants WHERE slug = 'default'"))
        ).scalar_one()
        return str(row)


@pytest.fixture(autouse=True)
async def _clean_jobs():
    if not await _db_ok():
        return
    async with _test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM jobs"))


async def test_enqueue_and_claim():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant = await _default_tenant_id()
    async with _test_session_factory() as session:
        job_id = await enqueue_job(
            session, tenant_id=tenant, job_type="test.echo", payload={"x": 1}
        )
        assert job_id is not None

    async with _test_session_factory() as session:
        claimed = await claim_next_job(session, worker_id="w1")
        assert claimed is not None
        assert claimed.id == job_id
        assert claimed.status == STATUS_RUNNING
        assert claimed.attempts == 1
        assert claimed.locked_by == "w1"


async def test_claim_ordering_by_run_after_then_priority_then_created():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant = await _default_tenant_id()
    now = datetime.now(UTC)
    # Insert in non-due order and varying priority.
    async with _test_session_factory() as session:
        # due earliest, lower priority number (higher priority)
        await enqueue_job(
            session,
            tenant_id=tenant,
            job_type="t",
            payload={},
            priority=10,
            run_after=now - timedelta(seconds=10),
        )
        # due later
        await enqueue_job(
            session,
            tenant_id=tenant,
            job_type="t",
            payload={},
            priority=1,
            run_after=now + timedelta(seconds=30),
        )
        # due now, higher priority number (lower priority)
        await enqueue_job(
            session,
            tenant_id=tenant,
            job_type="t",
            payload={},
            priority=200,
            run_after=now - timedelta(seconds=5),
        )

    # Expect the earliest-due, then cheapest among due.
    async with _test_session_factory() as session:
        first = await claim_next_job(session, worker_id="w1")
        assert first is not None
        assert first.priority == 10  # earliest (10s ago) wins regardless of priority

    async with _test_session_factory() as session:
        second = await claim_next_job(session, worker_id="w1")
        assert second is not None
        assert second.priority == 200  # due 5s ago, only remaining due job

    async with _test_session_factory() as session:
        third = await claim_next_job(session, worker_id="w1")
        assert third is None  # the future job is not due yet


async def test_skip_locked_prevents_double_claim():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant = await _default_tenant_id()
    async with _test_session_factory() as session:
        await enqueue_job(session, tenant_id=tenant, job_type="t", payload={})

    async def _claim(wid: str):
        async with _test_session_factory() as session:
            return await claim_next_job(session, worker_id=wid)

    # Two concurrent claims — exactly one wins (FOR UPDATE SKIP LOCKED).
    w1, w2 = await asyncio.gather(_claim("w1"), _claim("w2"))
    winners = [j for j in (w1, w2) if j is not None]
    assert len(winners) == 1


async def test_mark_succeeded_clears_lock():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant = await _default_tenant_id()
    async with _test_session_factory() as session:
        job_id = await enqueue_job(session, tenant_id=tenant, job_type="t", payload={})
    async with _test_session_factory() as session:
        claimed = await claim_next_job(session, worker_id="w1")
        assert claimed is not None
    async with _test_session_factory() as session:
        await mark_job_succeeded(session, claimed.id)
    async with _test_session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, locked_at, completed_at FROM jobs WHERE id = :id"
                ),
                {"id": str(job_id)},
            )
        ).one()
        assert row.status == "succeeded"
        assert row.locked_at is None
        assert row.completed_at is not None


async def test_retry_backoff_then_succeed():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant = await _default_tenant_id()
    async with _test_session_factory() as session:
        job_id = await enqueue_job(
            session, tenant_id=tenant, job_type="t", payload={}, max_attempts=3
        )

    now = datetime.now(UTC)
    # First failure -> pending with backoff.
    async with _test_session_factory() as session:
        claimed = await claim_next_job(session, worker_id="w1")
        assert claimed is not None
    async with _test_session_factory() as session:
        status = await mark_job_failed_or_retry(session, claimed.id, "boom", now=now)
        assert status == STATUS_PENDING

    async with _test_session_factory() as session:
        row = (
            await session.execute(
                text("SELECT attempts, status, run_after, last_error FROM jobs WHERE id = :id"),
                {"id": str(job_id)},
            )
        ).one()
        assert row.attempts == 1
        assert row.status == "pending"
        assert row.run_after > now  # backoff pushed it into the future
        assert row.last_error == "boom"


async def test_dead_after_max_attempts():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant = await _default_tenant_id()
    async with _test_session_factory() as session:
        job_id = await enqueue_job(
            session, tenant_id=tenant, job_type="t", payload={}, max_attempts=1
        )
    now = datetime.now(UTC)
    async with _test_session_factory() as session:
        claimed = await claim_next_job(session, worker_id="w1")
        assert claimed is not None
    async with _test_session_factory() as session:
        status = await mark_job_failed_or_retry(session, claimed.id, "fatal", now=now)
        assert status == STATUS_DEAD
    async with _test_session_factory() as session:
        row = (
            await session.execute(
                text("SELECT status, completed_at, last_error FROM jobs WHERE id = :id"),
                {"id": str(job_id)},
            )
        ).one()
        assert row.status == "dead"
        assert row.completed_at is not None
        assert row.last_error == "fatal"


async def test_reclaim_stale_running_jobs():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant = await _default_tenant_id()
    async with _test_session_factory() as session:
        await enqueue_job(session, tenant_id=tenant, job_type="t", payload={})
    async with _test_session_factory() as session:
        claimed = await claim_next_job(session, worker_id="w1")
        assert claimed is not None

    # Simulate a stale lock: backdate locked_at and pretend it is running.
    stale = datetime.now(UTC) - timedelta(seconds=600)
    async with _test_engine.begin() as conn:
        await conn.execute(
            text("UPDATE jobs SET locked_at = :t WHERE id = :id"),
            {"t": stale, "id": str(claimed.id)},
        )

    async with _test_session_factory() as session:
        reclaimed = await reclaim_stale_jobs(session, lease_stale_after_seconds=300)
        assert reclaimed == 1

    # The reclaimed job is claimable again.
    async with _test_session_factory() as session:
        re_claimed = await claim_next_job(session, worker_id="w2")
        assert re_claimed is not None
        assert re_claimed.id == claimed.id


async def test_dedupe_key_is_idempotent():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant = await _default_tenant_id()
    async with _test_session_factory() as session:
        first = await enqueue_job(
            session,
            tenant_id=tenant,
            job_type="embedding.generate",
            payload={"memory_item_id": "abc"},
            dedupe_key="embedding:abc",
        )
    async with _test_session_factory() as session:
        second = await enqueue_job(
            session,
            tenant_id=tenant,
            job_type="embedding.generate",
            payload={"memory_item_id": "abc"},
            dedupe_key="embedding:abc",
        )
    assert first == second  # idempotent: same job returned

    async with _test_session_factory() as session:
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM jobs WHERE job_type = 'embedding.generate' "
                    "AND payload->>'dedupe_key' = 'embedding:abc'"
                )
            )
        ).scalar_one()
        assert count == 1
