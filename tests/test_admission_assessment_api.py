"""API contract proof for admission assessments (issue #159).

Covers the current/history/detail/reevaluate routes and the safe summaries on
promotion readiness and the review queue: what each authority tier may see,
that ``missing`` stays distinguishable from an ``unknown`` outcome, and that
no provider, transcript or conflict-candidate detail reaches an ordinary
reader.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.admission_assessment import POLICY_CONTRACT_VERSION, POLICY_PROFILE_KEY
from engram.api.app import create_app
from engram.config import settings
from engram.db import get_session
from engram.promotion import auto_promote_proposed_memories

_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    _engine, class_=AsyncSession, expire_on_commit=False
)


@pytest.fixture(autouse=True)
async def _fresh_engine():
    global _engine, _factory
    _engine = create_async_engine(settings.database_url, poolclass=NullPool)
    _factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    yield
    await _engine.dispose()


async def _db_ok() -> bool:
    try:
        async with _engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.fixture(autouse=True)
async def _requires_db():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema")


@pytest.fixture(autouse=True)
async def _capture_default():
    original = settings.admission_assessment_capture_enabled
    settings.admission_assessment_capture_enabled = False
    yield
    settings.admission_assessment_capture_enabled = original


@pytest.fixture(autouse=True)
async def _clean():
    if not await _db_ok():
        return
    async with _engine.begin() as conn:
        await conn.execute(text("DELETE FROM admission_assessment_current"))
        await conn.execute(text("DELETE FROM admission_assessments"))
        await conn.execute(text("DELETE FROM jobs"))
        await conn.execute(text("DELETE FROM item_events"))
        await conn.execute(text("DELETE FROM classification_runs"))
        await conn.execute(text("DELETE FROM memory_items"))
        await conn.execute(
            text(
                "UPDATE memory_kinds SET enabled = TRUE, "
                "auto_promote_from_inferred = TRUE WHERE name = 'fact'"
            )
        )
        await conn.execute(
            text(
                "UPDATE tenant_config SET auto_promote_enabled = TRUE, "
                "auto_promote_confidence_threshold = 0.7, "
                "auto_promote_min_age_hours = 72, "
                "auto_promote_evidence_enabled = FALSE, "
                "auto_promote_evidence_threshold = 0.7"
            )
        )


async def _default_ids() -> tuple[str, str]:
    async with _factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT t.id::text AS tenant_id, p.id::text AS principal_id "
                        "FROM tenants t JOIN principals p "
                        "ON p.tenant_id = t.id AND p.name = 'admin' WHERE t.slug = 'default'"
                    )
                )
            )
            .mappings()
            .one()
        )
    return str(row["tenant_id"]), str(row["principal_id"])


async def _get_test_session():
    async with _factory() as session:
        from engram.db import apply_rls_context

        tenant_id, principal_id = await _default_ids()
        await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
        yield session


@pytest.fixture
def app():
    import engram.db as db_module

    application = create_app()
    application.dependency_overrides[get_session] = _get_test_session
    saved = (
        db_module.async_session_factory,
        db_module.owner_session_factory,
        db_module.read_session_factory,
    )
    db_module.async_session_factory = _factory
    db_module.owner_session_factory = _factory
    db_module.read_session_factory = _factory
    yield application
    (
        db_module.async_session_factory,
        db_module.owner_session_factory,
        db_module.read_session_factory,
    ) = saved


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _insert_item(
    *,
    tenant_id: str,
    principal_id: str,
    memory_confidence: float = 0.9,
    kind: str = "fact",
) -> uuid.UUID:
    item_id = uuid.uuid4()
    async with _factory() as session:
        await session.execute(
            text(
                "INSERT INTO memory_items (id, tenant_id, principal_id, content, "
                "content_hash, kind, visibility, review_status, memory_confidence, "
                "source_trust, authority, importance, source_type, created_at, valid_from) "
                "VALUES (:id, :t, :p, :c, :h, :k, 'tenant', 'proposed', :mc, 0.5, 10, "
                "0.5, 'manual', :created, :created)"
            ),
            {
                "id": item_id,
                "t": tenant_id,
                "p": principal_id,
                "c": f"content {item_id}",
                "h": f"sha256:{uuid.uuid4().hex * 2}",
                "k": kind,
                "mc": memory_confidence,
                "created": datetime.now(UTC) - timedelta(hours=100),
            },
        )
        await session.commit()
    return item_id


async def _promote(tenant_id: str) -> None:
    async with _factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id}
        )
        await auto_promote_proposed_memories(session, tenant_id)


# --- Current -----------------------------------------------------------------


async def test_current_reports_missing_before_anything_is_recorded(client) -> None:
    """The expected state while capture is disabled — and it must say
    ``missing``, not fabricate a decision."""
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(tenant_id=tenant_id, principal_id=principal_id)

    response = await client.get(f"/v1/items/{item_id}/admission-assessment")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "missing"
    assert body["assessment"] is None
    assert body["capture_enabled"] is False
    assert body["policy_profile_key"] == POLICY_PROFILE_KEY
    assert body["policy_contract_version"] == POLICY_CONTRACT_VERSION
    # The digests a caller would compare against are still returned, so an
    # operator can see what current state hashes to before any capture.
    assert body["current_input_digest"].startswith("sha256:")
    assert body["current_policy_config_digest"].startswith("sha256:")


async def test_current_returns_the_recorded_decision_and_then_reports_stale(client) -> None:
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, memory_confidence=0.1
    )
    await _promote(tenant_id)

    body = (await client.get(f"/v1/items/{item_id}/admission-assessment")).json()
    assert body["status"] == "current"
    assert body["assessment"]["outcome"] == "insufficient_evidence"
    assert body["assessment"]["decision_hash"].startswith("sha256:")
    recorded_id = body["assessment"]["assessment_id"]

    async with _engine.begin() as conn:
        await conn.execute(
            text("UPDATE memory_items SET memory_confidence = 0.95 WHERE id = :id"),
            {"id": item_id},
        )

    body = (await client.get(f"/v1/items/{item_id}/admission-assessment")).json()
    assert body["status"] == "stale"
    # Still the same immutable row: staleness is a comparison, not a rewrite.
    assert body["assessment"]["assessment_id"] == recorded_id


async def test_the_basic_view_carries_no_decision_inputs_or_evidence_refs(client) -> None:
    """Ordinary item readers get the decision, never the reviewer detail."""
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, memory_confidence=0.1
    )
    await _promote(tenant_id)

    assessment = (
        await client.get(f"/v1/items/{item_id}/admission-assessment")
    ).json()["assessment"]
    assert "decision_inputs" not in assessment
    assert "available_memory_assessment_refs" not in assessment
    assert "classification_run_id" not in assessment
    assert "actor_principal_id" not in assessment


# --- History -----------------------------------------------------------------


async def test_history_is_newest_first_and_keyset_paginated(client) -> None:
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, memory_confidence=0.1
    )
    for _ in range(3):
        await _promote(tenant_id)

    body = (await client.get(f"/v1/items/{item_id}/admission-assessments")).json()
    assert len(body["assessments"]) == 3
    evaluated = [row["evaluated_at"] for row in body["assessments"]]
    assert evaluated == sorted(evaluated, reverse=True)
    assert body["next_before"] is None

    page = (
        await client.get(f"/v1/items/{item_id}/admission-assessments", params={"limit": 2})
    ).json()
    assert len(page["assessments"]) == 2
    assert page["next_before"] is not None
    rest = (
        await client.get(
            f"/v1/items/{item_id}/admission-assessments",
            params={"limit": 2, "before": page["next_before"]},
        )
    ).json()
    assert len(rest["assessments"]) == 1
    seen = {row["assessment_id"] for row in page["assessments"]} | {
        row["assessment_id"] for row in rest["assessments"]
    }
    assert len(seen) == 3


async def test_an_unknown_history_cursor_is_rejected(client) -> None:
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(tenant_id=tenant_id, principal_id=principal_id)
    response = await client.get(
        f"/v1/items/{item_id}/admission-assessments",
        params={"before": str(uuid.uuid4())},
    )
    assert response.status_code == 422


# --- Detail ------------------------------------------------------------------


async def test_reviewer_detail_exposes_normalized_inputs_and_diagnostic_refs(client) -> None:
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, memory_confidence=0.1
    )
    await _promote(tenant_id)
    assessment_id = (
        await client.get(f"/v1/items/{item_id}/admission-assessment")
    ).json()["assessment"]["assessment_id"]

    detail = (
        await client.get(f"/v1/items/{item_id}/admission-assessments/{assessment_id}")
    ).json()
    inputs = detail["decision_inputs"]
    # Both lanes' qualification facts are present: this is what lets an
    # operator verify that cooling was proven rather than guessed.
    assert inputs["legacy_trust_qualified"] is False
    assert "legacy_age_qualified" in inputs
    assert "evidence_trust_qualified" in inputs
    assert "evidence_age_qualified" in inputs
    assert detail["available_memory_assessment_refs"] == []
    # No content, transcript, provider output or conflict candidate identity.
    for leak in ("content", "transcript", "provider", "model", "conflicts_with_item_id"):
        assert leak not in inputs


async def test_detail_for_an_unknown_assessment_is_not_found(client) -> None:
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(tenant_id=tenant_id, principal_id=principal_id)
    response = await client.get(
        f"/v1/items/{item_id}/admission-assessments/{uuid.uuid4()}"
    )
    assert response.status_code == 404


# --- Reevaluation ------------------------------------------------------------


async def test_reevaluate_enqueues_the_existing_promotion_evaluate_job(client) -> None:
    """No new job type: the bounded request reuses #155 orchestration."""
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(tenant_id=tenant_id, principal_id=principal_id)

    response = await client.post(
        f"/v1/items/{item_id}/admission-assessments/reevaluate",
        json={"reason": "operator_request", "trigger_id": "ops-1"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["trigger_type"] == "manual"
    assert body["policy_profile_key"] == POLICY_PROFILE_KEY

    async with _factory() as session:
        job_type = await session.scalar(
            text("SELECT job_type FROM jobs WHERE id = :id"), {"id": body["job_id"]}
        )
    assert job_type == "promotion.evaluate"


async def test_replaying_the_same_reevaluation_returns_the_same_job(client) -> None:
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(tenant_id=tenant_id, principal_id=principal_id)

    first = await client.post(
        f"/v1/items/{item_id}/admission-assessments/reevaluate",
        json={"reason": "policy_changed", "trigger_id": "ops-2"},
    )
    second = await client.post(
        f"/v1/items/{item_id}/admission-assessments/reevaluate",
        json={"reason": "policy_changed", "trigger_id": "ops-2"},
    )
    assert first.json()["job_id"] == second.json()["job_id"]

    async with _factory() as session:
        count = await session.scalar(
            text("SELECT count(*) FROM jobs WHERE job_type = 'promotion.evaluate'")
        )
    assert count == 1


async def test_reevaluate_rejects_an_unknown_reason(client) -> None:
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(tenant_id=tenant_id, principal_id=principal_id)
    response = await client.post(
        f"/v1/items/{item_id}/admission-assessments/reevaluate",
        json={"reason": "because_i_said_so", "trigger_id": "x"},
    )
    assert response.status_code == 422


# --- Readiness and review queue summaries ------------------------------------


async def test_promotion_readiness_carries_the_safe_admission_summary(client) -> None:
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, memory_confidence=0.1
    )

    before = (await client.get(f"/v1/review/promotion-readiness/{item_id}")).json()
    assert before["admission_assessment_status"] == "missing"
    assert before["admission_outcome"] is None

    await _promote(tenant_id)

    after = (await client.get(f"/v1/review/promotion-readiness/{item_id}")).json()
    assert after["admission_assessment_status"] == "current"
    assert after["admission_outcome"] == "insufficient_evidence"
    assert after["admission_policy_profile"] == POLICY_PROFILE_KEY
    assert "new_evidence_required" in after["admission_next_actions"]
    # Readiness must not become a back door to reviewer-only detail.
    assert "decision_inputs" not in after
    assert "available_memory_assessment_refs" not in after


async def test_review_queue_filters_on_admission_state(client) -> None:
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    recorded = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, memory_confidence=0.1
    )
    await _promote(tenant_id)
    unrecorded = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, memory_confidence=0.1
    )

    everything = (await client.get("/v1/review/queue")).json()
    assert {row["id"] for row in everything} == {str(recorded), str(unrecorded)}

    by_outcome = (
        await client.get(
            "/v1/review/queue", params={"admission_outcome": "insufficient_evidence"}
        )
    ).json()
    assert [row["id"] for row in by_outcome] == [str(recorded)]

    by_blocker = (
        await client.get("/v1/review/queue", params={"admission_blocker": "confidence"})
    ).json()
    assert [row["id"] for row in by_blocker] == [str(recorded)]

    by_action = (
        await client.get(
            "/v1/review/queue", params={"admission_next_action": "new_evidence_required"}
        )
    ).json()
    assert [row["id"] for row in by_action] == [str(recorded)]

    by_state = (
        await client.get("/v1/review/queue", params={"admission_state": "current"})
    ).json()
    assert [row["id"] for row in by_state] == [str(recorded)]

    # An item with no decision matches only the missing filter — it cannot
    # honestly satisfy an outcome or blocker filter.
    missing = (
        await client.get("/v1/review/queue", params={"admission_state": "missing"})
    ).json()
    assert [row["id"] for row in missing] == [str(unrecorded)]


