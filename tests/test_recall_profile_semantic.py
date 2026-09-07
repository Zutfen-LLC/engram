"""Integration tests for recall admission profiles (issues #160 / #186).

These tests require a live PostgreSQL with the v2 schema (migrations/) and
pgvector. They skip automatically when no DB is reachable, mirroring
tests/test_semantic_recall.py. Embeddings are deterministic fakes so CI never
depends on OpenAI.

The regressions pinned here:

* the rollout boundary — before accepted #162 certification, ordinary
  ``POST /v1/recall`` serves the legacy packet only: requesting (or
  defaulting to) governed/exploratory can never change the authoritative
  served packet;
* the shadow comparison surface evaluates governed/exploratory packets
  read-only (no recall_logs row, no exposure counters, serving unchanged)
  and is gated by reviewer capability AND tenant policy;
* since issue #186 the candidate profiles are V2-bound: admission is the
  exact #158 ``risk_aware_shadow_v1`` per-surface decision over the
  live-proposal corpus — a missing/stale/mismatched V2 row withholds
  explicitly, and an active item is never admissible (``not_live``);
* corpus eligibility is applied in SQL before the bounded HNSW window, so
  rows the V2 gate would inevitably withhold cannot starve eligible
  proposals;
* durable admission assessments captured earlier still bind recall when
  ``admission_assessment_capture_enabled`` is disabled (rollback invariant);
* the legacy profile's behavior is byte-for-byte unchanged.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.admission_policy import AdmissionPolicyDecision, load_admission_policy
from engram.api.app import create_app
from engram.api.routes import memory as memory_routes
from engram.config import settings
from engram.db import get_session

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


async def _get_test_session() -> AsyncSession:
    async with _test_session_factory() as session:
        from sqlalchemy import text as sa_text

        from engram.db import _DEFAULT_PRINCIPAL_NAME, _DEFAULT_TENANT_SLUG, apply_rls_context

        row = (
            (
                await session.execute(
                    sa_text(
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
        await conn.execute(text("DELETE FROM usage_events"))
        await conn.execute(text("DELETE FROM feedback_events"))
        await conn.execute(text("DELETE FROM recall_logs"))
        await conn.execute(text("DELETE FROM admission_assessment_current"))
        await conn.execute(text("DELETE FROM admission_assessments"))
        await conn.execute(text("DELETE FROM jobs"))
        await conn.execute(text("DELETE FROM item_events"))
        await conn.execute(text("DELETE FROM classification_runs"))
        # Issue #190 expansion fixtures: edges cascade with their items, but
        # tunnels, workspaces/principals, and cross-tenant rows need explicit
        # cleanup (after memory_items, which reference them).
        await conn.execute(text("DELETE FROM memory_edges"))
        await conn.execute(text("DELETE FROM tunnels"))
        await conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'exp190-%'"))
        await conn.execute(text("DELETE FROM memory_embeddings"))
        await conn.execute(text("DELETE FROM memory_items"))
        await conn.execute(text("DELETE FROM workspaces WHERE slug LIKE 'exp190-%'"))
        await conn.execute(text("DELETE FROM principals WHERE name LIKE 'exp190-%'"))
        # Tenant shadow policy fails closed after every test.
        await conn.execute(
            text(
                "UPDATE tenant_config SET recall_profile_shadow_enabled = FALSE "
                "WHERE tenant_id = (SELECT id FROM tenants WHERE slug = 'default')"
            )
        )


@pytest.fixture(autouse=True)
def _reset_embedding_provider():
    original_provider = settings.embedding_provider
    original_conflict = settings.conflict_check_on_write
    original_capture = settings.admission_assessment_capture_enabled
    original_default_profile = settings.recall_default_profile
    settings.conflict_check_on_write = False
    yield
    settings.embedding_provider = original_provider
    settings.conflict_check_on_write = original_conflict
    settings.admission_assessment_capture_enabled = original_capture
    settings.recall_default_profile = original_default_profile


_TARGET_VEC = [1.0] + [0.0] * 1535
_DISTRACTOR_VEC = [0.0, 1.0] + [0.0] * 1534

# A vector measurably close to the query (cosine ~0.924) but strictly farther
# than an exact hit — for the pre-LIMIT eligibility regression.
_NEAR_VEC = [0.9238795325112867, 0.3826834323650898] + [0.0] * 1534

_TARGET_PREFIXES = ("semantic target", "semantic query", "proposed target")


def _fake_embedding_for(text_value: str) -> list[float]:
    if text_value.startswith(_TARGET_PREFIXES):
        return _TARGET_VEC
    return _DISTRACTOR_VEC


async def _remember(client: AsyncClient, content: str, **payload: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"content": content, "source_type": "manual"}
    body.update(payload)
    resp = await client.post("/v1/remember", json=body)
    assert resp.status_code == 201, resp.text
    await _drain_jobs()
    return resp.json()


def _patch_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_embedding(
        text_value: str, *_args: object, **_kwargs: object
    ) -> list[float] | None:
        return _fake_embedding_for(text_value)

    import engram.embeddings as embeddings_mod
    from engram import recall as recall_mod

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


async def _recall(
    client: AsyncClient, *, recall_profile: str | None = None, **extra: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {"mode": "semantic", "query": "semantic query"}
    if recall_profile is not None:
        body["recall_profile"] = recall_profile
    body.update(extra)
    resp = await client.post("/v1/recall", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _shadow_compare(
    client: AsyncClient,
    *,
    profiles: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {"query": "semantic query"}
    if profiles is not None:
        body["profiles"] = profiles
    body.update(extra)
    resp = await client.post("/v1/recall/shadow-compare", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _enable_tenant_shadow_policy() -> None:
    async with _test_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE tenant_config SET recall_profile_shadow_enabled = TRUE "
                "WHERE tenant_id = (SELECT id FROM tenants WHERE slug = 'default')"
            )
        )


async def _seed_active_and_proposed(client: AsyncClient) -> tuple[str, str]:
    active = await _remember(client, "semantic target active")
    assert active["review_status"] == "active"
    proposed = await _remember(
        client, "proposed target unreviewed", source_type="extraction", importance=0.95
    )
    assert proposed["review_status"] == "proposed"
    return active["id"], proposed["id"]


async def _skip_without_db() -> None:
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")


async def _recall_log_count() -> int:
    async with _test_session_factory() as session:
        return int(await session.scalar(text("SELECT count(*) FROM recall_logs"))) or 0


async def _recall_counts(item_ids: list[str]) -> list[int]:
    async with _test_session_factory() as session:
        rows = (
            
                await session.execute(
                    text(
                        "SELECT recall_count FROM memory_items "
                        "WHERE id = ANY(CAST(:ids AS uuid[])) ORDER BY id"
                    ),
                    {"ids": item_ids},
                )
            
        ).scalars().all()
        return list(rows)


# ---- V2 (#158) admission fixtures --------------------------------------------
#
# The candidate profiles consume exact risk_aware_shadow_v1 decisions bound to
# persisted V2 shadow rows. These helpers create the prerequisite state the
# way production would: a completed #157 combined assessment whose contract
# hash and input digest the selection accepts, then the #158 simulator +
# persist path for the V2 row itself.

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


@pytest.fixture(autouse=True)
def _enable_v2_selection():
    """Point #157 effective selection at the policy artifact's contract.

    Selection settings are deployment knobs in production; for these tests the
    artifact's accepted contract is the operative one.
    """
    enabled = settings.assessment_selection_enabled
    contract = settings.assessment_effective_contract_hash
    settings.assessment_selection_enabled = True
    settings.assessment_effective_contract_hash = _V2_CONTRACT_HASH
    yield
    settings.assessment_selection_enabled = enabled
    settings.assessment_effective_contract_hash = contract


def _test_memory_context(tenant_id: str, principal_id: str) -> Any:
    from engram.memory_context import MEMORY_CONTEXT_VERSION, ResolvedMemoryContext

    return ResolvedMemoryContext(
        version=MEMORY_CONTEXT_VERSION,
        tenant_id=tenant_id,
        principal_id=principal_id,
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


async def _persist_v2_row(
    item_id: str,
    *,
    risk: str | None = "low",
    epistemic_state: str = "supported",
    retention_disposition: str = "retain",
    created_hours_ago: float | None = None,
) -> AdmissionPolicyDecision:
    """Give one live proposal a qualifying #157 assessment and a persisted V2 row.

    ``risk=None`` skips the #157 row entirely (the item's V2 decision then
    reflects absent evidence — exploratory still allows it, governed
    withholds), which is how exploratory-visible-but-unqualified candidates
    are built. The V2 row is produced by the real #158 simulator + persist
    path, so the shadow comparison resolves it ``current``.
    """
    from sqlalchemy import select

    from engram.admission_shadow import persist_shadow_comparison, simulate_item
    from engram.assessments import evidence_snapshot
    from engram.db import apply_rls_context
    from engram.extraction import digest
    from engram.models import MemoryAssessment, MemoryItem

    async with _test_session_factory() as session:
        ids = (
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
        await apply_rls_context(
            session, tenant_id=ids["tenant_id"], principal_id=ids["principal_id"]
        )
        item = await session.scalar(
            select(MemoryItem).where(MemoryItem.id == item_id)
        )
        assert item is not None
        if created_hours_ago is not None:
            item.created_at = datetime.now(UTC) - timedelta(hours=created_hours_ago)
            await session.flush()
        context = _test_memory_context(ids["tenant_id"], ids["principal_id"])
        if risk is not None:
            # A bound classification receipt gives the assessment its required
            # legacy-run link (the trigger validates tenant/item/bound_at).
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
        evaluation_time = datetime.now(UTC)
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
        return comparison.shadow


# ---- rollout boundary: ordinary recall serves the legacy packet only ----


async def test_ordinary_semantic_recall_serves_legacy_packet(client, monkeypatch):
    """Requirement 1: before #162 certification the authoritative served
    packet is the legacy one — proposed items included, blended trust_score
    present, no separated-signal fields."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)

    _active_id, proposed_id = await _seed_active_and_proposed(client)

    body = await _recall(client)
    assert body["recall_profile"] == "legacy"
    assert body["scoring_version"] == "semantic-v3"
    assert body["signals_version"] is None
    assert body["omitted_by_admission"] == {}
    served_ids = {item["id"] for item in body["items"]}
    assert proposed_id in served_ids
    proposal = next(i for i in body["items"] if i["id"] == proposed_id)
    assert "trust_score" in proposal
    assert "unreviewed" in proposal["warnings"]
    assert "epistemic_state" not in proposal
    # Issue #188: the structured evidence block and machine codes are
    # candidate-profile additions — the legacy item shape gains no keys.
    assert "evidence" not in proposal
    assert "warning_codes" not in proposal

    # Explicit legacy behaves identically.
    explicit = await _recall(client, recall_profile="legacy")
    assert explicit["recall_profile"] == "legacy"
    assert {i["id"] for i in explicit["items"]} == served_ids


async def test_requesting_governed_cannot_change_the_served_packet(client, monkeypatch):
    """Requirement 2: merely requesting governed is refused (422) — it can
    never produce a different authoritative working set, and the error names
    the certification boundary."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)

    await _seed_active_and_proposed(client)

    resp = await client.post(
        "/v1/recall", json={"mode": "semantic", "query": "q", "recall_profile": "governed"}
    )
    assert resp.status_code == 422
    detail = str(resp.json()["detail"])
    assert "not certified" in detail

    # Serving is unaffected: the ordinary packet is still the legacy one.
    body = await _recall(client)
    assert body["recall_profile"] == "legacy"
    assert body["scoring_version"] == "semantic-v3"


async def test_requesting_exploratory_cannot_change_the_served_packet(client, monkeypatch):
    """Requirement 3: merely requesting exploratory is refused (422) before
    certification — no broader working memory for any caller of /v1/recall."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)

    await _seed_active_and_proposed(client)

    resp = await client.post(
        "/v1/recall",
        json={"mode": "semantic", "query": "q", "recall_profile": "exploratory"},
    )
    assert resp.status_code == 422
    assert "not certified" in str(resp.json()["detail"])

    body = await _recall(client)
    assert body["recall_profile"] == "legacy"


