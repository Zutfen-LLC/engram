"""Tests for engram/usage_report.py (ENG-METER-001 Deliverable 8).

Requires a live PostgreSQL with the v2 schema. Skips automatically when no DB
is reachable.

Covers: empty report handling, tenant filtering, date-window filtering,
candidate funnel math, 1-KiB rounding, token/cost coverage percentages,
p50/p90/p99 percentiles, active-principal grouping, job snapshot, storage
snapshot, and a stable/JSON-serializable report shape.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.config import settings
from engram.usage_report import build_report

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


@pytest.fixture(autouse=True)
async def _clean_usage_events():
    if not await _db_ok():
        return
    async with _test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM usage_events"))


@pytest.fixture
async def tenant() -> str:
    async with _test_session_factory() as session:
        tenant_id = str(uuid.uuid4())
        await session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'report-test', :slug)"),
            {"id": tenant_id, "slug": f"report-test-{tenant_id[:8]}"},
        )
        await session.commit()
        yield tenant_id
        await session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})
        await session.commit()


@pytest.fixture
async def principal(tenant: str) -> str:
    async with _test_session_factory() as session:
        principal_id = str(uuid.uuid4())
        await session.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES (:id, :tid, 'report-agent', 'agent')"
            ),
            {"id": principal_id, "tid": tenant},
        )
        await session.commit()
        yield principal_id


async def _insert_event(
    session: AsyncSession,
    *,
    tenant_id: str,
    principal_id: str | None = None,
    event_type: str,
    operation: str,
    status: str,
    input_count: int = 0,
    input_bytes: int = 0,
    prompt_tokens: int | None = None,
    total_tokens: int | None = None,
    reported_cost_usd: float | None = None,
    latency_ms: int | None = None,
    source_type: str | None = None,
    provider_host: str | None = None,
    model: str | None = None,
    dedupe_key: str | None = None,
    created_at: datetime | None = None,
) -> None:
    await session.execute(
        text(
            "INSERT INTO usage_events (tenant_id, principal_id, event_type, operation, status, "
            "input_count, input_bytes, prompt_tokens, total_tokens, reported_cost_usd, "
            "latency_ms, source_type, provider_host, model, dedupe_key, created_at) "
            "VALUES (:tenant_id, :principal_id, :event_type, :operation, :status, "
            ":input_count, :input_bytes, :prompt_tokens, :total_tokens, :reported_cost_usd, "
            ":latency_ms, :source_type, :provider_host, :model, :dedupe_key, "
            "COALESCE(:created_at, now()))"
        ),
        {
            "tenant_id": tenant_id,
            "principal_id": principal_id,
            "event_type": event_type,
            "operation": operation,
            "status": status,
            "input_count": input_count,
            "input_bytes": input_bytes,
            "prompt_tokens": prompt_tokens,
            "total_tokens": total_tokens,
            "reported_cost_usd": reported_cost_usd,
            "latency_ms": latency_ms,
            "source_type": source_type,
            "provider_host": provider_host,
            "model": model,
            "dedupe_key": dedupe_key,
            "created_at": created_at,
        },
    )


async def test_empty_ledger_report_handles_cleanly(tenant):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _test_session_factory() as session:
        report = await build_report(session, tenant_id=tenant)
    assert report["candidate_funnel"]["candidate_observations"] == 0
    assert report["candidate_funnel"]["kib_candidate_units"] == 0
    assert report["by_source_type"] == []
    assert report["by_principal"] == []
    assert report["provider_economics"] == []
    assert report["coverage"]["first_event_at"] is None
    assert any("no usage_events rows" in w for w in report["coverage"]["warnings"])
    # Must still be JSON-serializable.
    json.dumps(report, default=str)


async def test_tenant_filtering_excludes_other_tenants(tenant, principal):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    other_tenant_id = str(uuid.uuid4())
    async with _test_session_factory() as session:
        await session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'other', :slug)"),
            {"id": other_tenant_id, "slug": f"other-{other_tenant_id[:8]}"},
        )
        await _insert_event(
            session,
            tenant_id=tenant,
            principal_id=principal,
            event_type="candidate.observed",
            operation="process_memory_candidate",
            status="accepted_for_processing",
            input_bytes=100,
        )
        await _insert_event(
            session,
            tenant_id=other_tenant_id,
            event_type="candidate.observed",
            operation="process_memory_candidate",
            status="accepted_for_processing",
            input_bytes=999,
        )
        await session.commit()

        report = await build_report(session, tenant_id=tenant)
        await session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": other_tenant_id})
        await session.commit()

    assert report["candidate_funnel"]["candidate_observations"] == 1


async def test_date_window_filtering(tenant, principal):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    now = datetime.now(UTC)
    async with _test_session_factory() as session:
        await _insert_event(
            session,
            tenant_id=tenant,
            principal_id=principal,
            event_type="candidate.observed",
            operation="process_memory_candidate",
            status="accepted_for_processing",
            input_bytes=10,
            created_at=now - timedelta(days=30),
        )
        await _insert_event(
            session,
            tenant_id=tenant,
            principal_id=principal,
            event_type="candidate.observed",
            operation="process_memory_candidate",
            status="accepted_for_processing",
            input_bytes=20,
            created_at=now,
        )
        await session.commit()

        report = await build_report(
            session,
            tenant_id=tenant,
            since=now - timedelta(hours=1),
            until=now + timedelta(hours=1),
        )
    assert report["candidate_funnel"]["candidate_observations"] == 1


async def test_kib_rounding_uses_ceil(tenant, principal):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _test_session_factory() as session:
        # 1025 bytes -> ceil(1025/1024) = 2 KiB units.
        await _insert_event(
            session,
            tenant_id=tenant,
            principal_id=principal,
            event_type="candidate.observed",
            operation="process_memory_candidate",
            status="accepted_for_processing",
            input_bytes=1025,
        )
        await session.commit()
        report = await build_report(session, tenant_id=tenant)
    assert report["candidate_funnel"]["kib_candidate_units"] == 2
    assert report["candidate_funnel"]["flat_candidate_units"] == 1


async def test_candidate_funnel_outcomes(tenant, principal):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _test_session_factory() as session:
        for status in ("created", "created", "deduped", "superseded", "failed"):
            await _insert_event(
                session,
                tenant_id=tenant,
                principal_id=principal,
                event_type="candidate.outcome",
                operation="process_memory_candidate",
                status=status,
            )
        await session.commit()
        report = await build_report(session, tenant_id=tenant)
    funnel = report["candidate_funnel"]
    assert funnel["created"] == 2
    assert funnel["deduped"] == 1
    assert funnel["superseded"] == 1
    assert funnel["failed"] == 1
    assert funnel["remember_attempts"] == 5


async def test_provider_call_token_and_cost_coverage(tenant, principal):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _test_session_factory() as session:
        await _insert_event(
            session,
            tenant_id=tenant,
            principal_id=principal,
            event_type="provider.call",
            operation="classification",
            status="succeeded",
            total_tokens=100,
            reported_cost_usd=0.01,
            provider_host="api.deepinfra.com",
            model="gpt-x",
        )
        await _insert_event(
            session,
            tenant_id=tenant,
            principal_id=principal,
            event_type="provider.call",
            operation="classification",
            status="succeeded",
            total_tokens=None,  # no usage returned this time
            provider_host="api.deepinfra.com",
            model="gpt-x",
        )
        await session.commit()
        report = await build_report(session, tenant_id=tenant)

    assert report["coverage"]["pct_provider_calls_with_tokens"] == 50.0
    assert report["coverage"]["pct_provider_calls_with_cost"] == 50.0
    econ = report["provider_economics"]
    assert len(econ) == 1
    assert econ[0]["calls"] == 2
    assert econ[0]["reported_cost_coverage_pct"] == 50.0


async def test_latency_percentiles(tenant, principal):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _test_session_factory() as session:
        for latency in (100, 200, 300, 400, 500):
            await _insert_event(
                session,
                tenant_id=tenant,
                principal_id=principal,
                event_type="provider.call",
                operation="classification",
                status="succeeded",
                latency_ms=latency,
                total_tokens=10,
            )
        await session.commit()
        report = await build_report(session, tenant_id=tenant)
    econ = report["provider_economics"][0]
    assert econ["latency_p50"] == pytest.approx(300, abs=1)
    assert econ["latency_p90"] is not None
    assert econ["latency_p99"] is not None


async def test_active_principal_grouping(tenant):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _test_session_factory() as session:
        p1 = str(uuid.uuid4())
        p2 = str(uuid.uuid4())
        for pid, name in ((p1, "agent-one"), (p2, "agent-two")):
            await session.execute(
                text(
                    "INSERT INTO principals (id, tenant_id, name, type) "
                    "VALUES (:id, :tid, :name, 'agent')"
                ),
                {"id": pid, "tid": tenant, "name": name},
            )
        await _insert_event(
            session,
            tenant_id=tenant,
            principal_id=p1,
            event_type="candidate.observed",
            operation="process_memory_candidate",
            status="accepted_for_processing",
            input_bytes=10,
        )
        await _insert_event(
            session,
            tenant_id=tenant,
            principal_id=p1,
            event_type="candidate.observed",
            operation="process_memory_candidate",
            status="accepted_for_processing",
            input_bytes=10,
        )
        await _insert_event(
            session,
            tenant_id=tenant,
            principal_id=p2,
            event_type="candidate.observed",
            operation="process_memory_candidate",
            status="accepted_for_processing",
            input_bytes=10,
        )
        await session.commit()
        report = await build_report(session, tenant_id=tenant)
    by_principal = {str(row["principal_id"]): row for row in report["by_principal"]}
    assert by_principal[p1]["candidate_count"] == 2
    assert by_principal[p2]["candidate_count"] == 1


async def test_worker_job_snapshot(tenant):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO jobs (tenant_id, job_type, status, payload) "
                "VALUES (:tid, 'embedding.generate', 'pending', '{}')"
            ),
            {"tid": tenant},
        )
        await session.execute(
            text(
                "INSERT INTO jobs (tenant_id, job_type, status, payload, completed_at) "
                "VALUES (:tid, 'embedding.generate', 'succeeded', '{}', now())"
            ),
            {"tid": tenant},
        )
        await session.commit()
        report = await build_report(session, tenant_id=tenant)
    statuses = {(r["job_type"], r["status"]) for r in report["worker"]["by_job_type_status"]}
    assert ("embedding.generate", "pending") in statuses
    assert ("embedding.generate", "succeeded") in statuses


async def test_storage_snapshot_has_expected_keys(tenant):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _test_session_factory() as session:
        report = await build_report(session, tenant_id=tenant)
    storage = report["storage"]
    for key in (
        "memory_items_total", "memory_items_live", "embeddings_ready",
        "embeddings_pending", "embeddings_failed", "table_bytes", "index_bytes",
        "database_bytes",
    ):
        assert key in storage


async def test_report_shape_is_stable_and_json_serializable(tenant, principal):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _test_session_factory() as session:
        await _insert_event(
            session,
            tenant_id=tenant,
            principal_id=principal,
            event_type="candidate.observed",
            operation="process_memory_candidate",
            status="accepted_for_processing",
            input_bytes=10,
        )
        await session.commit()
        report = await build_report(session, tenant_id=tenant)

    expected_top_level = {
        "tenant_id", "since", "until", "coverage", "candidate_funnel",
        "by_source_type", "by_principal", "provider_economics",
        "conflict_economics", "retrieval", "worker", "storage", "hourly_series",
    }
    assert set(report.keys()) == expected_top_level
    # Must round-trip through JSON with a Decimal/datetime-safe default.
    dumped = json.dumps(report, default=str)
    reloaded = json.loads(dumped)
    assert reloaded["tenant_id"] == tenant
