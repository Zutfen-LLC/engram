"""Real-PostgreSQL proofs for ``engram doctor`` (ENG-LOOP-001A).

Exercises ``engram.doctor.run_doctor`` end-to-end against a live PostgreSQL +
pgvector database: worker-queue states, lifecycle/candidate evidence, recall
activity, Context Receipt evidence, cross-tenant isolation, the no-mutation
guarantee, and the no-external-provider-call guarantee.

HTTP is mocked (``httpx.MockTransport``) since none of these proofs depend on
the HTTP layer touching the database — only ``run_doctor``'s DB-backed checks
do, and those run against the real database via a per-test NullPool session
factory (mirroring ``tests/test_promotion.py``). Skips automatically when no
DB is reachable; ``ENGRAM_FAIL_ON_DB_SKIP=1`` makes that skip fail the run
instead (see ``tests/conftest.py``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.config import settings
from engram.context_receipts import store_context_receipt
from engram.doctor import run_doctor
from tests.context_receipt_helpers import build_manifest, manifest_hash

_DB_SKIP_REASON = "requires a live PostgreSQL with the v2 schema"

_test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_test_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    _test_engine, class_=AsyncSession, expire_on_commit=False
)


@pytest.fixture(autouse=True)
async def _fresh_engine():
    """Fresh NullPool engine per test — avoids cross-event-loop asyncpg errors."""
    global _test_engine, _test_session_factory
    _test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
    _test_session_factory = async_sessionmaker(
        _test_engine, class_=AsyncSession, expire_on_commit=False
    )
    yield
    # Every test in this module seeds its own tenant(s) (never the seeded
    # 'default' tenant) and never mutates anything itself (that is the whole
    # point of the no-mutation proof), so cleanup here is safe: deleting the
    # tenant cascades to its principals/recall_logs/context_receipts/jobs/
    # usage_events/tenant_config. Without this, orphaned context_receipts
    # rows survive across test MODULES and their ON DELETE RESTRICT to
    # recall_logs breaks other files' unscoped `DELETE FROM recall_logs`
    # cleanup fixtures (e.g. tests/test_graph_recall.py).
    async with _test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM tenants WHERE slug != 'default'"))
    await _test_engine.dispose()


async def _db_ok() -> bool:
    try:
        async with _test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _session_factory() -> Any:
    return _test_session_factory


def _healthy_handler(tenant_id: uuid.UUID) -> Any:
    """A minimal MockTransport handler for the HTTP-side checks.

    None of the proofs in this module depend on the HTTP layer touching the
    database, so the handler is a fixed, deterministic stand-in — the real
    database is exercised entirely through the injected ``session_factory``.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/ready":
            return httpx.Response(
                200, json={"status": "ready", "database": "connected", "pgvector": "0.8.0"}
            )
        if path == "/whoami":
            return httpx.Response(
                200,
                json={
                    "principal_id": str(uuid.uuid4()),
                    "principal_type": "admin",
                    "tenant_id": str(tenant_id),
                    "scopes": ["admin"],
                    "api_key_id": "k",
                    "memory_profile": None,
                },
            )
        if path == "/v1/review/stats":
            return httpx.Response(
                200,
                json={"by_review_status": {}, "by_kind": {}, "by_confidence": {}, "total": 0},
            )
        if path == "/v1/review/queue":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"detail": "no route"})

    return handler


async def _seed_tenant(session: AsyncSession, *, label: str) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
        {"id": tenant_id, "name": label, "slug": f"{label}-{tenant_id.hex[:8]}"},
    )
    await session.execute(
        text(
            "INSERT INTO principals (id, tenant_id, name, type) "
            "VALUES (:id, :tid, 'admin', 'admin')"
        ),
        {"id": principal_id, "tid": tenant_id},
    )
    await session.execute(
        text(
            "INSERT INTO tenant_config (tenant_id, config_version, active) "
            "VALUES (:tid, 'v1', TRUE)"
        ),
        {"tid": tenant_id},
    )
    await session.commit()
    return tenant_id, principal_id


async def _insert_job(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    status: str,
    created_at: datetime,
    run_after: datetime | None = None,
    locked_at: datetime | None = None,
    job_type: str = "embedding.generate",
    completed_at: datetime | None = None,
) -> None:
    await session.execute(
        text(
            "INSERT INTO jobs (id, tenant_id, job_type, status, priority, run_after, "
            "attempts, max_attempts, locked_at, locked_by, payload, created_at, "
            "updated_at, completed_at) "
            "VALUES (:id, :tid, :jt, :status, 100, :run_after, 1, 5, :locked_at, "
            ":locked_by, '{}'::jsonb, :created_at, :created_at, :completed_at)"
        ),
        {
            "id": uuid.uuid4(),
            "tid": tenant_id,
            "jt": job_type,
            "status": status,
            "run_after": run_after or created_at,
            "locked_at": locked_at,
            "locked_by": "worker-1" if locked_at is not None else None,
            "created_at": created_at,
            "completed_at": completed_at,
        },
    )
    await session.commit()


