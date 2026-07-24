#!/usr/bin/env python3
"""Operator-runnable benchmark for Fixture E ranking evidence (ENG-AUDIT-002B).

Usage (existing corpus with explicit query vector):

    python scripts/run_recall_benchmark.py \\
        --mode existing \\
        --tenant-id <uuid> \\
        --principal-id <uuid> \\
        --query "the query text" \\
        --expected-item-id <uuid> \\
        --item-budget 10 \\
        --query-vector-file /tmp/query_vec.json \\
        --output /tmp/fixture-e-report.json

Existing corpus with injected embedding (resolves active EmbeddingProfile):

    python scripts/run_recall_benchmark.py \\
        --mode existing \\
        --tenant-id <uuid> \\
        --principal-id <uuid> \\
        --query "the query text" \\
        --expected-item-id <uuid> \\
        --item-budget 10 \\
        --use-embedding-provider \\
        --output /tmp/fixture-e-report.json

With memory profile and workspace context (F15):

    python scripts/run_recall_benchmark.py \\
        --mode existing \\
        --tenant-id <uuid> \\
        --principal-id <uuid> \\
        --memory-profile-id <uuid> \\
        --memory-profile-revision-id <uuid> \\
        --workspace-id <uuid> \\
        --query "..." --expected-item-id <uuid> \\
        --use-embedding-provider \\
        --redacted \\
        --output /tmp/fixture-e-redacted.json

The script emits a machine-readable JSON report with all ranking stages,
corpus fingerprint, embedding profile, resolved memory context, and
production-window diagnostics.

Requires a live PostgreSQL with the v2 schema and pgvector.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
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


async def run(args: argparse.Namespace) -> int:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from engram.config import settings
    from engram.embedding_profiles import get_active_profile
    from engram.relationship_recall import RECALL_SCORING_VERSION
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
        # --- Resolve active EmbeddingProfile (F14) ---
        async with session_factory() as session:
            ep = await get_active_profile(session)

        # --- Resolve query vector ---
        query_vector: list[float] | None = None
        embedding_fn = None

        if args.query_vector_file:
            with open(args.query_vector_file) as f:
                query_vector = list(json.load(f))
            # F14: validate dimensions match the active profile.
            if len(query_vector) != ep.dimensions:
                print(
                    f"ERROR: query vector dims ({len(query_vector)}) do not match "
                    f"active profile ({ep.dimensions})",
                    file=sys.stderr,
                )
                return 2

        if query_vector is None and args.use_embedding_provider:
            # F14: resolve the active EmbeddingProfile BEFORE generating the
            # embedding, and pass it explicitly to generate_embedding.
            from engram.embeddings import generate_embedding

            async def profile_aware_embedding_fn(query: str) -> list[float] | None:
                result = await generate_embedding(
                    query,
                    profile=ep,
                    tenant_id=args.tenant_id,
                    principal_id=args.principal_id,
                    workspace_id=args.workspace_id,
                    operation="embedding_query_recall",
                    usage_class="diagnostic",
                )
                return list(result) if result is not None else None

            embedding_fn = profile_aware_embedding_fn

        # --- Build corpus profile ---
        corpus: ControlledCorpus | None = None
        if args.mode == "existing":
            profile = existing_corpus_mode(
                query_vector=query_vector,
                query_text=args.query,
                query_embedding_fn=embedding_fn,
            )
        else:
            # Controlled mode.
            dims = ep.dimensions
            profile = small_controlled_corpus(dims)
            corpus = ControlledCorpus(args.tenant_id, args.principal_id, profile)
            label_map = await corpus.setup(session_factory)
            if args.expected_item_id == "auto" and "target" in label_map:
                args.expected_item_id = label_map["target"]

        try:
            suite = ServiceBenchmarkSuite(session_factory)

            query_vector_for_run = query_vector if query_vector else profile.query_vector

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
                workspace_id=args.workspace_id,
                memory_profile_id=args.memory_profile_id,
                memory_profile_revision_id=args.memory_profile_revision_id,
                memory_profile_slug=args.memory_profile_slug,
                memory_profile_version=args.memory_profile_version,
            )
        finally:
            if corpus is not None:
                await corpus.teardown(session_factory)

        # --- Compute eligible corpus fingerprint (F13) ---
        from engram.auth import Principal
        from engram.memory_context import unrestricted_memory_context

        principal = Principal(
            tenant_id=args.tenant_id,
            principal_id=args.principal_id,
            api_key_id="",
            scopes=("read",),
            memory_profile_id=args.memory_profile_id,
            memory_profile_revision_id=args.memory_profile_revision_id,
            memory_profile_slug=args.memory_profile_slug,
            memory_profile_version=args.memory_profile_version,
        )
        ctx = unrestricted_memory_context(principal)
        eligible_fp, eligible_count = await suite.compute_eligible_corpus_fingerprint(
            tenant_id=args.tenant_id,
            memory_context=ctx,
            embedding_profile_key=ep.profile_key,
        )

        # --- Build the report ---
        sha = _get_repo_sha()

        from evals.recall_benchmark import BenchmarkResult

        all_results: list[BenchmarkResult] = [result]

        # Run comparison budgets if requested.
        if args.comparison_budgets:
            for budget in args.comparison_budgets:
                comp_result = await suite.run_single_query(
                    tenant_id=args.tenant_id,
                    principal_id=args.principal_id,
                    query=args.query,
                    query_vector=query_vector_for_run,
                    expected_item_id=args.expected_item_id,
                    item_budget=budget,
                    query_embedding_fn=embedding_fn,
                    workspace_id=args.workspace_id,
                    memory_profile_id=args.memory_profile_id,
                    memory_profile_revision_id=args.memory_profile_revision_id,
                    memory_profile_slug=args.memory_profile_slug,
                    memory_profile_version=args.memory_profile_version,
                )
                all_results.append(comp_result)

        report = ServiceBenchmarkSuite.generate_report(
            repository_sha=sha,
            results=all_results,
            corpus_profile=profile,
            embedding_profile_key=ep.profile_key,
            embedding_model=ep.model,
            embedding_dimensions=ep.dimensions,
            scoring_version=RECALL_SCORING_VERSION,
            config_version="v1",
            eligible_corpus_fingerprint=eligible_fp,
            eligible_item_count=eligible_count,
            memory_context_version=ctx.version,
            memory_profile_slug=args.memory_profile_slug,
            memory_profile_version=args.memory_profile_version,
            workspace_id=args.workspace_id,
        )

        # --- Output ---
        import dataclasses

        def _default(obj: Any) -> Any:
            if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
                return dataclasses.asdict(obj)
            raise TypeError(f"Not serializable: {type(obj)}")

        if args.redacted:
            output_data: Any = ServiceBenchmarkSuite.generate_redacted_report(report)
        else:
            output_data = dataclasses.asdict(report)

        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w") as f:
                json.dump(output_data, f, indent=2, default=_default)
            print(f"Report written to {output_path}", file=sys.stderr)
        else:
            print(json.dumps(output_data, indent=2, default=_default))

        return 0 if all(r.error is None for r in all_results) else 1

    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Operator-runnable recall benchmark for Fixture E evidence"
    )
    parser.add_argument("--tenant-id", required=True, help="Tenant UUID")
    parser.add_argument("--principal-id", required=True, help="Principal UUID")
    parser.add_argument("--query", required=True, help="Query text")
    parser.add_argument(
        "--expected-item-id", required=True, help="Expected item UUID (or 'auto')"
    )
    parser.add_argument("--item-budget", type=int, default=10)
    parser.add_argument("--byte-budget", type=int, default=None)
    parser.add_argument("--token-budget", type=int, default=None)
    parser.add_argument(
        "--comparison-budgets",
        type=int,
        nargs="*",
        default=None,
        help="Additional budgets to run for comparison (e.g., 5 10 20)",
    )
    parser.add_argument(
        "--mode", choices=["controlled", "existing"], default="existing"
    )
    parser.add_argument("--query-vector-file", help="JSON file with explicit query vector")
    parser.add_argument(
        "--use-embedding-provider",
        action="store_true",
        help="Resolve active EmbeddingProfile and use it to generate the query vector",
    )
    # F15: memory profile and workspace context.
    parser.add_argument("--memory-profile-id", default=None, help="Memory profile UUID")
    parser.add_argument(
        "--memory-profile-revision-id", default=None, help="Memory profile revision UUID"
    )
    parser.add_argument("--memory-profile-slug", default=None, help="Memory profile slug")
    parser.add_argument(
        "--memory-profile-version", type=int, default=None, help="Memory profile version"
    )
    parser.add_argument("--workspace-id", default=None, help="Workspace UUID")
    parser.add_argument("--redacted", action="store_true", help="Emit redacted report")
    parser.add_argument("--output", help="Output path for machine-readable JSON report")
    args = parser.parse_args()

    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
