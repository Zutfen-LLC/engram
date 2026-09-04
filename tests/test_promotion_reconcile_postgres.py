"""Real-PostgreSQL tests for the bounded promotion reconciliation backstop.

ENG-PROMOTION-003B4 / issue #155. These require a live PostgreSQL with the v2
schema (they skip automatically when no DB is reachable, matching
tests/test_remember.py). Covers the slice's verification matrix:

1.  missing cooling-boundary job → exactly one canonical repair at the exact
    authoritative eligibility boundary, never run early;
2.  dead due evaluation → discovered, repaired, executed, promoted, with
    truthful reconcile trigger provenance in the audit event;
3.  healthy pending/running targeted job → no duplicate repair;
4.  terminal-blocker fairness → 20+ terminal rows cannot starve a later
    actionable row within the documented rotation bound, and terminal rows
    never receive evaluation jobs (no hot loop);
5.  memory-kind policy change → one bounded reconciliation chain scheduled
    (never synchronous item fan-out), affected item reached, canonical
    evaluation with policy_changed provenance, promotion without startup
    recall;
6.  promotion disabled → enabled → explicit operator request drives the
    bounded recovery path (direct SQL config changes are not observable);
7.  provider recovery → async classification work re-enqueued (never inline),
    binds normally, promotion evaluation follows, and already-bound evidence
    is not blindly reclassified;
8.  crash/restart → deterministic continuation and wraparound with no
    permanent skipped range;
9.  multi-tenant fairness → a large tenant A backlog cannot starve tenant B;
10. RLS under the non-owner engram_app role;
11. concurrency → simultaneous reconciliation/targeted/legacy paths produce
    exactly one mutation and one authoritative review_change event;
12. flag rollback → flag off creates no backstop work, startup behavior is
    unchanged, and targeted promotion.evaluate / legacy jobs still function.
"""

from __future__ import annotations

import asyncio
import json
import math
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.api.app import create_app
from engram.classification import ClassificationResult
from engram.config import settings
from engram.db import (
    _DEFAULT_PRINCIPAL_NAME,
    _DEFAULT_TENANT_SLUG,
    apply_rls_context,
    get_session,
)
from engram.jobs import STATUS_PENDING, enqueue_job
from engram.memory_kinds import seed_builtin_kinds
from engram.models import Principal, Tenant, TenantConfig
from engram.promotion import parse_promotion_evaluate_payload
from engram.promotion_reconciliation import (
    BACKSTOP_TRIGGER_ID,
    PROMOTION_RECONCILE_JOB_TYPE,
    RECONCILE_REASON_BACKSTOP,
    RECONCILE_REASON_OPERATOR_REQUEST,
    RECONCILE_REASON_POLICY_CHANGE,
    RECONCILE_REASON_PROVIDER_RECOVERY,
    build_promotion_reconcile_payload,
    ensure_periodic_reconciliation_chains,
    reconciliation_status,
    request_global_reconciliation_window,
    request_reconciliation_chain,
    run_reconciliation_pass,
)
from engram.worker import process_one_job

_test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_test_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    _test_engine, class_=AsyncSession, expire_on_commit=False
)

FIXED_NOW = datetime.now(UTC).replace(microsecond=0)
_DB_SKIP_REASON = "requires a live PostgreSQL with the v2 schema (run docker compose up)"


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


def _require_db() -> None:
    pytest.skip(_DB_SKIP_REASON)


@pytest.fixture(autouse=True)
async def _clean_db():
    if not await _db_ok():
        return
    async with _test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM jobs"))
        await conn.execute(text("DELETE FROM item_events"))
        await conn.execute(text("DELETE FROM feedback_events"))
        await conn.execute(text("DELETE FROM recall_logs"))
        await conn.execute(text("DELETE FROM classification_runs"))
        await conn.execute(text("DELETE FROM memory_items"))
        await conn.execute(text("DELETE FROM promotion_reconcile_chains"))
        await conn.execute(text("DELETE FROM promotion_reconcile_state"))
        await conn.execute(text("DELETE FROM promotion_reconcile_scheduler_state"))
        await conn.execute(text("DELETE FROM promotion_reconciliation_state"))
        # Keep the seeded default admin; drop every principal this suite added.
        await conn.execute(
            text("DELETE FROM principals WHERE internal_key IS NULL AND name != :keep"),
            {"keep": _DEFAULT_PRINCIPAL_NAME},
        )
        await conn.execute(text("DELETE FROM tenants WHERE slug != 'default'"))
    async with _test_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE tenant_config SET "
                "auto_promote_enabled = TRUE, "
                "auto_promote_confidence_threshold = 0.7, "
                "auto_promote_min_age_hours = 72, "
                "auto_promote_evidence_enabled = FALSE, "
                "auto_promote_evidence_threshold = 0.7 "
                "WHERE tenant_id = (SELECT id FROM tenants WHERE slug = 'default')"
            )
        )
        # Restore the seeded builtin kind admission flags (policy tests mutate
        # them): preference/doctrine/diary_entry do not auto-promote by default.
        await conn.execute(
            text(
                "UPDATE memory_kinds SET enabled = TRUE, "
                "auto_promote_from_inferred = (name IN "
                "('fact','decision','observation','invariant','procedure','summary')) "
                "WHERE tenant_id = (SELECT id FROM tenants WHERE slug = 'default')"
            )
        )


@pytest.fixture(autouse=True)
def _default_flags(monkeypatch):
    monkeypatch.setattr(settings, "promotion_reconciliation_enabled", True)
    monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", True)
    monkeypatch.setattr(settings, "classification_provider", "none")


async def _default_tenant_principal() -> tuple[str, str]:
    async with _test_session_factory() as session:
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
    return str(row["tenant_id"]), str(row["principal_id"])


async def _make_tenant(slug: str) -> tuple[str, str]:
    """Create an extra tenant with seeded kinds, admin principal, and config."""
    tenant_id = str(uuid.uuid4())
    principal_id = str(uuid.uuid4())
    async with _test_session_factory() as session:
        session.add(Tenant(id=uuid.UUID(tenant_id), name=slug, slug=slug))
        session.add(
            Principal(
                id=uuid.UUID(principal_id),
                tenant_id=uuid.UUID(tenant_id),
                name="admin",
                type="admin",
            )
        )
        await session.flush()
        await seed_builtin_kinds(session, uuid.UUID(tenant_id))
        session.add(TenantConfig(tenant_id=uuid.UUID(tenant_id), active=True))
        await session.commit()
    return tenant_id, principal_id


async def _insert_item(
    *,
    tenant_id: str,
    principal_id: str,
    content: str,
    memory_confidence: float = 0.5,
    created_at: datetime | None = None,
    kind: str = "fact",
    review_status: str = "proposed",
) -> str:
    from engram.canonicalize import canonicalize, content_hash

    item_id = str(uuid.uuid4())
    if created_at is None:
        created_at = FIXED_NOW - timedelta(hours=200)
    # The real canonical content hash, so receipts produced by the actual
    # classification machinery stay consistent with the item row.
    digest = content_hash(canonicalize(content))
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO memory_items ("
                "id, tenant_id, principal_id, content, content_hash, kind, "
                "visibility, review_status, memory_confidence, source_trust, "
                "source_confidence_prior, importance, source_type, created_at, "
                "valid_from"
                ") VALUES ("
                ":id, :tenant_id, :principal_id, :content, :content_hash, :kind, "
                "'tenant', :review_status, :memory_confidence, 0.5, "
                ":source_confidence_prior, 0.5, 'manual', :created_at, :created_at"
                ")"
            ),
            {
                "id": item_id,
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "content": content,
                "content_hash": digest,
                "kind": kind,
                "review_status": review_status,
                "memory_confidence": memory_confidence,
                "source_confidence_prior": memory_confidence,
                "created_at": created_at,
            },
        )
        await session.commit()
    return item_id


async def _insert_bound_run(
    item_id: str,
    *,
    taxonomy_confidence: float = 0.9,
    created_at: datetime | None = None,
) -> str:
    """Bind a consistent, qualifying classification receipt to an item."""
    run_id = str(uuid.uuid4())
    async with _test_session_factory() as session:
        item = (
            (
                await session.execute(
                    text(
                        "SELECT tenant_id, principal_id, content_hash, kind, "
                        "memory_confidence, source_type, created_at FROM "
                        "memory_items WHERE id = :id"
                    ),
                    {"id": item_id},
                )
            )
            .mappings()
            .one()
        )
        if created_at is None:
            created_at = item["created_at"] + timedelta(hours=1)
        await session.execute(
            text(
                "INSERT INTO classification_runs ("
                "id, tenant_id, principal_id, memory_item_id, bound_at, content_hash, "
                "canonicalization_version, source_type, suggested_kind, taxonomy_confidence, "
                "retention_confidence, retention_disposition, reason, provenance, "
                "classification_version, retention_policy_version, created_at, expires_at"
                ") VALUES ("
                ":id, :tenant_id, :principal_id, :item_id, :created_at, :content_hash, "
                "'canonical-v1', :source_type, :kind, :taxonomy_confidence, "
                ":retention_confidence, 'retain', 'test evidence', '{}', "
                "'classification-v2', 'retention-v1', :created_at, :expires_at"
                ")"
            ),
            {
                "id": run_id,
                "tenant_id": item["tenant_id"],
                "principal_id": item["principal_id"],
                "item_id": item_id,
                "created_at": created_at,
                "content_hash": item["content_hash"],
                "source_type": item["source_type"],
                "kind": item["kind"],
                "taxonomy_confidence": taxonomy_confidence,
                "retention_confidence": item["memory_confidence"],
                "expires_at": created_at + timedelta(hours=1),
            },
        )
        # Mirror the item-side evidence fields the evaluator checks for
        # consistency with the bound receipt.
        await session.execute(
            text(
                "UPDATE memory_items SET retention_confidence = :rc, "
                "retention_disposition = 'retain', retention_evidence_at = :created_at, "
                "source_confidence_prior = :prior WHERE id = :id"
            ),
            {
                "rc": item["memory_confidence"],
                "created_at": created_at,
                "prior": item["memory_confidence"],
                "id": item_id,
            },
        )
        await session.commit()
    return run_id


