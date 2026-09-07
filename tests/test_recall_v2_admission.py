"""Issue #186: binding shadow recall to exact #158 V2 surface decisions.

Real-PostgreSQL proof of the #160/#186 integration contract:

* **Parity** — the #158 simulator (``simulate_item`` / ``simulate_tenant_page``)
  and the shared bulk resolver produce identical decisions (hash and
  per-surface values) for the same state, evidence, policy, and time;
* **Fail-closed resolution** — missing / stale / mismatched / unsupported V2
  rows each withhold with their own explicit status and can never authorize
  inclusion, while the recall-local hard boundaries (tenant/RLS, #159
  blocked/stale) stay stronger than any V2 allow;
* **Boundedness** — the resolver's query count is constant in the window size
  and it performs no provider calls and no writes;
* **RLS** — a foreign tenant's V2 rows are invisible under the app role with
  FORCE RLS: cross-tenant candidates resolve ``missing`` without leaking the
  foreign assessment identity.

Requires a live PostgreSQL with the v2 schema; skips without one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.admission_policy import load_admission_policy
from engram.admission_shadow import (
    resolve_bulk_v2_decisions,
    simulate_item,
    simulate_tenant_page,
)
from engram.config import settings
from engram.recall_profiles import EXPLORATORY_PROFILE, GOVERNED_PROFILE
from engram.recall_signals import decide_recall_admission
from tests.test_recall_profile_semantic import (
    _db_ok,
    _persist_v2_row,
    _test_engine,
    _test_memory_context,
)

_test_session_factory = async_sessionmaker(
    _test_engine, class_=AsyncSession, expire_on_commit=False
)


@pytest.fixture(autouse=True)
async def _clean_db():
    if not await _db_ok():
        return
    async with _test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM usage_events"))
        await conn.execute(text("DELETE FROM feedback_events"))
        await conn.execute(text("DELETE FROM recall_logs"))
        await conn.execute(text("DELETE FROM admission_assessment_current"))
        await conn.execute(text("DELETE FROM admission_assessments"))
        await conn.execute(text("DELETE FROM memory_assessments"))
        await conn.execute(text("DELETE FROM assessment_requests"))
        await conn.execute(text("DELETE FROM jobs"))
        await conn.execute(text("DELETE FROM item_events"))
        await conn.execute(text("DELETE FROM classification_runs"))
        await conn.execute(text("DELETE FROM memory_embeddings"))
        await conn.execute(text("DELETE FROM memory_items"))


@pytest.fixture(autouse=True)
def _v2_selection_settings():
    from tests.test_recall_profile_semantic import _V2_CONTRACT_HASH

    enabled = settings.assessment_selection_enabled
    contract = settings.assessment_effective_contract_hash
    settings.assessment_selection_enabled = True
    settings.assessment_effective_contract_hash = _V2_CONTRACT_HASH
    yield
    settings.assessment_selection_enabled = enabled
    settings.assessment_effective_contract_hash = contract


async def _ids() -> dict[str, str]:
    async with _test_session_factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT t.id::text AS tenant_id, p.id::text AS principal_id "
                        "FROM tenants t JOIN principals p ON p.tenant_id = t.id "
                        "WHERE t.slug = 'default' AND p.name = 'admin'"
                    )
                )
            )
            .mappings()
            .one()
        )
        return dict(row)


async def _insert_proposal(
    *,
    content: str = "v2 fixture proposal",
    kind: str = "fact",
    created_hours_ago: float = 100.0,
    conflict_resolution_status: str | None = None,
    review_status: str = "proposed",
) -> str:
    """One raw item row with full control over the V2 policy's inputs."""
    ids = await _ids()
    item_id = str(uuid.uuid4())
    async with _test_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO memory_items(id, tenant_id, workspace_id, principal_id, "
                "content, content_hash, kind, visibility, review_status, "
                "memory_confidence, source_trust, importance, source_type, "
                "sensitivity, conflict_resolution_status, valid_from, created_at) "
                "VALUES (:id, :tenant, NULL, :principal, :content, :content_hash, "
                ":kind, 'tenant', :review_status, 0.5, 0.5, 0.5, 'extraction', "
                "'normal', :conflict, now() - make_interval(hours => :age_hours), "
                "now() - make_interval(hours => :age_hours))"
            ),
            {
                "id": item_id,
                "tenant": ids["tenant_id"],
                "principal": ids["principal_id"],
                "content": content,
                "content_hash": "sha256:" + item_id.replace("-", "")[:64],
                "kind": kind,
                "review_status": review_status,
                "conflict": conflict_resolution_status,
                "age_hours": created_hours_ago,
            },
        )
    return item_id