async def _insert_recall_log(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    mode: str,
    created_at: datetime,
    recall_log_id: uuid.UUID | None = None,
    item_ids: list[uuid.UUID] | None = None,
    byte_budget: int | None = None,
    token_budget: int | None = None,
) -> uuid.UUID:
    recall_log_id = recall_log_id or uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO recall_logs (id, tenant_id, principal_id, mode, item_ids, "
            "byte_budget, token_budget, scoring_version, config_version, "
            "memory_context_version, created_at) "
            "VALUES (:id, :tid, :pid, :mode, :item_ids, :bb, :tb, 'v1', 'v1', "
            "'memory-context-v2', :created_at)"
        ),
        {
            "id": recall_log_id,
            "tid": tenant_id,
            "pid": principal_id,
            "mode": mode,
            "item_ids": item_ids,
            "bb": byte_budget,
            "tb": token_budget,
            "created_at": created_at,
        },
    )
    await session.commit()
    return recall_log_id


async def _insert_usage_event(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID | None,
    event_type: str,
    operation: str,
    status: str,
    created_at: datetime,
    correlation_id: uuid.UUID | None = None,
    input_count: int = 0,
    input_bytes: int = 0,
    metadata: dict[str, Any] | None = None,
) -> None:
    import json

    await session.execute(
        text(
            "INSERT INTO usage_events (id, tenant_id, principal_id, event_type, "
            "operation, status, correlation_id, input_count, input_bytes, metadata, "
            "created_at) "
            "VALUES (:id, :tid, :pid, :et, :op, :status, :corr, :ic, :ib, "
            "CAST(:meta AS jsonb), :created_at)"
        ),
        {
            "id": uuid.uuid4(),
            "tid": tenant_id,
            "pid": principal_id,
            "et": event_type,
            "op": operation,
            "status": status,
            "corr": correlation_id,
            "ic": input_count,
            "ib": input_bytes,
            "meta": json.dumps(metadata or {}),
            "created_at": created_at,
        },
    )
    await session.commit()


def _check(report: Any, check_id: str) -> Any:
    return {c.id: c for c in report.checks}[check_id]


NOW = datetime.now(UTC)
SINCE = NOW - timedelta(hours=24)


async def _run(tenant_id: uuid.UUID, *, usage_telemetry_enabled: bool = True) -> Any:
    original = settings.usage_telemetry_enabled
    settings.usage_telemetry_enabled = usage_telemetry_enabled
    try:
        # Compute the window fresh, AFTER the caller has already committed its
        # seed rows: some of those rows (e.g. a stored Context Receipt) get
        # their created_at from the DATABASE's own now() rather than a
        # client-supplied timestamp, so a window bound frozen at module-import
        # time could exclude a row committed moments later. A fresh `until`
        # (with a short forward buffer) safely covers everything already
        # committed by the time this call is made. Kept small (not minutes)
        # so it does not itself inflate a due-pending job's apparent age past
        # the FIX-4 due-pending diagnostic threshold in precision tests.
        until = datetime.now(UTC) + timedelta(seconds=30)
        since = until - timedelta(hours=25)
        return await run_doctor(
            base_url="http://test",
            tenant=str(tenant_id),
            since=since,
            until=until,
            http_transport=httpx.MockTransport(_healthy_handler(tenant_id)),
            session_factory=_session_factory(),
            clock=lambda: until,
        )
    finally:
        settings.usage_telemetry_enabled = original


# --- worker.queue --------------------------------------------------------------


async def test_worker_queue_healthy_with_recent_succeeded_job() -> None:
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    async with _test_session_factory() as session:
        tenant_id, _ = await _seed_tenant(session, label="wq-healthy")
        await _insert_job(
            session, tenant_id=tenant_id, status="succeeded",
            created_at=NOW - timedelta(minutes=5), completed_at=NOW - timedelta(minutes=4),
        )
    report = await _run(tenant_id)
    check = _check(report, "worker.queue")
    assert check.status == "pass"
    assert check.reason_code == "WORKER_HEALTHY"
    assert check.evidence["dead_jobs"] == 0
    assert check.evidence["stale_running_jobs"] == 0


async def test_worker_queue_detects_dead_job() -> None:
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    async with _test_session_factory() as session:
        tenant_id, _ = await _seed_tenant(session, label="wq-dead")
        await _insert_job(
            session, tenant_id=tenant_id, status="dead", created_at=NOW - timedelta(hours=1)
        )
    report = await _run(tenant_id)
    check = _check(report, "worker.queue")
    assert check.status == "fail"
    assert check.reason_code == "DEAD_JOBS_PRESENT"
    assert check.evidence["dead_jobs"] == 1


async def test_worker_queue_detects_stale_running_job() -> None:
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    async with _test_session_factory() as session:
        tenant_id, _ = await _seed_tenant(session, label="wq-stale")
        await _insert_job(
            session,
            tenant_id=tenant_id,
            status="running",
            created_at=NOW - timedelta(hours=2),
            locked_at=NOW - timedelta(hours=1),  # far past the 300s default lease
        )
    report = await _run(tenant_id)
    check = _check(report, "worker.queue")
    assert check.status == "fail"
    assert check.reason_code == "STALE_RUNNING_JOBS"
    assert check.evidence["stale_running_jobs"] == 1


async def test_worker_queue_warns_on_aged_pending_backlog() -> None:
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    async with _test_session_factory() as session:
        tenant_id, _ = await _seed_tenant(session, label="wq-pending")
        await _insert_job(
            session,
            tenant_id=tenant_id,
            status="pending",
            created_at=NOW - timedelta(hours=1),
            run_after=NOW - timedelta(hours=1),
        )
    report = await _run(tenant_id)
    check = _check(report, "worker.queue")
    assert check.status == "warn"
    assert check.reason_code == "PENDING_BACKLOG_AGED"
    assert check.evidence["due_pending_jobs"] == 1


