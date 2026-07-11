"""Real-PostgreSQL verification for migration 013 session-end defaults."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from asyncpg import CheckViolationError

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


async def test_migration_013_backfills_defaults_bounds_and_preserves_existing_state() -> None:
    conn = await _connect()
    schema = f"session_end_migration_{uuid.uuid4().hex}"
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}"')
        await conn.execute(
            """
            CREATE TABLE tenant_config (
                tenant_id UUID PRIMARY KEY,
                trust_sync_turn REAL NOT NULL DEFAULT 0.4,
                confidence_sync_turn REAL NOT NULL DEFAULT 0.4
            );
            CREATE TABLE memory_items (
                id UUID PRIMARY KEY,
                authority SMALLINT NOT NULL
            );
            """
        )
        existing_tenant = uuid.uuid4()
        marker_item = uuid.uuid4()
        await conn.execute(
            "INSERT INTO tenant_config "
            "(tenant_id, trust_sync_turn, confidence_sync_turn) VALUES ($1, 0.61, 0.62)",
            existing_tenant,
        )
        await conn.execute("INSERT INTO memory_items (id, authority) VALUES ($1, 40)", marker_item)
        rls_before = await conn.fetchval(
            "SELECT relrowsecurity FROM pg_class WHERE oid='tenant_config'::regclass"
        )

        await conn.execute(Path("migrations/013_session_end_defaults.sql").read_text())

        existing = await conn.fetchrow(
            "SELECT trust_sync_turn, confidence_sync_turn, trust_session_end, "
            "confidence_session_end FROM tenant_config WHERE tenant_id=$1",
            existing_tenant,
        )
        assert existing is not None
        assert tuple(existing) == pytest.approx((0.61, 0.62, 0.35, 0.35))
        assert (
            await conn.fetchval("SELECT authority FROM memory_items WHERE id=$1", marker_item) == 40
        )
        assert (
            await conn.fetchval(
                "SELECT relrowsecurity FROM pg_class WHERE oid='tenant_config'::regclass"
            )
            is rls_before
        )

        for trust, confidence in ((0.0, 1.0), (1.0, 0.0), (0.35, 0.35)):
            row = await conn.fetchrow(
                "INSERT INTO tenant_config "
                "(tenant_id, trust_session_end, confidence_session_end) "
                "VALUES ($1, $2, $3) RETURNING trust_session_end, confidence_session_end",
                uuid.uuid4(),
                trust,
                confidence,
            )
            assert row is not None
            assert tuple(row) == pytest.approx((trust, confidence))

        default_row = await conn.fetchrow(
            "INSERT INTO tenant_config (tenant_id) VALUES ($1) "
            "RETURNING trust_session_end, confidence_session_end",
            uuid.uuid4(),
        )
        assert default_row is not None
        assert tuple(default_row) == pytest.approx((0.35, 0.35))

        for trust, confidence in ((-0.01, 0.5), (1.01, 0.5), (0.5, -0.01), (0.5, 1.01)):
            with pytest.raises(CheckViolationError):
                await conn.execute(
                    "INSERT INTO tenant_config "
                    "(tenant_id, trust_session_end, confidence_session_end) VALUES ($1, $2, $3)",
                    uuid.uuid4(),
                    trust,
                    confidence,
                )
            await conn.execute("ROLLBACK")
            await conn.execute(f'SET search_path TO "{schema}"')
    finally:
        await conn.execute("RESET search_path")
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()
