from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from engram.migrations import normalize_asyncpg_url

_MIGRATION = Path(__file__).parents[1] / "migrations" / "023_profile_read_context.sql"


def _dsn() -> str | None:
    return os.environ.get("ENGRAM_OWNER_DATABASE_URL") or os.environ.get(
        "ENGRAM_DATABASE_URL"
    )


async def _owner():
    import asyncpg

    if not _dsn():
        pytest.skip("requires owner and app PostgreSQL URLs")
    try:
        return await asyncpg.connect(normalize_asyncpg_url(_dsn()))  # type: ignore[arg-type]
    except Exception:
        pytest.skip("requires a live PostgreSQL with the v2 schema")


def test_migration_is_additive_parameter_safe_and_documents_legacy_rows() -> None:
    sql = _MIGRATION.read_text()
    assert "ADD COLUMN IF NOT EXISTS memory_profile_id" in sql
    assert "ADD COLUMN IF NOT EXISTS memory_profile_revision_id" in sql
    assert "legacy-unprofiled-v0" in sql
    assert "memory-context-v1" in sql
    assert "DEFERRABLE INITIALLY DEFERRED" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql


async def test_migration_idempotency_pair_fk_force_rls_and_tenant_delete() -> None:
    import asyncpg

    owner = await _owner()
    tenant_id, principal_id, profile_id, revision_id, recall_id = (
        uuid.uuid4() for _ in range(5)
    )
    try:
        # Reapplication is part of the migration contract.
        await owner.execute(_MIGRATION.read_text())
        await owner.execute(_MIGRATION.read_text())
        metadata = await owner.fetchrow(
            "SELECT is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'recall_logs' "
            "AND column_name = 'memory_context_version'"
        )
        assert metadata["is_nullable"] == "NO"
        assert "memory-context-v1" in metadata["column_default"]
        assert await owner.fetchval(
            "SELECT relforcerowsecurity FROM pg_class WHERE oid = 'recall_logs'::regclass"
        )

        await owner.execute(
            "INSERT INTO tenants (id, name, slug) VALUES ($1, 'context migration', $2)",
            tenant_id,
            f"context-migration-{tenant_id.hex[:10]}",
        )
        await owner.execute(
            "INSERT INTO principals (id, tenant_id, name, type) "
            "VALUES ($1, $2, 'admin', 'admin')",
            principal_id,
            tenant_id,
        )
        await owner.execute(
            "INSERT INTO memory_profiles "
            "(id, tenant_id, name, slug, created_by_principal_id) "
            "VALUES ($1, $2, 'audit', $3, $4)",
            profile_id,
            tenant_id,
            f"audit-{profile_id.hex[:10]}",
            principal_id,
        )
        await owner.execute(
            "INSERT INTO memory_profile_revisions "
            "(id, tenant_id, profile_id, version, created_by_principal_id, reason) "
            "VALUES ($1, $2, $3, 1, $4, 'audit')",
            revision_id,
            tenant_id,
            profile_id,
            principal_id,
        )
        await owner.execute(
            "UPDATE memory_profiles SET active_revision_id = $1 WHERE id = $2",
            revision_id,
            profile_id,
        )
        await owner.execute(
            "INSERT INTO recall_logs "
            "(id, tenant_id, principal_id, mode, memory_profile_id, "
            "memory_profile_revision_id, memory_context_version) "
            "VALUES ($1, $2, $3, 'startup', $4, $5, 'memory-context-v1')",
            recall_id,
            tenant_id,
            principal_id,
            profile_id,
            revision_id,
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await owner.execute(
                "INSERT INTO recall_logs "
                "(tenant_id, principal_id, mode, memory_profile_id, memory_context_version) "
                "VALUES ($1, $2, 'startup', $3, 'memory-context-v1')",
                tenant_id,
                principal_id,
                profile_id,
            )

        # Deferred NO ACTION does not obstruct the complete tenant cascade.
        async with owner.transaction():
            await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        assert not await owner.fetchval(
            "SELECT EXISTS(SELECT 1 FROM recall_logs WHERE id = $1)", recall_id
        )
    finally:
        await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await owner.close()
