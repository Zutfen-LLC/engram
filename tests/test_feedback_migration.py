"""Real-PostgreSQL proof for migration 011 feedback canonicalization."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from asyncpg import CheckViolationError, UniqueViolationError

from engram.migrations import normalize_asyncpg_url


async def _connect():
    import asyncpg

    url = os.environ.get("ENGRAM_OWNER_DATABASE_URL") or os.environ.get("ENGRAM_DATABASE_URL")
    if url is None:
        pytest.skip("requires a live PostgreSQL with the v2 schema")
    try:
        return await asyncpg.connect(normalize_asyncpg_url(url))
    except Exception:
        pytest.skip("requires a live PostgreSQL with the v2 schema")


async def test_migration_011_deterministically_canonicalizes_history_and_constraints():
    conn = await _connect()
    schema = f"feedback_migration_{uuid.uuid4().hex}"
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}"')
        await conn.execute(
            """
            CREATE TABLE feedback_events (
                id UUID PRIMARY KEY,
                tenant_id UUID NOT NULL,
                item_id UUID NOT NULL,
                principal_id UUID NOT NULL,
                verdict VARCHAR(10) NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            );
            CREATE TABLE tenant_config (
                tenant_id UUID PRIMARY KEY
            );
            CREATE TABLE memory_items (
                id UUID PRIMARY KEY,
                importance DOUBLE PRECISION NOT NULL
            );
            """
        )
        tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
        item_a, item_b = uuid.uuid4(), uuid.uuid4()
        principal_a, principal_b = uuid.uuid4(), uuid.uuid4()
        base = datetime(2026, 1, 1, tzinfo=UTC)
        ordinary = [uuid.uuid4() for _ in range(3)]
        tied = [uuid.UUID(int=value) for value in (30, 10, 20)]
        independent = [uuid.uuid4(), uuid.uuid4()]
        seeded = [
            (ordinary[2], tenant_a, item_a, principal_a, "useful", base + timedelta(hours=2)),
            (ordinary[0], tenant_a, item_a, principal_a, "noise", base),
            (ordinary[1], tenant_a, item_a, principal_a, "useful", base + timedelta(hours=1)),
            *((event_id, tenant_a, item_b, principal_a, "noise", base) for event_id in tied),
            (independent[0], tenant_a, item_a, principal_b, "useful", base),
            (independent[1], tenant_b, item_a, principal_a, "noise", base),
        ]
        await conn.executemany(
            "INSERT INTO feedback_events "
            "(id, tenant_id, item_id, principal_id, verdict, created_at) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            seeded,
        )
        marker_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO memory_items (id, importance) VALUES ($1, 0.731)", marker_id
        )
        migration = Path("migrations/011_feedback_integrity.sql").read_text()
        await conn.execute(migration)

        rows = await conn.fetch(
            "SELECT id, replaces_feedback_event_id, superseded_at, created_at "
            "FROM feedback_events WHERE tenant_id=$1 AND item_id=$2 AND principal_id=$3 "
            "ORDER BY created_at, id",
            tenant_a,
            item_a,
            principal_a,
        )
        assert [row["id"] for row in rows] == ordinary
        assert [row["replaces_feedback_event_id"] for row in rows] == [
            None,
            ordinary[0],
            ordinary[1],
        ]
        assert [row["superseded_at"] for row in rows] == [
            rows[1]["created_at"],
            rows[2]["created_at"],
            None,
        ]

        tied_rows = await conn.fetch(
            "SELECT id, replaces_feedback_event_id, superseded_at FROM feedback_events "
            "WHERE tenant_id=$1 AND item_id=$2 ORDER BY created_at, id",
            tenant_a,
            item_b,
        )
        expected_tied = sorted(tied)
        assert [row["id"] for row in tied_rows] == expected_tied
        assert [row["replaces_feedback_event_id"] for row in tied_rows] == [
            None,
            expected_tied[0],
            expected_tied[1],
        ]
        assert [row["superseded_at"] is None for row in tied_rows] == [False, False, True]

        current_groups = await conn.fetchval(
            "SELECT count(*) FROM feedback_events WHERE superseded_at IS NULL"
        )
        assert current_groups == 4
        assert await conn.fetchval(
            "SELECT importance FROM memory_items WHERE id=$1", marker_id
        ) == pytest.approx(0.731)

        with pytest.raises(UniqueViolationError):
            await conn.execute(
                "INSERT INTO feedback_events "
                "(id, tenant_id, item_id, principal_id, verdict, created_at) "
                "VALUES ($1, $2, $3, $4, 'noise', now())",
                uuid.uuid4(),
                tenant_a,
                item_a,
                principal_a,
            )
        await conn.execute("ROLLBACK")
        await conn.execute(f'SET search_path TO "{schema}"')

        for limit in (1, 100000):
            await conn.execute(
                "INSERT INTO tenant_config (tenant_id, feedback_daily_limit) VALUES ($1, $2)",
                uuid.uuid4(),
                limit,
            )
        for invalid in (0, 100001):
            with pytest.raises(CheckViolationError):
                await conn.execute(
                    "INSERT INTO tenant_config (tenant_id, feedback_daily_limit) VALUES ($1, $2)",
                    uuid.uuid4(),
                    invalid,
                )
            await conn.execute("ROLLBACK")
            await conn.execute(f'SET search_path TO "{schema}"')
    finally:
        await conn.execute("RESET search_path")
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()
