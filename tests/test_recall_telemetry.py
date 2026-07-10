"""Integration tests for async recall telemetry (ENG-AUD-011 / F18).

Startup recall no longer writes ``last_recalled_at``/``recall_count``/
``startup_recall_count`` inline in the read transaction — it enqueues a
``recall.telemetry`` job (handled by ``engram.worker.handle_recall_telemetry``)
after the recall set is selected. These tests cover:

* the read transaction leaves recall counters untouched
* the worker applies them once the job is processed
* a job retry/redelivery does not double-increment counters (idempotency via
  ``recall_logs.telemetry_applied_at``, claimed transactionally)
* a deleted/expired item does not fail telemetry processing
* enqueue failure does not fail the recall response

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
from engram.recall import execute_startup_recall

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
def _use_test_read_session_factory():
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


async def _insert_item(tenant_id: str, principal_id: str, **overrides: object) -> str:
    item_id = uuid.uuid4()
    now = datetime.now(UTC)
    row: dict[str, object] = {
        "id": item_id,
        "tenant_id": uuid.UUID(tenant_id),
        "workspace_id": None,
        "principal_id": uuid.UUID(principal_id),
        "content": "telemetry test memory",
        "content_hash": f"sha256:{uuid.uuid4().hex}",
        "kind": "fact",
        "visibility": "tenant",
        "review_status": "active",
        "memory_confidence": 0.5,
        "source_trust": 0.5,
        "importance": 0.5,
        "pinned": False,
        "human_verified": False,
        "created_at": now - timedelta(days=1),
        "valid_from": now - timedelta(days=1),
        "recall_count": 0,
        "startup_recall_count": 0,
        "last_recalled_at": None,
    }
    row.update(overrides)
    async with _test_session_factory() as session:
        await session.execute(insert(MemoryItem), [row])
        await session.commit()
    return str(item_id)


async def _run_startup_recall(tenant_id: str, principal_id: str) -> dict:
    async with _test_session_factory() as session:
        await _apply_rls(session, tenant_id, principal_id)
        return await execute_startup_recall(
            session=session,
            tenant_id=tenant_id,
            principal_id=principal_id,
            workspace=None,
            byte_budget=10_000_000,
            token_budget=None,
        )


async def _item_counters(item_id: str) -> dict[str, object]:
    async with _test_session_factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT recall_count, startup_recall_count, last_recalled_at "
                        "FROM memory_items WHERE id = :id"
                    ),
                    {"id": uuid.UUID(item_id)},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def _drain_jobs(max_iterations: int = 10) -> int:
    from engram.worker import process_one_job

    processed = 0
    for _ in range(max_iterations):
        did = await process_one_job(
            worker_id="test",
            session_factory=_test_session_factory,
            app_session_factory=_test_session_factory,
        )
        if not did:
            return processed
        processed += 1
    return processed


async def test_recall_response_does_not_write_counters_inline():
    """The read transaction leaves last_recalled_at/recall_count untouched."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(tenant_id, principal_id)

    result = await _run_startup_recall(tenant_id, principal_id)
    assert result["item_count"] == 1
    assert result["telemetry_enqueued"] is True

    counters = await _item_counters(item_id)
    assert counters["recall_count"] == 0
    assert counters["startup_recall_count"] == 0
    assert counters["last_recalled_at"] is None


async def test_worker_applies_telemetry_after_recall():
    """Once the recall.telemetry job is processed, counters are updated exactly once."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(tenant_id, principal_id)

    await _run_startup_recall(tenant_id, principal_id)
    processed = await _drain_jobs()
    assert processed == 1

    counters = await _item_counters(item_id)
    assert counters["recall_count"] == 1
    assert counters["startup_recall_count"] == 1
    assert counters["last_recalled_at"] is not None


async def test_telemetry_retry_does_not_double_increment():
    """A redelivered/retried job must not double-increment counters (requirement 8/15)."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(tenant_id, principal_id)

    await _run_startup_recall(tenant_id, principal_id)
    processed = await _drain_jobs()
    assert processed == 1
    counters_after_first = await _item_counters(item_id)
    assert counters_after_first["recall_count"] == 1

    # Simulate an at-least-once redelivery: flip the succeeded job back to
    # pending (as a lease-timeout reclaim or queue redelivery would) and let
    # the worker pick it up again.
    async with _test_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE jobs SET status = 'pending', locked_at = NULL, locked_by = NULL "
                "WHERE job_type = 'recall.telemetry'"
            )
        )
    reprocessed = await _drain_jobs()
    assert reprocessed == 1

    counters_after_retry = await _item_counters(item_id)
    assert counters_after_retry["recall_count"] == 1
    assert counters_after_retry["startup_recall_count"] == 1


async def test_telemetry_survives_item_deleted_before_processing():
    """A hard-deleted item must not fail the telemetry job (requirement 7/15)."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(tenant_id, principal_id)

    await _run_startup_recall(tenant_id, principal_id)

    async with _test_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM memory_items WHERE id = :id"), {"id": uuid.UUID(item_id)}
        )

    processed = await _drain_jobs()
    assert processed == 1

    async with _test_session_factory() as session:
        status = (
            await session.execute(
                text("SELECT status FROM jobs WHERE job_type = 'recall.telemetry'")
            )
        ).scalar_one()
    assert status == "succeeded"


async def test_telemetry_survives_item_expired_before_processing():
    """An item that expired between recall and telemetry processing still gets
    its counters bumped (harmless historical bookkeeping) without failing."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(tenant_id, principal_id)

    await _run_startup_recall(tenant_id, principal_id)

    async with _test_engine.begin() as conn:
        await conn.execute(
            text("UPDATE memory_items SET valid_to = now() WHERE id = :id"),
            {"id": uuid.UUID(item_id)},
        )

    processed = await _drain_jobs()
    assert processed == 1
    counters = await _item_counters(item_id)
    assert counters["recall_count"] == 1


async def test_telemetry_enqueue_failure_does_not_fail_recall(monkeypatch):
    """Enqueue failure is logged and swallowed; recall still returns items."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id, principal_id = await _default_tenant_principal()
    await _insert_item(tenant_id, principal_id)

    async def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("queue unavailable")

    import engram.recall as recall_module

    monkeypatch.setattr(recall_module, "enqueue_job", _boom)

    result = await _run_startup_recall(tenant_id, principal_id)
    assert result["item_count"] == 1
    assert result["telemetry_enqueued"] is False

    async with _test_session_factory() as session:
        job_count = (
            await session.execute(text("SELECT count(*) FROM jobs"))
        ).scalar_one()
    assert job_count == 0
