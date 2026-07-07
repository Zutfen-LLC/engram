"""Tests for POST /v1/search — keyword, semantic, hybrid, and default filters."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.api.app import create_app
from engram.api.routes import memory as memory_routes
from engram.config import settings
from engram.db import get_session

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


async def _get_test_session() -> AsyncSession:
    async with _test_session_factory() as session:
        from sqlalchemy import text as sa_text

        from engram.db import _DEFAULT_PRINCIPAL_NAME, _DEFAULT_TENANT_SLUG

        row = (
            (
                await session.execute(
                    sa_text(
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
        await session.execute(
            sa_text("SELECT set_config(app.tenant_id, :tid, true)"),
            {"tid": row["tenant_id"]},
        )
        await session.execute(
            sa_text("SELECT set_config(app.principal_id, :pid, true)"),
            {"pid": row["principal_id"]},
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
        await conn.execute(text("DELETE FROM memory_embeddings"))
        await conn.execute(text("DELETE FROM memory_items"))


@pytest.fixture(autouse=True)
def _reset_embedding_provider():
    original = settings.embedding_provider
    yield
    settings.embedding_provider = original


async def _remember(client: AsyncClient, content: str, **payload: object):
    body = {"content": content, "source_type": "manual"}
    body.update(payload)
    return await client.post("/v1/remember", json=body)


async def _memory_embeddings_rows() -> list[dict[str, object]]:
    async with _test_engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT
                    embedding_model,
                    embedding_dim,
                    embedding_status,
                    embedding IS NOT NULL AS has_embedding
                FROM memory_embeddings
                ORDER BY embedded_at ASC
                """
            )
        )
        return [dict(row) for row in result.mappings().all()]


async def test_keyword_search_returns_active_match(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "none"
    remember = await _remember(client, "keyword search target alpha")
    assert remember.status_code == 201

    response = await client.post("/v1/search", json={"query": "keyword target", "mode": "keyword"})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert [item["content"] for item in body["results"]] == ["keyword search target alpha"]
    assert body["message"] is None


async def test_search_returns_only_active_items_by_default(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "none"
    active = await _remember(client, "shared keyword filter item")
    proposed = await _remember(
        client,
        "shared keyword filter item",
        source_type="extraction",
    )
    assert active.status_code == 201
    assert proposed.status_code == 201

    response = await client.post(
        "/v1/search", json={"query": "shared keyword filter", "mode": "keyword"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert [item["content"] for item in body["results"]] == ["shared keyword filter item"]


async def test_semantic_search_returns_best_match_and_ready_embedding_row(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "openai"
    target = [1.0] + [0.0] * 1535
    distractor = [0.0, 1.0] + [0.0] * 1534

    async def fake_embedding(text_value: str):
        if text_value in {"semantic target", "semantic query"}:
            return target
        if text_value == "semantic distractor":
            return distractor
        return distractor

    monkeypatch.setattr(memory_routes, "generate_embedding", fake_embedding)

    first = await _remember(client, "semantic target")
    second = await _remember(client, "semantic distractor")
    assert first.status_code == 201
    assert second.status_code == 201

    response = await client.post(
        "/v1/search", json={"query": "semantic query", "mode": "semantic", "limit": 5}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["results"][0]["content"] == "semantic target"
    assert body["results"][0]["embedding_model"] == "text-embedding-3-small"
    assert body["results"][0]["embedding_dim"] == 1536

    rows = await _memory_embeddings_rows()
    assert rows == [
        {
            "embedding_model": "text-embedding-3-small",
            "embedding_dim": 1536,
            "embedding_status": "ready",
            "has_embedding": True,
        },
        {
            "embedding_model": "text-embedding-3-small",
            "embedding_dim": 1536,
            "embedding_status": "ready",
            "has_embedding": True,
        },
    ]


async def test_hybrid_search_fuses_keyword_and_semantic_rankings(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "openai"
    target = [1.0] + [0.0] * 1535
    distractor = [0.0, 1.0] + [0.0] * 1534

    async def fake_embedding(text_value: str):
        if text_value == "blue":
            return target
        if text_value == "blue keyword match":
            return distractor
        if text_value == "semantic only match":
            return target
        return distractor

    monkeypatch.setattr(memory_routes, "generate_embedding", fake_embedding)

    keyword_only = await _remember(client, "blue keyword match")
    semantic_only = await _remember(client, "semantic only match")
    assert keyword_only.status_code == 201
    assert semantic_only.status_code == 201

    response = await client.post(
        "/v1/search", json={"query": "blue", "mode": "hybrid", "limit": 10}
    )
    assert response.status_code == 200
    body = response.json()
    contents = [item["content"] for item in body["results"]]
    assert "blue keyword match" in contents
    assert "semantic only match" in contents


async def test_semantic_search_without_embeddings_returns_helpful_message(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "none"
    remember = await _remember(client, "semantic empty corpus item")
    assert remember.status_code == 201

    response = await client.post(
        "/v1/search", json={"query": "semantic empty corpus", "mode": "semantic"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["results"] == []
    assert body["total"] == 0
    assert "embeddings" in body["message"].lower()
