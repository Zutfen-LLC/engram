"""PostgreSQL proofs for the read-only recall evaluation framework (#198).

These tests require a live PostgreSQL with the v2 schema (migrations/) and
pgvector. They skip automatically when no DB is reachable, mirroring
tests/test_recall_profile_semantic.py. Embeddings are deterministic fakes so
CI never depends on OpenAI.

The regressions pinned here are the #198 framework obligations:

* repeated exposure without new qualifying feedback changes nothing that
  determines a packet — not explicit priority, usefulness, utility,
  epistemic/evidence state, V2 admission, review, or lifecycle state — and
  the shared candidate path writes no exposure state at all;
* the demonstrated-usefulness qualification matrix (states, adjustments,
  exclusions, non-rescue, ordering-only effect) through the real PostgreSQL
  usefulness loader and the shared candidate path;
* bounded query-delta contracts for admitted-candidate count, qualifying
  feedback actor count, and relationship/packing edge count (no N+1);
* the evaluation runner's per-case statement accounting — including the
  neutral-usefulness counterfactual — reconciles with the transaction total;
* deterministic replay through frozen query embeddings is independent of
  later provider output;
* real packing omissions surface under the exact ``packing["omitted"]``
  contract;
* V2 resolution states count each resolved item exactly once;
* drifting any outcome-affecting runtime setting invalidates replay against
  the previously frozen snapshot identity.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.admission_policy import load_admission_policy
from engram.api.app import create_app
from engram.config import settings
from engram.db import get_session
from engram.demonstrated_usefulness import load_demonstrated_usefulness
from engram.models import MemoryItem
from engram.recall_shadow import evaluate_recall_shadow_comparison

_test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_test_session_factory = async_sessionmaker(
    _test_engine, class_=AsyncSession, expire_on_commit=False
)


async def _db_ok() -> bool:
    try:
        async with _test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _skip_without_db() -> None:
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")


async def _get_test_session() -> AsyncIterator[AsyncSession]:
    async with _test_session_factory() as session:
        from engram.db import (
            _DEFAULT_PRINCIPAL_NAME,
            _DEFAULT_TENANT_SLUG,
            apply_rls_context,
        )

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


@pytest.fixture(autouse=True)
async def _clean_db():
    if not await _db_ok():
        return
    async with _test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM context_receipts"))
        await conn.execute(text("DELETE FROM usage_events"))
        await conn.execute(text("DELETE FROM feedback_events"))
        await conn.execute(text("DELETE FROM recall_logs"))
        await conn.execute(text("DELETE FROM admission_assessment_current"))
        await conn.execute(text("DELETE FROM admission_assessments"))
        await conn.execute(text("DELETE FROM jobs"))
        await conn.execute(text("DELETE FROM item_events"))
        await conn.execute(text("DELETE FROM classification_runs"))
        await conn.execute(text("DELETE FROM memory_edges"))
        await conn.execute(text("DELETE FROM tunnels"))
        await conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'eval198-%'"))
        await conn.execute(text("DELETE FROM memory_embeddings"))
        await conn.execute(text("DELETE FROM memory_items"))
        await conn.execute(text("DELETE FROM principals WHERE name LIKE 'eval198-%'"))
        await conn.execute(
            text(
                "UPDATE tenant_config SET recall_profile_shadow_enabled = FALSE "
                "WHERE tenant_id = (SELECT id FROM tenants WHERE slug = 'default')"
            )
        )


_V2_POLICY = load_admission_policy("risk_aware_shadow_v1")
_V2_CONTRACT_HASH = sorted(_V2_POLICY.accepted_contract_hashes)[0]

_CALIBRATED = {
    "raw_value": 0.9,
    "status": "calibrated",
    "calibrated_value": 0.9,
    "calibrated_band": [0.8, 1.0],
    "profile_version": "test-profile-v1",
    "dataset_version": "test-dataset-v1",
}

_TARGET_VEC = [1.0] + [0.0] * 1535
_DISTRACTOR_VEC = [0.0, 1.0] + [0.0] * 1534

_TARGET_PREFIXES = ("semantic target", "semantic query", "proposed target")
_FROZEN_NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)


def _fake_embedding_for(text_value: str) -> list[float]:
    if text_value.startswith(_TARGET_PREFIXES):
        return _TARGET_VEC
    return _DISTRACTOR_VEC


@pytest.fixture(autouse=True)
def _fixed_evaluator_settings(monkeypatch: pytest.MonkeyPatch):
    """Bind the outcome-affecting settings the identity tests rely on."""
    for field, value in (
        ("embedding_provider", "openai"),
        ("conflict_check_on_write", False),
        ("assessment_selection_enabled", True),
        ("assessment_effective_contract_hash", _V2_CONTRACT_HASH),
        ("relationship_expansion_enabled", True),
    ):
        monkeypatch.setattr(settings, field, value, raising=True)


def _patch_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_embedding(
        text_value: str, *_args: object, **_kwargs: object
    ) -> list[float] | None:
        return _fake_embedding_for(text_value)

    import engram.embeddings as embeddings_mod
    from engram import recall as recall_mod
    from engram.api.routes import memory as memory_routes

    monkeypatch.setattr(recall_mod, "generate_embedding", fake_embedding)
    monkeypatch.setattr(memory_routes, "generate_embedding", fake_embedding)
    monkeypatch.setattr(embeddings_mod, "generate_embedding", fake_embedding)


async def _drain_jobs(max_iterations: int = 10) -> None:
    from engram.worker import process_one_job

    for _ in range(max_iterations):
        processed = await process_one_job(
            worker_id="test",
            session_factory=_test_session_factory,
            app_session_factory=_test_session_factory,
            job_types=["embedding.generate"],
        )
        if not processed:
            return


async def _remember(
    client: AsyncClient, content: str, **payload: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {"content": content, "source_type": "manual"}
    body.update(payload)
    resp = await client.post("/v1/remember", json=body)
    assert resp.status_code == 201, resp.text
    await _drain_jobs()
    return resp.json()


def _test_memory_context(tenant_id: str, principal_id: str) -> Any:
    from engram.memory_context import MEMORY_CONTEXT_VERSION, ResolvedMemoryContext

    return ResolvedMemoryContext(
        version=MEMORY_CONTEXT_VERSION,
        tenant_id=UUID(tenant_id),
        principal_id=UUID(principal_id),
        api_key_id=None,
        memory_profile_id=None,
        memory_profile_revision_id=None,
        memory_profile_slug=None,
        memory_profile_version=None,
        include_private=True,
        include_tenant=True,
        include_public=True,
        readable_workspace_ids=None,
        allow_tenant_write=True,
        allow_public_write=True,
        default_write_visibility="private",
        default_write_workspace_id=None,
        writable_workspace_ids=None,
    )


async def _default_ids() -> dict[str, str]:
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


async def _persist_v2_row(
    item_id: str,
    *,
    risk: str | None = "low",
    epistemic_state: str = "supported",
    retention_disposition: str = "retain",
    evaluation_time: datetime | None = None,
) -> None:
    """Give one live proposal a qualifying #157 assessment and a V2 row.

    ``evaluation_time`` pins both the persisted row and the shadow decision;
    comparisons evaluated at the same frozen time resolve the row
    ``current`` deterministically.
    """
    from engram.admission_shadow import persist_shadow_comparison, simulate_item
    from engram.assessments import evidence_snapshot
    from engram.db import apply_rls_context
    from engram.extraction import digest
    from engram.models import MemoryAssessment

    async with _test_session_factory() as session:
        ids = await _default_ids()
        await apply_rls_context(
            session, tenant_id=ids["tenant_id"], principal_id=ids["principal_id"]
        )
        item = await session.scalar(select(MemoryItem).where(MemoryItem.id == item_id))
        assert item is not None
        context = _test_memory_context(ids["tenant_id"], ids["principal_id"])
        if risk is not None:
            run_values = (
                (
                    await session.execute(
                        text(
                            "SELECT content_hash, source_type, kind, created_at "
                            "FROM memory_items WHERE id = :id"
                        ),
                        {"id": item.id},
                    )
                )
                .mappings()
                .one()
            )
            run_id = uuid4()
            await session.execute(
                text(
                    "INSERT INTO classification_runs(id, tenant_id, principal_id, "
                    "memory_item_id, bound_at, content_hash, canonicalization_version, "
                    "source_type, suggested_kind, taxonomy_confidence, retention_confidence, "
                    "retention_disposition, reason, provenance, classification_version, "
                    "retention_policy_version, created_at, expires_at) "
                    "VALUES (:id, :tenant_id, :principal_id, :item, now(), :content_hash, "
                    "'v1', :source_type, :kind, 0.9, 0.9, 'retain', 'test', '{}'::jsonb, "
                    "'classification-v2', 'retention-v1', :created_at, "
                    "now() + interval '1 year')"
                ),
                {
                    "id": run_id,
                    "tenant_id": item.tenant_id,
                    "principal_id": item.principal_id,
                    "item": item.id,
                    "content_hash": run_values["content_hash"],
                    "source_type": run_values["source_type"],
                    "kind": run_values["kind"],
                    "created_at": run_values["created_at"],
                },
            )
            snapshot = await evidence_snapshot(session, item, context)
            receipt = {
                "schema_version": "engram.assessment.v1",
                "dimensions": {
                    "taxonomy": _CALIBRATED,
                    "suggested_kind": item.kind,
                    "retention": _CALIBRATED,
                    "retention_disposition": retention_disposition,
                    "epistemic": _CALIBRATED,
                    "epistemic_state": epistemic_state,
                    "risk": risk,
                    "assertion_mode": "direct_statement",
                    "origin": "user",
                    "reason_codes": [],
                },
            }
            session.add(
                MemoryAssessment(
                    id=uuid4(),
                    tenant_id=item.tenant_id,
                    memory_item_id=item.id,
                    legacy_run_id=run_id,
                    attempt=0,
                    purpose="combined",
                    contract_hash=_V2_CONTRACT_HASH,
                    input_digest=digest(snapshot),
                    state="completed",
                    receipt=receipt,
                    canonical_hash="sha256:" + "0" * 64,
                )
            )
            await session.flush()
        evaluation_time = evaluation_time or datetime.now(UTC)
        comparison = await simulate_item(
            session, item=item, context=context, evaluation_time=evaluation_time
        )
        await persist_shadow_comparison(
            session,
            comparison=comparison,
            item=item,
            actor_principal_id=item.principal_id,
            evaluated_at=evaluation_time,
            trigger_id=f"test:{uuid4()}",
        )
        await session.commit()


async def _backdate(item_id: str, *, hours: float = 48.0) -> None:
    """Give an item positive age at the frozen evaluation time.

    The frozen comparison evaluates at ``_FROZEN_NOW``; items created at the
    real clock could be newer than that (negative age flips time-dependent
    policy branches). Backdating makes every fixture's age deterministic.
    """
    async with _test_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE memory_items SET created_at = :anchor, "
                "valid_from = :anchor WHERE id = :id"
            ),
            {
                "anchor": _FROZEN_NOW - timedelta(hours=hours),
                "id": item_id,
            },
        )


async def _seed_qualified(
    client: AsyncClient, content: str, **payload: Any
) -> str:
    item = await _remember(client, content, source_type="extraction", **payload)
    await _backdate(item["id"])
    await _persist_v2_row(item["id"], evaluation_time=_FROZEN_NOW)
    return item["id"]


# ---- shared statement measurement -------------------------------------------


@contextmanager
def _measured_statements(session: AsyncSession) -> dict[str, int]:
    """Count actual DBAPI executions on this session's engine."""
    count = {"total": 0}
    engine = session.sync_session.get_bind()

    def observe(*_args: Any, **_kwargs: Any) -> None:
        count["total"] += 1

    event.listen(engine, "before_cursor_execute", observe)
    try:
        yield count
    finally:
        event.remove(engine, "before_cursor_execute", observe)


