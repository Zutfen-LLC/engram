"""Tests for classification and remember auto-classification."""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.api.app import create_app
from engram.api.routes import memory as memory_routes
from engram.classification import ClassificationResult
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
        await conn.execute(text("DELETE FROM item_events"))
        await conn.execute(text("DELETE FROM memory_embeddings"))
        await conn.execute(text("DELETE FROM memory_items"))


@pytest.fixture(autouse=True)
def _reset_classification_settings():
    provider = settings.classification_provider
    model = settings.classification_model
    threshold = settings.classification_confidence_threshold
    yield
    settings.classification_provider = provider
    settings.classification_model = model
    settings.classification_confidence_threshold = threshold


async def _remember(client: AsyncClient, content: str, **payload: object):
    body = {"content": content, "source_type": "manual"}
    body.update(payload)
    return await client.post("/v1/remember", json=body)


async def _latest_item_event() -> dict[str, object]:
    async with _test_engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT event_type, field_name, reason, new_value
                FROM item_events
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        )
        row = result.mappings().one()
        return dict(row)


async def test_rule_based_classification_without_llm(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")

    settings.classification_provider = "none"
    response = await client.post("/v1/classify", json={"content": "User prefers dark mode"})
    assert response.status_code == 200
    body = response.json()
    assert body["suggested_kind"] == "preference"
    assert 0.6 <= body["confidence"] <= 0.8
    assert body["rules_matched"]
    assert "kind_preference" in body["rules_matched"]
    assert body["suggested_visibility"] == "workspace"
    assert body["reason"]


async def test_llm_enriched_classification_uses_taxonomy_and_vocab(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")

    settings.classification_provider = "none"
    seeded = await client.post(
        "/v1/remember",
        json={
            "content": "Vocabulary seed for prompt inspection",
            "kind": "decision",
            "wing": "wing-alpha",
            "room": "room-1",
            "source_type": "manual",
        },
    )
    assert seeded.status_code == 201

    captured: list[str] = []

    async def fake_openai(prompt: str) -> dict[str, object]:
        captured.append(prompt)
        return {
            "suggested_kind": "decision",
            "suggested_wing": "wing-alpha",
            "suggested_room": "room-1",
            "confidence": 0.88,
            "reason": "LLM sees a decision with matching vocabulary",
            "rules_matched": ["kind_decision"],
        }

    monkeypatch.setattr("engram.classification._call_openai_classification", fake_openai)
    settings.classification_provider = "openai"
    settings.classification_model = "gpt-4o-mini"

    response = await client.post(
        "/v1/classify",
        json={"content": "We decided to keep wing-alpha / room-1 as the landing zone."},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["suggested_kind"] == "decision"
    assert 0.7 <= body["confidence"] <= 0.95
    assert body["rules_matched"] == ["kind_decision"]
    assert captured, "expected LLM prompt to be captured"
    prompt = captured[0]
    assert "fact" in prompt
    assert "decision" in prompt
    assert "wing-alpha" in prompt
    assert "room-1" in prompt
    assert "We decided to keep wing-alpha / room-1 as the landing zone." in prompt


async def test_auto_classify_on_remember_stores_provenance(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")

    async def fake_classifier(content: str, tenant_id, session, context=None):
        return ClassificationResult(
            suggested_kind="decision",
            suggested_wing="wing-alpha",
            suggested_room="room-1",
            confidence=0.86,
            reason="matched explicit decision context",
            rules_matched=["kind_decision"],
            provenance={"provider": "openai", "mode": "llm", "matched_rules": ["kind_decision"]},
        )

    monkeypatch.setattr(memory_routes, "classify_memory", fake_classifier)
    response = await client.post("/v1/remember", json={"content": "We decided to keep wing-alpha."})
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "created"
    assert body["review_status"] == "proposed"
    event = await _latest_item_event()
    assert event["event_type"] == "classification"
    payload = json.loads(event["new_value"])
    assert payload["source"] == "auto_classified"
    assert payload["kind"] == "decision"
    assert payload["classification"]["suggested_kind"] == "decision"
    assert payload["classification_provenance"]["provider"] == "openai"


async def test_explicit_kind_override_skips_auto_classify(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")

    async def should_not_run(*args, **kwargs):
        raise AssertionError("classify() must not be called when kind is explicit")

    monkeypatch.setattr(memory_routes, "classify_memory", should_not_run)
    response = await client.post(
        "/v1/remember",
        json={
            "content": "Explicit kind should win",
            "kind": "invariant",
            "source_type": "manual",
        },
    )
    assert response.status_code == 201
    event = await _latest_item_event()
    assert event["event_type"] == "classification"
    payload = json.loads(event["new_value"])
    assert payload["source"] == "explicit_kind"
    assert payload["kind"] == "invariant"
    assert payload["provider"] == "caller"
