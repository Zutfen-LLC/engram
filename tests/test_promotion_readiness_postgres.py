"""Real-PostgreSQL coverage for per-item promotion readiness diagnostics
(ENG-PROMOTION-003A / issue #154).

Exercises ``GET /v1/review/promotion-readiness/{item_id}`` and the underlying
``engram.promotion_readiness`` module against a live PostgreSQL with the v2
schema: every evidence/job/readiness state, exact canonical blocker output,
the required-retention-confidence regression fixture, explicit-unknown
behavior, RLS/negative-scope isolation, and the no-provider-call /
no-mutation guarantee. Skips automatically when no DB is reachable
(mirroring tests/test_promotion.py).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.api.app import create_app
from engram.config import settings
from engram.db import get_session

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
                        "FROM tenants t "
                        "JOIN principals p ON p.tenant_id = t.id AND p.name = 'admin' "
                        "WHERE t.slug = 'default'"
                    ),
                )
            )
            .mappings()
            .one()
        )
    return str(row["tenant_id"]), str(row["principal_id"])


def _make_client(tenant_id: str, principal_id: str) -> AsyncClient:
    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        async with _test_session_factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
            )
            await session.execute(
                text("SELECT set_config('app.principal_id', :pid, true)"),
                {"pid": principal_id},
            )
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = _override_get_session
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def _enable_evidence_lane() -> None:
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "UPDATE tenant_config SET auto_promote_evidence_enabled = TRUE "
                "WHERE tenant_id = (SELECT id FROM tenants WHERE slug = 'default')"
            )
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
) -> str:
    item_id = str(uuid.uuid4())
    if created_at is None:
        created_at = FIXED_NOW - timedelta(hours=100)
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
                "'tenant', :review_status, :memory_confidence, 0.5, "
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
    classification_version: str = "classification-v2",
    retention_policy_version: str = "retention-v1",
    provenance: dict[str, Any] | None = None,
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
                "classification_version, retention_policy_version, created_at, expires_at"
                ") VALUES ("
                ":id, :tenant_id, :principal_id, :item_id, :created_at, :content_hash, "
                "'canonical-v1', :source_type, :kind, :taxonomy_confidence, "
                ":retention_confidence, :retention_disposition, 'test evidence', "
                ":provenance, :classification_version, :retention_policy_version, "
                ":created_at, :expires_at"
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
                "classification_version": classification_version,
                "retention_policy_version": retention_policy_version,
                "provenance": json.dumps(provenance or {}),
                "expires_at": created_at + timedelta(hours=1),
            },
        )
        await session.commit()
    return run_id


async def _insert_job(
    *,
    tenant_id: str,
    item_id: str,
    job_type: str,
    status: str = "pending",
    run_after: datetime,
    dedupe_suffix: str = "",
) -> str:
    job_id = str(uuid.uuid4())
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO jobs (id, tenant_id, job_type, status, run_after, attempts, "
                "max_attempts, payload) VALUES (:id, :tenant_id, :job_type, :status, "
                ":run_after, 0, 5, :payload)"
            ),
            {
                "id": job_id,
                "tenant_id": tenant_id,
                "job_type": job_type,
                "status": status,
                "run_after": run_after,
                "payload": json.dumps(
                    {
                        "memory_item_id": item_id,
                        "dedupe_key": f"{job_type}:{item_id}{dedupe_suffix}",
                    }
                ),
            },
        )
        await session.commit()
    return job_id


async def _fetch_readiness(client: AsyncClient, item_id: str) -> dict[str, Any]:
    resp = await client.get(f"/v1/review/promotion-readiness/{item_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---- readiness states ---------------------------------------------------------


async def test_cooling_item_reports_age_blocker_only():
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="cooling legacy item",
        memory_confidence=0.9,
        created_at=FIXED_NOW - timedelta(hours=1),
    )
    async with _make_client(tenant_id, principal_id) as client:
        body = await _fetch_readiness(client, item_id)
    # Exact shared-evaluator output: the age gate plus the (here irrelevant)
    # evidence-lane blockers — the evidence lane is disabled and this item has
    # no receipt, so those blockers never block the legacy-selected promotion.
    assert body["blockers"] == [
        "age",
        "missing_source_prior",
        "no_retention_evidence",
        "retention_disposition",
        "evidence_disabled",
    ]
    assert body["readiness_state"] == "cooling"
    assert body["selected_basis"] is None
    assert body["legacy_threshold_met"] is True
    assert body["legacy_trust_qualified"] is True
    assert body["legacy_age_qualified"] is False
    assert body["terminal_under_current_policy"] is False
    assert body["can_auto_promote_without_new_evidence_or_review"] is True
    assert body["remaining_cooling_seconds"] is not None
    assert body["remaining_cooling_seconds"] > 0
    assert body["evidence_state"] == "none"
    assert body["conflict_recheck_status"] == "not_run"
    assert "conflict_recheck" not in body["blockers"]


async def test_explicit_kind_backfill_without_receipt_is_missing_evidence():
    """Issue #154 verification item 6: reported as missing evidence, never as
    organically accumulating evidence."""
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="backfilled explicit-kind item",
        kind="fact",
        memory_confidence=0.5,
        source_type="import",
        source_confidence_prior=0.5,
    )
    async with _make_client(tenant_id, principal_id) as client:
        body = await _fetch_readiness(client, item_id)
    assert "no_retention_evidence" in body["blockers"]
    assert "confidence" in body["blockers"]
    assert body["readiness_state"] == "missing_evidence"
    assert body["evidence_state"] == "none"
    assert body["classification_run_id"] is None
    assert body["taxonomy_confidence"] is None
    assert body["retention_confidence"] is None
    assert body["required_retention_status"] == "computable"
    assert body["required_retention_confidence"] == pytest.approx(0.75)
    assert body["terminal_under_current_policy"] is True
    assert body["can_auto_promote_without_new_evidence_or_review"] is False


async def test_below_evidence_threshold_required_retention_fixture():
    """Issue #154 verification item 5: sync_turn prior 0.4 vs threshold 0.70
    requires retention confidence 0.775 under 0.20/0.80 weights."""
    if not await _db_ok():
        _require_db()
    await _enable_evidence_lane()
    tenant_id, principal_id = await _default_tenant_principal()
    evidence_at = FIXED_NOW - timedelta(hours=100)
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="sync turn candidate below threshold",
        source_type="sync_turn",
        memory_confidence=0.4,
        source_confidence_prior=0.4,
        retention_confidence=0.5,
        retention_disposition="retain",
        retention_evidence_at=evidence_at,
    )
    await _insert_bound_evidence(
        item_id, tenant_id=tenant_id, principal_id=principal_id, created_at=evidence_at
    )
    async with _make_client(tenant_id, principal_id) as client:
        body = await _fetch_readiness(client, item_id)
    assert body["evidence_enabled"] is True
    assert body["evidence_score"] == pytest.approx(0.48)
    assert "evidence_score" in body["blockers"]
    assert body["readiness_state"] == "below_evidence_threshold"
    assert body["evidence_state"] == "bound-below-threshold"
    assert body["required_retention_status"] == "computable"
    assert body["required_retention_confidence"] == pytest.approx(0.775)


async def test_malformed_receipt_version_is_malformed_state():
    if not await _db_ok():
        _require_db()
    await _enable_evidence_lane()
    tenant_id, principal_id = await _default_tenant_principal()
    evidence_at = FIXED_NOW - timedelta(hours=100)
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="stale receipt item",
        source_type="sync_turn",
        memory_confidence=0.4,
        source_confidence_prior=0.4,
        retention_confidence=0.9,
        retention_disposition="retain",
        retention_evidence_at=evidence_at,
    )
    await _insert_bound_evidence(
        item_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        created_at=evidence_at,
        classification_version="classification-v1",
    )
    async with _make_client(tenant_id, principal_id) as client:
        body = await _fetch_readiness(client, item_id)
    assert "evidence_version" in body["blockers"]
    assert body["readiness_state"] == "malformed_or_stale_evidence"
    assert body["evidence_state"] == "malformed/stale"
    assert body["evidence_score"] is None


async def test_kind_policy_is_terminal():
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="doctrine kind item",
        kind="doctrine",
        memory_confidence=0.9,
    )
    async with _make_client(tenant_id, principal_id) as client:
        body = await _fetch_readiness(client, item_id)
    assert "kind_policy" in body["blockers"]
    assert body["readiness_state"] == "blocked_by_kind_policy"
    assert body["terminal_under_current_policy"] is True
    assert body["can_auto_promote_without_new_evidence_or_review"] is False


async def test_statically_eligible_awaiting_reconciliation():
    if not await _db_ok():
        _require_db()
    await _enable_evidence_lane()
    tenant_id, principal_id = await _default_tenant_principal()
    evidence_at = FIXED_NOW - timedelta(hours=100)
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="fully qualified evidence item",
        source_type="sync_turn",
        memory_confidence=0.4,
        source_confidence_prior=0.4,
        retention_confidence=0.85,
        retention_disposition="retain",
        retention_evidence_at=evidence_at,
    )
    await _insert_bound_evidence(
        item_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        created_at=evidence_at,
        provenance={"provider": "openai", "model": "gpt-test"},
    )
    async with _make_client(tenant_id, principal_id) as client:
        body = await _fetch_readiness(client, item_id)
    assert body["blockers"] == []
    assert body["readiness_state"] == "eligible_now"
    assert body["selected_basis"] == "retention_evidence"
    assert body["promotion_policy_version"] == "promotion-evidence-v1"
    assert body["evidence_state"] == "bound-qualified"
    assert body["evidence_score"] == pytest.approx(min(0.85, 0.2 * 0.4 + 0.8 * 0.85))
    assert body["classification_provider"] == "openai"
    assert body["classification_model"] == "gpt-test"
    assert body["classification_version"] == "classification-v2"
    assert body["retention_policy_version"] == "retention-v1"
    # Statically eligible with no scheduled job: awaiting reconciliation.
    assert body["promotion_job_state"] is None
    assert body["jobs"] == []
    assert body["terminal_under_current_policy"] is False
    assert body["can_auto_promote_without_new_evidence_or_review"] is True


async def test_promotion_job_states_scheduled_overdue_dead():
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    base = dict(tenant_id=tenant_id, principal_id=principal_id, memory_confidence=0.9)

    scheduled_item = await _insert_item(content="scheduled job item", **base)
    overdue_item = await _insert_item(content="overdue job item", **base)
    dead_item = await _insert_item(content="dead job item", **base)
    mixed_item = await _insert_item(content="dead plus scheduled item", **base)

    await _insert_job(
        tenant_id=tenant_id,
        item_id=scheduled_item,
        job_type="promotion.path_a",
        run_after=FIXED_NOW + timedelta(hours=10),
    )
    await _insert_job(
        tenant_id=tenant_id,
        item_id=overdue_item,
        job_type="promotion.path_a",
        run_after=FIXED_NOW - timedelta(hours=10),
    )
    await _insert_job(
        tenant_id=tenant_id,
        item_id=dead_item,
        job_type="promotion.path_a",
        status="dead",
        run_after=FIXED_NOW - timedelta(hours=10),
    )
    await _insert_job(
        tenant_id=tenant_id,
        item_id=mixed_item,
        job_type="promotion.path_a",
        status="dead",
        run_after=FIXED_NOW - timedelta(hours=10),
    )
    await _insert_job(
        tenant_id=tenant_id,
        item_id=mixed_item,
        job_type="classification.refine",
        run_after=FIXED_NOW + timedelta(hours=5),
        dedupe_suffix="-refine",
    )

    async with _make_client(tenant_id, principal_id) as client:
        scheduled = await _fetch_readiness(client, scheduled_item)
        overdue = await _fetch_readiness(client, overdue_item)
        dead = await _fetch_readiness(client, dead_item)
        mixed = await _fetch_readiness(client, mixed_item)
    assert scheduled["promotion_job_state"] == "scheduled"
    assert overdue["promotion_job_state"] == "overdue"
    assert dead["promotion_job_state"] == "dead"
    # dead outranks scheduled; the classification job is listed but does not
    # drive promotion_job_state.
    assert mixed["promotion_job_state"] == "dead"
    assert {job["job_type"] for job in mixed["jobs"]} == {
        "promotion.path_a",
        "classification.refine",
    }


async def test_last_evaluation_explicit_unknown_and_classification_trigger():
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    plain_item = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="no events item",
        memory_confidence=0.9,
    )
    async with _make_client(tenant_id, principal_id) as client:
        body = await _fetch_readiness(client, plain_item)
    assert body["last_evaluation"]["trigger"] == "unknown"
    assert body["last_evaluation"]["at"] is None
    assert body["last_evaluation"]["policy_version"] is None

    eventful_item = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="item with classification event",
        memory_confidence=0.9,
    )
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO item_events (item_id, tenant_id, event_type, field_name, "
                "new_value, reason) VALUES (:item_id, :tenant_id, 'classification', 'kind', "
                "'{}', :reason)"
            ),
            {
                "item_id": eventful_item,
                "tenant_id": tenant_id,
                "reason": json.dumps(
                    {"worker_operation": "classification.refine", "result": "no_change"}
                ),
            },
        )
        await session.commit()
    async with _make_client(tenant_id, principal_id) as client:
        body = await _fetch_readiness(client, eventful_item)
    assert body["last_evaluation"]["trigger"] == "classification.refine"
    assert body["last_evaluation"]["at"] is not None


async def test_non_proposed_item_is_not_a_candidate():
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="already active item",
        memory_confidence=0.9,
        review_status="active",
    )
    async with _make_client(tenant_id, principal_id) as client:
        body = await _fetch_readiness(client, item_id)
    assert body["is_promotion_candidate"] is False
    assert body["readiness_state"] == "not_a_promotion_candidate"
    assert body["terminal_under_current_policy"] is True


# ---- authorization / RLS --------------------------------------------------------


async def test_readiness_requires_review_scope(monkeypatch: pytest.MonkeyPatch):
    if not await _db_ok():
        _require_db()
    import engram.db as db_module
    from engram.auth import digest_api_key_secret, generate_api_key, parse_api_key

    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="scoped access item",
        memory_confidence=0.9,
    )

    plaintext = generate_api_key()
    parsed = parse_api_key(plaintext)
    assert parsed.key_id is not None
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO api_keys (id, tenant_id, principal_id, key_id, secret_digest, "
                "digest_algorithm, scopes, label) VALUES (:id, :tid, :pid, :kid, :sd, "
                "'sha256', :scopes, 'readiness-test')"
            ),
            {
                "id": str(uuid.uuid4()),
                "tid": tenant_id,
                "pid": principal_id,
                "kid": parsed.key_id,
                "sd": digest_api_key_secret(parsed.secret),
                "scopes": ["read", "write"],
            },
        )
        await session.commit()

    settings.auth_enabled = True
    try:
        app = create_app()
        monkeypatch.setattr(db_module, "async_session_factory", _test_session_factory)
        monkeypatch.setattr(db_module, "owner_session_factory", _test_session_factory)
        monkeypatch.setattr(db_module, "read_session_factory", _test_session_factory)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            denied = await client.get(
                f"/v1/review/promotion-readiness/{item_id}",
                headers={"Authorization": f"Bearer {plaintext}"},
            )
            assert denied.status_code == 403, denied.text
    finally:
        settings.auth_enabled = False


async def test_cross_tenant_item_is_non_disclosing_404():
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    other_tenant = str(uuid.uuid4())
    other_principal = str(uuid.uuid4())
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO tenants (id, name, slug) VALUES (:id, 'ReadinessT2', :slug)"
            ),
            {"id": other_tenant, "slug": f"readiness-t2-{other_tenant[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES (:pid, :tid, 'admin', 'admin')"
            ),
            {"pid": other_principal, "tid": other_tenant},
        )
        await session.commit()
    foreign_item = await _insert_item(
        tenant_id=other_tenant,
        principal_id=other_principal,
        content="foreign tenant item",
        memory_confidence=0.9,
    )
    async with _make_client(tenant_id, principal_id) as client:
        resp = await client.get(f"/v1/review/promotion-readiness/{foreign_item}")
    assert resp.status_code == 404
    assert "Item not found" in resp.text


async def test_unknown_item_is_404():
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    async with _make_client(tenant_id, principal_id) as client:
        resp = await client.get(f"/v1/review/promotion-readiness/{uuid.uuid4()}")
    assert resp.status_code == 404


# ---- no-mutation / no provider call --------------------------------------------


async def test_readiness_never_mutates_state():
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="unchanged item",
        memory_confidence=0.9,
    )
    async with _test_session_factory() as session:
        before = dict(
            (
                await session.execute(
                    text("SELECT * FROM memory_items WHERE id = :id"), {"id": item_id}
                )
            )
            .mappings()
            .one()
        )
    async with _make_client(tenant_id, principal_id) as client:
        await _fetch_readiness(client, item_id)
        await _fetch_readiness(client, item_id)
    async with _test_session_factory() as session:
        after = dict(
            (
                await session.execute(
                    text("SELECT * FROM memory_items WHERE id = :id"), {"id": item_id}
                )
            )
            .mappings()
            .one()
        )
        events = (
            await session.execute(
                text("SELECT count(*) FROM item_events WHERE item_id = :id"), {"id": item_id}
            )
        ).scalar_one()
        jobs = (
            await session.execute(text("SELECT count(*) FROM jobs"))
        ).scalar_one()
    assert before == after
    assert events == 0
    assert jobs == 0
