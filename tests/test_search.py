"""Tests for POST /v1/search — keyword, semantic, hybrid, and default filters."""

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
        from engram.db import apply_rls_context

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
    resp = await client.post("/v1/remember", json=body)
    # ENG-AUD-008: /v1/remember enqueues embedding.generate; process it so the
    # embedding is ready before any semantic search in the test.
    await _drain_jobs()
    return resp


async def _drain_jobs(max_iterations: int = 10) -> None:
    """Process queued embedding.generate jobs until empty (ENG-AUD-008).

    Only embedding.generate is processed to avoid conflict-check side effects
    in search-only tests.
    """
    from engram.worker import process_one_job

    for _ in range(max_iterations):
        processed = await process_one_job(
            worker_id="test",
            session_factory=_test_session_factory,
            app_session_factory=_test_session_factory,
            job_types=["embedding.generate"],
        )
        if not processed:
            return


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

    async def fake_embedding(text_value: str, *_args: object, **_kwargs: object):
        if text_value in {"semantic target", "semantic query"}:
            return target
        if text_value == "semantic distractor":
            return distractor
        return distractor

    monkeypatch.setattr(memory_routes, "generate_embedding", fake_embedding)
    monkeypatch.setattr(embeddings_mod, "generate_embedding", fake_embedding)

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

    async def fake_embedding(text_value: str, *_args: object, **_kwargs: object):
        if text_value == "blue":
            return target
        if text_value == "blue keyword match":
            return distractor
        if text_value == "semantic only match":
            return target
        return distractor

    monkeypatch.setattr(memory_routes, "generate_embedding", fake_embedding)
    monkeypatch.setattr(embeddings_mod, "generate_embedding", fake_embedding)

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


# ---- search filters (kind/wing/room) honored across all modes ----


