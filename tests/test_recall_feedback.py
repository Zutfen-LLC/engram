"""Tests for recall explanations (warnings) and POST /v1/feedback.

These tests require a live PostgreSQL with the v2 schema (migrations/001_init.sql).
They skip automatically when no DB is reachable.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.api.app import create_app
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
            await session.execute(
                sa_text(
                    "SELECT t.id::text AS tenant_id, p.id::text AS principal_id "
                    "FROM tenants t "
                    "JOIN principals p ON p.tenant_id = t.id AND p.name = :principal "
                    "WHERE t.slug = :slug"
                ),
                {"slug": _DEFAULT_TENANT_SLUG, "principal": _DEFAULT_PRINCIPAL_NAME},
            )
        ).mappings().one()
        from engram.db import apply_rls_context

        await apply_rls_context(
            session, tenant_id=row["tenant_id"], principal_id=row["principal_id"]
        )
        yield session


@pytest.fixture
def app():
    # Startup recall's bounded candidate selection (ENG-AUD-011) reads through
    # engram.db.read_session_factory, a real pooled app engine independent of
    # the get_session override above. A pooled connection bound to one test's
    # event loop can get reused by a later test on a different loop → asyncpg
    # "Future attached to a different loop" / "another operation in progress".
    # Point it at this file's own NullPool factory (same pattern as
    # test_promotion.py) so every connection stays on the current test's loop.
    import engram.db as db_module

    app = create_app()
    app.dependency_overrides[get_session] = _get_test_session
    app.state._engram_real_read_factory = db_module.read_session_factory  # type: ignore[attr-defined]
    db_module.read_session_factory = _test_session_factory
    return app


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    import engram.db as db_module

    db_module.read_session_factory = app.state._engram_real_read_factory


@pytest.fixture(autouse=True)
async def _clean_db():
    if not await _db_ok():
        return
    async with _test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM feedback_events"))
        await conn.execute(text("DELETE FROM memory_embeddings"))
        await conn.execute(text("DELETE FROM memory_items"))
        await conn.execute(text("UPDATE tenant_config SET feedback_daily_limit = 500"))


async def _create_item(client, content: str, **overrides) -> dict:
    payload = {"content": content, "source_type": "manual"}
    payload.update(overrides)
    resp = await client.post("/v1/remember", json=payload)
    assert resp.status_code == 201
    return resp.json()


# ---- Recall response includes reasons and warnings ----


async def test_recall_response_has_reasons_and_warnings(client):
    """Recall response items include 'reasons' and 'warnings' arrays."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    await _create_item(client, "Test memory for recall warnings")
    resp = await client.post("/v1/recall", json={"mode": "startup"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["item_count"] >= 1
    for item in body["items"]:
        assert "reasons" in item
        assert isinstance(item["reasons"], list)
        assert "warnings" in item
        assert isinstance(item["warnings"], list)


async def test_recall_response_has_scoring_and_config_version(client):
    """Recall response includes scoring_version and config_version."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    await _create_item(client, "Version test memory")
    resp = await client.post("/v1/recall", json={"mode": "startup"})
    assert resp.status_code == 200
    body = resp.json()
    assert "scoring_version" in body
    assert "config_version" in body


# ---- Feedback endpoint ----


async def test_feedback_useful_raises_importance(client):
    """Useful feedback raises importance (capped at 0.95)."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    item = await _create_item(client, "Feedback useful test", importance=0.5)
    item_id = item["id"]
    resp = await client.post(
        "/v1/feedback",
        json={"item_id": item_id, "feedback": "useful"},
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "recorded"


async def test_feedback_noise_lowers_importance(client):
    """Noise feedback lowers importance (floor at 0.1)."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    item = await _create_item(client, "Feedback noise test", importance=0.5)
    item_id = item["id"]
    resp = await client.post(
        "/v1/feedback",
        json={"item_id": item_id, "feedback": "noise"},
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "recorded"


async def test_feedback_accepts_recall_log_id(client):
    """Feedback endpoint accepts and stores recall_log_id from a real recall run."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    item = await _create_item(client, "Feedback recall log test")
    item_id = item["id"]
    # Issue a real recall so a recall_logs row exists, then reference it.
    recall_resp = await client.post("/v1/recall", json={"mode": "startup"})
    assert recall_resp.status_code == 200
    recall_log_id = recall_resp.json().get("recall_log_id")
    assert recall_log_id, "recall response should include recall_log_id"
    resp = await client.post(
        "/v1/feedback",
        json={"item_id": item_id, "feedback": "useful", "recall_log_id": recall_log_id},
    )
    assert resp.status_code == 201


async def test_feedback_nonexistent_item_returns_404(client):
    """Feedback on a non-existent item returns 404."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    resp = await client.post(
        "/v1/feedback",
        json={"item_id": str(uuid.uuid4()), "feedback": "useful"},
    )
    assert resp.status_code == 404


async def test_feedback_is_logged(client):
    """Feedback creates a feedback_events row."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    item = await _create_item(client, "Feedback logging test")
    item_id = item["id"]
    await client.post(
        "/v1/feedback",
        json={"item_id": item_id, "feedback": "useful"},
    )
    async with _test_session_factory() as session:
        from sqlalchemy import text as sa_text

        result = await session.execute(
            sa_text("SELECT verdict FROM feedback_events WHERE item_id = :iid"),
            {"iid": item_id},
        )
        rows = result.all()
    assert len(rows) == 1
    assert rows[0][0] == "useful"


async def test_feedback_invalid_verdict_returns_422(client):
    """Invalid feedback value returns 422."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    item = await _create_item(client, "Feedback invalid test")
    resp = await client.post(
        "/v1/feedback",
        json={"item_id": item["id"], "feedback": "invalid"},
    )
    assert resp.status_code == 422


async def test_concurrent_identical_feedback_is_one_contribution(client):
    """Item locking makes concurrent retry storms canonical and idempotent."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    item = await _create_item(client, "Concurrent identical feedback", importance=0.5)
    responses = await asyncio.gather(
        *(
            client.post(
                "/v1/feedback", json={"item_id": item["id"], "feedback": "useful"}
            )
            for _ in range(8)
        )
    )
    assert [response.status_code for response in responses].count(201) == 1
    assert [response.status_code for response in responses].count(200) == 7
    async with _test_session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT count(*) AS events, "
                    "count(*) FILTER (WHERE superseded_at IS NULL) AS current "
                    "FROM feedback_events WHERE item_id = :iid"
                ),
                {"iid": item["id"]},
            )
        ).one()
        importance = await session.scalar(
            text("SELECT importance FROM memory_items WHERE id = :iid"), {"iid": item["id"]}
        )
    assert row.events == row.current == 1
    assert importance == pytest.approx(0.55)


async def test_concurrent_feedback_cannot_exceed_daily_limit(client):
    """Principal-row locking makes count+insert safe across simultaneous items."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _test_engine.begin() as conn:
        await conn.execute(text("UPDATE tenant_config SET feedback_daily_limit = 5"))
    items = [
        await _create_item(client, f"Concurrent rate item {index}", importance=0.5)
        for index in range(10)
    ]
    responses = await asyncio.gather(
        *(
            client.post(
                "/v1/feedback", json={"item_id": item["id"], "feedback": "useful"}
            )
            for item in items
        )
    )
    assert [response.status_code for response in responses].count(201) == 5
    limited = [response for response in responses if response.status_code == 429]
    assert len(limited) == 5
    assert all(response.headers.get("Retry-After") for response in limited)
    async with _test_session_factory() as session:
        event_count = await session.scalar(text("SELECT count(*) FROM feedback_events"))
        changed_count = await session.scalar(
            text("SELECT count(*) FROM memory_items WHERE importance > 0.5")
        )
    assert event_count == 5
    assert changed_count == 5