async def test_uncertified_default_profile_cannot_promote_itself(
    client, monkeypatch, caplog
):
    """Requirement 4: recall_default_profile=governed is refused, not honored
    — every ordinary request still serves the certified legacy packet."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    settings.recall_default_profile = "governed"

    _active_id, proposed_id = await _seed_active_and_proposed(client)

    body = await _recall(client)
    assert body["recall_profile"] == "legacy"
    assert body["scoring_version"] == "semantic-v3"
    served_ids = {item["id"] for item in body["items"]}
    assert proposed_id in served_ids  # still the legacy active+proposed corpus

    # The refusal is visible, not silent.
    assert any("recall_default_profile_refused" in r.message for r in caplog.records)


# ---- shadow comparison surface ----


async def test_shadow_comparison_evaluates_candidates_without_mutating_serving(
    client, monkeypatch
):
    """Requirements 5 + 1: the shadow surface evaluates legacy vs
    governed/exploratory for the same query, writes no recall_logs row,
    bumps no exposure counters, and changes nothing about serving.

    Under the #186 binding, the governed candidate admits only items with a
    current V2 allow: the qualified proposal enters; the active item —
    ``not_live`` in the #158 policy — never can, no matter its review
    status.
    """
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    active_id, proposed_id = await _seed_active_and_proposed(client)
    await _persist_v2_row(proposed_id)  # qualified: low risk, supported, retained
    served = await _recall(client)
    served_ids = {item["id"] for item in served["items"]}
    logs_before = await _recall_log_count()
    counts_before = await _recall_counts([active_id, proposed_id])

    shadow = await _shadow_compare(client, profiles=["governed", "exploratory"])

    # The shadow response names the authority and the boundary.
    assert shadow["authoritative_profile"] == "legacy"
    assert shadow["certified_serving_profiles"] == ["legacy"]
    assert shadow["shadow_comparison_version"] == "recall-shadow-compare-v2"
    assert shadow["legacy"]["profile"] == "legacy"
    assert shadow["legacy"]["item_count"] == served["item_count"]
    assert {i["id"] for i in shadow["legacy"]["items"]} == served_ids

    # Governed candidate: only the V2-qualified proposal. The active item is
    # outside the policy's live-proposal domain entirely.
    governed = next(c for c in shadow["candidates"] if c["profile"] == "governed")
    assert {i["id"] for i in governed["items"]} == {proposed_id}
    assert governed["comparison"]["only_in_legacy"] == [active_id]
    assert governed["v2_resolution"]["profile_key"] == "risk_aware_shadow_v1"
    assert governed["v2_resolution"]["resolution_status_counts"] == {"current": 1}
    assert governed["admission_diagnostics"] == []

    # Exploratory candidate: the qualified proposal is admitted. Since #188
    # its served evidence state is the canonical V2 fresh evaluation
    # ("supported") — not the item-local proposal heuristic ("unknown").
    exploratory = next(c for c in shadow["candidates"] if c["profile"] == "exploratory")
    expl_by_id = {i["id"]: i for i in exploratory["items"]}
    assert proposed_id in expl_by_id
    assert expl_by_id[proposed_id]["epistemic_state"] == "supported"
    assert (
        expl_by_id[proposed_id]["evidence"]["epistemic_state"] == "supported"
    )
    assert "unreviewed" in expl_by_id[proposed_id]["warning_codes"]

    # Read-only proof: no audit row, no exposure counters, serving unchanged.
    assert await _recall_log_count() == logs_before
    assert await _recall_counts([active_id, proposed_id]) == counts_before
    after = await _recall(client)
    assert {i["id"] for i in after["items"]} == served_ids


async def test_shadow_runs_when_the_candidate_corpus_is_empty(
    client, monkeypatch
):
    """Preflight regression: the comparison must run whenever ANY requested
    packet has an eligible corpus. A tenant whose only item is ACTIVE has a
    non-empty legacy corpus (active + proposed window) but an EMPTY candidate
    corpus (the V2 policy's domain is live proposals) — the candidates
    legitimately evaluate to empty packets while legacy serves its item."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    item = await _remember(client, "semantic target active only")

    shadow = await _shadow_compare(client, profiles=["governed", "exploratory"])

    # The legacy baseline evaluated (non-empty), it did not abort the
    # comparison.
    assert shadow["legacy"] is not None
    assert shadow["legacy"]["item_count"] == 1
    assert shadow["message"] is None

    governed = next(c for c in shadow["candidates"] if c["profile"] == "governed")
    assert governed["item_count"] == 0
    assert governed["candidate_count"] == 0
    assert governed["comparison"] == {
        "in_both": [],
        "only_in_legacy": [item["id"]],
        "only_in_candidate": [],
    }

    exploratory = next(c for c in shadow["candidates"] if c["profile"] == "exploratory")
    assert exploratory["item_count"] == 0
    assert exploratory["candidate_count"] == 0


async def test_governed_candidate_admits_only_v2_qualified_proposals(
    client, monkeypatch
):
    """The governed candidate packet carries the separated signal model, no
    blended trust_score, and — since #186 — admits exactly the V2-allowed
    items, itemizing the others as explicit missing/withheld diagnostics."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    active_id, qualified_id = await _seed_active_and_proposed(client)
    await _persist_v2_row(qualified_id)
    # A second proposal with absent evidence but a persisted V2 row: the
    # policy routes unknown-risk evidence to review (governed withholds it)
    # while exploratory — allow on that same output — may show it.
    unqualified = await _remember(
        client, "proposed target unassessed", source_type="extraction"
    )
    await _persist_v2_row(unqualified["id"], risk=None)

    shadow = await _shadow_compare(client, profiles=["governed", "exploratory"])
    governed = shadow["candidates"][0]
    assert governed["scoring_version"] == "semantic-signals-v1"
    assert governed["signals_version"] == "recall-signals-v1"
    assert {i["id"] for i in governed["items"]} == {qualified_id}
    # The active item never entered the pre-LIMIT eligible window (the V2
    # domain is live proposals); the unqualified proposal was retrieved and
    # withheld by the exact surface decision.
    assert governed["omitted_by_admission"] == {"v2_surface_review_required": 1}
    by_id = {d["item_id"]: d for d in governed["admission_diagnostics"]}
    assert by_id[unqualified["id"]]["v2_resolution_status"] == "current"
    assert by_id[unqualified["id"]]["v2_surface_decision"] == "review_required"
    # The local hard gate (live proposal, no #159 withholds) would admit it
    # while the current V2 decision routes it to review — exactly the bounded
    # mismatch diagnostic operators evaluating #162 certification must see.
    assert by_id[unqualified["id"]]["gates_disagree"] is True

    served = governed["items"][0]
    assert "trust_score" not in served
    assert served["relevance_score"] > 0
    assert 0.0 <= served["utility_score"] <= 1.0
    # Issue #188: the served evidence state is the canonical V2 fresh state.
    assert served["epistemic_state"] == "supported"
    assert served["admission"]["profile"] == "governed"
    assert served["admission"]["decision"] == "admit"
    assert served["admission"]["surface"] == "semantic_governed"
    assert served["admission"]["surface_decision"] == "allow"
    assert served["admission"]["reason_codes"] == ["admitted_v2_surface_allow"]
    v2 = served["admission"]["v2"]
    assert v2["resolution_status"] == "current"
    assert v2["profile_key"] == "risk_aware_shadow_v1"
    assert v2["fresh"]["risk_state"] == "low"
    assert v2["fresh"]["decision_hash"].startswith("sha256:")
    # current means the persisted identity and the fresh evaluation agree.
    assert v2["persisted"]["decision_hash"] == v2["fresh"]["decision_hash"]
    assert v2["persisted"]["policy_artifact_digest"] == v2["fresh"]["policy_artifact_digest"]
    # The evidence block is an exact projection of that same binding.
    _evidence_identity_asserts(served)
    from engram.recall_signals import compute_signal_rank_score

    assert served["score"] == compute_signal_rank_score(
        similarity=served["relevance_score"], utility=served["utility_score"]
    )

    # Exploratory consumes its own exact surface: both proposals' decisions
    # say semantic_exploratory=allow.
    exploratory = next(
        c for c in shadow["candidates"] if c["profile"] == "exploratory"
    )
    assert {i["id"] for i in exploratory["items"]} == {qualified_id, unqualified["id"]}


async def test_shadow_comparison_latency_is_recorded_and_bounded(client, monkeypatch):
    """Issue #186 performance evidence: on a deterministic fixture the whole
    comparison — one shared query embedding, legacy packet, and both V2-bound
    candidate packets with bulk resolution — completes within a bounded
    budget. The measured number is printed for the issue's performance
    record; the bound is deliberately generous (CI hardware varies)."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    for i in range(10):
        item = await _remember(
            client, f"semantic target latency {i:02d}", source_type="extraction"
        )
        await _persist_v2_row(item["id"])

    import time

    started = time.perf_counter()
    shadow = await _shadow_compare(client, profiles=["governed", "exploratory"])
    elapsed = time.perf_counter() - started
    print(
        f"\nshadow-compare latency (10 qualified candidates, 3 packets): "
        f"{elapsed:.3f}s; resolver queries per candidate packet: "
        f"{shadow['candidates'][0]['v2_resolution']['query_count']}"
    )
    assert shadow["candidates"][0]["v2_resolution"]["resolved_count"] == 10
    assert elapsed < 10.0


async def test_exploratory_candidate_budget_capped(client, monkeypatch):
    """Exploratory packets stay under their tighter budget caps."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    for i in range(25):
        item = await _remember(
            client, f"semantic target numbered {i:02d}", source_type="extraction"
        )
        await _persist_v2_row(item["id"])

    shadow = await _shadow_compare(
        client, profiles=["governed", "exploratory"], item_budget=50
    )
    by_profile = {c["profile"]: c for c in shadow["candidates"]}
    assert by_profile["exploratory"]["item_count"] == 20
    assert by_profile["governed"]["item_count"] == 25


async def test_governed_candidate_withholds_item_with_stale_assessment(
    client, monkeypatch
):
    """A stale durable #159 assessment withholds in the governed candidate and
    is merely marked in the exploratory one — local defense in depth stays
    stronger than any V2 allow."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    _active_id, proposed_id = await _seed_active_and_proposed(client)
    await _persist_v2_row(proposed_id)

    from engram import recall_signals as signals_mod

    async def fake_bindings(session, *, tenant_id, items):  # type: ignore[no-untyped-def]
        return {
            item.id: signals_mod.AdmissionAssessmentBinding(
                assessment_id=str(item.id),  # opaque fixture id is fine
                status="stale",
                outcome="admitted",
            )
            for item in items
        }

    monkeypatch.setattr(signals_mod, "load_admission_bindings", fake_bindings)

    shadow = await _shadow_compare(client, profiles=["governed", "exploratory"])
    by_profile = {c["profile"]: c for c in shadow["candidates"]}
    assert by_profile["governed"]["item_count"] == 0
    assert by_profile["governed"]["omitted_by_admission"].get(
        "admission_assessment_stale"
    ) == 1

    expl = by_profile["exploratory"]
    assert expl["item_count"] == 1
    expl_by_id = {item["id"]: item for item in expl["items"]}
    assert "admission_assessment_stale" in expl_by_id[proposed_id]["warning_codes"]
    # Issue #188: the #159 stale mark stays an independent lifecycle warning,
    # while the served epistemic state remains the canonical V2 fresh state
    # ("supported"), mirrored in the evidence block — not the local heuristic.
    assert expl_by_id[proposed_id]["epistemic_state"] == "supported"
    _evidence_identity_asserts(expl_by_id[proposed_id])


async def test_governed_candidate_ordering_is_deterministic(client, monkeypatch):
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    for content, importance in (
        ("semantic target one", 0.9),
        ("semantic target two", 0.1),
        ("semantic target three", 0.5),
    ):
        item = await _remember(client, content, importance=importance, source_type="extraction")
        await _persist_v2_row(item["id"])

    first = await _shadow_compare(client, profiles=["governed"])
    second = await _shadow_compare(client, profiles=["governed"])
    ids_first = [item["id"] for item in first["candidates"][0]["items"]]
    ids_second = [item["id"] for item in second["candidates"][0]["items"]]
    assert ids_first == ids_second
    # Highest importance orders first at (near-)equal similarity: all three
    # share the query vector, so utility breaks the tie.
    assert first["candidates"][0]["items"][0]["content"] == "semantic target one"


async def test_shadow_denied_workspace_never_falls_back_to_broader_corpus(
    client, monkeypatch
):
    """Requirement 17 (shadow side): an unresolvable/denied workspace yields
    an empty comparison, never the unscoped corpus."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    await _remember(client, "semantic target")

    resp = await client.post(
        "/v1/recall/shadow-compare",
        json={"query": "semantic query", "workspace": "no-such-workspace-xyz"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["legacy"] is None
    assert body["candidates"] == []
    assert body["candidate_count"] == 0


# ---- shadow authorization: capability AND tenant policy ----


def _principal_override(app, scopes: tuple[str, ...]):
    from engram.auth import Principal, get_current_principal

    async def override():
        return Principal(
            tenant_id="00000000-0000-0000-0000-000000000000",
            principal_id="00000000-0000-0000-0000-000000000000",
            scopes=scopes,
        )

    app.dependency_overrides[get_current_principal] = override


async def test_shadow_denies_ordinary_read_caller(client, app, monkeypatch):
    """Requirement 6: an ordinary read-only caller cannot inspect exploratory
    candidate output — capability is checked before the handler runs."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()
    await _remember(client, "semantic target")

    _principal_override(app, scopes=("read",))
    resp = await client.post(
        "/v1/recall/shadow-compare", json={"query": "semantic query"}
    )
    assert resp.status_code == 403


async def test_shadow_tenant_denial_wins_even_for_a_capable_caller(
    client, monkeypatch
):
    """Requirement 7a: reviewer capability is not sufficient — the tenant's
    policy allow is required, and its denial wins."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _remember(client, "semantic target")

    # Default dev principal carries admin (superset of review); tenant
    # policy stays FALSE (the _clean_db default).
    resp = await client.post(
        "/v1/recall/shadow-compare", json={"query": "semantic query"}
    )
    assert resp.status_code == 403
    assert "recall_profile_shadow_enabled" in str(resp.json()["detail"])


async def test_shadow_allows_reviewer_when_tenant_policy_permits(
    client, app, monkeypatch
):
    """Requirement 7b: a review-scoped caller plus the tenant allow decision
    together unlock candidate inspection."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()
    _active_id, proposed_id = await _seed_active_and_proposed(client)
    await _persist_v2_row(proposed_id)

    _principal_override(app, scopes=("review",))
    body = await _shadow_compare(client, profiles=["exploratory"])
    exploratory = next(c for c in body["candidates"] if c["profile"] == "exploratory")
    assert proposed_id in {i["id"] for i in exploratory["items"]}


# ---- pre-LIMIT corpus eligibility (blocker regression) ----


async def test_inevitably_withheld_rows_cannot_consume_the_bounded_window(
    client, monkeypatch
):
    """Requirement 10 (post-#186 form): enough rows the V2 gate would
    inevitably withhold — active items (``not_live``) and unresolved-conflict
    proposals — sit CLOSER to the query than the eligible proposals. The
    eligible rows must still be retrieved: eligibility is applied in SQL
    before the bounded LIMIT, not by overfetching or post-hoc filtering."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    await _enable_tenant_shadow_policy()

    exact = [1.0] + [0.0] * 1535
    near = [math.cos(math.radians(10))] + [math.sin(math.radians(10))] + [0.0] * 1534
    assert abs(sum(v * v for v in near) - 1.0) < 1e-9

    async def fake_embedding(
        text_value: str, *_args: object, **_kwargs: object
    ) -> list[float] | None:
        if text_value.startswith("ineligible"):
            return exact  # closest possible to the query
        if text_value.startswith("eligible proposal"):
            return near  # measurably farther than every ineligible row
        return _DISTRACTOR_VEC

    import engram.embeddings as embeddings_mod
    from engram import recall as recall_mod

    monkeypatch.setattr(recall_mod, "generate_embedding", fake_embedding)
    monkeypatch.setattr(memory_routes, "generate_embedding", fake_embedding)
    monkeypatch.setattr(embeddings_mod, "generate_embedding", fake_embedding)

    # Manual captures land active; extraction captures land proposed.
    active_ids = [
        (await _remember(client, f"ineligible active row {i:02d}"))["id"] for i in range(4)
    ]
    conflicted_ids = [
        (
            await _remember(
                client, f"ineligible conflicted row {i:02d}", source_type="extraction"
            )
        )["id"]
        for i in range(4)
    ]
    eligible_ids = [
        (
            await _remember(
                client, f"eligible proposal row {i:02d}", source_type="extraction"
            )
        )["id"]
        for i in range(3)
    ]
    for item_id in eligible_ids:
        await _persist_v2_row(item_id)
    # The conflicted rows are live proposals with an unresolved conflict.
    async with _test_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE memory_items SET conflict_resolution_status = 'unresolved', "
                "conflicts_with_item_id = :other WHERE id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": conflicted_ids, "other": active_ids[0]},
        )

    # item_budget=2 -> fetch_limit = 2*3 = 6 < 8 ineligible rows: under a
    # post-hoc-filter behavior the whole window would be ineligible and the
    # governed packet would starve.
    shadow = await _shadow_compare(client, profiles=["governed"], item_budget=2)
    governed = shadow["candidates"][0]
    governed_ids = {i["id"] for i in governed["items"]}
    assert governed["item_count"] == 2
    assert governed_ids.issubset(set(eligible_ids))
    assert not governed_ids & (set(active_ids) | set(conflicted_ids))
    # They were excluded by the SQL predicate, not gate-withheld.
    assert governed["omitted_by_admission"] == {}
    assert governed["admission_diagnostics"] == []

    # Legacy serving also sees the active rows (its window is active+proposed
    # with no conflict filter) — same request, item_budget=2, nearest first.
    legacy_ids = {i["id"] for i in shadow["legacy"]["items"]}
    assert legacy_ids
    assert legacy_ids.issubset(set(active_ids) | set(eligible_ids))


# ---- capture-disabled assessment resolution (rollback invariant) ----


async def test_capture_disabled_preserves_stale_assessment_enforcement(
    client, monkeypatch
):
    """Requirement 9 (#159 rollback invariant): an assessment captured while
    capture was enabled stays visible to the recall resolver after
    ``admission_assessment_capture_enabled=false`` — disabling capture stops
    new capture, it does not erase persisted projection state. The item keeps
    a current V2 allow, so the *only* thing withholding it from governed is
    the stale durable #159 binding."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    # 1. Capture a non-promoting authoritative assessment for a proposal
    #    (below the confidence threshold, so Path A records its decision and
    #    projects it current without promoting the item).
    settings.admission_assessment_capture_enabled = True
    item = await _remember(client, "semantic target assessed", source_type="extraction")
    assert item["review_status"] == "proposed"
    async with _test_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE memory_items SET memory_confidence = 0.3, "
                "created_at = now() - interval '100 hours', "
                "valid_from = now() - interval '100 hours' WHERE id = :id"
            ),
            {"id": item["id"]},
        )
    from engram.promotion import auto_promote_proposed_memories

    async with _test_session_factory() as session:
        tenant_id = str(
            await session.scalar(
                text("SELECT tenant_id::text FROM memory_items WHERE id = :id"),
                {"id": item["id"]},
            )
        )
        await session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id}
        )
        result = await auto_promote_proposed_memories(session, tenant_id)
        await session.commit()
    assert result.promoted == 0
    async with _test_session_factory() as session:
        status = await session.scalar(
            text("SELECT review_status FROM memory_items WHERE id = :id"), {"id": item["id"]}
        )
        assert status == "proposed"
        outcome = await session.scalar(
            text(
                "SELECT a.outcome FROM admission_assessments a "
                "JOIN admission_assessment_current c ON c.assessment_id = a.id "
                "WHERE c.memory_item_id = :id"
            ),
            {"id": item["id"]},
        )
        assert outcome is not None  # captured, projected current

    # 2. Give the proposal a qualifying V2 decision (the #159 change below
    #    must be the ONLY thing that withholds it from governed).
    await _persist_v2_row(item["id"])

    # 3. Make the #159 assessment materially stale (input-state digest change)
    #    and roll back: disable capture (the #159 documented rollback).
    async with _test_engine.begin() as conn:
        await conn.execute(
            text("UPDATE memory_items SET memory_confidence = 0.35 WHERE id = :id"),
            {"id": item["id"]},
        )
    settings.admission_assessment_capture_enabled = False

    # 4/5. The candidate recall resolver still sees the stale assessment and
    # its withhold still applies — with a current V2 allow in hand.
    shadow = await _shadow_compare(client, profiles=["governed", "exploratory"])
    by_profile = {c["profile"]: c for c in shadow["candidates"]}
    assert by_profile["governed"]["item_count"] == 0
    assert (
        by_profile["governed"]["omitted_by_admission"].get("admission_assessment_stale")
        == 1
    )
    # BLOCKER 1 regression (#186 review): the withheld diagnostic reports the
    # disagreement truthfully — V2 resolution current, exact surface allow,
    # gates_disagree true — instead of a fabricated missing/absent V2 state.
    diag = by_profile["governed"]["admission_diagnostics"][0]
    assert diag["item_id"] == item["id"]
    assert diag["reason_codes"] == ["admission_assessment_stale"]
    assert diag["v2_resolution_status"] == "current"
    assert diag["v2_surface_decision"] == "allow"
    assert diag["gates_disagree"] is True
    # BLOCKER 2 regression: the diagnostic carries the full safe binding —
    # enough to identify the exact V2 decision without hidden state.
    v2_diag = diag["v2"]
    assert v2_diag["resolution_status"] == "current"
    assert v2_diag["surface"] == "semantic_governed"
    assert v2_diag["fresh"]["surface_decision"] == "allow"
    assert v2_diag["persisted"]["assessment_id"] is not None
    assert v2_diag["persisted"]["decision_hash"] == v2_diag["fresh"]["decision_hash"]
    expl_by_id = {i["id"]: i for i in by_profile["exploratory"]["items"]}
    assert item["id"] in expl_by_id
    assert "admission_assessment_stale" in expl_by_id[item["id"]]["warning_codes"]
    assert expl_by_id[item["id"]]["admission"]["v2"]["resolution_status"] == "current"


