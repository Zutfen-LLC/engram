"""Integration tests for embedding backfill (BL-006).

These tests require a live PostgreSQL with the v2 schema
(migrations/001_init.sql) and pgvector. They skip automatically when no DB is
reachable, mirroring tests/test_promotion.py.

The OpenAI provider is mocked with deterministic 1536-dim vectors — no
network calls are made. Tests cover: dry-run reporting, pending→ready,
missing-row creation, idempotency, batch/limit, provider failure handling,
the failed-row skip / --retry-failed contract, tenant isolation, the
provider-disabled paths (dry-run still scans; real run returns nonzero), and
the CLI wrapper.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.config import settings
from engram.embeddings import EXIT_PROVIDER_DISABLED, backfill_embeddings

# Module-global engine/factory, recreated per test by the ``_fresh_engine``
# autouse fixture (see test_promotion.py for the asyncpg cross-loop rationale).
_test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_test_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    _test_engine, class_=AsyncSession, expire_on_commit=False
)


@pytest.fixture(autouse=True)
async def _fresh_engine():
    """Give each test a brand-new NullPool engine on its own loop."""
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


_DB_SKIP = "requires a live PostgreSQL with the v2 schema (run docker compose up)"


@pytest.fixture(autouse=True)
async def _clean_db():
    if not await _db_ok():
        return
    async with _test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM memory_embeddings"))
        await conn.execute(text("DELETE FROM memory_items"))


@pytest.fixture(autouse=True)
def _reset_embedding_provider():
    """Restore embedding_provider/conflict settings mutated by individual tests."""
    original_provider = settings.embedding_provider
    original_conflict = settings.conflict_check_on_write
    settings.conflict_check_on_write = False  # avoid setup-time auto-dedup
    yield
    settings.embedding_provider = original_provider
    settings.conflict_check_on_write = original_conflict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Deterministic 1536-dim vectors. The exact values don't matter for backfill
# (it doesn't query vectors, only writes them) — they just need the right
# dimension for the vector(1536) column.
_VEC_OK = [1.0] + [0.0] * 1535
_VEC_FAIL_TRIGGER = "backfill-fail-item"


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


async def _insert_item(
    *,
    tenant_id: str,
    principal_id: str,
    content: str,
    review_status: str = "active",
    valid_to: datetime | None = None,
    content_hash: str | None = None,
) -> str:
    item_id = str(uuid.uuid4())
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO memory_items ("
                "id, tenant_id, principal_id, content, content_hash, kind, "
                "visibility, review_status, memory_confidence, source_trust, "
                "importance, source_type, valid_from, created_at"
                ") VALUES ("
                ":id, :tenant_id, :principal_id, :content, :content_hash, 'fact', "
                "'workspace', :review_status, 0.5, 0.5, 0.5, 'manual', "
                "now(), now()"
                ")"
            ),
            {
                "id": item_id,
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "content": content,
                "content_hash": content_hash or f"sha256:{uuid.uuid4().hex}",
                "review_status": review_status,
                "valid_to": valid_to,
            },
        )
        await session.commit()
    return item_id


async def _insert_embedding_row(
    *,
    memory_item_id: str,
    tenant_id: str,
    status: str,
    model: str = "text-embedding-3-small",
    with_vector: bool = False,
) -> str:
    """Insert a memory_embeddings row with explicit status/vector control."""
    emb_id = str(uuid.uuid4())
    # Use CAST(:vec AS vector) rather than ":vec::vector": the adjacent "::"
    # after the named bind confuses the asyncpg dialect (it leaves the param
    # unconverted → syntax error). pgvector accepts the text literal cast.
    vec_sql = "CAST(:vec AS vector)" if with_vector else "NULL"
    vec_literal = "[" + ",".join(f"{v:.1f}" for v in _VEC_OK) + "]" if with_vector else None
    async with _test_session_factory() as session:
        await session.execute(
            text(
                f"INSERT INTO memory_embeddings ("
                "id, memory_item_id, tenant_id, embedding_model, embedding_dim, "
                f"embedding, embedding_status, embedded_at"
                f") VALUES ("
                ":id, :memory_item_id, :tenant_id, :model, 1536, "
                f"{vec_sql}, :status, now()"
                ")"
            ),
            {
                "id": emb_id,
                "memory_item_id": memory_item_id,
                "tenant_id": tenant_id,
                "model": model,
                "status": status,
                "vec": vec_literal,
            },
        )
        await session.commit()
    return emb_id


def _patch_backfill_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the backfill module's generate_embeddings with a deterministic fake.

    The fake mirrors a real batched provider call: it raises for the whole
    batch when any input is the failure trigger, otherwise returns one vector
    per input in order. backfill resolves ``generate_embeddings`` at call time,
    so patching the module global is sufficient.
    """
    from engram import embeddings as embeddings_mod

    async def fake_embeddings(
        texts: list[str], *_args: object, **_kwargs: object
    ) -> list[list[float] | None]:
        # A whole batch fails if any input is the trigger (batched providers
        # are all-or-nothing per request); use --batch-size 1 to isolate a
        # single bad item into its own batch.
        if any(text_value.startswith(_VEC_FAIL_TRIGGER) for text_value in texts):
            raise RuntimeError("simulated provider failure")
        return [list(_VEC_OK) for _ in texts]

    monkeypatch.setattr(embeddings_mod, "generate_embeddings", fake_embeddings)


