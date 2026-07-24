"""Recall-quality benchmark and ranking decomposition (ENG-AUDIT-002B).

A repeatable test-only diagnostic that decomposes the semantic recall ranking
pipeline to answer: where does an item move from its raw semantic rank to its
final served rank?

This is NOT a production endpoint. It exposes ranking internals for audit and
diagnostic purposes through an owner/test-only path.

The benchmark covers:
- Controlled query-to-item pairs with distractors
- top-1, top-5, top-10, MRR, recall@k metrics
- Budget sensitivity (5, 10, 20)
- Corpus-size effects (small, dogfood, distractor-heavy)
- Latency and returned-bytes measurement

Requires a live PostgreSQL with the v2 schema. Skips when no DB is available.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "RankingDecomposition",
    "BenchmarkResult",
    "BenchmarkSuite",
    "QueryFixture",
    "CorpusProfile",
]


@dataclass(frozen=True)
class RankingDecomposition:
    """Full decomposition of one item's ranking in one query result."""

    eligible_candidate_count: int
    raw_similarity_rank: int | None  # rank by cosine distance only (before trust)
    raw_similarity_score: float | None
    trust_score: float | None
    trust_adjustment: float | None  # difference between raw rank and trust-reranked rank
    final_score: float | None
    final_rank: int | None  # 0-based position in final served list
    item_budget_cutoff: int | None
    context_packing_result: str  # "selected", "budget_excluded", "not_in_candidates"
    passed_budget: bool


@dataclass(frozen=True)
class BenchmarkResult:
    """One query-item-budget measurement."""

    query: str
    expected_item_id: str
    item_budget: int
    returned_count: int
    exact_item_rank: int | None  # None if not in results
    top_k_hit: dict[str, bool]  # {"top_1": False, "top_5": True, "top_10": True}
    latency_ms: float
    returned_bytes: int
    decomposition: RankingDecomposition | None


@dataclass(frozen=True)
class QueryFixture:
    """A controlled query-to-item pair for benchmarking."""

    query: str
    expected_item_id: str
    label: str
    kind: str  # "normal_durable", "preference", "decision", "epistemic_questionable"


@dataclass(frozen=True)
class CorpusProfile:
    """A corpus configuration for benchmarking."""

    name: str
    description: str
    # When True, this corpus uses only items already in the DB (dogfood).
    # When False, uses synthetic distractor items.
    use_existing_corpus: bool = True