async def _run_comparison(
    *,
    byte_budget: int | None = None,
    token_budget: int | None = None,
    item_budget: int | None = None,
    neutralize: bool = False,
) -> tuple[dict[str, Any], int]:
    """One shared candidate-path comparison with a fixed frozen embedding."""
    ids = await _default_ids()
    context = _test_memory_context(ids["tenant_id"], ids["principal_id"])
    async with _test_session_factory() as session:
        from engram.db import apply_rls_context

        await apply_rls_context(
            session, tenant_id=ids["tenant_id"], principal_id=ids["principal_id"]
        )
        with _measured_statements(session) as statements:
            result = await evaluate_recall_shadow_comparison(
                session,
                memory_context=context,
                workspace=None,
                query="semantic query",
                candidate_profiles=["governed", "exploratory"],
                byte_budget=byte_budget,
                token_budget=token_budget,
                item_budget=item_budget,
                now=_FROZEN_NOW,
                query_embedding_override=_TARGET_VEC,
                neutralize_demonstrated_usefulness=neutralize,
            )
        return result, statements["total"]


def _candidate(result: dict[str, Any], profile: str) -> dict[str, Any]:
    return next(entry for entry in result["candidates"] if entry["profile"] == profile)


def _packet_item(packet: dict[str, Any], item_id: str) -> dict[str, Any]:
    return next(item for item in packet["items"] if item["id"] == item_id)


