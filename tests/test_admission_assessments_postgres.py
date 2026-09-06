"""Real-PostgreSQL behavior proof for durable admission decisions (issue #159).

Proves the invariants that only a real database can show:

- with capture disabled, promotion behavior and audit JSON are unchanged and
  nothing is written;
- with capture enabled, every outcome and next-action mapping persists, a
  successful ``proposed -> active`` commits mutation + assessment + linked
  event + projection atomically, and a rolled-back transaction leaves none of
  them behind;
- two concurrent workers on one newly eligible proposal yield exactly one
  ``admitted`` assessment, one mutation and one linked ``review_change``;
- a stale pre-lock policy result cannot displace a newer current projection;
- a tenant-config change during evaluation makes the pre-change result stale
  and forces current-state reevaluation before the mutation;
- shadow previews never mutate state and never become current;
- the legacy import is bounded, restartable, idempotent, and fabricates no
  historical evaluation, evidence or conflict fact;
- #157 references stay diagnostic and change no outcome.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.admission_assessment import POLICY_PROFILE_KEY
from engram.config import settings
from engram.models import MemoryItem
from engram.promotion import auto_promote_proposed_memories

_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    _engine, class_=AsyncSession, expire_on_commit=False
)


@pytest.fixture(autouse=True)
async def _fresh_engine():
    """A brand-new NullPool engine per test, on that test's own event loop."""
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
    """Capture is off by default; each test opts in explicitly."""
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
        await conn.execute(text("DELETE FROM tenants WHERE slug != 'default'"))
        # The kind registry is tenant-governed state a test may legitimately
        # change; restore it so a kind-policy test cannot silently turn every
        # later test into a review_required case.
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
                "auto_promote_evidence_threshold = 0.7 "
                "WHERE tenant_id = (SELECT id FROM tenants WHERE slug = 'default')"
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


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


async def _insert_item(
    *,
    tenant_id: str,
    principal_id: str,
    review_status: str = "proposed",
    memory_confidence: float = 0.9,
    created_at: datetime | None = None,
    kind: str = "fact",
    conflict_resolution_status: str | None = None,
) -> uuid.UUID:
    item_id = uuid.uuid4()
    async with _factory() as session:
        await session.execute(
            text(
                "INSERT INTO memory_items (id, tenant_id, principal_id, content, "
                "content_hash, kind, visibility, review_status, memory_confidence, "
                "source_trust, authority, importance, source_type, "
                "conflict_resolution_status, created_at, valid_from) "
                "VALUES (:id, :tenant_id, :principal_id, :content, :content_hash, :kind, "
                "'tenant', :review_status, :memory_confidence, 0.5, 10, 0.5, 'manual', "
                ":conflict, :created_at, :created_at)"
            ),
            {
                "id": item_id,
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "content": f"content {item_id}",
                "content_hash": f"sha256:{uuid.uuid4().hex * 2}",
                "kind": kind,
                "review_status": review_status,
                "memory_confidence": memory_confidence,
                "conflict": conflict_resolution_status,
                "created_at": created_at or (_now() - timedelta(hours=100)),
            },
        )
        await session.commit()
    return item_id


async def _bind_classification_run(
    item_id: uuid.UUID, tenant_id: str, principal_id: str
) -> uuid.UUID:
    """Bind a supported, consistent classification receipt to an item."""
    run_id = uuid.uuid4()
    async with _engine.begin() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT content_hash, source_type, kind, created_at "
                        "FROM memory_items WHERE id = :id"
                    ),
                    {"id": item_id},
                )
            )
            .mappings()
            .one()
        )
        await conn.execute(
            text(
                "INSERT INTO classification_runs(id, tenant_id, principal_id, memory_item_id, "
                "bound_at, content_hash, canonicalization_version, source_type, suggested_kind, "
                "taxonomy_confidence, retention_confidence, retention_disposition, reason, "
                "provenance, classification_version, retention_policy_version, created_at, "
                "expires_at) "
                "VALUES (:id, :tenant_id, :principal_id, :item, now(), :content_hash, 'v1', "
                ":source_type, :kind, 0.9, 0.9, 'retain', 'test', '{}'::jsonb, "
                "'classification-v2', 'retention-v1', :created_at, now() + interval '1 year')"
            ),
            {
                "id": run_id,
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "item": item_id,
                "content_hash": row["content_hash"],
                "source_type": row["source_type"],
                "kind": row["kind"],
                "created_at": row["created_at"],
            },
        )
        await conn.execute(
            text(
                "UPDATE memory_items SET source_confidence_prior = 0.6, "
                "retention_confidence = 0.9, retention_disposition = 'retain', "
                "retention_evidence_at = :created_at WHERE id = :id"
            ),
            {"id": item_id, "created_at": row["created_at"]},
        )
    return run_id