async def test_worker_queue_future_scheduled_old_job_remains_healthy() -> None:
    """An old-created job scheduled to run in the future is not yet due (FIX-4).

    Only ``run_after``-due backlog may trigger a warning — an intentionally
    scheduled or backed-off retry must not be diagnosed as an aged backlog
    merely because ``created_at`` is old.
    """
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    async with _test_session_factory() as session:
        tenant_id, _ = await _seed_tenant(session, label="wq-future")
        await _insert_job(
            session,
            tenant_id=tenant_id,
            status="pending",
            created_at=NOW - timedelta(hours=2),
            run_after=NOW + timedelta(hours=1),
        )
    report = await _run(tenant_id)
    check = _check(report, "worker.queue")
    assert check.status == "pass"
    assert check.reason_code == "WORKER_HEALTHY"
    assert check.evidence["due_pending_jobs"] == 0


async def test_worker_queue_due_job_only_slightly_overdue_remains_healthy() -> None:
    """A due job overdue by only a few seconds stays under the diagnostic threshold."""
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    async with _test_session_factory() as session:
        tenant_id, _ = await _seed_tenant(session, label="wq-just-due")
        await _insert_job(
            session,
            tenant_id=tenant_id,
            status="pending",
            created_at=NOW - timedelta(seconds=10),
            run_after=NOW - timedelta(seconds=10),
        )
    report = await _run(tenant_id)
    check = _check(report, "worker.queue")
    assert check.status == "pass"
    assert check.reason_code == "WORKER_HEALTHY"
    assert check.evidence["due_pending_jobs"] == 1


# --- capture.lifecycle / capture.remember --------------------------------------


async def test_capture_lifecycle_recent_error_free_evidence() -> None:
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    async with _test_session_factory() as session:
        tenant_id, principal_id = await _seed_tenant(session, label="lifecycle-ok")
        await _insert_usage_event(
            session,
            tenant_id=tenant_id,
            principal_id=principal_id,
            event_type="client.lifecycle_summary",
            operation="sync_turn",
            status="succeeded",
            created_at=NOW - timedelta(minutes=10),
            input_count=3,
            metadata={
                "authoritative": False, "guard_rejected": 0, "classified": 2,
                "promoted": 0, "parked": 0, "errors": 0,
            },
        )
    report = await _run(tenant_id)
    check = _check(report, "capture.lifecycle")
    assert check.status == "pass"
    assert check.reason_code == "LIFECYCLE_EVIDENCE_RECENT"
    assert check.evidence["lifecycle_errors_reported"] == 0


async def test_capture_lifecycle_disabled_telemetry_is_unknown() -> None:
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    async with _test_session_factory() as session:
        tenant_id, _ = await _seed_tenant(session, label="lifecycle-disabled")
    report = await _run(tenant_id, usage_telemetry_enabled=False)
    check = _check(report, "capture.lifecycle")
    assert check.status == "unknown"
    assert check.reason_code == "USAGE_TELEMETRY_DISABLED"


async def test_capture_remember_lifecycle_observed_without_server_candidate() -> None:
    """Lifecycle extraction observed, but no candidate.observed reached the server."""
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    async with _test_session_factory() as session:
        tenant_id, principal_id = await _seed_tenant(session, label="not-reaching")
        await _insert_usage_event(
            session,
            tenant_id=tenant_id,
            principal_id=principal_id,
            event_type="client.lifecycle_summary",
            operation="sync_turn",
            status="succeeded",
            created_at=NOW - timedelta(minutes=10),
            input_count=5,
            metadata={
                "authoritative": False, "guard_rejected": 1, "classified": 4,
                "promoted": 0, "parked": 0, "errors": 0,
            },
        )
    report = await _run(tenant_id)
    check = _check(report, "capture.remember")
    assert check.status == "warn"
    assert check.reason_code == "CAPTURE_NOT_REACHING_SERVER"


async def test_capture_remember_all_attempts_failed() -> None:
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    async with _test_session_factory() as session:
        tenant_id, principal_id = await _seed_tenant(session, label="all-failed")
        correlation_id = uuid.uuid4()
        await _insert_usage_event(
            session, tenant_id=tenant_id, principal_id=principal_id,
            event_type="candidate.observed", operation="process_memory_candidate",
            status="accepted_for_processing", created_at=NOW - timedelta(minutes=20),
            correlation_id=correlation_id, input_bytes=100,
        )
        await _insert_usage_event(
            session, tenant_id=tenant_id, principal_id=principal_id,
            event_type="candidate.outcome", operation="process_memory_candidate",
            status="failed", created_at=NOW - timedelta(minutes=19),
            correlation_id=correlation_id,
        )
    report = await _run(tenant_id)
    check = _check(report, "capture.remember")
    assert check.status == "fail"
    assert check.reason_code == "REMEMBER_PIPELINE_FAILED"
    assert check.evidence["candidate_observations"] == 1
    assert check.evidence["failed"] == 1
    assert check.evidence["created"] == 0


