"""Tests for classification and remember auto-classification."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime

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
        await conn.execute(text("DELETE FROM classification_runs"))
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
    assert body["suggested_visibility"] is None
    assert body["reason"]


async def test_classify_persists_attested_receipt_without_raw_context(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.classification_provider = "none"
    context = "exact context that must not be stored"
    before = datetime.now(UTC)
    response = await client.post(
        "/v1/classify",
        json={
            "content": "User prefers dark mode",
            "context": context,
            "source_type": "sync_turn",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["confidence"] == body["taxonomy_confidence"]
    assert body["retention_disposition"] == "uncertain"
    async with _test_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT * FROM classification_runs WHERE id = :id"),
                {"id": body["classification_run_id"]},
            )
        ).mappings().one()
    assert row["source_type"] == "sync_turn"
    assert row["context_hash"] == hashlib.sha256(context.encode()).hexdigest()
    assert row["context_length"] == len(context)
    assert row["canonicalization_version"] == "canonical-v1"
    assert row["classification_version"] == "classification-v2"
    assert row["retention_policy_version"] == "retention-v1"
    assert row["expires_at"] >= before
    assert (row["expires_at"] - row["created_at"]).total_seconds() == 3600
    assert context not in json.dumps(row["provenance"])


async def test_receipt_bound_remember_uses_server_evidence_and_source_prior(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.classification_provider = "none"
    classified = await client.post(
        "/v1/classify",
        json={"content": "User prefers dark mode", "source_type": "sync_turn"},
    )
    receipt_id = classified.json()["classification_run_id"]
    remembered = await client.post(
        "/v1/remember",
        json={
            "content": "User prefers dark mode",
            "source_type": "sync_turn",
            "classification_run_id": receipt_id,
            "retention_confidence": 0.95,
            "retention_disposition": "retain",
        },
    )
    assert remembered.status_code == 201
    assert remembered.json()["memory_confidence"] == pytest.approx(0.4)
    async with _test_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT m.source_confidence_prior,m.retention_confidence,"
                    "m.retention_disposition,r.memory_item_id "
                    "FROM memory_items m JOIN classification_runs r ON r.memory_item_id=m.id "
                    "WHERE r.id=:id"
                ),
                {"id": receipt_id},
            )
        ).mappings().one()
    assert row["source_confidence_prior"] == pytest.approx(0.4)
    assert row["retention_confidence"] == 0.0
    assert row["retention_disposition"] == "uncertain"


async def test_receipt_content_and_kind_mismatch_are_rejected(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.classification_provider = "none"
    classified = await client.post(
        "/v1/classify", json={"content": "User prefers dark mode"}
    )
    receipt_id = classified.json()["classification_run_id"]
    content_mismatch = await client.post(
        "/v1/remember",
        json={"content": "different", "classification_run_id": receipt_id},
    )
    assert content_mismatch.status_code == 422
    kind_mismatch = await client.post(
        "/v1/remember",
        json={
            "content": "User prefers dark mode",
            "kind": "fact",
            "classification_run_id": receipt_id,
        },
    )
    assert kind_mismatch.status_code == 422


async def test_concurrent_same_receipt_is_idempotent(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    classified = await client.post(
        "/v1/classify", json={"content": "Concurrent receipt identity"}
    )
    receipt_id = classified.json()["classification_run_id"]
    payload = {
        "content": "Concurrent receipt identity",
        "classification_run_id": receipt_id,
    }
    first, second = await asyncio.gather(
        client.post("/v1/remember", json=payload),
        client.post("/v1/remember", json=payload),
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    async with _test_engine.connect() as conn:
        counts = (
            await conn.execute(
                text(
                    "SELECT (SELECT count(*) FROM memory_items WHERE content_hash=("
                    "SELECT content_hash FROM classification_runs WHERE id=:id)), "
                    "(SELECT count(*) FROM item_events WHERE item_id=("
                    "SELECT memory_item_id FROM classification_runs WHERE id=:id) "
                    "AND event_type='classification')"
                ),
                {"id": receipt_id},
            )
        ).one()
    assert tuple(counts) == (1, 1)


async def test_concurrent_different_receipts_have_one_conflict_loser(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    content = "Two receipts one dedup identity"
    one, two = await asyncio.gather(
        client.post("/v1/classify", json={"content": content}),
        client.post("/v1/classify", json={"content": content}),
    )
    responses = await asyncio.gather(
        client.post(
            "/v1/remember",
            json={"content": content, "classification_run_id": one.json()["classification_run_id"]},
        ),
        client.post(
            "/v1/remember",
            json={"content": content, "classification_run_id": two.json()["classification_run_id"]},
        ),
    )
    assert sorted(response.status_code for response in responses) == [201, 409]
    async with _test_engine.connect() as conn:
        bound = await conn.scalar(
            text(
                "SELECT count(*) FROM classification_runs "
                "WHERE id IN (:one,:two) AND memory_item_id IS NOT NULL"
            ),
            {
                "one": one.json()["classification_run_id"],
                "two": two.json()["classification_run_id"],
            },
        )
    assert bound == 1


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
    assert "kind_decision" in body["rules_matched"]
    assert captured, "expected LLM prompt to be captured"
    prompt = captured[0]
    assert "fact" in prompt
    assert "decision" in prompt
    assert "wing-alpha" in prompt
    assert "room-1" in prompt
    assert "We decided to keep wing-alpha / room-1 as the landing zone." in prompt
    # The prompt now advertises the real 0.0-0.95 confidence range and asks for
    # an advisory suggested_visibility.
    assert "0.0-0.95" in prompt
    assert "suggested_visibility" in prompt


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

    monkeypatch.setattr(memory_routes, "classify_rules_only", fake_classifier)
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

    monkeypatch.setattr(memory_routes, "classify_rules_only", should_not_run)
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


# ---- Confidence preservation (0.7 floor removed) ----


async def _seed_vocab_for_llm(client):
    """Seed wing/room vocabulary so the LLM prompt includes them."""
    seeded = await client.post(
        "/v1/remember",
        json={
            "content": "Vocabulary seed for classification tests",
            "kind": "decision",
            "wing": "wing-alpha",
            "room": "room-1",
            "source_type": "manual",
        },
    )
    assert seeded.status_code == 201


async def test_llm_low_confidence_is_preserved(client, monkeypatch):
    """An LLM reporting 0.35 keeps 0.35 instead of being floored to 0.7."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")

    settings.classification_provider = "none"
    await _seed_vocab_for_llm(client)

    async def fake_openai(prompt: str) -> dict[str, object]:
        return {
            "suggested_kind": "fact",
            "confidence": 0.35,
            "reason": "genuinely uncertain",
            "rules_matched": [],
        }

    monkeypatch.setattr("engram.classification._call_openai_classification", fake_openai)
    settings.classification_provider = "openai"

    response = await client.post("/v1/classify", json={"content": "Ambiguous text here."})
    assert response.status_code == 200
    assert response.json()["confidence"] == pytest.approx(0.35)