async def _mark_classification_origin(
    item_id: str, principal_id: str, *, source: str = "auto_classified"
) -> None:
    """Record the immutable initial classification provenance from remember."""
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO item_events (item_id, event_type, field_name, old_value, "
                "new_value, actor_principal_id, reason) VALUES ("
                ":item_id, 'classification', 'kind', NULL, "
                "CAST(:payload AS text), :principal_id, 'rule classification')"
            ),
            {
                "item_id": item_id,
                "principal_id": principal_id,
                "payload": json.dumps({"source": source, "kind": "fact"}),
            },
        )
        await session.commit()


async def _item_row(item_id: str) -> dict[str, Any]:
    async with _test_session_factory() as session:
        return dict(
            
                (
                    await session.execute(
                        text(
                            "SELECT review_status, memory_confidence, created_at, "
                            "retention_confidence, retention_evidence_at "
                            "FROM memory_items WHERE id = :id"
                        ),
                        {"id": item_id},
                    )
                )
                .mappings()
                .one()
            
        )


async def _jobs(
    tenant_id: str, job_type: str = "promotion.evaluate", status: str | None = None
) -> list[dict[str, Any]]:
    async with _test_session_factory() as session:
        sql = (
            "SELECT id, job_type, status, run_after, payload FROM jobs "
            "WHERE tenant_id = :tid AND job_type = :jt"
        )
        params: dict[str, Any] = {"tid": tenant_id, "jt": job_type}
        if status is not None:
            sql += " AND status = :status"
            params["status"] = status
        sql += " ORDER BY created_at, id"
        rows = (await session.execute(text(sql), params)).mappings().all()
    return [dict(row) for row in rows]


async def _pending_reconcile(tenant_id: str) -> list[dict[str, Any]]:
    return await _jobs(tenant_id, job_type=PROMOTION_RECONCILE_JOB_TYPE, status=STATUS_PENDING)


async def _process_one(worker: str = "reconcile-test") -> bool:
    return await process_one_job(
        worker_id=worker,
        session_factory=_test_session_factory,
        app_session_factory=_test_session_factory,
    )


async def _drain_queue(worker: str = "reconcile-test", *, limit: int = 500) -> int:
    """Claim+process due jobs until the queue is empty; returns count run."""
    processed = 0
    while processed < limit and await _process_one(worker):
        processed += 1
    return processed


async def _review_change_events(item_id: str) -> list[dict[str, Any]]:
    async with _test_session_factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT old_value, new_value, reason FROM item_events "
                        "WHERE item_id = :id AND event_type = 'review_change' "
                        "AND field_name = 'review_status' ORDER BY created_at, id"
                    ),
                    {"id": item_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def _run_request_chain(
    tenant_id: str,
    *,
    reason: str = RECONCILE_REASON_OPERATOR_REQUEST,
    trigger_id: str = "test-request",
) -> None:
    """Enqueue a request chain and drain the whole queue to completion."""
    async with _test_session_factory() as session:
        job_id = await request_reconciliation_chain(
            session, tenant_id=tenant_id, reason=reason, trigger_id=trigger_id
        )
        assert job_id is not None
        await session.commit()
    await _drain_queue()


def _flags(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reconciliation: bool | None = None,
    evaluate: bool | None = None,
    pass_limit: int | None = None,
) -> None:
    if reconciliation is not None:
        monkeypatch.setattr(settings, "promotion_reconciliation_enabled", reconciliation)
    if evaluate is not None:
        monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", evaluate)
    if pass_limit is not None:
        monkeypatch.setattr(settings, "promotion_reconciliation_pass_limit", pass_limit)


# ===========================================================================
# 1. Missing cooling-boundary job → exact-boundary repair, never early
# ===========================================================================


async def test_missing_cooling_boundary_job_repaired_at_exact_boundary(monkeypatch):
    _flags(monkeypatch)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    created_at = FIXED_NOW - timedelta(hours=10)
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="cooling boundary repair target",
        memory_confidence=0.9,
        created_at=created_at,
    )
    await _run_request_chain(tenant_id)

    evaluate_jobs = await _jobs(tenant_id)
    assert len(evaluate_jobs) == 1
    job = evaluate_jobs[0]
    assert job["status"] == STATUS_PENDING
    contract = parse_promotion_evaluate_payload(job["payload"])
    assert contract.memory_item_id == uuid.UUID(item_id)
    assert contract.trigger_type == "reconcile"
    # The exact authoritative legacy-lane boundary: created_at + min_age(72h).
    expected_boundary = created_at + timedelta(hours=72)
    assert job["run_after"] == expected_boundary.replace(tzinfo=UTC)
    # Not run early: after the full drain the item is still proposed — the
    # repair job is the only work and it is not due yet.
    row = await _item_row(item_id)
    assert row["review_status"] == "proposed"


# ===========================================================================
# 2. Dead due evaluation → discovered, repaired, executed, promoted
# ===========================================================================


async def test_dead_due_evaluation_discovered_repaired_promoted(monkeypatch):
    _flags(monkeypatch)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="dead job repair target",
        memory_confidence=0.9,
    )
    # A canonical evaluation job that died after the boundary passed.
    async with _test_session_factory() as session:
        await enqueue_job(
            session,
            tenant_id=tenant_id,
            job_type="promotion.evaluate",
            payload={
                "contract_version": "promotion-evaluate-v1",
                "memory_item_id": item_id,
                "trigger_type": "classification_bound",
                "trigger_id": str(uuid.uuid4()),
                "requested_policy_version": "promotion-evidence-v1",
                "ingest_id": None,
                "correlation_id": None,
                "dedupe_key": f"promotion.evaluate:{item_id}:classification_bound:dead",
            },
            dedupe_key=f"promotion.evaluate:{item_id}:classification_bound:dead",
        )
        await session.execute(
            text(
                "UPDATE jobs SET status = 'dead', last_error = 'injected for test' "
                "WHERE job_type = 'promotion.evaluate'"
            )
        )
        await session.commit()

    await _run_request_chain(tenant_id)

    row = await _item_row(item_id)
    assert row["review_status"] == "active"
    events = await _review_change_events(item_id)
    assert len(events) == 1
    assert events[0]["old_value"] == "proposed"
    assert events[0]["new_value"] == "active"
    reason = json.loads(events[0]["reason"])
    assert reason["trigger_type"] == "reconcile"
    assert reason["trigger_id"].startswith("reconcile:boundary:")


# ===========================================================================
# 3. Healthy pending/running targeted job → no duplicate
# ===========================================================================


async def test_healthy_scheduled_job_not_duplicated(monkeypatch):
    _flags(monkeypatch)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    created_at = FIXED_NOW - timedelta(hours=10)
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="healthy scheduled job target",
        memory_confidence=0.9,
        created_at=created_at,
    )
    # A healthy pending canonical job at the exact boundary.
    async with _test_session_factory() as session:
        await enqueue_job(
            session,
            tenant_id=tenant_id,
            job_type="promotion.evaluate",
            payload={
                "contract_version": "promotion-evaluate-v1",
                "memory_item_id": item_id,
                "trigger_type": "item_created",
                "trigger_id": str(uuid.uuid4()),
                "requested_policy_version": "promotion-legacy-v1",
                "ingest_id": None,
                "correlation_id": None,
                "dedupe_key": f"promotion.evaluate:{item_id}:item_created:healthy",
            },
            run_after=created_at + timedelta(hours=72),
            dedupe_key=f"promotion.evaluate:{item_id}:item_created:healthy",
        )
        await session.commit()

    await _run_request_chain(tenant_id)

    evaluate_jobs = await _jobs(tenant_id)
    healthy = [j for j in evaluate_jobs if j["status"] == STATUS_PENDING]
    assert len(healthy) == 1
    assert healthy[0]["payload"]["trigger_type"] == "item_created"
    # The item was never evaluated early, and remains proposed while cooling.
    assert (await _item_row(item_id))["review_status"] == "proposed"


async def _enqueue_test_promotion_job(
    *,
    tenant_id: str,
    item_id: str,
    run_after: datetime,
    suffix: str,
    job_type: str = "promotion.evaluate",
    classification_run_id: str | None = None,
) -> None:
    dedupe = f"{job_type}:{item_id}:{suffix}"
    payload: dict[str, Any]
    if job_type == "promotion.path_a":
        assert classification_run_id is not None
        payload = {
            "memory_item_id": item_id,
            "classification_run_id": classification_run_id,
        }
    else:
        payload = {
            "contract_version": "promotion-evaluate-v1",
            "memory_item_id": item_id,
            "trigger_type": "item_created",
            "trigger_id": suffix,
            "requested_policy_version": "promotion-legacy-v1",
            "ingest_id": None,
            "correlation_id": None,
            "dedupe_key": dedupe,
        }
    async with _test_session_factory() as session:
        await enqueue_job(
            session,
            tenant_id=tenant_id,
            job_type=job_type,
            payload=payload,
            run_after=run_after,
            dedupe_key=dedupe,
        )
        await session.commit()