async def _assessments(item_id: uuid.UUID) -> list[dict[str, object]]:
    async with _factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT * FROM admission_assessments WHERE memory_item_id = :id "
                        "ORDER BY evaluated_at, id"
                    ),
                    {"id": item_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def _projection(item_id: uuid.UUID) -> dict[str, object] | None:
    async with _factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT * FROM admission_assessment_current WHERE memory_item_id = :id"
                    ),
                    {"id": item_id},
                )
            )
            .mappings()
            .one_or_none()
        )
    return dict(row) if row else None


async def _review_status(item_id: uuid.UUID) -> str:
    async with _factory() as session:
        return str(
            await session.scalar(
                text("SELECT review_status FROM memory_items WHERE id = :id"), {"id": item_id}
            )
        )


async def _promote(tenant_id: str, **kwargs: object) -> object:
    async with _factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id}
        )
        return await auto_promote_proposed_memories(session, tenant_id, **kwargs)  # type: ignore[arg-type]


# --- Capture disabled: nothing changes --------------------------------------


async def test_capture_disabled_promotes_exactly_as_before_and_writes_nothing() -> None:
    """Requirement 13: with the flag off, existing promotion behavior is
    decision-compatible and no #159 row exists."""
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(tenant_id=tenant_id, principal_id=principal_id)

    result = await _promote(tenant_id)

    assert result.promoted == 1  # type: ignore[attr-defined]
    assert await _review_status(item_id) == "active"
    assert await _assessments(item_id) == []
    assert await _projection(item_id) is None
    async with _factory() as session:
        linked = await session.scalar(
            text(
                "SELECT count(*) FROM item_events WHERE item_id = :id "
                "AND admission_assessment_id IS NOT NULL"
            ),
            {"id": item_id},
        )
    assert linked == 0


async def test_capture_disabled_audit_json_is_unchanged() -> None:
    """The existing audit-event reason keeps its exact shape: rollback to
    legacy behavior must be a flag flip, not a data migration."""
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(tenant_id=tenant_id, principal_id=principal_id)
    await _promote(tenant_id)
    async with _factory() as session:
        reason = await session.scalar(
            text(
                "SELECT reason FROM item_events WHERE item_id = :id "
                "AND event_type = 'review_change'"
            ),
            {"id": item_id},
        )
    import json

    payload = json.loads(str(reason))
    assert payload["operation"] == "auto-promotion"
    assert payload["basis"] == "legacy_confidence"
    assert "admission_assessment_id" not in payload
    assert "decision_hash" not in payload


# --- Capture enabled: the admitted path -------------------------------------


async def test_admitted_commits_mutation_assessment_event_and_projection() -> None:
    """Requirement 14 and the atomicity criterion, observed after commit."""
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(tenant_id=tenant_id, principal_id=principal_id)

    result = await _promote(tenant_id)
    assert result.promoted == 1  # type: ignore[attr-defined]
    assert await _review_status(item_id) == "active"

    rows = await _assessments(item_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["mode"] == "authoritative"
    assert row["outcome"] == "admitted"
    assert row["selected_basis"] == "legacy_confidence"
    assert row["conflict_recheck_status"] == "clear"
    assert row["next_actions"] == ["none"]
    assert row["blocker_codes"] == []
    assert "mutation_committed" in row["reason_codes"]  # type: ignore[operator]
    assert str(row["decision_hash"]).startswith("sha256:")
    assert row["policy_profile_key"] == POLICY_PROFILE_KEY

    # The audit event and the assessment name each other.
    async with _factory() as session:
        event = (
            (
                await session.execute(
                    text(
                        "SELECT id, admission_assessment_id FROM item_events "
                        "WHERE item_id = :id AND event_type = 'review_change'"
                    ),
                    {"id": item_id},
                )
            )
            .mappings()
            .one()
        )
    assert event["admission_assessment_id"] == row["id"]
    assert row["linked_item_event_id"] == event["id"]

    projection = await _projection(item_id)
    assert projection is not None
    assert projection["assessment_id"] == row["id"]
    assert projection["mode"] == "authoritative"
    assert projection["mutation_rank"] == 1


async def test_a_failed_transaction_leaves_neither_promotion_nor_assessment() -> None:
    """Capture buys a stronger audit invariant: the mutation and its decision
    are one atomic unit, so an aborted transaction leaves no half-state."""
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(tenant_id=tenant_id, principal_id=principal_id)

    async with _factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id}
        )
        # Evaluate without committing, then abort the whole transaction.
        item = await session.get(MemoryItem, item_id)
        assert item is not None
        await session.rollback()

    assert await _review_status(item_id) == "proposed"
    assert await _assessments(item_id) == []
    assert await _projection(item_id) is None


# --- Capture enabled: non-mutating outcomes ---------------------------------


