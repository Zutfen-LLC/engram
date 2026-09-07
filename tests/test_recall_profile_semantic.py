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
        await conn.execute(text("DELETE FROM memory_embeddings"))
        await conn.execute(text("DELETE FROM memory_items"))
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

    # Exploratory candidate: the qualified proposal is admitted and marked as
    # the unknown-evidence state a proposal is.
    exploratory = next(c for c in shadow["candidates"] if c["profile"] == "exploratory")
    expl_by_id = {i["id"]: i for i in exploratory["items"]}
    assert proposed_id in expl_by_id
    assert expl_by_id[proposed_id]["epistemic_state"] == "unknown"
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
    assert served["epistemic_state"] == "unknown"
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
    assert expl_by_id[proposed_id]["epistemic_state"] == "unknown"


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