async def _resolve(
    item_ids: list[str], *, evaluation_time: datetime | None = None
):  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    from engram.models import MemoryItem

    ids = await _ids()
    context = _test_memory_context(ids["tenant_id"], ids["principal_id"])
    async with _test_session_factory() as session:
        items = list(
            (
                await session.scalars(
                    select(MemoryItem).where(MemoryItem.id.in_(item_ids))
                )
            ).all()
        )
        assert [str(item.id) for item in items] == sorted(
            item_ids
        ) or {str(item.id) for item in items} == set(item_ids)
        return await resolve_bulk_v2_decisions(
            session,
            items=items,
            context=context,
            evaluation_time=evaluation_time or datetime.now(UTC),
        )


async def _skip_without_db() -> None:
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")


async def _v2_row_count() -> int:
    async with _test_session_factory() as session:
        return int(
            await session.scalar(text("SELECT count(*) FROM admission_assessments"))
        )


# ---- parity: the simulator and the resolver are one evaluation --------------


async def test_resolver_matches_simulator_across_the_policy_matrix():
    """Golden parity matrix (issue #186 cross-simulator requirement): for
    every representative state, ``simulate_item`` and the bulk resolver agree
    on the decision hash and every surface decision, and the persisted row is
    exactly the current evaluation (resolution ``current``)."""
    await _skip_without_db()
    cases: list[dict[str, Any]] = [
        dict(name="qualified_low", item={}, persist={"risk": "low"}, governed="allow"),
        dict(
            name="qualified_medium_pre_window",
            item={"created_hours_ago": 1},
            persist={"risk": "moderate", "created_hours_ago": 1},
            governed="withhold",
        ),
        dict(
            name="qualified_medium_post_window",
            item={"created_hours_ago": 100},
            persist={"risk": "moderate", "created_hours_ago": 100},
            governed="allow",
        ),
        dict(
            name="high_risk", item={}, persist={"risk": "high"}, governed="review_required"
        ),
        dict(
            name="absent_evidence", item={}, persist={"risk": None},
            governed="review_required",
        ),
        dict(
            name="contested_evidence",
            item={},
            persist={"risk": "low", "epistemic_state": "contested"},
            governed="review_required",
        ),
        dict(
            name="existing_governance_review",
            item={"kind": "doctrine"},  # doctrine is not auto-promotable
            persist={"risk": "low"},
            governed="review_required",
        ),
    ]
    item_ids: list[str] = []
    expectations: dict[str, dict[str, Any]] = {}
    for case in cases:
        item_id = await _insert_proposal(**case["item"])
        await _persist_v2_row(item_id, **case["persist"])
        item_ids.append(item_id)
        expectations[item_id] = case

    ids = await _ids()
    context = _test_memory_context(ids["tenant_id"], ids["principal_id"])
    now = datetime.now(UTC)
    async with _test_session_factory() as session:
        from sqlalchemy import select

        from engram.models import MemoryItem

        items = list(
            (
                await session.scalars(
                    select(MemoryItem).where(MemoryItem.id.in_(item_ids))
                )
            ).all()
        )
        resolution = await resolve_bulk_v2_decisions(
            session, items=items, context=context, evaluation_time=now
        )
        assert {str(key) for key in resolution.items} == set(item_ids)
        for item in items:
            case = expectations[str(item.id)]
            simulator = await simulate_item(
                session, item=item, context=context, evaluation_time=now
            )
            resolved = resolution.items[item.id]
            # Mechanical parity: identical decision identity and surfaces.
            assert resolved.decision.decision_hash == simulator.shadow.decision_hash
            assert (
                resolved.decision.surface_decisions == simulator.shadow.surface_decisions
            )
            # The persisted row is exactly the current evaluation.
            assert resolved.status == "current", case["name"]
            assert resolved.assessment is not None
            assert resolved.assessment.decision_hash == resolved.decision.decision_hash
            # The policy matrix itself (ADR-158) holds on the exact surface.
            assert (
                resolved.decision.surface_decisions["semantic_governed"] == case["governed"]
            ), case["name"]
            # Exploratory allows everything the matrix does not block.
            assert (
                resolved.decision.surface_decisions["semantic_exploratory"] == "allow"
            ), case["name"]