async def test_capture_remember_healthy_when_candidates_succeed() -> None:
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    async with _test_session_factory() as session:
        tenant_id, principal_id = await _seed_tenant(session, label="remember-ok")
        correlation_id = uuid.uuid4()
        await _insert_usage_event(
            session, tenant_id=tenant_id, principal_id=principal_id,
            event_type="candidate.observed", operation="process_memory_candidate",
            status="accepted_for_processing", created_at=NOW - timedelta(minutes=20),
            correlation_id=correlation_id, input_bytes=100,
        )
        await _insert_usage_event(
            session, tenant_id=tenant_id, principal_id=principal_id,
            event_type="candidate.outcome", operation="process_memory_candidate",
            status="created", created_at=NOW - timedelta(minutes=19),
            correlation_id=correlation_id,
        )
    report = await _run(tenant_id)
    check = _check(report, "capture.remember")
    assert check.status == "pass"
    assert check.reason_code == "REMEMBER_PIPELINE_HEALTHY"


async def test_capture_remember_unresolved_candidate_does_not_pass() -> None:
    """A candidate.observed with no matching outcome yet must never read as healthy.

    ENG-LOOP-001A-FIX1 / FIX-3: an unresolved logical candidate must never be
    silently folded into a successful or failed terminal outcome.
    """
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    async with _test_session_factory() as session:
        tenant_id, principal_id = await _seed_tenant(session, label="unresolved-only")
        await _insert_usage_event(
            session, tenant_id=tenant_id, principal_id=principal_id,
            event_type="candidate.observed", operation="process_memory_candidate",
            status="accepted_for_processing", created_at=NOW - timedelta(minutes=20),
            correlation_id=uuid.uuid4(), input_bytes=100,
        )
        # Deliberately no candidate.outcome event for this correlation_id.
    report = await _run(tenant_id)
    check = _check(report, "capture.remember")
    assert check.status == "warn"
    assert check.reason_code == "REMEMBER_OUTCOMES_PENDING"
    assert check.evidence["unresolved_candidates"] == 1
    assert check.evidence["created"] == 0
    assert check.evidence["failed"] == 0


async def test_capture_remember_success_plus_unresolved_does_not_pass() -> None:
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    async with _test_session_factory() as session:
        tenant_id, principal_id = await _seed_tenant(session, label="mixed-unresolved")
        resolved_correlation_id = uuid.uuid4()
        await _insert_usage_event(
            session, tenant_id=tenant_id, principal_id=principal_id,
            event_type="candidate.observed", operation="process_memory_candidate",
            status="accepted_for_processing", created_at=NOW - timedelta(minutes=20),
            correlation_id=resolved_correlation_id, input_bytes=100,
        )
        await _insert_usage_event(
            session, tenant_id=tenant_id, principal_id=principal_id,
            event_type="candidate.outcome", operation="process_memory_candidate",
            status="created", created_at=NOW - timedelta(minutes=19),
            correlation_id=resolved_correlation_id,
        )
        await _insert_usage_event(
            session, tenant_id=tenant_id, principal_id=principal_id,
            event_type="candidate.observed", operation="process_memory_candidate",
            status="accepted_for_processing", created_at=NOW - timedelta(minutes=15),
            correlation_id=uuid.uuid4(), input_bytes=100,
        )
    report = await _run(tenant_id)
    check = _check(report, "capture.remember")
    assert check.status == "warn"
    assert check.reason_code == "REMEMBER_OUTCOMES_PENDING"
    assert check.evidence["created"] == 1
    assert check.evidence["unresolved_candidates"] == 1


# --- recall.activity -------------------------------------------------------------


async def test_recall_activity_recent_startup_and_semantic() -> None:
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    async with _test_session_factory() as session:
        tenant_id, principal_id = await _seed_tenant(session, label="recall-ok")
        await _insert_recall_log(
            session, tenant_id=tenant_id, principal_id=principal_id, mode="startup",
            created_at=NOW - timedelta(minutes=30),
        )
        await _insert_recall_log(
            session, tenant_id=tenant_id, principal_id=principal_id, mode="semantic",
            created_at=NOW - timedelta(minutes=20),
        )
    report = await _run(tenant_id)
    check = _check(report, "recall.activity")
    assert check.status == "pass"
    assert check.reason_code == "RECALL_ACTIVITY_RECENT"
    assert check.evidence["startup_count"] == 1
    assert check.evidence["semantic_count"] == 1


async def test_recall_activity_none_warns() -> None:
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    async with _test_session_factory() as session:
        tenant_id, _ = await _seed_tenant(session, label="recall-none")
    report = await _run(tenant_id)
    check = _check(report, "recall.activity")
    assert check.status == "warn"
    assert check.reason_code == "NO_RECALL_ACTIVITY"


# --- receipts.activity -----------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_dark_write_setting():
    original_enabled = settings.context_receipt_dark_write_enabled
    yield
    settings.context_receipt_dark_write_enabled = original_enabled


