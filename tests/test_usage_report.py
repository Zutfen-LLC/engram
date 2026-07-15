"""Tests for engram/usage_report.py (ENG-METER-001 Deliverable 8).

Requires a live PostgreSQL with the v2 schema. Skips automatically when no DB
is reachable.

Covers: empty report handling, tenant filtering, date-window filtering,
candidate funnel math, 1-KiB rounding, token/cost coverage percentages,
p50/p90/p99 percentiles, active-principal grouping, job snapshot, storage
snapshot, and a stable/JSON-serializable report shape.
"""

from __future__ import annotations

import hashlib
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
    correlation_id: str | None = None,
    created_at: datetime | None = None,
) -> None:
    await session.execute(
        text(
            "INSERT INTO usage_events (tenant_id, principal_id, event_type, operation, status, "
            "input_count, input_bytes, prompt_tokens, total_tokens, reported_cost_usd, "
            "latency_ms, source_type, provider_host, model, dedupe_key, correlation_id, "
            "created_at) "
            "VALUES (:tenant_id, :principal_id, :event_type, :operation, :status, "
            ":input_count, :input_bytes, :prompt_tokens, :total_tokens, :reported_cost_usd, "
            ":latency_ms, :source_type, :provider_host, :model, :dedupe_key, :correlation_id, "
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
            "correlation_id": correlation_id,
            "created_at": created_at,
        },
    )


async def _insert_memory_item(
    session: AsyncSession,
    *,
    tenant: str,
    principal: str,
    content: str | None = None,
    review_status: str = "active",
    valid_to: str | None = None,
    superseded_by: str | None = None,
) -> str:
    """Insert a minimal memory_items row for storage-section tests."""
    item_id = str(uuid.uuid4())
    raw_content = content or f"content-{item_id[:8]}"
    valid_to_dt = datetime.fromisoformat(valid_to) if valid_to else None
    await session.execute(
        text(
            "INSERT INTO memory_items "
            "(id, tenant_id, principal_id, content, content_hash, kind, "
            "review_status, source_type, authority, memory_confidence, visibility, "
            "valid_to, superseded_by) "
            "VALUES (:id, :tid, :pid, :content, :chash, 'fact', "
            ":review_status, 'manual', 10, 0.5, 'private', :valid_to, :superseded_by)"
        ),
        {
            "id": item_id,
            "tid": tenant,
            "pid": principal,
            "content": raw_content,
            "chash": hashlib.sha256(raw_content.encode("utf-8")).hexdigest(),
            "review_status": review_status,
            "valid_to": valid_to_dt,
            "superseded_by": superseded_by,
        },
    )
    return item_id


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
                correlation_id=str(uuid.uuid4()),
            )
        await session.commit()
        report = await build_report(session, tenant_id=tenant)
    funnel = report["candidate_funnel"]
    # Logical outcomes: one per correlation_id (each test event is a distinct
    # candidate). created/deduped/superseded/failed are the logical counts.
    assert funnel["created"] == 2
    assert funnel["deduped"] == 1
    assert funnel["superseded"] == 1
    assert funnel["failed"] == 1
    assert funnel["remember_attempts"] == 5
    # Attempt-level diagnostics mirror the logical counts (no retries here).
    assert funnel["total_attempts"] == 5
    assert funnel["distinct_candidates"] == 5
    assert funnel["failed_attempts"] == 1
    assert funnel["successful_attempts"] == 4
    assert funnel["attempts_per_candidate_avg"] == 1.0


