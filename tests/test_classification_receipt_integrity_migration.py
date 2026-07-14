"""Real-PostgreSQL upgrade proof for migration 015."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import asyncpg
import pytest

from engram.migrations import normalize_asyncpg_url


async def test_migration_015_backfills_only_previously_bound_receipts() -> None:
    url = os.environ.get("ENGRAM_OWNER_DATABASE_URL") or os.environ.get("ENGRAM_DATABASE_URL")
    if url is None:
        pytest.skip("requires a live PostgreSQL with the v2 schema")
    try:
        conn = await asyncpg.connect(normalize_asyncpg_url(url))
    except Exception:
        pytest.skip("requires a live PostgreSQL with the v2 schema")
    schema = f"classification_receipt_integrity_{uuid.uuid4().hex}"
    bound_id = uuid.uuid4()
    unbound_id = uuid.uuid4()
    memory_id = uuid.uuid4()
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}", public')
        await conn.execute(
            """
            CREATE TABLE memory_items (id UUID PRIMARY KEY);
            CREATE TABLE classification_runs (
                id UUID PRIMARY KEY,
                tenant_id UUID NOT NULL,
                memory_item_id UUID UNIQUE REFERENCES memory_items(id) ON DELETE SET NULL,
                created_at TIMESTAMPTZ NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL
            );
            CREATE INDEX idx_classification_runs_expired_unbound
                ON classification_runs (tenant_id, expires_at)
                WHERE memory_item_id IS NULL;
            """
        )
        await conn.execute("INSERT INTO memory_items VALUES ($1)", memory_id)
        await conn.execute(
            """
            INSERT INTO classification_runs
                (id, tenant_id, memory_item_id, created_at, expires_at)
            VALUES
                ($1, $3, $4, '2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z'),
                ($2, $3, NULL, '2026-01-02T00:00:00Z', '2026-01-02T01:00:00Z')
            """,
            bound_id,
            unbound_id,
            uuid.uuid4(),
            memory_id,
        )

        await conn.execute(Path("migrations/015_classification_receipt_integrity.sql").read_text())

        bound_at = await conn.fetchval(
            "SELECT bound_at FROM classification_runs WHERE id=$1", bound_id
        )
        created_at = await conn.fetchval(
            "SELECT created_at FROM classification_runs WHERE id=$1", bound_id
        )
        assert bound_at == created_at
        assert (
            await conn.fetchval("SELECT bound_at FROM classification_runs WHERE id=$1", unbound_id)
            is None
        )
        predicate = await conn.fetchval(
            "SELECT pg_get_expr(indpred, indrelid) FROM pg_index "
            "WHERE indexrelid='idx_classification_runs_expired_unbound'::regclass"
        )
        assert predicate == "(bound_at IS NULL)"
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE classification_runs SET memory_item_id=$1 WHERE id=$2",
                memory_id,
                unbound_id,
            )
    finally:
        await conn.execute("RESET search_path")
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()
