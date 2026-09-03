"""Real-PostgreSQL coverage for the canonical ``promotion.evaluate`` job
contract (issue #155, ENG-PROMOTION-003B2).

Exercises ``engram.promotion.enqueue_promotion_evaluation``,
``engram.promotion.evaluate_promotion_item_current_state``, and
``engram.worker.handle_promotion_evaluate`` against a live PostgreSQL with
the v2 schema: canonical enqueue dedupe/idempotency, the flag-gated
classification.refine producer, current-state (not stale enqueue-time)
semantics, audit/evaluation correlation, no-op failure semantics, mixed
canonical/legacy concurrency, and RLS/tenant isolation. Skips automatically
when no DB is reachable (mirroring tests/test_promotion.py).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.config import settings
from engram.db import _DEFAULT_PRINCIPAL_NAME, _DEFAULT_TENANT_SLUG, apply_rls_context
from engram.jobs import STATUS_PENDING, enqueue_job
from engram.models import ClassificationRun, Job, MemoryItem
from engram.promotion import (
    PROMOTION_EVALUATE_CONTRACT_VERSION,
    TRIGGER_CLASSIFICATION_BOUND,
    TRIGGER_MANUAL,
    enqueue_promotion_evaluation,
    evaluate_promotion_item_current_state,
    promotion_evaluate_dedupe_key,
)
from engram.worker import handle_promotion_evaluate, process_one_job

pytestmark = pytest.mark.asyncio

_test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_test_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    _test_engine, class_=AsyncSession, expire_on_commit=False
)

FIXED_NOW = datetime.now(UTC).replace(microsecond=0)


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
    pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")


@pytest.fixture(autouse=True)
async def _clean_db():
    if not await _db_ok():
        return
    async with _test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM jobs"))
        await conn.execute(text("DELETE FROM item_events"))
        await conn.execute(text("DELETE FROM classification_runs"))
        await conn.execute(text("DELETE FROM memory_items"))
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


async def _rls_session(tenant_id: str, principal_id: str) -> AsyncSession:
    session = _test_session_factory()
    await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
    return session


async def _enable_evidence_lane(tenant_id: str) -> None:
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "UPDATE tenant_config SET auto_promote_evidence_enabled = TRUE "
                "WHERE tenant_id = :tid"
            ),
            {"tid": tenant_id},
        )
        await session.commit()


async def _insert_item(
    *,
    tenant_id: str,
    principal_id: str,
    content: str,
    memory_confidence: float = 0.5,
    created_at: datetime | None = None,
    kind: str = "fact",
    source_type: str = "manual",
    source_confidence_prior: float | None = None,
    retention_confidence: float | None = None,
    retention_disposition: str | None = None,
    retention_evidence_at: datetime | None = None,
    review_status: str = "proposed",
    visibility: str = "tenant",
) -> str:
    item_id = str(uuid.uuid4())
    if created_at is None:
        created_at = FIXED_NOW - timedelta(hours=200)
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO memory_items ("
                "id, tenant_id, principal_id, content, content_hash, kind, "
                "visibility, review_status, memory_confidence, source_trust, "
                "source_confidence_prior, retention_confidence, retention_disposition, "
                "retention_evidence_at, importance, source_type, created_at, valid_from"
                ") VALUES ("
                ":id, :tenant_id, :principal_id, :content, :content_hash, :kind, "
                ":visibility, :review_status, :memory_confidence, 0.5, "
                ":source_confidence_prior, :retention_confidence, :retention_disposition, "
                ":retention_evidence_at, 0.5, :source_type, :created_at, :created_at"
                ")"
            ),
            {
                "id": item_id,
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "content": content,
                "content_hash": f"sha256:{uuid.uuid4().hex}",
                "kind": kind,
                "visibility": visibility,
                "review_status": review_status,
                "memory_confidence": memory_confidence,
                "source_type": source_type,
                "source_confidence_prior": source_confidence_prior,
                "retention_confidence": retention_confidence,
                "retention_disposition": retention_disposition,
                "retention_evidence_at": retention_evidence_at,
                "created_at": created_at,
            },
        )
        await session.commit()
    return item_id


async def _insert_bound_evidence(
    item_id: str,
    *,
    tenant_id: str,
    principal_id: str,
    created_at: datetime,
    taxonomy_confidence: float = 0.9,
    ingest_id: str | None = None,
) -> str:
    run_id = str(uuid.uuid4())
    async with _test_session_factory() as session:
        item = (
            (
                await session.execute(
                    text(
                        "SELECT content_hash, source_type, kind, retention_confidence, "
                        "retention_disposition FROM memory_items WHERE id = :id"
                    ),
                    {"id": item_id},
                )
            )
            .mappings()
            .one()
        )
        await session.execute(
            text(
                "INSERT INTO classification_runs ("
                "id, tenant_id, principal_id, memory_item_id, bound_at, content_hash, "
                "canonicalization_version, source_type, suggested_kind, taxonomy_confidence, "
                "retention_confidence, retention_disposition, reason, provenance, "
                "classification_version, retention_policy_version, created_at, expires_at, "
                "ingest_id"
                ") VALUES ("
                ":id, :tenant_id, :principal_id, :item_id, :created_at, :content_hash, "
                "'canonical-v1', :source_type, :kind, :taxonomy_confidence, "
                ":retention_confidence, :retention_disposition, 'test evidence', "
                "'{}', 'classification-v2', 'retention-v1', "
                ":created_at, :expires_at, :ingest_id"
                ")"
            ),
            {
                "id": run_id,
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "item_id": item_id,
                "created_at": created_at,
                "content_hash": item["content_hash"],
                "source_type": item["source_type"],
                "kind": item["kind"],
                "taxonomy_confidence": taxonomy_confidence,
                "retention_confidence": item["retention_confidence"],
                "retention_disposition": item["retention_disposition"],
                "expires_at": created_at + timedelta(hours=1),
                "ingest_id": ingest_id,
            },
        )
        await session.commit()
    return run_id


async def _fetch_item(item_id: str) -> dict[str, Any]:
    async with _test_session_factory() as session:
        return (
            (
                await session.execute(
                    text(
                        "SELECT review_status, conflict_resolution_status, valid_to "
                        "FROM memory_items WHERE id = :id"
                    ),
                    {"id": item_id},
                )
            )
            .mappings()
            .one()
        )


async def _events_for(item_id: str) -> list[dict[str, Any]]:
    async with _test_session_factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT event_type, field_name, new_value, reason "
                        "FROM item_events WHERE item_id = :id ORDER BY created_at, id"
                    ),
                    {"id": item_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def _jobs_for_tenant(tenant_id: str) -> list[dict[str, Any]]:
    async with _test_session_factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT id, job_type, status, run_after, payload "
                        "FROM jobs WHERE tenant_id = :tid ORDER BY created_at"
                    ),
                    {"tid": tenant_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def _run_evaluate_job(
    tenant_id: str,
    principal_id: str,
    *,
    memory_item_id: str,
    trigger_type: str = TRIGGER_MANUAL,
    trigger_id: str = "trigger-1",
    run_after: datetime | None = None,
) -> None:
    async with _test_session_factory() as session:
        await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
        await enqueue_promotion_evaluation(
            session,
            tenant_id=tenant_id,
            memory_item_id=uuid.UUID(memory_item_id),
            trigger_type=trigger_type,
            trigger_id=trigger_id,
            run_after=run_after,
        )
        await session.commit()
    await process_one_job(
        worker_id="test",
        session_factory=_test_session_factory,
        app_session_factory=_test_session_factory,
        job_types=["promotion.evaluate"],
    )


# ===========================================================================
# 1. Canonical enqueue helper: dedupe / idempotency / transaction preservation
# ===========================================================================


async def test_enqueue_dedupes_same_item_trigger_type_trigger_id():
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="dedupe target"
    )
    async with _test_session_factory() as session:
        await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
        first = await enqueue_promotion_evaluation(
            session,
            tenant_id=tenant_id,
            memory_item_id=uuid.UUID(item_id),
            trigger_type=TRIGGER_CLASSIFICATION_BOUND,
            trigger_id="run-A",
        )
        await session.commit()
    async with _test_session_factory() as session:
        await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
        second = await enqueue_promotion_evaluation(
            session,
            tenant_id=tenant_id,
            memory_item_id=uuid.UUID(item_id),
            trigger_type=TRIGGER_CLASSIFICATION_BOUND,
            trigger_id="run-A",
        )
        await session.commit()
    assert first == second
    jobs = [j for j in await _jobs_for_tenant(tenant_id) if j["job_type"] == "promotion.evaluate"]
    assert len(jobs) == 1


async def test_enqueue_distinct_trigger_id_yields_distinct_job():
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="distinct trigger target"
    )
    async with _test_session_factory() as session:
        await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
        first = await enqueue_promotion_evaluation(
            session,
            tenant_id=tenant_id,
            memory_item_id=uuid.UUID(item_id),
            trigger_type=TRIGGER_CLASSIFICATION_BOUND,
            trigger_id="run-A",
        )
        await session.commit()
    async with _test_session_factory() as session:
        await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
        second = await enqueue_promotion_evaluation(
            session,
            tenant_id=tenant_id,
            memory_item_id=uuid.UUID(item_id),
            trigger_type=TRIGGER_CLASSIFICATION_BOUND,
            trigger_id="run-B",
        )
        await session.commit()
    assert first != second
    jobs = [j for j in await _jobs_for_tenant(tenant_id) if j["job_type"] == "promotion.evaluate"]
    assert len(jobs) == 2


async def test_enqueue_preserves_callers_outer_transaction():
    """A caller that rolls back its outer transaction must see no job row —
    enqueue_promotion_evaluation must never commit prematurely (issue #155)."""
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="rollback target"
    )
    async with _test_session_factory() as session:
        await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
        await enqueue_promotion_evaluation(
            session,
            tenant_id=tenant_id,
            memory_item_id=uuid.UUID(item_id),
            trigger_type=TRIGGER_MANUAL,
            trigger_id="never-committed",
        )
        await session.rollback()
    jobs = [j for j in await _jobs_for_tenant(tenant_id) if j["job_type"] == "promotion.evaluate"]
    assert jobs == []