async def test_review_queue_filters_on_due_time(client) -> None:
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    # A cooling item: trust-qualified, waiting only on its age boundary.
    item_id = uuid.uuid4()
    async with _factory() as session:
        await session.execute(
            text(
                "INSERT INTO memory_items (id, tenant_id, principal_id, content, "
                "content_hash, kind, visibility, review_status, memory_confidence, "
                "source_trust, authority, importance, source_type, created_at, valid_from) "
                "VALUES (:id, :t, :p, 'cooling item', :h, 'fact', 'tenant', 'proposed', "
                "0.95, 0.5, 10, 0.5, 'manual', :created, :created)"
            ),
            {
                "id": item_id,
                "t": tenant_id,
                "p": principal_id,
                "h": f"sha256:{uuid.uuid4().hex * 2}",
                "created": datetime.now(UTC) - timedelta(hours=1),
            },
        )
        await session.commit()
    await _promote(tenant_id)

    far_future = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    near_past = (datetime.now(UTC) - timedelta(days=1)).isoformat()

    due_soon = (
        await client.get("/v1/review/queue", params={"admission_due_before": far_future})
    ).json()
    assert [row["id"] for row in due_soon] == [str(item_id)]

    not_yet = (
        await client.get("/v1/review/queue", params={"admission_due_before": near_past})
    ).json()
    assert not_yet == []