async def test_receipts_activity_present_and_valid() -> None:
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    settings.context_receipt_dark_write_enabled = True
    async with _test_session_factory() as session:
        tenant_id, principal_id = await _seed_tenant(session, label="receipt-ok")
        item_ids = [uuid.uuid4()]
        recall_log_id = await _insert_recall_log(
            session, tenant_id=tenant_id, principal_id=principal_id, mode="startup",
            created_at=NOW - timedelta(minutes=10), item_ids=item_ids, byte_budget=8192,
        )
        manifest = build_manifest(
            tenant_id=str(tenant_id), principal_id=str(principal_id),
            item_ids=[str(i) for i in item_ids], byte_budget=8192,
        )
        await store_context_receipt(
            session, tenant_id=tenant_id, principal_id=principal_id,
            recall_log_id=recall_log_id, manifest=manifest,
        )
        await session.commit()
    report = await _run(tenant_id)
    check = _check(report, "receipts.activity")
    assert check.status == "pass"
    assert check.reason_code == "RECEIPTS_HEALTHY"
    assert check.evidence["latest_verification_status"] == "valid"
    assert check.evidence["unmatched_startup_recall_count"] == 0


async def test_receipts_activity_gap_detected_when_fail_open() -> None:
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    settings.context_receipt_dark_write_enabled = True
    async with _test_session_factory() as session:
        tenant_id, principal_id = await _seed_tenant(session, label="receipt-gap")
        # A startup recall with no matching receipt (writes are fail-open).
        await _insert_recall_log(
            session, tenant_id=tenant_id, principal_id=principal_id, mode="startup",
            created_at=NOW - timedelta(minutes=10),
        )
    report = await _run(tenant_id)
    check = _check(report, "receipts.activity")
    assert check.status == "warn"
    assert check.reason_code == "RECEIPT_GAP_DETECTED"


async def test_receipts_activity_no_startup_recall_is_unknown() -> None:
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    settings.context_receipt_dark_write_enabled = True
    async with _test_session_factory() as session:
        tenant_id, _ = await _seed_tenant(session, label="receipt-none")
    report = await _run(tenant_id)
    check = _check(report, "receipts.activity")
    assert check.status == "unknown"
    assert check.reason_code == "NO_STARTUP_RECALL_TO_ASSESS"


async def test_receipts_activity_disabled_locally_warns() -> None:
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    settings.context_receipt_dark_write_enabled = False
    async with _test_session_factory() as session:
        tenant_id, _ = await _seed_tenant(session, label="receipt-disabled")
    report = await _run(tenant_id)
    check = _check(report, "receipts.activity")
    assert check.status == "warn"
    assert check.reason_code == "RECEIPTS_DISABLED"


async def test_receipts_activity_latest_receipt_invalid() -> None:
    """A stored receipt whose manifest_hash does not match its manifest content."""
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    settings.context_receipt_dark_write_enabled = True
    async with _test_session_factory() as session:
        tenant_id, principal_id = await _seed_tenant(session, label="receipt-invalid")
        item_ids = [uuid.uuid4()]
        recall_log_id = await _insert_recall_log(
            session, tenant_id=tenant_id, principal_id=principal_id, mode="startup",
            created_at=NOW - timedelta(minutes=10), item_ids=item_ids, byte_budget=8192,
        )
        manifest = build_manifest(
            tenant_id=str(tenant_id), principal_id=str(principal_id),
            item_ids=[str(i) for i in item_ids], byte_budget=8192,
        )
        payload = manifest.model_dump(mode="json", by_alias=True, exclude_none=False)
        import json as _json

        # Insert directly with a syntactically-valid but WRONG manifest_hash —
        # the DB CHECK only validates the sha256:<hex> *format*, not that it is
        # the correct RFC 8785 hash of the stored manifest, so this row passes
        # every database constraint but must fail the repository's
        # deterministic re-verification.
        wrong_hash = "sha256:" + "a" * 64
        await session.execute(
            text(
                "INSERT INTO context_receipts (id, tenant_id, principal_id, "
                "recall_log_id, manifest_schema, manifest_schema_version, "
                "canonicalization, mode, manifest, manifest_hash, packet_hash) "
                "VALUES (:id, :tid, :pid, :rlid, 'engram.context-manifest', '1.0', "
                "'rfc8785', 'startup', CAST(:manifest AS jsonb), :mh, :ph)"
            ),
            {
                "id": uuid.uuid4(),
                "tid": tenant_id,
                "pid": principal_id,
                "rlid": recall_log_id,
                "manifest": _json.dumps(payload),
                "mh": wrong_hash,
                "ph": manifest.packet.hash,
            },
        )
        await session.commit()
    report = await _run(tenant_id)
    check = _check(report, "receipts.activity")
    assert check.status == "fail"
    assert check.reason_code == "LATEST_RECEIPT_INVALID"
    assert check.evidence["latest_verification_status"] == "invalid"


async def _insert_raw_receipt(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    recall_log_id: uuid.UUID,
    item_ids: list[uuid.UUID],
    created_at: datetime,
    stored_manifest_hash: str | None = None,
    receipt_id: uuid.UUID | None = None,
) -> Any:
    """Insert a context_receipts row with an explicit created_at (raw SQL).

    ``store_context_receipt`` always uses the server's ``now()`` default, so
    window-scoping/tie-break tests that need explicit control over
    ``created_at`` insert directly, mirroring the syntactically-valid-but-
    wrong-hash pattern already used for the "invalid receipt" test above.
    ``stored_manifest_hash=None`` computes the correct RFC 8785 hash (a
    genuinely valid receipt); any other value is inserted verbatim (an
    invalid receipt whose format still satisfies the DB CHECK constraint).
    """
    import json as _json

    manifest = build_manifest(
        tenant_id=str(tenant_id), principal_id=str(principal_id),
        item_ids=[str(i) for i in item_ids], byte_budget=8192,
    )
    payload = manifest.model_dump(mode="json", by_alias=True, exclude_none=False)
    correct_hash = manifest_hash(manifest)
    row_id = receipt_id or uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO context_receipts (id, tenant_id, principal_id, "
            "recall_log_id, manifest_schema, manifest_schema_version, "
            "canonicalization, mode, manifest, manifest_hash, packet_hash, created_at) "
            "VALUES (:id, :tid, :pid, :rlid, 'engram.context-manifest', '1.0', "
            "'rfc8785', 'startup', CAST(:manifest AS jsonb), :mh, :ph, :created_at)"
        ),
        {
            "id": row_id,
            "tid": tenant_id,
            "pid": principal_id,
            "rlid": recall_log_id,
            "manifest": _json.dumps(payload),
            "mh": stored_manifest_hash if stored_manifest_hash is not None else correct_hash,
            "ph": manifest.packet.hash,
            "created_at": created_at,
        },
    )
    await session.commit()
    return row_id


