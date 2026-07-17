from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from engram.migrations import normalize_asyncpg_url
from engram.models import RecallLog

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


def test_recall_log_omitted_value_fallback_is_legacy_unprofiled() -> None:
    column = RecallLog.__table__.c.memory_context_version
    assert column.default is not None
    assert column.default.arg == "legacy-unprofiled-v0"
    assert column.server_default is not None
    assert str(column.server_default.arg) == "'legacy-unprofiled-v0'"


async def test_migration_idempotency_pair_fk_force_rls_and_tenant_delete() -> None:
    import asyncpg

    owner = await _owner()
    (
        tenant_id,
        principal_id,
        other_tenant_id,
        other_principal_id,
        profile_id,
        revision_id,
        other_profile_id,
        other_revision_id,
        legacy_id,
        old_writer_id,
        explicit_unprofiled_id,
        profiled_id,
    ) = (uuid.uuid4() for _ in range(12))
    transaction = owner.transaction()
    transaction_started = False
    try:
        await transaction.start()
        transaction_started = True
        await owner.execute(
            "INSERT INTO tenants (id, name, slug) VALUES ($1, 'context migration', $2)",
            tenant_id,
            f"context-migration-{tenant_id.hex[:10]}",
        )
        await owner.execute(
            "INSERT INTO principals (id, tenant_id, name, type) VALUES ($1, $2, 'admin', 'admin')",
            principal_id,
            tenant_id,
        )
        await owner.execute(
            "INSERT INTO tenants (id, name, slug) VALUES ($1, 'other context migration', $2)",
            other_tenant_id,
            f"other-context-migration-{other_tenant_id.hex[:10]}",
        )
        await owner.execute(
            "INSERT INTO principals (id, tenant_id, name, type) VALUES ($1, $2, 'admin', 'admin')",
            other_principal_id,
            other_tenant_id,
        )

        # Simulate a row written before migration 023 added a non-null default.
        await owner.execute(
            "ALTER TABLE recall_logs "
            "ALTER COLUMN memory_context_version DROP NOT NULL, "
            "ALTER COLUMN memory_context_version DROP DEFAULT"
        )
        await owner.execute(
            "INSERT INTO recall_logs (id, tenant_id, principal_id, mode) "
            "VALUES ($1, $2, $3, 'startup')",
            legacy_id,
            tenant_id,
            principal_id,
        )
        assert (
            await owner.fetchval(
                "SELECT memory_context_version FROM recall_logs WHERE id = $1", legacy_id
            )
            is None
        )
        # Flush the existing deferred FK trigger before migration 023 alters
        # the same table, as PostgreSQL forbids ALTER TABLE with pending events.
        await owner.execute("SET CONSTRAINTS ALL IMMEDIATE")

        # Reapplication is part of the migration contract.
        await owner.execute(_MIGRATION.read_text())
        await owner.execute(_MIGRATION.read_text())
        await owner.execute("SET CONSTRAINTS ALL DEFERRED")
        metadata = await owner.fetchrow(
            "SELECT is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'recall_logs' "
            "AND column_name = 'memory_context_version'"
        )
        assert metadata["is_nullable"] == "NO"
        assert "legacy-unprofiled-v0" in metadata["column_default"]
        legacy = await owner.fetchrow(
            "SELECT memory_profile_id, memory_profile_revision_id, memory_context_version "
            "FROM recall_logs WHERE id = $1",
            legacy_id,
        )
        assert legacy["memory_profile_id"] is None
        assert legacy["memory_profile_revision_id"] is None
        assert legacy["memory_context_version"] == "legacy-unprofiled-v0"
        assert await owner.fetchval(
            "SELECT relforcerowsecurity FROM pg_class WHERE oid = 'recall_logs'::regclass"
        )
        assert await owner.fetchval(
            "SELECT has_table_privilege('engram_app', 'recall_logs', 'SELECT,INSERT,UPDATE,DELETE')"
        )

        # A mixed-version writer that omits all provenance must fail safe.
        await owner.execute(
            "INSERT INTO recall_logs (id, tenant_id, principal_id, mode) "
            "VALUES ($1, $2, $3, 'startup')",
            old_writer_id,
            tenant_id,
            principal_id,
        )
        old_writer = await owner.fetchrow(
            "SELECT memory_profile_id, memory_profile_revision_id, memory_context_version "
            "FROM recall_logs WHERE id = $1",
            old_writer_id,
        )
        assert old_writer["memory_profile_id"] is None
        assert old_writer["memory_profile_revision_id"] is None
        assert old_writer["memory_context_version"] == "legacy-unprofiled-v0"
        assert old_writer["memory_context_version"] != "memory-context-v1"

        # New 002B paths explicitly attest v1; migration reapplication must not relabel them.
        await owner.execute(
            "INSERT INTO recall_logs "
            "(id, tenant_id, principal_id, mode, memory_context_version) "
            "VALUES ($1, $2, $3, 'semantic', 'memory-context-v1')",
            explicit_unprofiled_id,
            tenant_id,
            principal_id,
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
            profiled_id,
            tenant_id,
            principal_id,
            profile_id,
            revision_id,
        )

        with pytest.raises(asyncpg.CheckViolationError):
            async with owner.transaction():
                await owner.execute(
                    "INSERT INTO recall_logs "
                    "(tenant_id, principal_id, mode, memory_profile_id, "
                    "memory_context_version) "
                    "VALUES ($1, $2, 'startup', $3, 'memory-context-v1')",
                    tenant_id,
                    principal_id,
                    profile_id,
                )

        # The composite FK must reject a revision/profile from another tenant.
        await owner.execute(
            "INSERT INTO memory_profiles "
            "(id, tenant_id, name, slug, created_by_principal_id) "
            "VALUES ($1, $2, 'other audit', $3, $4)",
            other_profile_id,
            other_tenant_id,
            f"other-audit-{other_profile_id.hex[:10]}",
            other_principal_id,
        )
        await owner.execute(
            "INSERT INTO memory_profile_revisions "
            "(id, tenant_id, profile_id, version, created_by_principal_id, reason) "
            "VALUES ($1, $2, $3, 1, $4, 'other audit')",
            other_revision_id,
            other_tenant_id,
            other_profile_id,
            other_principal_id,
        )
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            async with owner.transaction():
                await owner.execute(
                    "INSERT INTO recall_logs "
                    "(tenant_id, principal_id, mode, memory_profile_id, "
                    "memory_profile_revision_id, memory_context_version) "
                    "VALUES ($1, $2, 'startup', $3, $4, 'memory-context-v1')",
                    tenant_id,
                    principal_id,
                    other_profile_id,
                    other_revision_id,
                )
                await owner.execute(
                    "SET CONSTRAINTS fk_recall_logs_memory_profile_revision IMMEDIATE"
                )

        # Repair the unsafe default from an earlier development application.
        await owner.execute("SET CONSTRAINTS ALL IMMEDIATE")
        await owner.execute(
            "ALTER TABLE recall_logs ALTER COLUMN memory_context_version "
            "SET DEFAULT 'memory-context-v1'"
        )
        await owner.execute(_MIGRATION.read_text())
        await owner.execute("SET CONSTRAINTS ALL DEFERRED")
        repaired_default = await owner.fetchval(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'recall_logs' "
            "AND column_name = 'memory_context_version'"
        )
        assert "legacy-unprofiled-v0" in repaired_default
        assert (
            await owner.fetchval(
                "SELECT memory_context_version FROM recall_logs WHERE id = $1",
                explicit_unprofiled_id,
            )
            == "memory-context-v1"
        )

        # Deferred NO ACTION does not obstruct the complete tenant cascade.
        async with owner.transaction():
            await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
            await owner.execute("SET CONSTRAINTS ALL IMMEDIATE")
        assert not await owner.fetchval(
            "SELECT EXISTS(SELECT 1 FROM recall_logs WHERE id = $1)", profiled_id
        )
    finally:
        if transaction_started:
            await transaction.rollback()
        await owner.close()