async def test_review_queue_entries_carry_the_summary_without_blocker_internals(
    client,
) -> None:
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, memory_confidence=0.1
    )
    await _promote(tenant_id)

    entry = (await client.get("/v1/review/queue")).json()[0]
    assert entry["admission_assessment_status"] == "current"
    assert entry["admission_outcome"] == "insufficient_evidence"
    # The filter's internal blocker list is not part of the response shape;
    # current-policy blockers are already published as promotion_blockers.
    assert "admission_blocker_codes" not in entry
    assert "promotion_blockers" in entry


async def test_queue_filter_finds_a_match_beyond_the_first_unfiltered_window(client) -> None:
    """The regression that motivated SQL-side filtering.

    Sixty proposed items, only the oldest of which is review_required. With a
    limit of 5, a filter applied to a preselected newest-first page would see
    only the five newest and answer "nothing matches" — a false negative on
    exactly the operational question the queue exists to answer.

    The needle is a ``preference``, a seeded kind that is permanently not
    auto-promotable, so its ``review_required`` decision stays put across
    later passes instead of quietly being admitted out of the queue.
    """
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()

    needle = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, kind="preference"
    )
    # A wall of newer, low-confidence proposals ahead of it.
    for _ in range(59):
        await _insert_item(
            tenant_id=tenant_id, principal_id=principal_id, memory_confidence=0.1
        )
    async with _engine.begin() as conn:
        await conn.execute(
            text("UPDATE memory_items SET created_at = :c WHERE id = :i"),
            {"c": datetime.now(UTC) - timedelta(days=400), "i": needle},
        )
    await _promote(tenant_id)

    unfiltered = (await client.get("/v1/review/queue", params={"limit": 5})).json()
    assert len(unfiltered) == 5
    assert str(needle) not in {row["id"] for row in unfiltered}

    for params in (
        {"admission_outcome": "review_required"},
        {"admission_blocker": "kind_policy"},
        {"admission_next_action": "human_review_required"},
    ):
        found = (
            await client.get("/v1/review/queue", params={**params, "limit": 5})
        ).json()
        assert [row["id"] for row in found] == [str(needle)], params