async def _seed_path_a_blocked(item_id: str) -> None:
    """Record a digest-current #159 ``blocked`` decision through the real
    capture path (insert + project), so the recall resolver resolves it
    ``current`` with outcome ``blocked`` — the strongest local withhold,
    seeded against current state so V2 is the only other voice."""
    from sqlalchemy import select

    from engram.admission_assessment import (
        AdmissionDecision,
        digest,
        input_state_payload,
        insert_assessment,
        policy_config_payload,
        project_current,
    )
    from engram.db import apply_rls_context
    from engram.models import MemoryItem
    from engram.promotion import _config, _config_values, load_promotion_support

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
        await apply_rls_context(
            session, tenant_id=row["tenant_id"], principal_id=row["principal_id"]
        )
        item = await session.scalar(select(MemoryItem).where(MemoryItem.id == item_id))
        assert item is not None
        config = await _config(session, str(item.tenant_id))
        _, threshold, min_age, evidence_enabled, evidence_threshold = _config_values(config)
        support = (await load_promotion_support(session, [item]))[item.id]
        kind = support.kind
        decision = AdmissionDecision(
            tenant_id=item.tenant_id,
            memory_item_id=item.id,
            mode="authoritative",
            item_content_hash=item.content_hash,
            input_digest=digest(input_state_payload(item, support.classification_run)),
            resulting_state_digest=None,
            policy_config_digest=digest(
                policy_config_payload(
                    confidence_threshold=threshold,
                    min_age_hours=min_age,
                    evidence_enabled=evidence_enabled,
                    evidence_threshold=evidence_threshold,
                    kind_auto_promote_allowed=bool(
                        kind and kind.enabled and kind.auto_promote_from_inferred
                    ),
                )
            ),
            selected_basis=None,
            outcome="blocked",
            blocker_codes=("test_blocked",),
            reason_codes=("test_blocked",),
            decision_inputs={},
            conflict_recheck_status="not_run",
            cooling_period_start=None,
            eligible_at=None,
            next_evaluation_at=None,
            next_actions=("none",),
        )
        persisted = await insert_assessment(
            session,
            decision,
            trigger_type="test",
            trigger_id=f"test:{item_id}",
            invocation_source="test",
            evaluated_at=datetime.now(UTC),
        )
        await project_current(session, persisted)
        await session.commit()


