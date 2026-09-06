"""Integration tests for recall admission profiles (issue #160 / ENG-RECALL-003).

These tests require a live PostgreSQL with the v2 schema (migrations/) and
pgvector. They skip automatically when no DB is reachable, mirroring
tests/test_semantic_recall.py. Embeddings are deterministic fakes so CI never
depends on OpenAI.

The core regressions pinned here (issue #160 §Evaluation):

* governed mode excludes a highly similar proposal with unknown evidence;
* exploratory mode admits it but marks it machine-readably as unknown;
* the default (legacy) behavior is unchanged, including ``trust_score``;
* a stale admission assessment withholds (governed) or marks (exploratory);
* recall logs record the effective profile and ranking version.
"""

from __future__ import annotations

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
        await conn.execute(text("DELETE FROM memory_embeddings"))
        await conn.execute(text("DELETE FROM memory_items"))


@pytest.fixture(autouse=True)
def _reset_embedding_provider():
    original_provider = settings.embedding_provider
    original_conflict = settings.conflict_check_on_write
    settings.conflict_check_on_write = False
    yield
    settings.embedding_provider = original_provider
    settings.conflict_check_on_write = original_conflict


_TARGET_VEC = [1.0] + [0.0] * 1535
_DISTRACTOR_VEC = [0.0, 1.0] + [0.0] * 1534

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


# ---- governed profile ----


async def test_governed_excludes_highly_similar_proposal(client, monkeypatch):
    """Issue eval requirement 3: a highly similar, important proposal with
    unknown evidence is excluded from governed mode."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)

    active_id, proposed_id = await _seed_active_and_proposed(client)

    body = await _recall(client, recall_profile="governed")
    served_ids = {item["id"] for item in body["items"]}
    assert active_id in served_ids
    assert proposed_id not in served_ids
    assert body["recall_profile"] == "governed"
    assert body["scoring_version"] == "semantic-signals-v1"
    assert body["signals_version"] == "recall-signals-v1"
    # The proposal never even entered the candidate window (governed corpus
    # eligibility is active+disputed), so nothing was gate-withheld here and
    # no withheld content is retained anywhere in the response. The
    # rule-based "proposed_not_admitted" gate remains defense in depth.
    assert body["omitted_by_admission"] == {}

    served = body["items"][0]
    assert "trust_score" not in served
    assert "relevance_score" in served and served["relevance_score"] > 0
    assert "utility_score" in served and 0.0 <= served["utility_score"] <= 1.0
    assert served["epistemic_state"] == "insufficient_evidence"
    assert isinstance(served["admission"]["reason_codes"], list)
    assert served["admission"]["profile"] == "governed"
    assert served["admission"]["decision"] == "admit"
    assert isinstance(served["warning_codes"], list)
    # Rank is reproducible from published inputs.
    from engram.recall_signals import compute_signal_rank_score

    assert served["score"] == compute_signal_rank_score(
        similarity=served["relevance_score"], utility=served["utility_score"]
    )


async def test_governed_withholds_item_with_stale_assessment(client, monkeypatch):
    """Issue eval requirement 4: an active item under a stale policy
    assessment is withheld in governed mode, not silently served."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)

    active_id, proposed_id = await _seed_active_and_proposed(client)

    from engram import recall_signals as signals_mod

    async def fake_bindings(session, *, tenant_id, items):  # type: ignore[no-untyped-def]
        # Every candidate resolves as a stale admitted assessment.
        return {
            item.id: signals_mod.AdmissionAssessmentBinding(
                assessment_id=str(item.id),  # opaque fixture id is fine
                status="stale",
                outcome="admitted",
            )
            for item in items
        }

    monkeypatch.setattr(signals_mod, "load_admission_bindings", fake_bindings)

    body = await _recall(client, recall_profile="governed")
    assert body["item_count"] == 0
    assert body["omitted_by_admission"].get("admission_assessment_stale") == 1

    # Exploratory marks instead of withholding — and still admits the
    # proposal as unknown evidence alongside the marked active item.
    expl = await _recall(client, recall_profile="exploratory")
    assert expl["item_count"] == 2
    expl_by_id = {item["id"]: item for item in expl["items"]}
    assert "admission_assessment_stale" in expl_by_id[active_id]["warning_codes"]
    assert expl_by_id[proposed_id]["epistemic_state"] == "unknown"