async def _embedding_state(item_id: str) -> dict[str, Any]:
    async with _test_session_factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT embedding_status, embedding_dim, "
                        "embedding IS NOT NULL AS has_vector "
                        "FROM memory_embeddings WHERE memory_item_id = :id "
                        "ORDER BY embedded_at DESC LIMIT 1"
                    ),
                    {"id": item_id},
                )
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        return {"exists": False}
    return {"exists": True, **dict(row)}


async def _count_embedding_rows(tenant_id: str) -> int:
    async with _test_session_factory() as session:
        return int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM memory_embeddings "
                        "WHERE tenant_id = :tid AND embedding_model = 'text-embedding-3-small'"
                    ),
                    {"tid": tenant_id},
                )
            ).scalar_one()
        )


async def _run_with_tenant(tenant_id: str, **kwargs: Any):
    """Run backfill inside a session with RLS context set to ``tenant_id``."""
    async with _test_session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
        )
        return await backfill_embeddings(session, tenant_id, **kwargs)


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------


async def test_dry_run_reports_work_without_mutating(monkeypatch):
    if not await _db_ok():
        pytest.skip(_DB_SKIP)
    _patch_backfill_embedding(monkeypatch)
    settings.embedding_provider = "openai"
    tenant_id, principal_id = await _default_tenant_principal()

    pending_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="pending one"
    )
    await _insert_embedding_row(
        memory_item_id=pending_id, tenant_id=tenant_id, status="pending"
    )
    # Missing-row item (no embedding row at all).
    await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="missing one"
    )
    rows_before = await _count_embedding_rows(tenant_id)

    result = await _run_with_tenant(tenant_id, dry_run=True)

    assert result.dry_run is True
    assert result.scanned == 2
    assert result.would_populate == 1
    assert result.would_create == 1
    assert result.created == 0
    assert result.populated == 0
    # Nothing written.
    assert await _count_embedding_rows(tenant_id) == rows_before
    st = await _embedding_state(pending_id)
    assert st["exists"] is True and st["has_vector"] is False
    assert st["embedding_status"] == "pending"


async def test_dry_run_with_provider_disabled_still_reports(monkeypatch):
    """--dry-run scans and reports even when the provider is 'none'."""
    if not await _db_ok():
        pytest.skip(_DB_SKIP)
    _patch_backfill_embedding(monkeypatch)
    settings.embedding_provider = "none"  # disabled
    tenant_id, principal_id = await _default_tenant_principal()

    pending_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="pending disabled"
    )
    await _insert_embedding_row(
        memory_item_id=pending_id, tenant_id=tenant_id, status="pending"
    )
    await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="missing disabled"
    )
    rows_before = await _count_embedding_rows(tenant_id)

    result = await _run_with_tenant(tenant_id, dry_run=True)

    assert result.dry_run is True
    assert result.provider_enabled is False
    assert result.scanned == 2
    assert result.would_create == 1
    assert result.would_populate == 1
    assert result.message is not None and "provider" in result.message.lower()
    # Still no writes.
    assert await _count_embedding_rows(tenant_id) == rows_before


# ---------------------------------------------------------------------------
# Real runs (provider enabled)
# ---------------------------------------------------------------------------