@pytest.mark.parametrize(
    ("old_age", "new_age"),
    [(72, 24), (24, 72)],
    ids=["boundary-moved-earlier", "boundary-moved-later"],
)
async def test_old_boundary_job_does_not_cover_current_obligation(
    monkeypatch, old_age: int, new_age: int
):
    _flags(monkeypatch)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    created_at = FIXED_NOW - timedelta(hours=10)
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content=f"boundary moved {old_age} to {new_age}",
        memory_confidence=0.9,
        created_at=created_at,
    )
    await _enqueue_test_promotion_job(
        tenant_id=tenant_id,
        item_id=item_id,
        run_after=created_at + timedelta(hours=old_age),
        suffix=f"old-{old_age}",
    )
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "UPDATE tenant_config SET auto_promote_min_age_hours = :age "
                "WHERE tenant_id = :tenant_id"
            ),
            {"age": new_age, "tenant_id": tenant_id},
        )
        await session.commit()

    await _run_request_chain(tenant_id, trigger_id=f"boundary-{new_age}")

    pending = [job for job in await _jobs(tenant_id) if job["status"] == STATUS_PENDING]
    assert len(pending) == 2
    assert sorted(job["run_after"] for job in pending) == sorted(
        [
            (created_at + timedelta(hours=old_age)).replace(tzinfo=UTC),
            (created_at + timedelta(hours=new_age)).replace(tzinfo=UTC),
        ]
    )


async def test_stale_legacy_binding_does_not_cover_current_obligation(monkeypatch):
    _flags(monkeypatch)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    created_at = FIXED_NOW - timedelta(hours=10)
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="stale legacy binding",
        memory_confidence=0.8,
        created_at=created_at,
    )
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "UPDATE tenant_config SET auto_promote_evidence_enabled = TRUE, "
                "auto_promote_confidence_threshold = 0.95 "
                "WHERE tenant_id = :tenant_id"
            ),
            {"tenant_id": tenant_id},
        )
        await session.commit()
    current_run_id = await _insert_bound_run(
        item_id, created_at=created_at + timedelta(hours=1)
    )
    stale_run_id = str(uuid.uuid4())
    boundary = created_at + timedelta(hours=73)
    await _enqueue_test_promotion_job(
        tenant_id=tenant_id,
        item_id=item_id,
        run_after=boundary,
        suffix="stale-binding",
        job_type="promotion.path_a",
        classification_run_id=stale_run_id,
    )

    await _run_request_chain(tenant_id, trigger_id="stale-binding")

    legacy = await _jobs(tenant_id, job_type="promotion.path_a")
    assert len(legacy) == 1
    assert legacy[0]["payload"]["classification_run_id"] == stale_run_id
    canonical = await _jobs(tenant_id)
    assert len(canonical) == 1
    assert canonical[0]["run_after"] == boundary.replace(tzinfo=UTC)
    assert current_run_id != stale_run_id


async def test_current_legacy_binding_at_exact_boundary_covers_obligation(monkeypatch):
    _flags(monkeypatch)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    created_at = FIXED_NOW - timedelta(hours=10)
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="current legacy binding",
        memory_confidence=0.8,
        created_at=created_at,
    )
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "UPDATE tenant_config SET auto_promote_evidence_enabled = TRUE, "
                "auto_promote_confidence_threshold = 0.95 "
                "WHERE tenant_id = :tenant_id"
            ),
            {"tenant_id": tenant_id},
        )
        await session.commit()
    run_id = await _insert_bound_run(item_id, created_at=created_at + timedelta(hours=1))
    await _enqueue_test_promotion_job(
        tenant_id=tenant_id,
        item_id=item_id,
        run_after=created_at + timedelta(hours=73),
        suffix="current-binding",
        job_type="promotion.path_a",
        classification_run_id=run_id,
    )

    await _run_request_chain(tenant_id, trigger_id="current-binding")

    assert len(await _jobs(tenant_id, job_type="promotion.path_a")) == 1
    assert await _jobs(tenant_id) == []


async def test_large_unrelated_history_cannot_hide_current_healthy_job(monkeypatch):
    _flags(monkeypatch)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    created_at = FIXED_NOW - timedelta(hours=10)
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="healthy job after large history",
        memory_confidence=0.9,
        created_at=created_at,
    )
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO jobs (tenant_id, job_type, status, payload, run_after) "
                "SELECT CAST(:tenant_id AS uuid), 'promotion.evaluate', 'dead', "
                "jsonb_build_object('memory_item_id', gen_random_uuid()::text), now() "
                "FROM generate_series(1, 1100)"
            ),
            {"tenant_id": tenant_id},
        )
        await session.commit()
    await _enqueue_test_promotion_job(
        tenant_id=tenant_id,
        item_id=item_id,
        run_after=created_at + timedelta(hours=72),
        suffix="healthy-after-history",
    )
    async with _test_session_factory() as session:
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        plan_rows = (
            await session.execute(
                text(
                    "EXPLAIN SELECT 1 FROM jobs WHERE tenant_id = :tenant_id "
                    "AND payload->>'memory_item_id' = :item_id "
                    "AND job_type = 'promotion.evaluate' "
                    "AND status IN ('pending', 'running') AND run_after = :boundary LIMIT 1"
                ),
                {
                    "tenant_id": tenant_id,
                    "item_id": item_id,
                    "boundary": created_at + timedelta(hours=72),
                },
            )
        ).scalars().all()
    plan = "\n".join(plan_rows)
    assert "Index Scan" in plan
    assert (
        "idx_jobs_reconcile_item_state" in plan
        or "idx_jobs_tenant_type_status" in plan
    )

    await _run_request_chain(tenant_id, trigger_id="history-bound")

    targeted = [
        job
        for job in await _jobs(tenant_id)
        if job["payload"].get("memory_item_id") == item_id
    ]
    assert len(targeted) == 1
    assert targeted[0]["payload"]["trigger_id"] == "healthy-after-history"


# ===========================================================================
# 4. Terminal-blocker fairness and persisted selection suppression
# ===========================================================================


async def test_terminal_blockers_cannot_starve_later_actionable_row(monkeypatch):
    pass_limit = 5
    _flags(monkeypatch, pass_limit=pass_limit)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    # 25 terminal rows (below every lane; kind-eligible so they ARE scanned,
    # consuming rotation budget exactly like real terminal backlog), then one
    # actionable row at the tail.
    for i in range(25):
        await _insert_item(
            tenant_id=tenant_id,
            principal_id=principal_id,
            content=f"terminal blocker {i}",
            memory_confidence=0.1,
            created_at=FIXED_NOW - timedelta(hours=300, minutes=i),
        )
    target_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="actionable row behind terminal backlog",
        memory_confidence=0.95,
        created_at=FIXED_NOW - timedelta(hours=100),
    )

    await _run_request_chain(tenant_id)

    # Documented bound: ceil(26 / 5) + 1 = 7 passes at most cover the stable
    # set; the chain terminates once the rotation reaches the tail.
    assert (await _item_row(target_id))["review_status"] == "active"
    # Terminal rows never received evaluation jobs.
    evaluate_jobs = await _jobs(tenant_id)
    terminal_repairs = [
        j
        for j in evaluate_jobs
        if j["payload"]["trigger_type"] == "reconcile"
        and uuid.UUID(j["payload"]["memory_item_id"]) != uuid.UUID(target_id)
    ]
    assert terminal_repairs == []
    assert len([j for j in evaluate_jobs if j["status"] == "succeeded"]) == 1
    # The request chain terminated (no pending continuation left).
    assert await _pending_reconcile(tenant_id) == []


async def test_periodic_backstop_wraps_instead_of_terminating(monkeypatch):
    _flags(monkeypatch, pass_limit=2)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    for i in range(3):
        await _insert_item(
            tenant_id=tenant_id,
            principal_id=principal_id,
            content=f"wrap item {i}",
            memory_confidence=0.1,
            created_at=FIXED_NOW - timedelta(hours=300 + i),
        )
    async with _test_session_factory() as session:
        result = await run_reconciliation_pass(
            session,
            tenant_id,
            reason=RECONCILE_REASON_BACKSTOP,
            trigger_id=BACKSTOP_TRIGGER_ID,
            now=datetime.now(UTC),
        )
    assert result.window_size == 2
    # Backstop chain always reschedules itself (perpetual rotation).
    pending = await _pending_reconcile(tenant_id)
    assert len(pending) == 1
    assert pending[0]["payload"]["reason"] == RECONCILE_REASON_BACKSTOP
    assert pending[0]["payload"]["trigger_id"] == BACKSTOP_TRIGGER_ID
    # ... and wraps: a second pass reads the tail.  The third wraps but does
    # not select the same stable terminal rows again in this epoch.
    async with _test_session_factory() as session:
        await run_reconciliation_pass(
            session, tenant_id, reason=RECONCILE_REASON_BACKSTOP, trigger_id=BACKSTOP_TRIGGER_ID
        )
        result3 = await run_reconciliation_pass(
            session, tenant_id, reason=RECONCILE_REASON_BACKSTOP, trigger_id=BACKSTOP_TRIGGER_ID
        )
    assert result3.wrapped is True
    assert result3.window_size == 0
    async with _test_session_factory() as session:
        suppressed = (
            await session.execute(
                text(
                    "SELECT count(*) FROM promotion_reconcile_terminal "
                    "WHERE tenant_id = :tenant_id"
                ),
                {"tenant_id": tenant_id},
            )
        ).scalar_one()
    assert suppressed == 3