async def test_blocked_path_a_binding_withholds_with_truthful_v2_disagreement(
    client, monkeypatch
):
    """BLOCKER 1 regression, end to end (#186 review): a current V2 allow that
    a digest-current #159 ``blocked`` binding withholds is reported as exactly
    that — resolution ``current``, surface ``allow``, ``gates_disagree`` true,
    full binding attached — on BOTH candidate profiles, never as absent V2
    state."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    item = await _remember(
        client, "semantic target blocked binding", source_type="extraction"
    )
    # Current V2 allow on the exact surface (low risk, qualified).
    await _persist_v2_row(item["id"])
    # A digest-current #159 blocked decision — the local boundary that wins.
    await _seed_path_a_blocked(item["id"])

    shadow = await _shadow_compare(client, profiles=["governed", "exploratory"])
    by_profile = {c["profile"]: c for c in shadow["candidates"]}
    assert by_profile["governed"]["item_count"] == 0
    assert by_profile["exploratory"]["item_count"] == 0
    for profile_key in ("governed", "exploratory"):
        packet = by_profile[profile_key]
        assert packet["omitted_by_admission"] == {"admission_blocked": 1}, profile_key
        diag = packet["admission_diagnostics"][0]
        assert diag["item_id"] == item["id"]
        assert diag["reason_codes"] == ["admission_blocked"]
        # The exact V2 state the local gate disagreed with stays visible.
        assert diag["v2_resolution_status"] == "current"
        assert diag["v2_surface_decision"] == "allow"
        assert diag["gates_disagree"] is True
        v2 = diag["v2"]
        assert v2["resolution_status"] == "current"
        assert v2["fresh"]["surface_decision"] == "allow"
        assert v2["persisted"]["assessment_id"] is not None
        assert v2["persisted"]["decision_hash"] == v2["fresh"]["decision_hash"]
        assert v2["persisted"]["policy_artifact_digest"] == v2["fresh"][
            "policy_artifact_digest"
        ]


# ---- audit ----


async def test_recall_log_records_only_servable_profiles(client, monkeypatch):
    """recall_logs records the effective served profile. Because serving is
    certified-legacy only, /v1/recall writes legacy/startup rows — never a
    governed/exploratory row, and the shadow surface writes nothing."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    await _remember(client, "semantic target")
    legacy = await _recall(client)
    startup_resp = await client.post("/v1/recall", json={"mode": "startup"})
    assert startup_resp.status_code == 200
    startup = startup_resp.json()
    await _shadow_compare(client, profiles=["governed", "exploratory"])

    async with _test_session_factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT recall_profile, scoring_version FROM recall_logs "
                        "WHERE id = ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": [legacy["recall_log_id"], startup["recall_log_id"]]},
                )
            )
            .mappings()
            .all()
        )
        all_profiles = (
            (await session.execute(text("SELECT DISTINCT recall_profile FROM recall_logs")))
            .scalars()
            .all()
        )
    by_profile = {row["recall_profile"]: row for row in rows}
    assert set(by_profile) == {"legacy", "startup"}
    assert by_profile["legacy"]["scoring_version"] == "semantic-v3"
    assert by_profile["startup"]["scoring_version"] == "v1"
    # The shadow comparison added no audit rows at all.
    assert set(all_profiles) == {"legacy", "startup"}


# ---- validation ----


async def test_startup_mode_rejects_semantic_profiles(client):
    await _skip_without_db()
    resp = await client.post(
        "/v1/recall",
        json={"mode": "startup", "recall_profile": "governed"},
    )
    assert resp.status_code == 422
    assert "requires mode='semantic'" in resp.json()["detail"]


async def test_unknown_profile_returns_422(client):
    """Unknown profiles are rejected by request validation (Literal union)
    before any embedding work; the error names the valid values."""
    await _skip_without_db()
    resp = await client.post(
        "/v1/recall",
        json={"mode": "semantic", "query": "x", "recall_profile": "review"},
    )
    assert resp.status_code == 422
    detail = str(resp.json()["detail"])
    assert "legacy" in detail and "governed" in detail and "exploratory" in detail


async def test_startup_profile_accepted_for_startup_mode(client):
    await _skip_without_db()
    resp = await client.post(
        "/v1/recall", json={"mode": "startup", "recall_profile": "startup"}
    )
    assert resp.status_code == 200
    assert resp.json()["recall_profile"] == "startup"


async def test_shadow_rejects_certified_or_unknown_profiles(client):
    await _skip_without_db()
    await _enable_tenant_shadow_policy()
    for profiles in (["legacy"], ["governed", "legacy"]):
        resp = await client.post(
            "/v1/recall/shadow-compare", json={"query": "q", "profiles": profiles}
        )
        assert resp.status_code == 422, resp.text
    resp = await client.post("/v1/recall/shadow-compare", json={"query": "q"})
    assert resp.status_code == 200  # default governed selection is valid


# ---- canonical served evidence state (issue #188) ---------------------------


def _evidence_identity_asserts(item: dict[str, Any]) -> None:
    """The #188 identity invariant, mechanically: every evidence field equals
    the ``admission.v2`` binding the admission decision consumed, and the
    top-level epistemic state mirrors the evidence block."""
    v2 = item["admission"]["v2"]
    fresh = v2["fresh"]
    evidence = item["evidence"]
    assert evidence["source"] == "v2_fresh_evaluation"
    assert evidence["profile_key"] == v2["profile_key"]
    assert evidence["policy_version"] == fresh["policy_version"]
    assert evidence["policy_artifact_digest"] == fresh["policy_artifact_digest"]
    assert evidence["decision_hash"] == fresh["decision_hash"]
    assert evidence["v2_resolution_status"] == v2["resolution_status"] == "current"
    assert evidence["epistemic_state"] == fresh["epistemic_state"]
    assert evidence["risk_state"] == fresh["risk_state"]
    assert evidence["retention_state"] == fresh["retention_state"]
    assert evidence["effective_assessment_refs"] == fresh["effective_assessment_refs"]
    assert item["epistemic_state"] == evidence["epistemic_state"]