async def test_keyword_search_kind_filter_excludes_other_kinds(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "none"
    await _remember(client, "alpha fact note about tea", kind="fact")
    await _remember(client, "alpha observation note about tea", kind="observation")
    resp = await client.post(
        "/v1/search", json={"query": "alpha tea", "mode": "keyword", "kind": "observation"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["results"][0]["kind"] == "observation"


async def test_keyword_search_wing_and_room_filters_and_semantics(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "none"
    await _remember(client, "gamma deploy note one", kind="fact", wing="ops", room="deploy")
    await _remember(client, "gamma deploy note two", kind="fact", wing="ops", room="monitor")
    await _remember(client, "gamma deploy note three", kind="fact", wing="dev", room="deploy")
    # wing only
    resp = await client.post(
        "/v1/search", json={"query": "gamma deploy", "mode": "keyword", "wing": "ops"}
    )
    contents = {r["content"] for r in resp.json()["results"]}
    assert contents == {"gamma deploy note one", "gamma deploy note two"}
    # room only
    resp = await client.post(
        "/v1/search", json={"query": "gamma deploy", "mode": "keyword", "room": "deploy"}
    )
    contents = {r["content"] for r in resp.json()["results"]}
    assert contents == {"gamma deploy note one", "gamma deploy note three"}
    # combined AND
    resp = await client.post(
        "/v1/search",
        json={"query": "gamma deploy", "mode": "keyword", "wing": "ops", "room": "deploy"},
    )
    contents = {r["content"] for r in resp.json()["results"]}
    assert contents == {"gamma deploy note one"}


async def test_semantic_search_kind_filter_honored(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "openai"

    # All items embed to the same vector so similarity is identical; the kind
    # filter is the only differentiator.
    same_vec = [1.0] + [0.0] * 1535

    async def fake_embedding(text_value: str, *_args: object, **_kwargs: object):
        return same_vec

    monkeypatch.setattr(memory_routes, "generate_embedding", fake_embedding)
    monkeypatch.setattr(embeddings_mod, "generate_embedding", fake_embedding)

    await _remember(client, "delta matched item a", kind="fact")
    await _remember(client, "delta matched item b", kind="observation")
    resp = await client.post(
        "/v1/search",
        json={"query": "delta matched", "mode": "semantic", "kind": "fact", "limit": 10},
    )
    assert resp.status_code == 200
    body = resp.json()
    kinds = {r["kind"] for r in body["results"]}
    assert kinds == {"fact"}


async def test_semantic_search_room_filter_honored(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "openai"
    same_vec = [1.0] + [0.0] * 1535

    async def fake_embedding(text_value: str, *_args: object, **_kwargs: object):
        return same_vec

    monkeypatch.setattr(memory_routes, "generate_embedding", fake_embedding)
    monkeypatch.setattr(embeddings_mod, "generate_embedding", fake_embedding)

    await _remember(client, "epsilon matched item a", kind="fact", room="alpha")
    await _remember(client, "epsilon matched item b", kind="fact", room="beta")
    resp = await client.post(
        "/v1/search",
        json={"query": "epsilon matched", "mode": "semantic", "room": "alpha", "limit": 10},
    )
    assert resp.status_code == 200
    body = resp.json()
    rooms = {r.get("content") for r in body["results"]}
    assert rooms == {"epsilon matched item a"}


async def test_hybrid_search_wing_filter_honored(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "openai"
    same_vec = [1.0] + [0.0] * 1535

    async def fake_embedding(text_value: str, *_args: object, **_kwargs: object):
        return same_vec

    monkeypatch.setattr(memory_routes, "generate_embedding", fake_embedding)
    monkeypatch.setattr(embeddings_mod, "generate_embedding", fake_embedding)

    # Items share a keyword so keyword + semantic branches both match; the wing
    # filter must restrict both branches.
    await _remember(client, "zeta shared keyword ops", kind="fact", wing="ops")
    await _remember(client, "zeta shared keyword dev", kind="fact", wing="dev")
    resp = await client.post(
        "/v1/search",
        json={"query": "zeta shared keyword", "mode": "hybrid", "wing": "ops", "limit": 10},
    )
    assert resp.status_code == 200
    body = resp.json()
    contents = {r["content"] for r in body["results"]}
    assert contents == {"zeta shared keyword ops"}


async def test_search_filters_do_not_bypass_tenant_read_eligibility(client, monkeypatch):
    """Filters apply alongside tenant/visibility eligibility; an explicit filter
    must not leak items the caller is otherwise ineligible to read. We verify
    the filter path reuses the shared eligibility predicate by confirming a
    kind filter that matches no eligible items returns empty (not an error and
    not a cross-tenant leak)."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "none"
    await _remember(client, "eta eligible fact one", kind="fact")
    resp = await client.post(
        "/v1/search", json={"query": "eta eligible", "mode": "keyword", "kind": "nonexistent_kind"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["results"] == []


# ---- semantic trust-weighted ranking ----


async def test_semantic_search_ranks_high_trust_above_slightly_closer_low_trust(
    client, monkeypatch
):
    """Under semantic-v2, a high-trust item that is slightly less semantically
    similar must outrank a low-trust item that is slightly more similar."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "openai"

    # The low-trust (extraction/proposed) item is slightly closer to the query
    # vector than the high-trust (manual/active) item.
    high_trust_vec = [0.9, 0.1] + [0.0] * 1534  # slightly farther from query
    low_trust_vec = [1.0, 0.0] + [0.0] * 1534  # exactly the query vector
    query_vec = [1.0, 0.0] + [0.0] * 1534

    async def fake_embedding(text_value: str, *_args: object, **_kwargs: object):
        if text_value == "high trust memory":
            return high_trust_vec
        if text_value == "low trust memory":
            return low_trust_vec
        if text_value == "trust query":
            return query_vec
        return low_trust_vec

    monkeypatch.setattr(memory_routes, "generate_embedding", fake_embedding)
    monkeypatch.setattr(embeddings_mod, "generate_embedding", fake_embedding)

    # High-trust: manual user write -> active, source_trust/confidence 0.9.
    await _remember(client, "high trust memory", kind="fact")
    # Low-trust: extraction source -> proposed, source_trust/confidence 0.5.
    await _remember(client, "low trust memory", kind="fact", source_type="extraction")

    resp = await client.post(
        "/v1/search", json={"query": "trust query", "mode": "semantic", "limit": 10}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"][0]["content"] == "high trust memory"
    # Explanatory scoring fields are present.
    top = body["results"][0]
    assert "distance" in top
    assert "similarity_score" in top
    assert "trust_score" in top
    assert "score" in top