async def test_queue_computed_state_filter_also_reaches_past_the_page(client) -> None:
    """``current``/``stale`` cannot be a SQL predicate, so it walks the queue
    in bounded batches rather than stopping at the first window."""
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()

    needle = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, kind="preference"
    )
    # Age the needle to the back of the queue *before* evaluating it:
    # created_at is part of the evaluated input, so moving it afterwards would
    # legitimately make its own decision stale.
    async with _engine.begin() as conn:
        await conn.execute(
            text("UPDATE memory_items SET created_at = :c WHERE id = :i"),
            {"c": datetime.now(UTC) - timedelta(days=400), "i": needle},
        )
    await _promote(tenant_id)
    # Newer items with no recorded decision at all sit ahead of it.
    settings.admission_assessment_capture_enabled = False
    for _ in range(40):
        await _insert_item(tenant_id=tenant_id, principal_id=principal_id)

    found = (
        await client.get(
            "/v1/review/queue", params={"admission_state": "current", "limit": 5}
        )
    ).json()
    assert [row["id"] for row in found] == [str(needle)]

    # And the complement resolves the other way.
    missing = (
        await client.get(
            "/v1/review/queue", params={"admission_state": "missing", "limit": 100}
        )
    ).json()
    assert len(missing) == 40
    assert str(needle) not in {row["id"] for row in missing}


async def test_queue_filter_scan_is_bounded(client) -> None:
    """Bounded means bounded: the walk stops at an explicit cap rather than
    scanning a whole tenant backlog to answer one filtered page."""
    from engram.api.routes.admission_assessments import (
        MAX_ADMISSION_FILTER_SCAN,
        AdmissionQueueFilters,
    )

    assert MAX_ADMISSION_FILTER_SCAN > 0
    assert AdmissionQueueFilters(state="current").needs_computed_state is True
    # Stored-fact filters never need the walk at all.
    assert AdmissionQueueFilters(outcome="cooling").needs_computed_state is False
    assert AdmissionQueueFilters(state="missing").needs_computed_state is False
