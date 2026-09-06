"""Integration tests for recall admission profiles (issue #160 / ENG-RECALL-003).

These tests require a live PostgreSQL with the v2 schema (migrations/) and
pgvector. They skip automatically when no DB is reachable, mirroring
tests/test_semantic_recall.py. Embeddings are deterministic fakes so CI never
depends on OpenAI.

The regressions pinned here (issue #160 correction pass):

* the rollout boundary — before accepted #162 certification, ordinary
  ``POST /v1/recall`` serves the legacy packet only: requesting (or
  defaulting to) governed/exploratory can never change the authoritative
  served packet;
* the shadow comparison surface evaluates governed/exploratory packets
  read-only (no recall_logs row, no exposure counters, serving unchanged)
  and is gated by reviewer capability AND tenant policy;
* corpus eligibility is applied in SQL before the bounded HNSW window, so
  ineligible disputed rows cannot starve eligible active memories;
* durable admission assessments captured earlier still bind recall when
  ``admission_assessment_capture_enabled`` is disabled (rollback invariant);
* the legacy profile's behavior is byte-for-byte unchanged.
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

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
    bumps no exposure counters, and changes nothing about serving."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    active_id, proposed_id = await _seed_active_and_proposed(client)
    served = await _recall(client)
    served_ids = {item["id"] for item in served["items"]}
    logs_before = await _recall_log_count()
    counts_before = await _recall_counts([active_id, proposed_id])

    shadow = await _shadow_compare(client, profiles=["governed", "exploratory"])

    # The shadow response names the authority and the boundary.
    assert shadow["authoritative_profile"] == "legacy"
    assert shadow["certified_serving_profiles"] == ["legacy"]
    assert shadow["legacy"]["profile"] == "legacy"
    assert shadow["legacy"]["item_count"] == served["item_count"]
    assert {i["id"] for i in shadow["legacy"]["items"]} == served_ids

    # Governed candidate: the highly similar proposal is excluded.
    governed = next(c for c in shadow["candidates"] if c["profile"] == "governed")
    governed_ids = {i["id"] for i in governed["items"]}
    assert active_id in governed_ids
    assert proposed_id not in governed_ids
    assert governed["comparison"]["only_in_legacy"] == [proposed_id]

    # Exploratory candidate: the proposal is admitted and marked unknown.
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


async def test_governed_candidate_excludes_proposal_with_signal_fields(
    client, monkeypatch
):
    """The governed candidate packet carries the separated signal model and
    no blended trust_score."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    active_id, proposed_id = await _seed_active_and_proposed(client)

    shadow = await _shadow_compare(client, profiles=["governed"])
    governed = shadow["candidates"][0]
    assert governed["scoring_version"] == "semantic-signals-v1"
    assert governed["signals_version"] == "recall-signals-v1"
    # The proposal never even entered the pre-LIMIT eligible window
    # (governed corpus eligibility is active + disputed stay kinds).
    assert governed["omitted_by_admission"] == {}

    served = governed["items"][0]
    assert "trust_score" not in served
    assert served["relevance_score"] > 0
    assert 0.0 <= served["utility_score"] <= 1.0
    assert served["epistemic_state"] == "insufficient_evidence"
    assert served["admission"]["profile"] == "governed"
    assert served["admission"]["decision"] == "admit"
    from engram.recall_signals import compute_signal_rank_score

    assert served["score"] == compute_signal_rank_score(
        similarity=served["relevance_score"], utility=served["utility_score"]
    )


async def test_exploratory_candidate_budget_capped(client, monkeypatch):
    """Exploratory packets stay under their tighter budget caps."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    for i in range(25):
        await _remember(client, f"semantic target numbered {i:02d}")

    shadow = await _shadow_compare(
        client, profiles=["governed", "exploratory"], item_budget=50
    )
    by_profile = {c["profile"]: c for c in shadow["candidates"]}
    assert by_profile["exploratory"]["item_count"] == 20
    assert by_profile["governed"]["item_count"] == 25


async def test_governed_candidate_withholds_item_with_stale_assessment(
    client, monkeypatch
):
    """A stale durable assessment withholds in the governed candidate and is
    merely marked in the exploratory one."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    active_id, proposed_id = await _seed_active_and_proposed(client)

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
    assert expl["item_count"] == 2
    expl_by_id = {item["id"]: item for item in expl["items"]}
    assert "admission_assessment_stale" in expl_by_id[active_id]["warning_codes"]
    assert expl_by_id[proposed_id]["epistemic_state"] == "unknown"


async def test_governed_candidate_ordering_is_deterministic(client, monkeypatch):
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    await _remember(client, "semantic target one", importance=0.9)
    await _remember(client, "semantic target two", importance=0.1)
    await _remember(client, "semantic target three", importance=0.5)

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

    _principal_override(app, scopes=("review",))
    body = await _shadow_compare(client, profiles=["exploratory"])
    exploratory = next(c for c in body["candidates"] if c["profile"] == "exploratory")
    assert proposed_id in {i["id"] for i in exploratory["items"]}


# ---- pre-LIMIT corpus eligibility (blocker regression) ----