async def test_enqueue_rejects_unknown_trigger_type():
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="bad trigger target"
    )
    from engram.promotion import PromotionEvaluateContractError

    async with _test_session_factory() as session:
        await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
        with pytest.raises(PromotionEvaluateContractError):
            await enqueue_promotion_evaluation(
                session,
                tenant_id=tenant_id,
                memory_item_id=uuid.UUID(item_id),
                trigger_type="not_a_real_trigger",
                trigger_id="x",
            )


# ===========================================================================
# 2. Flag-gated classification.refine producer
# ===========================================================================


async def test_flag_disabled_schedules_legacy_path_a(monkeypatch):
    if not await _db_ok():
        _require_db()
    monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", False)
    await _enable_evidence_lane((await _default_tenant_principal())[0])
    tenant_id, principal_id = await _default_tenant_principal()
    evidence_at = FIXED_NOW - timedelta(hours=1)
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="flag off item",
        source_type="sync_turn",
        memory_confidence=0.4,
        source_confidence_prior=0.4,
        retention_confidence=0.85,
        retention_disposition="retain",
        retention_evidence_at=evidence_at,
        created_at=evidence_at,
    )
    run_id = await _insert_bound_evidence(
        item_id, tenant_id=tenant_id, principal_id=principal_id, created_at=evidence_at
    )
    from engram.promotion import schedule_evidence_promotion_if_qualified

    async with _test_session_factory() as session:
        await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
        item_obj = (
            await session.execute(select(MemoryItem).where(MemoryItem.id == item_id))
        ).scalar_one()
        run_obj = (
            await session.execute(select(ClassificationRun).where(ClassificationRun.id == run_id))
        ).scalar_one()
        job_id = await schedule_evidence_promotion_if_qualified(session, item_obj, run_obj)
        await session.commit()
    assert job_id is not None
    jobs = await _jobs_for_tenant(tenant_id)
    assert [j["job_type"] for j in jobs] == ["promotion.path_a"]
    assert jobs[0]["payload"]["classification_run_id"] == run_id


async def test_flag_enabled_schedules_canonical_job_with_same_due_boundary(monkeypatch):
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    await _enable_evidence_lane(tenant_id)
    evidence_at = FIXED_NOW - timedelta(hours=1)
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="flag on item",
        source_type="sync_turn",
        memory_confidence=0.4,
        source_confidence_prior=0.4,
        retention_confidence=0.85,
        retention_disposition="retain",
        retention_evidence_at=evidence_at,
        created_at=evidence_at,
    )
    run_id = await _insert_bound_evidence(
        item_id, tenant_id=tenant_id, principal_id=principal_id, created_at=evidence_at
    )

    # Compute the expected run_after under the legacy code path (flag off)
    # first, from an identical fixture, so the two are directly comparable.
    monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", False)
    legacy_expected_run_after = evidence_at + timedelta(hours=72)

    monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", True)
    from engram.promotion import schedule_evidence_promotion_if_qualified

    async with _test_session_factory() as session:
        await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
        item_obj = (
            await session.execute(select(MemoryItem).where(MemoryItem.id == item_id))
        ).scalar_one()
        run_obj = (
            await session.execute(select(ClassificationRun).where(ClassificationRun.id == run_id))
        ).scalar_one()
        job_id = await schedule_evidence_promotion_if_qualified(session, item_obj, run_obj)
        await session.commit()
    assert job_id is not None
    jobs = await _jobs_for_tenant(tenant_id)
    assert [j["job_type"] for j in jobs] == ["promotion.evaluate"]
    payload = jobs[0]["payload"]
    assert payload["contract_version"] == PROMOTION_EVALUATE_CONTRACT_VERSION
    assert payload["trigger_type"] == TRIGGER_CLASSIFICATION_BOUND
    assert payload["trigger_id"] == run_id
    assert jobs[0]["run_after"] == legacy_expected_run_after


