"""Integration tests for the async conflict.check job handler (ENG-AUD-008).

These require a live PostgreSQL with the v2 schema. They skip automatically when
no DB is reachable.

Conflict detection runs OFF the write path as of ENG-AUD-008. These tests
exercise the handler directly (two items + a forced verdict) and assert on the
eventual memory-state transitions: dedup rejects the duplicate, auto-supersede
supersedes the old item, contradiction/scope-overlap set conflict metadata.
Also: expired items are skipped, and reruns are idempotent.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import engram.embeddings as embeddings_mod
from engram.config import settings
from engram.conflicts import ConflictVerdict

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
        await conn.execute(text("DELETE FROM memory_embeddings"))
        await conn.execute(text("DELETE FROM memory_items"))


_VECTOR_A = [1.0] + [0.0] * 1535


def _enable_embeddings(monkeypatch):
    settings.embedding_provider = "openai"

    async def fake_embedding(_text_value: str):
        return _VECTOR_A

    monkeypatch.setattr(embeddings_mod, "generate_embedding", fake_embedding)


def _stub_verdict(monkeypatch, verdict: ConflictVerdict, confidence: float = 0.9):
    async def fake_classify(old_content: str, new_content: str, similarity: float):
        return verdict, confidence, f"forced {verdict.value}", {"provider": "test"}

    monkeypatch.setattr("engram.conflicts._classify_relationship", fake_classify)


async def _ctx_and_tenant():
    async with _test_session_factory() as session:
        from engram.db import _DEFAULT_PRINCIPAL_NAME, _DEFAULT_TENANT_SLUG, apply_rls_context

        row = (
            (
                await session.execute(
                    text(
                        "SELECT t.id::text AS tenant_id, p.id::text AS principal_id "
                        "FROM tenants t JOIN principals p "
                        "ON p.tenant_id = t.id AND p.name = :principal "
                        "WHERE t.slug = :slug"
                    ),
                    {"slug": _DEFAULT_TENANT_SLUG, "principal": _DEFAULT_PRINCIPAL_NAME},
                )
            ).mappings().one()
        )
        await apply_rls_context(
            session, tenant_id=row["tenant_id"], principal_id=row["principal_id"]
        )
        return session, row["tenant_id"], row["principal_id"]


async def _insert_item(session, *, content: str, kind: str = "fact") -> str:
    import uuid as _uuid

    from engram.models import MemoryItem

    item = MemoryItem(
        content=content,
        content_hash=content,
        kind=kind,
        source_type="manual",
        review_status="active",
        visibility="workspace",
    )
    # tenant/principal come from the RLS context, but the model requires values.
    tid = (await session.execute(text("SELECT current_setting('app.tenant_id', true)"))).scalar()
    pid = (
        await session.execute(text("SELECT current_setting('app.principal_id', true)"))
    ).scalar()
    item.tenant_id = tid
    item.principal_id = pid
    item.id = _uuid.uuid4()
    session.add(item)
    await session.flush()
    return str(item.id)


async def _run_conflict_job(new_item_id: str) -> None:
    from engram.jobs import enqueue_job
    from engram.worker import process_one_job

    session, tenant_id, _ = await _ctx_and_tenant()
    await enqueue_job(
        session,
        tenant_id=tenant_id,
        job_type="conflict.check",
        payload={"memory_item_id": new_item_id},
    )
    await session.commit()

    await process_one_job(
        worker_id="test",
        session_factory=_test_session_factory,
        app_session_factory=_test_session_factory,
        job_types=["conflict.check"],
    )


async def _fields(item_id: str) -> dict[str, object]:
    async with _test_session_factory() as session:
        return (
            (
                await session.execute(
                    text(
                        "SELECT review_status, valid_to, superseded_by, "
                        "conflicts_with_item_id, conflict_type, "
                        "conflict_resolution_status "
                        "FROM memory_items WHERE id = :id"
                    ),
                    {"id": item_id},
                )
            )
            .mappings()
            .one()
        )


async def _add_ready_embedding(session, item_id: str) -> None:
    """Attach a ready embedding to an item so detect_conflicts sees similarity."""
    from engram.models import MemoryEmbedding

    tid = (
        await session.execute(text("SELECT current_setting('app.tenant_id', true)"))
    ).scalar()
    session.add(
        MemoryEmbedding(
            memory_item_id=item_id,
            tenant_id=tid,
            embedding_model="text-embedding-3-small",
            embedding_dim=settings.embedding_dim,
            embedding=_VECTOR_A,
            embedding_status="ready",
        )
    )


async def test_dedupe_rejects_new_item(monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    _enable_embeddings(monkeypatch)
    _stub_verdict(monkeypatch, ConflictVerdict.DUPLICATE, confidence=0.95)

    session, _, _ = await _ctx_and_tenant()
    old_id = await _insert_item(session, content="original dup")
    new_id = await _insert_item(session, content="reworded dup")
    # Give both items ready embeddings so detect_conflicts sees similarity.
    for iid in (old_id, new_id):
        await _add_ready_embedding(session, iid)
    await session.commit()

    await _run_conflict_job(new_id)

    new = await _fields(new_id)
    assert new["review_status"] == "rejected"
    assert new["valid_to"] is not None
    old = await _fields(old_id)
    assert old["review_status"] != "rejected"


async def test_auto_supersede_marks_old_item(monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    _enable_embeddings(monkeypatch)
    _stub_verdict(monkeypatch, ConflictVerdict.REFINE, confidence=0.9)

    session, _, _ = await _ctx_and_tenant()
    old_id = await _insert_item(session, content="auto supersede old")
    new_id = await _insert_item(session, content="auto supersede new")
    for iid in (old_id, new_id):
        await _add_ready_embedding(session, iid)
    await session.commit()

    await _run_conflict_job(new_id)

    old = await _fields(old_id)
    assert old["valid_to"] is not None
    assert str(old["superseded_by"]) == new_id


async def test_flag_sets_conflict_metadata(monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    _enable_embeddings(monkeypatch)
    _stub_verdict(monkeypatch, ConflictVerdict.CONTRADICT, confidence=0.9)

    session, _, _ = await _ctx_and_tenant()
    old_id = await _insert_item(session, content="contradict old")
    new_id = await _insert_item(session, content="contradict new")
    for iid in (old_id, new_id):
        await _add_ready_embedding(session, iid)
    await session.commit()

    await _run_conflict_job(new_id)

    new = await _fields(new_id)
    assert str(new["conflicts_with_item_id"]) == old_id
    assert new["conflict_resolution_status"] == "unresolved"
    assert new["review_status"] == "proposed"


async def test_expired_item_is_skipped(monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    _enable_embeddings(monkeypatch)
    _stub_verdict(monkeypatch, ConflictVerdict.DUPLICATE, confidence=0.95)

    session, _, _ = await _ctx_and_tenant()
    new_id = await _insert_item(session, content="expired before conflict check")
    await session.execute(
        text("UPDATE memory_items SET valid_to = :t WHERE id = :id"),
        {"t": datetime.now(UTC), "id": new_id},
    )
    await session.commit()

    # Should succeed without applying any change (skipped safely).
    await _run_conflict_job(new_id)
    new = await _fields(new_id)
    # valid_to stays set (the item was already expired); review_status untouched.
    assert new["valid_to"] is not None
    assert new["conflicts_with_item_id"] is None


async def test_conflict_rerun_is_idempotent(monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    _enable_embeddings(monkeypatch)
    _stub_verdict(monkeypatch, ConflictVerdict.REFINE, confidence=0.9)

    session, _, _ = await _ctx_and_tenant()
    old_id = await _insert_item(session, content="idempotent old")
    new_id = await _insert_item(session, content="idempotent new")
    for iid in (old_id, new_id):
        await _add_ready_embedding(session, iid)
    await session.commit()

    await _run_conflict_job(new_id)
    state_after_first = await _fields(old_id)

    # Re-enqueue and run again with the same verdict.
    from engram.jobs import enqueue_job
    from engram.worker import process_one_job

    session2, tenant_id, _ = await _ctx_and_tenant()
    await enqueue_job(
        session2,
        tenant_id=tenant_id,
        job_type="conflict.check",
        payload={"memory_item_id": new_id},
    )
    await session2.commit()
    await process_one_job(
        worker_id="test",
        session_factory=_test_session_factory,
        app_session_factory=_test_session_factory,
        job_types=["conflict.check"],
    )

    state_after_second = await _fields(old_id)
    assert state_after_second == state_after_first
