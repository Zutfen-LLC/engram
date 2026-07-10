"""Integration tests for async embedding generation (ENG-AUD-008).

These require a live PostgreSQL with the v2 schema. They skip automatically when
no DB is reachable, matching test_remember.py.

Covers:
* /v1/remember enqueues embedding.generate and returns WITHOUT a provider call
* the worker processes the job and marks the embedding ready
* a failed provider call retries and eventually dead-letters
* an expired/rejected item is skipped safely
* a duplicate job is idempotent
* the conflict.check job is enqueued after the embedding is ready
* the embedding-provider-disabled mode enqueues no jobs
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import engram.embeddings as embeddings_mod
from engram.api.app import create_app
from engram.api.routes import memory as memory_routes
from engram.config import settings
from engram.db import get_session

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


async def _get_test_session() -> AsyncSession:
    async with _test_session_factory() as session:
        from engram.db import _DEFAULT_PRINCIPAL_NAME, _DEFAULT_TENANT_SLUG, apply_rls_context

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
        await apply_rls_context(
            session, tenant_id=row["tenant_id"], principal_id=row["principal_id"]
        )
        yield session


@pytest.fixture
def app():
    app = create_app()
    app.dependency_overrides[get_session] = _get_test_session
    return app


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
async def _clean_db():
    if not await _db_ok():
        return
    async with _test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM jobs"))
        await conn.execute(text("DELETE FROM memory_embeddings"))
        await conn.execute(text("DELETE FROM memory_items"))


def _provider(enabled: bool, monkeypatch) -> None:
    if enabled:
        settings.embedding_provider = "openai"
    else:
        settings.embedding_provider = "none"

    async def fake_embedding(text_value: str):
        return [0.01] * settings.embedding_dim

    if enabled:
        monkeypatch.setattr(memory_routes, "generate_embedding", fake_embedding)
        monkeypatch.setattr(embeddings_mod, "generate_embedding", fake_embedding)


async def _drain_jobs(max_iterations: int = 10) -> None:
    from engram.worker import process_one_job

    for _ in range(max_iterations):
        processed = await process_one_job(
            worker_id="test",
            session_factory=_test_session_factory,
            app_session_factory=_test_session_factory,
        )
        if not processed:
            return


async def _embedding_state(item_id: str) -> dict[str, object]:
    async with _test_session_factory() as session:
        return (
            (
                await session.execute(
                    text(
                        "SELECT embedding_status, embedding_dim, "
                        "(embedding IS NOT NULL) AS has_vector "
                        "FROM memory_embeddings WHERE memory_item_id = :id"
                    ),
                    {"id": item_id},
                )
            )
            .mappings()
            .one()
        )


async def _job_counts() -> dict[str, int]:
    async with _test_session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT job_type, status, count(*) FROM jobs "
                    "GROUP BY job_type, status"
                )
            )
        ).all()
    return {f"{r[0]}:{r[1]}": r[2] for r in rows}


async def test_remember_enqueues_embedding_job_without_provider_call(client, monkeypatch):
    """The write path returns before any provider call; a job is queued."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    _provider(True, monkeypatch)
    # If the provider were called inline, this flag flips.
    provider_called = False
    original = embeddings_mod.generate_embedding

    async def spy(text_value: str):
        nonlocal provider_called
        provider_called = True
        return await original(text_value)  # type: ignore[misc]

    monkeypatch.setattr(memory_routes, "generate_embedding", spy)

    response = await client.post(
        "/v1/remember", json={"content": "async embedding write", "kind": "fact"}
    )
    assert response.status_code == 201
    assert response.json()["status"] == "created"
    assert not provider_called, "write path must not call the provider inline"

    # A pending embedding.generate job exists; the placeholder row is pending.
    counts = await _job_counts()
    assert counts.get("embedding.generate:pending", 0) >= 1
    state = await _embedding_state(response.json()["id"])
    assert state["embedding_status"] == "pending"
    assert state["has_vector"] is False