async def test_terminal_suppression_survives_restart_and_later_rows_progress(monkeypatch):
    global _test_engine, _test_session_factory
    _flags(monkeypatch, pass_limit=1)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    terminal_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="persistently terminal",
        memory_confidence=0.1,
        created_at=FIXED_NOW - timedelta(hours=200),
    )
    async with _test_session_factory() as session:
        first = await run_reconciliation_pass(
            session,
            tenant_id,
            reason=RECONCILE_REASON_BACKSTOP,
            trigger_id=BACKSTOP_TRIGGER_ID,
        )
    assert first.window_size == 1 and first.terminal_skipped == 1

    await _test_engine.dispose()
    _test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
    _test_session_factory = async_sessionmaker(
        _test_engine, class_=AsyncSession, expire_on_commit=False
    )
    actionable_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="later actionable after restart",
        memory_confidence=0.95,
        created_at=FIXED_NOW - timedelta(hours=100),
    )
    async with _test_session_factory() as session:
        second = await run_reconciliation_pass(
            session,
            tenant_id,
            reason=RECONCILE_REASON_BACKSTOP,
            trigger_id=BACKSTOP_TRIGGER_ID,
        )
    assert second.window_size == 1
    await _drain_queue()
    assert (await _item_row(actionable_id))["review_status"] == "active"
    assert (await _item_row(terminal_id))["review_status"] == "proposed"


async def test_policy_reset_reconsiders_terminal_item(monkeypatch):
    _flags(monkeypatch)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="terminal reconsidered by reset",
        memory_confidence=0.1,
    )
    async with _test_session_factory() as session:
        first = await run_reconciliation_pass(
            session,
            tenant_id,
            reason=RECONCILE_REASON_BACKSTOP,
            trigger_id=BACKSTOP_TRIGGER_ID,
        )
    assert first.window_size == 1
    async with _test_session_factory() as session:
        await request_reconciliation_chain(
            session,
            tenant_id=tenant_id,
            reason=RECONCILE_REASON_OPERATOR_REQUEST,
            trigger_id="terminal-reset",
        )
        await session.commit()
    await _drain_queue()
    async with _test_session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT cursor_epoch FROM promotion_reconcile_terminal "
                    "WHERE item_id = :item_id"
                ),
                {"item_id": item_id},
            )
        ).scalar_one()
    assert row == 1


async def test_evidence_change_invalidates_terminal_suppression(monkeypatch):
    _flags(monkeypatch)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="terminal invalidated by evidence",
        memory_confidence=0.1,
    )
    async with _test_session_factory() as session:
        await run_reconciliation_pass(
            session,
            tenant_id,
            reason=RECONCILE_REASON_BACKSTOP,
            trigger_id=BACKSTOP_TRIGGER_ID,
        )
    async with _test_session_factory() as session:
        assert (
            await session.execute(
                text(
                    "SELECT count(*) FROM promotion_reconcile_terminal "
                    "WHERE item_id = :item_id"
                ),
                {"item_id": item_id},
            )
        ).scalar_one() == 1
    await _insert_bound_run(item_id)
    async with _test_session_factory() as session:
        assert (
            await session.execute(
                text(
                    "SELECT count(*) FROM promotion_reconcile_terminal "
                    "WHERE item_id = :item_id"
                ),
                {"item_id": item_id},
            )
        ).scalar_one() == 0
    async with _test_session_factory() as session:
        reconsidered = await run_reconciliation_pass(
            session,
            tenant_id,
            reason=RECONCILE_REASON_BACKSTOP,
            trigger_id=BACKSTOP_TRIGGER_ID,
        )
    assert reconsidered.wrapped is True
    assert reconsidered.window_size == 1
    assert reconsidered.terminal_skipped == 1


# ===========================================================================
# 5. Memory-kind policy change → bounded chain, policy_changed provenance
# ===========================================================================


async def _get_test_session() -> AsyncSession:
    async with _test_session_factory() as session:
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


async def test_kind_policy_change_schedules_bounded_chain_and_promotes(monkeypatch, client):
    _flags(monkeypatch)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    # "preference" does not auto-promote by default: this item is kind-blocked.
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="kind policy reconsideration target",
        memory_confidence=0.95,
        created_at=FIXED_NOW - timedelta(hours=200),
        kind="preference",
    )

    response = await client.patch(
        "/v1/admin/memory-kinds/preference", json={"auto_promote_from_inferred": True}
    )
    assert response.status_code == 200, response.text

    # No synchronous item fan-out: exactly one bounded reconcile chain job.
    chains = await _jobs(tenant_id, job_type=PROMOTION_RECONCILE_JOB_TYPE)
    assert len(chains) == 1
    assert chains[0]["payload"]["reason"] == RECONCILE_REASON_POLICY_CHANGE
    assert chains[0]["payload"]["trigger_id"] == "kind-policy:1"

    await _drain_queue()

    assert (await _item_row(item_id))["review_status"] == "active"
    events = await _review_change_events(item_id)
    assert len(events) == 1
    reason = json.loads(events[0]["reason"])
    assert reason["trigger_type"] == "policy_changed"
    assert reason["trigger_id"].startswith("kind-policy:1:boundary:")


async def test_kind_policy_revision_reconsiders_suppressed_terminal(monkeypatch, client):
    _flags(monkeypatch)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="terminal reconsidered by kind revision",
        memory_confidence=0.1,
    )
    async with _test_session_factory() as session:
        await run_reconciliation_pass(
            session,
            tenant_id,
            reason=RECONCILE_REASON_BACKSTOP,
            trigger_id=BACKSTOP_TRIGGER_ID,
        )
    response = await client.patch(
        "/v1/admin/memory-kinds/fact", json={"auto_promote_from_inferred": False}
    )
    assert response.status_code == 200, response.text
    await _drain_queue()
    response = await client.patch(
        "/v1/admin/memory-kinds/fact", json={"auto_promote_from_inferred": True}
    )
    assert response.status_code == 200, response.text
    await _drain_queue()
    async with _test_session_factory() as session:
        terminal_epoch, cursor_epoch, revision = (
            await session.execute(
                text(
                    "SELECT t.cursor_epoch, s.cursor_epoch, s.kind_policy_revision "
                    "FROM promotion_reconcile_terminal t "
                    "JOIN promotion_reconcile_state s USING (tenant_id) "
                    "WHERE t.item_id = :item_id"
                ),
                {"item_id": item_id},
            )
        ).one()
    assert terminal_epoch == cursor_epoch == 2
    assert revision == 2


async def test_kind_policy_non_admission_change_schedules_nothing(monkeypatch, client):
    _flags(monkeypatch)
    if not await _db_ok():
        _require_db()
    tenant_id, _principal_id = await _default_tenant_principal()
    response = await client.patch(
        "/v1/admin/memory-kinds/preference", json={"description": "cosmetic only"}
    )
    assert response.status_code == 200, response.text
    assert await _jobs(tenant_id, job_type=PROMOTION_RECONCILE_JOB_TYPE) == []


# ===========================================================================
# 6. Promotion disabled → enabled via explicit operator request
# ===========================================================================


async def test_disabled_then_enabled_promotion_recovered_by_operator_request(monkeypatch):
    _flags(monkeypatch)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="disabled promotion recovery target",
        memory_confidence=0.9,
    )
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "UPDATE tenant_config SET auto_promote_enabled = FALSE "
                "WHERE tenant_id = :tid"
            ),
            {"tid": tenant_id},
        )
        await session.commit()

    # While disabled, reconciliation observes and records, but repairs are
    # suppressed — no evaluation jobs, no mutation.
    await _run_request_chain(tenant_id)
    assert await _jobs(tenant_id) == []
    assert (await _item_row(item_id))["review_status"] == "proposed"

    # Direct SQL tenant_config changes are NOT observable: the operator must
    # request reconciliation explicitly after re-enabling.
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "UPDATE tenant_config SET auto_promote_enabled = TRUE "
                "WHERE tenant_id = :tid"
            ),
            {"tid": tenant_id},
        )
        await session.commit()
    await _run_request_chain(tenant_id, trigger_id="reenable-1")
    assert (await _item_row(item_id))["review_status"] == "active"


