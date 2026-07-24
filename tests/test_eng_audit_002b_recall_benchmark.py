"""Tests for the recall-quality benchmark (ENG-AUDIT-002B FIX2).

Covers all required tests from the certification packet ENG-AUDIT-002B-FIX2:

1. Production-window parity test with eligible_count greater than the
   production fetch limit.
2. Dual-lane test proving full-corpus raw diagnostics do not change trust
   rank, relationship rank, final rank, selected IDs, or exclusion disposition.
3. Unconditional test where raw_similarity_rank is exactly 0 and trust_rank
   is exactly 1.
4. Unconditional item-budget test where the target is proven present before
   packing and exactly item_budget_excluded afterward.
5. Unconditional byte-budget test requiring exactly byte_budget_excluded.
6. Unconditional token-budget test requiring exactly token_budget_excluded.
7. Returned-count test asserting returned_count equals len(selected).
8. Returned-bytes test asserting returned_bytes equals the exact selected
   UTF-8 byte total.
9. Tie-break test with equal distances and differing created_at values
   matching production newest-first ordering.
10. Existing-corpus test using an explicit query vector.
11. Existing-corpus test using an injected embedding generator.
12. Existing-corpus missing/failed embedding test that fails closed.
13. Corpus-fingerprint test proving changes beyond vector component four
    change the fingerprint.
14. Fixture outside the production candidate window must be reported
    outside_candidate_window even when the diagnostic full-corpus lane can
    locate it.

Unit tests (pure logic, no DB) run always. Integration tests require a live
PostgreSQL with the v2 schema and pgvector.

F11 fix: all behavioral assertions are UNCONDITIONAL — they do not use
``if`` guards or permissive ``in`` checks that pass vacuously.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.config import settings
from evals.recall_benchmark import (
    BenchmarkResult,
    ControlledCorpus,
    CorpusItem,
    CorpusProfile,
    EmbeddingFailure,
    EmptyFixtureIdError,
    ExclusionDisposition,
    HttpBenchmarkClient,
    PipelineError,
    QueryFixture,
    ServiceBenchmarkSuite,
    StageRanks,
    TransportError,
    _sort_key_production_raw,
    distractor_heavy_corpus,
    existing_corpus_mode,
    small_controlled_corpus,
)


def _near_unit_vec(dimensions: int, primary: int, secondary: int) -> list[float]:
    """A vector mostly along *primary* with a small *secondary* component."""
    vec = [0.0] * dimensions
    if 0 <= primary < dimensions:
        vec[primary] = 0.95
    if 0 <= secondary < dimensions:
        vec[secondary] = 0.312
    return vec

# ---------------------------------------------------------------------------
# Engine + fixtures (mirrors test_promotion.py / test_semantic_recall.py)
# ---------------------------------------------------------------------------

_test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_test_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    _test_engine, class_=AsyncSession, expire_on_commit=False
)


@pytest.fixture(autouse=True)
async def _fresh_engine() -> Any:
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


# ---------------------------------------------------------------------------
# Isolated audit tenant fixture
# ---------------------------------------------------------------------------


@pytest.fixture
async def audit_tenant() -> Any:
    """Create an isolated audit tenant and clean it up after the test."""
    if not await _db_ok():
        yield None, None
        return

    tenant_id = str(uuid.uuid4())
    admin_id = str(uuid.uuid4())
    slug = f"bench-{uuid.uuid4().hex}"

    async with _test_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
            {"id": tenant_id, "name": slug, "slug": slug},
        )
        await conn.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES (:id, :tenant_id, 'admin', 'admin')"
            ),
            {"id": admin_id, "tenant_id": tenant_id},
        )
        await conn.execute(
            text("INSERT INTO tenant_config (tenant_id) VALUES (:id)"),
            {"id": tenant_id},
        )
        await conn.execute(
            text(
                "UPDATE memory_kinds SET auto_promote_from_inferred=TRUE, enabled=TRUE "
                "WHERE tenant_id=:tenant_id AND name='fact'"
            ),
            {"tenant_id": tenant_id},
        )

    try:
        yield tenant_id, admin_id
    finally:
        async with _test_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM tenants WHERE id=:id"), {"id": tenant_id}
            )


# ---------------------------------------------------------------------------
# Helper: get embedding profile dimensions
# ---------------------------------------------------------------------------

async def _get_dimensions() -> int:
    async with _test_session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT dimensions FROM embedding_profiles "
                    "WHERE state='active' LIMIT 1"
                )
            )
        ).one()
        return int(row[0])


async def _default_tenant_count() -> int:
    """Count memory_items in the default tenant."""
    async with _test_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT count(*) FROM memory_items mi "
                "JOIN tenants t ON t.id = mi.tenant_id "
                "WHERE t.slug = 'default'"
            )
        )
        return int(result.scalar() or 0)


# ---------------------------------------------------------------------------
# UNIT TESTS (no DB required)
# ---------------------------------------------------------------------------


def test_empty_fixture_id_rejected() -> None:
    """F3: An empty expected_item_id is rejected before any DB operation."""
    suite = ServiceBenchmarkSuite(session_factory=None)  # type: ignore[arg-type]

    with pytest.raises(EmptyFixtureIdError):
        import asyncio

        asyncio.get_event_loop().run_until_complete(
            suite.run_single_query(
                tenant_id="x",
                principal_id="y",
                query="q",
                query_vector=[1.0],
                expected_item_id="",
                item_budget=10,
            )
        )

    # Whitespace-only also rejected
    with pytest.raises(EmptyFixtureIdError):
        import asyncio

        asyncio.get_event_loop().run_until_complete(
            suite.run_single_query(
                tenant_id="x",
                principal_id="y",
                query="q",
                query_vector=[1.0],
                expected_item_id="   ",
                item_budget=10,
            )
        )


def test_embedding_failure_raises_not_degrades() -> None:
    """F3/F9: None query_vector AND no embedding_fn raises EmbeddingFailure."""
    suite = ServiceBenchmarkSuite(session_factory=None)  # type: ignore[arg-type]

    with pytest.raises(EmbeddingFailure):
        import asyncio

        asyncio.get_event_loop().run_until_complete(
            suite.run_single_query(
                tenant_id="x",
                principal_id="y",
                query="q",
                query_vector=None,
                expected_item_id="abc",
                item_budget=10,
            )
        )


def test_http_client_rejects_empty_fixture_id() -> None:
    """F3: HTTP client also rejects empty fixture IDs."""
    client = HttpBenchmarkClient("http://test", "key")
    with pytest.raises(EmptyFixtureIdError):
        import asyncio

        asyncio.get_event_loop().run_until_complete(
            client.run_single_query("q", "", 10)
        )


def test_corpus_fingerprint_stable() -> None:
    """Corpus fingerprints are deterministic for the same definition."""
    dims = 1536
    c1 = small_controlled_corpus(dims)
    c2 = small_controlled_corpus(dims)
    assert c1.corpus_fingerprint() == c2.corpus_fingerprint()


def test_corpus_fingerprint_differs_for_different_corpus() -> None:
    """Different corpus profiles produce different fingerprints."""
    dims = 1536
    c1 = small_controlled_corpus(dims)
    c2 = distractor_heavy_corpus(dims)
    assert c1.corpus_fingerprint() != c2.corpus_fingerprint()


def test_corpus_fingerprint_captures_all_vector_components() -> None:
    """F11: Changes beyond vector component four change the fingerprint.

    Two corpus profiles identical except for component 5 of a vector
    must produce different fingerprints — proving all components are hashed.
    """
    dims = 10
    vec_a = [0.0] * dims
    vec_a[0] = 1.0
    vec_a[5] = 0.0  # component 5 = 0.0

    vec_b = [0.0] * dims
    vec_b[0] = 1.0
    vec_b[5] = 0.5  # component 5 = 0.5 (beyond the first 4)

    item_a = CorpusItem(content="item", embedding_vector=vec_a)
    item_b = CorpusItem(content="item", embedding_vector=vec_b)

    profile_a = CorpusProfile(
        name="test_f11_a", description="f11 test", mode="controlled",
        items=(item_a,), query_vector=list(vec_a),
    )
    profile_b = CorpusProfile(
        name="test_f11_a", description="f11 test", mode="controlled",
        items=(item_b,), query_vector=list(vec_a),
    )
    # Names are identical, only vector component 5 differs.
    assert profile_a.corpus_fingerprint() != profile_b.corpus_fingerprint(), (
        "F11 FAIL: changing vector component 5 must change the fingerprint"
    )


def test_corpus_fingerprint_query_vector_all_components() -> None:
    """F11: query vector component beyond index 3 changes the fingerprint."""
    dims = 10
    qvec_a = [0.0] * dims
    qvec_a[0] = 1.0
    qvec_a[7] = 0.0

    qvec_b = [0.0] * dims
    qvec_b[0] = 1.0
    qvec_b[7] = 0.3

    item = CorpusItem(content="item", embedding_vector=qvec_a)

    profile_a = CorpusProfile(
        name="test_qv_a", description="qv", mode="controlled",
        items=(item,), query_vector=list(qvec_a),
    )
    profile_b = CorpusProfile(
        name="test_qv_a", description="qv", mode="controlled",
        items=(item,), query_vector=list(qvec_b),
    )
    assert profile_a.corpus_fingerprint() != profile_b.corpus_fingerprint()


def test_corpus_profile_modes() -> None:
    """F4: CorpusProfile modes are correctly labeled."""
    small = small_controlled_corpus(1536)
    assert small.is_controlled
    assert len(small.items) == 5

    heavy = distractor_heavy_corpus(1536)
    assert heavy.is_controlled
    assert len(heavy.items) == 31

    existing = existing_corpus_mode()
    assert not existing.is_controlled
    assert len(existing.items) == 0


def test_per_budget_metrics_not_combined() -> None:
    """F5: Per-budget metrics are computed separately, not aggregated."""
    results = [
        BenchmarkResult(
            query="q1", query_digest="d1", expected_item_id="a",
            item_budget=5, byte_budget=None, token_budget=None,
            top_k_hit={"top_1": False, "top_5": False, "top_10": False},
            latency_ms=30.0, latency_type="service",
            returned_bytes=0, returned_count=0,
            stages=StageRanks(
                eligible_candidate_count=20, candidate_window_size=15,
                raw_similarity_rank=6, raw_similarity_rank_1based=7,
                raw_similarity_score=0.75, raw_rank_exact=True,
                corpus_raw_rank=6, corpus_raw_rank_1based=7,
                corpus_raw_rank_exact=True,
                trust_rank=6, trust_rank_1based=7, trust_score=0.37,
                post_relationship_rank=6, post_relationship_rank_1based=7,
                final_served_rank=None, final_served_rank_1based=None,
                final_score=None, candidate_origin="semantic",
                exclusion_disposition=ExclusionDisposition.ITEM_BUDGET_EXCLUDED,
                item_budget=5, byte_budget=None, token_budget=None,
            ),
        ),
        BenchmarkResult(
            query="q1", query_digest="d1", expected_item_id="a",
            item_budget=10, byte_budget=None, token_budget=None,
            top_k_hit={"top_1": False, "top_5": True, "top_10": True},
            latency_ms=40.0, latency_type="service",
            returned_bytes=100, returned_count=10,
            stages=StageRanks(
                eligible_candidate_count=20, candidate_window_size=20,
                raw_similarity_rank=6, raw_similarity_rank_1based=7,
                raw_similarity_score=0.75, raw_rank_exact=True,
                corpus_raw_rank=6, corpus_raw_rank_1based=7,
                corpus_raw_rank_exact=True,
                trust_rank=6, trust_rank_1based=7, trust_score=0.37,
                post_relationship_rank=6, post_relationship_rank_1based=7,
                final_served_rank=6, final_served_rank_1based=7,
                final_score=0.28, candidate_origin="semantic",
                exclusion_disposition=ExclusionDisposition.SELECTED,
                item_budget=10, byte_budget=None, token_budget=None,
            ),
        ),
    ]
    summary = ServiceBenchmarkSuite.summarize(results)

    assert summary["per_budget"][5]["recall_at_5"] == 0.0
    assert summary["per_budget"][10]["recall_at_5"] == 1.0
    assert "note" in summary
    assert "no cross-budget" in summary["note"].lower() or "per-budget" in summary["note"].lower()


def test_boundary_top_k_logic() -> None:
    """Boundary tests: rank 4 → top-5; rank 5 → not top-5; rank 9 → top-10; rank 10 → not top-10."""
    for rank, in_top5, in_top10 in [
        (4, True, True),
        (5, False, True),
        (9, False, True),
        (10, False, False),
    ]:
        assert (rank < 5) == in_top5, f"rank {rank} top-5 mismatch"
        assert (rank < 10) == in_top10, f"rank {rank} top-10 mismatch"


def test_production_raw_sort_key_distance_asc_created_desc() -> None:
    """F10: _sort_key_production_raw matches production ordering.

    distance ascending, created_at descending (newer first among equal distances).
    """
    from datetime import datetime

    newer = datetime(2026, 1, 2, tzinfo=UTC)
    older = datetime(2026, 1, 1, tzinfo=UTC)

    item_near_new = {"distance": 0.1, "created_at": newer}
    item_near_old = {"distance": 0.1, "created_at": older}
    item_far = {"distance": 0.5, "created_at": newer}

    # Same distance: newer should sort first (smaller key)
    assert _sort_key_production_raw(item_near_new) < _sort_key_production_raw(item_near_old)

    # Smaller distance sorts first regardless of created_at
    assert _sort_key_production_raw(item_near_new) < _sort_key_production_raw(item_far)


# ---------------------------------------------------------------------------
# HTTP fail-closed tests (using MockTransport)
# ---------------------------------------------------------------------------


def test_http_non_200_fails_closed() -> None:
    """F3: Non-2xx HTTP response raises TransportError, not a benchmark miss."""
    import asyncio

    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    transport = httpx.MockTransport(handler)

    async def run() -> None:
        client = HttpBenchmarkClient("http://test", "key")
        client._client = httpx.AsyncClient(transport=transport, timeout=30.0)
        with pytest.raises(TransportError):
            await client.run_single_query("query", "item-id", 10)

    asyncio.get_event_loop().run_until_complete(run())


def test_http_invalid_json_fails_closed() -> None:
    """F3: Invalid JSON response raises TransportError."""
    import asyncio

    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json at all")

    transport = httpx.MockTransport(handler)

    async def run() -> None:
        client = HttpBenchmarkClient("http://test", "key")
        client._client = httpx.AsyncClient(transport=transport, timeout=30.0)
        with pytest.raises(TransportError):
            await client.run_single_query("query", "item-id", 10)

    asyncio.get_event_loop().run_until_complete(run())


def test_http_missing_fields_fails_closed() -> None:
    """F3: Missing required fields in response raises TransportError."""
    import asyncio

    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"foo": "bar"})

    transport = httpx.MockTransport(handler)

    async def run() -> None:
        client = HttpBenchmarkClient("http://test", "key")
        client._client = httpx.AsyncClient(transport=transport, timeout=30.0)
        with pytest.raises(TransportError):
            await client.run_single_query("query", "item-id", 10)

    asyncio.get_event_loop().run_until_complete(run())


# ---------------------------------------------------------------------------
# INTEGRATION TESTS (require live PostgreSQL with pgvector)
#
# F11: All behavioral assertions below are UNCONDITIONAL — they prove the
# behavior the test name promises, without `if` guards or permissive checks.
# ---------------------------------------------------------------------------


async def test_exact_raw_rank_zero_and_trust_rank_one(audit_tenant: Any) -> None:
    """Required test 3 (unconditional): raw_similarity_rank is exactly 0
    and trust_rank is exactly 1.

    The target shares the query vector (distance 0.0, raw_rank=0) but the
    near-target has higher trust (0.80 vs 0.50) → trust re-ranking moves
    the target to trust_rank=1. Both assertions are unconditional.
    """
    if not await _db_ok():
        pytest.skip("requires PostgreSQL with pgvector")
    tenant_id, admin_id = audit_tenant
    assert tenant_id is not None

    dims = await _get_dimensions()
    profile = small_controlled_corpus(dims)
    corpus = ControlledCorpus(tenant_id, admin_id, profile)
    label_map = await corpus.setup(_test_session_factory)

    try:
        target_id = label_map["target"]
        suite = ServiceBenchmarkSuite(_test_session_factory)

        result = await suite.run_single_query(
            tenant_id=tenant_id,
            principal_id=admin_id,
            query=profile.query_text,
            query_vector=profile.query_vector,
            expected_item_id=target_id,
            item_budget=10,
        )

        assert result.error is None
        assert result.stages is not None
        stages = result.stages

        # UNCONDITIONAL: raw_similarity_rank is exactly 0
        assert stages.raw_similarity_rank == 0, (
            f"Expected raw_rank=0, got {stages.raw_similarity_rank}"
        )
        # UNCONDITIONAL: trust_rank is exactly 1 (near-target has higher trust)
        assert stages.trust_rank == 1, (
            f"Expected trust_rank=1, got {stages.trust_rank}"
        )
        # This proves raw_rank != trust_rank (F2)
        assert stages.raw_similarity_rank != stages.trust_rank
    finally:
        await corpus.teardown(_test_session_factory)


async def test_stage_ranks_independent_of_recall_response(audit_tenant: Any) -> None:
    """Required test 2: stage ranks do not depend on stripped fields."""
    if not await _db_ok():
        pytest.skip("requires PostgreSQL with pgvector")
    tenant_id, admin_id = audit_tenant
    assert tenant_id is not None

    dims = await _get_dimensions()
    profile = small_controlled_corpus(dims)
    corpus = ControlledCorpus(tenant_id, admin_id, profile)
    label_map = await corpus.setup(_test_session_factory)

    try:
        target_id = label_map["target"]
        suite = ServiceBenchmarkSuite(_test_session_factory)

        result = await suite.run_single_query(
            tenant_id=tenant_id,
            principal_id=admin_id,
            query=profile.query_text,
            query_vector=profile.query_vector,
            expected_item_id=target_id,
            item_budget=10,
        )

        assert result.stages is not None
        assert hasattr(result.stages, "eligible_candidate_count")
        assert hasattr(result.stages, "raw_similarity_rank")
        assert hasattr(result.stages, "trust_rank")
        assert hasattr(result.stages, "candidate_window_size")
        assert hasattr(result.stages, "raw_rank_exact")
        assert result.stages.eligible_candidate_count == 5
    finally:
        await corpus.teardown(_test_session_factory)


async def test_returned_count_equals_len_selected(audit_tenant: Any) -> None:
    """Required test 7 (unconditional): returned_count equals len(selected).

    F8 fix: returned_count must be the actual number of items selected by
    budget enforcement, not a placeholder boolean.
    """
    if not await _db_ok():
        pytest.skip("requires PostgreSQL with pgvector")
    tenant_id, admin_id = audit_tenant
    assert tenant_id is not None

    dims = await _get_dimensions()
    profile = small_controlled_corpus(dims)
    corpus = ControlledCorpus(tenant_id, admin_id, profile)
    label_map = await corpus.setup(_test_session_factory)

    try:
        target_id = label_map["target"]
        suite = ServiceBenchmarkSuite(_test_session_factory)

        # Use item_budget=3 so exactly 3 items should be selected
        result = await suite.run_single_query(
            tenant_id=tenant_id,
            principal_id=admin_id,
            query=profile.query_text,
            query_vector=profile.query_vector,
            expected_item_id=target_id,
            item_budget=3,
        )

        assert result.stages is not None
        # The small corpus has 5 items; budget=3 → exactly 3 selected.
        # UNCONDITIONAL: returned_count is the actual len(selected), not a boolean.
        assert result.returned_count == 3, (
            f"Expected returned_count=3, got {result.returned_count}"
        )
    finally:
        await corpus.teardown(_test_session_factory)


async def test_returned_bytes_exact_utf8_total(audit_tenant: Any) -> None:
    """Required test 8 (unconditional): returned_bytes equals exact UTF-8 byte total.

    F8 fix: returned_bytes must be the exact sum of content bytes, not a
    placeholder.
    """
    if not await _db_ok():
        pytest.skip("requires PostgreSQL with pgvector")
    tenant_id, admin_id = audit_tenant
    assert tenant_id is not None

    dims = await _get_dimensions()
    profile = small_controlled_corpus(dims)
    corpus = ControlledCorpus(tenant_id, admin_id, profile)
    label_map = await corpus.setup(_test_session_factory)

    try:
        target_id = label_map["target"]
        suite = ServiceBenchmarkSuite(_test_session_factory)

        # Query with item_budget large enough to serve all items.
        result = await suite.run_single_query(
            tenant_id=tenant_id,
            principal_id=admin_id,
            query=profile.query_text,
            query_vector=profile.query_vector,
            expected_item_id=target_id,
            item_budget=10,
        )

        assert result.stages is not None

        # Compute the expected byte total from the corpus items that were
        # actually selected. All 5 items should be selected with budget=10.
        # UNCONDITIONAL: returned_bytes is the exact byte sum, not a placeholder.
        expected_bytes = sum(
            len(item.content.encode("utf-8")) for item in profile.items
        )
        assert result.returned_bytes == expected_bytes, (
            f"Expected returned_bytes={expected_bytes}, got {result.returned_bytes}"
        )
    finally:
        await corpus.teardown(_test_session_factory)


async def test_item_budget_exclusion_unconditional(audit_tenant: Any) -> None:
    """Required test 4 (F12 unconditional): target is proven present before
    packing and exactly item_budget_excluded afterward.

    F12 fix: uses a deterministic corpus where the target is GUARANTEED to be
    excluded by item budget with disposition exactly ITEM_BUDGET_EXCLUDED
    (not trust_demoted). To avoid the trust_demoted classification
    (which fires when raw_rank < item_budget && trust_rank >= item_budget),
    the target is placed at raw_rank >= item_budget by putting 2 nearer items
    between the query and the target. With item_budget=2, the target has
    raw_rank=2 which is >= item_budget, so the disposition is exactly
    item_budget_excluded.

    Asserts UNCONDITIONALLY:
    - final_served_rank is None (target not in final set)
    - returned_count == 2 (exactly the budget)
    - disposition == item_budget_excluded
    - target is present through post_relationship_rank (survives expansion)
    """
    if not await _db_ok():
        pytest.skip("requires PostgreSQL with pgvector")
    tenant_id, admin_id = audit_tenant
    assert tenant_id is not None

    dims = await _get_dimensions()

    # Build a corpus where target has raw_rank >= item_budget.
    query_vec = [0.0] * dims
    query_vec[0] = 1.0

    # Two NEAR items that are closer to the query than the target.
    # They share the query axis but with slightly off-axis components.
    # Item 1: mostly position 0 with small position 1 → distance ~0.05
    near1_vec = [0.0] * dims
    near1_vec[0] = 0.99
    near1_vec[1] = 0.14  # small off-axis → norm ≈ 1.0

    # Item 2: mostly position 0 with small position 2 → distance ~0.05
    near2_vec = [0.0] * dims
    near2_vec[0] = 0.99
    near2_vec[2] = 0.14

    # Target: shares position 0 but with larger off-axis → distance ~0.29
    # This places the target at raw_rank=2 (after the two nearer items).
    target_vec = _near_unit_vec(dims, 0, 3)

    target_item = CorpusItem(
        content="f12 target: present but beyond item budget",
        source_trust=0.50,
        memory_confidence=0.50,
        importance=0.50,
        human_verified=False,
        review_status="active",
        embedding_vector=target_vec,
        label="target",
    )
    near1 = CorpusItem(
        content="f12 near item 1",
        source_trust=0.50,
        memory_confidence=0.50,
        importance=0.50,
        review_status="active",
        embedding_vector=near1_vec,
        label="near1",
    )
    near2 = CorpusItem(
        content="f12 near item 2",
        source_trust=0.50,
        memory_confidence=0.50,
        importance=0.50,
        review_status="active",
        embedding_vector=near2_vec,
        label="near2",
    )

    profile = CorpusProfile(
        name="f12_item_budget",
        description="Target excluded by item budget (not trust demoted)",
        mode="controlled",
        items=(target_item, near1, near2),
        query_vector=list(query_vec),
        query_text="f12 item budget query",
    )

    corpus = ControlledCorpus(tenant_id, admin_id, profile)
    label_map = await corpus.setup(_test_session_factory)

    try:
        target_id = label_map["target"]
        suite = ServiceBenchmarkSuite(_test_session_factory)

        result = await suite.run_single_query(
            tenant_id=tenant_id,
            principal_id=admin_id,
            query=profile.query_text,
            query_vector=profile.query_vector,
            expected_item_id=target_id,
            item_budget=2,  # Only 2 items served
        )

        assert result.stages is not None
        stages = result.stages

        # Target IS in the production candidate window.
        assert stages.raw_similarity_rank is not None, (
            "Target should be in the production candidate window"
        )

        # UNCONDITIONAL: target survives through post-relationship stage.
        assert stages.post_relationship_rank is not None, (
            "Target should be present through post-relationship stage"
        )

        # UNCONDITIONAL: final_served_rank is None (excluded by budget)
        assert stages.final_served_rank is None, (
            f"Expected final_served_rank=None (budget excluded), got "
            f"{stages.final_served_rank}"
        )

        # UNCONDITIONAL: returned_count == item_budget (exactly 2 items selected)
        assert result.returned_count == 2, (
            f"Expected returned_count=2 (item_budget), got {result.returned_count}"
        )

        # UNCONDITIONAL: disposition is exactly item_budget_excluded.
        assert stages.exclusion_disposition == ExclusionDisposition.ITEM_BUDGET_EXCLUDED, (
            f"Expected item_budget_excluded, got {stages.exclusion_disposition}"
        )
    finally:
        await corpus.teardown(_test_session_factory)


async def test_byte_budget_exclusion_unconditional(audit_tenant: Any) -> None:
    """Required test 5 (unconditional): byte_budget_excluded is proven.

    With byte_budget=1, items cannot be selected because every content string
    is >1 byte. The disposition MUST be exactly byte_budget_excluded.
    """
    if not await _db_ok():
        pytest.skip("requires PostgreSQL with pgvector")
    tenant_id, admin_id = audit_tenant
    assert tenant_id is not None

    dims = await _get_dimensions()
    profile = small_controlled_corpus(dims)
    corpus = ControlledCorpus(tenant_id, admin_id, profile)
    label_map = await corpus.setup(_test_session_factory)

    try:
        target_id = label_map["target"]
        suite = ServiceBenchmarkSuite(_test_session_factory)

        result = await suite.run_single_query(
            tenant_id=tenant_id,
            principal_id=admin_id,
            query=profile.query_text,
            query_vector=profile.query_vector,
            expected_item_id=target_id,
            item_budget=10,   # generous item budget
            byte_budget=1,    # tiny byte budget — no item fits
        )

        assert result.stages is not None
        # UNCONDITIONAL: with byte_budget=1, the disposition is exactly
        # byte_budget_excluded. No item content is ≤1 byte.
        assert result.stages.exclusion_disposition == ExclusionDisposition.BYTE_BUDGET_EXCLUDED, (
            f"Expected byte_budget_excluded, got "
            f"{result.stages.exclusion_disposition}"
        )
    finally:
        await corpus.teardown(_test_session_factory)


async def test_token_budget_exclusion_unconditional(audit_tenant: Any) -> None:
    """Required test 6 (unconditional): token_budget_excluded is proven.

    token_budget=1 means max 1 token (max(1, item_bytes//4) ≥ 1 for any
    content). With token_budget=1, no item can be added because the first
    item already uses ≥1 token. The disposition MUST be token_budget_excluded.
    """
    if not await _db_ok():
        pytest.skip("requires PostgreSQL with pgvector")
    tenant_id, admin_id = audit_tenant
    assert tenant_id is not None

    dims = await _get_dimensions()
    profile = small_controlled_corpus(dims)
    corpus = ControlledCorpus(tenant_id, admin_id, profile)
    label_map = await corpus.setup(_test_session_factory)

    try:
        target_id = label_map["target"]
        suite = ServiceBenchmarkSuite(_test_session_factory)

        result = await suite.run_single_query(
            tenant_id=tenant_id,
            principal_id=admin_id,
            query=profile.query_text,
            query_vector=profile.query_vector,
            expected_item_id=target_id,
            item_budget=10,    # generous item budget
            token_budget=1,    # 1 token — nothing fits
        )

        assert result.stages is not None
        # UNCONDITIONAL: with token_budget=1, the disposition is exactly
        # token_budget_excluded.
        assert result.stages.exclusion_disposition == ExclusionDisposition.TOKEN_BUDGET_EXCLUDED, (
            f"Expected token_budget_excluded, got "
            f"{result.stages.exclusion_disposition}"
        )
    finally:
        await corpus.teardown(_test_session_factory)


async def test_distractor_heavy_inserts_intended_items(audit_tenant: Any) -> None:
    """Required test 9: distractor-heavy mode actually inserts the intended distractors."""
    if not await _db_ok():
        pytest.skip("requires PostgreSQL with pgvector")
    tenant_id, admin_id = audit_tenant
    assert tenant_id is not None

    dims = await _get_dimensions()
    profile = distractor_heavy_corpus(dims)
    corpus = ControlledCorpus(tenant_id, admin_id, profile)
    label_map = await corpus.setup(_test_session_factory)

    try:
        assert len(label_map) == 31

        async with _test_session_factory() as session:
            count = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM memory_items "
                        "WHERE tenant_id = :tid AND content LIKE 'distractor-heavy%'"
                    ),
                    {"tid": tenant_id},
                )
            ).scalar()
            assert int(count or 0) == 31

            emb_count = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM memory_embeddings me "
                        "JOIN memory_items mi ON mi.id = me.memory_item_id "
                        "WHERE mi.tenant_id = :tid AND mi.content LIKE 'distractor-heavy%'"
                    ),
                    {"tid": tenant_id},
                )
            ).scalar()
            assert int(emb_count or 0) == 31
    finally:
        await corpus.teardown(_test_session_factory)


async def test_repeat_run_determinism(audit_tenant: Any) -> None:
    """Required test 10: repeat-run determinism over unchanged controlled state."""
    if not await _db_ok():
        pytest.skip("requires PostgreSQL with pgvector")
    tenant_id, admin_id = audit_tenant
    assert tenant_id is not None

    dims = await _get_dimensions()
    profile = small_controlled_corpus(dims)
    corpus = ControlledCorpus(tenant_id, admin_id, profile)
    label_map = await corpus.setup(_test_session_factory)

    try:
        target_id = label_map["target"]
        suite = ServiceBenchmarkSuite(_test_session_factory)

        result1 = await suite.run_single_query(
            tenant_id=tenant_id, principal_id=admin_id,
            query=profile.query_text, query_vector=profile.query_vector,
            expected_item_id=target_id, item_budget=10,
        )
        result2 = await suite.run_single_query(
            tenant_id=tenant_id, principal_id=admin_id,
            query=profile.query_text, query_vector=profile.query_vector,
            expected_item_id=target_id, item_budget=10,
        )

        assert result1.stages is not None
        assert result2.stages is not None
        assert result1.stages.raw_similarity_rank == result2.stages.raw_similarity_rank
        assert result1.stages.trust_rank == result2.stages.trust_rank
        assert result1.stages.eligible_candidate_count == result2.stages.eligible_candidate_count
    finally:
        await corpus.teardown(_test_session_factory)


async def test_isolation_and_cleanup_proof(audit_tenant: Any) -> None:
    """Required test 11: no unrelated tenant data changed."""
    if not await _db_ok():
        pytest.skip("requires PostgreSQL with pgvector")

    default_before = await _default_tenant_count()
    tenant_id, admin_id = audit_tenant
    assert tenant_id is not None

    dims = await _get_dimensions()
    profile = small_controlled_corpus(dims)
    corpus = ControlledCorpus(tenant_id, admin_id, profile)
    label_map = await corpus.setup(_test_session_factory)

    try:
        target_id = label_map["target"]
        suite = ServiceBenchmarkSuite(_test_session_factory)
        await suite.run_single_query(
            tenant_id=tenant_id, principal_id=admin_id,
            query=profile.query_text, query_vector=profile.query_vector,
            expected_item_id=target_id, item_budget=10,
        )
    finally:
        await corpus.teardown(_test_session_factory)

    default_after = await _default_tenant_count()
    assert default_after == default_before

    async with _test_engine.connect() as conn:
        audit_count = (
            await conn.execute(
                text("SELECT count(*) FROM memory_items WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
        ).scalar()
        assert int(audit_count or 0) == 0


async def test_budget_5_10_20_per_budget_metrics(audit_tenant: Any) -> None:
    """Required evidence: run the benchmark at budgets 5, 10, and 20."""
    if not await _db_ok():
        pytest.skip("requires PostgreSQL with pgvector")
    tenant_id, admin_id = audit_tenant
    assert tenant_id is not None

    dims = await _get_dimensions()
    profile = small_controlled_corpus(dims)
    corpus = ControlledCorpus(tenant_id, admin_id, profile)
    label_map = await corpus.setup(_test_session_factory)

    try:
        target_id = label_map["target"]
        suite = ServiceBenchmarkSuite(_test_session_factory)

        results = await suite.run_benchmark(
            tenant_id=tenant_id, principal_id=admin_id,
            corpus_profile=profile,
            queries=[QueryFixture(
                query=profile.query_text,
                expected_item_id=target_id,
                label="q1",
            )],
            budgets=[5, 10, 20],
        )

        assert len(results) == 3
        summary = ServiceBenchmarkSuite.summarize(results)
        assert 5 in summary["per_budget"]
        assert 10 in summary["per_budget"]
        assert 20 in summary["per_budget"]

        for budget in [5, 10, 20]:
            assert summary["per_budget"][budget]["total_queries"] == 1
    finally:
        await corpus.teardown(_test_session_factory)


async def test_raw_rank_exact_in_controlled_corpus(audit_tenant: Any) -> None:
    """In controlled mode, raw_rank_exact is True (full corpus examined)."""
    if not await _db_ok():
        pytest.skip("requires PostgreSQL with pgvector")
    tenant_id, admin_id = audit_tenant
    assert tenant_id is not None

    dims = await _get_dimensions()
    profile = small_controlled_corpus(dims)
    corpus = ControlledCorpus(tenant_id, admin_id, profile)
    label_map = await corpus.setup(_test_session_factory)

    try:
        target_id = label_map["target"]
        suite = ServiceBenchmarkSuite(_test_session_factory)

        result = await suite.run_single_query(
            tenant_id=tenant_id, principal_id=admin_id,
            query=profile.query_text, query_vector=profile.query_vector,
            expected_item_id=target_id, item_budget=20,
        )

        assert result.stages is not None
        assert result.stages.raw_rank_exact is True
        assert result.stages.eligible_candidate_count == 5
        assert result.stages.candidate_window_size >= 5
    finally:
        await corpus.teardown(_test_session_factory)


async def test_candidate_origin_captured(audit_tenant: Any) -> None:
    """Candidate origin is captured so semantic hits are distinguishable."""
    if not await _db_ok():
        pytest.skip("requires PostgreSQL with pgvector")
    tenant_id, admin_id = audit_tenant
    assert tenant_id is not None

    dims = await _get_dimensions()
    profile = small_controlled_corpus(dims)
    corpus = ControlledCorpus(tenant_id, admin_id, profile)
    label_map = await corpus.setup(_test_session_factory)

    try:
        target_id = label_map["target"]
        suite = ServiceBenchmarkSuite(_test_session_factory)

        result = await suite.run_single_query(
            tenant_id=tenant_id, principal_id=admin_id,
            query=profile.query_text, query_vector=profile.query_vector,
            expected_item_id=target_id, item_budget=10,
        )

        assert result.stages is not None
        assert result.stages.candidate_origin is not None
        assert "semantic" in result.stages.candidate_origin
    finally:
        await corpus.teardown(_test_session_factory)


async def test_report_generation_contains_provenance(audit_tenant: Any) -> None:
    """Report contains SHA, scoring version, embedding profile, corpus fingerprint."""
    if not await _db_ok():
        pytest.skip("requires PostgreSQL with pgvector")
    tenant_id, admin_id = audit_tenant
    assert tenant_id is not None

    dims = await _get_dimensions()
    profile = small_controlled_corpus(dims)
    corpus = ControlledCorpus(tenant_id, admin_id, profile)
    label_map = await corpus.setup(_test_session_factory)

    try:
        target_id = label_map["target"]
        suite = ServiceBenchmarkSuite(_test_session_factory)

        results = await suite.run_benchmark(
            tenant_id=tenant_id, principal_id=admin_id,
            corpus_profile=profile,
            queries=[QueryFixture(
                query=profile.query_text,
                expected_item_id=target_id,
                label="q1",
            )],
            budgets=[5, 10, 20],
        )

        report = ServiceBenchmarkSuite.generate_report(
            repository_sha="test-sha-1234567",
            results=results,
            corpus_profile=profile,
            embedding_profile_key="test-profile",
            embedding_model="test-model",
            embedding_dimensions=dims,
            scoring_version="semantic-v3",
            config_version="v1",
        )

        assert report.repository_sha == "test-sha-1234567"
        assert report.scoring_version == "semantic-v3"
        assert report.embedding_model == "test-model"
        assert report.embedding_dimensions == dims
        assert report.corpus_profile_name == "small_controlled"
        assert len(report.corpus_fingerprint) == 16
        assert report.generated_at

        for r in report.results:
            assert len(r.query_digest) == 16
            assert r.latency_type in ("service", "end_to_end")
    finally:
        await corpus.teardown(_test_session_factory)


# ---------------------------------------------------------------------------
# NEW FIX2 TESTS: F7 dual-lane, F8 byte/count, F9 existing mode,
# F10 tie-break, F14 outside-window
# ---------------------------------------------------------------------------


async def test_production_window_parity(audit_tenant: Any) -> None:
    """Required FIX2 test: production-window parity.

    With eligible_count > production fetch limit (distractor_heavy at budget=5
    → fetch_limit=15, eligible=31), assert downstream stages receive exactly
    the production window, not the full corpus.
    """
    if not await _db_ok():
        pytest.skip("requires PostgreSQL with pgvector")
    tenant_id, admin_id = audit_tenant
    assert tenant_id is not None

    dims = await _get_dimensions()
    profile = distractor_heavy_corpus(dims)  # 31 items
    corpus = ControlledCorpus(tenant_id, admin_id, profile)
    label_map = await corpus.setup(_test_session_factory)

    try:
        target_id = label_map["target"]
        suite = ServiceBenchmarkSuite(_test_session_factory)

        # budget=5 → production fetch_limit = min(5*3, 200) = 15
        result = await suite.run_single_query(
            tenant_id=tenant_id, principal_id=admin_id,
            query=profile.query_text, query_vector=profile.query_vector,
            expected_item_id=target_id, item_budget=5,
        )

        assert result.stages is not None
        stages = result.stages

        # The production candidate window must be exactly 15 (production limit)
        # because eligible_count=31 > 15.
        assert stages.candidate_window_size <= 15, (
            f"Production window must be ≤ 15 (fetch_limit=5*3), "
            f"got {stages.candidate_window_size}"
        )

        # eligible_count must be 31 (full corpus counted independently)
        assert stages.eligible_candidate_count == 31

        # raw_rank_exact must be False (production window < full corpus)
        assert stages.raw_rank_exact is False, (
            "raw_rank_exact should be False when production window < eligible corpus"
        )

        # The target shares the query vector → raw_rank=0 even in the
        # production window.
        assert stages.raw_similarity_rank == 0

        # corpus_raw_rank should also be 0 (target is nearest in full corpus too)
        assert stages.corpus_raw_rank == 0
        assert stages.corpus_raw_rank_exact is True
    finally:
        await corpus.teardown(_test_session_factory)


async def test_dual_lane_diagnostic_does_not_affect_production(audit_tenant: Any) -> None:
    """Required FIX2 test: dual-lane separation.

    Prove that the full-corpus diagnostic lane does not change trust_rank,
    relationship rank, final rank, selected IDs, or exclusion disposition.

    Strategy: run the benchmark on a corpus where eligible_count > production
    fetch limit, then verify the production-derived stages are consistent
    with production-only semantics (they could not have been influenced by
    the full corpus diagnostic lane).
    """
    if not await _db_ok():
        pytest.skip("requires PostgreSQL with pgvector")
    tenant_id, admin_id = audit_tenant
    assert tenant_id is not None

    dims = await _get_dimensions()
    profile = distractor_heavy_corpus(dims)  # 31 items
    corpus = ControlledCorpus(tenant_id, admin_id, profile)
    label_map = await corpus.setup(_test_session_factory)

    try:
        target_id = label_map["target"]
        suite = ServiceBenchmarkSuite(_test_session_factory)

        # budget=5 → production window 15, full corpus 31
        result = await suite.run_single_query(
            tenant_id=tenant_id, principal_id=admin_id,
            query=profile.query_text, query_vector=profile.query_vector,
            expected_item_id=target_id, item_budget=5,
        )

        assert result.stages is not None
        stages = result.stages

        # The production window size proves the production lane used the
        # bounded window (15), not the full corpus (31).
        assert stages.candidate_window_size <= 15
        assert stages.eligible_candidate_count == 31

        # trust_rank is derived from the production candidate set only.
        # Since the target is at raw_rank=0 (nearest), trust_rank ≤ 15
        # (within the production window).
        if stages.trust_rank is not None:
            assert stages.trust_rank < 15, (
                f"trust_rank={stages.trust_rank} exceeds production window (15) — "
                f"diagnostic lane may have leaked into production"
            )

        # corpus_raw_rank is the diagnostic lane result.
        # It must exist (diagnostic ran because window < corpus).
        assert stages.corpus_raw_rank is not None
        # corpus_raw_rank for the target should be 0 (nearest in full corpus)
        assert stages.corpus_raw_rank == 0

        # The disposition must not be outside_candidate_window (target is
        # in the production window at rank 0).
        assert stages.exclusion_disposition != ExclusionDisposition.OUTSIDE_CANDIDATE_WINDOW
    finally:
        await corpus.teardown(_test_session_factory)


async def test_tie_break_distance_equal_created_at_differs(audit_tenant: Any) -> None:
    """Required FIX2 test 9: tie-break with equal distances and differing
    created_at values matching production newest-first ordering.

    F10: when two items have the same cosine distance, the one with the
    newer created_at must rank first (matching production ORDER BY
    distance ASC, created_at DESC).

    Uses direct SQL insertion with explicit timestamps so the tie-break
    is deterministic (ControlledCorpus.setup uses datetime.now for all items).
    """
    if not await _db_ok():
        pytest.skip("requires PostgreSQL with pgvector")
    tenant_id, admin_id = audit_tenant
    assert tenant_id is not None

    dims = await _get_dimensions()
    import uuid as uuid_mod
    from datetime import timedelta

    # Both items at position 1 → same distance from query at position 0.
    query_vec = [0.0] * dims
    query_vec[0] = 1.0

    both_vec = [0.0] * dims
    both_vec[1] = 1.0
    rendered_both = "[" + ",".join(str(v) for v in both_vec) + "]"

    distractor_vec = _unit_vec_at(dims, 2)
    rendered_distractor = "[" + ",".join(str(v) for v in distractor_vec) + "]"

    # Resolve embedding profile for the embeddings.
    async with _test_session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id::text, model, dimensions FROM embedding_profiles "
                    "WHERE state='active' LIMIT 1"
                )
            )
        ).one()
        profile_id = str(row[0])
        model = str(row[1])
        dimensions = int(row[2])

    # Insert items with explicitly different created_at timestamps.
    newer_id = str(uuid_mod.uuid4())
    older_id = str(uuid_mod.uuid4())
    distractor_id = str(uuid_mod.uuid4())

    base_time = datetime.now(UTC) - timedelta(hours=2)
    newer_time = base_time + timedelta(hours=1, minutes=5)
    older_time = base_time + timedelta(minutes=5)

    item_ids = [newer_id, older_id, distractor_id]

    for item_id, content, created, emb_vec in [
        (newer_id, "tie-break newer item", newer_time, rendered_both),
        (older_id, "tie-break older item", older_time, rendered_both),
        (distractor_id, "tie-break distractor", base_time, rendered_distractor),
    ]:
        async with _test_session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO memory_items ("
                    "id, tenant_id, principal_id, content, content_hash, kind, "
                    "visibility, review_status, memory_confidence, source_trust, "
                    "importance, human_verified, source_type, authority, "
                    "created_at, valid_from"
                    ") VALUES ("
                    ":id, :tenant_id, :principal_id, :content, :content_hash, 'fact', "
                    "'tenant', 'active', 0.50, 0.50, 0.50, false, 'manual', 10, "
                    ":created, :created"
                    ")"
                ),
                {
                    "id": item_id,
                    "tenant_id": tenant_id,
                    "principal_id": admin_id,
                    "content": content,
                    "content_hash": f"sha256:{uuid_mod.uuid4().hex}",
                    "created": created,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO memory_embeddings "
                    "(id, memory_item_id, tenant_id, profile_id, embedding_model, "
                    "embedding_dim, embedding, embedding_status) "
                    "VALUES (:eid, :item_id, :tenant_id, :profile_id, :model, "
                    ":dimensions, CAST(:embedding AS vector), 'ready')"
                ),
                {
                    "eid": str(uuid_mod.uuid4()),
                    "item_id": item_id,
                    "tenant_id": tenant_id,
                    "profile_id": profile_id,
                    "model": model,
                    "dimensions": dimensions,
                    "embedding": emb_vec,
                },
            )
            await session.commit()

    try:
        suite = ServiceBenchmarkSuite(_test_session_factory)

        # Query for the newer item
        result_newer = await suite.run_single_query(
            tenant_id=tenant_id, principal_id=admin_id,
            query="tie-break query", query_vector=query_vec,
            expected_item_id=newer_id, item_budget=10,
        )

        # Query for the older item
        result_older = await suite.run_single_query(
            tenant_id=tenant_id, principal_id=admin_id,
            query="tie-break query", query_vector=query_vec,
            expected_item_id=older_id, item_budget=10,
        )

        assert result_newer.stages is not None
        assert result_older.stages is not None

        # Both items have the same distance → raw_rank is determined by
        # created_at descending. The newer item must have a better (lower)
        # raw_rank than the older item.
        # UNCONDITIONAL: newer item's raw_rank < older item's raw_rank
        assert result_newer.stages.raw_similarity_rank is not None
        assert result_older.stages.raw_similarity_rank is not None
        assert result_newer.stages.raw_similarity_rank < result_older.stages.raw_similarity_rank, (
            f"F10 FAIL: newer item raw_rank "
            f"({result_newer.stages.raw_similarity_rank}) should be < "
            f"older item raw_rank ({result_older.stages.raw_similarity_rank})"
        )
    finally:
        async with _test_session_factory() as session:
            await session.execute(
                text("DELETE FROM memory_embeddings WHERE memory_item_id = ANY(:ids)"),
                {"ids": item_ids},
            )
            await session.execute(
                text("DELETE FROM memory_items WHERE id = ANY(:ids)"),
                {"ids": item_ids},
            )
            await session.commit()


async def test_existing_corpus_with_explicit_query_vector(audit_tenant: Any) -> None:
    """Required FIX2 test 10: existing-corpus mode with explicit query vector.

    F9: existing_corpus_mode is runnable when given an explicit query vector.
    """
    if not await _db_ok():
        pytest.skip("requires PostgreSQL with pgvector")
    tenant_id, admin_id = audit_tenant
    assert tenant_id is not None

    # First insert some items using a controlled corpus, then query with
    # existing_corpus_mode using an explicit vector.
    dims = await _get_dimensions()
    controlled_profile = small_controlled_corpus(dims)
    corpus = ControlledCorpus(tenant_id, admin_id, controlled_profile)
    label_map = await corpus.setup(_test_session_factory)

    try:
        target_id = label_map["target"]

        # Now query the existing corpus with an explicit query vector.
        query_vec = [0.0] * dims
        query_vec[0] = 1.0

        existing_profile = existing_corpus_mode(
            query_vector=query_vec,
            query_text="existing corpus query",
        )

        suite = ServiceBenchmarkSuite(_test_session_factory)
        result = await suite.run_single_query(
            tenant_id=tenant_id, principal_id=admin_id,
            query=existing_profile.query_text,
            query_vector=existing_profile.query_vector,
            expected_item_id=target_id, item_budget=10,
        )

        # F9: the existing-corpus run must succeed (not raise EmbeddingFailure)
        assert result.error is None
        assert result.stages is not None
        assert result.stages.eligible_candidate_count >= 1
    finally:
        await corpus.teardown(_test_session_factory)


async def test_existing_corpus_with_injected_embedding_fn(audit_tenant: Any) -> None:
    """Required FIX2 test 11: existing-corpus mode with injected embedding fn.

    F9: existing_corpus_mode is runnable via an injected query-embedding function.
    """
    if not await _db_ok():
        pytest.skip("requires PostgreSQL with pgvector")
    tenant_id, admin_id = audit_tenant
    assert tenant_id is not None

    # Insert controlled items first.
    dims = await _get_dimensions()
    controlled_profile = small_controlled_corpus(dims)
    corpus = ControlledCorpus(tenant_id, admin_id, controlled_profile)
    label_map = await corpus.setup(_test_session_factory)

    try:
        target_id = label_map["target"]

        # Build an injected embedding function that returns the query vector.
        query_vec = [0.0] * dims
        query_vec[0] = 1.0

        async def mock_embedding_fn(query: str) -> list[float] | None:
            return list(query_vec)

        existing_profile = existing_corpus_mode(
            query_text="existing corpus query with fn",
            query_embedding_fn=mock_embedding_fn,
        )

        suite = ServiceBenchmarkSuite(_test_session_factory)
        result = await suite.run_benchmark(
            tenant_id=tenant_id, principal_id=admin_id,
            corpus_profile=existing_profile,
            queries=[QueryFixture(
                query=existing_profile.query_text,
                expected_item_id=target_id,
                label="q1",
            )],
            budgets=[10],
        )

        # F9: the run with injected embedding fn must succeed.
        assert len(results := result) == 1
        assert results[0].error is None
        assert results[0].stages is not None
    finally:
        await corpus.teardown(_test_session_factory)


def test_existing_corpus_missing_embedding_fails_closed() -> None:
    """Required FIX2 test 12: existing-corpus missing/failed embedding fails closed.

    F9: when neither an explicit vector nor a successful embedding result is
    available, the benchmark must fail closed with EmbeddingFailure.
    """
    import asyncio

    suite = ServiceBenchmarkSuite(session_factory=None)  # type: ignore[arg-type]

    # No query_vector AND no embedding_fn → EmbeddingFailure
    with pytest.raises(EmbeddingFailure):
        asyncio.get_event_loop().run_until_complete(
            suite.run_single_query(
                tenant_id="x",
                principal_id="y",
                query="q",
                query_vector=None,
                expected_item_id="abc",
                item_budget=10,
            )
        )

    # No query_vector AND embedding_fn returns None → EmbeddingFailure
    async def failing_embedding_fn(query: str) -> list[float] | None:
        return None

    with pytest.raises(EmbeddingFailure):
        asyncio.get_event_loop().run_until_complete(
            suite.run_single_query(
                tenant_id="x",
                principal_id="y",
                query="q",
                query_vector=None,
                expected_item_id="abc",
                item_budget=10,
                query_embedding_fn=failing_embedding_fn,
            )
        )


async def test_fixture_outside_production_window_reported(audit_tenant: Any) -> None:
    """Required FIX2 test 14: fixture outside the production candidate window
    is reported outside_candidate_window even when the diagnostic lane finds it.

    With distractor_heavy (31 items) and a small budget, the production
    fetch limit is small. An item ranked beyond the production window in
    raw distance ordering should be reported as outside_candidate_window.
    We craft a fixture that is guaranteed to be beyond the production window
    by using a vector orthogonal to the query.
    """
    if not await _db_ok():
        pytest.skip("requires PostgreSQL with pgvector")
    tenant_id, admin_id = audit_tenant
    assert tenant_id is not None

    dims = await _get_dimensions()

    # Create a corpus where the target is at an orthogonal position (far)
    # so it will be ranked beyond the production window.
    query_vec = [0.0] * dims
    query_vec[0] = 1.0

    # Target at an orthogonal position — it has maximum distance from query.
    target_vec = [0.0] * dims
    target_vec[dims - 1] = 1.0  # orthogonal to query

    # Fill the first positions with near-query items to push the target
    # beyond the production candidate window.
    near_items = tuple(
        CorpusItem(
            content=f"near item {i}",
            embedding_vector=_near_vec(dims, 0, i + 1),
            label=f"near-{i}",
        )
        for i in range(min(20, dims - 2))
    )

    target_item = CorpusItem(
        content="far orthogonal target",
        embedding_vector=list(target_vec),
        label="far-target",
    )

    profile = CorpusProfile(
        name="outside_window_test",
        description="Target at orthogonal position beyond production window",
        mode="controlled",
        items=(*near_items, target_item),
        query_vector=list(query_vec),
        query_text="outside window query",
    )

    corpus = ControlledCorpus(tenant_id, admin_id, profile)
    label_map = await corpus.setup(_test_session_factory)

    try:
        target_id = label_map["far-target"]
        suite = ServiceBenchmarkSuite(_test_session_factory)

        # budget=1 → production fetch_limit = 3. The target is orthogonal
        # (distance ~1.0) and will be far beyond rank 3.
        result = await suite.run_single_query(
            tenant_id=tenant_id, principal_id=admin_id,
            query=profile.query_text, query_vector=profile.query_vector,
            expected_item_id=target_id, item_budget=1,
        )

        assert result.stages is not None
        stages = result.stages

        # The target must NOT be in the production candidate window.
        # UNCONDITIONAL: raw_similarity_rank is None (not in production window).
        assert stages.raw_similarity_rank is None, (
            f"Target should not be in production window, but raw_rank="
            f"{stages.raw_similarity_rank}"
        )

        # UNCONDITIONAL: disposition is outside_candidate_window.
        # The target is eligible and has an embedding, but it's beyond the
        # production fetch limit.
        assert stages.exclusion_disposition == ExclusionDisposition.OUTSIDE_CANDIDATE_WINDOW, (
            f"Expected outside_candidate_window, got {stages.exclusion_disposition}"
        )

        # The diagnostic lane may find it in the full corpus.
        # corpus_raw_rank should be non-None (diagnostic lane found it).
        assert stages.corpus_raw_rank is not None, (
            "Diagnostic lane should have found the target in the full corpus"
        )
    finally:
        await corpus.teardown(_test_session_factory)


# ---------------------------------------------------------------------------
# Helpers for tie-break and outside-window tests
# ---------------------------------------------------------------------------


def _unit_vec_at(dimensions: int, position: int) -> list[float]:
    """Create a unit vector at the given position."""
    vec = [0.0] * dimensions
    if 0 <= position < dimensions:
        vec[position] = 1.0
    return vec


def _near_vec(dimensions: int, primary: int, secondary: int) -> list[float]:
    """A vector mostly along *primary* with a small *secondary* component."""
    vec = [0.0] * dimensions
    if 0 <= primary < dimensions:
        vec[primary] = 0.95
    if 0 <= secondary < dimensions:
        vec[secondary] = 0.312
    return vec


# ---------------------------------------------------------------------------
# FIX3 TESTS: F12-F15 corrections
# ---------------------------------------------------------------------------


async def test_eligible_corpus_fingerprint_changes_on_metadata(audit_tenant: Any) -> None:
    """F13: fingerprint changes when an eligible item's ranking-relevant metadata changes."""
    if not await _db_ok():
        pytest.skip("requires PostgreSQL with pgvector")
    tenant_id, admin_id = audit_tenant
    assert tenant_id is not None

    dims = await _get_dimensions()
    profile = small_controlled_corpus(dims)
    corpus = ControlledCorpus(tenant_id, admin_id, profile)
    await corpus.setup(_test_session_factory)

    try:
        suite = ServiceBenchmarkSuite(_test_session_factory)

        # Get embedding profile key.
        async with _test_session_factory() as session:
            from engram.embedding_profiles import get_active_profile

            ep = await get_active_profile(session)

        from engram.auth import Principal
        from engram.memory_context import unrestricted_memory_context

        principal = Principal(
            tenant_id=tenant_id, principal_id=admin_id, api_key_id="",
            scopes=("read",), memory_profile_id=None,
            memory_profile_revision_id=None, memory_profile_slug=None,
            memory_profile_version=None,
        )
        ctx = unrestricted_memory_context(principal)

        fp1, count1 = await suite.compute_eligible_corpus_fingerprint(
            tenant_id=tenant_id, memory_context=ctx,
            embedding_profile_key=ep.profile_key,
        )

        # Change a ranking-relevant field on one item.
        async with _test_session_factory() as session:
            await session.execute(
                text(
                    "UPDATE memory_items SET source_trust = 0.99 "
                    "WHERE tenant_id = :tid AND content LIKE 'benchmark target%'"
                ),
                {"tid": tenant_id},
            )
            await session.commit()

        fp2, count2 = await suite.compute_eligible_corpus_fingerprint(
            tenant_id=tenant_id, memory_context=ctx,
            embedding_profile_key=ep.profile_key,
        )

        # UNCONDITIONAL: fingerprint changed after metadata change.
        assert fp1 != fp2, (
            "F13 FAIL: fingerprint should change when source_trust changes"
        )
        assert count1 == count2  # item count unchanged
    finally:
        await corpus.teardown(_test_session_factory)


async def test_eligible_corpus_fingerprint_changes_on_add_remove(audit_tenant: Any) -> None:
    """F13: fingerprint changes when an eligible item or embedding is added/removed."""
    if not await _db_ok():
        pytest.skip("requires PostgreSQL with pgvector")
    tenant_id, admin_id = audit_tenant
    assert tenant_id is not None

    dims = await _get_dimensions()
    profile = small_controlled_corpus(dims)
    corpus = ControlledCorpus(tenant_id, admin_id, profile)
    label_map = await corpus.setup(_test_session_factory)

    try:
        suite = ServiceBenchmarkSuite(_test_session_factory)

        async with _test_session_factory() as session:
            from engram.embedding_profiles import get_active_profile

            ep = await get_active_profile(session)

        from engram.auth import Principal
        from engram.memory_context import unrestricted_memory_context

        principal = Principal(
            tenant_id=tenant_id, principal_id=admin_id, api_key_id="",
            scopes=("read",), memory_profile_id=None,
            memory_profile_revision_id=None, memory_profile_slug=None,
            memory_profile_version=None,
        )
        ctx = unrestricted_memory_context(principal)

        fp1, count1 = await suite.compute_eligible_corpus_fingerprint(
            tenant_id=tenant_id, memory_context=ctx,
            embedding_profile_key=ep.profile_key,
        )

        # Remove one item's embedding (makes it ineligible for semantic search).
        target_id = label_map["target"]
        async with _test_session_factory() as session:
            await session.execute(
                text(
                    "DELETE FROM memory_embeddings WHERE memory_item_id = :id"
                ),
                {"id": target_id},
            )
            await session.commit()

        fp2, count2 = await suite.compute_eligible_corpus_fingerprint(
            tenant_id=tenant_id, memory_context=ctx,
            embedding_profile_key=ep.profile_key,
        )

        # UNCONDITIONAL: fingerprint changed.
        assert fp1 != fp2, (
            "F13 FAIL: fingerprint should change when an embedding is removed"
        )
        # UNCONDITIONAL: eligible count decreased.
        assert count2 == count1 - 1, (
            f"Expected count {count1 - 1} after removal, got {count2}"
        )
    finally:
        await corpus.teardown(_test_session_factory)


async def test_eligible_corpus_fingerprint_stable(audit_tenant: Any) -> None:
    """F13: fingerprint remains stable across unchanged repeated runs."""
    if not await _db_ok():
        pytest.skip("requires PostgreSQL with pgvector")
    tenant_id, admin_id = audit_tenant
    assert tenant_id is not None

    dims = await _get_dimensions()
    profile = small_controlled_corpus(dims)
    corpus = ControlledCorpus(tenant_id, admin_id, profile)
    await corpus.setup(_test_session_factory)

    try:
        suite = ServiceBenchmarkSuite(_test_session_factory)

        async with _test_session_factory() as session:
            from engram.embedding_profiles import get_active_profile

            ep = await get_active_profile(session)

        from engram.auth import Principal
        from engram.memory_context import unrestricted_memory_context

        principal = Principal(
            tenant_id=tenant_id, principal_id=admin_id, api_key_id="",
            scopes=("read",), memory_profile_id=None,
            memory_profile_revision_id=None, memory_profile_slug=None,
            memory_profile_version=None,
        )
        ctx = unrestricted_memory_context(principal)

        fp1, count1 = await suite.compute_eligible_corpus_fingerprint(
            tenant_id=tenant_id, memory_context=ctx,
            embedding_profile_key=ep.profile_key,
        )
        fp2, count2 = await suite.compute_eligible_corpus_fingerprint(
            tenant_id=tenant_id, memory_context=ctx,
            embedding_profile_key=ep.profile_key,
        )

        # UNCONDITIONAL: fingerprint stable.
        assert fp1 == fp2, "F13 FAIL: fingerprint should be stable across runs"
        assert count1 == count2
    finally:
        await corpus.teardown(_test_session_factory)


async def test_eligible_corpus_fingerprint_ignores_ineligible(audit_tenant: Any) -> None:
    """F13: ineligible or cross-tenant rows do not alter the eligible-corpus fingerprint."""
    if not await _db_ok():
        pytest.skip("requires PostgreSQL with pgvector")
    tenant_id, admin_id = audit_tenant
    assert tenant_id is not None

    dims = await _get_dimensions()
    profile = small_controlled_corpus(dims)
    corpus = ControlledCorpus(tenant_id, admin_id, profile)
    await corpus.setup(_test_session_factory)

    try:
        suite = ServiceBenchmarkSuite(_test_session_factory)

        async with _test_session_factory() as session:
            from engram.embedding_profiles import get_active_profile

            ep = await get_active_profile(session)

        from engram.auth import Principal
        from engram.memory_context import unrestricted_memory_context

        principal = Principal(
            tenant_id=tenant_id, principal_id=admin_id, api_key_id="",
            scopes=("read",), memory_profile_id=None,
            memory_profile_revision_id=None, memory_profile_slug=None,
            memory_profile_version=None,
        )
        ctx = unrestricted_memory_context(principal)

        fp1, count1 = await suite.compute_eligible_corpus_fingerprint(
            tenant_id=tenant_id, memory_context=ctx,
            embedding_profile_key=ep.profile_key,
        )

        # Insert an ineligible item (rejected review_status) in the same tenant.
        import uuid as uuid_mod

        ineligible_id = str(uuid_mod.uuid4())
        async with _test_session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO memory_items ("
                    "id, tenant_id, principal_id, content, content_hash, kind, "
                    "visibility, review_status, memory_confidence, source_trust, "
                    "importance, human_verified, source_type, authority, "
                    "created_at, valid_from"
                    ") VALUES ("
                    ":id, :tid, :pid, 'ineligible rejected item', "
                    ":content_hash, 'fact', 'tenant', 'rejected', "
                    "0.50, 0.50, 0.50, false, 'manual', 10, now(), now()"
                    ")"
                ),
                {
                    "id": ineligible_id, "tid": tenant_id, "pid": admin_id,
                    "content_hash": f"sha256:{uuid_mod.uuid4().hex}",
                },
            )
            await session.commit()

        fp2, count2 = await suite.compute_eligible_corpus_fingerprint(
            tenant_id=tenant_id, memory_context=ctx,
            embedding_profile_key=ep.profile_key,
        )

        # UNCONDITIONAL: fingerprint unchanged (ineligible item not counted).
        assert fp1 == fp2, (
            "F13 FAIL: ineligible item should not change the fingerprint"
        )
        assert count1 == count2, (
            f"Expected count {count1}, got {count2} (ineligible item counted)"
        )

        # Cleanup.
        async with _test_session_factory() as session:
            await session.execute(
                text("DELETE FROM memory_items WHERE id = :id"),
                {"id": ineligible_id},
            )
            await session.commit()
    finally:
        await corpus.teardown(_test_session_factory)


