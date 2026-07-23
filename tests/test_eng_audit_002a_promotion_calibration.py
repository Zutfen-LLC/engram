"""Database-backed regression proof for ENG-AUDIT-002A promotion calibration."""

from __future__ import annotations

import json
import os
import uuid
from datetime import timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.api.app import create_app
from engram.config import settings
from engram.db import apply_rls_context, get_session

_test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_test_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    _test_engine, class_=AsyncSession, expire_on_commit=False
)
_app_engine = create_async_engine(
    os.environ.get("ENGRAM_APP_DATABASE_URL", settings.database_url), poolclass=NullPool
)
_app_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    _app_engine, class_=AsyncSession, expire_on_commit=False
)
_tenant_id: str | None = None
_admin_id: str | None = None


@pytest.fixture(autouse=True)
async def _fresh_engine() -> Any:
    global _app_engine, _app_session_factory, _test_engine, _test_session_factory
    _test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
    _test_session_factory = async_sessionmaker(
        _test_engine, class_=AsyncSession, expire_on_commit=False
    )
    _app_engine = create_async_engine(
        os.environ.get("ENGRAM_APP_DATABASE_URL", settings.database_url), poolclass=NullPool
    )
    _app_session_factory = async_sessionmaker(
        _app_engine, class_=AsyncSession, expire_on_commit=False
    )
    yield
    await _app_engine.dispose()
    await _test_engine.dispose()


async def _db_ok() -> bool:
    if not os.environ.get("ENGRAM_APP_DATABASE_URL"):
        return False
    try:
        async with _test_engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        async with _app_engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.fixture(autouse=True)
def _restore_mutated_settings() -> Any:
    names = (
        "classification_provider",
        "classification_confidence_threshold",
        "classification_retention_confidence_threshold",
        "embedding_provider",
    )
    previous = {name: getattr(settings, name) for name in names if hasattr(settings, name)}
    try:
        yield
    finally:
        for name, value in previous.items():
            setattr(settings, name, value)


@pytest.fixture(autouse=True)
async def _isolated_test_tenant(_fresh_engine: Any) -> Any:
    global _tenant_id, _admin_id
    if not await _db_ok():
        yield
        return

    tenant_id = str(uuid.uuid4())
    admin_id = str(uuid.uuid4())
    slug = f"eng-audit-002a-{uuid.uuid4().hex}"
    async with _test_engine.connect() as connection:
        default_before = (
            await connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM memory_items WHERE tenant_id=t.id), "
                    "(SELECT count(*) FROM jobs WHERE tenant_id=t.id), "
                    "(SELECT count(*) FROM item_events e JOIN memory_items m ON m.id=e.item_id "
                    " WHERE m.tenant_id=t.id) "
                    "FROM tenants t WHERE t.slug='default'"
                )
            )
        ).one()

    async with _test_engine.begin() as connection:
        await connection.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
            {"id": tenant_id, "name": slug, "slug": slug},
        )
        await connection.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES (:id, :tenant_id, 'admin', 'admin')"
            ),
            {"id": admin_id, "tenant_id": tenant_id},
        )
        await connection.execute(
            text("INSERT INTO tenant_config (tenant_id) VALUES (:id)"),
            {"id": tenant_id},
        )
        await connection.execute(
            text(
                "UPDATE tenant_config SET auto_promote_enabled=TRUE, "
                "auto_promote_evidence_enabled=TRUE, auto_promote_evidence_threshold=0.70, "
                "auto_promote_min_age_hours=72 WHERE tenant_id=:id AND active=TRUE"
            ),
            {"id": tenant_id},
        )
        await connection.execute(
            text(
                "UPDATE memory_kinds SET auto_promote_from_inferred=TRUE, enabled=TRUE "
                "WHERE tenant_id=:id AND name='fact'"
            ),
            {"id": tenant_id},
        )

    _tenant_id, _admin_id = tenant_id, admin_id
    try:
        yield
    finally:
        async with _test_engine.begin() as connection:
            await connection.execute(text("DELETE FROM tenants WHERE id=:id"), {"id": tenant_id})
        async with _test_engine.connect() as connection:
            default_after = (
                await connection.execute(
                    text(
                        "SELECT "
                        "(SELECT count(*) FROM memory_items WHERE tenant_id=t.id), "
                        "(SELECT count(*) FROM jobs WHERE tenant_id=t.id), "
                        "(SELECT count(*) FROM item_events e JOIN memory_items m ON m.id=e.item_id "
                        " WHERE m.tenant_id=t.id) "
                        "FROM tenants t WHERE t.slug='default'"
                    )
                )
            ).one()
        assert default_after == default_before
        _tenant_id = _admin_id = None