# ---- item/exposure state capture --------------------------------------------


_EXPOSURE_COLUMNS = ("recall_count", "startup_recall_count", "last_recalled_at")


async def _frozen_db_state(item_id: str) -> dict[str, str]:
    """Every memory_items column except the intentionally mutated exposure set."""
    async with _test_session_factory() as session:
        row = (
            (
                await session.execute(
                    text("SELECT * FROM memory_items WHERE id = :id"), {"id": item_id}
                )
            )
            .mappings()
            .one()
        )
        return {
            key: str(value)
            for key, value in dict(row).items()
            if key not in _EXPOSURE_COLUMNS
        }


async def _exposure_state(item_id: str) -> dict[str, str]:
    async with _test_session_factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT recall_count, startup_recall_count, "
                        "last_recalled_at::text FROM memory_items WHERE id = :id"
                    ),
                    {"id": item_id},
                )
            )
            .mappings()
            .one()
        )
        return dict(row)


def _packet_frozen_fields(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item.get(key)
        for key in (
            "score",
            "relevance_score",
            "utility_score",
            "utility",
            "epistemic_state",
            "evidence",
            "admission",
            "warnings",
            "warning_codes",
            "packing_reason",
            "review_status",
            "relationship",
        )
    }


# ---- recall-log/feedback fixtures -------------------------------------------


async def _seed_actor(name: str, *, tenant_id: str | None = None) -> str:
    actor_tenant = tenant_id or (await _default_ids())["tenant_id"]
    principal_id = uuid4()
    async with _test_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, created_at) "
                "VALUES (:id, :tenant, :name, now())"
            ),
            {"id": principal_id, "tenant": actor_tenant, "name": name},
        )
    return str(principal_id)


async def _insert_recall_log(
    principal_id: str,
    item_ids: list[str] | None,
    *,
    tenant_id: str | None = None,
) -> str:
    log_id = uuid4()
    effective_tenant = tenant_id or (await _default_ids())["tenant_id"]
    async with _test_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO recall_logs (id, tenant_id, principal_id, mode, query, "
                "item_ids, recall_profile, created_at) VALUES (:id, :tenant, "
                ":principal, 'semantic', 'semantic query', :items, 'legacy', now())"
            ),
            {
                "id": log_id,
                "tenant": effective_tenant,
                "principal": principal_id,
                "items": (
                    [UUID(str(value)) for value in item_ids] if item_ids is not None else None
                ),
            },
        )
    return str(log_id)


async def _insert_feedback(
    item_id: str,
    principal_id: str,
    verdict: str,
    recall_log_id: str | None,
    *,
    tenant_id: str | None = None,
) -> str:
    feedback_id = uuid4()
    effective_tenant = tenant_id or (await _default_ids())["tenant_id"]
    async with _test_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO feedback_events (id, tenant_id, item_id, principal_id, "
                "verdict, recall_log_id, created_at) VALUES (:id, :tenant, :item, "
                ":principal, :verdict, :log, now())"
            ),
            {
                "id": feedback_id,
                "tenant": effective_tenant,
                "item": item_id,
                "principal": principal_id,
                "verdict": verdict,
                "log": UUID(recall_log_id) if recall_log_id else None,
            },
        )
    return str(feedback_id)


async def _bound_useful_feedback(item_id: str, actor: str) -> None:
    """One qualifying external useful verdict: actor saw the item, then rated."""
    log_id = await _insert_recall_log(actor, [item_id])
    await _insert_feedback(item_id, actor, "useful", log_id)


async def _usefulness_of(item_id: str) -> Any:
    ids = await _default_ids()
    async with _test_session_factory() as session:
        from engram.db import apply_rls_context

        await apply_rls_context(
            session, tenant_id=ids["tenant_id"], principal_id=ids["principal_id"]
        )
        item = await session.scalar(select(MemoryItem).where(MemoryItem.id == item_id))
        assert item is not None
        summaries = await load_demonstrated_usefulness(
            session, tenant_id=UUID(ids["tenant_id"]), items=[item]
        )
        return summaries[item.id]


# ---- 1. repeated-exposure invariance ----------------------------------------