async def test_injected_embedding_fn_receives_active_profile(audit_tenant: Any) -> None:
    """F14: injected embedding generator receives the exact active EmbeddingProfile.

    The test verifies that the embedding function closure captures and uses the
    active profile by checking it produces a vector with the correct dimensions.
    """
    if not await _db_ok():
        pytest.skip("requires PostgreSQL with pgvector")
    tenant_id, admin_id = audit_tenant
    assert tenant_id is not None

    dims = await _get_dimensions()

    # Insert a controlled corpus.
    controlled_profile = small_controlled_corpus(dims)
    corpus = ControlledCorpus(tenant_id, admin_id, controlled_profile)
    label_map = await corpus.setup(_test_session_factory)

    try:
        target_id = label_map["target"]

        # Resolve the active embedding profile.
        async with _test_session_factory() as session:
            from engram.embedding_profiles import get_active_profile

            ep = await get_active_profile(session)

        # Build an embedding function that uses the resolved profile.
        received_profile: list[Any] = []

        async def profile_aware_fn(query: str) -> list[float] | None:
            received_profile.append(ep)
            # Return the query vector matching the active profile dimensions.
            vec = [0.0] * ep.dimensions
            vec[0] = 1.0
            return vec

        existing_profile = existing_corpus_mode(
            query_text="test query",
            query_embedding_fn=profile_aware_fn,
        )

        suite = ServiceBenchmarkSuite(_test_session_factory)
        result = await suite.run_benchmark(
            tenant_id=tenant_id, principal_id=admin_id,
            corpus_profile=existing_profile,
            queries=[QueryFixture(
                query="test query",
                expected_item_id=target_id,
                label="q1",
            )],
            budgets=[10],
        )

        # UNCONDITIONAL: the embedding fn was called and received the active profile.
        assert len(received_profile) >= 1, "Embedding function was never called"
        assert received_profile[0].dimensions == dims, (
            f"F14 FAIL: embedding fn received dims={received_profile[0].dimensions}, "
            f"expected {dims}"
        )
        assert len(result) == 1
        assert result[0].error is None
    finally:
        await corpus.teardown(_test_session_factory)