async def _get_test_session() -> Any:
    assert _tenant_id is not None and _admin_id is not None
    async with _app_session_factory() as session:
        await apply_rls_context(session, tenant_id=_tenant_id, principal_id=_admin_id)
        yield session


@pytest.fixture
def app() -> Any:
    app = create_app()
    app.dependency_overrides[get_session] = _get_test_session
    return app


@pytest.fixture
async def client(app: Any) -> Any:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
        yield api


async def _remember(client: AsyncClient, **fields: Any) -> dict[str, Any]:
    payload = {
        "content": f"classification calibration {uuid.uuid4()}",
        "source_type": "manual",
    }
    payload.update(fields)
    response = await client.post("/v1/remember", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def _jobs_for_item(item_id: str) -> list[dict[str, Any]]:
    async with _test_session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id::text, tenant_id::text, job_type, status, attempts, run_after, "
                    "completed_at, payload FROM jobs "
                    "WHERE payload->>'memory_item_id'=:id ORDER BY created_at, id"
                ),
                {"id": item_id},
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def _pipeline_proof(item_id: str) -> dict[str, Any]:
    async with _test_session_factory() as session:
        item = dict(
            (
                await session.execute(
                    text(
                        "SELECT id::text, tenant_id::text, principal_id::text, kind, "
                        "review_status, retention_disposition, retention_confidence, "
                        "retention_evidence_at, created_at "
                        "FROM memory_items WHERE id=:id"
                    ),
                    {"id": item_id},
                )
            ).mappings().one()
        )
        runs = [
            dict(row)
            for row in (
                await session.execute(
                    text(
                        "SELECT cr.id::text, cr.tenant_id::text, cr.principal_id::text, "
                        "cr.ingest_id::text, cr.memory_item_id::text, cr.bound_at, cr.created_at, "
                        "cr.suggested_kind, cr.taxonomy_confidence, cr.retention_confidence, "
                        "cr.retention_disposition, cr.provenance, p.type AS principal_type, "
                        "p.internal_key FROM classification_runs cr "
                        "JOIN principals p ON p.id=cr.principal_id "
                        "WHERE cr.memory_item_id=:id ORDER BY cr.created_at, cr.id"
                    ),
                    {"id": item_id},
                )
            ).mappings().all()
        ]
        events = [
            dict(row)
            for row in (
                await session.execute(
                    text(
                        "SELECT id::text, actor_principal_id::text, new_value "
                        "FROM item_events WHERE item_id=:id AND event_type='classification' "
                        "AND new_value::jsonb->>'worker_operation'='classification.refine' "
                        "ORDER BY created_at, id"
                    ),
                    {"id": item_id},
                )
            ).mappings().all()
        ]
        min_age = (
            await session.execute(
                text(
                    "SELECT auto_promote_min_age_hours FROM tenant_config "
                    "WHERE tenant_id=:id AND active=TRUE"
                ),
                {"id": item["tenant_id"]},
            )
        ).scalar_one()
    return {
        "item": item,
        "runs": runs,
        "events": events,
        "jobs": await _jobs_for_item(item_id),
        "min_age": min_age,
    }