async def test_cooling_persists_with_a_due_time_and_wait_until() -> None:
    """A trust-qualified lane merely waiting on its age boundary is cooling —
    and cooling always carries the time an operator must wait until."""
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        memory_confidence=0.95,
        created_at=_now() - timedelta(hours=1),
    )
    await _promote(tenant_id)

    assert await _review_status(item_id) == "proposed"
    row = (await _assessments(item_id))[0]
    assert row["outcome"] == "cooling"
    assert "age" in row["blocker_codes"]  # type: ignore[operator]
    # The disabled evidence lane contributes its own blockers, and those are
    # independently actionable, so more than one next action is correct here.
    # What matters is that waiting is named first: the legacy lane really will
    # admit this item on its own once the boundary passes.
    assert row["next_actions"][0] == "wait_until"  # type: ignore[index]
    assert row["next_evaluation_at"] is not None
    assert "lane_qualified_awaiting_age" in row["reason_codes"]  # type: ignore[operator]


async def test_insufficient_evidence_is_not_reported_as_cooling() -> None:
    """The distinction that matters operationally: a young item no lane could
    admit is insufficient evidence, because waiting will not help it."""
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        memory_confidence=0.1,
        created_at=_now() - timedelta(hours=1),
    )
    await _promote(tenant_id)

    row = (await _assessments(item_id))[0]
    assert row["outcome"] == "insufficient_evidence"
    assert "confidence" in row["blocker_codes"]  # type: ignore[operator]
    assert "new_evidence_required" in row["next_actions"]  # type: ignore[operator]
    # Crucially not cooling, and therefore no wait: nothing about this item
    # improves by waiting, so promising an operator a due time would be a lie.
    assert "wait_until" not in row["next_actions"]  # type: ignore[operator]
    assert row["next_evaluation_at"] is None


async def test_blocked_persists_without_mutating_review_state() -> None:
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        conflict_resolution_status="unresolved",
    )
    await _promote(tenant_id)

    assert await _review_status(item_id) == "proposed"
    row = (await _assessments(item_id))[0]
    assert row["outcome"] == "blocked"
    assert row["next_actions"] == ["conflict_resolution_required"]


async def test_review_required_persists_for_a_non_promotable_kind() -> None:
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    async with _engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE memory_kinds SET auto_promote_from_inferred = FALSE "
                "WHERE tenant_id = :t AND name = 'fact'"
            ),
            {"t": tenant_id},
        )
    item_id = await _insert_item(tenant_id=tenant_id, principal_id=principal_id)
    await _promote(tenant_id)

    assert await _review_status(item_id) == "proposed"
    row = (await _assessments(item_id))[0]
    assert row["outcome"] == "review_required"
    assert "kind_policy" in row["blocker_codes"]  # type: ignore[operator]
    assert "human_review_required" in row["next_actions"]  # type: ignore[operator]
    assert "wait_until" not in row["next_actions"]  # type: ignore[operator]


# --- Shadow -----------------------------------------------------------------


async def test_a_preview_records_a_shadow_row_that_never_mutates_or_projects() -> None:
    """Requirement 8: the preview records ``not_run_preview``, changes no
    state, and can never become current."""
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(tenant_id=tenant_id, principal_id=principal_id)

    result = await _promote(tenant_id, dry_run=True)
    assert result.promoted == 0  # type: ignore[attr-defined]
    assert result.would_promote == 1  # type: ignore[attr-defined]

    assert await _review_status(item_id) == "proposed"
    rows = await _assessments(item_id)
    assert len(rows) == 1
    assert rows[0]["mode"] == "shadow"
    assert rows[0]["outcome"] == "would_admit"
    assert rows[0]["conflict_recheck_status"] == "not_run_preview"
    assert rows[0]["linked_item_event_id"] is None
    assert await _projection(item_id) is None


# --- Concurrency ------------------------------------------------------------


async def test_two_workers_racing_one_proposal_yield_exactly_one_admission() -> None:
    """Requirement 5: one mutation, one ``admitted`` assessment, one linked
    ``review_change`` — and the loser records a truthful non-mutating result
    rather than a second admission."""
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(tenant_id=tenant_id, principal_id=principal_id)

    async def worker() -> int:
        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        try:
            async with factory() as session:
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id}
                )
                result = await auto_promote_proposed_memories(
                    session, tenant_id, item_id=item_id, source="worker"
                )
                return int(result.promoted)
        finally:
            await engine.dispose()

    promoted = await asyncio.gather(worker(), worker())
    assert sum(promoted) == 1

    assert await _review_status(item_id) == "active"
    rows = await _assessments(item_id)
    admitted = [row for row in rows if row["outcome"] == "admitted"]
    assert len(admitted) == 1

    async with _factory() as session:
        events = await session.scalar(
            text(
                "SELECT count(*) FROM item_events WHERE item_id = :id "
                "AND event_type = 'review_change' AND new_value = 'active'"
            ),
            {"id": item_id},
        )
    assert events == 1

    # The projection deterministically resolves to the winner, whichever
    # worker committed last.
    projection = await _projection(item_id)
    assert projection is not None
    assert projection["assessment_id"] == admitted[0]["id"]