async def test_llm_high_confidence_clamped_to_ceiling(client, monkeypatch):
    """An LLM reporting 1.0 is clamped to 0.95, not stored as 1.0."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")

    settings.classification_provider = "none"
    await _seed_vocab_for_llm(client)

    async def fake_openai(prompt: str) -> dict[str, object]:
        return {
            "suggested_kind": "fact",
            "confidence": 1.0,
            "reason": "absolutely certain",
            "rules_matched": [],
        }

    monkeypatch.setattr("engram.classification._call_openai_classification", fake_openai)
    settings.classification_provider = "openai"

    response = await client.post("/v1/classify", json={"content": "Very certain fact."})
    assert response.status_code == 200
    assert response.json()["confidence"] == pytest.approx(0.95)


async def test_llm_below_threshold_falls_back_without_re_raising(client, monkeypatch):
    """Below-threshold confidence keeps the conservative fact default but does
    NOT get re-floored above the threshold."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")

    settings.classification_provider = "none"
    settings.classification_confidence_threshold = 0.5
    await _seed_vocab_for_llm(client)

    async def fake_openai(prompt: str) -> dict[str, object]:
        return {
            "suggested_kind": "decision",
            "confidence": 0.2,
            "reason": "low confidence guess",
            "rules_matched": [],
        }

    monkeypatch.setattr("engram.classification._call_openai_classification", fake_openai)
    settings.classification_provider = "openai"

    response = await client.post("/v1/classify", json={"content": "Uncertain decision text."})
    assert response.status_code == 200
    body = response.json()
    # Fell back to fact, confidence stayed low (not floored to 0.7).
    assert body["suggested_kind"] == "fact"
    assert body["confidence"] == pytest.approx(0.2)
    assert body["confidence"] < settings.classification_confidence_threshold