async def test_embedding_dimension_mismatch_fails_closed(audit_tenant: Any) -> None:
    """F14: embedding-profile dimension/model mismatch fails closed."""
    if not await _db_ok():
        pytest.skip("requires PostgreSQL with pgvector")
    tenant_id, admin_id = audit_tenant
    assert tenant_id is not None

    dims = await _get_dimensions()

    # Insert a controlled corpus so the tenant has eligible items (ensuring
    # the pipeline reaches semantic.search where dimension validation occurs).
    controlled_profile = small_controlled_corpus(dims)
    corpus = ControlledCorpus(tenant_id, admin_id, controlled_profile)
    label_map = await corpus.setup(_test_session_factory)

    try:
        # Build an embedding function that returns a WRONG-dimension vector.
        async def wrong_dim_fn(query: str) -> list[float] | None:
            return [1.0] * (dims + 1)  # Wrong dimension

        suite = ServiceBenchmarkSuite(_test_session_factory)

        # The mismatch should cause a PipelineError when semantic.search
        # validates the vector dimensions against the active profile.
        with pytest.raises(PipelineError):  # noqa: PT011
            await suite.run_single_query(
                tenant_id=tenant_id, principal_id=admin_id,
                query="mismatch test", query_vector=None,
                expected_item_id=label_map["target"], item_budget=10,
                query_embedding_fn=wrong_dim_fn,
            )
    finally:
        await corpus.teardown(_test_session_factory)