# ===========================================================================
# 3. Current-state semantics (issue #155's central proof)
# ===========================================================================


async def test_current_state_supersedes_stale_enqueue_time_trigger_and_promotes():
    """A trigger enqueued for an earlier observation (trigger_id=A) must not
    block or override a newer, currently-qualifying authoritative state — the
    job must promote based on what is in the database *now*."""
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    await _enable_evidence_lane(tenant_id)
    old_evidence_at = FIXED_NOW - timedelta(hours=200)
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="supersede me",
        source_type="sync_turn",
        memory_confidence=0.3,
        source_confidence_prior=0.3,
        retention_confidence=None,
        retention_disposition=None,
        retention_evidence_at=None,
        created_at=old_evidence_at,
    )
    # Enqueue an evaluation whose trigger_id names an observation ("run A")
    # that never actually got bound — the handler must never look it up.
    async with _test_session_factory() as session:
        await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
        await enqueue_promotion_evaluation(
            session,
            tenant_id=tenant_id,
            memory_item_id=uuid.UUID(item_id),
            trigger_type=TRIGGER_CLASSIFICATION_BOUND,
            trigger_id=str(uuid.uuid4()),  # "run A" — never bound, pure provenance
        )
        await session.commit()

    # Before the job runs, a newer authoritative state ("run B") becomes the
    # item's current bound evidence — directly at the data level, since the
    # application-level rebind path is out of scope for this slice. The item
    # row is updated first so the bound-evidence insert (which copies the
    # item's *current* retention fields, mirroring how a real bind_run()
    # ties a receipt to its item) reflects the new state, not the old one.
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "UPDATE memory_items SET source_confidence_prior = 0.4, "
                "retention_confidence = 0.85, retention_disposition = 'retain', "
                "retention_evidence_at = :evidence_at WHERE id = :id"
            ),
            {"evidence_at": old_evidence_at, "id": item_id},
        )
        await session.commit()
    await _insert_bound_evidence(
        item_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        created_at=old_evidence_at,
    )

    await process_one_job(
        worker_id="test",
        session_factory=_test_session_factory,
        app_session_factory=_test_session_factory,
        job_types=["promotion.evaluate"],
    )

    item = await _fetch_item(item_id)
    assert item["review_status"] == "active"
    events = await _events_for(item_id)
    promo_events = [e for e in events if e["event_type"] == "review_change"]
    assert len(promo_events) == 1
    reason = json.loads(promo_events[0]["reason"])
    assert reason["invocation_source"] == "promotion.evaluate"
    assert reason["basis"] == "retention_evidence"


async def test_current_state_defeats_previously_favorable_enqueue_time_state():
    """The inverse: state looked promotable when the trigger was enqueued,
    but current state no longer qualifies — the job must not promote."""
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="revoked eligibility",
        memory_confidence=0.9,
        created_at=FIXED_NOW - timedelta(hours=200),
    )
    async with _test_session_factory() as session:
        await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
        await enqueue_promotion_evaluation(
            session,
            tenant_id=tenant_id,
            memory_item_id=uuid.UUID(item_id),
            trigger_type=TRIGGER_MANUAL,
            trigger_id="looked-good-at-enqueue-time",
        )
        await session.commit()

    # Current state no longer qualifies: a kind-policy change makes the item
    # terminal under current policy, independent of confidence/age.
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "UPDATE memory_kinds SET auto_promote_from_inferred = FALSE "
                "WHERE tenant_id = :tid AND name = 'fact'"
            ),
            {"tid": tenant_id},
        )
        await session.commit()
    try:
        await process_one_job(
            worker_id="test",
            session_factory=_test_session_factory,
            app_session_factory=_test_session_factory,
            job_types=["promotion.evaluate"],
        )
        item = await _fetch_item(item_id)
        assert item["review_status"] == "proposed"
        events = await _events_for(item_id)
        assert not any(e["event_type"] == "review_change" for e in events)
    finally:
        async with _test_session_factory() as session:
            await session.execute(
                text(
                    "UPDATE memory_kinds SET auto_promote_from_inferred = TRUE "
                    "WHERE tenant_id = :tid AND name = 'fact'"
                ),
                {"tid": tenant_id},
            )
            await session.commit()


# ===========================================================================
# 4. Eligible evaluation promotes; audit carries evaluation/job/trigger metadata
# ===========================================================================


async def test_eligible_evaluation_promotes_with_full_audit_metadata():
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="legacy lane eligible",
        memory_confidence=0.9,
        created_at=FIXED_NOW - timedelta(hours=200),
    )
    async with _test_session_factory() as session:
        await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
        job_id = await enqueue_promotion_evaluation(
            session,
            tenant_id=tenant_id,
            memory_item_id=uuid.UUID(item_id),
            trigger_type=TRIGGER_MANUAL,
            trigger_id="manual-op-42",
            requested_policy_version="promotion-legacy-v1",
        )
        await session.commit()

    await process_one_job(
        worker_id="test",
        session_factory=_test_session_factory,
        app_session_factory=_test_session_factory,
        job_types=["promotion.evaluate"],
    )

    item = await _fetch_item(item_id)
    assert item["review_status"] == "active"
    events = await _events_for(item_id)
    promo_events = [e for e in events if e["event_type"] == "review_change"]
    assert len(promo_events) == 1
    reason = json.loads(promo_events[0]["reason"])
    assert reason["invocation_source"] == "promotion.evaluate"
    assert reason["job_id"] == str(job_id)
    assert reason["job_contract_version"] == PROMOTION_EVALUATE_CONTRACT_VERSION
    assert reason["trigger_type"] == TRIGGER_MANUAL
    assert reason["trigger_id"] == "manual-op-42"
    assert reason["requested_policy_version"] == "promotion-legacy-v1"
    assert reason["promotion_policy_version"] == "promotion-legacy-v1"
    assert reason["conflict_recheck"] == "clear"
    uuid.UUID(reason["evaluation_id"])  # well-formed, present


async def test_evaluate_promotion_item_current_state_direct_call():
    """The reusable current-state evaluator function, called directly (not
    through a job payload) — proves it, not just the worker glue, is what
    performs the evaluation."""
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="direct evaluator call",
        memory_confidence=0.9,
        created_at=FIXED_NOW - timedelta(hours=200),
    )
    async with _test_session_factory() as session:
        await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
        result = await evaluate_promotion_item_current_state(
            session,
            tenant_id,
            uuid.UUID(item_id),
            evaluation_context={
                "evaluation_id": str(uuid.uuid4()),
                "job_id": str(uuid.uuid4()),
                "job_contract_version": PROMOTION_EVALUATE_CONTRACT_VERSION,
                "trigger_type": TRIGGER_MANUAL,
                "trigger_id": "direct-call",
                "requested_policy_version": "promotion-legacy-v1",
            },
        )
    assert result.promoted == 1
    item = await _fetch_item(item_id)
    assert item["review_status"] == "active"