async def test_llm_suggested_visibility_passes_through(client, monkeypatch):
    """A valid suggested_visibility from the LLM surfaces on /v1/classify."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")

    settings.classification_provider = "none"
    await _seed_vocab_for_llm(client)

    async def fake_openai(prompt: str) -> dict[str, object]:
        return {
            "suggested_kind": "fact",
            "suggested_visibility": "private",
            "confidence": 0.8,
            "reason": "looks personal",
            "rules_matched": [],
        }

    monkeypatch.setattr("engram.classification._call_openai_classification", fake_openai)
    settings.classification_provider = "openai"

    response = await client.post("/v1/classify", json={"content": "Personal detail."})
    assert response.status_code == 200
    assert response.json()["suggested_visibility"] == "private"


async def test_llm_invalid_suggested_visibility_becomes_none(client, monkeypatch):
    """An out-of-enum visibility is dropped to None, not stored as garbage."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")

    settings.classification_provider = "none"
    await _seed_vocab_for_llm(client)

    async def fake_openai(prompt: str) -> dict[str, object]:
        return {
            "suggested_kind": "fact",
            "suggested_visibility": "global",
            "confidence": 0.8,
            "reason": "x",
            "rules_matched": [],
        }

    monkeypatch.setattr("engram.classification._call_openai_classification", fake_openai)
    settings.classification_provider = "openai"

    response = await client.post("/v1/classify", json={"content": "Some text."})
    assert response.status_code == 200
    assert response.json()["suggested_visibility"] is None


# ---- Seed rule behavior (F9) ----


async def test_status_only_text_is_handled_conservatively(client):
    """Bare status tokens like 'ok', 'done', 'passed' still get the fact default."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")

    settings.classification_provider = "none"
    for status in ("ok", "done", "passed"):
        response = await client.post("/v1/classify", json={"content": status})
        assert response.status_code == 200
        body = response.json()
        assert body["suggested_kind"] == "fact"
        assert "skip" in body["reason"].lower() or "conservative" in body["reason"].lower()


@pytest.mark.parametrize(
    "content",
    [
        "The deploy is done and PR #42 is merged.",
        "Tests passed after changing the auth lookup.",
        "The migration failed because the app role lacked sequence usage.",
    ],
)
async def test_meaningful_sentences_not_swallowed_by_skip(client, content):
    """Status words inside meaningful sentences must not trigger the skip rule.

    The contract is narrow: the skip rule must not fire. Whatever else the rule
    layer picks (decision, observation, or the conservative fact default) is fine
    — the point is that meaningful sentences are not swallowed as status text.
    """
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")

    settings.classification_provider = "none"
    response = await client.post("/v1/classify", json={"content": content})
    assert response.status_code == 200
    body = response.json()
    assert "skip" not in body["reason"].lower(), body["reason"]
    assert "skip_status_only" not in body["rules_matched"]
    assert "skip_single_token" not in body["rules_matched"]


async def test_casual_should_not_become_doctrine(client):
    """Casual 'should' statements must not be classified as doctrine."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")

    settings.classification_provider = "none"
    response = await client.post(
        "/v1/classify", json={"content": "We should probably update the README."}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["suggested_kind"] != "doctrine"
    assert "kind_doctrine" not in body["rules_matched"]


@pytest.mark.parametrize(
    "content",
    [
        "Policy: agents must never widen memory visibility automatically.",
        "Invariant: content is append-first and must not be updated in place.",
        "The service must always enforce tenant isolation.",
    ],
)
async def test_explicit_policy_language_becomes_doctrine(client, content):
    """Explicit policy/invariant phrasing still classifies as doctrine."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")

    settings.classification_provider = "none"
    response = await client.post("/v1/classify", json={"content": content})
    assert response.status_code == 200
    body = response.json()
    assert body["suggested_kind"] == "doctrine"
    assert "kind_doctrine" in body["rules_matched"]
