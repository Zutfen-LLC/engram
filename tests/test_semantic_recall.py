"""Integration tests for POST /v1/recall mode='semantic'.

These tests require a live PostgreSQL with the v2 schema
(migrations/001_init.sql) and pgvector. They skip automatically when no DB is
reachable, mirroring tests/test_search.py.

Embeddings are deterministic fakes (monkeypatched) so CI never depends on
OpenAI. The fake maps known content strings to fixed 1536-dim vectors so that
"semantic target" is nearest to "semantic query".
"""

from __future__ import annotations

from typing import Any

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
        await conn.execute(text("DELETE FROM feedback_events"))
        await conn.execute(text("DELETE FROM recall_logs"))
        await conn.execute(text("DELETE FROM memory_embeddings"))
        await conn.execute(text("DELETE FROM memory_items"))


@pytest.fixture(autouse=True)
def _reset_embedding_provider():
    original_provider = settings.embedding_provider
    original_conflict = settings.conflict_check_on_write
    # These tests exercise recall, not the write-path conflict detector.
    # With provider=openai, items sharing the query vector hit similarity 1.0
    # and would otherwise be auto-deduped by the conflict classifier — which
    # has its own dedicated test suite. Disable it here for isolation.
    settings.conflict_check_on_write = False
    yield
    settings.embedding_provider = original_provider
    settings.conflict_check_on_write = original_conflict


_TARGET_VEC = [1.0] + [0.0] * 1535
_DISTRACTOR_VEC = [0.0, 1.0] + [0.0] * 1534

# Strings that should embed near the query vector. Using a prefix match lets
# tests create multiple DISTINCT-content items (so dedup doesn't collapse
# them) that all share the target vector — e.g. "semantic target one" and
# "semantic target two" both embed to _TARGET_VEC.
_TARGET_PREFIXES = ("semantic target", "semantic query", "proposed target")


def _fake_embedding_for(text_value: str) -> list[float]:
    """Deterministic embedding: target-prefix strings map to the query vector."""
    if text_value.startswith(_TARGET_PREFIXES):
        return _TARGET_VEC
    return _DISTRACTOR_VEC