async def test_handle_promotion_evaluate_handler_directly():
    """The registered worker handler, invoked directly (not via
    ``process_one_job``) — proves the handler itself does the parse/reload/
    evaluate sequence documented in its contract."""
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="direct handler call",
        memory_confidence=0.9,
        created_at=FIXED_NOW - timedelta(hours=200),
    )
    async with _test_session_factory() as session:
        await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
        job_id = await enqueue_promotion_evaluation(
            session,
            tenant_id=tenant_id,
            memory_item_id=uuid.UUID(item_id),
            trigger_type=TRIGGER_MANUAL,
            trigger_id="direct-handler",
        )
        await session.commit()
    async with _test_session_factory() as session:
        await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
        job_obj = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        assert job_obj.status == "pending"  # sanity: was pending before the handler ran
        await handle_promotion_evaluate(session, job_obj)
        await session.commit()
    item = await _fetch_item(item_id)
    assert item["review_status"] == "active"


# ===========================================================================
# 5. No-op failure semantics
# ===========================================================================


async def test_noop_evaluations_succeed_without_mutation():
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()

    cooling_item = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="cooling",
        memory_confidence=0.9,
        created_at=FIXED_NOW - timedelta(hours=1),
    )
    missing_evidence_item = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="missing evidence",
        memory_confidence=0.1,
        created_at=FIXED_NOW - timedelta(hours=200),
    )
    already_active_item = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="already active",
        memory_confidence=0.9,
        created_at=FIXED_NOW - timedelta(hours=200),
        review_status="active",
    )

    for item_id in (cooling_item, missing_evidence_item, already_active_item):
        await _run_evaluate_job(tenant_id, principal_id, memory_item_id=item_id)

    for item_id, expected_status in (
        (cooling_item, "proposed"),
        (missing_evidence_item, "proposed"),
        (already_active_item, "active"),
    ):
        item = await _fetch_item(item_id)
        assert item["review_status"] == expected_status
        events = await _events_for(item_id)
        assert not any(e["event_type"] == "review_change" for e in events)