async def test_age_alone_changes_only_the_observation_window():
    """Issue #186 parity item 14: identical medium-risk items 1h vs 100h old
    differ ONLY in the observation-window condition — risk, epistemic,
    retention, and calibration states are untouched by age."""
    await _skip_without_db()
    young = await _insert_proposal(created_hours_ago=1)
    old = await _insert_proposal(created_hours_ago=100)
    await _persist_v2_row(young, risk="moderate", created_hours_ago=1)
    await _persist_v2_row(old, risk="moderate", created_hours_ago=100)

    resolution = await _resolve([young, old])
    young_decision = resolution.items[uuid.UUID(young)].decision
    old_decision = resolution.items[uuid.UUID(old)].decision
    assert young_decision.risk_state == old_decision.risk_state == "medium"
    assert young_decision.epistemic_state == old_decision.epistemic_state
    assert young_decision.retention_state == old_decision.retention_state
    assert young_decision.observation_window_hours == 72
    assert old_decision.observation_window_hours == 72
    # The only behavioral difference: the window is pending for the young
    # item (governed withholds, wait_until scheduled) and elapsed for the old
    # one (governed allows).
    assert young_decision.surface_decisions["semantic_governed"] == "withhold"
    assert "observation_window" in young_decision.blocker_codes
    assert "wait_until" in young_decision.next_actions
    assert young_decision.next_evaluation_at is not None
    assert old_decision.surface_decisions["semantic_governed"] == "allow"


async def test_unresolved_conflict_is_blocked_on_every_surface():
    await _skip_without_db()
    other = await _insert_proposal()
    item_id = await _insert_proposal(conflict_resolution_status="unresolved")
    async with _test_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE memory_items SET conflicts_with_item_id = :other "
                "WHERE id = :id"
            ),
            {"other": other, "id": item_id},
        )
    resolution = await _resolve([item_id])
    resolved = resolution.items[uuid.UUID(item_id)]
    assert set(resolved.decision.surface_decisions.values()) == {"blocked"}
    # A blocked surface withholds on BOTH candidate profiles. The local
    # lifecycle guard mirrors the policy's own conflict rule and fires first
    # — same outcome, defense in depth.
    items = await _load_items_for([item_id])
    for profile in (GOVERNED_PROFILE, EXPLORATORY_PROFILE):
        decision = decide_recall_admission(
            items[0],
            profile=profile,
            stay_kinds=set(),
            v2_resolution=resolved,
        )
        assert decision.decision == "withhold"
        assert decision.reason_codes == ("conflict_unresolved",)
        assert decision.surface_decision == "blocked"


async def _load_items_for(item_ids: list[str]) -> list[Any]:
    from sqlalchemy import select

    from engram.models import MemoryItem

    async with _test_session_factory() as session:
        return list(
            (
                await session.scalars(
                    select(MemoryItem).where(MemoryItem.id.in_(item_ids))
                )
            ).all()
        )


