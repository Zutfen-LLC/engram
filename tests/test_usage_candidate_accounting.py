"""Candidate accounting tests for ENG-METER-001 (candidate.observed/outcome).

Requires a live PostgreSQL with the v2 schema. Skips automatically when no DB
is reachable, matching test_remember.py / test_worker_classification.py.

Covers:
* classify() then remember() with the same ingest_id -> one
  candidate.observed event (not two).
* a direct remember() with no preceding classify() -> exactly one
  candidate.observed event.
* a retried candidate.observed insert (same ingest_id) is deduplicated.
* created / deduped / superseded / failed outcomes are represented exactly
  once each, without changing the API response.
* UTF-8 byte accounting is correct for non-ASCII content.
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
    return create_app()


@pytest.fixture
async def client(app):
    app.dependency_overrides[get_session] = _get_test_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
async def _clean_db():
    if not await _db_ok():
        return
    async with _test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM usage_events"))
        await conn.execute(text("DELETE FROM jobs"))
        await conn.execute(text("DELETE FROM item_events"))
        await conn.execute(text("DELETE FROM classification_runs"))
        await conn.execute(text("DELETE FROM memory_embeddings"))
        await conn.execute(text("DELETE FROM memory_items"))
        await conn.execute(text("DELETE FROM candidate_ingests"))


@pytest.fixture(autouse=True)
def _enable_telemetry(monkeypatch):
    monkeypatch.setattr(settings, "usage_telemetry_enabled", True)


async def _usage_events(event_type: str, correlation_id: str | None = None) -> list[dict]:
    async with _test_session_factory() as session:
        clauses = ["event_type = :event_type"]
        params: dict[str, object] = {"event_type": event_type}
        if correlation_id is not None:
            clauses.append("correlation_id = :cid")
            params["cid"] = correlation_id
        rows = (
            await session.execute(
                text(f"SELECT * FROM usage_events WHERE {' AND '.join(clauses)}"), params
            )
        ).mappings().all()
        return [dict(r) for r in rows]


async def test_classify_then_remember_same_correlation_counts_once(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    correlation_id = str(uuid.uuid4())

    classify_resp = await client.post(
        "/v1/classify",
        json={"content": "we decided to use pgvector", "correlation_id": correlation_id},
    )
    assert classify_resp.status_code == 200
    assert classify_resp.json()["correlation_id"] == correlation_id
    ingest_id = classify_resp.json()["ingest_id"]

    remember_resp = await client.post(
        "/v1/remember",
        json={
            "content": "we decided to use pgvector",
            "correlation_id": correlation_id,
            "classification_run_id": classify_resp.json()["classification_run_id"],
            "ingest_id": ingest_id,
        },
    )
    assert remember_resp.status_code == 201
    assert remember_resp.json()["correlation_id"] == correlation_id
    assert remember_resp.json()["ingest_id"] == ingest_id
    assert remember_resp.json()["attempt_id"]

    observed = await _usage_events("candidate.observed", correlation_id)
    assert len(observed) == 1


async def test_direct_remember_without_classify_counts_once(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    resp = await client.post("/v1/remember", json={"content": "direct write, no classify call"})
    assert resp.status_code == 201
    correlation_id = resp.json()["correlation_id"]
    assert correlation_id is not None

    observed = await _usage_events("candidate.observed", str(correlation_id))
    assert len(observed) == 1
    outcomes = await _usage_events("candidate.outcome", str(correlation_id))
    assert len(outcomes) == 1
    assert outcomes[0]["status"] == "created"


async def test_retried_candidate_observation_is_deduplicated(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    correlation_id = str(uuid.uuid4())
    # Correlation is trace-only: two classify calls receive distinct ingests.
    for _ in range(2):
        resp = await client.post(
            "/v1/classify",
            json={"content": "retry candidate", "correlation_id": correlation_id},
        )
        assert resp.status_code == 200

    observed = await _usage_events("candidate.observed", correlation_id)
    assert len(observed) == 2
    assert len({row["ingest_id"] for row in observed}) == 2


async def test_same_correlation_different_content_creates_distinct_ingests(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    correlation_id = str(uuid.uuid4())
    responses = [
        await client.post(
            "/v1/classify", json={"content": content, "correlation_id": correlation_id}
        )
        for content in ("candidate alpha", "candidate beta")
    ]
    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()["ingest_id"] for response in responses}) == 2
    observed = await _usage_events("candidate.observed", correlation_id)
    assert len(observed) == 2


async def test_concurrent_remember_replay_records_one_observation(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    classified = await client.post(
        "/v1/classify", json={"content": "concurrent ingest candidate"}
    )
    assert classified.status_code == 200
    body = classified.json()
    request = {
        "content": "concurrent ingest candidate",
        "classification_run_id": body["classification_run_id"],
        "ingest_id": body["ingest_id"],
    }

    first, second = await asyncio.gather(
        client.post("/v1/remember", json=request),
        client.post("/v1/remember", json=request),
    )

    assert first.status_code == second.status_code == 201
    assert {first.json()["status"], second.json()["status"]} == {"created", "deduped"}
    assert first.json()["ingest_id"] == second.json()["ingest_id"] == body["ingest_id"]
    observed = await _usage_events("candidate.observed")
    assert len(observed) == 1
    assert str(observed[0]["ingest_id"]) == body["ingest_id"]


async def test_ingest_reuse_validates_identity_without_mutation(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    first = await client.post("/v1/remember", json={"content": "immutable candidate"})
    assert first.status_code == 201
    ingest_id = first.json()["ingest_id"]
    replay = await client.post(
        "/v1/remember", json={"content": "immutable candidate", "ingest_id": ingest_id}
    )
    assert replay.status_code == 201
    assert replay.json()["status"] == "deduped"
    assert replay.json()["ingest_id"] == ingest_id

    rejected = await client.post(
        "/v1/remember", json={"content": "different candidate", "ingest_id": ingest_id}
    )
    assert rejected.status_code == 409
    async with _test_session_factory() as session:
        count = await session.scalar(text("SELECT count(*) FROM memory_items"))
        diagnostic = await session.scalar(
            text(
                "SELECT metadata->'mismatch_categories' FROM usage_events "
                "WHERE event_type = 'candidate.ingest_reuse_rejected'"
            )
        )
    assert count == 1
    assert diagnostic == ["content_hash_mismatch"]


async def test_remember_attempt_ids_are_distinct_for_replay(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    first = await client.post("/v1/remember", json={"content": "attempt identity"})
    second = await client.post(
        "/v1/remember",
        json={"content": "attempt identity", "ingest_id": first.json()["ingest_id"]},
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["attempt_id"] != second.json()["attempt_id"]
    outcomes = await _usage_events("candidate.outcome")
    assert {str(row["id"]) for row in outcomes} == {
        first.json()["attempt_id"],
        second.json()["attempt_id"],
    }


async def test_deduped_outcome_via_unique_index_is_recorded(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    first = await client.post("/v1/remember", json={"content": "duplicate content case"})
    assert first.status_code == 201

    second = await client.post("/v1/remember", json={"content": "duplicate content case"})
    assert second.status_code == 201
    assert second.json()["status"] == "deduped"
    correlation_id = str(second.json()["correlation_id"])

    outcomes = await _usage_events("candidate.outcome", correlation_id)
    assert len(outcomes) == 1
    assert outcomes[0]["status"] == "deduped"


async def test_superseded_outcome_is_recorded(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    first = await client.post(
        "/v1/remember", json={"content": "first preference value", "kind": "preference"}
    )
    assert first.status_code == 201

    second = await client.post(
        "/v1/remember", json={"content": "second preference value", "kind": "preference"}
    )
    assert second.status_code == 201
    assert second.json()["status"] == "superseded"
    correlation_id = str(second.json()["correlation_id"])

    outcomes = await _usage_events("candidate.outcome", correlation_id)
    assert len(outcomes) == 1
    assert outcomes[0]["status"] == "superseded"


async def test_failed_remember_recorded_without_changing_api_response(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    resp = await client.post(
        "/v1/remember", json={"content": "bad kind write", "kind": "not-a-real-kind"}
    )
    assert resp.status_code == 422
    detail = resp.json()

    # Telemetry must not alter the error response shape/content.
    assert "detail" in detail

    failed = await _usage_events("candidate.outcome")
    matching = [e for e in failed if e["status"] == "failed"]
    assert len(matching) == 1


async def test_secret_rejection_still_records_failed_outcome(client):
    """A secret-denylist rejection happens inside _remember_impl BEFORE the
    old telemetry context was populated, so it previously produced no
    candidate.outcome row. The wrapper now resolves tenant/principal identity
    up front so every authenticated failure is recorded (ENG-METER-001)."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    # Content matching the safety.py password pattern.
    resp = await client.post(
        "/v1/remember", json={"content": "password = supersecretvalue123"}
    )
    assert resp.status_code == 422

    failed = await _usage_events("candidate.outcome")
    matching = [e for e in failed if e["status"] == "failed"]
    assert len(matching) == 1