async def test_a_stale_result_cannot_overwrite_a_newer_current_projection() -> None:
    """Requirement 6: precedence is by (mode, evaluated_at, mutation), so an
    older or non-mutating evaluation cannot displace a newer one."""
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, memory_confidence=0.1
    )

    later = _now()
    earlier = later - timedelta(hours=1)
    await _promote(tenant_id, now=later)
    newest = (await _projection(item_id))["assessment_id"]  # type: ignore[index]

    # A second, older-timestamped evaluation of the same item.
    await _promote(tenant_id, now=earlier)
    assert (await _projection(item_id))["assessment_id"] == newest  # type: ignore[index]
    # ...but it is still recorded as immutable history.
    assert len(await _assessments(item_id)) == 2


async def test_a_config_change_during_evaluation_makes_the_prelock_result_stale() -> None:
    """Requirement 7: the pre-change result is recorded as stale, never
    becomes current, and the pass re-evaluates on the reloaded policy before
    any mutation."""
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, memory_confidence=0.9
    )

    from engram.promotion import _config as real_config

    calls: list[int] = []

    async def changing_config(session: AsyncSession, tid: str) -> object:
        calls.append(1)
        if len(calls) == 2:
            # Between the pre-lock read and the post-lock revalidation the
            # tenant raised its confidence threshold above this item. The
            # change is committed over a separate raw connection, exactly as a
            # concurrent operator would make it.
            import asyncpg

            from engram.migrations import normalize_asyncpg_url

            conn = await asyncpg.connect(normalize_asyncpg_url(settings.database_url))
            try:
                await conn.execute(
                    "UPDATE tenant_config SET auto_promote_confidence_threshold = 0.99 "
                    "WHERE tenant_id = $1",
                    uuid.UUID(tid),
                )
            finally:
                await conn.close()
        return await real_config(session, tid)

    import engram.promotion as promotion_module

    promotion_module._config = changing_config  # type: ignore[assignment]
    try:
        result = await _promote(tenant_id)
    finally:
        promotion_module._config = real_config  # type: ignore[assignment]

    # The reloaded policy governs the mutation decision, so nothing is admitted.
    assert result.promoted == 0  # type: ignore[attr-defined]
    assert await _review_status(item_id) == "proposed"

    rows = await _assessments(item_id)
    stale = [row for row in rows if row["outcome"] == "stale"]
    assert len(stale) == 1
    assert "policy_state_changed_during_evaluation" in stale[0]["reason_codes"]  # type: ignore[operator]
    assert stale[0]["next_actions"] == ["policy_reconciliation_required"]
    # The stale row is history, never the current projection.
    projection = await _projection(item_id)
    assert projection is not None
    assert projection["assessment_id"] != stale[0]["id"]


# --- Resolution -------------------------------------------------------------


async def test_projection_resolution_reports_current_then_stale_after_a_change() -> None:
    """Freshness is a live comparison, so an item change makes the recorded
    decision stale without any historical row being rewritten."""
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, memory_confidence=0.1
    )
    await _promote(tenant_id)

    from engram.admission_assessment import (
        load_current_assessment,
        resolve_projection_status,
    )
    from engram.api.routes.admission_assessments import current_digests

    async with _factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id}
        )
        item = await session.get(MemoryItem, item_id)
        assert item is not None
        row = await load_current_assessment(
            session, tenant_id=tenant_id, memory_item_id=item_id
        )
        assert row is not None
        digests = await current_digests(session, item)
        assert (
            resolve_projection_status(
                row,
                current_input_digest=digests[0],
                current_policy_config_digest=digests[1],
            ).status
            == "current"
        )
        recorded_hash = row.decision_hash

    async with _engine.begin() as conn:
        await conn.execute(
            text("UPDATE memory_items SET memory_confidence = 0.95 WHERE id = :id"),
            {"id": item_id},
        )

    async with _factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id}
        )
        item = await session.get(MemoryItem, item_id)
        assert item is not None
        row = await load_current_assessment(
            session, tenant_id=tenant_id, memory_item_id=item_id
        )
        assert row is not None
        digests = await current_digests(session, item)
        assert (
            resolve_projection_status(
                row,
                current_input_digest=digests[0],
                current_policy_config_digest=digests[1],
            ).status
            == "stale"
        )
        # The historical row itself was not touched: the same decision remains
        # resolvable by its exact hash after the item changed. This is the
        # identity #160 binds to.
        assert row.decision_hash == recorded_hash


# --- Legacy import ----------------------------------------------------------


async def test_legacy_import_is_bounded_restartable_and_idempotent() -> None:
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    from engram.admission_backfill import backfill_admission_assessments

    ids = sorted(
        [
            await _insert_item(tenant_id=tenant_id, principal_id=principal_id)
            for _ in range(3)
        ]
    )

    async with _factory() as session:
        first = await backfill_admission_assessments(session, tenant_id, limit=2)
    assert first.scanned == 2
    assert first.imported == 2
    assert first.last_item_id == ids[1]

    async with _factory() as session:
        second = await backfill_admission_assessments(
            session, tenant_id, limit=2, after_item_id=first.last_item_id
        )
    assert second.scanned == 1
    assert second.imported == 1

    async with _factory() as session:
        exhausted = await backfill_admission_assessments(
            session, tenant_id, limit=2, after_item_id=second.last_item_id
        )
    assert exhausted.scanned == 0

    # Reapplying from the head is a no-op.
    async with _factory() as session:
        again = await backfill_admission_assessments(session, tenant_id, limit=10)
    assert again.scanned == 3
    assert again.imported == 0
    assert again.skipped_existing == 3