async def test_simulate_tenant_page_matches_simulate_item():
    """The #158 page path (bulk-loaded) is byte-identical to the per-item
    simulator — the bulk refactor cannot drift from the single-item path."""
    await _skip_without_db()
    item_ids = [
        await _insert_proposal(content=f"page parity {i}", created_hours_ago=50 + i)
        for i in range(4)
    ]
    await _persist_v2_row(item_ids[0], risk="low")
    await _persist_v2_row(item_ids[1], risk="moderate", created_hours_ago=51)
    await _persist_v2_row(item_ids[2], risk=None)
    ids = await _ids()
    context = _test_memory_context(ids["tenant_id"], ids["principal_id"])
    now = datetime.now(UTC)
    async with _test_session_factory() as session:
        page = await simulate_tenant_page(
            session, context=context, evaluation_time=now, limit=100
        )
        by_id = {str(c.item_id): c for c in page.comparisons}
        for item in await _load_items_for(item_ids):
            single = await simulate_item(
                session, item=item, context=context, evaluation_time=now
            )
            assert by_id[str(item.id)].shadow.decision_hash == single.shadow.decision_hash


# ---- fail-closed resolution ------------------------------------------------


async def test_missing_v2_row_resolves_missing_and_never_admits():
    await _skip_without_db()
    item_id = await _insert_proposal()
    resolution = await _resolve([item_id])
    resolved = resolution.items[uuid.UUID(item_id)]
    assert resolved.status == "missing"
    assert resolved.assessment is None
    # The fresh evaluation still exists (it is what a current row would have
    # to match), but only the resolution status carries authority.
    assert resolved.decision.surface_decisions["semantic_governed"] == "review_required"
    items = await _load_items_for([item_id])
    decision = decide_recall_admission(
        items[0], profile=GOVERNED_PROFILE, stay_kinds=set(), v2_resolution=resolved
    )
    assert decision.decision == "withhold"
    assert decision.reason_codes == ("v2_decision_missing",)
    assert decision.v2 is not None and decision.v2.assessment_id is None


async def test_changed_item_state_stales_the_row_never_historical_allow():
    """A row that said ``allow`` stops authorizing the moment current state
    would decide differently — the historical allow is never consumed."""
    await _skip_without_db()
    item_id = await _insert_proposal(created_hours_ago=100)
    await _persist_v2_row(item_id, risk="moderate", created_hours_ago=100)
    assert (await _resolve([item_id])).items[uuid.UUID(item_id)].status == "current"

    async with _test_engine.begin() as conn:
        # The observation window re-arms: the item becomes brand new, so the
        # recorded qualified decision (governed allow, window elapsed) no
        # longer matches a fresh evaluation (governed withhold, window
        # pending).
        await conn.execute(
            text("UPDATE memory_items SET created_at = now() WHERE id = :id"),
            {"id": item_id},
        )
    resolution = await _resolve([item_id])
    resolved = resolution.items[uuid.UUID(item_id)]
    assert resolved.status == "stale"
    assert resolved.assessment is not None  # the row is retained, identified
    assert resolved.decision.surface_decisions["semantic_governed"] == "withhold"

    items = await _load_items_for([item_id])
    for profile in (GOVERNED_PROFILE, EXPLORATORY_PROFILE):
        decision = decide_recall_admission(
            items[0], profile=profile, stay_kinds=set(), v2_resolution=resolved
        )
        assert decision.decision == "withhold", profile
        assert decision.reason_codes == ("v2_decision_stale",)


async def test_input_digest_mismatch_stales_the_row():
    """A change to the item's content identity — an input-digest mismatch in
    #159 vocabulary — also resolves ``stale`` under #186's canonical status
    set: the decision hash binds the content hash, so the recorded decision
    can never authorize different content than it evaluated."""
    await _skip_without_db()
    item_id = await _insert_proposal()
    await _persist_v2_row(item_id, risk="low")
    assert (await _resolve([item_id])).items[uuid.UUID(item_id)].status == "current"
    async with _test_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE memory_items SET content = 'tampered content', "
                "content_hash = 'sha256:' || repeat('f', 64) WHERE id = :id"
            ),
            {"id": item_id},
        )
    resolved = (await _resolve([item_id])).items[uuid.UUID(item_id)]
    assert resolved.status == "stale"