async def test_noop_for_missing_target_item():
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    ghost_id = str(uuid.uuid4())
    # Must not raise and must not dead-letter — a legitimate no-op outcome.
    await _run_evaluate_job(tenant_id, principal_id, memory_item_id=ghost_id)
    async with _test_session_factory() as session:
        job = (
            await session.execute(
                text("SELECT status FROM jobs WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        ).mappings().one()
    assert job["status"] == "succeeded"


async def test_malformed_contract_fails_and_retries_not_silently_noops():
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    async with _test_session_factory() as session:
        await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
        await enqueue_job(
            session,
            tenant_id=tenant_id,
            job_type="promotion.evaluate",
            payload={"contract_version": "not-a-real-version"},
        )
        await session.commit()
    await process_one_job(
        worker_id="test",
        session_factory=_test_session_factory,
        app_session_factory=_test_session_factory,
        job_types=["promotion.evaluate"],
    )
    async with _test_session_factory() as session:
        job = (
            await session.execute(
                text("SELECT status, attempts FROM jobs WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        ).mappings().one()
    # Retried (pending, attempts=1), not silently succeeded.
    assert job["status"] == STATUS_PENDING
    assert job["attempts"] == 1


# ===========================================================================
# 6. Conflict recheck only after otherwise-admissible assessment
# ===========================================================================


async def test_conflict_recheck_not_invoked_for_non_qualifying_item(monkeypatch):
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="not qualifying",
        memory_confidence=0.1,
        created_at=FIXED_NOW - timedelta(hours=1),
    )
    called = {"n": 0}

    async def _boom(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("conflict recheck must not run for a non-qualifying candidate")

    monkeypatch.setattr("engram.promotion.check_promotion_conflict", _boom)
    await _run_evaluate_job(tenant_id, principal_id, memory_item_id=item_id)
    assert called["n"] == 0


async def test_conflict_recheck_blocks_otherwise_admissible_item_with_audit(monkeypatch):
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="would qualify but conflicts",
        memory_confidence=0.9,
        created_at=FIXED_NOW - timedelta(hours=200),
    )
    # A real second item: the guarded UPDATE's conflicts_with_item_id FK
    # requires an actual memory_items row, even though check_promotion_conflict
    # is mocked below.
    conflicting_id = uuid.UUID(
        await _insert_item(
            tenant_id=tenant_id,
            principal_id=principal_id,
            content="the active item it conflicts with",
            memory_confidence=0.9,
            created_at=FIXED_NOW - timedelta(hours=200),
            review_status="active",
        )
    )

    from engram.conflicts import PromotionConflictCheck

    async def _blocked(*args, **kwargs):
        return PromotionConflictCheck(
            conflicting_item_id=conflicting_id,
            verdict="contradiction",
            reason="test-forced-conflict",
            used_embeddings=False,
        )

    monkeypatch.setattr("engram.promotion.check_promotion_conflict", _blocked)
    await _run_evaluate_job(
        tenant_id, principal_id, memory_item_id=item_id, trigger_id="conflict-case"
    )

    item = await _fetch_item(item_id)
    assert item["review_status"] == "proposed"
    assert item["conflict_resolution_status"] == "unresolved"
    events = await _events_for(item_id)
    conflict_events = [e for e in events if e["event_type"] == "conflict_resolution"]
    assert len(conflict_events) == 1
    reason = json.loads(conflict_events[0]["reason"])
    assert reason["conflict_recheck"] == "blocked"
    assert reason["invocation_source"] == "promotion.evaluate"
    assert reason["trigger_id"] == "conflict-case"
    uuid.UUID(reason["evaluation_id"])


# ===========================================================================
# 7. No provider reassessment
# ===========================================================================


async def test_handler_never_calls_classifier_or_embedding_provider(monkeypatch):
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="no provider calls",
        memory_confidence=0.9,
        created_at=FIXED_NOW - timedelta(hours=200),
    )

    async def _boom(*args, **kwargs):
        raise AssertionError("promotion.evaluate must never call a provider")

    monkeypatch.setattr("engram.classification.classify", _boom)
    monkeypatch.setattr("engram.embeddings.generate_embedding", _boom)
    await _run_evaluate_job(tenant_id, principal_id, memory_item_id=item_id)
    item = await _fetch_item(item_id)
    assert item["review_status"] == "active"


# ===========================================================================
# 8. Concurrency: duplicate canonical jobs, and canonical vs legacy race
# ===========================================================================


def _install_promo_pause_trigger(tag: str, pause_key: int, tenant_id: str, item_id: str):
    trigger = f"promo_eval_pause_{tag}"
    return (
        trigger,
        (
            f"CREATE FUNCTION {trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ "
            f"BEGIN PERFORM pg_advisory_xact_lock({pause_key}); RETURN NEW; END $$"
        ),
        (
            f"CREATE TRIGGER {trigger} BEFORE UPDATE OF review_status "
            f"ON memory_items FOR EACH ROW WHEN (OLD.tenant_id = '{tenant_id}' "
            f"AND OLD.review_status = 'proposed' AND OLD.id = '{item_id}') "
            f"EXECUTE FUNCTION {trigger}()"
        ),
    )


async def _wait_until_blocked(
    coordinator: AsyncSession, blocker_pid: int, expected: int = 1
) -> None:
    sql = text(
        "SELECT count(*) FROM pg_stat_activity"
        " WHERE :blocker_pid = ANY(pg_blocking_pids(pid))"
        " AND wait_event_type = 'Lock'"
    )
    for _ in range(1000):
        await coordinator.execute(text("SELECT pg_stat_clear_snapshot()"))
        n = (await coordinator.execute(sql, {"blocker_pid": blocker_pid})).scalar()
        if n == expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {expected} sessions blocked on pid {blocker_pid}")


_ADVISORY_WAITER_SQL = text(
    "SELECT pid FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
    "AND wait_event = 'advisory' AND pid != pg_backend_pid() "
    "ORDER BY backend_start DESC LIMIT 1"
)


async def _find_advisory_waiter_pid(
    coordinator: AsyncSession, *, attempts: int = 500
) -> int | None:
    """Poll for the pid of a session paused on the trigger's advisory lock.

    ``pg_stat_clear_snapshot()`` forces a fresh read of ``pg_stat_activity``
    on each attempt — like ``_wait_until_blocked`` above, without it the
    coordinator's own statement-level snapshot can miss a very recent wait.
    """
    for _ in range(attempts):
        await coordinator.execute(text("SELECT pg_stat_clear_snapshot()"))
        pid = (await coordinator.execute(_ADVISORY_WAITER_SQL)).scalar()
        if pid is not None:
            return pid
        await asyncio.sleep(0.01)
    return None


async def test_two_canonical_jobs_race_yields_one_mutation():
    """Two distinct valid promotion.evaluate jobs for the same currently
    eligible item, executed concurrently: one authoritative transition, one
    audit event, zero duplicates, both terminate safely (issue #155 §11)."""
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="racing canonical jobs",
        memory_confidence=0.9,
        created_at=FIXED_NOW - timedelta(hours=200),
    )
    tag = uuid.uuid4().hex[:10]
    pause_key = uuid.uuid4().int & ((1 << 63) - 1)
    trigger, create_fn_sql, create_trigger_sql = _install_promo_pause_trigger(
        tag, pause_key, tenant_id, item_id
    )

    coordinator = await _test_engine.connect()
    await coordinator.execute(text(create_fn_sql))
    await coordinator.execute(text(create_trigger_sql))
    await coordinator.commit()
    lock_conn = await _test_engine.connect()
    await lock_conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": pause_key})
    await lock_conn.commit()

    async def _enqueue_and_run(trigger_id: str) -> None:
        async with _test_session_factory() as session:
            await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
            await enqueue_promotion_evaluation(
                session,
                tenant_id=tenant_id,
                memory_item_id=uuid.UUID(item_id),
                trigger_type=TRIGGER_MANUAL,
                trigger_id=trigger_id,
            )
            await session.commit()
        await process_one_job(
            worker_id=f"race-{trigger_id}",
            session_factory=_test_session_factory,
            app_session_factory=_test_session_factory,
            job_types=["promotion.evaluate"],
        )

    try:
        task1 = asyncio.create_task(_enqueue_and_run("race-A"))
        blocker_pid = await _find_advisory_waiter_pid(coordinator)
        assert blocker_pid is not None, "task1 never reached the paused UPDATE"

        task2 = asyncio.create_task(_enqueue_and_run("race-B"))
        await _wait_until_blocked(coordinator, blocker_pid, expected=1)

        await lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": pause_key})
        await lock_conn.commit()

        await asyncio.wait_for(asyncio.gather(task1, task2), timeout=30)
    finally:
        await lock_conn.close()
        async with _test_engine.begin() as conn:
            await conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger} ON memory_items"))
            await conn.execute(text(f"DROP FUNCTION IF EXISTS {trigger}()"))
        await coordinator.close()

    item = await _fetch_item(item_id)
    assert item["review_status"] == "active"
    events = await _events_for(item_id)
    promo_events = [e for e in events if e["event_type"] == "review_change"]
    assert len(promo_events) == 1
    jobs = await _jobs_for_tenant(tenant_id)
    assert all(j["status"] == "succeeded" for j in jobs)


async def test_canonical_and_legacy_race_yields_one_mutation():
    """A legacy promotion.path_a job and a canonical promotion.evaluate job
    for the same eligible item, racing: at most one proposed->active
    transition and one promotion review_change event; the loser is an
    idempotent no-op (issue #155 §6, §11)."""
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    await _enable_evidence_lane(tenant_id)
    evidence_at = FIXED_NOW - timedelta(hours=200)
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="canonical vs legacy race",
        source_type="sync_turn",
        memory_confidence=0.4,
        source_confidence_prior=0.4,
        retention_confidence=0.85,
        retention_disposition="retain",
        retention_evidence_at=evidence_at,
        created_at=evidence_at,
    )
    run_id = await _insert_bound_evidence(
        item_id, tenant_id=tenant_id, principal_id=principal_id, created_at=evidence_at
    )

    tag = uuid.uuid4().hex[:10]
    pause_key = uuid.uuid4().int & ((1 << 63) - 1)
    trigger, create_fn_sql, create_trigger_sql = _install_promo_pause_trigger(
        tag, pause_key, tenant_id, item_id
    )
    coordinator = await _test_engine.connect()
    await coordinator.execute(text(create_fn_sql))
    await coordinator.execute(text(create_trigger_sql))
    await coordinator.commit()
    lock_conn = await _test_engine.connect()
    await lock_conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": pause_key})
    await lock_conn.commit()

    async def _run_legacy() -> None:
        async with _test_session_factory() as session:
            await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
            await enqueue_job(
                session,
                tenant_id=tenant_id,
                job_type="promotion.path_a",
                payload={"memory_item_id": item_id, "classification_run_id": run_id},
            )
            await session.commit()
        await process_one_job(
            worker_id="race-legacy",
            session_factory=_test_session_factory,
            app_session_factory=_test_session_factory,
            job_types=["promotion.path_a"],
        )

    async def _run_canonical() -> None:
        async with _test_session_factory() as session:
            await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
            await enqueue_promotion_evaluation(
                session,
                tenant_id=tenant_id,
                memory_item_id=uuid.UUID(item_id),
                trigger_type=TRIGGER_CLASSIFICATION_BOUND,
                trigger_id=run_id,
            )
            await session.commit()
        await process_one_job(
            worker_id="race-canonical",
            session_factory=_test_session_factory,
            app_session_factory=_test_session_factory,
            job_types=["promotion.evaluate"],
        )

    try:
        task1 = asyncio.create_task(_run_legacy())
        blocker_pid = await _find_advisory_waiter_pid(coordinator)
        assert blocker_pid is not None, "legacy task never reached the paused UPDATE"

        task2 = asyncio.create_task(_run_canonical())
        await _wait_until_blocked(coordinator, blocker_pid, expected=1)

        await lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": pause_key})
        await lock_conn.commit()

        await asyncio.wait_for(asyncio.gather(task1, task2), timeout=30)
    finally:
        await lock_conn.close()
        async with _test_engine.begin() as conn:
            await conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger} ON memory_items"))
            await conn.execute(text(f"DROP FUNCTION IF EXISTS {trigger}()"))
        await coordinator.close()

    item = await _fetch_item(item_id)
    assert item["review_status"] == "active"
    events = await _events_for(item_id)
    promo_events = [e for e in events if e["event_type"] == "review_change"]
    assert len(promo_events) == 1
    jobs = await _jobs_for_tenant(tenant_id)
    assert all(j["status"] == "succeeded" for j in jobs)
    assert {j["job_type"] for j in jobs} == {"promotion.path_a", "promotion.evaluate"}