async def test_receipts_activity_invalid_receipt_outside_window_does_not_fail() -> None:
    """A receipt outside [since, until) must never affect the report."""
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    settings.context_receipt_dark_write_enabled = False
    async with _test_session_factory() as session:
        tenant_id, principal_id = await _seed_tenant(session, label="receipt-old-invalid")
        item_ids = [uuid.uuid4()]
        recall_log_id = await _insert_recall_log(
            session, tenant_id=tenant_id, principal_id=principal_id, mode="startup",
            created_at=NOW - timedelta(hours=26), item_ids=item_ids, byte_budget=8192,
        )
        # Invalid AND far outside the default ~25h report window.
        await _insert_raw_receipt(
            session, tenant_id=tenant_id, principal_id=principal_id,
            recall_log_id=recall_log_id, item_ids=item_ids,
            created_at=NOW - timedelta(hours=26), stored_manifest_hash="sha256:" + "a" * 64,
        )
    report = await _run(tenant_id)
    check = _check(report, "receipts.activity")
    assert check.reason_code != "LATEST_RECEIPT_INVALID"
    assert check.status != "fail"


async def test_receipts_activity_selects_in_window_over_newer_out_of_window() -> None:
    """The newest receipt WITHIN the window is selected, not a newer one outside it."""
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    settings.context_receipt_dark_write_enabled = True
    async with _test_session_factory() as session:
        tenant_id, principal_id = await _seed_tenant(session, label="receipt-window-pick")
        in_window_items = [uuid.uuid4()]
        in_window_recall_log = await _insert_recall_log(
            session, tenant_id=tenant_id, principal_id=principal_id, mode="startup",
            created_at=NOW - timedelta(minutes=5), item_ids=in_window_items, byte_budget=8192,
        )
        # Valid, in-window receipt.
        await _insert_raw_receipt(
            session, tenant_id=tenant_id, principal_id=principal_id,
            recall_log_id=in_window_recall_log, item_ids=in_window_items,
            created_at=NOW - timedelta(minutes=5),
        )
        # A newer but OUT-OF-WINDOW, INVALID receipt on a separate recall log —
        # if window scoping were broken, this one (being "latest" overall)
        # would be selected instead and the report would incorrectly fail.
        future_items = [uuid.uuid4()]
        future_recall_log = await _insert_recall_log(
            session, tenant_id=tenant_id, principal_id=principal_id, mode="startup",
            created_at=NOW + timedelta(minutes=10), item_ids=future_items, byte_budget=8192,
        )
        await _insert_raw_receipt(
            session, tenant_id=tenant_id, principal_id=principal_id,
            recall_log_id=future_recall_log, item_ids=future_items,
            created_at=NOW + timedelta(minutes=10), stored_manifest_hash="sha256:" + "b" * 64,
        )
    report = await _run(tenant_id)
    check = _check(report, "receipts.activity")
    assert check.status == "pass"
    assert check.reason_code == "RECEIPTS_HEALTHY"
    assert check.evidence["latest_verification_status"] == "valid"


async def test_receipts_activity_invalid_receipt_fails_even_when_dark_writes_disabled() -> None:
    """Local write configuration must never hide existing receipt corruption."""
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    settings.context_receipt_dark_write_enabled = False
    async with _test_session_factory() as session:
        tenant_id, principal_id = await _seed_tenant(session, label="receipt-inv-disabled")
        item_ids = [uuid.uuid4()]
        recall_log_id = await _insert_recall_log(
            session, tenant_id=tenant_id, principal_id=principal_id, mode="startup",
            created_at=NOW - timedelta(minutes=5), item_ids=item_ids, byte_budget=8192,
        )
        await _insert_raw_receipt(
            session, tenant_id=tenant_id, principal_id=principal_id,
            recall_log_id=recall_log_id, item_ids=item_ids,
            created_at=NOW - timedelta(minutes=5), stored_manifest_hash="sha256:" + "c" * 64,
        )
    report = await _run(tenant_id)
    check = _check(report, "receipts.activity")
    assert check.status == "fail"
    assert check.reason_code == "LATEST_RECEIPT_INVALID"
    assert check.evidence["dark_write_enabled"] is False