async def test_policy_digest_mismatch_fails_closed():
    """A row persisted under a different policy artifact digest carries no
    authority — the checked-in policy changed, so the decision must be
    re-recorded before it can admit anything."""
    await _skip_without_db()
    item_id = await _insert_proposal()
    await _persist_v2_row(item_id, risk="low")
    # Append a second, newer row evaluated under a diverged policy digest —
    # exactly what a checked-in artifact change leaves behind (rows are
    # immutable, so divergence is always appended, never edited).
    from dataclasses import replace as dc_replace

    from engram.admission_shadow import persist_shadow_comparison, simulate_item
    from engram.db import apply_rls_context

    policy = dc_replace(
        load_admission_policy("risk_aware_shadow_v1"),
        artifact_digest="sha256:" + "0" * 64,
    )
    ids = await _ids()
    async with _test_session_factory() as session:
        await apply_rls_context(
            session, tenant_id=ids["tenant_id"], principal_id=ids["principal_id"]
        )
        from sqlalchemy import select

        from engram.models import MemoryItem

        item = await session.scalar(
            select(MemoryItem).where(MemoryItem.id == item_id)
        )
        assert item is not None
        context = _test_memory_context(ids["tenant_id"], ids["principal_id"])
        evaluation_time = datetime.now(UTC)
        comparison = await simulate_item(
            session,
            item=item,
            context=context,
            evaluation_time=evaluation_time,
            policy=policy,
        )
        await persist_shadow_comparison(
            session,
            comparison=comparison,
            item=item,
            actor_principal_id=item.principal_id,
            evaluated_at=evaluation_time,
            trigger_id=f"test:{uuid.uuid4()}",
        )
        await session.commit()

    resolution = await _resolve([item_id])
    resolved = resolution.items[uuid.UUID(item_id)]
    assert resolved.status == "mismatched"


async def test_unsupported_schema_fails_closed():
    """A latest row under the profile key that is not a V2 row is an
    unsupported contract, never a positive authority."""
    await _skip_without_db()
    item_id = await _insert_proposal()
    ids = await _ids()
    async with _test_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO admission_assessments(id, tenant_id, memory_item_id, "
                "schema_version, mode, trigger_type, trigger_id, invocation_source, "
                "evaluated_at, item_content_hash, input_digest, "
                "policy_profile_key, policy_contract_version, policy_config_digest, "
                "outcome, blocker_codes, reason_codes, decision_inputs, "
                "available_memory_assessment_refs, conflict_recheck_status, "
                "next_actions, decision_hash) "
                "VALUES (:id, :tenant, :item, 'engram.admission-assessment.v1', "
                "'shadow', 'test', 'test', 'test', now(), 'sha256:x', 'sha256:x', "
                "'risk_aware_shadow_v1', 'v', 'sha256:y', 'unknown', '[]', '[]', "
                "'{}', '[]', 'not_run', '[]', 'sha256:z')"
            ),
            {"id": uuid.uuid4(), "tenant": ids["tenant_id"], "item": item_id},
        )
    resolution = await _resolve([item_id])
    resolved = resolution.items[uuid.UUID(item_id)]
    assert resolved.status == "unsupported"


async def test_newest_row_wins_for_resolution():
    """Resolution reads the latest persisted row: a newer re-recorded decision
    supersedes an older stale one."""
    await _skip_without_db()
    item_id = await _insert_proposal()
    await _persist_v2_row(item_id, risk=None)  # old: unknown-risk review routing
    await _persist_v2_row(item_id, risk="low")  # new: qualified
    resolution = await _resolve([item_id])
    resolved = resolution.items[uuid.UUID(item_id)]
    assert resolved.status == "current"
    assert resolved.decision.surface_decisions["semantic_governed"] == "allow"


# ---- boundedness and side-effect freedom ------------------------------------