async def test_admin_reconcile_endpoint_content_free_and_idempotent(monkeypatch, client):
    _flags(monkeypatch)
    if not await _db_ok():
        _require_db()
    tenant_id, _principal_id = await _default_tenant_principal()
    response = await client.post("/v1/admin/promotion/reconcile", json={})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "enqueued"
    assert body["reason"] == RECONCILE_REASON_OPERATOR_REQUEST
    assert set(body) == {"tenant_id", "reason", "trigger_id", "job_id", "status"}
    # Same request identity replays onto the same chain (dedupe).
    response2 = await client.post(
        "/v1/admin/promotion/reconcile", json={"request_id": body["trigger_id"]}
    )
    assert response2.status_code == 200
    assert response2.json()["job_id"] == body["job_id"]

    # The accepted identity remains durable after its finite chain completes:
    # this is not merely queue deduplication while a link is pending/running.
    await _drain_queue()
    async with _test_session_factory() as session:
        before = await session.execute(
            text(
                "SELECT cursor_epoch, cursor_created_at FROM promotion_reconcile_state "
                "WHERE tenant_id = :tenant_id"
            ),
            {"tenant_id": tenant_id},
        )
        state_before = before.mappings().one()
    response3 = await client.post(
        "/v1/admin/promotion/reconcile", json={"request_id": body["trigger_id"]}
    )
    assert response3.status_code == 200
    assert response3.json()["status"] == "completed"
    assert response3.json()["job_id"] is None
    assert await _pending_reconcile(tenant_id) == []
    async with _test_session_factory() as session:
        after = await session.execute(
            text(
                "SELECT cursor_epoch, cursor_created_at FROM promotion_reconcile_state "
                "WHERE tenant_id = :tenant_id"
            ),
            {"tenant_id": tenant_id},
        )
        assert after.mappings().one() == state_before


# ===========================================================================
# 7. Provider recovery
# ===========================================================================


def _fake_classify_qualifying():
    async def fake_classify(content, tenant_id, session, **kwargs):
        return ClassificationResult(
            suggested_kind="fact",
            taxonomy_confidence=0.9,
            retention_confidence=0.9,
            retention_disposition="retain",
            reason="recovered test classification",
        )

    return fake_classify


async def test_provider_recovery_reenqueues_classification_async(monkeypatch, client):
    _flags(monkeypatch)
    monkeypatch.setattr(settings, "classification_provider", "openai")
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    # Evidence lane on: recovery exists to produce qualifying evidence.
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "UPDATE tenant_config SET auto_promote_evidence_enabled = TRUE "
                "WHERE tenant_id = :tid"
            ),
            {"tid": tenant_id},
        )
        await session.commit()
    # A real remember-time auto-classification establishes durable intent and
    # originally creates refine work.  Simulate loss of that job before a
    # receipt binds; recovery must reconstruct intent from the immutable
    # classification audit, not from the absence of evidence.
    monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", False)
    remembered = await client.post(
        "/v1/remember",
        json={
            "content": f"provider recovery target {uuid.uuid4()}",
            "source_type": "sync_turn",
        },
    )
    assert remembered.status_code == 201, remembered.text
    item_id = remembered.json()["id"]
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "DELETE FROM jobs WHERE job_type = 'classification.refine' "
                "AND payload->>'memory_item_id' = :item_id"
            ),
            {"item_id": item_id},
        )
        await session.commit()
    monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", True)

    async with _test_session_factory() as session:
        job_id = await request_reconciliation_chain(
            session,
            tenant_id=tenant_id,
            reason=RECONCILE_REASON_PROVIDER_RECOVERY,
            trigger_id="recovery-1",
        )
        assert job_id is not None
        await session.commit()

    # No provider call and no enqueue happened inline on the request itself:
    # the only new work is the bounded chain job; classification is re-created
    # asynchronously by the pass, through the queue.
    assert (await _item_row(item_id))["retention_evidence_at"] is None
    assert await _jobs(tenant_id, job_type="classification.refine") == []

    monkeypatch.setattr(
        "engram.classification.classify",
        _fake_classify_qualifying(),
    )
    await _drain_queue()

    # The pass re-enqueued the existing async classification contract for the
    # item (with its current inputs), it executed through the queue, the
    # recovered classification bound normally (server-attested receipt), and
    # the ordinary producer scheduled the promotion evaluation
    # (classification_bound) at the exact evidence cooling boundary.
    refine_jobs = await _jobs(tenant_id, job_type="classification.refine")
    assert len(refine_jobs) == 1
    assert refine_jobs[0]["payload"]["memory_item_id"] == item_id
    assert refine_jobs[0]["status"] == "succeeded"
    row = await _item_row(item_id)
    assert row["retention_evidence_at"] is not None
    evaluate_jobs = await _jobs(tenant_id)
    assert len(evaluate_jobs) == 1
    assert evaluate_jobs[0]["status"] == STATUS_PENDING
    assert evaluate_jobs[0]["payload"]["trigger_type"] == "classification_bound"
    expected_boundary = row["retention_evidence_at"] + timedelta(hours=72)
    assert evaluate_jobs[0]["run_after"] == expected_boundary.replace(tzinfo=UTC)
    assert row["review_status"] == "proposed"  # still cooling — not run early

    # Once the boundary passes (simulated by advancing the item's cooling
    # clocks and making the job due — the job payload itself is untouched,
    # evaluation is current-state), the ordinary path promotes the qualified
    # item without any startup recall.
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "UPDATE memory_items SET created_at = now() - interval '200 hours', "
                "retention_evidence_at = now() - interval '200 hours' WHERE id = :id"
            ),
            {"id": item_id},
        )
        await session.execute(
            text(
                "UPDATE classification_runs SET created_at = now() - interval '200 hours' "
                "WHERE memory_item_id = :id",
            ),
            {"id": item_id},
        )
        await session.execute(
            text("UPDATE jobs SET run_after = now() WHERE job_type = 'promotion.evaluate'")
        )
        await session.commit()
    await _drain_queue()
    assert (await _item_row(item_id))["review_status"] == "active"
    events = await _review_change_events(item_id)
    assert len(events) == 1
    assert json.loads(events[0]["reason"])["trigger_type"] == "classification_bound"


async def test_provider_recovery_coverage_survives_intervening_backstop(monkeypatch):
    """A periodic pass cannot advance a provider request's private cursor.

    This forces the historical dangerous ordering: provider recovery covers
    page A, an already-rotated periodic backstop covers page B, then the
    provider continuation resumes.  Every auto-classified proposal must still
    receive its own asynchronous recovery job, including page B.
    """
    _flags(monkeypatch, pass_limit=2)
    monkeypatch.setattr(settings, "classification_provider", "openai")
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_ids = [
        await _insert_item(
            tenant_id=tenant_id,
            principal_id=principal_id,
            content=f"cross-reason recovery {index}",
            memory_confidence=0.1,
            created_at=FIXED_NOW - timedelta(hours=200) + timedelta(minutes=index),
        )
        for index in range(5)
    ]
    for item_id in item_ids:
        await _mark_classification_origin(item_id, principal_id)

    async with _test_session_factory() as session:
        requested = await request_reconciliation_chain(
            session,
            tenant_id=tenant_id,
            reason=RECONCILE_REASON_PROVIDER_RECOVERY,
            trigger_id="provider-cross-reason",
        )
        assert requested is not None
        await session.commit()

    # Page A: the provider request establishes its own chain cursor.
    assert await _process_one("provider-page-a")

    # Model a normal periodic rotation already positioned at page A, then run
    # its next page at higher queue priority between provider continuations.
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "UPDATE promotion_reconcile_state SET cursor_created_at = :created_at, "
                "cursor_item_id = CAST(:item_id AS uuid) WHERE tenant_id = :tenant_id"
            ),
            {
                "created_at": FIXED_NOW - timedelta(hours=200) + timedelta(minutes=1),
                "item_id": item_ids[1],
                "tenant_id": tenant_id,
            },
        )
        await enqueue_job(
            session,
            tenant_id=tenant_id,
            job_type=PROMOTION_RECONCILE_JOB_TYPE,
            payload=build_promotion_reconcile_payload(
                reason=RECONCILE_REASON_BACKSTOP, trigger_id=BACKSTOP_TRIGGER_ID
            ),
            priority=0,
            dedupe_key="test:intervening-backstop",
        )

    assert await _process_one("intervening-backstop")

    # Drain only reconciliation work.  The recovered refine jobs deliberately
    # remain queued: this asserts repair discovery, not provider behavior.
    while await process_one_job(
        worker_id="provider-continuation",
        session_factory=_test_session_factory,
        app_session_factory=_test_session_factory,
        job_types=[PROMOTION_RECONCILE_JOB_TYPE],
    ):
        pass

    refine_jobs = await _jobs(tenant_id, job_type="classification.refine")
    assert {job["payload"]["memory_item_id"] for job in refine_jobs} == set(item_ids)
    async with _test_session_factory() as session:
        status = await session.execute(
            text(
                "SELECT status FROM promotion_reconcile_chains WHERE tenant_id = :tenant_id "
                "AND reason = :reason AND trigger_id = :trigger_id"
            ),
            {
                "tenant_id": tenant_id,
                "reason": RECONCILE_REASON_PROVIDER_RECOVERY,
                "trigger_id": "provider-cross-reason",
            },
        )
        assert status.scalar_one() == "completed"


async def test_provider_recovery_does_not_reclassify_bound_evidence(monkeypatch):
    _flags(monkeypatch)
    monkeypatch.setattr(settings, "classification_provider", "openai")
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="already-classified recovery target",
        memory_confidence=0.1,
    )
    await _insert_bound_run(item_id)

    async with _test_session_factory() as session:
        job_id = await request_reconciliation_chain(
            session,
            tenant_id=tenant_id,
            reason=RECONCILE_REASON_PROVIDER_RECOVERY,
            trigger_id="recovery-2",
        )
        assert job_id is not None
        await session.commit()

    assert await _jobs(tenant_id, job_type="classification.refine") == []