# ===========================================================================
# 9. RLS / tenant isolation
# ===========================================================================


async def test_cross_tenant_forged_item_id_cannot_mutate():
    if not await _db_ok():
        _require_db()
    owner_url = os.getenv("ENGRAM_OWNER_DATABASE_URL") or os.getenv("ENGRAM_DATABASE_URL")
    app_url = os.getenv("ENGRAM_APP_DATABASE_URL")
    if not owner_url or not app_url:
        pytest.skip("requires migrated PostgreSQL and the non-owner application role")
    owner = create_async_engine(owner_url, poolclass=NullPool)
    app = create_async_engine(app_url, poolclass=NullPool)
    owner_factory = async_sessionmaker(owner, class_=AsyncSession, expire_on_commit=False)
    app_factory = async_sessionmaker(app, class_=AsyncSession, expire_on_commit=False)
    try:
        async with owner.connect() as conn:
            await conn.execute(text("SELECT 1"))
        async with app.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        await owner.dispose()
        await app.dispose()
        pytest.skip("requires migrated PostgreSQL and the non-owner application role")

    tag = uuid.uuid4().hex[:12]
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    principal_a = uuid.uuid4()
    principal_b = uuid.uuid4()
    victim_item = uuid.uuid4()
    try:
        async with owner.begin() as conn:
            for tid, pid, name in ((tenant_a, principal_a, "a"), (tenant_b, principal_b, "b")):
                await conn.execute(
                    text("INSERT INTO tenants (id,name,slug) VALUES (:id,:n,:n)"),
                    {"id": tid, "n": f"pe-rls-{tag}-{name}"},
                )
                await conn.execute(
                    text(
                        "INSERT INTO tenant_config (tenant_id,config_version,active) "
                        "VALUES (:id,'proof',true)"
                    ),
                    {"id": tid},
                )
                await conn.execute(
                    text(
                        "INSERT INTO principals (id,tenant_id,name,type) "
                        "VALUES (:id,:tid,:n,'agent')"
                    ),
                    {"id": pid, "tid": tid, "n": f"principal-{name}-{tag}"},
                )
            old = datetime.now(UTC) - timedelta(hours=200)
            await conn.execute(
                text(
                    "INSERT INTO memory_items (id,tenant_id,principal_id,content,content_hash,"
                    "kind,visibility,review_status,memory_confidence,source_trust,importance,"
                    "source_type,created_at,valid_from) VALUES (:id,:tid,:pid,:content,:hash,"
                    "'fact','tenant','proposed',.95,.8,.5,'manual',:created,:created)"
                ),
                {
                    "id": victim_item,
                    "tid": tenant_b,
                    "pid": principal_b,
                    "content": f"{tag}:victim",
                    "hash": f"sha256:{victim_item.hex}",
                    "created": old,
                },
            )

        # Tenant A's app-role session enqueues + runs a promotion.evaluate job
        # forging tenant B's item id, routed under tenant A's RLS context.
        async with app_factory() as session:
            await apply_rls_context(session, tenant_id=str(tenant_a), principal_id=str(principal_a))
            await enqueue_promotion_evaluation(
                session,
                tenant_id=tenant_a,
                memory_item_id=victim_item,
                trigger_type=TRIGGER_MANUAL,
                trigger_id="forged-cross-tenant",
            )
            await session.commit()

        processed = await process_one_job(
            worker_id="rls-test",
            session_factory=owner_factory,
            app_session_factory=app_factory,
            job_types=["promotion.evaluate"],
        )
        assert processed is True

        async with owner_factory() as session:
            job = (
                await session.execute(
                    text("SELECT status FROM jobs WHERE tenant_id = :tid"), {"tid": str(tenant_a)}
                )
            ).mappings().one()
            victim = (
                await session.execute(
                    text("SELECT review_status FROM memory_items WHERE id = :id"),
                    {"id": victim_item},
                )
            ).mappings().one()
        # Safe no-op: succeeded (not an error/retry loop), and tenant B's item
        # is untouched.
        assert job["status"] == "succeeded"
        assert victim["review_status"] == "proposed"
    finally:
        async with owner.begin() as conn:
            await conn.execute(text("DELETE FROM jobs WHERE tenant_id IN (:a,:b)"), {
                "a": str(tenant_a), "b": str(tenant_b)
            })
            await conn.execute(text("DELETE FROM memory_items WHERE tenant_id IN (:a,:b)"), {
                "a": str(tenant_a), "b": str(tenant_b)
            })
            await conn.execute(text("DELETE FROM principals WHERE tenant_id IN (:a,:b)"), {
                "a": str(tenant_a), "b": str(tenant_b)
            })
            await conn.execute(text("DELETE FROM tenant_config WHERE tenant_id IN (:a,:b)"), {
                "a": str(tenant_a), "b": str(tenant_b)
            })
            await conn.execute(text("DELETE FROM tenants WHERE id IN (:a,:b)"), {
                "a": str(tenant_a), "b": str(tenant_b)
            })
        await owner.dispose()
        await app.dispose()


# ===========================================================================
# 10. Worker retry/dead-letter proof for an invalid canonical envelope
#     (correction pass: parser must be self-validating, not merely tolerant)
# ===========================================================================