async def test_candidate_funnel_logical_outcome_resolves_retry(tenant, principal):
    """A failed attempt followed by a successful retry resolves to 'created'
    at the logical-outcome level, while both attempts remain in the attempt
    diagnostics (ENG-METER-001 append-only-attempt correction)."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    retried_cid = str(uuid.uuid4())
    async with _test_session_factory() as session:
        now = datetime.now(UTC)
        # First attempt: failed.
        await _insert_event(
            session,
            tenant_id=tenant,
            principal_id=principal,
            event_type="candidate.outcome",
            operation="process_memory_candidate",
            status="failed",
            correlation_id=retried_cid,
            created_at=now - timedelta(minutes=1),
        )
        # Second attempt for the SAME candidate: succeeded.
        await _insert_event(
            session,
            tenant_id=tenant,
            principal_id=principal,
            event_type="candidate.outcome",
            operation="process_memory_candidate",
            status="created",
            correlation_id=retried_cid,
            created_at=now,
        )
        for operation in ("semantic_recall", "semantic_search"):
            await _insert_event(
                session,
                tenant_id=tenant,
                principal_id=principal,
                event_type="retrieval.request",
                operation=operation,
                status="succeeded",
            )
        await session.commit()
        report = await build_report(session, tenant_id=tenant)
    funnel = report["candidate_funnel"]
    # Logical: the retry succeeded, so this candidate counts as 'created', NOT 'failed'.
    assert funnel["created"] == 1
    assert funnel["failed"] == 0
    assert funnel["distinct_candidates"] == 1
    assert funnel["logical_candidates"] == 1
    assert funnel["remember_attempts"] == 2
    # Attempt-level: both attempts are visible (one failed, one succeeded).
    assert funnel["total_attempts"] == 2
    assert funnel["failed_attempts"] == 1
    assert funnel["successful_attempts"] == 1
    assert funnel["attempts_per_candidate_avg"] == 2.0
    principal_row = next(
        row for row in report["by_principal"] if str(row["principal_id"]) == principal
    )
    assert principal_row["created_count"] == 1
    assert report["retrieval"]["semantic_queries_per_created_memory"] == 2.0


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
    assert econ[0]["actual_calls"] == 2
    assert econ[0]["reported_cost_coverage_pct"] == 50.0


async def test_disabled_provider_calls_excluded_from_coverage(tenant, principal):
    """``disabled`` provider.call events are not external calls and must be
    excluded from the actual-call and usage-coverage denominators
    (ENG-METER-001 correction)."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _test_session_factory() as session:
        # One real call with tokens.
        await _insert_event(
            session,
            tenant_id=tenant,
            principal_id=principal,
            event_type="provider.call",
            operation="classification",
            status="succeeded",
            total_tokens=100,
            provider_host="api.openai.com",
            model="gpt-x",
        )
        # Two disabled rows (provider='none') — not external calls.
        for _ in range(2):
            await _insert_event(
                session,
                tenant_id=tenant,
                principal_id=principal,
                event_type="provider.call",
                operation="classification",
                status="disabled",
            )
        await session.commit()
        report = await build_report(session, tenant_id=tenant)
    coverage = report["coverage"]
    assert coverage["provider_calls_total"] == 3
    assert coverage["provider_actual_calls"] == 1
    assert coverage["provider_disabled_calls"] == 2
    # The single actual call carried tokens → 100%, not dragged down by disabled.
    assert coverage["pct_provider_calls_with_tokens"] == 100.0
    econ = report["provider_economics"]
    assert sum(row["calls"] for row in econ) == 3
    assert sum(row["actual_calls"] for row in econ) == 1
    assert sum(row["disabled_n"] for row in econ) == 2
    hourly = report["hourly_series"]
    assert sum(row["provider_calls"] for row in hourly) == 3
    assert sum(row["actual_provider_calls"] for row in hourly) == 1


