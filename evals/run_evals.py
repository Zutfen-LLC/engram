"""Engram evaluation harness: classification accuracy + recall precision@K/MRR.

This is a standalone script that measures two things:

1. Classification accuracy: runs each golden-set sample through Engram's
   rule-based classifier and (optionally) the LLM classifier, then reports
   per-kind accuracy, confusion patterns, and overall accuracy.

2. Recall relevance: seeds a corpus of memories into a test instance,
   runs golden-set queries, and reports precision@K, MRR, and recall@K.

Usage against a live instance:
    python evals/run_evals.py --base-url http://engram01:8000 --api-key eng_...

Usage with rule-only classification (no live instance needed for the
classification eval — but recall eval requires a live instance):

    python evals/run_evals.py --rules-only

The classification golden set version and recall golden set version are
embedded in the JSON files under evals/golden/. Results are tagged with
the version so baselines can be compared across releases.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

GOLDEN_DIR = Path(__file__).parent / "golden"


@dataclass
class ClassificationResult:
    sample_id: str
    content_preview: str
    expected_kind: str
    predicted_kind: str
    correct: bool
    confidence: float
    reason: str


@dataclass
class ClassificationReport:
    version: int
    total: int
    correct: int
    accuracy: float
    per_kind: dict[str, dict[str, int]] = field(default_factory=dict)
    confusion: list[dict[str, str]] = field(default_factory=list)
    results: list[ClassificationResult] = field(default_factory=list)
    elapsed_seconds: float = 0.0


@dataclass
class RecallResult:
    sample_id: str
    query: str
    k: int
    relevant_returned: int
    total_relevant: int
    precision_at_k: float
    mrr: float
    recall_at_k: float
    first_hit_rank: int | None


@dataclass
class RecallReport:
    version: int
    total_queries: int
    mean_precision_at_k: float
    mean_mrr: float
    mean_recall_at_k: float
    results: list[RecallResult] = field(default_factory=list)
    elapsed_seconds: float = 0.0


def load_golden(filename: str) -> dict:
    path = GOLDEN_DIR / filename
    if not path.exists():
        print(f"ERROR: golden set not found: {path}", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Classification eval — rule-only (in-process, no network)
# ---------------------------------------------------------------------------


async def eval_classification_rules_only() -> ClassificationReport:
    """Run classification using the rule-based engine only (no LLM, no network)."""
    golden = load_golden("classification_v1.json")
    samples = golden["samples"]
    version = golden["version"]

    # Import the rule classifier directly
    from engram.classification import RuleSnapshot, _classify_rules

    # Rule engine needs rules + taxonomy; with no DB, use empty rules and
    # the default taxonomy. This exercises the "no rule matched" fallback
    # path, which should default to 'fact' — the correct answer for the
    # fact samples but wrong for everything else.
    from engram.memory_kinds import DEFAULT_KIND_TAXONOMY

    taxonomy = list(DEFAULT_KIND_TAXONOMY)
    rules: list[RuleSnapshot] = []

    results: list[ClassificationResult] = []
    start = time.monotonic()

    for sample in samples:
        result = _classify_rules(sample["content"], rules, taxonomy)
        expected = sample["expected_kind"]
        predicted = result.suggested_kind
        correct = predicted == expected
        results.append(
            ClassificationResult(
                sample_id=sample["id"],
                content_preview=sample["content"][:80],
                expected_kind=expected,
                predicted_kind=predicted,
                correct=correct,
                confidence=result.confidence,
                reason=result.reason,
            )
        )

    elapsed = time.monotonic() - start
    correct_count = sum(1 for r in results if r.correct)
    report = ClassificationReport(
        version=version,
        total=len(results),
        correct=correct_count,
        accuracy=correct_count / len(results) if results else 0.0,
        elapsed_seconds=elapsed,
        results=results,
    )

    # Per-kind breakdown
    per_kind: dict[str, dict[str, int]] = {}
    for r in results:
        kind = r.expected_kind
        if kind not in per_kind:
            per_kind[kind] = {"correct": 0, "total": 0}
        per_kind[kind]["total"] += 1
        if r.correct:
            per_kind[kind]["correct"] += 1

    report.per_kind = per_kind

    # Confusion (expected != predicted)
    report.confusion = [
        {
            "sample_id": r.sample_id,
            "expected": r.expected_kind,
            "predicted": r.predicted_kind,
            "content": r.content_preview,
        }
        for r in results
        if not r.correct
    ]

    return report


# ---------------------------------------------------------------------------
# Classification eval — live API (rule + LLM)
# ---------------------------------------------------------------------------


async def eval_classification_live(
    base_url: str, api_key: str
) -> ClassificationReport:
    """Run classification against the live /v1/classify endpoint."""
    import urllib.request

    golden = load_golden("classification_v1.json")
    samples = golden["samples"]
    version = golden["version"]

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    results: list[ClassificationResult] = []
    start = time.monotonic()

    for sample in samples:
        data = json.dumps({"content": sample["content"]}).encode()
        req = urllib.request.Request(
            f"{base_url}/v1/classify", data=data, headers=headers
        )
        try:
            import urllib.error

            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            print(
                f"  ERROR on {sample['id']}: {e.code} {e.read().decode()[:200]}",
                file=sys.stderr,
            )
            continue

        expected = sample["expected_kind"]
        predicted = body.get("suggested_kind", "unknown")
        correct = predicted == expected
        results.append(
            ClassificationResult(
                sample_id=sample["id"],
                content_preview=sample["content"][:80],
                expected_kind=expected,
                predicted_kind=predicted,
                correct=correct,
                confidence=body.get("confidence", 0.0),
                reason=body.get("reason", ""),
            )
        )

    elapsed = time.monotonic() - start
    correct_count = sum(1 for r in results if r.correct)
    report = ClassificationReport(
        version=version,
        total=len(results),
        correct=correct_count,
        accuracy=correct_count / len(results) if results else 0.0,
        elapsed_seconds=elapsed,
        results=results,
    )

    per_kind: dict[str, dict[str, int]] = {}
    for r in results:
        kind = r.expected_kind
        if kind not in per_kind:
            per_kind[kind] = {"correct": 0, "total": 0}
        per_kind[kind]["total"] += 1
        if r.correct:
            per_kind[kind]["correct"] += 1
    report.per_kind = per_kind

    report.confusion = [
        {
            "sample_id": r.sample_id,
            "expected": r.expected_kind,
            "predicted": r.predicted_kind,
            "content": r.content_preview,
        }
        for r in results
        if not r.correct
    ]

    return report


# ---------------------------------------------------------------------------
# Corpus seeding
# ---------------------------------------------------------------------------


async def seed_corpus(base_url: str, api_key: str, corpus_file: str = "corpus_v2.json") -> int:
    """Seed a corpus of memories into a live instance. Returns count written."""
    import urllib.error
    import urllib.request

    corpus = load_golden(corpus_file)
    memories = corpus["memories"]
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    written = 0
    for item in memories:
        payload = {
            "content": item["content"],
            "kind": item.get("kind", "fact"),
            "wing": item.get("wing"),
            "room": item.get("room"),
            "source_type": "import",
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{base_url}/v1/remember", data=data, headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                json.loads(resp.read())  # consume response
                written += 1
        except urllib.error.HTTPError as e:
            err = e.read().decode()[:100]
            print(f"  SEED ERROR: {e.code} {err}", file=sys.stderr)

    return written


async def promote_all_proposed(base_url: str, api_key: str) -> int:
    """Promote all proposed items to active so they're visible to search."""
    import urllib.error
    import urllib.request

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # Get all items — need to find proposed ones via DB since /v1/items
    # doesn't expose review_status filtering reliably
    promoted = 0
    # Try the admin promote endpoint
    try:
        req = urllib.request.Request(
            f"{base_url}/v1/admin/promote", method="POST",
            headers=headers, data=b"{}",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
            promoted = body.get("promoted", 0)
    except urllib.error.HTTPError:
        pass

    return promoted


# ---------------------------------------------------------------------------
# Recall eval — live API
# ---------------------------------------------------------------------------


async def eval_recall_live(
    base_url: str, api_key: str, version: int = 2
) -> RecallReport:
    """Run recall relevance evaluation against a live instance."""
    import urllib.error
    import urllib.request

    golden = load_golden(f"recall_v{version}.json")
    samples = golden["samples"]
    version = golden["version"]

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    results: list[RecallResult] = []
    start = time.monotonic()

    for sample in samples:
        query = sample["query"]
        k = sample.get("k", 5)
        fragments = sample["relevant_content_fragments"]

        data = json.dumps({"query": query, "limit": k}).encode()
        req = urllib.request.Request(
            f"{base_url}/v1/search", data=data, headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            print(
                f"  ERROR on {sample['id']}: {e.code} {e.read().decode()[:200]}",
                file=sys.stderr,
            )
            continue

        items = body.get("results", body.get("items", []))

        # Check which returned items contain relevant fragments
        relevant_found = 0
        first_hit_rank: int | None = None
        for rank, item in enumerate(items, 1):
            content = item.get("content", "").lower()
            for fragment in fragments:
                if fragment.lower() in content:
                    relevant_found += 1
                    if first_hit_rank is None:
                        first_hit_rank = rank
                    break  # one fragment match per item

        total_relevant = len(fragments)
        precision_at_k = relevant_found / k if k > 0 else 0.0
        mrr = 1.0 / first_hit_rank if first_hit_rank is not None else 0.0
        recall_at_k = relevant_found / total_relevant if total_relevant > 0 else 0.0

        results.append(
            RecallResult(
                sample_id=sample["id"],
                query=query,
                k=k,
                relevant_returned=relevant_found,
                total_relevant=total_relevant,
                precision_at_k=precision_at_k,
                mrr=mrr,
                recall_at_k=recall_at_k,
                first_hit_rank=first_hit_rank,
            )
        )

    elapsed = time.monotonic() - start

    if results:
        mean_p = sum(r.precision_at_k for r in results) / len(results)
        mean_mrr = sum(r.mrr for r in results) / len(results)
        mean_r = sum(r.recall_at_k for r in results) / len(results)
    else:
        mean_p = mean_mrr = mean_r = 0.0

    return RecallReport(
        version=version,
        total_queries=len(results),
        mean_precision_at_k=mean_p,
        mean_mrr=mean_mrr,
        mean_recall_at_k=mean_r,
        results=results,
        elapsed_seconds=elapsed,
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_classification_report(report: ClassificationReport, mode: str) -> None:
    print(f"\n{'=' * 70}")
    print(f"  CLASSIFICATION EVAL — {mode.upper()}")
    print(f"  Golden set v{report.version} | {report.total} samples")
    print(f"{'=' * 70}")
    print(f"\n  Accuracy: {report.accuracy:.1%} ({report.correct}/{report.total})")
    print(f"  Elapsed:  {report.elapsed_seconds:.2f}s")

    print("\n  Per-kind breakdown:")
    print(f"  {'Kind':<16} {'Accuracy':>8} {'Correct':>8} {'Total':>6}")
    print(f"  {'-' * 16} {'-' * 8} {'-' * 8} {'-' * 6}")
    for kind in sorted(report.per_kind):
        stats = report.per_kind[kind]
        acc = stats["correct"] / stats["total"] if stats["total"] > 0 else 0.0
        print(f"  {kind:<16} {acc:>7.0%} {stats['correct']:>8} {stats['total']:>6}")

    if report.confusion:
        print(f"\n  Misclassifications ({len(report.confusion)}):")
        for c in report.confusion:
            print(
                f"    {c['sample_id']}: expected={c['expected']}, "
                f"got={c['predicted']}"
            )
            print(f"      \"{c['content']}...\"")

    print()


def print_recall_report(report: RecallReport) -> None:
    print(f"\n{'=' * 70}")
    print("  RECALL RELEVANCE EVAL")
    print(f"  Golden set v{report.version} | {report.total_queries} queries")
    print(f"{'=' * 70}")
    print(f"\n  Mean Precision@K: {report.mean_precision_at_k:.3f}")
    print(f"  Mean MRR:         {report.mean_mrr:.3f}")
    print(f"  Mean Recall@K:    {report.mean_recall_at_k:.3f}")
    print(f"  Elapsed:          {report.elapsed_seconds:.2f}s")

    print("\n  Per-query results:")
    print(f"  {'ID':<10} {'P@K':>6} {'MRR':>6} {'R@K':>6} {'Hit@':>6} Query")
    print(f"  {'-' * 10} {'-' * 6} {'-' * 6} {'-' * 6} {'-' * 6} {'-' * 30}")
    for r in report.results:
        hit_str = str(r.first_hit_rank) if r.first_hit_rank else "-"
        print(
            f"  {r.sample_id:<10} {r.precision_at_k:>6.2f} {r.mrr:>6.2f} "
            f"{r.recall_at_k:>6.2f} {hit_str:>6} {r.query[:40]}"
        )
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Engram evaluation harness — classification + recall metrics"
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Base URL of a live Engram instance (e.g. http://engram01:8000)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key for the live instance",
    )
    parser.add_argument(
        "--rules-only",
        action="store_true",
        help="Run classification eval with rule engine only (no LLM, no instance)",
    )
    parser.add_argument(
        "--recall-only",
        action="store_true",
        help="Run only the recall eval (skip classification)",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Seed the corpus before running recall eval",
    )
    parser.add_argument(
        "--recall-version",
        type=int,
        default=2,
        help="Recall golden set version (default: 2)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )
    args = parser.parse_args()

    reports: dict[str, object] = {}

    if args.seed and args.base_url and args.api_key:
        count = await seed_corpus(args.base_url, args.api_key)
        print(f"Seeded {count} memories")
        # Promote proposed items so they're visible to search
        promoted = await promote_all_proposed(args.base_url, args.api_key)
        if promoted:
            print(f"Promoted {promoted} items to active")
        # Wait for embedding jobs to process
        print("Waiting 10s for embeddings...")
        import time as _time
        _time.sleep(10)

    if not args.recall_only:
        if args.rules_only:
            report = await eval_classification_rules_only()
            print_classification_report(report, "rule-only")
            reports["classification_rules"] = report
        elif args.base_url and args.api_key:
            report = await eval_classification_live(args.base_url, args.api_key)
            print_classification_report(report, "live (rule+LLM)")
            reports["classification_live"] = report
        else:
            # Default: rules-only (no instance required)
            report = await eval_classification_rules_only()
            print_classification_report(report, "rule-only")
            reports["classification_rules"] = report

    if args.base_url and args.api_key and not args.rules_only:
        report = await eval_recall_live(
            args.base_url, args.api_key, version=args.recall_version
        )
        print_recall_report(report)
        reports["recall"] = report

    if args.json:
        def _serialize(obj):
            if hasattr(obj, "__dataclass_fields__"):
                return {k: _serialize(v) for k, v in obj.__dict__.items()}
            if isinstance(obj, list):
                return [_serialize(v) for v in obj]
            return obj

        print(json.dumps(reports, indent=2, default=_serialize))


if __name__ == "__main__":
    asyncio.run(main())