async def test_populates_pending_to_ready(monkeypatch):
    if not await _db_ok():
        pytest.skip(_DB_SKIP)
    _patch_backfill_embedding(monkeypatch)
    settings.embedding_provider = "openai"
    tenant_id, principal_id = await _default_tenant_principal()

    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="pending to ready"
    )
    await _insert_embedding_row(memory_item_id=item_id, tenant_id=tenant_id, status="pending")

    result = await _run_with_tenant(tenant_id)

    assert result.populated == 1
    assert result.created == 0
    assert result.failed == 0
    st = await _embedding_state(item_id)
    assert st["embedding_status"] == "ready"
    assert st["has_vector"] is True
    assert st["embedding_dim"] == 1536


async def test_creates_missing_embedding_row(monkeypatch):
    if not await _db_ok():
        pytest.skip(_DB_SKIP)
    _patch_backfill_embedding(monkeypatch)
    settings.embedding_provider = "openai"
    tenant_id, principal_id = await _default_tenant_principal()

    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="missing row item"
    )
    assert (await _embedding_state(item_id))["exists"] is False

    result = await _run_with_tenant(tenant_id)

    assert result.created == 1
    assert result.populated == 0
    st = await _embedding_state(item_id)
    assert st["exists"] is True
    assert st["embedding_status"] == "ready"
    assert st["has_vector"] is True


async def test_skips_already_ready(monkeypatch):
    if not await _db_ok():
        pytest.skip(_DB_SKIP)
    _patch_backfill_embedding(monkeypatch)
    settings.embedding_provider = "openai"
    tenant_id, principal_id = await _default_tenant_principal()

    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="already ready"
    )
    await _insert_embedding_row(
        memory_item_id=item_id, tenant_id=tenant_id, status="ready", with_vector=True
    )

    result = await _run_with_tenant(tenant_id)

    assert result.scanned == 0
    assert result.populated == 0
    assert result.created == 0
    assert result.skipped_ready == 1
    # Untouched.
    st = await _embedding_state(item_id)
    assert st["embedding_status"] == "ready" and st["has_vector"] is True


async def test_legacy_complete_with_vector_counts_as_ready(monkeypatch):
    """A migration-default 'complete' row that already has a vector is counted
    as skipped_ready (a populated vector is effectively ready) and not re-embedded."""
    if not await _db_ok():
        pytest.skip(_DB_SKIP)
    _patch_backfill_embedding(monkeypatch)
    settings.embedding_provider = "openai"
    tenant_id, principal_id = await _default_tenant_principal()

    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="legacy complete"
    )
    await _insert_embedding_row(
        memory_item_id=item_id, tenant_id=tenant_id, status="complete", with_vector=True
    )
    rows_before = await _count_embedding_rows(tenant_id)

    result = await _run_with_tenant(tenant_id)

    # Visible in the summary as skipped_ready, not invisible; and untouched.
    assert result.scanned == 0
    assert result.skipped_ready == 1
    assert result.populated == 0
    assert await _count_embedding_rows(tenant_id) == rows_before
    st = await _embedding_state(item_id)
    assert st["embedding_status"] == "complete" and st["has_vector"] is True


async def test_idempotent_second_run(monkeypatch):
    if not await _db_ok():
        pytest.skip(_DB_SKIP)
    _patch_backfill_embedding(monkeypatch)
    settings.embedding_provider = "openai"
    tenant_id, principal_id = await _default_tenant_principal()

    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="idempotent item"
    )
    await _insert_embedding_row(memory_item_id=item_id, tenant_id=tenant_id, status="pending")

    first = await _run_with_tenant(tenant_id)
    assert first.populated == 1

    second = await _run_with_tenant(tenant_id)
    assert second.scanned == 0
    assert second.populated == 0
    assert second.created == 0
    assert second.skipped_ready == 1