async def test_served_evidence_state_is_the_exact_v2_fresh_evaluation(
    client, monkeypatch
):
    """Issue #188 required tests 1-6 against the real resolver path: for every
    #157 evidence state the exploratory surface admits (supported, unknown,
    contested, insufficient_evidence, high/unknown risk), the served packet
    preserves the exact V2 fresh state, mirrors it in the structured evidence
    block, and carries the stable warning code; governed admits only the
    qualified item and itemizes the rest as surface diagnostics."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    fixtures: dict[str, dict[str, Any]] = {}
    supported = await _remember(client, "evidence matrix supported", source_type="extraction")
    await _persist_v2_row(supported["id"], risk="low", epistemic_state="supported")
    fixtures[supported["id"]] = {
        "epistemic_state": "supported",
        "risk_state": "low",
        "warning_code": None,
        "governed_admitted": True,
        "governed_surface_decision": "allow",
    }
    absent = await _remember(client, "evidence matrix absent", source_type="extraction")
    await _persist_v2_row(absent["id"], risk=None)
    fixtures[absent["id"]] = {
        "epistemic_state": "unknown",
        "risk_state": "unknown",
        "warning_code": ("evidence_unknown", "risk_unknown"),
        "governed_admitted": False,
        # unknown risk hits the risk_unknown rule -> review_required output.
        "governed_surface_decision": "review_required",
    }
    contested = await _remember(client, "evidence matrix contested", source_type="extraction")
    await _persist_v2_row(contested["id"], risk="low", epistemic_state="contested")
    fixtures[contested["id"]] = {
        "epistemic_state": "contested",
        "risk_state": "low",
        "warning_code": ("evidence_contested",),
        "governed_admitted": False,
        "governed_surface_decision": "review_required",
    }
    insufficient = await _remember(
        client, "evidence matrix insufficient", source_type="extraction"
    )
    await _persist_v2_row(insufficient["id"], risk="low", epistemic_state="insufficient_evidence")
    fixtures[insufficient["id"]] = {
        "epistemic_state": "insufficient_evidence",
        "risk_state": "low",
        "warning_code": ("evidence_insufficient",),
        "governed_admitted": False,
        # epistemic_insufficient (low risk) -> withhold output on governed.
        "governed_surface_decision": "withhold",
    }
    high = await _remember(client, "evidence matrix high risk", source_type="extraction")
    await _persist_v2_row(high["id"], risk="high", epistemic_state="supported")
    fixtures[high["id"]] = {
        "epistemic_state": "supported",
        "risk_state": "high",
        "warning_code": ("risk_high",),
        "governed_admitted": False,
        "governed_surface_decision": "review_required",
    }

    shadow = await _shadow_compare(client, profiles=["governed", "exploratory"])
    governed = next(c for c in shadow["candidates"] if c["profile"] == "governed")
    exploratory = next(c for c in shadow["candidates"] if c["profile"] == "exploratory")

    # Exploratory: every exact semantic_exploratory allow is admitted with the
    # exact V2 state, its warning code, and no contradicting code.
    assert {i["id"] for i in exploratory["items"]} == set(fixtures)
    by_id = {i["id"]: i for i in exploratory["items"]}
    for item_id, expected in fixtures.items():
        item = by_id[item_id]
        _evidence_identity_asserts(item)
        assert item["evidence"]["epistemic_state"] == expected["epistemic_state"]
        assert item["evidence"]["risk_state"] == expected["risk_state"]
        codes = item["warning_codes"]
        for code in ("evidence_unknown", "evidence_contested", "evidence_insufficient",
                     "risk_high", "risk_unknown"):
            if expected["warning_code"] and code in expected["warning_code"]:
                assert code in codes, (item_id, code)
            else:
                assert code not in codes, (item_id, code)
        # The evidence/risk presentation never moved the rank inputs.
        from engram.recall_signals import compute_signal_rank_score

        assert item["score"] == compute_signal_rank_score(
            similarity=item["relevance_score"], utility=item["utility_score"]
        )

    # Governed: only the qualified (supported/low) item; every other
    # candidate's exact surface decision withheld it — no evidence block is
    # served for withheld candidates, and their diagnostic V2 identity stays
    # intact (required test 10).
    assert {i["id"] for i in governed["items"]} == {
        item_id for item_id, expected in fixtures.items() if expected["governed_admitted"]
    }
    for item in governed["items"]:
        _evidence_identity_asserts(item)
    diag_by_id = {d["item_id"]: d for d in governed["admission_diagnostics"]}
    for item_id, expected in fixtures.items():
        if expected["governed_admitted"]:
            assert item_id not in diag_by_id
            continue
        assert diag_by_id[item_id]["v2_resolution_status"] == "current"
        assert (
            diag_by_id[item_id]["v2_surface_decision"]
            == expected["governed_surface_decision"]
        )
        assert diag_by_id[item_id]["decision"] == "withhold"


async def test_evidence_presentation_adds_no_resolution_or_evaluation(
    client, monkeypatch
):
    """Issue #188 required tests 11-12: the evidence block is projected from
    the admission binding — the shared V2 evaluation core runs exactly once
    per candidate window (never a second pass for presentation), the bulk
    resolution runs once per packet, and no provider call happens beyond the
    one shared query embedding."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    for i in range(3):
        item = await _remember(client, f"evidence io {i}", source_type="extraction")
        await _persist_v2_row(item["id"])

    import engram.admission_shadow as admission_shadow_mod

    evaluation_calls = {"count": 0}
    original_evaluate = admission_shadow_mod._evaluate_shadow_decision

    def counting_evaluate(*args: Any, **kwargs: Any) -> Any:
        evaluation_calls["count"] += 1
        return original_evaluate(*args, **kwargs)

    monkeypatch.setattr(
        admission_shadow_mod, "_evaluate_shadow_decision", counting_evaluate
    )

    resolve_calls = {"count": 0}
    original_resolve = admission_shadow_mod.resolve_bulk_v2_decisions

    async def counting_resolve(*args: Any, **kwargs: Any) -> Any:
        resolve_calls["count"] += 1
        return await original_resolve(*args, **kwargs)

    monkeypatch.setattr(
        admission_shadow_mod, "resolve_bulk_v2_decisions", counting_resolve
    )

    import engram.embeddings as embeddings_mod
    from engram import recall as recall_mod

    provider_calls = {"count": 0}

    async def counting_embedding(text_value: str, *_args: object, **kwargs: object) -> Any:
        provider_calls["count"] += 1
        return _fake_embedding_for(text_value)

    monkeypatch.setattr(recall_mod, "generate_embedding", counting_embedding)
    monkeypatch.setattr(memory_routes, "generate_embedding", counting_embedding)
    monkeypatch.setattr(embeddings_mod, "generate_embedding", counting_embedding)

    shadow = await _shadow_compare(client, profiles=["governed", "exploratory"])

    # Two candidate packets, one bulk resolution each — never per item,
    # never a second presentation pass.
    assert resolve_calls["count"] == 2
    # Exactly one V2 evaluation per resolved item per packet: the evaluation
    # admission consumed is the same one presentation projects.
    resolved_total = sum(
        c["v2_resolution"]["resolved_count"] for c in shadow["candidates"]
    )
    assert resolved_total == 6  # 3 items x 2 packets
    assert evaluation_calls["count"] == resolved_total
    # One shared query embedding for the whole comparison (legacy + both
    # candidates) — evidence presentation never calls a provider.
    assert provider_calls["count"] == 1
    # The per-window query count equals a standalone resolution of the same
    # items: presentation happens after resolution and adds no query.
    from sqlalchemy import select as sa_select

    from engram.models import MemoryItem

    async with _test_session_factory() as session:
        from engram.db import apply_rls_context

        ids = (
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
        await apply_rls_context(
            session, tenant_id=ids["tenant_id"], principal_id=ids["principal_id"]
        )
        window = list(
            (
                await session.scalars(
                    sa_select(MemoryItem).where(MemoryItem.review_status == "proposed")
                )
            ).all()
        )
        context = _test_memory_context(ids["tenant_id"], ids["principal_id"])
        standalone = await original_resolve(
            session, items=window, context=context, evaluation_time=datetime.now(UTC)
        )
    packet_queries = {
        c["v2_resolution"]["query_count"] for c in shadow["candidates"]
    }
    assert packet_queries == {standalone.query_count}
    assert standalone.query_count > 0


async def test_shadow_compare_with_evidence_remains_read_only(client, monkeypatch):
    """Issue #188 required test 13: with evidence blocks in the candidate
    packets, the comparison still writes nothing — no recall log, no exposure
    counters, no review/promotion/assessment mutation."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    item = await _remember(client, "evidence read-only target", source_type="extraction")
    await _persist_v2_row(item["id"])
    logs_before = await _recall_log_count()
    counts_before = await _recall_counts([item["id"]])

    shadow = await _shadow_compare(client, profiles=["governed", "exploratory"])
    assert shadow["candidates"][0]["items"]
    assert shadow["candidates"][0]["items"][0]["evidence"]["source"] == "v2_fresh_evaluation"

    assert await _recall_log_count() == logs_before
    assert await _recall_counts([item["id"]]) == counts_before


# ---- admission-first relationship expansion (issue #190 / ENG-RECALL-003D) ----
#
# The candidate profiles expand through the bounded graph/tunnel mechanics,
# strictly after direct V2 admission: only admitted direct items seed
# discovery, every expanded neighbor is independently admitted through the
# exact V2 surface, and relationship-aware relevance (versioned,
# importance-free) feeds the separated utility ranking. All fixtures below
# make neighbors *expansion-only* by deleting their embedding row — semantic
# retrieval can then never surface them, so their presence in a packet is
# mechanical proof the expansion path (and only it) reached them.


async def _link_items(
    source_id: str, target_id: str, edge_type: str, *, weight: float | None = None
) -> None:
    async with _test_engine.begin() as conn:
        tenant_id = await conn.scalar(
            text("SELECT tenant_id::text FROM memory_items WHERE id = :id"), {"id": source_id}
        )
        await conn.execute(
            text(
                "INSERT INTO memory_edges (id, tenant_id, source_item_id, target_item_id, "
                "edge_type, weight) "
                "VALUES (gen_random_uuid(), :tenant, :src, :tgt, :et, :w)"
            ),
            {"tenant": tenant_id, "src": source_id, "tgt": target_id, "et": edge_type, "w": weight},
        )


async def _unlink_items(source_id: str, target_id: str) -> None:
    async with _test_engine.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM memory_edges WHERE source_item_id = :src AND target_item_id = :tgt"
            ),
            {"src": source_id, "tgt": target_id},
        )


async def _mk_tunnel(source_wing: str, target_wing: str, *, label: str | None = None) -> None:
    async with _test_engine.begin() as conn:
        tenant_id = await conn.scalar(text("SELECT id::text FROM tenants WHERE slug = 'default'"))
        await conn.execute(
            text(
                "INSERT INTO tunnels (id, tenant_id, source_wing, target_wing, label) "
                "VALUES (gen_random_uuid(), :tenant, :sw, :tw, :label)"
            ),
            {"tenant": tenant_id, "sw": source_wing, "tw": target_wing, "label": label},
        )


async def _make_expansion_only(item_id: str) -> None:
    """Remove the item's embedding so only relationship expansion can reach it."""
    async with _test_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM memory_embeddings WHERE memory_item_id = :id"), {"id": item_id}
        )


async def _update_item(item_id: str, **assignments: Any) -> None:
    if not assignments:
        return
    clause = ", ".join(f"{column} = :{column}" for column in assignments)
    async with _test_engine.begin() as conn:
        await conn.execute(
            text(f"UPDATE memory_items SET {clause} WHERE id = :id"),
            {"id": item_id, **assignments},
        )


async def _fabricate_v2_row(
    item_id: str,
    *,
    schema_version: str | None = None,
    policy_config_digest: str | None = None,
) -> None:
    """Append a newer V2-profile row with a chosen identity defect.

    The latest-row lookup (evaluated_at desc, id desc) resolves this row, so
    a wrong schema_version makes the item ``unsupported`` and a wrong policy
    digest makes it ``mismatched`` — otherwise shaped exactly like a real
    persisted V2 shadow row (the DB CHECK contract validates that shape).
    """
    from engram.admission_policy import V2_SCHEMA_VERSION
    from engram.models import AdmissionAssessment as AssessmentRow

    async with _test_session_factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT tenant_id::text, content_hash FROM memory_items WHERE id = :id"
                    ),
                    {"id": item_id},
                )
            )
            .mappings()
            .one()
        )
        session.add(
            AssessmentRow(
                id=uuid4(),
                tenant_id=row["tenant_id"],
                memory_item_id=item_id,
                schema_version=schema_version or V2_SCHEMA_VERSION,
                mode="shadow",
                trigger_type="test",
                trigger_id=f"test:{uuid4()}",
                invocation_source="test",
                evaluated_at=datetime.now(UTC) + timedelta(seconds=1),
                item_content_hash=row["content_hash"],
                input_digest="sha256:" + "0" * 64,
                policy_profile_key="risk_aware_shadow_v1",
                policy_contract_version=_V2_POLICY.policy_version,
                policy_config_digest=policy_config_digest or _V2_POLICY.artifact_digest,
                selected_basis=None,
                outcome="would_admit",
                blocker_codes=[],
                reason_codes=[],
                decision_inputs={},
                available_memory_assessment_refs=[],
                # The v2 shadow-contract CHECK requires the V2 column set.
                risk_state="low",
                epistemic_state="supported",
                retention_state="retain",
                effective_memory_assessment_refs=[],
                highest_admission_tier="semantic_governed",
                surface_decisions={
                    "semantic_exploratory": "allow",
                    "semantic_governed": "allow",
                    "startup": "withhold",
                },
                conflict_recheck_status="not_run",
                next_actions=[],
                decision_hash="sha256:" + "0" * 64,
            )
        )
        await session.commit()


async def _seed_qualified(
    client: AsyncClient, content: str, **payload: Any
) -> dict[str, Any]:
    """A live proposal with a current qualifying V2 row (low risk, supported)."""
    item = await _remember(client, content, source_type="extraction", **payload)
    await _persist_v2_row(item["id"])
    return item


async def _candidate_packet(
    client: AsyncClient, profile: str = "governed", **extra: Any
) -> dict[str, Any]:
    shadow = await _shadow_compare(client, profiles=[profile], **extra)
    return shadow["candidates"][0]


def _relationship(item: dict[str, Any]) -> dict[str, Any]:
    relationship = item.get("relationship")
    assert relationship is not None, "admitted expanded item must carry the relationship block"
    return relationship


# ---- admission-before-expansion ----