class BenchmarkSuite:
    """Orchestrates recall-quality benchmarks.

    Usage:
        suite = BenchmarkSuite(base_url, api_key)
        results = await suite.run_benchmark(queries, budgets=[5, 10, 20])
        report = suite.summarize(results)
    """

    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url
        self.api_key = api_key

    @staticmethod
    def default_query_fixtures(fixture_e_id: str | None = None) -> list[QueryFixture]:
        """Return the standard comparative query set."""
        fixtures = [
            QueryFixture(
                query="What color is the sky on February 30th?",
                expected_item_id=fixture_e_id or "",
                label="exact_semantic_paraphrase",
                kind="epistemic_questionable",
            ),
            QueryFixture(
                query="sky purple February 30th",
                expected_item_id=fixture_e_id or "",
                label="short_keyword_heavy",
                kind="epistemic_questionable",
            ),
            QueryFixture(
                query="February 30 sky claim",
                expected_item_id=fixture_e_id or "",
                label="marker_query",
                kind="epistemic_questionable",
            ),
        ]
        return fixtures

    async def run_single_query(
        self,
        query: str,
        expected_item_id: str,
        item_budget: int,
    ) -> BenchmarkResult:
        """Run one query at one budget and capture full metrics."""
        import time

        import httpx

        headers = {"Authorization": f"Bearer {self.api_key}"}
        start = time.monotonic()
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self.base_url}/v1/recall",
                json={"query": query, "mode": "semantic", "item_budget": item_budget},
                headers=headers,
            )
        latency_ms = (time.monotonic() - start) * 1000

        if resp.status_code != 200:
            return BenchmarkResult(
                query=query,
                expected_item_id=expected_item_id,
                item_budget=item_budget,
                returned_count=0,
                exact_item_rank=None,
                top_k_hit={"top_1": False, "top_5": False, "top_10": False},
                latency_ms=latency_ms,
                returned_bytes=0,
                decomposition=None,
            )

        data = resp.json()
        items = data.get("items") or []
        returned_bytes = data.get("byte_count", 0)
        candidate_count = data.get("candidate_count", 0)

        # Find the expected item's position
        exact_rank = None
        for i, item in enumerate(items):
            if str(item.get("id")) == expected_item_id:
                exact_rank = i
                break

        # Compute top-k hits
        top_k = {
            "top_1": exact_rank is not None and exact_rank < 1,
            "top_5": exact_rank is not None and exact_rank < 5,
            "top_10": exact_rank is not None and exact_rank < 10,
        }

        # Build decomposition when the item is found
        decomposition = None
        if exact_rank is not None:
            item_data = items[exact_rank]
            similarity = float(item_data.get("similarity_score", 0.0))
            trust = float(item_data.get("trust_score", 0.0))
            score = float(item_data.get("score", 0.0))

            # Estimate raw similarity rank: would need the full candidate list
            # for an exact value. The returned items are already trust-ranked,
            # so raw_similarity_rank is an approximation: count items with
            # higher similarity in the returned set.
            raw_rank = sum(
                1
                for other in items
                if float(other.get("similarity_score", 0.0)) > similarity
            )

            decomposition = RankingDecomposition(
                eligible_candidate_count=candidate_count,
                raw_similarity_rank=raw_rank,
                raw_similarity_score=round(similarity, 4),
                trust_score=round(trust, 4),
                trust_adjustment=raw_rank - exact_rank,
                final_score=round(score, 4),
                final_rank=exact_rank,
                item_budget_cutoff=item_budget,
                context_packing_result="selected",
                passed_budget=exact_rank < item_budget,
            )
        else:
            decomposition = RankingDecomposition(
                eligible_candidate_count=candidate_count,
                raw_similarity_rank=None,
                raw_similarity_score=None,
                trust_score=None,
                trust_adjustment=None,
                final_score=None,
                final_rank=None,
                item_budget_cutoff=item_budget,
                context_packing_result="budget_excluded"
                if candidate_count > 0
                else "not_in_candidates",
                passed_budget=False,
            )

        return BenchmarkResult(
            query=query,
            expected_item_id=expected_item_id,
            item_budget=item_budget,
            returned_count=len(items),
            exact_item_rank=exact_rank,
            top_k_hit=top_k,
            latency_ms=round(latency_ms, 1),
            returned_bytes=returned_bytes,
            decomposition=decomposition,
        )

    async def run_benchmark(
        self,
        queries: list[QueryFixture],
        budgets: list[int] | None = None,
    ) -> list[BenchmarkResult]:
        """Run all query-budget combinations."""
        if budgets is None:
            budgets = [5, 10, 20]

        results: list[BenchmarkResult] = []
        for qf in queries:
            for budget in budgets:
                result = await self.run_single_query(qf.query, qf.expected_item_id, budget)
                results.append(result)
        return results

    @staticmethod
    def summarize(results: list[BenchmarkResult]) -> dict[str, Any]:
        """Compute aggregate metrics from benchmark results."""
        if not results:
            return {"error": "no results"}

        total = len(results)
        hits_top1 = sum(1 for r in results if r.top_k_hit["top_1"])
        hits_top5 = sum(1 for r in results if r.top_k_hit["top_5"])
        hits_top10 = sum(1 for r in results if r.top_k_hit["top_10"])

        # MRR: 1/rank for each query (0 if not found)
        reciprocal_ranks = []
        for r in results:
            if r.exact_item_rank is not None:
                reciprocal_ranks.append(1.0 / (r.exact_item_rank + 1))
            else:
                reciprocal_ranks.append(0.0)
        mrr = sum(reciprocal_ranks) / total if total > 0 else 0.0

        # Recall@k
        recall_at_5 = hits_top5 / total if total > 0 else 0.0
        recall_at_10 = hits_top10 / total if total > 0 else 0.0
        hits_top20 = sum(
            1 for r in results
            if r.exact_item_rank is not None and r.exact_item_rank < 20
        )
        recall_at_20 = hits_top20 / total if total > 0 else 0.0

        latencies = [r.latency_ms for r in results]
        avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
        avg_bytes = sum(r.returned_bytes for r in results) / total if total > 0 else 0.0

        # Per-budget breakdown
        per_budget: dict[int, dict[str, Any]] = {}
        for budget in sorted({r.item_budget for r in results}):
            budget_results = [r for r in results if r.item_budget == budget]
            budget_hits = sum(1 for r in budget_results if r.exact_item_rank is not None)
            per_budget[budget] = {
                "total_queries": len(budget_results),
                "items_found": budget_hits,
                "recall_rate": round(budget_hits / len(budget_results), 4)
                if budget_results
                else 0.0,
                "avg_latency_ms": round(
                    sum(r.latency_ms for r in budget_results) / len(budget_results), 1
                )
                if budget_results
                else 0.0,
            }

        return {
            "total_measurements": total,
            "top_1_hit_rate": round(hits_top1 / total, 4),
            "top_5_hit_rate": round(hits_top5 / total, 4),
            "top_10_hit_rate": round(hits_top10 / total, 4),
            "recall_at_5": round(recall_at_5, 4),
            "recall_at_10": round(recall_at_10, 4),
            "recall_at_20": round(recall_at_20, 4),
            "mrr": round(mrr, 4),
            "avg_latency_ms": round(avg_latency, 1),
            "avg_returned_bytes": round(avg_bytes, 0),
            "per_budget": per_budget,
        }
