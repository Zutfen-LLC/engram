"""Real-PostgreSQL upgrade proof for migration 014."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import asyncpg
import pytest

from engram.migrations import normalize_asyncpg_url


async def test_migration_014_preserves_existing_memory_without_backfill() -> None:
    url = os.environ.get("ENGRAM_OWNER_DATABASE_URL") or os.environ.get("ENGRAM_DATABASE_URL")
    if url is None:
        pytest.skip("requires a live PostgreSQL with the v2 schema")
    try:
        conn = await asyncpg.connect(normalize_asyncpg_url(url))
    except Exception:
        pytest.skip("requires a live PostgreSQL with the v2 schema")
    schema = f"classification_evidence_{uuid.uuid4().hex}"
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    item_id = uuid.uuid4()
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}", public')
        await conn.execute(
            """
            CREATE TABLE tenants (id UUID PRIMARY KEY);
            CREATE TABLE workspaces (id UUID PRIMARY KEY);
            CREATE TABLE principals (id UUID PRIMARY KEY);
            CREATE TABLE memory_items (
                id UUID PRIMARY KEY,
                memory_confidence REAL NOT NULL,
                source_trust REAL NOT NULL
            );
            """
        )
        await conn.execute("INSERT INTO tenants VALUES ($1)", tenant_id)
        await conn.execute("INSERT INTO principals VALUES ($1)", principal_id)
        await conn.execute(
            "INSERT INTO memory_items VALUES ($1, 0.77, 0.66)", item_id
        )
        await conn.execute(Path("migrations/014_classification_evidence.sql").read_text())

        row = await conn.fetchrow(
            "SELECT memory_confidence,source_trust,source_confidence_prior,"
            "retention_confidence,retention_disposition,retention_evidence_at "
            "FROM memory_items WHERE id=$1",
            item_id,
        )
        assert row is not None
        assert tuple(row[:2]) == pytest.approx((0.77, 0.66))
        assert tuple(row[2:]) == (None, None, None, None)
        assert await conn.fetchval(
            "SELECT relforcerowsecurity FROM pg_class WHERE oid='classification_runs'::regclass"
        ) is True
    finally:
        await conn.execute("RESET search_path")
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()
