#!/usr/bin/env python3
"""Startup recall performance baseline / benchmark (ENG-AUD-011 / F18).

Populates a scratch tenant with a synthetic corpus at each requested size,
runs startup recall through one unprofiled ``ResolvedMemoryContext``, and
reports:

* eligible corpus rows
* rows loaded into Python (old full-corpus path, for comparison)
* rows fully scored by the bounded pipeline (candidate_count)
* returned item count
* query count for candidate selection
* approximate wall time for both paths

Connects directly to the database (``ENGRAM_DATABASE_URL`` / ``.env``) — does
not require the HTTP service to be running. Requires a live PostgreSQL with
the v2 schema (``engram init-db``).

Usage::

    python scripts/benchmark_startup_recall.py --items 100 --items 1000 --items 10000
"""

from __future__ import annotations

import argparse
import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.auth import Principal
from engram.config import settings
from engram.db import apply_rls_context
from engram.memory_context import ResolvedMemoryContext, unrestricted_memory_context
from engram.models import MemoryItem
from engram.recall import _fetch_active_items, execute_startup_recall

_TENANT_SLUG = "default"
_PRINCIPAL_NAME = "admin"


def _rows(tenant_id: str, principal_id: str, n: int) -> list[dict[str, object]]:
    now = datetime.now(UTC)
    rows = []
    for i in range(n):
        rows.append(
            {
                "id": uuid.uuid4(),
                "tenant_id": uuid.UUID(tenant_id),
                "workspace_id": None,
                "principal_id": uuid.UUID(principal_id),
                "content": f"benchmark memory item {i}",
                "content_hash": f"sha256:{uuid.uuid4().hex}",
                "kind": "fact",
                "visibility": "tenant",
                "review_status": "active",
                "memory_confidence": 0.5 + (i % 10) / 100,
                "source_trust": 0.5,
                "importance": 0.3 + (i % 7) / 10,
                "pinned": i < min(5, n // 100 + 1),
                "human_verified": i % 13 == 0,
                "created_at": now - timedelta(days=i % 90),
                "valid_from": now - timedelta(days=i % 90),
            }
        )
    return rows


async def _measure_recall_paths(
    factory: async_sessionmaker[AsyncSession],
    memory_context: ResolvedMemoryContext,
) -> tuple[list[MemoryItem], dict[str, Any], float, float]:
    """Run the reference and bounded paths with the same immutable context."""
    tenant_id = str(memory_context.tenant_id)
    principal_id = str(memory_context.principal_id)

    # Old path: full-corpus load into Python (for comparison only).
    async with factory() as session:
        await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
        start = time.perf_counter()
        full_corpus = await _fetch_active_items(session, memory_context, None)
        old_ms = (time.perf_counter() - start) * 1000

    # New path: bounded two-stage pipeline.
    async with factory() as session:
        await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
        start = time.perf_counter()
        result = await execute_startup_recall(
            session=session,
            memory_context=memory_context,
            workspace=None,
            byte_budget=10_000_000,
            token_budget=None,
        )
        new_ms = (time.perf_counter() - start) * 1000

    return full_corpus, result, old_ms, new_ms


async def _run(sizes: list[int]) -> None:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT t.id::text AS tenant_id, p.id::text AS principal_id "
                        "FROM tenants t "
                        "JOIN principals p ON p.tenant_id = t.id AND p.name = :principal "
                        "WHERE t.slug = :slug"
                    ),
                    {"slug": _TENANT_SLUG, "principal": _PRINCIPAL_NAME},
                )
            )
            .mappings()
            .one()
        )
    tenant_id, principal_id = str(row["tenant_id"]), str(row["principal_id"])
    memory_context = unrestricted_memory_context(
        Principal(
            tenant_id=tenant_id,
            principal_id=principal_id,
            scopes=("read",),
        )
    )

    print(
        f"{'items':>8} {'eligible':>8} {'py_loaded(old)':>15} {'candidates(new)':>16} "
        f"{'scored(new)':>12} {'returned':>9} {'queries':>8} "
        f"{'old_ms':>9} {'new_ms':>9}"
    )

    for n in sizes:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM recall_logs"))
            await conn.execute(text("DELETE FROM jobs"))
            await conn.execute(text("DELETE FROM memory_items"))

        async with factory() as session:
            await session.execute(insert(MemoryItem), _rows(tenant_id, principal_id, n))
            await session.commit()

        full_corpus, result, old_ms, new_ms = await _measure_recall_paths(factory, memory_context)

        print(
            f"{n:>8} {len(full_corpus):>8} {len(full_corpus):>15} "
            f"{result['candidate_count']:>16} {result['scored_count']:>12} "
            f"{result['item_count']:>9} {result['candidate_stats']['query_count']:>8} "
            f"{old_ms:>9.1f} {new_ms:>9.1f}"
        )

    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM recall_logs"))
        await conn.execute(text("DELETE FROM jobs"))
        await conn.execute(text("DELETE FROM memory_items"))
    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--items",
        type=int,
        action="append",
        default=None,
        help="Corpus size to benchmark (repeatable). Default: 100 1000 10000.",
    )
    args = parser.parse_args()
    sizes = args.items or [100, 1000, 10000]
    asyncio.run(_run(sizes))


if __name__ == "__main__":
    main()
