"""Real-PostgreSQL rollout proof for Promotion Path A v2 migration 016."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import asyncpg
import pytest

from engram.migrations import normalize_asyncpg_url


async def test_migration_016_preserves_legacy_policy_and_seeds_future_defaults() -> None:
    url = os.environ.get("ENGRAM_OWNER_DATABASE_URL") or os.environ.get("ENGRAM_DATABASE_URL")
    if url is None:
        pytest.skip("requires a live PostgreSQL owner database")
    try:
        conn = await asyncpg.connect(normalize_asyncpg_url(url))
    except Exception:
        pytest.skip("requires a live PostgreSQL owner database")
    schema = f"promotion_v2_{uuid.uuid4().hex}"
    existing = uuid.uuid4()
    missing_config = uuid.uuid4()
    future = uuid.uuid4()
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}", public')
        await conn.execute(
            """
            CREATE TABLE tenants (id UUID PRIMARY KEY);
            CREATE TABLE tenant_config (
                tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                config_version TEXT NOT NULL,
                active BOOLEAN NOT NULL,
                auto_promote_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                auto_promote_confidence_threshold REAL NOT NULL DEFAULT 0.7,
                auto_promote_min_age_hours INTEGER NOT NULL DEFAULT 72
            );
            CREATE TABLE memory_kinds (
                tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                display_name TEXT NOT NULL,
                description TEXT,
                is_builtin BOOLEAN NOT NULL DEFAULT FALSE,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                singleton BOOLEAN NOT NULL DEFAULT FALSE,
                stays_in_recall_when_disputed BOOLEAN NOT NULL DEFAULT FALSE,
                requires_review BOOLEAN NOT NULL DEFAULT FALSE,
                default_importance DOUBLE PRECISION,
                sort_order INTEGER NOT NULL DEFAULT 100,
                PRIMARY KEY (tenant_id, name)
            );
            CREATE FUNCTION seed_builtin_memory_kinds() RETURNS TRIGGER AS $$
            BEGIN RETURN NEW; END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER trg_seed_builtin_memory_kinds
                AFTER INSERT ON tenants FOR EACH ROW
                EXECUTE FUNCTION seed_builtin_memory_kinds();
            """
        )
        await conn.execute("INSERT INTO tenants VALUES ($1), ($2)", existing, missing_config)
        await conn.execute(
            "INSERT INTO tenant_config "
            "(tenant_id, config_version, active, auto_promote_enabled, "
            "auto_promote_confidence_threshold, auto_promote_min_age_hours) "
            "VALUES ($1, 'legacy', TRUE, FALSE, 0.81, 96)",
            existing,
        )
        await conn.execute(
            "INSERT INTO memory_kinds "
            "(tenant_id, name, display_name, is_builtin) VALUES "
            "($1, 'fact', 'Fact', TRUE), ($1, 'doctrine', 'Doctrine', TRUE), "
            "($1, 'custom', 'Custom', FALSE)",
            existing,
        )

        await conn.execute(Path("migrations/016_promotion_path_a_v2.sql").read_text())

        legacy = await conn.fetchrow(
            "SELECT auto_promote_enabled, auto_promote_confidence_threshold, "
            "auto_promote_min_age_hours, auto_promote_evidence_enabled "
            "FROM tenant_config WHERE tenant_id=$1 AND active=TRUE",
            existing,
        )
        assert legacy is not None
        assert legacy["auto_promote_enabled"] is False
        assert legacy["auto_promote_confidence_threshold"] == pytest.approx(0.81)
        assert legacy["auto_promote_min_age_hours"] == 96
        assert legacy["auto_promote_evidence_enabled"] is False
        assert (
            await conn.fetchval(
                "SELECT auto_promote_evidence_enabled FROM tenant_config "
                "WHERE tenant_id=$1 AND active=TRUE",
                missing_config,
            )
            is False
        )
        policies = {
            row["name"]: row["auto_promote_from_inferred"]
            for row in await conn.fetch(
                "SELECT name, auto_promote_from_inferred FROM memory_kinds WHERE tenant_id=$1",
                existing,
            )
        }
        assert policies == {"fact": True, "doctrine": False, "custom": False}

        await conn.execute("INSERT INTO tenants VALUES ($1)", future)
        await conn.execute(
            "INSERT INTO tenant_config (tenant_id, config_version, active) VALUES ($1, 'v1', TRUE)",
            future,
        )
        assert (
            await conn.fetchval(
                "SELECT auto_promote_evidence_enabled FROM tenant_config WHERE tenant_id=$1",
                future,
            )
            is True
        )
        future_policies = {
            row["name"]: row["auto_promote_from_inferred"]
            for row in await conn.fetch(
                "SELECT name, auto_promote_from_inferred FROM memory_kinds WHERE tenant_id=$1",
                future,
            )
        }
        assert future_policies["fact"] is True
        assert future_policies["decision"] is True
        assert future_policies["doctrine"] is False
    finally:
        await conn.execute("RESET search_path")
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()