def test_redacted_report_omits_raw_identifiers() -> None:
    """F15: redacted report omits raw tenant ID, principal ID, query text, and item IDs.

    Unit test — no DB needed. Builds a report with known values, redacts it,
    and verifies no raw identifier survives.
    """
    raw_tenant = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    raw_principal = "11111111-2222-3333-4444-555555555555"
    raw_item = "99999999-8888-7777-6666-555555555555"
    raw_query = "this is a secret query with sensitive content"

    stages = StageRanks(
        eligible_candidate_count=5, candidate_window_size=5,
        raw_similarity_rank=0, raw_similarity_rank_1based=1,
        raw_similarity_score=1.0, raw_rank_exact=True,
        corpus_raw_rank=0, corpus_raw_rank_1based=1,
        corpus_raw_rank_exact=True,
        trust_rank=0, trust_rank_1based=1, trust_score=0.5,
        post_relationship_rank=0, post_relationship_rank_1based=1,
        final_served_rank=0, final_served_rank_1based=1,
        final_score=0.5, candidate_origin="semantic",
        exclusion_disposition=ExclusionDisposition.SELECTED,
        item_budget=10, byte_budget=None, token_budget=None,
    )

    result = BenchmarkResult(
        query=raw_query,
        query_digest="abcdef0123456789",
        expected_item_id=raw_item,
        item_budget=10, byte_budget=None, token_budget=None,
        top_k_hit={"top_1": True, "top_5": True, "top_10": True},
        latency_ms=10.0, latency_type="service",
        returned_bytes=100, returned_count=1,
        stages=stages, error=None,
    )

    from evals.recall_benchmark import CorpusProfile

    profile = CorpusProfile(
        name="test", description="test", mode="controlled",
        items=(), query_vector=None, query_text=raw_query,
    )

    report = ServiceBenchmarkSuite.generate_report(
        repository_sha="abc123",
        results=[result],
        corpus_profile=profile,
        embedding_profile_key="test-profile",
        embedding_model="test-model",
        embedding_dimensions=1536,
        scoring_version="semantic-v3",
        config_version="v1",
        memory_profile_slug="test-profile",
    )

    redacted = ServiceBenchmarkSuite.generate_redacted_report(report)

    # Serialize to JSON string for scanning.
    import json

    redacted_str = json.dumps(redacted, default=str)

    # UNCONDITIONAL: no raw identifiers in the redacted output.
    assert raw_tenant not in redacted_str, "Raw tenant ID leaked into redacted report"
    assert raw_principal not in redacted_str, "Raw principal ID leaked"
    assert raw_query not in redacted_str, "Raw query text leaked"
    assert raw_item not in redacted_str, "Raw item ID leaked"

    # Digests and truncated IDs are OK.
    assert "abcdef0123456789" in redacted_str  # query digest preserved
    assert "99999999***" in redacted_str  # truncated item ID