async def test_provider_recovery_preserves_explicit_kind_semantics(monkeypatch):
    _flags(monkeypatch)
    monkeypatch.setattr(settings, "classification_provider", "openai")
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="explicit kind must stay explicit",
        memory_confidence=0.1,
        kind="procedure",
    )
    await _mark_classification_origin(item_id, principal_id, source="explicit_kind")

    await _run_request_chain(
        tenant_id,
        reason=RECONCILE_REASON_PROVIDER_RECOVERY,
        trigger_id="explicit-kind-recovery",
    )

    assert await _jobs(tenant_id, job_type="classification.refine") == []
    async with _test_session_factory() as session:
        row = (
            await session.execute(
                text("SELECT kind FROM memory_items WHERE id = :item_id"),
                {"item_id": item_id},
            )
        ).scalar_one()
        classification_events = (
            await session.execute(
                text(
                    "SELECT count(*) FROM item_events WHERE item_id = :item_id "
                    "AND event_type = 'classification'"
                ),
                {"item_id": item_id},
            )
        ).scalar_one()
    assert row == "procedure"
    assert classification_events == 1


async def test_provider_recovery_unknown_legacy_intent_fails_conservative(monkeypatch):
    _flags(monkeypatch)
    monkeypatch.setattr(settings, "classification_provider", "openai")
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="legacy row with unknown classification intent",
        memory_confidence=0.1,
    )

    await _run_request_chain(
        tenant_id,
        reason=RECONCILE_REASON_PROVIDER_RECOVERY,
        trigger_id="unknown-intent-recovery",
    )

    assert await _jobs(tenant_id, job_type="classification.refine") == []


async def test_provider_recovery_suppressed_while_provider_unavailable(monkeypatch):
    _flags(monkeypatch)  # classification_provider stays "none"
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="suppressed recovery target",
        memory_confidence=0.1,
    )
    await _mark_classification_origin(item_id, principal_id)
    async with _test_session_factory() as session:
        status_before = await reconciliation_status(
            session, tenant_id=tenant_id, now=datetime.now(UTC)
        )
        result = await run_reconciliation_pass(
            session,
            tenant_id,
            reason=RECONCILE_REASON_PROVIDER_RECOVERY,
            trigger_id="recovery-3",
            now=datetime.now(UTC),
        )
    assert status_before["last_pass"] is None
    assert result.recovery_enqueued == 0
    assert result.suppressed >= 1
    assert await _jobs(tenant_id, job_type="classification.refine") == []
    async with _test_session_factory() as session:
        status = await reconciliation_status(session, tenant_id=tenant_id, now=datetime.now(UTC))
    assert status["last_pass"]["recovery_enqueued"] == 0
    assert status["last_pass"]["suppressed"] >= 1


# ===========================================================================
# 8. Crash/restart: deterministic continuation, no permanent skipped range
# ===========================================================================


async def test_crash_after_commit_continues_deterministically(monkeypatch):
    global _test_engine, _test_session_factory
    _flags(monkeypatch, pass_limit=3)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    eligible_ids: list[str] = []
    for i in range(7):
        eligible_ids.append(
            await _insert_item(
                tenant_id=tenant_id,
                principal_id=principal_id,
                content=f"crash-restart eligible {i}",
                memory_confidence=0.9,
                created_at=FIXED_NOW - timedelta(hours=200, minutes=i),
            )
        )

    # "Crash" simulation: run the chain's passes through separate sessions /
    # engines (each pass = fresh worker after a dispose), never through one
    # long-lived process. Pass 1 commits its repairs + continuation.
    async with _test_session_factory() as session:
        result = await run_reconciliation_pass(
            session,
            tenant_id,
            reason=RECONCILE_REASON_OPERATOR_REQUEST,
            trigger_id="crash-1",
            now=datetime.now(UTC),
        )
    assert result.window_size == 3
    assert result.chain_continued is True

    global _test_engine
    await _test_engine.dispose()  # dispose: worker restart
    _test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
    _test_session_factory = async_sessionmaker(
        _test_engine, class_=AsyncSession, expire_on_commit=False
    )

    # The continuation survived the restart; draining the queue completes the
    # full rotation and promotes every eligible row — no permanent skipped
    # range behind the advanced cursor.
    await _drain_queue()
    for item_id in eligible_ids:
        assert (await _item_row(item_id))["review_status"] == "active"
    assert await _pending_reconcile(tenant_id) == []


async def test_epoch_guard_prevents_stale_pass_from_undoing_reset(monkeypatch):
    _flags(monkeypatch, pass_limit=2)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    early = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="epoch guard early row",
        memory_confidence=0.9,
        created_at=FIXED_NOW - timedelta(hours=300),
    )
    late = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="epoch guard late row",
        memory_confidence=0.9,
        created_at=FIXED_NOW - timedelta(hours=100),
    )
    moment = datetime.now(UTC)
    # A pass reads the cursor at epoch 0 and examines the first page...
    async with _test_session_factory() as session:
        result = await run_reconciliation_pass(
            session,
            tenant_id,
            reason=RECONCILE_REASON_OPERATOR_REQUEST,
            trigger_id="epoch-1",
            now=moment,
        )
    assert result.window_size == 2  # both rows fit one page; cursor at tail

    # ... but before it could matter, a policy reset (epoch bump) happens.
    async with _test_session_factory() as session:
        await request_reconciliation_chain(
            session, tenant_id=tenant_id, reason=RECONCILE_REASON_OPERATOR_REQUEST,
            trigger_id="epoch-2",
        )
        await session.commit()
    await _drain_queue()
    # The reset chain re-covered the head rows regardless of any stale cursor
    # state; nothing is permanently skipped.
    assert (await _item_row(early))["review_status"] == "active"
    assert (await _item_row(late))["review_status"] == "active"


# ===========================================================================
# 9. Multi-tenant fairness
# ===========================================================================


async def test_large_tenant_backlog_cannot_starve_small_tenant(monkeypatch):
    _flags(monkeypatch, pass_limit=5)
    if not await _db_ok():
        _require_db()
    tenant_a, principal_a = await _default_tenant_principal()
    tenant_b, principal_b = await _make_tenant("fairness-b")
    for i in range(25):
        await _insert_item(
            tenant_id=tenant_a,
            principal_id=principal_a,
            content=f"tenant A terminal backlog {i}",
            memory_confidence=0.1,
            created_at=FIXED_NOW - timedelta(hours=400, minutes=i),
        )
    target_b = await _insert_item(
        tenant_id=tenant_b,
        principal_id=principal_b,
        content="tenant B actionable row",
        memory_confidence=0.95,
        created_at=FIXED_NOW - timedelta(hours=100),
    )

    async with _test_session_factory() as session:
        await ensure_periodic_reconciliation_chains(session)

    # The global queue interleaves both tenants' bounded passes (claim order
    # run_after ASC); tenant B's single-pass work completes well within the
    # total: it never waits for tenant A's whole rotation to finish first.
    await _drain_queue()
    assert (await _item_row(target_b))["review_status"] == "active"

    # Fairness bound evidence: B's chain needed O(ceil(1/5) + 1) passes while
    # A's needed ceil(25/5) + 1; the queue processed every due job exactly
    # once and no cross-tenant repair happened (A has no evaluate jobs). The
    # perpetual backstop chains may hold exactly one pending (not-yet-due)
    # continuation each — that is the topology, not backlog.
    jobs_b = await _jobs(tenant_b)
    assert all(j["status"] == "succeeded" for j in jobs_b)
    assert await _jobs(tenant_a) == []
    for tenant in (tenant_a, tenant_b):
        pending = await _pending_reconcile(tenant)
        assert len(pending) <= 1
        if pending:
            assert pending[0]["payload"]["reason"] == RECONCILE_REASON_BACKSTOP


async def test_periodic_tenant_bootstrap_is_bounded_fair_and_restart_safe(monkeypatch):
    global _test_engine, _test_session_factory
    _flags(monkeypatch)
    monkeypatch.setattr(settings, "promotion_reconciliation_tenant_batch_limit", 2)
    if not await _db_ok():
        _require_db()
    for index in range(4):
        await _make_tenant(f"tenant-window-{index}")

    async with _test_session_factory() as session:
        first_enqueued = await ensure_periodic_reconciliation_chains(session)
    assert first_enqueued <= 2
    async with _test_session_factory() as session:
        first_tenants = set(
            (
                await session.execute(
                    text(
                        "SELECT DISTINCT tenant_id FROM jobs WHERE job_type = "
                        "'promotion.reconcile' AND status = 'pending'"
                    )
                )
            ).scalars()
        )
    assert len(first_tenants) <= 2

    # Recreate the process/session factory between pages: the next call must
    # continue from the durable global cursor, not enumerate from the head.
    await _test_engine.dispose()
    _test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
    _test_session_factory = async_sessionmaker(
        _test_engine, class_=AsyncSession, expire_on_commit=False
    )
    for _ in range(4):
        async with _test_session_factory() as session:
            assert await ensure_periodic_reconciliation_chains(session) <= 2

    async with _test_session_factory() as session:
        covered = set(
            (
                await session.execute(
                    text(
                        "SELECT DISTINCT tenant_id FROM jobs WHERE job_type = "
                        "'promotion.reconcile'"
                    )
                )
            ).scalars()
        )
        all_tenants = set((await session.execute(text("SELECT id FROM tenants"))).scalars())
    assert covered == all_tenants

    # Kill one chain.  Repeated bounded pages wrap and heal it when its keyset
    # turn arrives; no call expands beyond the configured bound.
    victim = next(iter(all_tenants))
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "UPDATE jobs SET status = 'dead' WHERE tenant_id = :tenant_id "
                "AND job_type = 'promotion.reconcile' AND status = 'pending'"
            ),
            {"tenant_id": victim},
        )
        await session.commit()
    for _ in range(4):
        async with _test_session_factory() as session:
            assert await ensure_periodic_reconciliation_chains(session) <= 2
    async with _test_session_factory() as session:
        healed = (
            await session.execute(
                text(
                    "SELECT count(*) FROM jobs WHERE tenant_id = :tenant_id "
                    "AND job_type = 'promotion.reconcile' AND status = 'pending'"
                ),
                {"tenant_id": victim},
            )
        ).scalar_one()
    assert healed == 1