async def test_repeated_exposure_changes_nothing_the_packet_depends_on(
    client, monkeypatch
):
    """The #198 counterfactual: materially different exposure state, no new
    qualifying feedback, identical packet through the shared candidate path."""
    await _skip_without_db()
    _patch_embeddings(monkeypatch)

    target_id = await _seed_qualified(client, "semantic target exposure primary")
    other_id = await _seed_qualified(client, "semantic target exposure sibling")
    # One qualifying external useful verdict exists for the target — the
    # counterfactual must keep it and never amplify or erode it.
    actor = await _seed_actor("eval198-exposure-actor")
    await _bound_useful_feedback(target_id, actor)

    before_db = await _frozen_db_state(target_id)
    before_exposure = await _exposure_state(target_id)
    before_usefulness = await _usefulness_of(target_id)
    first, _statements = await _run_comparison()
    before_governed = _candidate(first, "governed")
    before_item = _packet_frozen_fields(_packet_item(before_governed, target_id))
    before_order = [item["id"] for item in before_governed["items"]]

    assert before_usefulness.state == "positive"
    assert before_usefulness.adjustment == 0.10

    # Mutate ONLY exposure state: materially different counters/timestamps.
    async with _test_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE memory_items SET recall_count = recall_count + 7, "
                "startup_recall_count = startup_recall_count + 4, "
                "last_recalled_at = now() + interval '1 hour' "
                "WHERE id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": [UUID(target_id), UUID(other_id)]},
        )
    after_exposure = await _exposure_state(target_id)
    assert after_exposure != before_exposure

    after_db = await _frozen_db_state(target_id)
    after_usefulness = await _usefulness_of(target_id)
    second, _statements = await _run_comparison()
    after_governed = _candidate(second, "governed")
    after_item = _packet_frozen_fields(_packet_item(after_governed, target_id))
    after_order = [item["id"] for item in after_governed["items"]]

    # Every non-exposure DB column — explicit priority, review status,
    # lifecycle/promotion fields, evidence columns — is unchanged.
    assert after_db == before_db
    # Demonstrated usefulness did not move.
    assert after_usefulness == before_usefulness
    # Utility, final score, ordering, epistemic/evidence state, V2 admission
    # state and reasons, packing: all byte-identical.
    assert after_item == before_item
    assert after_order == before_order
    # Explicit priority is literally unchanged in the utility payload.
    assert after_item["utility"]["explicit_priority"] == before_item["utility"][
        "explicit_priority"
    ]
    assert after_item["utility"]["demonstrated_usefulness"]["state"] == "positive"
    assert after_item["admission"]["decision"] == "admit"
    assert after_item["admission"]["v2"]["resolution_status"] == "current"


async def test_shadow_candidate_path_writes_no_exposure_state(client, monkeypatch):
    """Separately: the shared candidate path bumps no counters, writes no
    recall logs, and leaves ordinary serving unchanged."""
    await _skip_without_db()
    _patch_embeddings(monkeypatch)

    target_id = await _seed_qualified(client, "semantic target no write")

    async def _snapshot() -> dict[str, Any]:
        async with _test_session_factory() as session:
            exposure = (
                await session.execute(
                    text(
                        "SELECT recall_count, startup_recall_count, "
                        "last_recalled_at::text FROM memory_items WHERE id = :id"
                    ),
                    {"id": target_id},
                )
            ).mappings().one()
            logs = await session.scalar(text("SELECT count(*) FROM recall_logs"))
            feedback = await session.scalar(text("SELECT count(*) FROM feedback_events"))
            items = await session.scalar(text("SELECT count(*) FROM memory_items"))
            return {
                "exposure": dict(exposure),
                "logs": int(logs or 0),
                "feedback": int(feedback or 0),
                "items": int(items or 0),
            }

    before = await _snapshot()
    for _ in range(3):
        await _run_comparison()
        await _run_comparison(neutralize=True)
    after = await _snapshot()

    assert after == before


# ---- 2. usefulness qualification matrix -------------------------------------


async def _seed_matrix_items(client: AsyncClient) -> dict[str, str]:
    items = {
        "none": await _seed_qualified(client, "semantic target matrix none"),
        "positive": await _seed_qualified(client, "semantic target matrix positive"),
        "negative": await _seed_qualified(client, "semantic target matrix negative"),
        "mixed": await _seed_qualified(client, "semantic target matrix mixed"),
        "single": await _seed_qualified(client, "semantic target matrix single"),
        "many": await _seed_qualified(client, "semantic target matrix many"),
        "author": await _seed_qualified(client, "semantic target matrix author"),
        "nolog": await _seed_qualified(client, "semantic target matrix nolog"),
        "nullitems": await _seed_qualified(client, "semantic target matrix nullitems"),
        "wrongprincipal": await _seed_qualified(
            client, "semantic target matrix wrongprincipal"
        ),
        "wrongtenant": await _seed_qualified(
            client, "semantic target matrix wrongtenant"
        ),
        "absent": await _seed_qualified(client, "semantic target matrix absent"),
        "withheld": (
            await _remember(
                client, "proposed target matrix withheld", source_type="extraction"
            )
        )["id"],
        "orderlow": await _seed_qualified(
            client, "semantic target matrix orderlow", importance=0.45
        ),
        "orderhigh": await _seed_qualified(
            client, "semantic target matrix orderhigh", importance=0.55
        ),
    }
    await _backdate(items["withheld"])
    return items


async def _packet_usefulness(result: dict[str, Any], item_id: str) -> dict[str, Any]:
    governed = _candidate(result, "governed")
    item = _packet_item(governed, item_id)
    utility = item["utility"]["demonstrated_usefulness"]
    return {
        "state": utility["state"],
        "adjustment": utility["adjustment"],
        "useful_actors": utility["qualifying_useful_actor_count"],
        "noise_actors": utility["qualifying_noise_actor_count"],
        "excluded_self_or_author": utility["excluded_self_or_author_count"],
        "excluded_unbound": utility["excluded_unbound_exposure_count"],
        "utility_score": item["utility_score"],
        "base_utility": item["utility"]["base_utility"],
    }