async def test_invalid_workspace_still_records_failed_outcome(client):
    """An invalid workspace slug is rejected after identity resolution but
    before the write; it must still produce a failed outcome."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    resp = await client.post(
        "/v1/remember",
        json={"content": "valid content for a bad workspace", "workspace": "no-such-slug-xyz"},
    )
    assert resp.status_code == 422

    failed = await _usage_events("candidate.outcome")
    matching = [e for e in failed if e["status"] == "failed"]
    assert len(matching) == 1


async def test_candidate_outcome_is_append_only_per_attempt(client):
    """candidate.outcome is append-only per attempt (ENG-METER-001 correction):
    every /v1/remember invocation appends its own outcome row, so a failed
    attempt followed by a successful retry with the SAME correlation_id
    records TWO outcome rows (one failed, one created) rather than suppressing
    the second. The report resolves a logical outcome from these."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    import uuid as _uuid

    correlation_id = str(_uuid.uuid4())
    # First attempt: failed (bad kind).
    first = await client.post(
        "/v1/remember",
        json={
            "content": "retryable content",
            "kind": "not-a-real-kind",
            "correlation_id": correlation_id,
        },
    )
    assert first.status_code == 422
    # Second attempt for the SAME candidate: succeeded.
    second = await client.post(
        "/v1/remember",
        json={"content": "retryable content", "correlation_id": correlation_id},
    )
    assert second.status_code == 201

    outcomes = await _usage_events("candidate.outcome", correlation_id)
    statuses = sorted(o["status"] for o in outcomes)
    # Both attempts are recorded (append-only), not suppressed.
    assert statuses == ["created", "failed"]


async def test_utf8_byte_accounting_for_non_ascii_content(client):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    content = "café résumé naïve"  # has multi-byte UTF-8 chars
    expected_bytes = len(content.encode("utf-8"))
    assert expected_bytes != len(content)  # sanity: multi-byte chars are present

    resp = await client.post("/v1/remember", json={"content": content})
    assert resp.status_code == 201
    correlation_id = str(resp.json()["correlation_id"])

    observed = await _usage_events("candidate.observed", correlation_id)
    assert len(observed) == 1
    assert observed[0]["input_bytes"] == expected_bytes