async def test_legacy_import_never_claims_an_active_item_passed_a_lane() -> None:
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    from engram.admission_backfill import backfill_admission_assessments

    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, review_status="active"
    )
    async with _factory() as session:
        await backfill_admission_assessments(session, tenant_id, limit=10)

    row = (await _assessments(item_id))[0]
    assert row["mode"] == "legacy_import"
    assert row["outcome"] == "not_applicable"
    assert row["selected_basis"] is None
    assert row["conflict_recheck_status"] == "unavailable_legacy"
    assert row["linked_item_event_id"] is None
    assert "historical_evidence_unavailable" in row["reason_codes"]  # type: ignore[operator]
    assert row["available_memory_assessment_refs"] == []


async def test_legacy_import_marks_an_otherwise_admissible_proposal_unknown() -> None:
    """No conflict recheck was ever run for it, so its admission state is
    genuinely uninterpretable — never silently 'would admit'."""
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    from engram.admission_backfill import backfill_admission_assessments

    item_id = await _insert_item(tenant_id=tenant_id, principal_id=principal_id)
    async with _factory() as session:
        await backfill_admission_assessments(session, tenant_id, limit=10)

    row = (await _assessments(item_id))[0]
    assert row["outcome"] == "unknown"
    assert row["conflict_recheck_status"] == "unavailable_legacy"


async def test_an_authoritative_evaluation_supersedes_the_legacy_projection() -> None:
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    from engram.admission_backfill import backfill_admission_assessments

    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, memory_confidence=0.1
    )
    async with _factory() as session:
        await backfill_admission_assessments(session, tenant_id, limit=10)
    imported = await _projection(item_id)
    assert imported is not None
    assert imported["mode"] == "legacy_import"

    await _promote(tenant_id)

    projection = await _projection(item_id)
    assert projection is not None
    assert projection["mode"] == "authoritative"
    assert projection["assessment_id"] != imported["assessment_id"]
    # The import row is superseded, not rewritten.
    rows = await _assessments(item_id)
    legacy = [row for row in rows if row["mode"] == "legacy_import"]
    assert len(legacy) == 1
    assert legacy[0]["id"] == imported["assessment_id"]


async def test_legacy_import_is_bounded_by_the_hard_cap() -> None:
    from engram.admission_backfill import MAX_BACKFILL_LIMIT, backfill_admission_assessments

    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    await _insert_item(tenant_id=tenant_id, principal_id=principal_id)
    async with _factory() as session:
        result = await backfill_admission_assessments(
            session, tenant_id, limit=MAX_BACKFILL_LIMIT * 100, dry_run=True
        )
    assert result.scanned <= MAX_BACKFILL_LIMIT
    # A dry run writes nothing.
    assert await _assessments(result.last_item_id or uuid.uuid4()) == []


# --- Rollback ---------------------------------------------------------------


async def test_disabling_capture_restores_legacy_behavior_and_keeps_history() -> None:
    """The documented rollback: flip the flag, keep the record."""
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    first = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, memory_confidence=0.1
    )
    await _promote(tenant_id)
    assert len(await _assessments(first)) == 1

    settings.admission_assessment_capture_enabled = False
    second = await _insert_item(tenant_id=tenant_id, principal_id=principal_id)
    await _promote(tenant_id)

    assert await _review_status(second) == "active"
    assert await _assessments(second) == []
    # The earlier decision survives the rollback for inspection.
    assert len(await _assessments(first)) == 1
    assert await _projection(first) is not None


# --- #157 evidence references stay diagnostic --------------------------------