async def test_disabled_excluded_from_conflict_and_query_embedding_counts(tenant, principal):
    """Disabled provider rows must not inflate the conflict_classifications
    or query_embedding actual-call counts (ENG-METER-001 correction). Both
    sections expose the total (with disabled) AND the actual-call count
    (without) so a disabled-provider deployment does not overstate inference
    volume."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _test_session_factory() as session:
        # One real conflict_classification call + one disabled.
        await _insert_event(
            session, tenant_id=tenant, principal_id=principal,
            event_type="provider.call", operation="conflict_classification",
            status="succeeded", total_tokens=50,
        )
        await _insert_event(
            session, tenant_id=tenant, principal_id=principal,
            event_type="provider.call", operation="conflict_classification",
            status="disabled",
        )
        # One real query embedding call + one disabled.
        await _insert_event(
            session, tenant_id=tenant, principal_id=principal,
            event_type="provider.call", operation="embedding_query_search",
            status="succeeded", total_tokens=10,
        )
        await _insert_event(
            session, tenant_id=tenant, principal_id=principal,
            event_type="provider.call", operation="embedding_query_search",
            status="disabled",
        )
        # Need a candidate.observed so the per-1000 ratio has a denominator.
        await _insert_event(
            session, tenant_id=tenant, principal_id=principal,
            event_type="candidate.observed", operation="process_memory_candidate",
            status="accepted_for_processing", input_bytes=10,
        )
        await session.commit()
        report = await build_report(session, tenant_id=tenant)
    conflict = report["conflict_economics"]
    # Total includes disabled; actual excludes it.
    assert conflict["conflict_classifications"] == 2
    assert conflict["conflict_actual_calls"] == 1
    # The per-1000 ratio uses actual calls (1 per 1 observation = 1000).
    assert conflict["conflict_calls_per_1000_candidate_observations"] == 1000.0
    retrieval = report["retrieval"]
    assert retrieval["query_embedding_calls"] == 2
    assert retrieval["query_embedding_actual_calls"] == 1


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
    # New keys from the storage-economics correction (ENG-METER-001).
    for key in (
        "memory_items_archived", "memory_items_invalidated",
        "memory_items_superseded", "global_physical_bytes",
        "logical_tenant_bytes", "bytes_per_retained_memory",
        "bytes_per_retained_memory_note", "warnings",
    ):
        assert key in storage


async def test_storage_invalidated_excludes_superseded(tenant, principal):
    """``invalidated_n`` counts manually-invalidated rows only — superseded
    rows are counted in ``superseded_n``, NOT double-counted in invalidated
    (ENG-METER-001 correction: the original condition lacked
    ``superseded_by IS NULL``)."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _test_session_factory() as session:
        # The superseding item must exist first (FK on superseded_by).
        other_item = await _insert_memory_item(
            session, tenant=tenant, principal=principal, content="superseder"
        )
        # A live item.
        await _insert_memory_item(session, tenant=tenant, principal=principal)
        # An archived item (review_status='archived').
        await _insert_memory_item(
            session, tenant=tenant, principal=principal, review_status="archived",
            valid_to="2026-01-01T00:00:00Z",
        )
        # A superseded item: valid_to set AND superseded_by set.
        await _insert_memory_item(
            session, tenant=tenant, principal=principal, review_status="active",
            valid_to="2026-01-01T00:00:00Z", superseded_by=other_item,
        )
        # A manually invalidated item: valid_to set, not archived/rejected,
        # NOT superseded.
        await _insert_memory_item(
            session, tenant=tenant, principal=principal, review_status="active",
            valid_to="2026-01-02T00:00:00Z",
        )
        await session.commit()
        report = await build_report(session, tenant_id=tenant)
    storage = report["storage"]
    assert storage["memory_items_archived"] == 1
    assert storage["memory_items_superseded"] == 1
    # The superseded row must NOT appear in invalidated (only the manual one).
    assert storage["memory_items_invalidated"] == 1


async def test_storage_tenant_scoped_suppresses_bytes_per_memory(tenant):
    """Under --tenant the global physical relation size cannot be attributed
    to one tenant, so bytes_per_retained_memory is suppressed (None) and a
    warning explains why (ENG-METER-001 correction)."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _test_session_factory() as session:
        report = await build_report(session, tenant_id=tenant)
    storage = report["storage"]
    assert storage["bytes_per_retained_memory"] is None
    assert storage["bytes_per_retained_memory_note"] is not None
    assert any("suppressed" in w or "global" in w for w in storage["warnings"])


async def test_storage_deployment_wide_reports_bytes_per_memory():
    """A deployment-wide (no --tenant) report computes bytes_per_retained_memory
    against the total row count (all retained rows occupy storage, not just
    live ones) — ENG-METER-001 correction."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _test_session_factory() as session:
        report = await build_report(session, tenant_id=None)
    storage = report["storage"]
    # When there is at least one memory_items row, the deployment-wide ratio is
    # a real number; when there are zero rows it is None (avoid divide-by-zero).
    total = storage["memory_items_total"] or 0
    if total:
        assert storage["bytes_per_retained_memory"] is not None
        assert storage["bytes_per_retained_memory"] > 0
    else:
        assert storage["bytes_per_retained_memory"] is None


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