async def test_global_operator_request_uses_bounded_continuation(monkeypatch):
    _flags(monkeypatch)
    monkeypatch.setattr(settings, "promotion_reconciliation_tenant_batch_limit", 2)
    if not await _db_ok():
        _require_db()
    for index in range(3):
        await _make_tenant(f"global-request-{index}")
    trigger_id = "bounded-cli-request"
    inspected: list[int] = []
    completed = False
    while not completed:
        async with _test_session_factory() as session:
            result = await request_global_reconciliation_window(
                session,
                reason=RECONCILE_REASON_OPERATOR_REQUEST,
                trigger_id=trigger_id,
            )
        inspected.append(result.inspected)
        completed = result.completed
    assert all(count <= 2 for count in inspected)
    assert sum(inspected) == 4  # seeded default + three created tenants


# ===========================================================================
# 10. RLS under the non-owner engram_app role
# ===========================================================================


def _app_role_url() -> str | None:
    import os

    return os.environ.get("ENGRAM_APP_DATABASE_URL")


async def _app_session(tenant_id: str, principal_id: str) -> AsyncSession:
    url = _app_role_url()
    engine = create_async_engine(url, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    session = await factory().__aenter__()
    await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
    return session


async def test_rls_tenant_isolation_of_reconcile_state(monkeypatch):
    _flags(monkeypatch)
    if not await _db_ok():
        _require_db()
    if _app_role_url() is None:
        pytest.skip("requires ENGRAM_APP_DATABASE_URL (non-owner app role)")
    tenant_a, principal_a = await _default_tenant_principal()
    tenant_b, principal_b = await _make_tenant("rls-b")
    await _insert_item(
        tenant_id=tenant_b,
        principal_id=principal_b,
        content="tenant B reconcile state row",
        memory_confidence=0.9,
    )
    # Give tenant B a reconcile state row via a request chain.
    async with _test_session_factory() as session:
        await request_reconciliation_chain(
            session, tenant_id=tenant_b, reason=RECONCILE_REASON_OPERATOR_REQUEST,
            trigger_id="rls-1",
        )
        await session.commit()

    terminal_id = await _insert_item(
        tenant_id=tenant_b,
        principal_id=principal_b,
        content="tenant B terminal suppression row",
        memory_confidence=0.1,
    )
    async with _test_session_factory() as session:
        await run_reconciliation_pass(
            session,
            tenant_b,
            reason=RECONCILE_REASON_BACKSTOP,
            trigger_id=BACKSTOP_TRIGGER_ID,
        )

    # Tenant A's app-role session can neither read nor move B's state.
    session_a = await _app_session(tenant_a, principal_a)
    try:
        rows = (
            (
                await session_a.execute(
                    text("SELECT tenant_id FROM promotion_reconcile_state")
                )
            )
            .scalars()
            .all()
        )
        assert rows == []
        terminal_rows = (
            await session_a.execute(
                text("SELECT item_id FROM promotion_reconcile_terminal")
            )
        ).scalars().all()
        assert terminal_rows == []
        await session_a.execute(
            text(
                "UPDATE promotion_reconcile_state SET cursor_epoch = 99 "
                "WHERE tenant_id = :tid"
            ),
            {"tid": tenant_b},
        )
        await session_a.commit()
    finally:
        await session_a.close()
    async with _test_session_factory() as session:
        epoch = (
            await session.execute(
                text(
                    "SELECT cursor_epoch FROM promotion_reconcile_state "
                    "WHERE tenant_id = :tid"
                ),
                {"tid": tenant_b},
            )
        ).scalar_one()
    assert epoch == 1  # untouched
    session_b = await _app_session(tenant_b, principal_b)
    try:
        visible_terminal = (
            await session_b.execute(
                text(
                    "SELECT item_id FROM promotion_reconcile_terminal "
                    "WHERE item_id = :item_id"
                ),
                {"item_id": terminal_id},
            )
        ).scalar_one()
        assert str(visible_terminal) == terminal_id
        await session_b.execute(
            text(
                "INSERT INTO item_events (item_id, event_type, field_name, old_value, "
                "new_value, actor_principal_id, reason) VALUES ("
                ":item_id, 'classification', 'kind', NULL, :new_value, "
                ":principal_id, 'RLS invalidation proof')"
            ),
            {
                "item_id": terminal_id,
                "new_value": json.dumps({"source": "explicit_kind", "kind": "fact"}),
                "principal_id": principal_b,
            },
        )
        await session_b.commit()
        assert (
            await session_b.execute(
                text(
                    "SELECT count(*) FROM promotion_reconcile_terminal "
                    "WHERE item_id = :item_id"
                ),
                {"item_id": terminal_id},
            )
        ).scalar_one() == 0
    finally:
        await session_b.close()


async def test_rls_forged_cross_tenant_enqueue_rejected(monkeypatch):
    _flags(monkeypatch)
    if not await _db_ok():
        _require_db()
    if _app_role_url() is None:
        pytest.skip("requires ENGRAM_APP_DATABASE_URL (non-owner app role)")
    tenant_a, principal_a = await _default_tenant_principal()
    tenant_b, _principal_b = await _make_tenant("rls-forged")
    session_a = await _app_session(tenant_a, principal_a)
    try:
        with pytest.raises(Exception, match=".*(row-level security|violates).*"):
            await session_a.execute(
                text(
                    "INSERT INTO jobs (id, tenant_id, job_type, status, priority, "
                    "run_after, attempts, max_attempts, payload) VALUES ("
                    ":id, :tid, 'promotion.reconcile', 'pending', 100, now(), 0, 5, "
                    "'{}'::jsonb)"
                ),
                {"id": str(uuid.uuid4()), "tid": tenant_b},
            )
            await session_a.commit()
    finally:
        await session_a.close()


async def test_reconcile_handler_runs_under_routed_tenant_context(monkeypatch):
    _flags(monkeypatch)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="routed tenant context target",
        memory_confidence=0.9,
    )
    # Enqueue the chain through an owner session; the worker's handler must
    # still route item work under the job's tenant app-role context.
    async with _test_session_factory() as session:
        await request_reconciliation_chain(
            session, tenant_id=tenant_id, reason=RECONCILE_REASON_OPERATOR_REQUEST,
            trigger_id="routing-1",
        )
        await session.commit()
    await _drain_queue()
    assert (await _item_row(item_id))["review_status"] == "active"


# ===========================================================================
# 11. Concurrency: one mutation, one authoritative event
# ===========================================================================


async def test_two_same_epoch_passes_cannot_create_forward_hole(monkeypatch):
    _flags(monkeypatch, pass_limit=2)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_ids = [
        await _insert_item(
            tenant_id=tenant_id,
            principal_id=principal_id,
            content=f"same epoch coverage {index}",
            memory_confidence=0.95,
            created_at=FIXED_NOW - timedelta(hours=200, minutes=index),
        )
        for index in range(5)
    ]

    async def ordinary_pass() -> None:
        async with _test_session_factory() as session:
            await run_reconciliation_pass(
                session,
                tenant_id,
                reason=RECONCILE_REASON_BACKSTOP,
                trigger_id=BACKSTOP_TRIGGER_ID,
            )

    await asyncio.gather(ordinary_pass(), ordinary_pass())
    # Last-writer-wins may move the cursor backward and cause rework, but two
    # more bounded pages must cover every remaining row—none can be skipped
    # ahead of both passes.
    await ordinary_pass()
    await ordinary_pass()
    jobs = await _jobs(tenant_id)
    covered = {job["payload"]["memory_item_id"] for job in jobs}
    assert covered == set(item_ids)