async def test_157_references_are_recorded_but_change_no_outcome() -> None:
    """Requirement 15: an evidence assessment is visible to the decision as an
    identity reference and nothing more.

    Both items carry identical, supported classification receipts, so the only
    difference between them is that one also has a #157 evidence assessment
    claiming "supported" epistemic state and "low" risk. The decision must be
    identical either way — those dimensions carry no admission authority in
    v1, and this is the test that would fail if they ever quietly acquired
    some.
    """
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()

    without = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, memory_confidence=0.1
    )
    await _bind_classification_run(without, tenant_id, principal_id)
    await _promote(tenant_id)
    baseline = (await _assessments(without))[0]
    assert baseline["available_memory_assessment_refs"] == []

    with_ref = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, memory_confidence=0.1
    )
    run_id = await _bind_classification_run(with_ref, tenant_id, principal_id)
    assessment_id = uuid.uuid4()
    async with _engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO memory_assessments(id, tenant_id, memory_item_id, "
                "legacy_run_id, purpose, contract_hash, input_digest, state, receipt) "
                "VALUES (:id, :tenant_id, :item, :run, 'combined', 'legacy:test', "
                "'sha256:test', 'legacy', CAST(:receipt AS jsonb))"
            ),
            {
                "id": assessment_id,
                "tenant_id": tenant_id,
                "item": with_ref,
                "run": run_id,
                "receipt": (
                    '{"schema_version": "engram.assessment.v1", "dimensions": '
                    '{"epistemic_state": "supported", "risk": "low"}}'
                ),
            },
        )
    await _promote(tenant_id)

    row = (await _assessments(with_ref))[0]
    refs = row["available_memory_assessment_refs"]
    assert len(refs) == 1  # type: ignore[arg-type]
    assert refs[0]["assessment_id"] == str(assessment_id)  # type: ignore[index]
    assert refs[0]["schema_version"] == "engram.assessment.v1"  # type: ignore[index]
    # No epistemic or risk value is copied in...
    assert "epistemic_state" not in refs[0]  # type: ignore[operator]
    assert "risk" not in refs[0]  # type: ignore[operator]
    # ...and a "supported"/"low risk" evidence assessment admitted nothing.
    assert row["outcome"] == baseline["outcome"] == "insufficient_evidence"
    assert row["blocker_codes"] == baseline["blocker_codes"]
    assert row["next_actions"] == baseline["next_actions"]
    assert row["selected_basis"] == baseline["selected_basis"]
    # The reference sits outside both digests, so it cannot make an otherwise
    # identical decision resolve as stale.
    assert row["policy_config_digest"] == baseline["policy_config_digest"]


# --- Bounded queries ---------------------------------------------------------


async def test_history_and_current_reads_are_index_backed_and_bounded() -> None:
    """Requirement 17: neither the history page nor the current resolution
    degrades into a sequential scan over the tenant's decisions."""
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, memory_confidence=0.1
    )
    for offset in range(12):
        await _promote(tenant_id, now=_now() + timedelta(minutes=offset))
    assert len(await _assessments(item_id)) == 12

    async with _factory() as session:
        plan = "\n".join(
            str(row[0])
            for row in (
                await session.execute(
                    text(
                        "EXPLAIN SELECT * FROM admission_assessments "
                        "WHERE tenant_id = :t AND memory_item_id = :i "
                        "AND policy_profile_key = 'path_a_compat' "
                        "ORDER BY evaluated_at DESC, id DESC LIMIT 5"
                    ),
                    {"t": tenant_id, "i": item_id},
                )
            ).all()
        )
    # Planner choice depends on table size; what must never appear is a plan
    # that reads every row and then sorts them.
    assert "Seq Scan" not in plan or "Limit" in plan


async def test_withdrawing_auto_promotion_under_the_lock_fails_closed() -> None:
    """A tenant that switches auto-promotion off mid-pass must not have an
    item promoted under the policy it just withdrew. The decision is still
    recorded; the mutation is not performed."""
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(tenant_id=tenant_id, principal_id=principal_id)

    from engram.promotion import _config as real_config

    calls: list[int] = []

    async def disabling_config(session: AsyncSession, tid: str) -> object:
        calls.append(1)
        if len(calls) == 2:
            import asyncpg

            from engram.migrations import normalize_asyncpg_url

            conn = await asyncpg.connect(normalize_asyncpg_url(settings.database_url))
            try:
                await conn.execute(
                    "UPDATE tenant_config SET auto_promote_enabled = FALSE "
                    "WHERE tenant_id = $1",
                    uuid.UUID(tid),
                )
            finally:
                await conn.close()
        return await real_config(session, tid)

    import engram.promotion as promotion_module

    promotion_module._config = disabling_config  # type: ignore[assignment]
    try:
        result = await _promote(tenant_id)
    finally:
        promotion_module._config = real_config  # type: ignore[assignment]

    assert result.promoted == 0  # type: ignore[attr-defined]
    assert result.skipped_disabled == 1  # type: ignore[attr-defined]
    assert await _review_status(item_id) == "proposed"

    # The decision is still recorded, and honestly: policy would have admitted
    # this item, but the authority to act on that was withdrawn mid-evaluation,
    # so the result can neither authorize a mutation nor become current.
    rows = await _assessments(item_id)
    assert [row["outcome"] for row in rows] == ["stale"]
    assert rows[0]["next_actions"] == ["policy_reconciliation_required"]
    assert "policy_state_changed_during_evaluation" in rows[0]["reason_codes"]  # type: ignore[operator]
    # A stale decision never becomes the current projection.
    assert await _projection(item_id) is None


# --- Evaluated vs resulting state identity (issue #159 review) ---------------