async def test_withheld_direct_candidate_cannot_seed_graph_expansion(
    client, monkeypatch
):
    """Required test 1: a direct candidate the exact V2 surface withholds
    never seeds expansion — its qualified graph neighbor is absent, has no
    diagnostic, and the packet records that no expansion ran at all."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    seed = await _remember(client, "semantic target withheld seed", source_type="extraction")
    await _persist_v2_row(seed["id"], risk=None)  # governed: review_required
    neighbor = await _seed_qualified(client, "graph neighbor of withheld seed")
    await _make_expansion_only(neighbor["id"])
    await _link_items(seed["id"], neighbor["id"], "derived_from", weight=1.0)

    governed = await _candidate_packet(client)
    assert governed["item_count"] == 0
    diagnostics = {d["item_id"]: d for d in governed["admission_diagnostics"]}
    # The seed itself is a DIRECT withhold; the neighbor was never discovered.
    assert diagnostics[seed["id"]]["origin"] == "direct"
    assert diagnostics[seed["id"]]["v2_surface_decision"] == "review_required"
    assert neighbor["id"] not in diagnostics
    assert governed["expansion"] is None  # zero admitted seeds -> no expansion


async def test_withheld_direct_candidate_cannot_seed_tunnel_expansion(
    client, monkeypatch
):
    """Required test 2: same boundary for tunnels — a withheld seed's wing
    membership reveals nothing."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    seed = await _remember(
        client,
        "semantic target withheld tunnel seed",
        source_type="extraction",
        wing="WithheldWing",
        room="src",
    )
    await _persist_v2_row(seed["id"], risk=None)
    neighbor = await _seed_qualified(
        client, "tunnel neighbor of withheld seed", wing="TunnelTarget", room="dst"
    )
    await _make_expansion_only(neighbor["id"])
    await _mk_tunnel("WithheldWing", "TunnelTarget", label="blocked")

    governed = await _candidate_packet(client)
    assert governed["item_count"] == 0
    diagnostics = {d["item_id"] for d in governed["admission_diagnostics"]}
    assert neighbor["id"] not in diagnostics
    assert governed["expansion"] is None


async def test_admitted_seed_discovers_qualified_graph_neighbor(client, monkeypatch):
    """Required tests 3 + 5: an admitted direct seed discovers an eligible
    graph neighbor, and that neighbor is admitted only through its own
    current + allow decision on the exact governed surface."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    seed = await _seed_qualified(client, "semantic target graph seed")
    neighbor = await _seed_qualified(client, "qualified graph neighbor content")
    await _make_expansion_only(neighbor["id"])
    await _link_items(seed["id"], neighbor["id"], "derived_from")

    governed = await _candidate_packet(client)
    by_id = {item["id"]: item for item in governed["items"]}
    assert set(by_id) == {seed["id"], neighbor["id"]}
    expanded = by_id[neighbor["id"]]
    # The neighbor presents the full admitted-candidate evidence identity.
    assert expanded["admission"]["surface"] == "semantic_governed"
    assert expanded["admission"]["surface_decision"] == "allow"
    assert expanded["admission"]["v2"]["resolution_status"] == "current"
    _evidence_identity_asserts(expanded)
    # Expansion-only: no vector was ever compared against it.
    assert expanded["distance"] is None
    assert expanded["similarity_score"] is None
    relationship = _relationship(expanded)
    assert relationship["version"] == "relationship-relevance-v1"
    assert relationship["origins"] == ["graph"]
    assert relationship["direct"] is False
    assert relationship["graph_edge_types"] == ["derived_from"]
    assert relationship["relevance_score"] == expanded["relevance_score"]
    assert any("linked via derived_from" in reason for reason in expanded["reasons"])
    assert governed["expansion"] == {
        "version": "relationship-relevance-v1",
        "seed_count": 1,
        "discovered_neighbors": 1,
        "graph_neighbors": 1,
        "tunnel_neighbors": 0,
        "admitted_expanded": 1,
        "withheld_expanded": 0,
    }
    assert governed["v2_resolution"]["resolved_count"] == 2
    assert governed["v2_resolution"]["resolution_status_counts"] == {"current": 2}


async def test_admitted_seed_discovers_qualified_tunnel_neighbor(client, monkeypatch):
    """Required test 4: an admitted seed's tunnel membership discovers an
    eligible neighbor in the tunneled (wing, room)."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    await _seed_qualified(
        client, "semantic target tunnel seed", wing="TunnelSeed", room="ops"
    )
    neighbor = await _seed_qualified(
        client, "qualified tunnel neighbor content", wing="TunnelFar", room="run"
    )
    await _make_expansion_only(neighbor["id"])
    await _mk_tunnel("TunnelSeed", "TunnelFar", label="ops-link")

    governed = await _candidate_packet(client)
    by_id = {item["id"]: item for item in governed["items"]}
    assert neighbor["id"] in by_id
    expanded = by_id[neighbor["id"]]
    _evidence_identity_asserts(expanded)
    relationship = _relationship(expanded)
    assert relationship["origins"] == ["tunnel"]
    assert relationship["tunnel_labels"] == ["ops-link"]
    assert relationship["graph_edge_types"] == []
    assert any('same tunnel "ops-link"' in r for r in expanded["reasons"])
    assert governed["expansion"]["tunnel_neighbors"] == 1
    assert governed["expansion"]["admitted_expanded"] == 1


# ---- independent neighbor admission ----


async def test_neighbor_withheld_by_surface_despite_strongest_edge(client, monkeypatch):
    """Required test 6: a maximal-weight edge cannot turn a review_required
    governed decision into an admission; the withhold is diagnosed with its
    graph origin."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    seed = await _seed_qualified(client, "semantic target strong edge seed")
    neighbor = await _remember(client, "review routed graph neighbor", source_type="extraction")
    await _persist_v2_row(neighbor["id"], risk=None)  # governed: review_required
    await _make_expansion_only(neighbor["id"])
    await _link_items(seed["id"], neighbor["id"], "supports", weight=1.0)

    governed = await _candidate_packet(client)
    assert {item["id"] for item in governed["items"]} == {seed["id"]}
    assert governed["omitted_by_admission"].get("v2_surface_review_required") == 1
    diagnostic = next(
        d for d in governed["admission_diagnostics"] if d["item_id"] == neighbor["id"]
    )
    assert diagnostic["origin"] == "graph"
    assert diagnostic["v2_resolution_status"] == "current"
    assert diagnostic["v2_surface_decision"] == "review_required"
    assert governed["expansion"]["withheld_expanded"] == 1


async def test_exploratory_high_risk_neighbor_admitted_on_its_own_surface(
    client, monkeypatch
):
    """Required test 7: a high-risk neighbor enters the exploratory packet
    only because the exact exploratory surface allows it, with the canonical
    risk warning preserved; governed withholds the same neighbor."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    seed = await _seed_qualified(client, "semantic target risk seed")
    neighbor = await _remember(client, "high risk graph neighbor", source_type="extraction")
    await _persist_v2_row(neighbor["id"], risk="high", epistemic_state="supported")
    await _make_expansion_only(neighbor["id"])
    await _link_items(seed["id"], neighbor["id"], "references")

    governed = await _candidate_packet(client)
    assert neighbor["id"] not in {item["id"] for item in governed["items"]}
    gov_diag = {d["item_id"]: d for d in governed["admission_diagnostics"]}
    assert gov_diag[neighbor["id"]]["v2_surface_decision"] == "review_required"

    exploratory = await _candidate_packet(client, profile="exploratory")
    by_id = {item["id"]: item for item in exploratory["items"]}
    assert neighbor["id"] in by_id
    item = by_id[neighbor["id"]]
    assert item["admission"]["surface"] == "semantic_exploratory"
    assert "risk_high" in item["warning_codes"]
    _evidence_identity_asserts(item)
    assert _relationship(item)["origins"] == ["graph"]


async def test_noncurrent_expanded_neighbors_fail_closed_with_diagnostics(
    client, monkeypatch
):
    """Required test 8: missing | stale | mismatched | unsupported expanded
    neighbors are withheld, diagnostic-only, each with its own resolution
    status and origin."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    seed = await _seed_qualified(client, "semantic target noncurrent seed")

    missing = await _remember(client, "never assessed neighbor", source_type="extraction")
    await _make_expansion_only(missing["id"])
    await _link_items(seed["id"], missing["id"], "references")

    stale = await _remember(client, "stale neighbor", source_type="extraction")
    await _persist_v2_row(stale["id"])
    await _update_item(stale["id"], human_verified=True)  # changes the fresh hash
    await _make_expansion_only(stale["id"])
    await _link_items(seed["id"], stale["id"], "references")

    unsupported = await _remember(client, "unsupported neighbor", source_type="extraction")
    await _persist_v2_row(unsupported["id"])
    await _fabricate_v2_row(unsupported["id"], schema_version="engram.admission-assessment.v1")
    await _make_expansion_only(unsupported["id"])
    await _link_items(seed["id"], unsupported["id"], "references")

    mismatched = await _remember(client, "mismatched neighbor", source_type="extraction")
    await _persist_v2_row(mismatched["id"])
    await _fabricate_v2_row(mismatched["id"], policy_config_digest="sha256:" + "f" * 64)
    await _make_expansion_only(mismatched["id"])
    await _link_items(seed["id"], mismatched["id"], "references")

    governed = await _candidate_packet(client)
    assert {item["id"] for item in governed["items"]} == {seed["id"]}
    diagnostics = {d["item_id"]: d for d in governed["admission_diagnostics"]}
    expected = {
        missing["id"]: ("missing", "v2_decision_missing"),
        stale["id"]: ("stale", "v2_decision_stale"),
        unsupported["id"]: ("unsupported", "v2_decision_unsupported"),
        mismatched["id"]: ("mismatched", "v2_decision_mismatched"),
    }
    for item_id, (status, reason) in expected.items():
        assert diagnostics[item_id]["origin"] == "graph", item_id
        assert diagnostics[item_id]["v2_resolution_status"] == status, item_id
        assert diagnostics[item_id]["reason_codes"] == [reason], item_id
    assert governed["expansion"]["withheld_expanded"] == 4
    assert governed["expansion"]["admitted_expanded"] == 0


async def test_supports_edge_cannot_upgrade_epistemic_state(client, monkeypatch):
    """Required test 9: a maximal supports edge leaves the V2 epistemic state
    unknown — relationship is relevance, never evidence."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    seed = await _seed_qualified(client, "semantic target epistemic seed")
    neighbor = await _remember(client, "unknown evidence neighbor", source_type="extraction")
    await _persist_v2_row(neighbor["id"], risk=None)  # epistemic: unknown
    await _make_expansion_only(neighbor["id"])
    await _link_items(seed["id"], neighbor["id"], "supports", weight=1.0)

    exploratory = await _candidate_packet(client, profile="exploratory")
    by_id = {item["id"]: item for item in exploratory["items"]}
    assert neighbor["id"] in by_id  # the exploratory surface allows it
    item = by_id[neighbor["id"]]
    assert item["epistemic_state"] == "unknown"
    assert item["evidence"]["epistemic_state"] == "unknown"
    assert "evidence_unknown" in item["warning_codes"]
    assert _relationship(item)["graph_edge_types"] == ["supports"]
    assert item["admission"]["surface_decision"] == "allow"


async def test_high_importance_neighbor_cannot_bypass_v2_withholding(
    client, monkeypatch
):
    """Required test 10: importance is utility — it can never buy admission."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    seed = await _seed_qualified(client, "semantic target importance seed")
    neighbor = await _remember(
        client, "important but unqualified neighbor", source_type="extraction", importance=1.0
    )
    await _persist_v2_row(neighbor["id"], risk=None)
    await _make_expansion_only(neighbor["id"])
    await _link_items(seed["id"], neighbor["id"], "derived_from", weight=1.0)

    governed = await _candidate_packet(client)
    assert neighbor["id"] not in {item["id"] for item in governed["items"]}
    assert governed["omitted_by_admission"].get("v2_surface_review_required") == 1


# ---- relevance/utility separation ----


async def test_importance_moves_utility_and_rank_but_not_relevance(client, monkeypatch):
    """Required test 11: changing only the neighbor's importance changes its
    utility and final rank, never its relationship relevance, evidence
    state, or admission."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    seed = await _seed_qualified(client, "semantic target utility seed")
    neighbor = await _seed_qualified(client, "utility probe neighbor", importance=0.1)
    await _make_expansion_only(neighbor["id"])
    await _link_items(seed["id"], neighbor["id"], "derived_from")

    def _snapshot(packet: dict[str, Any]) -> dict[str, Any]:
        item = next(i for i in packet["items"] if i["id"] == neighbor["id"])
        return {
            "relevance": item["relevance_score"],
            "relationship": item["relationship"],
            "evidence": item["evidence"],
            "admission": item["admission"],
            "utility": item["utility_score"],
            "score": item["score"],
        }

    before = _snapshot(await _candidate_packet(client, profile="exploratory"))
    await _update_item(neighbor["id"], importance=0.9)
    after = _snapshot(await _candidate_packet(client, profile="exploratory"))

    assert before["relevance"] == after["relevance"]
    assert before["relationship"] == after["relationship"]
    assert before["evidence"] == after["evidence"]
    assert before["admission"] == after["admission"]
    assert before["utility"] < after["utility"]
    assert before["score"] < after["score"]