async def test_mismatched_dedupe_key_envelope_fails_closed_through_retry_path():
    """A generic queue producer could insert a promotion.evaluate payload with
    correct identity fields but a self-consistent, non-canonical dedupe_key
    (never going through enqueue_promotion_evaluation at all). The worker
    must not trust that stored key: the job fails and retries (attempts
    incremented, not silently succeeded), the target item is untouched, and
    no audit mutation is ever emitted."""
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="mismatched dedupe key target",
        memory_confidence=0.9,
        created_at=FIXED_NOW - timedelta(hours=200),
    )
    bad_payload = {
        "contract_version": PROMOTION_EVALUATE_CONTRACT_VERSION,
        "memory_item_id": item_id,
        "trigger_type": TRIGGER_MANUAL,
        "trigger_id": "trigger-1",
        "requested_policy_version": "promotion-legacy-v1",
        "ingest_id": None,
        "correlation_id": None,
        # Same item, same trigger_type, but a different trigger_id than the
        # payload actually declares — structurally plausible, not a prefix
        # mismatch, and never produced by enqueue_promotion_evaluation.
        "dedupe_key": f"promotion.evaluate:{item_id}:manual:a-different-trigger",
    }
    async with _test_session_factory() as session:
        await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
        await enqueue_job(
            session, tenant_id=tenant_id, job_type="promotion.evaluate", payload=bad_payload
        )
        await session.commit()

    await process_one_job(
        worker_id="test",
        session_factory=_test_session_factory,
        app_session_factory=_test_session_factory,
        job_types=["promotion.evaluate"],
    )

    async with _test_session_factory() as session:
        job = (
            await session.execute(
                text("SELECT status, attempts FROM jobs WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
        ).mappings().one()
    assert job["status"] == STATUS_PENDING
    assert job["attempts"] == 1

    item = await _fetch_item(item_id)
    assert item["review_status"] == "proposed"
    assert await _events_for(item_id) == []


async def test_unknown_field_envelope_fails_closed_through_retry_path():
    """A payload with an otherwise-valid v1 shape plus one unrecognized field
    (enqueue-time decision state that must never belong in this contract)
    must fail the exact same way — retried, not silently accepted with the
    extra field ignored."""
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="unknown field target",
        memory_confidence=0.9,
        created_at=FIXED_NOW - timedelta(hours=200),
    )
    bad_payload = {
        "contract_version": PROMOTION_EVALUATE_CONTRACT_VERSION,
        "memory_item_id": item_id,
        "trigger_type": TRIGGER_MANUAL,
        "trigger_id": "trigger-1",
        "requested_policy_version": "promotion-legacy-v1",
        "ingest_id": None,
        "correlation_id": None,
        "dedupe_key": promotion_evaluate_dedupe_key(
            uuid.UUID(item_id), TRIGGER_MANUAL, "trigger-1"
        ),
        "retention_confidence": 0.99,
    }
    async with _test_session_factory() as session:
        await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
        await enqueue_job(
            session, tenant_id=tenant_id, job_type="promotion.evaluate", payload=bad_payload
        )
        await session.commit()

    await process_one_job(
        worker_id="test",
        session_factory=_test_session_factory,
        app_session_factory=_test_session_factory,
        job_types=["promotion.evaluate"],
    )

    async with _test_session_factory() as session:
        job = (
            await session.execute(
                text("SELECT status, attempts FROM jobs WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
        ).mappings().one()
    assert job["status"] == STATUS_PENDING
    assert job["attempts"] == 1

    item = await _fetch_item(item_id)
    assert item["review_status"] == "proposed"
    assert await _events_for(item_id) == []


# ===========================================================================
# 11. Canonical execution-authority proof (missing / corrupt v2 authority)
# ===========================================================================


async def _insert_v2_ingest(
    tenant_id: str,
    principal_id: str,
    *,
    with_execution: bool,
    execution_context_version: str = "memory-context-v2",
    memory_profile_id: str | None = None,
    memory_profile_revision_id: str | None = None,
) -> str:
    """Insert a memory-context-v2 candidate_ingests row, optionally with its
    durable candidate_ingest_executions row -- mirroring the direct-SQL
    execution-context fixture pattern used by
    tests/test_worker_audit_provenance_postgres.py, without a full
    /v1/remember round trip."""
    ingest_id = str(uuid.uuid4())
    async with _test_session_factory() as session:
        await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
        await session.execute(
            text(
                "INSERT INTO candidate_ingests (id, tenant_id, principal_id, "
                "source_type, content_hash, memory_context_version) "
                "VALUES (:id, :tid, :pid, 'manual', :hash, 'memory-context-v2')"
            ),
            {
                "id": ingest_id,
                "tid": tenant_id,
                "pid": principal_id,
                "hash": f"sha256:{uuid.uuid4().hex}",
            },
        )
        if with_execution:
            await session.execute(
                text(
                    "INSERT INTO candidate_ingest_executions (ingest_id, tenant_id, "
                    "memory_profile_id, memory_profile_revision_id, memory_context_version) "
                    "VALUES (:iid, :tid, :profile_id, :revision_id, :ctx)"
                ),
                {
                    "iid": ingest_id,
                    "tid": tenant_id,
                    "profile_id": memory_profile_id,
                    "revision_id": memory_profile_revision_id,
                    "ctx": execution_context_version,
                },
            )
        await session.commit()
    return ingest_id


async def test_missing_v2_execution_authority_fails_closed_not_broader_fallback():
    """A memory-context-v2 candidate ingest whose durable execution row is
    absent must never let evaluation proceed under broader fallback/origin
    authority. Even though the target is otherwise promotable (legacy lane
    qualifies), it must remain proposed, no promotion event may be emitted,
    and the job must not be marked succeeded -- it goes through the ordinary
    retry/dead-letter path. This is deliberately unlike legacy
    promotion.path_a's pre-025 carve-out: for canonical promotion.evaluate,
    unreconstructable execution authority is an operational failure that
    must stay visible, not a silent no-op."""
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="missing execution authority target",
        memory_confidence=0.9,
        created_at=FIXED_NOW - timedelta(hours=200),
    )
    ingest_id = await _insert_v2_ingest(tenant_id, principal_id, with_execution=False)

    async with _test_session_factory() as session:
        await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
        await enqueue_promotion_evaluation(
            session,
            tenant_id=tenant_id,
            memory_item_id=uuid.UUID(item_id),
            trigger_type=TRIGGER_MANUAL,
            trigger_id="missing-authority",
            ingest_id=uuid.UUID(ingest_id),
        )
        await session.commit()

    await process_one_job(
        worker_id="test",
        session_factory=_test_session_factory,
        app_session_factory=_test_session_factory,
        job_types=["promotion.evaluate"],
    )

    item = await _fetch_item(item_id)
    assert item["review_status"] == "proposed"
    events = await _events_for(item_id)
    assert not any(e["event_type"] in ("review_change", "conflict_resolution") for e in events)

    async with _test_session_factory() as session:
        job = (
            await session.execute(
                text("SELECT status, attempts FROM jobs WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
        ).mappings().one()
    assert job["status"] == STATUS_PENDING
    assert job["attempts"] == 1


async def test_corrupt_v2_execution_authority_fails_closed():
    """An execution row that is present but carries an unsupported/corrupt
    context version (memory_context_from_ingest raises) must fail exactly
    like a missing row: no evaluation-authorized mutation, ordinary worker
    failure semantics."""
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="corrupt execution authority target",
        memory_confidence=0.9,
        created_at=FIXED_NOW - timedelta(hours=200),
    )
    ingest_id = await _insert_v2_ingest(
        tenant_id,
        principal_id,
        with_execution=True,
        execution_context_version="unsupported-v99",
    )

    async with _test_session_factory() as session:
        await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
        await enqueue_promotion_evaluation(
            session,
            tenant_id=tenant_id,
            memory_item_id=uuid.UUID(item_id),
            trigger_type=TRIGGER_MANUAL,
            trigger_id="corrupt-authority",
            ingest_id=uuid.UUID(ingest_id),
        )
        await session.commit()

    await process_one_job(
        worker_id="test",
        session_factory=_test_session_factory,
        app_session_factory=_test_session_factory,
        job_types=["promotion.evaluate"],
    )

    item = await _fetch_item(item_id)
    assert item["review_status"] == "proposed"
    events = await _events_for(item_id)
    assert not any(e["event_type"] in ("review_change", "conflict_resolution") for e in events)

    async with _test_session_factory() as session:
        job = (
            await session.execute(
                text("SELECT status, attempts FROM jobs WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
        ).mappings().one()
    assert job["status"] == STATUS_PENDING
    assert job["attempts"] == 1


# ===========================================================================
# 12. Profile-bound write eligibility governs canonical evaluation
# ===========================================================================


async def _insert_profile_revision(
    tenant_id: str,
    *,
    slug: str,
    include_private: bool = True,
    include_tenant: bool = False,
    include_public: bool = False,
    allow_tenant_write: bool = False,
    allow_public_write: bool = False,
) -> tuple[str, str]:
    """Insert one memory_profiles + memory_profile_revisions row directly,
    mirroring the profile-authorization fixture shape used elsewhere (see
    tests/test_profile_authorization_regressions_postgres.py) without the
    full HTTP API round trip. Returns ``(profile_id, revision_id)``."""
    profile_id = str(uuid.uuid4())
    revision_id = str(uuid.uuid4())
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO memory_profiles (id, tenant_id, name, slug) "
                "VALUES (:id, :tid, :slug, :slug)"
            ),
            {"id": profile_id, "tid": tenant_id, "slug": slug},
        )
        await session.execute(
            text(
                "INSERT INTO memory_profile_revisions (id, tenant_id, profile_id, version, "
                "include_private, include_tenant, include_public, allow_tenant_write, "
                "allow_public_write, default_write_visibility, reason) "
                "VALUES (:id, :tid, :pid, 1, :inc_priv, :inc_ten, :inc_pub, :atw, :apw, "
                "'private', 'promotion-evaluate profile-bound proof')"
            ),
            {
                "id": revision_id,
                "tid": tenant_id,
                "pid": profile_id,
                "inc_priv": include_private,
                "inc_ten": include_tenant,
                "inc_pub": include_public,
                "atw": allow_tenant_write,
                "apw": allow_public_write,
            },
        )
        await session.commit()
    return profile_id, revision_id


async def test_profile_bound_write_ineligible_item_stays_proposed_as_legitimate_noop():
    """A restrictive profile (tenant-visibility readable but NOT writable)
    carried as the job's execution authority must keep an otherwise-eligible
    proposed item out of promotion. The handler runs under the correct
    tenant, the execution context reconstructs successfully, but the target
    is outside write eligibility -- so it must stay proposed with no
    promotion audit event, and the job itself completes as a legitimate
    no-op (not a failure)."""
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    profile_id, revision_id = await _insert_profile_revision(
        tenant_id,
        slug=f"restrictive-{uuid.uuid4().hex[:10]}",
        include_private=True,
        include_tenant=True,
        allow_tenant_write=False,
    )
    ingest_id = await _insert_v2_ingest(
        tenant_id,
        principal_id,
        with_execution=True,
        memory_profile_id=profile_id,
        memory_profile_revision_id=revision_id,
    )

    # Tenant-visibility item: readable under this profile (include_tenant),
    # otherwise promotable (legacy lane), but NOT writable
    # (allow_tenant_write=False) -- write_eligibility_expression must
    # exclude it from the scan entirely.
    blocked_item = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="profile write-ineligible target",
        memory_confidence=0.9,
        created_at=FIXED_NOW - timedelta(hours=200),
        visibility="tenant",
    )

    async with _test_session_factory() as session:
        await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
        await enqueue_promotion_evaluation(
            session,
            tenant_id=tenant_id,
            memory_item_id=uuid.UUID(blocked_item),
            trigger_type=TRIGGER_MANUAL,
            trigger_id="profile-write-ineligible",
            ingest_id=uuid.UUID(ingest_id),
        )
        await session.commit()

    processed = await process_one_job(
        worker_id="test",
        session_factory=_test_session_factory,
        app_session_factory=_test_session_factory,
        job_types=["promotion.evaluate"],
    )
    assert processed is True

    item = await _fetch_item(blocked_item)
    assert item["review_status"] == "proposed"
    events = await _events_for(blocked_item)
    assert not any(e["event_type"] == "review_change" for e in events)

    async with _test_session_factory() as session:
        job = (
            await session.execute(
                text(
                    "SELECT status FROM jobs WHERE tenant_id = :tid "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"tid": tenant_id},
            )
        ).mappings().one()
    # A legitimate no-op job succeeds -- it is not treated as a failure.
    assert job["status"] == "succeeded"


async def test_profile_bound_write_eligible_item_promotes_under_same_profile():
    """Companion to the restrictive case above, under the exact same
    profile: a private item owned by the execution principal is always
    write-eligible (profile_write_scope_expression permits private
    visibility unconditionally), so it promotes -- proving
    write_eligibility_expression narrows rather than blanket-blocking all
    mutation for a profile-bound canonical evaluation."""
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    profile_id, revision_id = await _insert_profile_revision(
        tenant_id,
        slug=f"restrictive-{uuid.uuid4().hex[:10]}",
        include_private=True,
        include_tenant=True,
        allow_tenant_write=False,
    )
    ingest_id = await _insert_v2_ingest(
        tenant_id,
        principal_id,
        with_execution=True,
        memory_profile_id=profile_id,
        memory_profile_revision_id=revision_id,
    )

    allowed_item = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="profile write-eligible target",
        memory_confidence=0.9,
        created_at=FIXED_NOW - timedelta(hours=200),
        visibility="private",
    )

    async with _test_session_factory() as session:
        await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
        await enqueue_promotion_evaluation(
            session,
            tenant_id=tenant_id,
            memory_item_id=uuid.UUID(allowed_item),
            trigger_type=TRIGGER_MANUAL,
            trigger_id="profile-write-eligible",
            ingest_id=uuid.UUID(ingest_id),
        )
        await session.commit()

    await process_one_job(
        worker_id="test",
        session_factory=_test_session_factory,
        app_session_factory=_test_session_factory,
        job_types=["promotion.evaluate"],
    )

    item = await _fetch_item(allowed_item)
    assert item["review_status"] == "active"
    events = await _events_for(allowed_item)
    assert any(e["event_type"] == "review_change" for e in events)