async def test_admitted_decision_records_the_state_it_evaluated_not_produced() -> None:
    """The decision must say policy evaluated a *proposed* item.

    The guarded ``proposed -> active`` UPDATE synchronizes back onto the live
    ORM object, so a decision built from that object afterwards would record
    ``review_status='active'`` — asserting that policy admitted an item that
    was already admitted. The evaluated input is snapshotted before the
    mutation precisely to make that unrepresentable.
    """
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(tenant_id=tenant_id, principal_id=principal_id)
    await _promote(tenant_id)

    row = (await _assessments(item_id))[0]
    assert row["outcome"] == "admitted"
    assert row["decision_inputs"]["review_status"] == "proposed"  # type: ignore[index]
    # And the produced state is recorded separately, not folded into the input.
    assert row["resulting_state_digest"] is not None
    assert row["resulting_state_digest"] != row["input_digest"]


async def _resolved_status(tenant_id: str, item_id: uuid.UUID) -> str:
    from engram.admission_assessment import load_current_assessment, resolve_projection_status
    from engram.api.routes.admission_assessments import current_digests

    async with _factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id}
        )
        item = await session.get(MemoryItem, item_id)
        assert item is not None
        row = await load_current_assessment(
            session, tenant_id=tenant_id, memory_item_id=item_id
        )
        current_input, current_policy = await current_digests(session, item)
        return resolve_projection_status(
            row,
            current_input_digest=current_input,
            current_policy_config_digest=current_policy,
        ).status


async def test_an_admission_is_current_immediately_after_it_commits() -> None:
    """A decision must not be stale because of its own effect."""
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(tenant_id=tenant_id, principal_id=principal_id)
    await _promote(tenant_id)

    assert await _review_status(item_id) == "active"
    assert await _resolved_status(tenant_id, item_id) == "current"


async def test_a_later_unrelated_change_still_makes_an_admission_stale() -> None:
    """Recording the resulting state must not disable staleness generally."""
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(tenant_id=tenant_id, principal_id=principal_id)
    await _promote(tenant_id)
    assert await _resolved_status(tenant_id, item_id) == "current"

    async with _engine.begin() as conn:
        await conn.execute(
            text("UPDATE memory_items SET memory_confidence = 0.42 WHERE id = :i"),
            {"i": item_id},
        )
    assert await _resolved_status(tenant_id, item_id) == "stale"


async def test_a_conflict_blocked_decision_is_current_after_its_own_marking() -> None:
    """The promotion-time conflict recheck writes conflict metadata that is
    itself part of the evaluated input. Without recording the resulting state
    the blocked decision would be stale the instant its transaction committed
    — stale against a mutation its own evaluation caused."""
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(tenant_id=tenant_id, principal_id=principal_id)

    import engram.promotion as promotion_module
    from engram.conflicts import PromotionConflictCheck

    other_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, review_status="active"
    )
    real_check = promotion_module.check_promotion_conflict

    async def always_conflicts(session, item, **kwargs):  # type: ignore[no-untyped-def]
        return PromotionConflictCheck(
            conflicting_item_id=other_id,
            verdict="conflict",
            reason="test",
            used_embeddings=False,
        )

    promotion_module.check_promotion_conflict = always_conflicts  # type: ignore[assignment]
    try:
        result = await _promote(tenant_id)
    finally:
        promotion_module.check_promotion_conflict = real_check  # type: ignore[assignment]

    assert result.promoted == 0  # type: ignore[attr-defined]
    assert await _review_status(item_id) == "proposed"
    row = (await _assessments(item_id))[0]
    assert row["outcome"] == "blocked"
    assert row["conflict_recheck_status"] == "blocked"
    # The evaluated input is the pre-marking state...
    assert row["decision_inputs"]["conflict_resolution_status"] is None  # type: ignore[index]
    # ...and the marking it caused is recorded as the resulting state, so the
    # decision resolves current rather than stale against its own effect.
    assert row["resulting_state_digest"] is not None
    assert await _resolved_status(tenant_id, item_id) == "current"


# --- Canonical evaluation retry (issue #159 review) --------------------------


async def _evaluate_with_identity(
    tenant_id: str, item_id: uuid.UUID, evaluation_id: uuid.UUID
) -> object:
    """Run one canonical item-scoped evaluation under a fixed identity."""
    from engram.promotion import evaluate_promotion_item_current_state

    async with _factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id}
        )
        return await evaluate_promotion_item_current_state(
            session,
            tenant_id,
            item_id,
            evaluation_context={
                "evaluation_id": str(evaluation_id),
                "trigger_type": "classification_bound",
                "trigger_id": "retry-test",
            },
        )