async def test_edge_strength_moves_relevance_but_not_utility_or_admission(
    client, monkeypatch
):
    """Required test 12: changing only the edge weight changes relationship
    relevance, never utility, epistemic state, or admission."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    seed = await _seed_qualified(client, "semantic target edge weight seed")
    neighbor = await _seed_qualified(client, "edge weight probe neighbor")
    await _make_expansion_only(neighbor["id"])
    await _link_items(seed["id"], neighbor["id"], "supports", weight=0.3)

    def _snapshot(packet: dict[str, Any]) -> dict[str, Any]:
        item = next(i for i in packet["items"] if i["id"] == neighbor["id"])
        return {
            "relevance": item["relevance_score"],
            "graph_contribution": item["relationship"]["graph_contribution"],
            "utility": item["utility_score"],
            "evidence": item["evidence"],
            "admission": item["admission"],
        }

    weak = _snapshot(await _candidate_packet(client, profile="exploratory"))
    await _unlink_items(seed["id"], neighbor["id"])
    await _link_items(seed["id"], neighbor["id"], "supports", weight=0.9)
    strong = _snapshot(await _candidate_packet(client, profile="exploratory"))

    assert weak["graph_contribution"] < strong["graph_contribution"]
    assert weak["relevance"] < strong["relevance"]
    assert weak["utility"] == strong["utility"]
    assert weak["evidence"] == strong["evidence"]
    assert weak["admission"] == strong["admission"]


async def test_epistemic_and_utility_inputs_cannot_move_relationship_relevance(
    client, monkeypatch
):
    """Required test 13: source trust, memory confidence, human verification,
    and exposure counters are neither relevance nor admission inputs — for a
    fixed expansion/admission binding they change nothing. (human_verified
    IS an input to the V2 decision hash, so flipping it re-binds the row;
    re-persisting restores the identical binding and proves relevance never
    moved.)"""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    seed = await _seed_qualified(client, "semantic target exclusion seed")
    neighbor = await _seed_qualified(client, "exclusion probe neighbor")
    await _make_expansion_only(neighbor["id"])
    await _link_items(seed["id"], neighbor["id"], "derived_from")

    def _snapshot(packet: dict[str, Any]) -> dict[str, Any]:
        item = next(i for i in packet["items"] if i["id"] == neighbor["id"])
        return {
            "relevance": item["relevance_score"],
            "relationship": item["relationship"],
            "evidence": item["evidence"],
            "admission": item["admission"],
        }

    before = _snapshot(await _candidate_packet(client))
    await _update_item(
        neighbor["id"],
        source_trust=0.05,
        memory_confidence=0.05,
        recall_count=99,
        last_recalled_at=datetime.now(UTC),
    )
    after = _snapshot(await _candidate_packet(client))
    assert before == after

    # human_verified moves the V2 decision hash (it is epistemic input to
    # the policy), so the binding re-persists first; with the binding fixed
    # again, relevance is still exactly what it was.
    await _update_item(neighbor["id"], human_verified=True)
    async with _test_engine.begin() as conn:
        # classification_runs is one-per-item; dropping the old run cascades
        # its #157 assessment so the re-persist can rebuild both fresh.
        await conn.execute(
            text("DELETE FROM classification_runs WHERE memory_item_id = :id"),
            {"id": neighbor["id"]},
        )
    await _persist_v2_row(neighbor["id"])
    rebound = _snapshot(await _candidate_packet(client))
    assert rebound["relevance"] == before["relevance"]
    assert rebound["relationship"] == before["relationship"]
    assert rebound["admission"]["decision"] == "admit"


async def test_direct_and_expanded_origin_merge_is_deterministic(client, monkeypatch):
    """Required test 14: a direct hit that is also a graph neighbor merges
    origins deterministically and exposes the structured components."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    seed_a = await _seed_qualified(client, "semantic target merge a")
    seed_b = await _seed_qualified(client, "semantic target merge b")
    await _link_items(seed_b["id"], seed_a["id"], "supports", weight=0.6)

    first = await _candidate_packet(client)
    second = await _candidate_packet(client)
    by_id = {item["id"]: item for item in first["items"]}
    merged = by_id[seed_a["id"]]
    # The merged item keeps its direct fields and gains the graph origin.
    assert merged["distance"] is not None
    assert merged["similarity_score"] is not None
    relationship = _relationship(merged)
    assert relationship["origins"] == ["semantic", "graph"]
    assert relationship["direct"] is True
    assert relationship["direct_semantic_score"] == merged["similarity_score"]
    assert relationship["graph_edge_types"] == ["supports"]
    assert set(relationship["components"]) == {"semantic", "graph", "tunnel"}
    assert any("linked via supports" in r for r in merged["reasons"])
    # Deterministic: identical packet on re-evaluation.
    assert first["items"] == second["items"]
    assert [i["id"] for i in first["items"]] == [i["id"] for i in second["items"]]


async def test_candidate_rank_reproducible_from_published_inputs(client, monkeypatch):
    """Required test 15: every packet item's rank is recomputable from its
    published relevance + utility — direct, expanded, and merged alike."""
    from engram.recall_signals import compute_signal_rank_score

    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    seed = await _seed_qualified(client, "semantic target reproducibility seed")
    graph_neighbor = await _seed_qualified(client, "reproducibility graph neighbor")
    await _make_expansion_only(graph_neighbor["id"])
    await _link_items(seed["id"], graph_neighbor["id"], "derived_from")

    for profile in ("governed", "exploratory"):
        packet = await _candidate_packet(client, profile=profile)
        assert len(packet["items"]) == 2
        for item in packet["items"]:
            assert item["score"] == compute_signal_rank_score(
                similarity=item["relevance_score"], utility=item["utility_score"]
            )


# ---- security / RLS boundaries through edges and tunnels ----


async def test_cross_tenant_neighbor_is_never_discoverable(client, monkeypatch):
    """Required test 16: an edge row can name a foreign item, but the
    neighbor is never discovered or diagnosed."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    from engram.models import MemoryItem as MemoryItemRow
    from engram.models import Principal as PrincipalRow
    from engram.models import Tenant as TenantRow

    seed = await _seed_qualified(client, "semantic target cross tenant seed")
    async with _test_session_factory() as session:
        tenant = TenantRow(name="exp190 other", slug=f"exp190-{uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        principal = PrincipalRow(tenant_id=tenant.id, name="exp190-agent", type="agent")
        session.add(principal)
        await session.flush()
        foreign = MemoryItemRow(
            tenant_id=tenant.id,
            principal_id=principal.id,
            content="foreign secret",
            content_hash=f"h-{uuid4()}",
            kind="fact",
            visibility="tenant",
            review_status="proposed",
        )
        session.add(foreign)
        await session.commit()
        foreign_id = str(foreign.id)
    # The edge row lives in the caller's tenant but points at the foreign item.
    await _link_items(seed["id"], foreign_id, "derived_from", weight=1.0)

    governed = await _candidate_packet(client)
    assert foreign_id not in {item["id"] for item in governed["items"]}
    assert foreign_id not in {d["item_id"] for d in governed["admission_diagnostics"]}


async def test_private_neighbor_is_undiscoverable_and_undiagnosable(client, monkeypatch):
    """Required test 17: another principal's private neighbor is invisible —
    not in the packet, not even diagnosable by identity."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    seed = await _seed_qualified(client, "semantic target private seed")
    neighbor = await _remember(
        client,
        "private neighbor of seed",
        source_type="extraction",
        visibility="private",
    )
    await _persist_v2_row(neighbor["id"])
    async with _test_engine.begin() as conn:
        other_principal = str(uuid4())
        principal_name = f"exp190-private-{other_principal[:8]}"
        tenant_id = await conn.scalar(
            text("SELECT tenant_id::text FROM memory_items WHERE id = :id"), {"id": seed["id"]}
        )
        await conn.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES (:pid, :tid, :pname, 'agent')"
            ),
            {"pid": other_principal, "tid": tenant_id, "pname": principal_name},
        )
        await conn.execute(
            text("UPDATE memory_items SET principal_id = :pid WHERE id = :id"),
            {"pid": other_principal, "id": neighbor["id"]},
        )
    await _link_items(seed["id"], neighbor["id"], "derived_from", weight=1.0)

    governed = await _candidate_packet(client)
    assert neighbor["id"] not in {item["id"] for item in governed["items"]}
    assert neighbor["id"] not in {d["item_id"] for d in governed["admission_diagnostics"]}
    # An inaccessible seed reveals nothing either: the only admitted item is
    # the seed itself (required test 19's non-disclosure property).
    assert governed["expansion"]["discovered_neighbors"] == 0


async def test_out_of_workspace_neighbor_unreachable_through_edge(client, monkeypatch):
    """Required test 18: a workspace-scoped comparison cannot reach a
    qualified neighbor outside the workspace, even through a visible edge."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    async with _test_engine.begin() as conn:
        tenant_id = await conn.scalar(text("SELECT id::text FROM tenants WHERE slug = 'default'"))
        admin_id = await conn.scalar(
            text("SELECT p.id::text FROM principals p JOIN tenants t ON t.id = p.tenant_id "
                 "WHERE t.slug = 'default' AND p.name = 'admin'")
        )
        workspace_id = str(uuid4())
        workspace_slug = f"exp190-ws-{workspace_id[:8]}"
        await conn.execute(
            text(
                "INSERT INTO workspaces (id, tenant_id, name, slug) "
                "VALUES (:id, :tid, 'exp190 ws', :slug)"
            ),
            {"id": workspace_id, "tid": tenant_id, "slug": workspace_slug},
        )
        await conn.execute(
            text(
                "INSERT INTO workspace_members (id, workspace_id, principal_id, role) "
                "VALUES (gen_random_uuid(), :ws, :pid, 'member')"
            ),
            {"ws": workspace_id, "pid": admin_id},
        )

    seed = await _seed_qualified(
        client, "semantic target workspace seed", workspace=workspace_slug
    )
    neighbor = await _seed_qualified(client, "out of workspace neighbor")
    await _make_expansion_only(neighbor["id"])
    await _link_items(seed["id"], neighbor["id"], "derived_from", weight=1.0)

    scoped = await _candidate_packet(client, workspace=workspace_slug)
    assert {item["id"] for item in scoped["items"]} == {seed["id"]}
    assert neighbor["id"] not in {d["item_id"] for d in scoped["admission_diagnostics"]}
    assert scoped["expansion"]["discovered_neighbors"] == 0


async def test_inaccessible_item_cannot_be_used_to_infer_neighbors(client, monkeypatch):
    """Required test 19: an item the caller cannot read sits between the
    admitted seed and further neighbors. Neither the inaccessible item nor
    its own neighbors may be discovered, diagnosed, or inferred — the packet
    and its diagnostics reveal nothing about them."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    seed = await _seed_qualified(client, "semantic target inaccessible seed")
    # An edge-visible but unreadable middle item (another principal's
    # private proposal), itself linked to a fully qualified neighbor.
    middle = await _remember(
        client, "private middle item", source_type="extraction", visibility="private"
    )
    far = await _seed_qualified(client, "qualified far neighbor of private middle")
    await _make_expansion_only(middle["id"])
    await _make_expansion_only(far["id"])
    async with _test_engine.begin() as conn:
        other_principal = str(uuid4())
        tenant_id = await conn.scalar(
            text("SELECT tenant_id::text FROM memory_items WHERE id = :id"), {"id": seed["id"]}
        )
        await conn.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES (:pid, :tid, :pname, 'agent')"
            ),
            {
                "pid": other_principal,
                "tid": tenant_id,
                "pname": f"exp190-infer-{other_principal[:8]}",
            },
        )
        await conn.execute(
            text("UPDATE memory_items SET principal_id = :pid WHERE id = :id"),
            {"pid": other_principal, "id": middle["id"]},
        )
    await _link_items(seed["id"], middle["id"], "derived_from", weight=1.0)
    await _link_items(middle["id"], far["id"], "derived_from", weight=1.0)

    governed = await _candidate_packet(client)
    assert {item["id"] for item in governed["items"]} == {seed["id"]}
    diagnosed = {d["item_id"] for d in governed["admission_diagnostics"]}
    assert middle["id"] not in diagnosed
    assert far["id"] not in diagnosed
    assert governed["expansion"]["discovered_neighbors"] == 0