# ---- exploratory profile ----


async def test_exploratory_marks_proposal_as_unknown_evidence(client, monkeypatch):
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)

    _active_id, proposed_id = await _seed_active_and_proposed(client)

    body = await _recall(client, recall_profile="exploratory")
    by_id = {item["id"]: item for item in body["items"]}
    assert proposed_id in by_id

    proposal = by_id[proposed_id]
    assert proposal["review_status"] == "proposed"
    assert proposal["epistemic_state"] == "unknown"
    assert "unreviewed" in proposal["warning_codes"]
    assert "evidence_unknown" in proposal["warning_codes"]
    assert "unreviewed" in proposal["warnings"]
    assert proposal["admission"]["profile"] == "exploratory"
    assert "exploratory_proposal" in proposal["admission"]["reason_codes"]
    assert "trust_score" not in proposal


async def test_exploratory_item_budget_capped(client, monkeypatch):
    """Exploratory packets are bounded below the caller's requested budget."""
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)

    for i in range(25):
        await _remember(client, f"semantic target numbered {i:02d}")

    body = await _recall(client, recall_profile="exploratory", item_budget=50)
    assert body["item_count"] == 20

    # Governed has no such cap: the same request serves all 25.
    gov = await _recall(client, recall_profile="governed", item_budget=50)
    assert gov["item_count"] == 25


# ---- legacy compatibility ----


async def test_legacy_default_behavior_unchanged(client, monkeypatch):
    """Default profile keeps the pre-#160 contract byte-for-byte: proposed
    items included, trust_score present, no signal fields."""
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


# ---- audit ----


async def test_recall_log_records_effective_profile(client, monkeypatch):
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)

    await _remember(client, "semantic target")
    governed = await _recall(client, recall_profile="governed")
    legacy = await _recall(client)
    startup_resp = await client.post("/v1/recall", json={"mode": "startup"})
    assert startup_resp.status_code == 200
    startup = startup_resp.json()

    async with _test_session_factory() as session:
        by_profile = {}
        log_ids = (
            governed["recall_log_id"],
            legacy["recall_log_id"],
            startup["recall_log_id"],
        )
        for log_id in log_ids:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT recall_profile, scoring_version FROM recall_logs "
                            "WHERE id = :rid"
                        ),
                        {"rid": log_id},
                    )
                )
                .mappings()
                .one()
            )
            by_profile[row["recall_profile"]] = row
    assert set(by_profile) == {"governed", "legacy", "startup"}
    assert by_profile["governed"]["scoring_version"] == "semantic-signals-v1"
    assert by_profile["legacy"]["scoring_version"] == "semantic-v3"
    assert by_profile["startup"]["scoring_version"] == "v1"


# ---- determinism ----


async def test_governed_ordering_is_deterministic(client, monkeypatch):
    await _skip_without_db()
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)

    await _remember(client, "semantic target one", importance=0.9)
    await _remember(client, "semantic target two", importance=0.1)
    await _remember(client, "semantic target three", importance=0.5)

    first = await _recall(client, recall_profile="governed")
    second = await _recall(client, recall_profile="governed")
    ids_first = [item["id"] for item in first["items"]]
    ids_second = [item["id"] for item in second["items"]]
    assert ids_first == ids_second
    # Highest importance orders first at (near-)equal similarity: all three
    # share the query vector, so utility breaks the tie.
    assert first["items"][0]["content"] == "semantic target one"
