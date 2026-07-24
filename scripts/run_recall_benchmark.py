#!/usr/bin/env python3
"""Operator-runnable benchmark for Fixture E ranking evidence (ENG-AUDIT-002B).

Usage:

    python scripts/run_recall_benchmark.py \\
        --tenant-id <uuid> \\
        --principal-id <uuid> \\
        --query "the query text" \\
        --expected-item-id <uuid> \\
        --item-budget 10 \\
        --output /tmp/fixture-e-report.json

Or with an existing corpus (no controlled insertion):

    python scripts/run_recall_benchmark.py \\
        --mode existing \\
        --tenant-id <uuid> \\
        --principal-id <uuid> \\
        --query "the query text" \\
        --expected-item-id <uuid> \\
        --item-budget 10 \\
        --query-vector-file /tmp/query_vec.json \\
        --output /tmp/fixture-e-report.json

The script emits a machine-readable JSON report with all ranking stages,
corpus fingerprint, embedding profile, and production-window diagnostics.

Requires a live PostgreSQL with the v2 schema and pgvector, and the
appropriate ENGRAM_DATABASE_URL / ENGRAM_OWNER_DATABASE_URL env vars.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Ensure we can import from the repo root.
_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root))


def _get_repo_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(_repo_root), stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        return "unknown"


async def _resolve_query_vector(
    args: argparse.Namespace,
) -> tuple[list[float] | None, Any]:
    """Resolve query vector from explicit arg, file, or embedding fn."""
    from engram.db import owner_session_factory  # noqa: F401 — ensure DB is ready

    if args.query_vector_file:
        with open(args.query_vector_file) as f:
            vec = json.load(f)
        return list(vec), None

    if args.query_embedding_fn:
        # Use the configured embedding provider to generate the vector.
        from engram.embeddings import generate_embedding

        async def embedding_fn(query: str) -> list[float] | None:
            result = await generate_embedding(query)
            return list(result) if result is not None else None

        return None, embedding_fn

    return None, None


async def run(args: argparse.Namespace) -> int:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from engram.config import settings
    from engram.embedding_profiles import get_active_profile
    from evals.recall_benchmark import (
        ControlledCorpus,
        ServiceBenchmarkSuite,
        existing_corpus_mode,
        small_controlled_corpus,
    )

    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    try:
        # Resolve query vector / embedding fn.
        explicit_vector, embedding_fn = await _resolve_query_vector(args)

        # Build corpus profile.
        corpus: ControlledCorpus | None = None
        if args.mode == "existing":
            profile = existing_corpus_mode(
                query_vector=explicit_vector,
                query_text=args.query,
                query_embedding_fn=embedding_fn,
            )
        else:
            # Controlled mode — use small_controlled_corpus for reproducibility.
            async with session_factory() as session:
                ep = await get_active_profile(session)
            dims = ep.dimensions
            profile = small_controlled_corpus(dims)
            # If using controlled mode, we need to set up the corpus.
            corpus = ControlledCorpus(args.tenant_id, args.principal_id, profile)
            label_map = await corpus.setup(session_factory)
            if args.expected_item_id == "auto" and "target" in label_map:
                args.expected_item_id = label_map["target"]

        try:
            # Run the benchmark.
            suite = ServiceBenchmarkSuite(session_factory)

            query_vector_for_run = explicit_vector if explicit_vector else profile.query_vector

            result = await suite.run_single_query(
                tenant_id=args.tenant_id,
                principal_id=args.principal_id,
                query=args.query,
                query_vector=query_vector_for_run,
                expected_item_id=args.expected_item_id,
                item_budget=args.item_budget,
                byte_budget=args.byte_budget,
                token_budget=args.token_budget,
                query_embedding_fn=embedding_fn,
            )
        finally:
            if corpus is not None:
                await corpus.teardown(session_factory)

        # Gather embedding profile info.
        async with session_factory() as session:
            ep = await get_active_profile(session)

        # Build the report.
        sha = _get_repo_sha()
        query_digest = hashlib.sha256(args.query.encode()).hexdigest()[:16]

        report: dict[str, Any] = {
            "repository_sha": sha,
            "generated_at": datetime.now(UTC).isoformat(),
            "tenant_id_safe": args.tenant_id[:8] + "***",
            "embedding_profile": ep.profile_key,
            "embedding_model": ep.model,
            "embedding_dimensions": ep.dimensions,
            "corpus_profile_name": profile.name,
            "corpus_fingerprint": profile.corpus_fingerprint(),
            "query_digest": query_digest,
            "query_text": args.query,
            "expected_item_id": args.expected_item_id,
            "item_budget": result.item_budget,
            "byte_budget": result.byte_budget,
            "token_budget": result.token_budget,
            "result": {
                "returned_count": result.returned_count,
                "returned_bytes": result.returned_bytes,
                "latency_ms": result.latency_ms,
                "top_k_hit": result.top_k_hit,
                "error": result.error,
            },
        }

        if result.stages:
            s = result.stages
            report["stages"] = {
                "eligible_candidate_count": s.eligible_candidate_count,
                "candidate_window_size": s.candidate_window_size,
                "production_window_raw_rank": s.raw_similarity_rank,
                "production_window_raw_rank_1based": s.raw_similarity_rank_1based,
                "production_window_raw_score": s.raw_similarity_score,
                "raw_rank_exact": s.raw_rank_exact,
                "corpus_raw_rank": s.corpus_raw_rank,
                "corpus_raw_rank_1based": s.corpus_raw_rank_1based,
                "corpus_raw_rank_exact": s.corpus_raw_rank_exact,
                "trust_rank": s.trust_rank,
                "trust_rank_1based": s.trust_rank_1based,
                "trust_score": s.trust_score,
                "post_relationship_rank": s.post_relationship_rank,
                "post_relationship_rank_1based": s.post_relationship_rank_1based,
                "final_served_rank": s.final_served_rank,
                "final_served_rank_1based": s.final_served_rank_1based,
                "final_score": s.final_score,
                "candidate_origin": s.candidate_origin,
                "exclusion_disposition": s.exclusion_disposition,
                "item_budget": s.item_budget,
                "byte_budget": s.byte_budget,
                "token_budget": s.token_budget,
            }

        # Write the report.
        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w") as f:
                json.dump(report, f, indent=2, default=str)
            print(f"Report written to {output_path}", file=sys.stderr)
        else:
            print(json.dumps(report, indent=2, default=str))

        return 0 if result.error is None else 1

    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Operator-runnable recall benchmark for Fixture E evidence"
    )
    parser.add_argument("--tenant-id", required=True, help="Tenant UUID")
    parser.add_argument("--principal-id", required=True, help="Principal UUID")
    parser.add_argument("--query", required=True, help="Query text")
    parser.add_argument("--expected-item-id", required=True, help="Expected item UUID (or 'auto')")
    parser.add_argument("--item-budget", type=int, default=10)
    parser.add_argument("--byte-budget", type=int, default=None)
    parser.add_argument("--token-budget", type=int, default=None)
    parser.add_argument("--mode", choices=["controlled", "existing"], default="existing")
    parser.add_argument("--query-vector-file", help="JSON file with explicit query vector")
    parser.add_argument(
        "--query-embedding-fn",
        action="store_true",
        help="Use the configured embedding provider to generate the query vector",
    )
    parser.add_argument("--output", help="Output path for machine-readable JSON report")
    args = parser.parse_args()

    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