async def test_usefulness_qualification_matrix_through_real_postgres(client, monkeypatch):
    """The full #198 bounded perturbation matrix on the real loader + path."""
    await _skip_without_db()
    _patch_embeddings(monkeypatch)

    items = await _seed_matrix_items(client)
    default = await _default_ids()

    # positive: one external bound useful actor
    actor_a = await _seed_actor("eval198-actor-a")
    await _bound_useful_feedback(items["positive"], actor_a)
    # negative: one external bound noise actor
    await _insert_feedback(
        items["negative"], actor_a, "noise", await _insert_recall_log(actor_a, [items["negative"]])
    )
    # mixed: both signs, two actors
    actor_b = await _seed_actor("eval198-actor-b")
    await _bound_useful_feedback(items["mixed"], actor_b)
    await _insert_feedback(
        items["mixed"], actor_a, "noise", await _insert_recall_log(actor_a, [items["mixed"]])
    )
    # single vs many: same sign, one actor vs six actors
    await _bound_useful_feedback(items["single"], actor_a)
    for index in range(6):
        actor = await _seed_actor(f"eval198-actor-many-{index}")
        await _bound_useful_feedback(items["many"], actor)
    # author/self: the item's own principal (the default admin author)
    author_log = await _insert_recall_log(default["principal_id"], [items["author"]])
    await _insert_feedback(items["author"], default["principal_id"], "useful", author_log)
    # missing recall log entirely
    await _insert_feedback(items["nolog"], actor_a, "useful", None)
    # recall log with NULL item_ids
    null_log = await _insert_recall_log(actor_a, None)
    await _insert_feedback(items["nullitems"], actor_a, "useful", null_log)
    # recall log owned by a different principal than the feedback actor
    wrong_log = await _insert_recall_log(actor_b, [items["wrongprincipal"]])
    await _insert_feedback(items["wrongprincipal"], actor_a, "useful", wrong_log)
    # wrong tenant: a second tenant's principal rates our item
    other_tenant = uuid4()
    async with _test_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"
            ),
            {"id": other_tenant, "name": "eval198-other", "slug": "eval198-other"},
        )
    other_actor = await _seed_actor(
        "eval198-actor-other", tenant_id=str(other_tenant)
    )
    await _insert_feedback(
        items["wrongtenant"],
        other_actor,
        "useful",
        None,
        tenant_id=str(other_tenant),
    )
    # item absent from the recall log's item_ids
    absent_log = await _insert_recall_log(actor_a, [items["none"]])
    await _insert_feedback(items["absent"], actor_a, "useful", absent_log)
    # withheld: no V2 row for this proposal, but a qualifying useful verdict
    await _bound_useful_feedback(items["withheld"], actor_a)

    # The real PostgreSQL loader first.
    expected_states = {
        "none": ("none", 0.0),
        "positive": ("positive", 0.10),
        "negative": ("negative", -0.10),
        "mixed": ("mixed", 0.0),
        "single": ("positive", 0.10),
        "many": ("positive", 0.10),
        "author": ("none", 0.0),
        "nolog": ("none", 0.0),
        "nullitems": ("none", 0.0),
        "wrongprincipal": ("none", 0.0),
        "wrongtenant": ("none", 0.0),
        "absent": ("none", 0.0),
    }
    for name, (state, adjustment) in expected_states.items():
        summary = await _usefulness_of(items[name])
        assert summary.state == state, name
        assert summary.adjustment == adjustment, name

    single = await _usefulness_of(items["single"])
    many = await _usefulness_of(items["many"])
    assert single.adjustment == many.adjustment
    assert single.qualifying_useful_actor_count == 1
    assert many.qualifying_useful_actor_count == 6
    author_summary = await _usefulness_of(items["author"])
    assert author_summary.excluded_self_or_author_count == 1
    nolog_summary = await _usefulness_of(items["nolog"])
    assert nolog_summary.excluded_unbound_exposure_count == 1

    # Then the shared candidate path end to end. Noise verdicts are a
    # promotion-support input to the #158 policy (by design), so the two
    # noise-rated items re-resolve stale and are withheld; their usefulness
    # states are proven above at the loader. Every other matrix item is
    # admitted and must carry the same state in its packet utility payload.
    result, _statements = await _run_comparison()
    governed = _candidate(result, "governed")
    packet_ids = {item["id"] for item in governed["items"]}
    admitted_names = set(expected_states) - {"negative", "mixed"}
    for name in admitted_names:
        assert items[name] in packet_ids, name
        packet_utility = await _packet_usefulness(result, items[name])
        assert packet_utility["state"] == expected_states[name][0], name
        assert packet_utility["adjustment"] == expected_states[name][1], name
    noise_withheld = {
        entry["item_id"] for entry in governed["admission_diagnostics"]
    }
    assert noise_withheld == {items["negative"], items["mixed"], items["withheld"]}
    assert governed["v2_resolution"]["resolution_status_counts"]["stale"] == 2

    single_packet = await _packet_usefulness(result, items["single"])
    many_packet = await _packet_usefulness(result, items["many"])
    assert single_packet["adjustment"] == many_packet["adjustment"]
    assert (
        abs(single_packet["utility_score"] - many_packet["utility_score"]) < 1e-9
    )  # identical magnitude, not scaled by actor count


async def test_usefulness_cannot_rescue_a_withheld_item(client, monkeypatch):
    """A qualifying useful verdict never changes a V2 withhold."""
    await _skip_without_db()
    _patch_embeddings(monkeypatch)

    item_id = (
        await _remember(client, "proposed target rescue attempt", source_type="extraction")
    )["id"]
    # No V2 row was persisted for this live proposal: the governed surface
    # resolves it missing and withholds regardless of usefulness.
    actor = await _seed_actor("eval198-actor-rescue")
    await _bound_useful_feedback(item_id, actor)
    assert (await _usefulness_of(item_id)).state == "positive"

    result, _statements = await _run_comparison()
    governed = _candidate(result, "governed")

    assert item_id not in {item["id"] for item in governed["items"]}
    withheld = next(
        (
            entry
            for entry in governed["admission_diagnostics"]
            if entry["item_id"] == item_id
        ),
        None,
    )
    assert withheld is not None
    assert withheld["v2_resolution_status"] == "missing"
    # The excluded unbound counter never ran: feedback qualified, admission
    # still withheld. Usefulness is post-admission only.
    assert governed["v2_resolution"]["resolution_status_counts"].get("missing") == 1


