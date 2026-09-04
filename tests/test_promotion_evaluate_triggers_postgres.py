"""Real-PostgreSQL coverage for the item-scoped ``promotion.evaluate`` trigger
producers (issue #155, ENG-PROMOTION-003B3).

The v1 job contract and its handler were introduced in
tests/test_promotion_evaluate_postgres.py. This file proves the *producers*:
which committed lifecycle events enqueue a canonical evaluation, with which
trigger identity, at which due boundary, and behind which gates — plus the two
end-to-end proofs that an unblocking event (external noise feedback lifted,
conflict resolved) actually promotes the item through the enqueued job with the
trigger recorded in the audit trail.

Skips automatically when no DB is reachable (mirroring
tests/test_promotion_evaluate_postgres.py).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.api.app import create_app
from engram.config import settings
from engram.db import _DEFAULT_PRINCIPAL_NAME, _DEFAULT_TENANT_SLUG, apply_rls_context, get_session
from engram.jobs import STATUS_PENDING
from engram.promotion import (
    PROMOTION_EVALUATE_CONTRACT_VERSION,
    PROMOTION_EVALUATE_CONTRACT_VERSION_V2,
    TRIGGER_CONFLICT_CHANGED,
    TRIGGER_FEEDBACK,
    TRIGGER_ITEM_CREATED,
    TRIGGER_MANUAL,
    TRIGGER_REVIEW_CHANGED,
    maybe_enqueue_promotion_evaluation,
    parse_promotion_evaluate_payload,
)
from engram.promotion_policy import LEGACY_PROMOTION_POLICY_VERSION
from engram.worker import process_one_job

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
        await conn.execute(text("DELETE FROM feedback_events"))
        await conn.execute(text("DELETE FROM recall_logs"))
        await conn.execute(text("DELETE FROM classification_runs"))
        await conn.execute(text("DELETE FROM memory_items"))
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


async def _insert_extra_principal(tenant_id: str, name: str = "external-agent") -> str:
    principal_id = str(uuid.uuid4())
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES (:id, :tid, :name, 'agent')"
            ),
            {"id": principal_id, "tid": tenant_id, "name": name},
        )
        await session.commit()
    return principal_id


@asynccontextmanager
async def _rls_session(tenant_id: str, principal_id: str):
    async with _test_session_factory() as session:
        await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
        yield session


async def _insert_item(
    *,
    tenant_id: str,
    principal_id: str,
    content: str,
    memory_confidence: float = 0.5,
    created_at: datetime | None = None,
    kind: str = "fact",
    source_type: str = "manual",
    review_status: str = "proposed",
    conflict_resolution_status: str | None = None,
    conflicts_with_item_id: str | None = None,
    valid_to: datetime | None = None,
    superseded_by: str | None = None,
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
                "source_confidence_prior, importance, source_type, created_at, "
                "valid_from, conflict_resolution_status, conflicts_with_item_id, "
                "valid_to, superseded_by"
                ") VALUES ("
                ":id, :tenant_id, :principal_id, :content, :content_hash, :kind, "
                ":visibility, :review_status, :memory_confidence, 0.5, "
                ":source_confidence_prior, 0.5, :source_type, :created_at, "
                ":created_at, :conflict_resolution_status, :conflicts_with_item_id, "
                ":valid_to, :superseded_by"
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
                "source_confidence_prior": memory_confidence,
                "created_at": created_at,
                "conflict_resolution_status": conflict_resolution_status,
                "conflicts_with_item_id": conflicts_with_item_id,
                "valid_to": valid_to,
                "superseded_by": superseded_by,
            },
        )
        await session.commit()
    return item_id


async def _item_row(item_id: str) -> dict[str, Any]:
    async with _test_session_factory() as session:
        return (
            (
                await session.execute(
                    text("SELECT * FROM memory_items WHERE id = :id"), {"id": item_id}
                )
            )
            .mappings()
            .one()
        )


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


async def _promotion_events_for(item_id: str) -> list[dict[str, Any]]:
    async with _test_session_factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT event_type, new_value, reason FROM item_events "
                        "WHERE item_id = :id AND event_type = 'review_change' "
                        "ORDER BY created_at, id"
                    ),
                    {"id": item_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def _jobs_for_tenant(
    tenant_id: str, job_type: str = "promotion.evaluate"
) -> list[dict[str, Any]]:
    async with _test_session_factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT id, job_type, status, run_after, payload "
                        "FROM jobs WHERE tenant_id = :tid AND job_type = :jt "
                        "ORDER BY created_at"
                    ),
                    {"tid": tenant_id, "jt": job_type},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def _feedback(item_id: str, verdict: str, *, client: AsyncClient) -> dict[str, Any]:
    response = await client.post(
        "/v1/feedback", json={"item_id": item_id, "feedback": verdict}
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


async def _process_one() -> bool:
    return (
        await process_one_job(
            worker_id="trigger-test",
            session_factory=_test_session_factory,
            app_session_factory=_test_session_factory,
        )
    )


# ---------------------------------------------------------------------------
# App/client fixtures for route-level producers (pattern: tests/test_remember.py)
# ---------------------------------------------------------------------------


async def _get_test_session() -> AsyncSession:
    async with _test_session_factory() as session:
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


# ===========================================================================
# 1. Feedback producer (route level — feedback effects stay promotion-blind;
#    only the committed transition schedules reevaluation)
# ===========================================================================


async def test_feedback_transition_enqueues_promotion_evaluate(monkeypatch, client):
    monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", True)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="feedback trigger target"
    )
    result = await _feedback(item_id, "useful", client=client)
    assert result["status"] == "recorded"
    jobs = await _jobs_for_tenant(tenant_id)
    assert len(jobs) == 1
    payload = parse_promotion_evaluate_payload(jobs[0]["payload"])
    assert payload.trigger_type == TRIGGER_FEEDBACK
    assert payload.trigger_id == str(result["feedback_event_id"])
    assert payload.contract_version == PROMOTION_EVALUATE_CONTRACT_VERSION
    assert payload.memory_item_id == uuid.UUID(item_id)
    assert jobs[0]["status"] == STATUS_PENDING
    # Event-driven triggers evaluate immediately: the committed event itself is
    # the reevaluation reason, not a cooling boundary.
    assert jobs[0]["run_after"] <= datetime.now(UTC) + timedelta(seconds=5)


async def test_feedback_producer_noop_when_flag_disabled(monkeypatch, client):
    monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", False)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="flag off feedback"
    )
    await _feedback(item_id, "useful", client=client)
    assert await _jobs_for_tenant(tenant_id) == []


async def test_feedback_producer_skips_non_proposed_item(monkeypatch, client):
    monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", True)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="already active",
        review_status="active",
    )
    await _feedback(item_id, "useful", client=client)
    assert await _jobs_for_tenant(tenant_id) == []


async def test_feedback_producer_skips_unchanged_verdict(monkeypatch, client):
    """Re-delivering the same verdict is status=unchanged: no FeedbackEvent
    transition, so no new evaluation job."""
    monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", True)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="unchanged verdict"
    )
    await _feedback(item_id, "useful", client=client)
    repeat = await _feedback(item_id, "useful", client=client)
    assert repeat["status"] == "unchanged"
    assert len(await _jobs_for_tenant(tenant_id)) == 1


async def test_feedback_replacement_enqueues_distinct_evaluation(monkeypatch, client):
    """noise -> useful is a new committed transition: a second, independently
    deduped evaluation whose trigger_id is the *new* feedback event id."""
    monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", True)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="verdict replacement"
    )
    await _feedback(item_id, "noise", client=client)
    replaced = await _feedback(item_id, "useful", client=client)
    assert replaced["status"] == "updated"
    jobs = await _jobs_for_tenant(tenant_id)
    assert len(jobs) == 2
    assert jobs[1]["payload"]["trigger_id"] == str(replaced["feedback_event_id"])
    assert jobs[0]["id"] != jobs[1]["id"]


async def test_feedback_producer_skips_when_promotion_disabled(monkeypatch, client):
    monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", True)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    async with _test_engine.begin() as conn:
        await conn.execute(
            text("UPDATE tenant_config SET auto_promote_enabled = FALSE WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        )
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="promotion disabled"
    )
    await _feedback(item_id, "useful", client=client)
    assert await _jobs_for_tenant(tenant_id) == []


# ===========================================================================
# 2. Gate helper: live-proposal semantics
# ===========================================================================


async def test_maybe_enqueue_skips_expired_or_superseded_items(monkeypatch):
    monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", True)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    expired_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="expired",
        valid_to=FIXED_NOW - timedelta(hours=1),
    )
    replacement_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="replacement for superseded",
        review_status="active",
    )
    superseded_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="superseded",
        superseded_by=replacement_id,
    )
    async with _rls_session(tenant_id, principal_id) as session:
        for item_id in (expired_id, superseded_id):
            row = await _item_row(item_id)
            job_id = await maybe_enqueue_promotion_evaluation(
                session,
                tenant_id=tenant_id,
                item=row,
                trigger_type=TRIGGER_MANUAL,
                trigger_id="gate-check",
            )
            assert job_id is None
        await session.commit()
    assert await _jobs_for_tenant(tenant_id) == []


# ===========================================================================
# 3. item_created producer (remember route)
# ===========================================================================


async def test_remember_proposed_item_enqueues_evaluation_at_exact_cooling_boundary(
    monkeypatch, client
):
    monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", True)
    if not await _db_ok():
        _require_db()
    tenant_id, _ = await _default_tenant_principal()
    before = datetime.now(UTC).replace(microsecond=0)
    response = await client.post(
        "/v1/remember",
        json={"content": "Turn summary needing admission", "source_type": "sync_turn"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["review_status"] == "proposed"
    item_id = body["id"]
    row = await _item_row(item_id)
    jobs = await _jobs_for_tenant(tenant_id)
    assert len(jobs) == 1
    payload = parse_promotion_evaluate_payload(jobs[0]["payload"])
    assert payload.trigger_type == TRIGGER_ITEM_CREATED
    assert payload.trigger_id == str(body["ingest_id"])
    # The item_created lane schedules at the legacy cooling boundary, so its
    # audit provenance reports the legacy requested policy version.
    assert payload.requested_policy_version == LEGACY_PROMOTION_POLICY_VERSION
    # Exact-boundary scheduling for the time-dependent assessment: the job is
    # due precisely at the legacy cooling boundary, not "immediately" and not
    # at an arbitrary delay.
    expected = row["created_at"] + timedelta(hours=72)
    assert jobs[0]["run_after"] == expected
    assert jobs[0]["run_after"] >= before + timedelta(hours=71)


async def test_remember_active_write_enqueues_nothing(monkeypatch, client):
    """A trusted direct write is active from birth — not a promotion
    candidate, so no item_created evaluation exists."""
    monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", True)
    if not await _db_ok():
        _require_db()
    tenant_id, _ = await _default_tenant_principal()
    response = await client.post(
        "/v1/remember", json={"content": "Direct explicit user fact"}
    )
    assert response.status_code == 201, response.text
    assert response.json()["review_status"] == "active"
    assert await _jobs_for_tenant(tenant_id) == []


async def test_remember_item_created_noop_when_flag_disabled(monkeypatch, client):
    monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", False)
    if not await _db_ok():
        _require_db()
    tenant_id, _ = await _default_tenant_principal()
    response = await client.post(
        "/v1/remember",
        json={"content": "Flag off turn summary", "source_type": "sync_turn"},
    )
    assert response.status_code == 201, response.text
    assert await _jobs_for_tenant(tenant_id) == []


async def test_supersede_enqueues_item_created_for_replacement_proposal(
    monkeypatch, client
):
    """A supersession's replacement is a new item creation (the
    dedup-material-update path): a proposed replacement restarts its cooling
    window and gets the boundary evaluation; an active replacement does not."""
    monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", True)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    proposed = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="supersede me"
    )
    before = datetime.now(UTC)
    response = await client.post(f"/v1/items/{proposed}/supersede")
    assert response.status_code == 200, response.text
    replacement_id = response.json()["new_item"]["id"]
    row = await _item_row(replacement_id)
    assert row["review_status"] == "proposed"
    jobs = await _jobs_for_tenant(tenant_id)
    assert len(jobs) == 1
    payload = parse_promotion_evaluate_payload(jobs[0]["payload"])
    assert payload.trigger_type == TRIGGER_ITEM_CREATED
    assert payload.memory_item_id == uuid.UUID(replacement_id)
    # The supersede producer intentionally uses the legacy cooling boundary,
    # so its requested-policy provenance must be the legacy version — the
    # same as /v1/remember's item_created producer — not the evidence-policy
    # default.
    assert payload.requested_policy_version == LEGACY_PROMOTION_POLICY_VERSION
    # The replacement's cooling window restarts at its own creation time.
    assert jobs[0]["run_after"] == row["created_at"] + timedelta(hours=72)
    assert jobs[0]["run_after"] >= before + timedelta(hours=71)
    # The expired original is not a candidate and gets nothing.
    assert payload.memory_item_id != uuid.UUID(proposed)


# ===========================================================================
# 4. conflict_changed producer (resolve-conflict route)
# ===========================================================================


async def _conflicted_proposed_item(tenant_id: str, principal_id: str) -> tuple[str, str]:
    counterpart = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="conflict counterpart",
        review_status="active",
        memory_confidence=0.9,
    )
    target = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="conflicted promotion target",
        memory_confidence=0.9,
        conflict_resolution_status="unresolved",
        conflicts_with_item_id=counterpart,
    )
    return target, counterpart


async def test_resolve_conflict_enqueues_evaluation_and_promotes(monkeypatch, client):
    monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", True)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    target, _counterpart = await _conflicted_proposed_item(tenant_id, principal_id)
    response = await client.post(
        f"/v1/items/{target}/resolve-conflict", json={"resolution": "accepted"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "resolved"
    jobs = await _jobs_for_tenant(tenant_id)
    assert len(jobs) == 1
    payload = parse_promotion_evaluate_payload(jobs[0]["payload"])
    assert payload.trigger_type == TRIGGER_CONFLICT_CHANGED
    # trigger_id is the committed conflict_resolution event's id.
    async with _test_session_factory() as session:
        event_id = (
            await session.execute(
                text(
                    "SELECT id::text FROM item_events WHERE item_id = :id "
                    "AND event_type = 'conflict_resolution'"
                ),
                {"id": target},
            )
        ).scalar_one_or_none()
    assert event_id is not None
    assert payload.trigger_id == event_id
    assert jobs[0]["run_after"] <= datetime.now(UTC) + timedelta(seconds=5)
    # The enqueued job is the one that admits the now-unblocked item.
    assert await _process_one()
    item = await _fetch_item(target)
    assert item["review_status"] == "active"
    events = await _promotion_events_for(target)
    promotion = [e for e in events if e["new_value"] == "active"]
    assert len(promotion) == 1
    reason = json.loads(promotion[0]["reason"])
    assert reason["trigger_type"] == TRIGGER_CONFLICT_CHANGED
    assert reason["trigger_id"] == payload.trigger_id


# ===========================================================================
# 5. review_changed producer (verify route)
# ===========================================================================


async def test_verify_on_proposed_item_enqueues_evaluation(monkeypatch, client):
    monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", True)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="verify trigger target",
        memory_confidence=0.5,
    )
    response = await client.post(f"/v1/items/{item_id}/verify")
    assert response.status_code == 200, response.text
    jobs = await _jobs_for_tenant(tenant_id)
    assert len(jobs) == 1
    payload = parse_promotion_evaluate_payload(jobs[0]["payload"])
    assert payload.trigger_type == TRIGGER_REVIEW_CHANGED
    # The job runs and is a legitimate no-op (confidence below threshold) —
    # verification today refreshes diagnostics, it is not admission evidence.
    assert await _process_one()
    item = await _fetch_item(item_id)
    assert item["review_status"] == "proposed"
    # Re-verifying by the same principal is a NOOP (no second event), so it
    # must not enqueue a second, semantically equivalent evaluation.
    repeat = await client.post(f"/v1/items/{item_id}/verify")
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["event"] is None
    assert len(await _jobs_for_tenant(tenant_id)) == 1


async def test_verify_on_active_item_enqueues_nothing(monkeypatch, client):
    monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", True)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="verify active target",
        review_status="active",
    )
    response = await client.post(f"/v1/items/{item_id}/verify")
    assert response.status_code == 200, response.text
    assert await _jobs_for_tenant(tenant_id) == []


# ===========================================================================
# 6. manual producer (admin endpoint)
# ===========================================================================


async def test_admin_evaluate_enqueues_manual_job(monkeypatch, client):
    monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", True)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="manual evaluate target",
        memory_confidence=0.9,
    )
    response = await client.post(f"/v1/admin/items/{item_id}/evaluate")
    assert response.status_code == 200, response.text
    body = response.json()
    jobs = await _jobs_for_tenant(tenant_id)
    assert len(jobs) == 1
    assert str(jobs[0]["id"]) == body["job_id"]
    payload = parse_promotion_evaluate_payload(jobs[0]["payload"])
    assert payload.trigger_type == TRIGGER_MANUAL
    # Unprofiled compatibility: no boundary to preserve, so the payload stays
    # v1 with no execution-authority reference and no authority row exists.
    assert payload.contract_version == PROMOTION_EVALUATE_CONTRACT_VERSION
    assert payload.execution_context_id is None
    assert await _execution_context_rows(tenant_id) == []
    assert await _process_one()
    assert (await _fetch_item(item_id))["review_status"] == "active"


async def test_admin_evaluate_rejects_non_proposed_item(monkeypatch, client):
    monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", True)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="already active manual target",
        review_status="active",
    )
    response = await client.post(f"/v1/admin/items/{item_id}/evaluate")
    assert response.status_code == 409
    assert await _jobs_for_tenant(tenant_id) == []


async def test_admin_evaluate_rejects_unknown_item(monkeypatch, client):
    monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", True)
    if not await _db_ok():
        _require_db()
    response = await client.post(f"/v1/admin/items/{uuid.uuid4()}/evaluate")
    assert response.status_code == 404


async def test_admin_evaluate_noop_when_flag_disabled(monkeypatch, client):
    monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", False)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content="flag off manual target"
    )
    response = await client.post(f"/v1/admin/items/{item_id}/evaluate")
    assert response.status_code == 200, response.text
    assert await _jobs_for_tenant(tenant_id) == []


# ===========================================================================
# 6b. manual producer under a profile-bound admin key — the authorization
#     boundary must hold at request time AND survive the queue delay
#     (correction for the review finding: ADMIN_SCOPE is not a bypass of a
#     bound memory profile)
# ===========================================================================


@dataclass
class _BoundKeyAuth:
    headers: dict[str, str]
    api_key_id: str
    profile_id: str
    revision_id: str


@asynccontextmanager
async def _profile_bound_admin_key(
    client: AsyncClient, *, allow_tenant_write: bool
) -> AsyncIterator[_BoundKeyAuth]:
    """Create an admin-scope API key bound to a memory profile (tenant
    readable, tenant write per ``allow_tenant_write``), over the auth-disabled
    admin API, then enable auth for the caller's Bearer requests — the
    fixture pattern of tests/test_profile_authorization_regressions_postgres.py.

    The key's principal is the default admin, so the target items below are
    principal-eligible and the profile read/write intersection is the only
    variable under test."""
    from engram.auth import reset_principal_cache
    from engram.db import owner_session_factory

    settings.auth_enabled = False
    reset_principal_cache()
    slug = f"manual-eval-{uuid.uuid4().hex[:10]}"
    tenant_id, principal_id = await _default_tenant_principal()
    profile_id: str | None = None
    key_id: str | None = None
    try:
        profile_response = await client.post(
            "/v1/memory-profiles",
            json={
                "name": slug,
                "slug": slug,
                "reason": "manual evaluate authorization proof",
                "policy": {
                    "include_private": True,
                    "include_tenant": True,
                    "include_public": False,
                    "allow_tenant_write": allow_tenant_write,
                    "allow_public_write": False,
                },
            },
        )
        assert profile_response.status_code == 201, profile_response.text
        profile_id = profile_response.json()["id"]
        key_response = await client.post(
            "/v1/admin/api-keys",
            json={
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "scopes": ["admin"],
                "label": slug,
                "memory_profile_id": profile_id,
            },
        )
        assert key_response.status_code == 201, key_response.text
        key_id = key_response.json()["id"]
        settings.auth_enabled = True
        reset_principal_cache()
        yield _BoundKeyAuth(
            headers={"Authorization": f"Bearer {key_response.json()['key']}"},
            api_key_id=key_id,
            profile_id=profile_id,
            revision_id=profile_response.json()["active_revision_id"],
        )
    finally:
        settings.auth_enabled = False
        reset_principal_cache()
        if profile_id is not None:
            async with owner_session_factory() as session:
                await session.execute(
                    text("DELETE FROM job_execution_contexts WHERE memory_profile_id = :pid"),
                    {"pid": profile_id},
                )
                # A worker run under this boundary records the pinned revision
                # on its audit events; dropping those rows first keeps the
                # profile delete (revisions cascade) FK-clean.
                await session.execute(
                    text("DELETE FROM item_events WHERE memory_profile_id = :pid"),
                    {"pid": profile_id},
                )
                if key_id is not None:
                    await session.execute(
                        text("DELETE FROM api_keys WHERE id = :kid"), {"kid": key_id}
                    )
                await session.execute(
                    text("DELETE FROM memory_profiles WHERE id = :pid"), {"pid": profile_id}
                )
                await session.commit()


async def _execution_context_rows(tenant_id: str) -> list[dict[str, Any]]:
    async with _test_session_factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT id::text, api_key_id::text, memory_profile_id::text, "
                        "memory_profile_revision_id::text, memory_context_version "
                        "FROM job_execution_contexts WHERE tenant_id = :tid"
                    ),
                    {"tid": tenant_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def test_manual_evaluate_profile_bound_write_ineligible_404s_and_enqueues_nothing(
    monkeypatch, client
):
    """A profile-bound admin key (tenant readable, NOT tenant writable) must
    not even learn that the write-ineligible proposed item exists: the manual
    endpoint resolves the target through the caller's existing-item write
    eligibility, so the response is the mutation API's non-disclosing 404 —
    byte-identical to an unknown item — and nothing is enqueued."""
    monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", True)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="profile write-ineligible manual target",
        memory_confidence=0.9,
        visibility="tenant",
    )
    async with _profile_bound_admin_key(client, allow_tenant_write=False) as auth:
        denied = await client.post(f"/v1/admin/items/{item_id}/evaluate", headers=auth.headers)
        assert denied.status_code == 404
        unknown = await client.post(
            f"/v1/admin/items/{uuid.uuid4()}/evaluate", headers=auth.headers
        )
        assert unknown.status_code == 404
        # Non-disclosing: the two cases are indistinguishable.
        assert denied.json() == unknown.json() == {"detail": "Item not found"}
        # Gates suppressed (flag off) behaves the same at the boundary and
        # leaves no orphaned execution-authority row behind.
        monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", False)
        suppressed = await client.post(
            f"/v1/admin/items/{item_id}/evaluate", headers=auth.headers
        )
        assert suppressed.status_code == 404
        monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", True)
    assert await _jobs_for_tenant(tenant_id) == []
    assert (await _fetch_item(item_id))["review_status"] == "proposed"
    assert await _promotion_events_for(item_id) == []
    assert await _execution_context_rows(tenant_id) == []


async def test_manual_evaluate_profile_bound_authorized_enqueues_executes_and_promotes(
    monkeypatch, client
):
    """The authorized counterpart: an otherwise comparable profile that DOES
    allow tenant writes enqueues exactly one v2 manual job whose durable
    execution-authority reference reconstructs the same profile boundary at
    worker time (proven by the promotion audit event's provenance columns) —
    never an unrestricted fallback."""
    monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", True)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="profile write-eligible manual target",
        memory_confidence=0.9,
        visibility="tenant",
    )
    async with _profile_bound_admin_key(client, allow_tenant_write=True) as auth:
        response = await client.post(
            f"/v1/admin/items/{item_id}/evaluate", headers=auth.headers
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "enqueued"
        assert body["trigger_type"] == TRIGGER_MANUAL

        jobs = await _jobs_for_tenant(tenant_id)
        assert len(jobs) == 1
        assert str(jobs[0]["id"]) == body["job_id"]
        payload = parse_promotion_evaluate_payload(jobs[0]["payload"])
        assert payload.trigger_type == TRIGGER_MANUAL
        assert payload.contract_version == PROMOTION_EVALUATE_CONTRACT_VERSION_V2
        assert payload.execution_context_id is not None
        assert payload.ingest_id is None
        # The authority row pins exactly the caller's resolved identity.
        contexts = await _execution_context_rows(tenant_id)
        assert len(contexts) == 1
        assert contexts[0]["id"] == str(payload.execution_context_id)
        assert contexts[0]["memory_profile_id"] == auth.profile_id
        assert contexts[0]["memory_profile_revision_id"] == auth.revision_id
        assert contexts[0]["api_key_id"] == auth.api_key_id

        assert await _process_one()
        # Assert on the promotion event BEFORE the fixture cleanup removes
        # the profile-referencing rows (required to keep the profile delete
        # FK-clean).
        async with _test_session_factory() as session:
            event = (
                (
                    await session.execute(
                        text(
                            "SELECT reason, api_key_id::text, memory_profile_id::text, "
                            "memory_profile_revision_id::text, memory_context_version "
                            "FROM item_events WHERE item_id = :id "
                            "AND event_type = 'review_change' AND new_value = 'active'"
                        ),
                        {"id": item_id},
                    )
                )
                .mappings()
                .one()
            )
        reason = json.loads(event["reason"])
        assert reason["trigger_type"] == TRIGGER_MANUAL
        assert reason["trigger_id"] == payload.trigger_id
        # Worker-time provenance is the reconstructed profile boundary itself —
        # not the internal-system fallback an unscoped execution would record.
        assert event["memory_profile_id"] == auth.profile_id
        assert event["memory_profile_revision_id"] == auth.revision_id
        assert event["api_key_id"] == auth.api_key_id
        assert event["memory_context_version"] == "memory-context-v2"
    assert (await _fetch_item(item_id))["review_status"] == "active"


async def test_manual_evaluate_worker_fails_closed_when_authority_row_missing(
    monkeypatch, client
):
    """If the durable execution-authority reference cannot be reconstructed
    (here: the row vanished before execution — under RLS a cross-tenant row is
    exactly this case, invisible), the job must NOT mutate the item, must NOT
    fall back to broader/unprofiled authority (the item would otherwise
    promote), and must enter the ordinary retry semantics."""
    from engram.db import owner_session_factory

    monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", True)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="missing authority manual target",
        memory_confidence=0.9,
        visibility="tenant",
    )
    async with _profile_bound_admin_key(client, allow_tenant_write=True) as auth:
        response = await client.post(
            f"/v1/admin/items/{item_id}/evaluate", headers=auth.headers
        )
        assert response.status_code == 200, response.text
        jobs = await _jobs_for_tenant(tenant_id)
        assert len(jobs) == 1
        payload = parse_promotion_evaluate_payload(jobs[0]["payload"])
        assert payload.execution_context_id is not None
        async with owner_session_factory() as session:
            await session.execute(
                text("DELETE FROM job_execution_contexts WHERE id = :id"),
                {"id": str(payload.execution_context_id)},
            )
            await session.commit()
        assert await _process_one()
    assert (await _fetch_item(item_id))["review_status"] == "proposed"
    assert await _promotion_events_for(item_id) == []
    async with _test_session_factory() as session:
        job = (
            await session.execute(
                text(
                    "SELECT status, attempts FROM jobs WHERE tenant_id = :tid "
                    "AND job_type = 'promotion.evaluate'"
                ),
                {"tid": tenant_id},
            )
        ).mappings().one()
    assert job["status"] == STATUS_PENDING
    assert job["attempts"] == 1


async def test_manual_evaluate_worker_fails_closed_on_unsupported_authority_version(
    monkeypatch, client
):
    """A present-but-corrupt authority row (unsupported context version) fails
    closed exactly like a missing one: no mutation, no broader fallback,
    ordinary failure semantics."""
    from engram.db import owner_session_factory

    monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", True)
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="corrupt authority manual target",
        memory_confidence=0.9,
        visibility="tenant",
    )
    async with _profile_bound_admin_key(client, allow_tenant_write=True) as auth:
        response = await client.post(
            f"/v1/admin/items/{item_id}/evaluate", headers=auth.headers
        )
        assert response.status_code == 200, response.text
        jobs = await _jobs_for_tenant(tenant_id)
        assert len(jobs) == 1
        payload = parse_promotion_evaluate_payload(jobs[0]["payload"])
        async with owner_session_factory() as session:
            await session.execute(
                text(
                    "UPDATE job_execution_contexts SET memory_context_version = 'unsupported-v99' "
                    "WHERE id = :id"
                ),
                {"id": str(payload.execution_context_id)},
            )
            await session.commit()
        assert await _process_one()
    assert (await _fetch_item(item_id))["review_status"] == "proposed"
    assert await _promotion_events_for(item_id) == []
    async with _test_session_factory() as session:
        job = (
            await session.execute(
                text(
                    "SELECT status, attempts FROM jobs WHERE tenant_id = :tid "
                    "AND job_type = 'promotion.evaluate'"
                ),
                {"tid": tenant_id},
            )
        ).mappings().one()
    assert job["status"] == STATUS_PENDING
    assert job["attempts"] == 1


# ===========================================================================
# 7. End-to-end: unblocking events admit through the enqueued job
# ===========================================================================


async def test_external_noise_blocks_then_lifted_feedback_promotes(monkeypatch, client):
    """The full feedback loop: an external noise verdict blocks an otherwise
    admissible proposal; replacing it with useful enqueues the evaluation that
    admits the item, with the feedback trigger recorded in the audit event."""
    monkeypatch.setattr(settings, "promotion_evaluate_jobs_enabled", True)
    if not await _db_ok():
        _require_db()
    tenant_id, admin_id = await _default_tenant_principal()
    author_id = await _insert_extra_principal(tenant_id)
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=author_id,
        content="noise-blocked then lifted",
        memory_confidence=0.9,
        visibility="tenant",
    )
    # External noise (the default admin is not the author) blocks admission.
    noise = await _feedback(item_id, "noise", client=client)
    assert noise["status"] == "recorded"
    async with _rls_session(tenant_id, admin_id) as session:
        from engram.promotion import evaluate_promotion_item_current_state

        result = await evaluate_promotion_item_current_state(
            session,
            tenant_id,
            uuid.UUID(item_id),
            evaluation_context={
                "evaluation_id": str(uuid.uuid4()),
                "trigger_type": TRIGGER_MANUAL,
                "trigger_id": "control-evaluation",
            },
        )
    assert result.promoted == 0
    assert result.skipped_dispute == 1
    assert (await _fetch_item(item_id))["review_status"] == "proposed"
    # Lifting the block: the replacement verdict is the committed event that
    # enqueues the admitting evaluation.
    lifted = await _feedback(item_id, "useful", client=client)
    assert lifted["status"] == "updated"
    assert await _process_one()
    assert (await _fetch_item(item_id))["review_status"] == "active"
    promotion = [
        e
        for e in await _promotion_events_for(item_id)
        if e["new_value"] == "active"
    ]
    assert len(promotion) == 1
    reason = json.loads(promotion[0]["reason"])
    assert reason["trigger_type"] == TRIGGER_FEEDBACK
    # Either feedback job may be the one that ran: both were pending and due,
    # and the handler evaluates current state (the lifted block), not the
    # enqueue-time observation — so the audit trigger_id is whichever feedback
    # event's job was claimed first. Both are legitimate provenance.
    assert reason["trigger_id"] in {
        str(noise["feedback_event_id"]),
        str(lifted["feedback_event_id"]),
    }
    # The remaining feedback job is an idempotent no-op: the item is already
    # active, so replaying the evaluation must not produce a second mutation
    # or audit event.
    assert await _process_one()
    assert (await _fetch_item(item_id))["review_status"] == "active"
    assert len([e for e in await _promotion_events_for(item_id) if e["new_value"] == "active"]) == 1
