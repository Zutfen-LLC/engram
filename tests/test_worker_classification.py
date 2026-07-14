"""Integration tests for async LLM classification refinement (ENG-AUD-008).

These require a live PostgreSQL with the v2 schema. They skip automatically when
no DB is reachable, matching test_remember.py.

Covers:
* /v1/remember with omitted kind does NOT call OpenAI synchronously
* rule-based classification still stores a safe initial memory
* the refinement job updates kind/wing/room only above the confidence threshold
* refinement can narrow visibility but NEVER widen it (ENG-AUD-005)
* refinement writes item events / provenance
* rerunning the job is idempotent (no oscillation)
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.api.app import create_app
from engram.classification import ClassificationResult
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
        await conn.execute(text("DELETE FROM item_events"))
        await conn.execute(text("DELETE FROM memory_embeddings"))
        await conn.execute(text("DELETE FROM classification_runs"))
        await conn.execute(text("DELETE FROM memory_items"))
        await conn.execute(
            text(
                "UPDATE tenant_config SET auto_promote_evidence_enabled = FALSE, "
                "auto_promote_evidence_threshold = 0.7 WHERE active = TRUE"
            )
        )


async def _remember(client, **fields) -> str:
    payload = {"content": "classify me", "source_type": "manual"}
    payload.update(fields)
    response = await client.post("/v1/remember", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _fetch_item(item_id: str) -> dict[str, object]:
    async with _test_session_factory() as session:
        return (
            (
                await session.execute(
                    text(
                        "SELECT kind, wing, room, visibility, review_status, "
                        "memory_confidence, source_trust "
                        "FROM memory_items WHERE id = :id"
                    ),
                    {"id": item_id},
                )
            )
            .mappings()
            .one()
        )


async def _event_count(item_id: str) -> int:
    async with _test_session_factory() as session:
        return (
            await session.execute(
                text("SELECT count(*) FROM item_events WHERE item_id = :id"),
                {"id": item_id},
            )
        ).scalar_one()


async def _run_refine_job(item_id: str) -> None:
    """Enqueue + run a classification.refine job for the item."""
    from engram.jobs import enqueue_job
    from engram.worker import process_one_job

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
            )
            .mappings()
            .one()
        )
        await apply_rls_context(
            session, tenant_id=row["tenant_id"], principal_id=row["principal_id"]
        )
        await enqueue_job(
            session,
            tenant_id=row["tenant_id"],
            job_type="classification.refine",
            payload={"memory_item_id": item_id},
        )

    await process_one_job(
        worker_id="test",
        session_factory=_test_session_factory,
        app_session_factory=_test_session_factory,
        job_types=["classification.refine"],
    )


async def test_remember_omitted_kind_does_not_call_openai(client, monkeypatch):
    """The synchronous write path uses rule-based classification only."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    import engram.classification as classification_mod

    original = classification_mod._call_openai_classification
    called = False

    async def spy(prompt: str):
        nonlocal called
        called = True
        return await original(prompt)  # type: ignore[misc]

    monkeypatch.setattr(classification_mod, "_call_openai_classification", spy)
    settings.classification_provider = "openai"

    response = await client.post("/v1/remember", json={"content": "rule-based only on write path"})
    assert response.status_code == 201
    assert not called, "the write path must not call OpenAI synchronously"