async def test_usefulness_changes_only_post_admission_ordering_and_packing(
    client, monkeypatch
):
    """Positive usefulness reorders admitted items but never admission."""
    await _skip_without_db()
    _patch_embeddings(monkeypatch)

    items = await _seed_matrix_items(client)
    low, high = items["orderlow"], items["orderhigh"]
    actor = await _seed_actor("eval198-actor-order")
    # The LOW-priority item earns positive usefulness so it can outrank the
    # high-priority one only through the bounded adjustment.
    await _bound_useful_feedback(low, actor)

    actual, _statements = await _run_comparison()
    neutral, _neutral_statements = await _run_comparison(neutralize=True)

    governed = _candidate(actual, "governed")
    neutral_governed = _candidate(neutral, "governed")
    actual_ids = [item["id"] for item in governed["items"]]
    neutral_ids = [item["id"] for item in neutral_governed["items"]]

    # Membership/admission identical; only order moved.
    assert set(actual_ids) == set(neutral_ids)
    low_neutral = neutral_ids.index(low)
    low_actual = actual_ids.index(low)
    high_neutral = neutral_ids.index(high)
    assert low_neutral > high_neutral  # without usefulness: high first
    assert low_actual < low_neutral  # usefulness raised the low item
    # No item crossed the admission boundary in either direction.
    assert governed["omitted_by_admission"] == neutral_governed["omitted_by_admission"]
    assert governed["admission_diagnostics"] == neutral_governed[
        "admission_diagnostics"
    ]
    assert governed["v2_resolution"] == neutral_governed["v2_resolution"]


# ---- 3. N+1 / scaling regressions -------------------------------------------


async def test_statement_count_is_bounded_in_admitted_candidate_count(
    client, monkeypatch
):
    await _skip_without_db()
    _patch_embeddings(monkeypatch)

    for index in range(3):
        await _seed_qualified(client, f"semantic target scale small {index:02d}")
    _small, small_statements = await _run_comparison()

    for index in range(9):
        await _seed_qualified(client, f"semantic target scale large {index:02d}")
    _large, large_statements = await _run_comparison()

    # 4x the admitted candidates: the whole comparison (legacy + governed +
    # exploratory: preflight counts, retrieval, V2 bulk resolution, usefulness
    # loader, packing relations) executes a bounded, non-per-item number of
    # statements.
    assert abs(large_statements - small_statements) <= 2


async def test_statement_count_is_constant_in_qualifying_feedback_actors(
    client, monkeypatch
):
    await _skip_without_db()
    _patch_embeddings(monkeypatch)

    target_id = await _seed_qualified(client, "semantic target feedback scaling")
    _before, before_statements = await _run_comparison()

    # 8 additional qualifying external actors: one aggregate loader query,
    # never one query per actor.
    for index in range(8):
        actor = await _seed_actor(f"eval198-actor-scale-{index}")
        await _bound_useful_feedback(target_id, actor)

    _after, after_statements = await _run_comparison()

    assert abs(after_statements - before_statements) <= 1


async def test_statement_count_is_bounded_in_relationship_edge_count(
    client, monkeypatch
):
    await _skip_without_db()
    _patch_embeddings(monkeypatch)

    seed_ids = [
        await _seed_qualified(client, f"semantic target edge seed {index}")
        for index in range(2)
    ]
    neighbor_ids = [
        await _seed_qualified(client, f"graph neighbor edge fixture {index:02d}")
        for index in range(3)
    ]
    # Same node set in both runs; only the edge count varies. Neighbors use
    # distractor vectors, so they enter packets only through expansion.
    async def _set_edges(pairs: list[tuple[int, int]]) -> None:
        tenant_id = (await _default_ids())["tenant_id"]
        async with _test_engine.begin() as conn:
            await conn.execute(text("DELETE FROM memory_edges"))
            for seed_index, neighbor_index in pairs:
                await conn.execute(
                    text(
                        "INSERT INTO memory_edges (id, tenant_id, "
                        "source_item_id, target_item_id, edge_type, weight, "
                        "created_at) VALUES "
                        "(:id, :tenant, :source, :target, 'supports', 0.8, now())"
                    ),
                    {
                        "id": uuid4(),
                        "tenant": tenant_id,
                        "source": UUID(seed_ids[seed_index]),
                        "target": UUID(neighbor_ids[neighbor_index]),
                    },
                )

    await _set_edges([(0, 0), (1, 1)])
    _few, few_statements = await _run_comparison()
    few_governed = _candidate(_few, "governed")
    assert any(
        item["id"] in set(neighbor_ids) for item in few_governed["items"]
    ), "fixture sanity: expansion must discover the linked neighbors"

    await _set_edges(
        [
            (0, 0), (0, 1), (0, 2),
            (1, 0), (1, 1), (1, 2),
        ]
    )
    _many, many_statements = await _run_comparison()

    # 3x the graph edges between the same nodes: discovery, bulk resolution,
    # and packing relations stay bulk — no per-edge queries.
    assert abs(many_statements - few_statements) <= 2

# ---- 4. runner-level obligations --------------------------------------------


async def _build_manifest(
    *,
    cases: list[dict[str, Any]],
    snapshot_digest: str = "0" * 64,
) -> Any:
    from evals.recall.schema import RecallEvaluationManifest

    ids = await _default_ids()
    async with _test_session_factory() as session:
        from engram.embedding_profiles import get_active_profile

        embedding_profile = await get_active_profile(session)
        tenant_config_version = await session.scalar(
            text(
                "SELECT config_version FROM tenant_config "
                "WHERE tenant_id = :tenant_id AND active = TRUE"
            ),
            {"tenant_id": ids["tenant_id"]},
        )
    return RecallEvaluationManifest.model_validate(
        {
            "schema_version": "engram-recall-evaluation-input-v2",
            "baseline_sha": "e20a62853be75916c6a890fd7876c7e14c2718fc",
            "repository_sha": "f" * 40,
            "snapshot_digest": snapshot_digest,
            "snapshot_at": _FROZEN_NOW,
            "evaluation_at": _FROZEN_NOW,
            "tenant_config_version": tenant_config_version,
            "embedding_profile_key": embedding_profile.profile_key,
            "memory_context": {
                "version": "memory-context-v2",
                "tenant_id": ids["tenant_id"],
                "principal_id": ids["principal_id"],
            },
            "cases": cases,
        }
    )