async def test_rule_only_path_enqueues_classification_refine(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema")
    monkeypatch.setattr(settings, "classification_provider", "openai")
    result = await _remember(client, source_type="sync_turn")
    item_id = result["id"]
    refine = [
        job
        for job in await _jobs_for_item(item_id)
        if job["job_type"] == "classification.refine"
    ]
    assert len(refine) == 1
    assert refine[0]["status"] == "pending"
    assert refine[0]["attempts"] == 0
    assert refine[0]["tenant_id"] == _tenant_id
    assert refine[0]["payload"] == {
        "memory_item_id": item_id,
        "correlation_id": result["correlation_id"],
        "ingest_id": result["ingest_id"],
        "dedupe_key": f"classification.refine:{item_id}",
    }


async def test_receipt_path_does_not_enqueue_classification_refine(client: AsyncClient) -> None:
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema")
    content = f"receipt-backed classification {uuid.uuid4()}"
    classified = await client.post(
        "/v1/classify", json={"content": content, "source_type": "manual"}
    )
    assert classified.status_code == 200, classified.text
    receipt = classified.json()
    remembered = await client.post(
        "/v1/remember",
        json={
            "content": content,
            "source_type": "manual",
            "classification_run_id": receipt["classification_run_id"],
            "ingest_id": receipt["ingest_id"],
            "correlation_id": receipt["correlation_id"],
        },
    )
    assert remembered.status_code == 201, remembered.text
    jobs = await _jobs_for_item(remembered.json()["id"])
    assert "classification.refine" not in {job["job_type"] for job in jobs}


async def test_classification_refine_job_dedupe_on_api_replay(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema")
    monkeypatch.setattr(settings, "classification_provider", "openai")
    content = f"API replay dedupe {uuid.uuid4()}"
    first = await _remember(client, content=content, source_type="sync_turn")
    replay = await client.post(
        "/v1/remember", json={"content": content, "source_type": "sync_turn"}
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["status"] == "deduped"
    assert replay.json()["id"] == first["id"]
    refine = [
        job
        for job in await _jobs_for_item(first["id"])
        if job["job_type"] == "classification.refine"
    ]
    assert len(refine) == 1
    assert refine[0]["payload"]["dedupe_key"] == f"classification.refine:{first['id']}"


async def test_classification_refine_proves_complete_pipeline_and_idempotent_replay(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema")

    from engram.classification import ClassificationResult
    from engram.models import Job
    from engram.worker import handle_classification_refine, process_one_job

    monkeypatch.setattr(settings, "classification_provider", "openai")
    monkeypatch.setattr(settings, "embedding_provider", "none")
    classify_calls = 0

    async def fake_classify(*_args: Any, **_kwargs: Any) -> ClassificationResult:
        nonlocal classify_calls
        classify_calls += 1
        return ClassificationResult(
            suggested_kind="fact",
            suggested_wing=None,
            suggested_room=None,
            taxonomy_confidence=0.85,
            reason="deterministic qualified refinement",
            rules_matched=[],
            provenance={"provider": "test-double", "mode": "deterministic"},
            retention_confidence=0.90,
            retention_disposition="retain",
        )

    monkeypatch.setattr("engram.classification.classify", fake_classify)
    result = await _remember(client, source_type="sync_turn")
    item_id = result["id"]
    async with _test_engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE jobs SET priority=-2147483648, run_after='1970-01-01' "
                "WHERE job_type='classification.refine' AND payload->>'memory_item_id'=:id"
            ),
            {"id": item_id},
        )

    processed = await process_one_job(
        worker_id="eng-audit-002a",
        session_factory=_test_session_factory,
        app_session_factory=_app_session_factory,
        job_types=["classification.refine"],
    )
    assert processed is True
    assert classify_calls == 1

    proof = await _pipeline_proof(item_id)
    item = proof["item"]
    assert len(proof["runs"]) == 1
    run = proof["runs"][0]
    assert run["memory_item_id"] == item_id
    assert run["tenant_id"] == item["tenant_id"] == _tenant_id
    assert run["bound_at"] is not None
    assert run["principal_type"] == "system"
    assert run["internal_key"] == "classification_automation"
    assert run["suggested_kind"] == "fact"
    assert run["retention_disposition"] == "retain"
    assert run["retention_confidence"] == pytest.approx(0.90)
    assert run["taxonomy_confidence"] == pytest.approx(0.85)
    assert run["provenance"]["provider"] == "test-double"

    assert item["kind"] == "fact"
    assert item["review_status"] == "proposed"
    assert item["retention_disposition"] == "retain"
    assert item["retention_confidence"] == pytest.approx(0.90)
    assert item["retention_evidence_at"] == run["created_at"]

    assert len(proof["events"]) == 1
    event = proof["events"][0]
    event_payload = json.loads(event["new_value"])
    assert event_payload["classification_run_id"] == run["id"]
    assert event_payload["worker_operation"] == "classification.refine"
    assert event_payload["retention_disposition"] == "retain"
    assert event_payload["retention_confidence"] == pytest.approx(0.90)
    assert event_payload["taxonomy_confidence"] == pytest.approx(0.85)
    assert event_payload["promotion_schedule_status"] == "scheduled"
    assert event_payload["promotion_job_id"]
    assert event["actor_principal_id"] == run["principal_id"]

    refine_jobs = [job for job in proof["jobs"] if job["job_type"] == "classification.refine"]
    promotion_jobs = [job for job in proof["jobs"] if job["job_type"] == "promotion.path_a"]
    assert len(refine_jobs) == 1
    assert refine_jobs[0]["status"] == "succeeded"
    assert refine_jobs[0]["attempts"] == 1
    assert refine_jobs[0]["completed_at"] is not None
    assert refine_jobs[0]["payload"]["ingest_id"] == run["ingest_id"]
    assert len(promotion_jobs) == 1
    promotion = promotion_jobs[0]
    assert promotion["status"] == "pending"
    assert promotion["tenant_id"] == item["tenant_id"]
    assert promotion["payload"] == {
        "memory_item_id": item_id,
        "classification_run_id": run["id"],
        "ingest_id": run["ingest_id"],
        "dedupe_key": f"promotion.path_a:{item_id}:{run['id']}",
    }
    assert proof["min_age"] == 72
    assert promotion["run_after"] == max(
        item["created_at"], item["retention_evidence_at"]
    ) + timedelta(hours=72)

    evidence_at = item["retention_evidence_at"]
    async with _app_session_factory() as session:
        await apply_rls_context(
            session,
            tenant_id=item["tenant_id"],
            principal_id=item["principal_id"],
        )
        refine_job = (
            await session.execute(select(Job).where(Job.id == uuid.UUID(refine_jobs[0]["id"])))
        ).scalar_one()
        await handle_classification_refine(session, refine_job)
        await session.commit()

    replay = await _pipeline_proof(item_id)
    assert classify_calls == 1
    assert len(replay["runs"]) == 1
    assert len(replay["events"]) == 1
    assert len([job for job in replay["jobs"] if job["job_type"] == "promotion.path_a"]) == 1
    assert replay["item"]["retention_evidence_at"] == evidence_at
    assert replay["item"]["retention_disposition"] == item["retention_disposition"]
    assert replay["item"]["retention_confidence"] == item["retention_confidence"]
    assert replay["item"]["kind"] == item["kind"]
    assert replay["item"]["review_status"] == item["review_status"]


async def test_explicit_kind_does_not_enqueue_classification_refine(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit caller-selected kind remains outside this refinement policy slice."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema")
    monkeypatch.setattr(settings, "classification_provider", "openai")
    result = await _remember(client, kind="fact")
    jobs = await _jobs_for_item(result["id"])
    assert "classification.refine" not in {job["job_type"] for job in jobs}


async def test_provider_disabled_does_not_enqueue_classification_refine(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The disabled-provider case is independent of prior provider-enabled tests."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema")
    monkeypatch.setattr(settings, "classification_provider", "none")
    result = await _remember(client, source_type="sync_turn")
    jobs = await _jobs_for_item(result["id"])
    assert "classification.refine" not in {job["job_type"] for job in jobs}