async def test_refinement_updates_kind_above_threshold(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")

    item_id = await _remember(client, content="some decision memory")

    async def fake_classify(content, tenant_id, session, context=None):
        return ClassificationResult(
            suggested_kind="decision",
            suggested_wing="project",
            suggested_room="backlog",
            confidence=0.9,
            reason="LLM refinement",
            rules_matched=[],
            provenance={"provider": "openai", "mode": "llm"},
        )

    monkeypatch.setattr("engram.classification.classify", fake_classify)
    await _run_refine_job(item_id)

    row = await _fetch_item(item_id)
    assert row["kind"] == "decision"
    assert row["wing"] == "project"
    assert row["room"] == "backlog"


async def test_refinement_narrows_visibility_never_widens(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")

    # Start at tenant visibility (fairly broad).
    item_id = await _remember(client, content="visibility narrow test", visibility="tenant")

    # First refinement: LLM suggests workspace (narrower) → applied.
    async def suggest_workspace(content, tenant_id, session, context=None):
        return ClassificationResult(
            suggested_kind="fact",
            suggested_visibility="workspace",
            confidence=0.9,
            reason="narrow to workspace",
            rules_matched=[],
            provenance={"provider": "openai", "mode": "llm"},
        )

    monkeypatch.setattr("engram.classification.classify", suggest_workspace)
    await _run_refine_job(item_id)
    assert (await _fetch_item(item_id))["visibility"] == "workspace"

    # Second refinement: LLM suggests tenant (WIDER) → must NOT widen.
    async def suggest_tenant(content, tenant_id, session, context=None):
        return ClassificationResult(
            suggested_kind="fact",
            suggested_visibility="tenant",
            confidence=0.9,
            reason="try to widen",
            rules_matched=[],
            provenance={"provider": "openai", "mode": "llm"},
        )

    monkeypatch.setattr("engram.classification.classify", suggest_tenant)
    await _run_refine_job(item_id)
    assert (await _fetch_item(item_id))["visibility"] == "workspace"


async def test_refinement_writes_item_events(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    item_id = await _remember(client, content="event provenance test")

    async def fake_classify(content, tenant_id, session, context=None):
        return ClassificationResult(
            suggested_kind="decision",
            suggested_visibility="private",  # narrower than default workspace
            confidence=0.9,
            reason="record an event",
            rules_matched=[],
            provenance={"provider": "openai", "mode": "llm"},
        )

    monkeypatch.setattr("engram.classification.classify", fake_classify)
    before = await _event_count(item_id)
    await _run_refine_job(item_id)
    after = await _event_count(item_id)
    assert after > before, "refinement must write at least one item event"
    async with _test_engine.connect() as conn:
        bound_at = await conn.scalar(
            text("SELECT bound_at FROM classification_runs WHERE memory_item_id=:id"),
            {"id": item_id},
        )
    assert bound_at is not None


async def test_refinement_rerun_is_idempotent(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    item_id = await _remember(client, content="idempotent refinement")

    async def fake_classify(content, tenant_id, session, context=None):
        return ClassificationResult(
            suggested_kind="decision",
            suggested_visibility="private",
            confidence=0.9,
            reason="stable suggestion",
            rules_matched=[],
            provenance={"provider": "openai", "mode": "llm"},
        )

    monkeypatch.setattr("engram.classification.classify", fake_classify)
    await _run_refine_job(item_id)
    state_after_first = await _fetch_item(item_id)
    events_after_first = await _event_count(item_id)

    # Run again with the SAME suggestion: bound evidence makes the job a no-op.
    await _run_refine_job(item_id)
    state_after_second = await _fetch_item(item_id)

    assert state_after_second == state_after_first
    assert state_after_second["memory_confidence"] == state_after_first["memory_confidence"]
    events_after_second = await _event_count(item_id)
    assert events_after_second == events_after_first


async def test_refinement_skips_below_threshold(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    item_id = await _remember(client, content="low confidence refine")
    before = await _fetch_item(item_id)

    async def fake_classify(content, tenant_id, session, context=None):
        return ClassificationResult(
            suggested_kind="doctrine",  # would change kind if applied
            confidence=0.1,  # well below the default 0.5 threshold
            reason="low confidence",
            rules_matched=[],
            provenance={"provider": "openai", "mode": "llm"},
        )

    monkeypatch.setattr("engram.classification.classify", fake_classify)
    await _run_refine_job(item_id)
    after = await _fetch_item(item_id)
    # kind/wing/room unchanged because confidence < threshold.
    assert after["kind"] == before["kind"]


async def test_refinement_schedules_from_reloaded_final_kind_and_reports_final_state(
    client, monkeypatch
):
    """A fact -> decision mutation qualifies from persisted state, exactly once."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    item_id = await _remember(
        client,
        content="we decided to retain this deployment choice",
        source_type="session_end",
        kind="fact",
        visibility="tenant",
    )
    async with _test_session_factory() as session:
        from engram.db import (
            _DEFAULT_PRINCIPAL_NAME,
            _DEFAULT_TENANT_SLUG,
            apply_rls_context,
        )

        context = (
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
            )
            .mappings()
            .one()
        )
        await apply_rls_context(
            session,
            tenant_id=context["tenant_id"],
            principal_id=context["principal_id"],
        )
        await session.execute(
            text(
                "UPDATE tenant_config SET auto_promote_evidence_enabled = TRUE "
                "WHERE tenant_id = :tenant_id AND active = TRUE"
            ),
            {"tenant_id": context["tenant_id"]},
        )
        await session.commit()

    async def qualifying_decision(content, tenant_id, session, context=None):
        return ClassificationResult(
            suggested_kind="decision",
            suggested_visibility="workspace",
            taxonomy_confidence=0.9,
            retention_confidence=0.9,
            retention_disposition="retain",
            reason="durable decision",
            rules_matched=[],
            provenance={"provider": "test", "mode": "fixture"},
        )

    monkeypatch.setattr("engram.classification.classify", qualifying_decision)
    await _run_refine_job(item_id)
    async with _test_session_factory() as session:
        await apply_rls_context(
            session,
            tenant_id=context["tenant_id"],
            principal_id=context["principal_id"],
        )
        jobs = (
            (
                await session.execute(
                    text(
                    "SELECT payload, payload->>'dedupe_key' AS dedupe_key FROM jobs "
                        "WHERE job_type = 'promotion.path_a' AND payload->>'memory_item_id' = :id"
                    ),
                    {"id": item_id},
                )
            )
            .mappings()
            .all()
        )
        event_payload = (
            await session.execute(
                text(
                    "SELECT new_value::jsonb FROM item_events WHERE item_id = :id "
                    "AND event_type = 'classification' ORDER BY created_at DESC LIMIT 1"
                ),
                {"id": item_id},
            )
        ).scalar_one()
    assert len(jobs) == 1
    assert jobs[0]["dedupe_key"].startswith(f"promotion.path_a:{item_id}:")
    assert event_payload["previous_kind"] == "fact"
    assert event_payload["final_kind"] == "decision"
    assert event_payload["final_visibility"] == "workspace"

    await _run_refine_job(item_id)
    async with _test_session_factory() as session:
        await apply_rls_context(
            session,
            tenant_id=context["tenant_id"],
            principal_id=context["principal_id"],
        )
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM jobs WHERE job_type = 'promotion.path_a' "
                    "AND payload->>'memory_item_id' = :id"
                ),
                {"id": item_id},
            )
        ).scalar_one()
        await session.execute(
            text(
                "UPDATE tenant_config SET auto_promote_evidence_enabled = FALSE "
                "WHERE tenant_id = :tenant_id AND active = TRUE"
            ),
            {"tenant_id": context["tenant_id"]},
        )
        await session.commit()
    assert count == 1