# ---- performance / boundedness ----


async def test_expansion_adds_no_provider_call(client, monkeypatch):
    """Required test 20: with graph+tunnel expansion active, the comparison
    still makes exactly one provider call — the shared query embedding."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    seed = await _seed_qualified(client, "semantic target provider seed")
    neighbor = await _seed_qualified(client, "provider probe neighbor")
    await _make_expansion_only(neighbor["id"])
    await _link_items(seed["id"], neighbor["id"], "derived_from")
    tunnel_neighbor = await _seed_qualified(
        client, "provider tunnel neighbor", wing="ProvWing", room="a"
    )
    await _seed_qualified(
        client, "semantic target provider wing seed", wing="ProvWing", room="a"
    )
    await _make_expansion_only(tunnel_neighbor["id"])
    await _mk_tunnel("ProvWing", "ProvOtherWing")

    # Seed first, THEN start counting: only the comparison itself is measured.
    provider_calls = {"count": 0}

    async def counting_embedding(text_value: str, *_args: object, **_kwargs: object) -> list[float]:
        provider_calls["count"] += 1
        return _fake_embedding_for(text_value)

    import engram.embeddings as embeddings_mod
    from engram import recall as recall_mod
    from engram.api.routes import memory as memory_routes

    monkeypatch.setattr(recall_mod, "generate_embedding", counting_embedding)
    monkeypatch.setattr(memory_routes, "generate_embedding", counting_embedding)
    monkeypatch.setattr(embeddings_mod, "generate_embedding", counting_embedding)

    await _shadow_compare(client, profiles=["governed", "exploratory"])
    assert provider_calls["count"] == 1


async def test_expanded_neighbor_resolution_is_bulk_without_nplus1(client, monkeypatch):
    """Required test 21: one bulk resolution per window — query count stays
    constant as the neighbor count grows within the configured bounds."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    import engram.admission_shadow as admission_shadow_mod

    resolve_calls = {"count": 0}
    original_resolve = admission_shadow_mod.resolve_bulk_v2_decisions

    async def counting_resolve(*args: Any, **kwargs: Any) -> Any:
        resolve_calls["count"] += 1
        return await original_resolve(*args, **kwargs)

    monkeypatch.setattr(admission_shadow_mod, "resolve_bulk_v2_decisions", counting_resolve)
    monkeypatch.setattr(settings, "max_graph_neighbors_per_item", 10)

    seed = await _seed_qualified(client, "semantic target bulk seed")

    async def add_neighbors(count: int, prefix: str) -> list[str]:
        ids = []
        for i in range(count):
            neighbor = await _seed_qualified(client, f"{prefix} neighbor {i:02d}")
            await _make_expansion_only(neighbor["id"])
            await _link_items(seed["id"], neighbor["id"], "references")
            ids.append(neighbor["id"])
        return ids

    few = await add_neighbors(2, "bulk few")
    packet_few = await _candidate_packet(client)
    assert len(few) == packet_few["expansion"]["admitted_expanded"]

    await add_neighbors(4, "bulk many")  # 6 neighbors total, within the caps
    packet_many = await _candidate_packet(client)
    assert packet_many["expansion"]["admitted_expanded"] == 6

    # Direct window + neighbor window: exactly two bulk resolutions per
    # packet regardless of neighbor count.
    assert resolve_calls["count"] == 4  # two packets x two windows
    # Merged per-packet query count is constant in neighbor count.
    assert packet_few["v2_resolution"]["query_count"] == packet_many[
        "v2_resolution"
    ]["query_count"]
    assert packet_many["v2_resolution"]["resolved_count"] == 7  # 1 direct + 6 neighbors


async def test_graph_and_tunnel_caps_remain_enforced(client, monkeypatch):
    """Required test 22: expansion cannot exceed the configured bounded
    windows."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()
    monkeypatch.setattr(settings, "max_graph_expanded_items", 2)
    monkeypatch.setattr(settings, "max_graph_neighbors_per_item", 5)
    monkeypatch.setattr(settings, "max_tunnel_additions", 1)

    seed = await _seed_qualified(client, "semantic target caps seed")
    for i in range(4):
        neighbor = await _seed_qualified(client, f"caps graph neighbor {i}")
        await _make_expansion_only(neighbor["id"])
        await _link_items(seed["id"], neighbor["id"], "derived_from", weight=0.9 - i * 0.1)
    tunnel_extra = await _seed_qualified(
        client, "caps tunnel neighbor", wing="CapsWing", room="src"
    )
    # A second admitted direct seed in the tunneled wing drives discovery.
    await _seed_qualified(
        client, "semantic target caps tunnel seed", wing="CapsFar", room="dst"
    )
    await _make_expansion_only(tunnel_extra["id"])
    await _mk_tunnel("CapsWing", "CapsFar")

    governed = await _candidate_packet(client)
    # max_graph_expanded_items=2 of the 4 graph neighbors, strongest first.
    assert governed["expansion"]["graph_neighbors"] == 2
    # Tunnel additions capped at 1 (the tunnel neighbor; the seed itself is
    # excluded as a direct candidate).
    assert governed["expansion"]["tunnel_neighbors"] == 1
    assert governed["expansion"]["discovered_neighbors"] <= 3


async def test_equal_score_tie_ordering_is_deterministic(client, monkeypatch):
    """Required test 23: two neighbors with identical relevance, utility,
    and timestamps keep a stable order (id-ordered) across evaluations."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    seed = await _seed_qualified(client, "semantic target tie seed")
    anchors = datetime.now(UTC) - timedelta(days=7)
    neighbors = []
    for i in range(2):
        neighbor = await _remember(client, f"tie neighbor {i}", source_type="extraction")
        await _make_expansion_only(neighbor["id"])
        await _link_items(seed["id"], neighbor["id"], "references", weight=0.5)
        # Identical timestamps/weights/importance BEFORE persisting the V2
        # row, so both neighbors stay current with identical rank inputs.
        await _update_item(neighbor["id"], created_at=anchors, valid_from=anchors)
        await _persist_v2_row(neighbor["id"])
        neighbors.append(neighbor)

    first = await _candidate_packet(client)
    second = await _candidate_packet(client)
    order_first = [item["id"] for item in first["items"]]
    order_second = [item["id"] for item in second["items"]]
    assert order_first == order_second
    scores = [item["score"] for item in first["items"]]
    assert scores[1] == scores[2]  # the tied neighbors
    # Equal on every ranking input: the stable order is the id tiebreak.
    assert order_first[1:] == sorted(order_first[1:])


# ---- compatibility / read-only ----


async def test_expansion_shadow_comparison_remains_read_only(client, monkeypatch):
    """Required tests 24 + 25: with expansion active the comparison writes
    nothing — no recall log, no exposure counter, no review/promotion/
    assessment mutation — and the authoritative legacy packet keeps its
    legacy-only shape (blended scoring; no signal/evidence/relationship
    blocks). Legacy byte-compatibility itself is pinned by the pre-existing
    legacy suites (test_relationship_recall / test_graph_recall /
    test_tunnel_recall / legacy semantic tests), which this change runs
    unchanged."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    seed = await _seed_qualified(client, "semantic target readonly seed")
    neighbor = await _seed_qualified(client, "readonly neighbor")
    await _make_expansion_only(neighbor["id"])
    await _link_items(seed["id"], neighbor["id"], "derived_from")

    async def _snapshot_state() -> tuple[int, list[int], dict[str, Any], int]:
        async with _test_session_factory() as session:
            logs = int(await session.scalar(text("SELECT count(*) FROM recall_logs")))
            counts = list(
                (
                    await session.execute(
                        text(
                            "SELECT recall_count FROM memory_items "
                            "WHERE id = ANY(CAST(:ids AS uuid[])) ORDER BY id"
                        ),
                        {"ids": [seed["id"], neighbor["id"]]},
                    )
                )
                .scalars()
                .all()
            )
            reviews = dict(
                (
                    await session.execute(
                        text("SELECT id::text, review_status FROM memory_items")
                    )
                ).all()
            )
            assessments = int(
                await session.scalar(text("SELECT count(*) FROM admission_assessments"))
            )
        return logs, counts, reviews, assessments

    before = await _snapshot_state()
    shadow = await _shadow_compare(client, profiles=["governed", "exploratory"])
    after = await _snapshot_state()
    assert before == after

    legacy_served = await _recall(client)
    # The authoritative legacy write path is the only one that may write;
    # its counts prove the comparison itself wrote nothing (the shadow left
    # exactly one recall log and +1 exposure counters, not two of each).
    async with _test_session_factory() as session:
        logs = int(await session.scalar(text("SELECT count(*) FROM recall_logs")))
        counts = list(
            (
                await session.execute(
                    text(
                        "SELECT recall_count FROM memory_items "
                        "WHERE id = ANY(CAST(:ids AS uuid[])) ORDER BY id"
                    ),
                    {"ids": [seed["id"], neighbor["id"]]},
                )
            )
            .scalars()
            .all()
        )
    assert logs == before[0] + 1
    assert counts == [count + 1 for count in before[1]]
    # The candidate packet did expand (the boundary under test is real).
    governed = next(c for c in shadow["candidates"] if c["profile"] == "governed")
    assert governed["expansion"]["admitted_expanded"] == 1
    # The legacy served packet is untouched by the candidate expansion
    # machinery: blended scoring, trust_score present, and — where the legacy
    # compatibility expansion surfaces the same neighbor — purely legacy
    # fields (no signal/evidence/relationship blocks).
    assert legacy_served["scoring_version"] == "semantic-v3"
    assert legacy_served["recall_profile"] == "legacy"
    for item in legacy_served["items"]:
        assert "trust_score" in item
        assert "relationship" not in item
        assert "evidence" not in item
        assert "warning_codes" not in item
    # And production remains legacy-only (required test 26).
    from engram.recall_profiles import CERTIFIED_SERVING_PROFILES

    assert sorted(CERTIFIED_SERVING_PROFILES) == ["legacy"]
    resp = await client.post(
        "/v1/recall",
        json={"mode": "semantic", "query": "q", "recall_profile": "governed"},
    )
    assert resp.status_code == 422