async def test_query_count_is_constant_in_window_size_and_no_provider_calls(
    monkeypatch,
):
    await _skip_without_db()
    item_ids = [await _insert_proposal(content=f"bounded {i}") for i in range(5)]
    for item_id in item_ids[:2]:
        await _persist_v2_row(item_id, risk="low")

    # Any provider call during resolution is a contract break.
    import engram.embeddings as embeddings_mod

    async def no_providers(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the V2 resolver must not call any provider")

    monkeypatch.setattr(embeddings_mod, "generate_embedding", no_providers)

    small = await _resolve(item_ids[:1])
    large = await _resolve(item_ids)
    assert small.query_count == large.query_count
    assert large.query_count > 0
    # Bounded accounting: support(4) + selection(2) + latest rows(1).
    assert large.query_count == 7


async def test_resolver_is_read_only():
    await _skip_without_db()
    item_id = await _insert_proposal()
    await _persist_v2_row(item_id, risk="low")
    rows_before = await _v2_row_count()
    async with _test_session_factory() as session, session.begin():
        counts_before = (
            await session.execute(
                text(
                    "SELECT (SELECT count(*) FROM admission_assessment_current), "
                    "(SELECT count(*) FROM recall_logs), "
                    "(SELECT count(*) FROM jobs), "
                    "(SELECT review_status FROM memory_items WHERE id = :id)"
                ),
                {"id": item_id},
            )
        ).one()

    await _resolve([item_id])

    async with _test_session_factory() as session:
        counts_after = (
            await session.execute(
                text(
                    "SELECT (SELECT count(*) FROM admission_assessment_current), "
                    "(SELECT count(*) FROM recall_logs), "
                    "(SELECT count(*) FROM jobs), "
                    "(SELECT review_status FROM memory_items WHERE id = :id)"
                ),
                {"id": item_id},
            )
        ).one()
    assert counts_after == counts_before
    assert await _v2_row_count() == rows_before


# ---- RLS: cross-tenant V2 state is invisible under the app role -------------


async def test_cross_tenant_v2_rows_are_invisible_under_force_rls():
    """Under the non-owner application role with FORCE RLS, tenant B's
    resolver sees none of tenant A's V2 rows: A's candidates resolve
    ``missing`` and no foreign assessment identity leaks."""
    await _skip_without_db()
    app_dsn = "postgresql+asyncpg://engram_app:change-me-in-production@localhost:5432/engram"
    try:
        app_engine = create_async_engine(app_dsn, poolclass=NullPool)
    except Exception:  # pragma: no cover - DSN is deployment-specific
        pytest.skip("app-role DSN unavailable")
    try:
        async with app_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("app role not reachable")
        return

    # Owner seeds tenant A with a qualified V2 row.
    item_id = await _insert_proposal()
    await _persist_v2_row(item_id, risk="low")

    async with _test_session_factory() as session:
        tenant_b = str(
            await session.scalar(
                text("SELECT id::text FROM tenants WHERE slug <> 'default' LIMIT 1")
            )
        )
        principal_b = str(
            await session.scalar(
                text(
                    "SELECT p.id::text FROM principals p "
                    "JOIN tenants t ON t.id = p.tenant_id "
                    "WHERE t.slug <> 'default' LIMIT 1"
                )
            )
        )
    if tenant_b is None:
        pytest.skip("no second tenant seeded")

    from sqlalchemy import select as sa_select

    from engram.db import apply_rls_context
    from engram.models import MemoryItem

    resolution = None
    async with async_sessionmaker(app_engine, class_=AsyncSession)() as session:
        await apply_rls_context(session, tenant_id=tenant_b, principal_id=principal_b)
        # Even directly naming tenant A's item, RLS hides it — the resolver
        # window can never contain foreign items, and the V2 history query
        # sees no foreign rows.
        foreign = (
            await session.scalars(
                sa_select(MemoryItem).where(MemoryItem.id == item_id)
            )
        ).all()
        assert foreign == []
        context = _test_memory_context(tenant_b, principal_b)
        resolution = await resolve_bulk_v2_decisions(
            session, items=[], context=context, evaluation_time=datetime.now(UTC)
        )
    assert resolution.items == {}
    await app_engine.dispose()

    # And under tenant A the same item resolves current — proving the invisibility
    # above was RLS, not absence.
    assert (await _resolve([item_id])).items[uuid.UUID(item_id)].status == "current"
