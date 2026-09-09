"""Real-PostgreSQL migration proof for explicit candidate priority (#196)."""

import os
import uuid
from pathlib import Path

import asyncpg
import pytest

from engram.migrations import normalize_asyncpg_url

_MIGRATION = Path("migrations/043_explicit_priority.sql")
_DOWNGRADE = Path("migrations/downgrades/043_explicit_priority.sql")


def test_explicit_priority_migration_snapshots_legacy_importance_without_reconstruction() -> None:
    sql = Path("migrations/043_explicit_priority.sql").read_text()

    assert "ADD COLUMN IF NOT EXISTS explicit_priority REAL" in sql
    assert "SET importance = 0.5" in sql
    assert "SET explicit_priority = importance" in sql
    assert "ALTER COLUMN explicit_priority SET NOT NULL" in sql
    assert "trg_memory_items_default_explicit_priority" in sql
    assert "feedback_events" not in sql


async def test_migration_043_snapshots_priority_and_enforces_insert_contract() -> None:
    url = os.environ.get("ENGRAM_OWNER_DATABASE_URL") or os.environ.get("ENGRAM_DATABASE_URL")
    if url is None:
        pytest.skip("requires a live PostgreSQL owner database")
    try:
        conn = await asyncpg.connect(normalize_asyncpg_url(url))
    except Exception:
        pytest.skip("requires a live PostgreSQL owner database")

    schema = f"explicit_priority_{uuid.uuid4().hex}"
    historical_item_id = uuid.uuid4()
    historical_null_item_id = uuid.uuid4()
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}", public')
        await conn.execute(
            "CREATE TABLE memory_items ("
            "id UUID PRIMARY KEY, importance REAL DEFAULT 0.5)"
        )
        await conn.execute(
            "INSERT INTO memory_items (id, importance) VALUES ($1, $2), ($3, NULL)",
            historical_item_id,
            0.731,
            historical_null_item_id,
        )

        await conn.execute(_MIGRATION.read_text())
        await conn.execute(_MIGRATION.read_text())

        historical = await conn.fetchrow(
            "SELECT importance, explicit_priority FROM memory_items WHERE id = $1",
            historical_item_id,
        )
        assert historical is not None
        assert historical["importance"] == pytest.approx(0.731)
        assert historical["explicit_priority"] == pytest.approx(0.731)

        historical_null = await conn.fetchrow(
            "SELECT importance, explicit_priority FROM memory_items WHERE id = $1",
            historical_null_item_id,
        )
        assert historical_null is not None
        assert historical_null["importance"] == pytest.approx(0.5)
        assert historical_null["explicit_priority"] == pytest.approx(0.5)

        trigger_default_item_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO memory_items (id, importance) VALUES ($1, $2)",
            trigger_default_item_id,
            0.41,
        )
        assert (
            await conn.fetchval(
                "SELECT explicit_priority FROM memory_items WHERE id = $1", trigger_default_item_id
            )
            == pytest.approx(0.41)
        )

        creation_default_item_id = uuid.uuid4()
        await conn.execute("INSERT INTO memory_items (id) VALUES ($1)", creation_default_item_id)
        assert (
            await conn.fetchval(
                "SELECT explicit_priority FROM memory_items WHERE id = $1", creation_default_item_id
            )
            == pytest.approx(0.5)
        )

        explicit_item_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO memory_items (id, importance, explicit_priority) VALUES ($1, $2, $3)",
            explicit_item_id,
            0.2,
            0.91,
        )
        assert (
            await conn.fetchval(
                "SELECT explicit_priority FROM memory_items WHERE id = $1", explicit_item_id
            )
            == pytest.approx(0.91)
        )

        assert (
            await conn.fetchval(
                "SELECT is_nullable = 'NO' FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'memory_items' "
                "AND column_name = 'explicit_priority'"
            )
            is True
        )
        assert (
            await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_trigger "
                "WHERE tgrelid = 'memory_items'::regclass "
                "AND tgname = 'trg_memory_items_default_explicit_priority' "
                "AND NOT tgisinternal)"
            )
            is True
        )

        await conn.execute(_DOWNGRADE.read_text())
        assert (
            await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'memory_items' "
                "AND column_name = 'explicit_priority')"
            )
            is False
        )
    finally:
        await conn.execute("RESET search_path")
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()