async def _remember(client: AsyncClient, content: str, **payload: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"content": content, "source_type": "manual"}
    body.update(payload)
    resp = await client.post("/v1/remember", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _patch_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_embedding(text_value: str) -> list[float] | None:
        return _fake_embedding_for(text_value)

    # recall.execute_semantic_recall imports generate_embedding at module load.
    from engram import recall as recall_mod

    monkeypatch.setattr(recall_mod, "generate_embedding", fake_embedding)
    monkeypatch.setattr(memory_routes, "generate_embedding", fake_embedding)


# ---- happy path ----


async def test_semantic_recall_returns_best_match(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)

    await _remember(client, "semantic target")
    await _remember(client, "semantic distractor")

    resp = await client.post(
        "/v1/recall", json={"mode": "semantic", "query": "semantic query"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["item_count"] >= 1
    assert body["items"][0]["content"] == "semantic target"
    # reasons identify semantic similarity as the inclusion mechanism
    reasons = body["items"][0]["reasons"]
    assert any("semantic similarity" in r for r in reasons)
    assert "distance" in body["items"][0]
    assert "score" in body["items"][0]
    # recall_log_id present, scoring_version reflects semantic mode
    assert body["recall_log_id"]
    assert body["scoring_version"] == "semantic-v1"
    assert body["config_version"]
    # working_set rendered one line per item
    assert "semantic target" in body["working_set"]


async def test_semantic_recall_includes_proposed_with_unreviewed_warning(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)

    # Distinct content so dedup doesn't collapse them; both share the target
    # vector via the prefix match in _fake_embedding_for.
    active = await _remember(client, "semantic target active")
    assert active["review_status"] == "active"
    proposed = await _remember(client, "proposed target unreviewed", source_type="extraction")
    assert proposed["review_status"] == "proposed"

    resp = await client.post(
        "/v1/recall", json={"mode": "semantic", "query": "semantic query"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    statuses = {item["review_status"] for item in body["items"]}
    assert "active" in statuses
    assert "proposed" in statuses

    for item in body["items"]:
        if item["review_status"] == "proposed":
            assert "unreviewed" in item["warnings"]
        else:
            assert "unreviewed" not in item["warnings"]


async def test_semantic_recall_excludes_rejected_items(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)

    item = await _remember(client, "semantic target")
    # Flip the item to rejected directly in the DB.
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "UPDATE memory_items SET review_status = 'rejected' WHERE id = :iid"
            ),
            {"iid": item["id"]},
        )
        await session.commit()

    resp = await client.post(
        "/v1/recall", json={"mode": "semantic", "query": "semantic query"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["item_count"] == 0
    assert body["items"] == []


async def test_semantic_recall_excludes_expired_items(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)

    item = await _remember(client, "semantic target")
    async with _test_session_factory() as session:
        await session.execute(
            text("UPDATE memory_items SET valid_to = now() WHERE id = :iid"),
            {"iid": item["id"]},
        )
        await session.commit()

    resp = await client.post(
        "/v1/recall", json={"mode": "semantic", "query": "semantic query"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["item_count"] == 0


# ---- input validation ----


async def test_semantic_recall_missing_query_returns_422(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    resp = await client.post("/v1/recall", json={"mode": "semantic"})
    assert resp.status_code == 422
    assert "non-empty query" in resp.json()["detail"]


async def test_semantic_recall_empty_query_returns_422(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    resp = await client.post("/v1/recall", json={"mode": "semantic", "query": "   "})
    assert resp.status_code == 422
    assert "non-empty query" in resp.json()["detail"]


async def test_recall_unknown_mode_returns_422(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    resp = await client.post("/v1/recall", json={"mode": "weird", "query": "x"})
    assert resp.status_code == 422
    assert "not supported" in resp.json()["detail"]


# ---- empty / no-embeddings behavior ----


async def test_semantic_recall_no_embeddings_returns_empty_non_500(client):
    """provider=none → generate_embedding returns None → empty 200 with a message."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "none"
    await _remember(client, "semantic target")

    resp = await client.post(
        "/v1/recall", json={"mode": "semantic", "query": "semantic query"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["item_count"] == 0
    assert body["items"] == []
    assert body["working_set"] == ""
    # helpful message present
    assert body["message"] is not None
    assert "embeddings" in body["message"].lower()
    # audit row still written
    assert body["recall_log_id"]


async def test_semantic_recall_no_candidates_returns_empty(client, monkeypatch):
    """provider=openai but empty corpus → empty 200 with message + audit row."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)

    resp = await client.post(
        "/v1/recall", json={"mode": "semantic", "query": "semantic query"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["item_count"] == 0
    assert body["message"] is not None
    assert body["recall_log_id"]


# ---- budget enforcement ----


async def test_semantic_recall_byte_budget_enforced(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)

    # Three distinct-content matches, all near the query vector.
    await _remember(client, "semantic target one")
    await _remember(client, "semantic target two")
    await _remember(client, "semantic target three")

    # A generous budget returns all three.
    all_resp = await client.post(
        "/v1/recall", json={"mode": "semantic", "query": "semantic query"}
    )
    assert all_resp.status_code == 200
    assert all_resp.json()["item_count"] == 3

    # A budget that admits only the first (best) item drops the rest.
    first_bytes = len(all_resp.json()["items"][0]["content"].encode())
    resp = await client.post(
        "/v1/recall",
        json={"mode": "semantic", "query": "semantic query", "byte_budget": first_bytes},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["item_count"] == 1
    # byte_count cannot exceed the budget (the single returned item fits).
    assert body["byte_count"] <= first_bytes
    assert body["omitted_count"] == 2


async def test_semantic_recall_token_budget_enforced(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)

    await _remember(client, "semantic target one")
    await _remember(client, "semantic target two")

    # token_budget that admits exactly one item (~19 bytes // 4 = 4 tokens).
    resp = await client.post(
        "/v1/recall",
        json={"mode": "semantic", "query": "semantic query", "token_budget": 4},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["item_count"] == 1


async def test_semantic_recall_item_budget_limits_count(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)

    await _remember(client, "semantic target one")
    await _remember(client, "semantic target two")

    resp = await client.post(
        "/v1/recall",
        json={"mode": "semantic", "query": "semantic query", "item_budget": 1},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["item_count"] == 1


# ---- recall_logs audit ----


async def test_semantic_recall_writes_recall_log(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)

    await _remember(client, "semantic target")
    resp = await client.post(
        "/v1/recall", json={"mode": "semantic", "query": "semantic query"}
    )
    assert resp.status_code == 200
    recall_log_id = resp.json()["recall_log_id"]

    async with _test_session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT mode, query, scoring_version, config_version, "
                    "array_length(item_ids, 1) AS n_items "
                    "FROM recall_logs WHERE id = :rid"
                ),
                {"rid": recall_log_id},
            )
        ).one()

    assert row.mode == "semantic"
    assert row.query == "semantic query"
    assert row.scoring_version == "semantic-v1"
    assert row.config_version is not None
    assert row.n_items == 1


async def test_semantic_recall_updates_recall_count_not_startup_count(client, monkeypatch):
    """Semantic recall increments recall_count + last_recalled_at, but NOT
    startup_recall_count (that counter drives the startup-only penalty)."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)

    item = await _remember(client, "semantic target")
    resp = await client.post(
        "/v1/recall", json={"mode": "semantic", "query": "semantic query"}
    )
    assert resp.status_code == 200

    async with _test_session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT recall_count, startup_recall_count, last_recalled_at "
                    "FROM memory_items WHERE id = :iid"
                ),
                {"iid": item["id"]},
            )
        ).one()

    assert row.recall_count == 1
    assert row.startup_recall_count == 0
    assert row.last_recalled_at is not None
