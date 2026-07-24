"""Unit tests for the recall-quality benchmark (ENG-AUDIT-002B).

Tests the summarization logic and data structures without requiring a
live Engram instance. The integration tests (run_single_query) require
a live instance and are tested separately.
"""

from __future__ import annotations

from evals.recall_benchmark import (
    BenchmarkResult,
    BenchmarkSuite,
    CorpusProfile,
    RankingDecomposition,
)


def test_summarize_empty() -> None:
    """Empty results produce an error summary."""
    summary = BenchmarkSuite.summarize([])
    assert summary == {"error": "no results"}


def test_summarize_all_hits() -> None:
    """All top-1 hits produce perfect metrics."""
    results = [
        BenchmarkResult(
            query="test query",
            expected_item_id="abc",
            item_budget=10,
            returned_count=5,
            exact_item_rank=0,
            top_k_hit={"top_1": True, "top_5": True, "top_10": True},
            latency_ms=50.0,
            returned_bytes=500,
            decomposition=None,
        ),
        BenchmarkResult(
            query="test query 2",
            expected_item_id="def",
            item_budget=10,
            returned_count=5,
            exact_item_rank=0,
            top_k_hit={"top_1": True, "top_5": True, "top_10": True},
            latency_ms=60.0,
            returned_bytes=600,
            decomposition=None,
        ),
    ]
    summary = BenchmarkSuite.summarize(results)
    assert summary["top_1_hit_rate"] == 1.0
    assert summary["mrr"] == 1.0
    assert summary["recall_at_5"] == 1.0


def test_summarize_mixed_results() -> None:
    """Mixed hit/miss results produce correct MRR."""
    results = [
        # rank 0 → RR = 1.0
        BenchmarkResult(
            query="q1",
            expected_item_id="a",
            item_budget=10,
            returned_count=5,
            exact_item_rank=0,
            top_k_hit={"top_1": True, "top_5": True, "top_10": True},
            latency_ms=50.0,
            returned_bytes=500,
            decomposition=None,
        ),
        # rank 4 → RR = 0.2
        BenchmarkResult(
            query="q2",
            expected_item_id="b",
            item_budget=10,
            returned_count=5,
            exact_item_rank=4,
            top_k_hit={"top_1": False, "top_5": True, "top_10": True},
            latency_ms=55.0,
            returned_bytes=550,
            decomposition=None,
        ),
        # not found → RR = 0.0
        BenchmarkResult(
            query="q3",
            expected_item_id="c",
            item_budget=10,
            returned_count=5,
            exact_item_rank=None,
            top_k_hit={"top_1": False, "top_5": False, "top_10": False},
            latency_ms=45.0,
            returned_bytes=400,
            decomposition=None,
        ),
    ]
    summary = BenchmarkSuite.summarize(results)
    assert summary["total_measurements"] == 3
    assert summary["top_1_hit_rate"] == round(1 / 3, 4)
    assert summary["top_5_hit_rate"] == round(2 / 3, 4)
    # MRR = (1.0 + 0.2 + 0.0) / 3
    assert summary["mrr"] == round((1.0 + 0.2 + 0.0) / 3, 4)
    assert summary["recall_at_5"] == round(2 / 3, 4)


def test_summarize_per_budget() -> None:
    """Per-budget breakdown aggregates correctly."""
    results = [
        BenchmarkResult(
            query="q1", expected_item_id="a", item_budget=5,
            returned_count=5, exact_item_rank=None,
            top_k_hit={"top_1": False, "top_5": False, "top_10": False},
            latency_ms=30.0, returned_bytes=300, decomposition=None,
        ),
        BenchmarkResult(
            query="q1", expected_item_id="a", item_budget=10,
            returned_count=10, exact_item_rank=5,
            top_k_hit={"top_1": False, "top_5": True, "top_10": True},
            latency_ms=40.0, returned_bytes=500, decomposition=None,
        ),
        BenchmarkResult(
            query="q1", expected_item_id="a", item_budget=20,
            returned_count=13, exact_item_rank=10,
            top_k_hit={"top_1": False, "top_5": False, "top_10": True},
            latency_ms=50.0, returned_bytes=700, decomposition=None,
        ),
    ]
    summary = BenchmarkSuite.summarize(results)
    assert 5 in summary["per_budget"]
    assert 10 in summary["per_budget"]
    assert 20 in summary["per_budget"]
    assert summary["per_budget"][5]["recall_rate"] == 0.0
    assert summary["per_budget"][10]["recall_rate"] == 1.0
    assert summary["per_budget"][20]["recall_rate"] == 1.0


def test_default_query_fixtures() -> None:
    """Default fixtures include the three required certification queries."""
    fixtures = BenchmarkSuite.default_query_fixtures("test-item-id")
    assert len(fixtures) == 3
    queries = [f.query for f in fixtures]
    assert "What color is the sky on February 30th?" in queries
    assert "sky purple February 30th" in queries
    assert "February 30 sky claim" in queries
    for f in fixtures:
        assert f.expected_item_id == "test-item-id"
        assert f.kind == "epistemic_questionable"


def test_ranking_decomposition_selected() -> None:
    """Decomposition correctly captures selection context."""
    d = RankingDecomposition(
        eligible_candidate_count=13,
        raw_similarity_rank=5,
        raw_similarity_score=0.82,
        trust_score=0.37,
        trust_adjustment=-5,  # moved from rank 5 to rank 0
        final_score=0.3034,
        final_rank=0,
        item_budget_cutoff=20,
        context_packing_result="selected",
        passed_budget=True,
    )
    assert d.passed_budget
    assert d.context_packing_result == "selected"


def test_ranking_decomposition_budget_excluded() -> None:
    """Decomposition correctly captures budget exclusion."""
    d = RankingDecomposition(
        eligible_candidate_count=50,
        raw_similarity_rank=None,
        raw_similarity_score=None,
        trust_score=None,
        trust_adjustment=None,
        final_score=None,
        final_rank=None,
        item_budget_cutoff=5,
        context_packing_result="budget_excluded",
        passed_budget=False,
    )
    assert not d.passed_budget
    assert d.context_packing_result == "budget_excluded"


def test_corpus_profiles() -> None:
    """Corpus profile dataclass works correctly."""
    small = CorpusProfile(name="small", description="5 items")
    dogfood = CorpusProfile(name="dogfood", description="current tenant corpus")
    distractor = CorpusProfile(
        name="distractor_heavy",
        description="50 semantically adjacent distractors",
        use_existing_corpus=False,
    )
    assert small.use_existing_corpus
    assert dogfood.use_existing_corpus
    assert not distractor.use_existing_corpus


def test_fixture_e_rank_decomposition_matches_certified() -> None:
    """The certified Fixture E rank (10th of 13) should be representable.

    From the certified audit report:
    - exact_item_rank: 10
    - returned_item_count: 13
    - item_budget: 20
    """
    d = RankingDecomposition(
        eligible_candidate_count=13,
        raw_similarity_rank=8,  # estimated: some items had higher cosine similarity
        raw_similarity_score=0.75,
        trust_score=0.37,
        trust_adjustment=-2,  # trust reranking moved it from 8 to 10
        final_score=0.2775,
        final_rank=10,
        item_budget_cutoff=20,
        context_packing_result="selected",
        passed_budget=True,
    )
    assert d.final_rank == 10
    assert d.passed_budget  # rank 10 < budget 20
    assert d.trust_adjustment == -2  # trust lowered it by 2 positions