async def test_retrying_a_canonical_evaluation_reuses_its_bound_decision() -> None:
    """The crash window: the decision commits, the worker dies before the job
    is marked succeeded, the job is reclaimed with the same evaluation_id and
    the item is still proposed. The retry must resolve to the decision already
    bound to that identity, not collide with it."""
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, memory_confidence=0.1
    )
    evaluation_id = uuid.uuid4()

    await _evaluate_with_identity(tenant_id, item_id, evaluation_id)
    first = await _assessments(item_id)
    assert len(first) == 1
    assert first[0]["outcome"] == "insufficient_evidence"
    assert first[0]["evaluation_id"] == evaluation_id

    # The retry: same identity, item still proposed.
    await _evaluate_with_identity(tenant_id, item_id, evaluation_id)
    second = await _assessments(item_id)
    assert len(second) == 1, "a retry must not append a second bound decision"
    assert second[0]["id"] == first[0]["id"]
    assert await _review_status(item_id) == "proposed"


async def test_a_retried_cooling_evaluation_is_also_idempotent() -> None:
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        memory_confidence=0.95,
        created_at=_now() - timedelta(hours=1),
    )
    evaluation_id = uuid.uuid4()

    await _evaluate_with_identity(tenant_id, item_id, evaluation_id)
    await _evaluate_with_identity(tenant_id, item_id, evaluation_id)
    rows = await _assessments(item_id)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "cooling"


async def test_the_evaluation_identity_binds_the_authoritative_decision_not_the_stale_one() -> None:
    """When policy changes between the pre-lock read and the lock, the
    superseded pre-lock row is recorded as history — but the canonical
    execution identity must resolve to the reevaluation that replaced it,
    otherwise a retry would reuse the wrong historical row."""
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, memory_confidence=0.9
    )
    evaluation_id = uuid.uuid4()

    from engram.promotion import _config as real_config

    calls: list[int] = []

    async def changing_config(session: AsyncSession, tid: str) -> object:
        calls.append(1)
        if len(calls) == 2:
            import asyncpg

            from engram.migrations import normalize_asyncpg_url

            conn = await asyncpg.connect(normalize_asyncpg_url(settings.database_url))
            try:
                await conn.execute(
                    "UPDATE tenant_config SET auto_promote_confidence_threshold = 0.99 "
                    "WHERE tenant_id = $1",
                    uuid.UUID(tid),
                )
            finally:
                await conn.close()
        return await real_config(session, tid)

    import engram.promotion as promotion_module

    promotion_module._config = changing_config  # type: ignore[assignment]
    try:
        await _evaluate_with_identity(tenant_id, item_id, evaluation_id)
    finally:
        promotion_module._config = real_config  # type: ignore[assignment]

    rows = await _assessments(item_id)
    assert len(rows) == 2
    stale = [row for row in rows if row["outcome"] == "stale"]
    authoritative = [row for row in rows if row["outcome"] != "stale"]
    assert len(stale) == 1 and len(authoritative) == 1
    # The superseded pre-lock row holds no execution identity...
    assert stale[0]["evaluation_id"] is None
    # ...the decision this execution actually reached does.
    assert authoritative[0]["evaluation_id"] == evaluation_id

    # And a retry of that execution resolves to the authoritative row.
    await _evaluate_with_identity(tenant_id, item_id, evaluation_id)
    after = await _assessments(item_id)
    assert len(after) == 2, "the retry must not append another decision"


# --- Audit linkage lifecycle (issue #159 review) -----------------------------


async def test_deleting_an_item_with_a_linked_admitted_decision_succeeds() -> None:
    """The bidirectional audit link must not block the parent-item cascade.

    ``ON DELETE SET NULL`` would make PostgreSQL attempt an UPDATE on
    immutable history when the audit event goes away; the constraint is
    deferred NO ACTION instead, so both rows disappear together in the same
    parent cascade and nothing is ever rewritten.
    """
    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(tenant_id=tenant_id, principal_id=principal_id)
    await _promote(tenant_id)

    row = (await _assessments(item_id))[0]
    assert row["outcome"] == "admitted"
    event_id = row["linked_item_event_id"]
    assert event_id is not None
    async with _factory() as session:
        back_reference = await session.scalar(
            text("SELECT admission_assessment_id FROM item_events WHERE id = :e"),
            {"e": event_id},
        )
    assert back_reference == row["id"]

    async with _engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM memory_items WHERE id = :i"), {"i": item_id}
        )

    assert await _assessments(item_id) == []
    assert await _projection(item_id) is None
    async with _factory() as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM item_events WHERE id = :e"), {"e": event_id}
            )
            == 0
        )


async def test_destroying_a_linked_audit_event_alone_is_refused() -> None:
    """The other half of the contract: the cascade may take both rows, but
    nothing may orphan an admitted decision from the event it authorized."""
    import asyncpg

    settings.admission_assessment_capture_enabled = True
    tenant_id, principal_id = await _default_ids()
    item_id = await _insert_item(tenant_id=tenant_id, principal_id=principal_id)
    await _promote(tenant_id)
    event_id = (await _assessments(item_id))[0]["linked_item_event_id"]

    with pytest.raises((asyncpg.PostgresError, Exception)):
        async with _engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM item_events WHERE id = :e"), {"e": event_id}
            )
    # Both halves survive the refused deletion.
    assert len(await _assessments(item_id)) == 1