async def test_receipts_activity_deterministic_tie_break_on_equal_timestamp() -> None:
    """Two receipts sharing one created_at resolve identically via descending id."""
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    settings.context_receipt_dark_write_enabled = True
    async with _test_session_factory() as session:
        tenant_id, principal_id = await _seed_tenant(session, label="receipt-tiebreak")
        shared_created_at = NOW - timedelta(minutes=5)
        low_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        high_id = uuid.UUID("ffffffff-ffff-ffff-ffff-fffffffffffe")

        items_a = [uuid.uuid4()]
        recall_log_a = await _insert_recall_log(
            session, tenant_id=tenant_id, principal_id=principal_id, mode="startup",
            created_at=shared_created_at, item_ids=items_a, byte_budget=8192,
        )
        # Lower UUID: valid.
        await _insert_raw_receipt(
            session, tenant_id=tenant_id, principal_id=principal_id,
            recall_log_id=recall_log_a, item_ids=items_a,
            created_at=shared_created_at, receipt_id=low_id,
        )

        items_b = [uuid.uuid4()]
        recall_log_b = await _insert_recall_log(
            session, tenant_id=tenant_id, principal_id=principal_id, mode="startup",
            created_at=shared_created_at, item_ids=items_b, byte_budget=8192,
        )
        # Higher UUID, same timestamp: invalid. Deterministic DESC id order
        # must select THIS one, so the report must fail.
        await _insert_raw_receipt(
            session, tenant_id=tenant_id, principal_id=principal_id,
            recall_log_id=recall_log_b, item_ids=items_b,
            created_at=shared_created_at, stored_manifest_hash="sha256:" + "d" * 64,
            receipt_id=high_id,
        )
    report = await _run(tenant_id)
    check = _check(report, "receipts.activity")
    assert check.status == "fail"
    assert check.reason_code == "LATEST_RECEIPT_INVALID"


# --- tenant isolation --------------------------------------------------------------


async def test_tenant_filter_excludes_other_tenants_data() -> None:
    """Jobs, recall logs, usage events, and receipts from another tenant are invisible."""
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    settings.context_receipt_dark_write_enabled = True
    async with _test_session_factory() as session:
        tenant_a, principal_a = await _seed_tenant(session, label="iso-a")
        tenant_b, principal_b = await _seed_tenant(session, label="iso-b")

        # Tenant B: dead job, startup+semantic recall, candidate failure, receipt.
        await _insert_job(
            session, tenant_id=tenant_b, status="dead", created_at=NOW - timedelta(minutes=5)
        )
        await _insert_recall_log(
            session, tenant_id=tenant_b, principal_id=principal_b, mode="startup",
            created_at=NOW - timedelta(minutes=5),
        )
        await _insert_recall_log(
            session, tenant_id=tenant_b, principal_id=principal_b, mode="semantic",
            created_at=NOW - timedelta(minutes=5),
        )
        correlation_id = uuid.uuid4()
        await _insert_usage_event(
            session, tenant_id=tenant_b, principal_id=principal_b,
            event_type="candidate.observed", operation="process_memory_candidate",
            status="accepted_for_processing", created_at=NOW - timedelta(minutes=5),
            correlation_id=correlation_id,
        )
        await _insert_usage_event(
            session, tenant_id=tenant_b, principal_id=principal_b,
            event_type="candidate.outcome", operation="process_memory_candidate",
            status="failed", created_at=NOW - timedelta(minutes=4),
            correlation_id=correlation_id,
        )
        b_item_ids = [uuid.uuid4()]
        b_recall_log_id = await _insert_recall_log(
            session, tenant_id=tenant_b, principal_id=principal_b, mode="startup",
            created_at=NOW - timedelta(minutes=3), item_ids=b_item_ids, byte_budget=8192,
        )
        b_manifest = build_manifest(
            tenant_id=str(tenant_b), principal_id=str(principal_b),
            item_ids=[str(i) for i in b_item_ids], byte_budget=8192,
        )
        await store_context_receipt(
            session, tenant_id=tenant_b, principal_id=principal_b,
            recall_log_id=b_recall_log_id, manifest=b_manifest,
        )
        await session.commit()

        # Tenant A: nothing at all.

    report = await _run(tenant_a)
    worker = _check(report, "worker.queue")
    recall = _check(report, "recall.activity")
    remember = _check(report, "capture.remember")
    receipts = _check(report, "receipts.activity")

    assert worker.evidence["dead_jobs"] == 0
    assert recall.status == "warn"
    assert recall.reason_code == "NO_RECALL_ACTIVITY"
    assert recall.evidence["startup_count"] == 0
    assert recall.evidence["semantic_count"] == 0
    assert remember.evidence.get("candidate_observations", 0) == 0
    assert receipts.status == "unknown"
    assert receipts.reason_code == "NO_STARTUP_RECALL_TO_ASSESS"


# --- no-mutation proof -------------------------------------------------------------

_SNAPSHOT_TABLES: tuple[str, ...] = (
    "tenants", "principals", "workspaces", "tenant_config", "memory_items",
    "memory_embeddings", "jobs", "recall_logs", "usage_events", "context_receipts",
    "item_events", "classification_runs", "candidate_ingests",
)


async def _snapshot(session: AsyncSession) -> dict[str, tuple[int, str | None]]:
    snapshot: dict[str, tuple[int, str | None]] = {}
    for table in _SNAPSHOT_TABLES:
        row = (
            await session.execute(
                text(
                    f"SELECT count(*) AS n, "
                    f"md5(COALESCE(string_agg(t::text, '|' ORDER BY id), '')) AS checksum "
                    f"FROM {table} t"
                )
            )
        ).mappings().one()
        snapshot[table] = (int(row["n"]), row["checksum"])
    return snapshot