async def _capture_snapshot_digest(manifest: Any) -> str:
    from evals.admission.schema import digest
    from evals.recall.runner import (
        evaluation_state_identity,
        read_only_evaluation_session,
    )

    async with read_only_evaluation_session(_test_session_factory, manifest) as session:
        return digest(await evaluation_state_identity(session, manifest))


async def test_runner_statement_accounting_includes_neutral_and_reconciles(
    client, monkeypatch
):
    await _skip_without_db()
    _patch_embeddings(monkeypatch)
    await _seed_qualified(client, "semantic target accounting")
    monkeypatch.setenv("ENGRAM_REPOSITORY_SHA", "f" * 40)

    from evals.recall.runner import read_only_evaluation_session, run_recall_evaluation

    manifest = await _build_manifest(
        cases=[
            {
                "case_id": "accounting-case",
                "query": "semantic query",
                "query_digest": _query_digest_of("semantic query"),
                "strata": {"corpus_scale": "typical"},
            }
        ]
    )
    manifest = manifest.model_copy(
        update={"snapshot_digest": await _capture_snapshot_digest(manifest)}
    )

    async with read_only_evaluation_session(_test_session_factory, manifest) as session:
        private, public = await run_recall_evaluation(session, manifest)

    proof = private["read_only_proof"]
    case_counts = proof["case_db_statement_counts"]["accounting-case"]
    # The neutral-usefulness counterfactual is now inside the per-case total.
    assert case_counts["neutral_usefulness_counterfactual"] > 0
    assert case_counts["primary_comparison"] > 0
    assert case_counts["total"] == (
        case_counts["primary_comparison"] + case_counts["neutral_usefulness_counterfactual"]
    )
    assert proof["per_case_sums_reconcile_to_total"] is True
    assert proof["total_db_statement_count"] == (
        proof["fixed_setup_statement_count"]
        + sum(entry["total"] for entry in proof["case_db_statement_counts"].values())
        + proof["metadata_query_count"]
        + proof["fixed_finalization_statement_count"]
    )
    accounting = public["read_only_proof"]["query_accounting"]
    assert accounting["per_case_sums_reconcile_to_total"] is True
    assert accounting["per_case_neutral_usefulness_counterfactual"]["min"] > 0
    # Public output carries aggregates only, never per-case identity.
    assert "case_db_statement_counts" not in public["read_only_proof"]
    assert "accounting-case" not in str(public)


def _query_digest_of(query: str) -> str:
    from engram.semantic_context_manifest import semantic_query_digest

    return semantic_query_digest(query)


async def test_frozen_query_embedding_replay_ignores_later_provider_output(
    client, monkeypatch
):
    await _skip_without_db()
    _patch_embeddings(monkeypatch)  # deterministic item embeddings for seeding
    await _seed_qualified(client, "semantic target frozen embedding")
    monkeypatch.setenv("ENGRAM_REPOSITORY_SHA", "f" * 40)

    from evals.recall.runner import read_only_evaluation_session, run_recall_evaluation
    from evals.recall.schema import query_embedding_vector_digest

    case = {
        "case_id": "frozen-embedding-case",
        "query": "semantic query",
        "query_digest": _query_digest_of("semantic query"),
    }
    manifest = await _build_manifest(cases=[case])
    manifest = manifest.model_copy(
        update={"snapshot_digest": await _capture_snapshot_digest(manifest)}
    )

    # Phase 1: provider capture through the real shared gateway.
    async def provider_vector_one(
        text_value: str, *_args: object, **_kwargs: object
    ) -> list[float] | None:
        return _TARGET_VEC

    import engram.embeddings as embeddings_mod
    from engram import recall as recall_mod

    monkeypatch.setattr(recall_mod, "generate_embedding", provider_vector_one)
    monkeypatch.setattr(embeddings_mod, "generate_embedding", provider_vector_one)

    async with read_only_evaluation_session(_test_session_factory, manifest) as session:
        capture_private, capture_public = await run_recall_evaluation(session, manifest)

    assert capture_public["read_only_proof"]["provider_calls"][
        "semantic_query_embedding"
    ] == 1
    captured = capture_private["case_rows"][0]["captured_query_embedding"]
    assert captured == _TARGET_VEC

    # Phase 2: freeze the captured vector into the manifest.
    frozen_case = {
        **case,
        "query_embedding": {
            "values": captured,
            "vector_digest": query_embedding_vector_digest(captured),
        },
    }
    frozen_manifest = await _build_manifest(cases=[frozen_case])
    frozen_manifest = frozen_manifest.model_copy(
        update={"snapshot_digest": await _capture_snapshot_digest(frozen_manifest)}
    )

    # Phase 3: the provider now returns a materially different vector.
    drifted = [0.0] + [1.0] + [0.0] * 1534

    async def provider_vector_drifted(
        text_value: str, *_args: object, **_kwargs: object
    ) -> list[float] | None:
        return drifted

    monkeypatch.setattr(recall_mod, "generate_embedding", provider_vector_drifted)
    monkeypatch.setattr(embeddings_mod, "generate_embedding", provider_vector_drifted)

    async with read_only_evaluation_session(
        _test_session_factory, frozen_manifest
    ) as session:
        replay_private, replay_public = await run_recall_evaluation(session, frozen_manifest)

    # No provider call happened; the frozen vector was used.
    assert replay_public["read_only_proof"]["provider_calls"][
        "semantic_query_embedding"
    ] == 0
    assert replay_public["read_only_proof"]["deterministic_replay"] == {
        "frozen_embedding_case_count": 1,
        "provider_capture_case_count": 0,
    }
    # The replayed packets are identical to the capture run's packets.
    assert replay_private["case_rows"][0]["candidates"] == capture_private["case_rows"][
        0
    ]["candidates"]
    assert replay_private["case_rows"][0]["legacy"] == capture_private["case_rows"][0][
        "legacy"
    ]
    # The drifted vector never leaked into the replay identity.
    assert drifted not in replay_private["case_rows"][0][
        "captured_query_embedding"
    ]
    assert replay_private["case_rows"][0]["captured_query_embedding"] == captured