async def test_ineligible_disputed_rows_cannot_consume_the_bounded_window(
    client, monkeypatch
):
    """Requirement 10: enough ineligible disputed non-stay rows to fill the
    entire former HNSW candidate window sit CLOSER to the query than the
    eligible active rows. The eligible rows must still be retrieved —
    eligibility is applied in SQL before the bounded LIMIT, not by
    overfetching or post-hoc filtering."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    await _enable_tenant_shadow_policy()

    exact = [1.0] + [0.0] * 1535
    near = [math.cos(math.radians(10))] + [math.sin(math.radians(10))] + [0.0] * 1534
    assert abs(sum(v * v for v in near) - 1.0) < 1e-9

    async def fake_embedding(
        text_value: str, *_args: object, **_kwargs: object
    ) -> list[float] | None:
        if text_value.startswith("ineligible disputed"):
            return exact  # closest possible to the query
        if text_value.startswith("eligible active"):
            return near  # measurably farther than every disputed row
        return _DISTRACTOR_VEC

    import engram.embeddings as embeddings_mod
    from engram import recall as recall_mod

    monkeypatch.setattr(recall_mod, "generate_embedding", fake_embedding)
    monkeypatch.setattr(memory_routes, "generate_embedding", fake_embedding)
    monkeypatch.setattr(embeddings_mod, "generate_embedding", fake_embedding)

    disputed_ids = [
        (await _remember(client, f"ineligible disputed row {i:02d}"))["id"] for i in range(8)
    ]
    active_ids = [
        (await _remember(client, f"eligible active row {i:02d}"))["id"] for i in range(3)
    ]

    # Make the close rows disputed items of a non-stay kind.
    async with _test_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE memory_items SET review_status = 'disputed' "
                "WHERE id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": disputed_ids},
        )
    from engram.memory_kinds import get_disputed_stay_kind_names

    async with _test_session_factory() as session:
        tenant_id = str(await session.scalar(text("SELECT id FROM tenants WHERE slug = 'default'")))
        stay_kinds = await get_disputed_stay_kind_names(session, tenant_id)
        kinds = (
            
                await session.execute(
                    text(
                        "SELECT DISTINCT kind FROM memory_items "
                        "WHERE id = ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": disputed_ids},
                )
            
        ).scalars().all()
    assert all(kind not in stay_kinds for kind in kinds), kinds

    # item_budget=2 -> fetch_limit = 2*3 = 6 < 8 disputed rows: under the
    # pre-fix behavior the whole window was ineligible and governed starved.
    shadow = await _shadow_compare(client, profiles=["governed"], item_budget=2)
    governed = shadow["candidates"][0]
    governed_ids = {i["id"] for i in governed["items"]}
    assert governed["item_count"] == 2
    assert governed_ids.issubset(set(active_ids))
    assert not governed_ids & set(disputed_ids)
    # They were excluded by the SQL predicate, not gate-withheld.
    assert governed["omitted_by_admission"] == {}

    # Legacy serving sees the same eligible actives (its window is
    # active+proposed, so it also never serves the disputed rows).
    legacy_ids = {i["id"] for i in shadow["legacy"]["items"]}
    assert legacy_ids.issubset(set(active_ids))


# ---- capture-disabled assessment resolution (rollback invariant) ----


async def test_capture_disabled_preserves_stale_assessment_enforcement(
    client, monkeypatch
):
    """Requirement 9 (#159 rollback invariant): an assessment captured while
    capture was enabled stays visible to the recall resolver after
    ``admission_assessment_capture_enabled=false`` — disabling capture stops
    new capture, it does not erase persisted projection state."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    # 1. Capture a current admitted assessment for an item while capture is
    #    enabled, through the real promotion pipeline.
    settings.admission_assessment_capture_enabled = True
    item = await _remember(client, "semantic target assessed", source_type="extraction")
    assert item["review_status"] == "proposed"
    async with _test_engine.begin() as conn:
        # Past the 72h auto-promote min age and above the confidence
        # threshold so the evaluation admits and promotes the item.
        await conn.execute(
            text(
                "UPDATE memory_items SET memory_confidence = 0.9, "
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
    assert result.promoted == 1
    async with _test_session_factory() as session:
        status = await session.scalar(
            text("SELECT review_status FROM memory_items WHERE id = :id"), {"id": item["id"]}
        )
        assert status == "active"
        outcome = await session.scalar(
            text(
                "SELECT a.outcome FROM admission_assessments a "
                "JOIN admission_assessment_current c ON c.assessment_id = a.id "
                "WHERE c.memory_item_id = :id"
            ),
            {"id": item["id"]},
        )
        assert outcome == "admitted"

    # 2. Make the assessment materially stale (input-state digest change).
    async with _test_engine.begin() as conn:
        await conn.execute(
            text("UPDATE memory_items SET memory_confidence = 0.85 WHERE id = :id"),
            {"id": item["id"]},
        )

    # 3. Roll back: disable capture (the #159 documented rollback).
    settings.admission_assessment_capture_enabled = False

    # 4/5. The candidate recall resolver still sees the stale assessment and
    # its withhold still applies.
    shadow = await _shadow_compare(client, profiles=["governed", "exploratory"])
    by_profile = {c["profile"]: c for c in shadow["candidates"]}
    assert by_profile["governed"]["item_count"] == 0
    assert (
        by_profile["governed"]["omitted_by_admission"].get("admission_assessment_stale")
        == 1
    )
    expl_by_id = {i["id"]: i for i in by_profile["exploratory"]["items"]}
    assert item["id"] in expl_by_id
    assert "admission_assessment_stale" in expl_by_id[item["id"]]["warning_codes"]


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