async def test_run_doctor_writes_nothing() -> None:
    """Byte/count-equivalent state before and after a full doctor run."""
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    async with _test_session_factory() as session:
        tenant_id, principal_id = await _seed_tenant(session, label="no-mutation")
        await _insert_job(
            session, tenant_id=tenant_id, status="succeeded",
            created_at=NOW - timedelta(minutes=5), completed_at=NOW - timedelta(minutes=4),
        )
        await _insert_recall_log(
            session, tenant_id=tenant_id, principal_id=principal_id, mode="startup",
            created_at=NOW - timedelta(minutes=5),
        )

    async with _test_session_factory() as session:
        before = await _snapshot(session)

    report = await _run(tenant_id)
    assert report.overall_status in ("healthy", "degraded", "unhealthy")

    async with _test_session_factory() as session:
        after = await _snapshot(session)

    assert before == after, "engram doctor must never mutate any business table"


# --- no external provider call proof ------------------------------------------------


async def test_run_doctor_never_calls_embedding_or_classification_provider() -> None:
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    async with _test_session_factory() as session:
        tenant_id, _ = await _seed_tenant(session, label="no-provider-call")

    original_embedding_provider = settings.embedding_provider
    original_openai_api_key = settings.openai_api_key
    original_classification_provider = settings.classification_provider
    original_classification_api_key = settings.classification_api_key
    settings.embedding_provider = "openai"
    settings.openai_api_key = "sk-doctor-must-not-use-this"
    settings.classification_provider = "openai"
    settings.classification_api_key = "sk-doctor-must-not-use-this-either"

    import engram.classification as classification_module
    import engram.embeddings as embeddings_module

    forbidden_embedding = Mock(
        side_effect=AssertionError("generate_embedding must not be called")
    )
    forbidden_embeddings = Mock(
        side_effect=AssertionError("generate_embeddings must not be called")
    )
    forbidden_classify = Mock(side_effect=AssertionError("classify must not be called"))
    forbidden_openai_call = Mock(
        side_effect=AssertionError("_call_openai_classification must not be called")
    )
    original_generate_embedding = embeddings_module.generate_embedding
    original_generate_embeddings = embeddings_module.generate_embeddings
    original_classify = classification_module.classify
    original_call_openai = classification_module._call_openai_classification
    embeddings_module.generate_embedding = forbidden_embedding  # type: ignore[assignment]
    embeddings_module.generate_embeddings = forbidden_embeddings  # type: ignore[assignment]
    classification_module.classify = forbidden_classify  # type: ignore[assignment]
    classification_module._call_openai_classification = forbidden_openai_call  # type: ignore[assignment]
    try:
        report = await _run(tenant_id)
    finally:
        settings.embedding_provider = original_embedding_provider
        settings.openai_api_key = original_openai_api_key
        settings.classification_provider = original_classification_provider
        settings.classification_api_key = original_classification_api_key
        embeddings_module.generate_embedding = original_generate_embedding
        embeddings_module.generate_embeddings = original_generate_embeddings
        classification_module.classify = original_classify
        classification_module._call_openai_classification = original_call_openai

    assert forbidden_embedding.call_count == 0
    assert forbidden_embeddings.call_count == 0
    assert forbidden_classify.call_count == 0
    assert forbidden_openai_call.call_count == 0
    embeddings_check = _check(report, "config.embeddings")
    classification_check = _check(report, "config.classification")
    assert embeddings_check.reason_code != "EMBEDDING_CONFIG_INVALID"
    assert classification_check.reason_code != "CLASSIFICATION_CONFIG_INVALID"


# --- FIX-7: --database-url scheme acceptance + one-shot engine disposal --------


async def test_database_url_bare_postgresql_scheme_builds_a_working_one_shot_engine() -> None:
    """A bare postgresql:// --database-url must work end-to-end, not just postgresql+asyncpg://.

    Exercises the real ``_build_session_factory`` path (no injected
    ``session_factory``), proving the scheme is normalized to an
    asyncpg-capable engine that can actually reach the live database, and
    that the one-shot engine this run constructs is disposed cleanly
    (ENG-LOOP-001A-FIX1 / FIX-7).
    """
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    async with _test_session_factory() as session:
        tenant_id, principal_id = await _seed_tenant(session, label="fix7-bare-scheme")
        await _insert_recall_log(
            session, tenant_id=tenant_id, principal_id=principal_id, mode="startup",
            created_at=NOW - timedelta(minutes=5),
        )

    assert settings.database_url.startswith("postgresql+asyncpg://")
    bare_url = "postgresql://" + settings.database_url[len("postgresql+asyncpg://") :]

    report = await run_doctor(
        base_url="http://test",
        tenant=str(tenant_id),
        since=SINCE,
        until=NOW,
        database_url=bare_url,
        http_transport=httpx.MockTransport(_healthy_handler(tenant_id)),
        clock=lambda: NOW,
    )
    # A real query actually ran against the live database — a DB-dependent
    # check reflects the seeded recall log rather than degrading to
    # "unknown" (which would mean the one-shot engine never connected).
    recall_check = _check(report, "recall.activity")
    assert recall_check.status == "pass"
    assert recall_check.reason_code == "RECALL_ACTIVITY_RECENT"