async def test_packing_omission_surfaces_in_report_aggregates(client, monkeypatch):
    """A real ``budget`` omission reaches every aggregate through the exact
    ``packing["omitted"]`` production contract."""
    await _skip_without_db()
    _patch_embeddings(monkeypatch)
    monkeypatch.setenv("ENGRAM_REPOSITORY_SHA", "f" * 40)

    first = await _seed_qualified(client, "semantic target packing budget one")
    second = await _seed_qualified(client, "semantic target packing budget two")
    # Budget sized to fit exactly one of the two items.
    async with _test_session_factory() as session:
        lengths = (
            await session.execute(
                text(
                    "SELECT length(content) FROM memory_items "
                    "WHERE id = ANY(CAST(:ids AS uuid[])) ORDER BY length(content) DESC",
                ),
                {"ids": [UUID(first), UUID(second)]},
            )
        ).scalars().all()
    byte_budget = int(lengths[-1])

    from evals.recall.runner import read_only_evaluation_session, run_recall_evaluation

    manifest = await _build_manifest(
        cases=[
            {
                "case_id": "packing-case",
                "query": "semantic query",
                "query_digest": _query_digest_of("semantic query"),
                "byte_budget": byte_budget,
                "item_budget": 10,
            }
        ]
    )
    manifest = manifest.model_copy(
        update={"snapshot_digest": await _capture_snapshot_digest(manifest)}
    )

    async with read_only_evaluation_session(_test_session_factory, manifest) as session:
        _private, public = await run_recall_evaluation(session, manifest)

    governed = public["profiles"]["governed"]
    assert governed["relationship_and_packing"]["packing_omission_reasons"].get(
        "budget", 0
    ) >= 1
    assert governed["packet_change"]["packing_omission_reasons"].get("budget", 0) >= 1


async def test_v2_resolution_states_count_mixed_statuses_exactly_once(
    client, monkeypatch
):
    """A current/missing/stale fixture: every resolved item counted once."""
    await _skip_without_db()
    _patch_embeddings(monkeypatch)

    current_id = await _seed_qualified(client, "semantic target v2 current")
    # missing: no V2 row at all.
    missing_id = (
        await _remember(
            client, "proposed target v2 missing", source_type="extraction"
        )
    )["id"]
    await _backdate(missing_id)
    # stale: V2 row persisted, then a decision-hash input changes so the
    # persisted hash no longer matches the fresh evaluation. human_verified
    # is part of the #158 item-state envelope (importance/confidence are not).
    stale_id = await _seed_qualified(client, "semantic target v2 stale")
    async with _test_engine.begin() as conn:
        await conn.execute(
            text("UPDATE memory_items SET human_verified = TRUE WHERE id = :id"),
            {"id": stale_id},
        )

    result, _statements = await _run_comparison(item_budget=10)
    governed = _candidate(result, "governed")

    counts = governed["v2_resolution"]["resolution_status_counts"]
    assert counts.get("current") == 1
    assert counts.get("missing") == 1
    assert counts.get("stale") == 1

    from evals.recall.runner import _admission_strata

    strata = _admission_strata([governed])
    assert strata["v2_resolution_state"] == {"current": 1, "missing": 1, "stale": 1}
    # Exactly-once identity on this no-truncation fixture: every resolved
    # item is either admitted or itemized as one withheld diagnostic.
    admitted = len(governed["items"])
    withheld = len(governed["admission_diagnostics"])
    assert sum(strata["v2_resolution_state"].values()) == admitted + withheld
    # The governed packet admitted exactly the current item; the missing and
    # stale proposals were withheld, each contributing one diagnostic.
    assert {item["id"] for item in governed["items"]} == {current_id}
    diagnostic_ids = {entry["item_id"] for entry in governed["admission_diagnostics"]}
    assert diagnostic_ids == {missing_id, stale_id}


async def test_runtime_setting_drift_invalidates_replay(client, monkeypatch):
    """Any material runtime setting change breaks the frozen snapshot."""
    await _skip_without_db()
    _patch_embeddings(monkeypatch)
    await _seed_qualified(client, "semantic target drift")
    monkeypatch.setenv("ENGRAM_REPOSITORY_SHA", "f" * 40)

    from evals.recall.runner import read_only_evaluation_session, run_recall_evaluation

    manifest = await _build_manifest(
        cases=[
            {
                "case_id": "drift-case",
                "query": "semantic query",
                "query_digest": _query_digest_of("semantic query"),
            }
        ]
    )
    manifest = manifest.model_copy(
        update={"snapshot_digest": await _capture_snapshot_digest(manifest)}
    )

    async with read_only_evaluation_session(_test_session_factory, manifest) as session:
        _private, _public = await run_recall_evaluation(session, manifest)

    # Replay still succeeds with unchanged settings...
    async with read_only_evaluation_session(_test_session_factory, manifest) as session:
        _private, _public = await run_recall_evaluation(session, manifest)

    # ...but drifting one material setting invalidates the frozen identity.
    monkeypatch.setattr(settings, "relationship_expansion_enabled", False)
    with pytest.raises(ValueError, match="evaluation_snapshot_identity_mismatch"):
        async with read_only_evaluation_session(
            _test_session_factory, manifest
        ) as session:
            await run_recall_evaluation(session, manifest)

    monkeypatch.setattr(settings, "assessment_selection_enabled", False)
    with pytest.raises(ValueError, match="evaluation_snapshot_identity_mismatch"):
        async with read_only_evaluation_session(
            _test_session_factory, manifest
        ) as session:
            await run_recall_evaluation(session, manifest)

    monkeypatch.setattr(settings, "recall_candidate_ceiling", 37)
    with pytest.raises(ValueError, match="evaluation_snapshot_identity_mismatch"):
        async with read_only_evaluation_session(
            _test_session_factory, manifest
        ) as session:
            await run_recall_evaluation(session, manifest)