async def test_batch_size_and_limit_honored(monkeypatch):
    if not await _db_ok():
        pytest.skip(_DB_SKIP)
    _patch_backfill_embedding(monkeypatch)
    settings.embedding_provider = "openai"
    tenant_id, principal_id = await _default_tenant_principal()

    # Two pending items; --limit 1 should process exactly one.
    a = await _insert_item(tenant_id=tenant_id, principal_id=principal_id, content="limit a")
    b = await _insert_item(tenant_id=tenant_id, principal_id=principal_id, content="limit b")
    await _insert_embedding_row(memory_item_id=a, tenant_id=tenant_id, status="pending")
    await _insert_embedding_row(memory_item_id=b, tenant_id=tenant_id, status="pending")

    result = await _run_with_tenant(tenant_id, limit=1, batch_size=1)

    assert result.batch_size == 1
    # The shared limit caps the total at one candidate (pending-first), so only
    # one row is gathered and processed; the other stays pending.
    assert result.scanned == 1
    assert result.populated == 1
    states = {a: (await _embedding_state(a))["embedding_status"],
              b: (await _embedding_state(b))["embedding_status"]}
    assert "ready" in states.values()
    assert "pending" in states.values()


async def test_batch_size_groups_provider_calls(monkeypatch):
    """batch_size actually groups items into one provider call per batch."""
    if not await _db_ok():
        pytest.skip(_DB_SKIP)
    from engram import embeddings as embeddings_mod

    settings.embedding_provider = "openai"
    tenant_id, principal_id = await _default_tenant_principal()

    call_sizes: list[int] = []

    async def fake_embeddings(
        texts: list[str], *_args: object, **_kwargs: object
    ) -> list[list[float] | None]:
        call_sizes.append(len(texts))
        return [list(_VEC_OK) for _ in texts]

    monkeypatch.setattr(embeddings_mod, "generate_embeddings", fake_embeddings)

    items = [
        await _insert_item(
            tenant_id=tenant_id, principal_id=principal_id, content=f"batch item {n}"
        )
        for n in range(3)
    ]
    for it in items:
        await _insert_embedding_row(memory_item_id=it, tenant_id=tenant_id, status="pending")

    # 3 items in batches of 2 → one call of size 2 then one of size 1.
    result = await _run_with_tenant(tenant_id, batch_size=2)

    assert call_sizes == [2, 1]
    assert result.populated == 3
    assert result.batch_size == 2


async def test_limit_shared_across_populations(monkeypatch):
    """--limit caps the TOTAL across pending + missing, applied pending-first."""
    if not await _db_ok():
        pytest.skip(_DB_SKIP)
    _patch_backfill_embedding(monkeypatch)
    settings.embedding_provider = "openai"
    tenant_id, principal_id = await _default_tenant_principal()

    # Two pending rows + two missing-row items. limit=3 → pending consumes its
    # 2, the missing population gets only the remaining 1 of its 2.
    p1 = await _insert_item(tenant_id=tenant_id, principal_id=principal_id, content="shared p1")
    p2 = await _insert_item(tenant_id=tenant_id, principal_id=principal_id, content="shared p2")
    await _insert_embedding_row(memory_item_id=p1, tenant_id=tenant_id, status="pending")
    await _insert_embedding_row(memory_item_id=p2, tenant_id=tenant_id, status="pending")
    m1 = await _insert_item(tenant_id=tenant_id, principal_id=principal_id, content="shared m1")
    m2 = await _insert_item(tenant_id=tenant_id, principal_id=principal_id, content="shared m2")

    result = await _run_with_tenant(tenant_id, limit=3)

    # pending (2) + missing (1) = 3 gathered and processed; nothing discarded.
    assert result.scanned == 3
    assert result.populated == 2
    assert result.created == 1
    # m1 (the one missing item inside the budget) gained a ready row...
    st_m1 = await _embedding_state(m1)
    assert st_m1["exists"] is True and st_m1["embedding_status"] == "ready"
    # ...while m2 was never collected (budget exhausted), so it gained no row.
    assert (await _embedding_state(m2))["exists"] is False


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


async def test_provider_failure_marks_failed(monkeypatch):
    if not await _db_ok():
        pytest.skip(_DB_SKIP)
    _patch_backfill_embedding(monkeypatch)
    settings.embedding_provider = "openai"
    tenant_id, principal_id = await _default_tenant_principal()

    fail_item = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content=_VEC_FAIL_TRIGGER + " should fail",
    )
    ok_item = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="should succeed"
    )
    await _insert_embedding_row(memory_item_id=fail_item, tenant_id=tenant_id, status="pending")
    await _insert_embedding_row(memory_item_id=ok_item, tenant_id=tenant_id, status="pending")

    # batch_size=1 isolates each item into its own provider call, so the one
    # bad item fails its batch while the good item still succeeds.
    result = await _run_with_tenant(tenant_id, batch_size=1)

    assert result.populated == 1
    assert result.failed == 1
    assert fail_item in result.failed_items
    assert (await _embedding_state(fail_item))["embedding_status"] == "failed"
    assert (await _embedding_state(ok_item))["embedding_status"] == "ready"