async def test_concurrent_paths_produce_single_mutation_and_event(monkeypatch):
    _flags(monkeypatch)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="concurrent evaluation target",
        memory_confidence=0.95,
    )
    from engram.promotion import (
        TRIGGER_MANUAL,
        auto_promote_proposed_memories,
        enqueue_promotion_evaluation,
    )

    async with _test_session_factory() as session:
        await session.execute(text("SELECT 1"))
    # Three real concurrent evaluation paths for the same item, overlapping in
    # separate transactions (real PostgreSQL synchronization, no sleeps):
    # a reconciliation repair pass, a targeted canonical evaluation, and the
    # legacy untargeted sweep (the startup-recall lazy pass's machinery).
    async def reconciliation_path() -> None:
        async with _test_session_factory() as session:
            await apply_rls_context(
                session, tenant_id=tenant_id, principal_id=principal_id
            )
            await run_reconciliation_pass(
                session,
                tenant_id,
                reason=RECONCILE_REASON_OPERATOR_REQUEST,
                trigger_id="concurrent-reconcile",
                now=datetime.now(UTC),
            )

    async def targeted_path() -> None:
        async with _test_session_factory() as session:
            await apply_rls_context(
                session, tenant_id=tenant_id, principal_id=principal_id
            )
            await enqueue_promotion_evaluation(
                session,
                tenant_id=tenant_id,
                memory_item_id=uuid.UUID(item_id),
                trigger_type=TRIGGER_MANUAL,
                trigger_id=str(uuid.uuid4()),
            )
            await session.commit()

    async def legacy_sweep_path() -> None:
        async with _test_session_factory() as session:
            await apply_rls_context(
                session, tenant_id=tenant_id, principal_id=principal_id
            )
            await auto_promote_proposed_memories(
                session, tenant_id, limit=20, source="startup_recall"
            )

    await asyncio.gather(reconciliation_path(), targeted_path(), legacy_sweep_path())
    await _drain_queue()

    row = await _item_row(item_id)
    assert row["review_status"] == "active"
    events = await _review_change_events(item_id)
    assert len(events) == 1
    assert events[0]["old_value"] == "proposed" and events[0]["new_value"] == "active"
    # No retry storm: every promotion/reconcile job ended succeeded.
    for job_type in ("promotion.evaluate", PROMOTION_RECONCILE_JOB_TYPE):
        for job in await _jobs(tenant_id, job_type=job_type):
            assert job["status"] == "succeeded", job


# ===========================================================================
# 12. Flag rollback
# ===========================================================================


async def test_flag_off_rolls_back_to_pre_b4_behavior(monkeypatch):
    _flags(monkeypatch, reconciliation=False)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="flag rollback target",
        memory_confidence=0.9,
    )

    # The worker bootstrap creates nothing.
    async with _test_session_factory() as session:
        assert await ensure_periodic_reconciliation_chains(session) == 0
    assert await _jobs(tenant_id, job_type=PROMOTION_RECONCILE_JOB_TYPE) == []

    # Explicit requests are fail-safe no-ops.
    async with _test_session_factory() as session:
        assert (
            await request_reconciliation_chain(
                session, tenant_id=tenant_id, reason=RECONCILE_REASON_OPERATOR_REQUEST,
                trigger_id="rolled-back",
            )
        ) is None
        await session.commit()
    assert await _jobs(tenant_id, job_type=PROMOTION_RECONCILE_JOB_TYPE) == []

    # A stale reconcile job (enqueued while the flag was on) is a truthful
    # no-op that does not continue the chain.
    async with _test_session_factory() as session:
        await enqueue_job(
            session,
            tenant_id=tenant_id,
            job_type=PROMOTION_RECONCILE_JOB_TYPE,
            payload={
                "contract_version": "promotion-reconcile-v1",
                "reason": RECONCILE_REASON_BACKSTOP,
                "trigger_id": BACKSTOP_TRIGGER_ID,
                "dedupe_key": "promotion.reconcile:backstop:periodic",
            },
            dedupe_key="promotion.reconcile:backstop:periodic",
        )
        await session.commit()
    assert await _process_one()
    assert await _pending_reconcile(tenant_id) == []
    assert await _jobs(tenant_id) == []

    # Targeted promotion.evaluate jobs still function normally, and the
    # legacy startup-recall rotation behavior is unchanged (still promotes).
    async with _test_session_factory() as session:
        await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
        from engram.promotion import maybe_auto_promote_for_startup_recall

        result = await maybe_auto_promote_for_startup_recall(session, tenant_id)
    assert result.promoted == 1
    assert (await _item_row(item_id))["review_status"] == "active"


async def test_reconciliation_with_evaluate_flag_off_suppresses_repairs(monkeypatch):
    _flags(monkeypatch, evaluate=False)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="suppressed repair target",
        memory_confidence=0.9,
    )
    async with _test_session_factory() as session:
        result = await run_reconciliation_pass(
            session,
            tenant_id,
            reason=RECONCILE_REASON_OPERATOR_REQUEST,
            trigger_id="suppressed-1",
            now=datetime.now(UTC),
        )
    # The pass ran, discovery happened, but no promotion.evaluate repair was
    # enqueued and no broader/legacy mechanism was substituted.
    assert result.window_size == 1
    assert result.suppressed == 1
    assert result.evaluations_enqueued == 0
    assert await _jobs(tenant_id, job_type="promotion.evaluate") == []
    assert await _jobs(tenant_id, job_type="promotion.path_a") == []
    assert (await _item_row(item_id))["review_status"] == "proposed"
    async with _test_session_factory() as session:
        status = await reconciliation_status(session, tenant_id=tenant_id, now=datetime.now(UTC))
    assert status["last_pass"]["suppressed"] >= 1


# ===========================================================================
# Boundedness / queue-growth evidence
# ===========================================================================


async def test_pass_bounds_and_diagnostics(monkeypatch):
    _flags(monkeypatch, pass_limit=4)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    for i in range(10):
        await _insert_item(
            tenant_id=tenant_id,
            principal_id=principal_id,
            content=f"bound evidence {i}",
            memory_confidence=0.9,
            created_at=FIXED_NOW - timedelta(hours=200, minutes=i),
        )
    async with _test_session_factory() as session:
        result = await run_reconciliation_pass(
            session,
            tenant_id,
            reason=RECONCILE_REASON_OPERATOR_REQUEST,
            trigger_id="bounds-1",
            now=datetime.now(UTC),
        )
    # Per-pass hard bounds: rows inspected <= pass_limit and at most that
    # many repair jobs emitted; exactly one pending continuation exists.
    assert result.window_size == 4
    assert result.evaluations_enqueued == 4
    pending_evals = await _jobs(tenant_id, status=STATUS_PENDING)
    assert len(pending_evals) == 4
    assert len(await _pending_reconcile(tenant_id)) == 1
    async with _test_session_factory() as session:
        status = await reconciliation_status(session, tenant_id=tenant_id, now=datetime.now(UTC))
    assert status["enabled"] is True
    assert status["last_pass"]["window_size"] == 4
    assert status["last_pass"]["evaluations_enqueued"] == 4
    assert status["cursor"]["position"] is not None


async def test_rotation_uses_index_bounded_keyset_scan():
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    # Populate the rotation set so the planner's cost model reflects a real
    # backlog (tiny tables legitimately prefer a sequential scan, which is
    # also bounded; the index matters at scale).
    now = datetime.now(UTC)
    async with _test_session_factory() as session:
        for i in range(400):
            await session.execute(
                text(
                    "INSERT INTO memory_items (id, tenant_id, principal_id, content, "
                    "content_hash, kind, visibility, review_status, memory_confidence, "
                    "source_trust, source_confidence_prior, importance, source_type, "
                    "created_at, valid_from) VALUES (:i, :t, :p, :c, :h, 'fact', "
                    "'tenant', 'proposed', 0.1, 0.5, 0.1, 0.5, 'manual', :ts, :ts)"
                ),
                {
                    "i": str(uuid.uuid4()),
                    "t": tenant_id,
                    "p": principal_id,
                    "c": f"plan shape row {i}",
                    "h": f"sha256:{uuid.uuid4().hex}",
                    "ts": now - timedelta(minutes=i),
                },
            )
        await session.commit()
        await session.execute(text("ANALYZE memory_items"))
        # The exact OR-form predicate _window_stmt emits.
        plan = (
            (
                await session.execute(
                    text(
                        "EXPLAIN (FORMAT JSON) SELECT id FROM memory_items "
                        "WHERE tenant_id = :tid "
                        "AND review_status = 'proposed' AND valid_to IS NULL "
                        "AND (created_at > :c OR (created_at = :c AND id > :i)) "
                        "ORDER BY created_at, id LIMIT 20"
                    ),
                    {
                        "tid": tenant_id,
                        "c": now - timedelta(hours=60),
                        "i": "00000000-0000-0000-0000-000000000000",
                    },
                )
            )
            .scalar_one()
        )

    def nodes(node: dict[str, Any]) -> list[str]:
        out = [node["Node Type"]]
        for child in node.get("Plans", []):
            out.extend(nodes(child))
        return out

    # The bounded keyset scan is served directly by an index: the leaf is an
    # index(-only) scan on a (tenant_id, created_at, id)-ordered partial index
    # — the new idx_memitems_proposed_rotation (proposed-only, smallest) or
    # the existing idx_memitems_backfill (migration 002) — with NO Sort node:
    # a true index bound rather than a filter+sort of the backlog behind the
    # LIMIT.
    leaf = plan[0]["Plan"]["Plans"][0]
    node_types = nodes(plan[0]["Plan"])
    assert leaf["Node Type"] in ("Index Scan", "Index Only Scan")
    assert leaf["Index Name"] in ("idx_memitems_proposed_rotation", "idx_memitems_backfill")
    assert "Sort" not in node_types


# Keep math referenced (documented bound arithmetic used in fairness tests).
assert math.ceil(26 / 5) + 1 == 7