async def test_worker_marks_embedding_ready(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    _provider(True, monkeypatch)

    response = await client.post(
        "/v1/remember", json={"content": "worker ready test", "kind": "fact"}
    )
    item_id = response.json()["id"]

    await _drain_jobs()

    state = await _embedding_state(item_id)
    assert state["embedding_status"] == "ready"
    assert state["has_vector"] is True
    assert state["embedding_dim"] == settings.embedding_dim


async def test_failed_provider_retries_then_dead(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "openai"
    settings.conflict_check_on_write = False

    call_count = 0

    async def always_fail(text_value: str):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("provider down")

    monkeypatch.setattr(embeddings_mod, "generate_embedding", always_fail)

    response = await client.post(
        "/v1/remember", json={"content": "will fail embedding", "kind": "fact"}
    )
    item_id = response.json()["id"]

    # Drain all attempts (max_attempts default 5). Each iteration claims+fails.
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

    state = await _embedding_state(item_id)
    assert state["embedding_status"] == "failed"

    async with _test_session_factory() as session:
        dead = (
            await session.execute(
                text(
                    "SELECT count(*) FROM jobs WHERE job_type = 'embedding.generate' "
                    "AND status = 'dead'"
                )
            )
        ).scalar_one()
        assert dead == 1
    assert call_count == 5


async def test_expired_item_is_skipped(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    _provider(True, monkeypatch)

    response = await client.post(
        "/v1/remember", json={"content": "soon to expire", "kind": "fact"}
    )
    item_id = response.json()["id"]

    # Invalidate the item before the job runs.
    async with _test_session_factory() as session:
        await session.execute(
            text("UPDATE memory_items SET valid_to = now() WHERE id = :id"),
            {"id": item_id},
        )
        await session.commit()

    from engram.worker import process_one_job

    await process_one_job(
        worker_id="test",
        session_factory=_test_session_factory,
        app_session_factory=_test_session_factory,
        job_types=["embedding.generate"],
    )

    state = await _embedding_state(item_id)
    # Skipped: the placeholder remains pending (no vector written).
    assert state["has_vector"] is False


async def test_duplicate_embedding_job_is_idempotent(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    _provider(True, monkeypatch)

    response = await client.post(
        "/v1/remember", json={"content": "idempotent embed", "kind": "fact"}
    )
    item_id = response.json()["id"]

    # Enqueue a second embedding.generate for the same item (different dedupe key
    # so it is not deduped at enqueue). The first job fills the vector; the
    # second must be a no-op (idempotent) and still succeed.
    from engram.jobs import enqueue_job

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
        await enqueue_job(
            session,
            tenant_id=row["tenant_id"],
            job_type="embedding.generate",
            payload={"memory_item_id": item_id},
            dedupe_key="embedding:manual-second",
        )

    await _drain_jobs()

    state = await _embedding_state(item_id)
    assert state["embedding_status"] == "ready"
    assert state["has_vector"] is True
    # No jobs left pending/running.
    counts = await _job_counts()
    pending = sum(v for k, v in counts.items() if ":pending" in k or ":running" in k)
    assert pending == 0


async def test_conflict_check_enqueued_after_ready(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    _provider(True, monkeypatch)
    settings.conflict_check_on_write = True

    await client.post(
        "/v1/remember", json={"content": "conflict enqueue check", "kind": "fact"}
    )
    await _drain_jobs()

    # The conflict.check job was enqueued and processed (succeeded).
    async with _test_session_factory() as session:
        conflict_succeeded = (
            await session.execute(
                text(
                    "SELECT count(*) FROM jobs WHERE job_type = 'conflict.check' "
                    "AND status = 'succeeded'"
                )
            )
        ).scalar_one()
        assert conflict_succeeded >= 1


async def test_provider_disabled_enqueues_no_jobs(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    _provider(False, monkeypatch)

    response = await client.post(
        "/v1/remember", json={"content": "no embeddings mode", "kind": "fact"}
    )
    assert response.status_code == 201
    counts = await _job_counts()
    assert counts.get("embedding.generate:pending", 0) == 0
    # No embedding row created at all in disabled mode.
    async with _test_session_factory() as session:
        emb_count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM memory_embeddings "
                    "WHERE memory_item_id = :id"
                ),
                {"id": response.json()["id"]},
            )
        ).scalar_one()
        assert emb_count == 0