async def test_failed_rows_skipped_by_default(monkeypatch):
    """A pre-existing 'failed' row is skipped (not retried) by default."""
    if not await _db_ok():
        pytest.skip(_DB_SKIP)
    _patch_backfill_embedding(monkeypatch)
    settings.embedding_provider = "openai"
    tenant_id, principal_id = await _default_tenant_principal()

    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="previously failed"
    )
    await _insert_embedding_row(memory_item_id=item_id, tenant_id=tenant_id, status="failed")

    result = await _run_with_tenant(tenant_id)

    assert result.scanned == 0  # failed not in the default work set
    assert result.populated == 0
    assert result.skipped_failed == 1
    assert (await _embedding_state(item_id))["embedding_status"] == "failed"


async def test_retry_failed_includes_failed_rows(monkeypatch):
    if not await _db_ok():
        pytest.skip(_DB_SKIP)
    _patch_backfill_embedding(monkeypatch)
    settings.embedding_provider = "openai"
    tenant_id, principal_id = await _default_tenant_principal()

    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="retry me now"
    )
    await _insert_embedding_row(memory_item_id=item_id, tenant_id=tenant_id, status="failed")

    result = await _run_with_tenant(tenant_id, retry_failed=True)

    assert result.scanned == 1
    assert result.populated == 1
    assert result.skipped_failed == 0  # counted as work, not skipped
    assert (await _embedding_state(item_id))["embedding_status"] == "ready"


async def test_fail_fast_raises(monkeypatch):
    if not await _db_ok():
        pytest.skip(_DB_SKIP)
    _patch_backfill_embedding(monkeypatch)
    settings.embedding_provider = "openai"
    tenant_id, principal_id = await _default_tenant_principal()

    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content=_VEC_FAIL_TRIGGER + " fail fast",
    )
    await _insert_embedding_row(memory_item_id=item_id, tenant_id=tenant_id, status="pending")

    with pytest.raises(RuntimeError, match="simulated provider failure"):
        await _run_with_tenant(tenant_id, fail_fast=True)


# ---------------------------------------------------------------------------
# Tenant isolation + provider-disabled real run
# ---------------------------------------------------------------------------


async def test_tenant_isolation(monkeypatch):
    if not await _db_ok():
        pytest.skip(_DB_SKIP)
    _patch_backfill_embedding(monkeypatch)
    settings.embedding_provider = "openai"
    tid_a, pid_a = await _default_tenant_principal()

    # Create tenant B.
    tid_b = str(uuid.uuid4())
    pid_b = str(uuid.uuid4())
    async with _test_session_factory() as session:
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
        await session.commit()

    item_a = await _insert_item(
        tenant_id=tid_a, principal_id=pid_a, content="tenant A pending"
    )
    await _insert_embedding_row(memory_item_id=item_a, tenant_id=tid_a, status="pending")
    item_b = await _insert_item(
        tenant_id=tid_b, principal_id=pid_b, content="tenant B pending"
    )
    await _insert_embedding_row(memory_item_id=item_b, tenant_id=tid_b, status="pending")

    result = await _run_with_tenant(tid_a)

    assert result.tenant_id == tid_a
    assert result.populated == 1
    # Tenant B untouched.
    assert (await _embedding_state(item_b))["embedding_status"] == "pending"


async def test_real_run_provider_disabled_returns_nonzero(monkeypatch):
    """A real (non-dry-run) run with provider=none writes nothing and signals it."""
    if not await _db_ok():
        pytest.skip(_DB_SKIP)
    _patch_backfill_embedding(monkeypatch)
    settings.embedding_provider = "none"
    tenant_id, principal_id = await _default_tenant_principal()

    pending_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="real run disabled"
    )
    await _insert_embedding_row(
        memory_item_id=pending_id, tenant_id=tenant_id, status="pending"
    )
    rows_before = await _count_embedding_rows(tenant_id)

    result = await _run_with_tenant(tenant_id)

    assert result.dry_run is False
    assert result.provider_enabled is False
    assert result.populated == 0
    assert result.created == 0
    assert result.message is not None and "provider" in result.message.lower()
    # Nothing written.
    assert await _count_embedding_rows(tenant_id) == rows_before
    assert (await _embedding_state(pending_id))["embedding_status"] == "pending"
    # The constant the CLI maps this to is nonzero.
    assert EXIT_PROVIDER_DISABLED != 0


# ---------------------------------------------------------------------------
# CLI wrapper
# ---------------------------------------------------------------------------


async def test_cli_backfill_runs_and_prints_summary(monkeypatch, capsys):
    if not await _db_ok():
        pytest.skip(_DB_SKIP)
    _patch_backfill_embedding(monkeypatch)
    settings.embedding_provider = "openai"
    from engram.cli import _run_backfill

    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="cli backfill item"
    )
    await _insert_embedding_row(memory_item_id=item_id, tenant_id=tenant_id, status="pending")

    rc = await _run_backfill(
        tenant_id,
        limit=None,
        batch_size=100,
        dry_run=False,
        fail_fast=False,
        retry_failed=False,
        session_factory=_test_session_factory,
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert f"tenant={tenant_id}" in out
    assert "populated=1" in out
    assert "Total:" in out
    assert (await _embedding_state(item_id))["embedding_status"] == "ready"


async def test_cli_dry_run_returns_zero_when_provider_disabled(monkeypatch, capsys):
    if not await _db_ok():
        pytest.skip(_DB_SKIP)
    _patch_backfill_embedding(monkeypatch)
    settings.embedding_provider = "none"
    from engram.cli import _run_backfill

    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="cli dry-run disabled"
    )
    await _insert_embedding_row(memory_item_id=item_id, tenant_id=tenant_id, status="pending")

    rc = await _run_backfill(
        tenant_id,
        limit=None,
        batch_size=100,
        dry_run=True,
        fail_fast=False,
        retry_failed=False,
        session_factory=_test_session_factory,
    )
    out = capsys.readouterr().out
    # Dry-run always returns 0 even when provider is disabled.
    assert rc == 0
    assert "dry_run=true" in out
    assert "would_populate=1" in out


async def test_cli_real_run_provider_disabled_returns_nonzero(monkeypatch, capsys):
    if not await _db_ok():
        pytest.skip(_DB_SKIP)
    _patch_backfill_embedding(monkeypatch)
    settings.embedding_provider = "none"
    from engram.cli import _run_backfill

    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="cli real disabled"
    )
    await _insert_embedding_row(memory_item_id=item_id, tenant_id=tenant_id, status="pending")

    rc = await _run_backfill(
        tenant_id,
        limit=None,
        batch_size=100,
        dry_run=False,
        fail_fast=False,
        retry_failed=False,
        session_factory=_test_session_factory,
    )
    out = capsys.readouterr().out
    assert rc == EXIT_PROVIDER_DISABLED
    assert "provider=disabled" in out
    assert "populated=0" in out
    # Pending row untouched.
    assert (await _embedding_state(item_id))["embedding_status"] == "pending"


async def test_cli_retry_failed_flag(monkeypatch, capsys):
    if not await _db_ok():
        pytest.skip(_DB_SKIP)
    _patch_backfill_embedding(monkeypatch)
    settings.embedding_provider = "openai"
    from engram.cli import _run_backfill

    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="cli retry failed"
    )
    await _insert_embedding_row(memory_item_id=item_id, tenant_id=tenant_id, status="failed")

    rc = await _run_backfill(
        tenant_id,
        limit=None,
        batch_size=100,
        dry_run=False,
        fail_fast=False,
        retry_failed=True,
        session_factory=_test_session_factory,
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "populated=1" in out
    assert "skipped_failed=0" in out


def test_cli_rejects_oversized_batch_size(monkeypatch, capsys):
    """--batch-size above the provider cap is rejected before any run (no DB needed)."""
    from engram import cli as cli_mod
    from engram.embeddings import MAX_PROVIDER_BATCH_SIZE

    monkeypatch.setattr(
        sys, "argv", ["engram", "backfill-embeddings", "--batch-size", "999999"]
    )
    with pytest.raises(SystemExit) as exc:
        cli_mod.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "batch-size" in err
    assert str(MAX_PROVIDER_BATCH_SIZE) in err
