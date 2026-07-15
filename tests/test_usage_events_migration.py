"""Migration and security tests for usage_events (ENG-METER-001).

Requires a live PostgreSQL with the v2 schema (migration 017 applied). Skips
automatically when no DB is reachable, matching the rest of the suite.

RLS/FORCE-RLS/cross-tenant coverage for usage_events is also asserted
generically in tests/test_rls_isolation.py (added to RLS_TABLES and
REPRESENTATIVE_RLS_TABLES there). This file covers the append-only posture
(no UPDATE/DELETE for the app role) and the migration-specific DDL contract
that isn't already exercised generically.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest


def _owner_dsn() -> str | None:
    return os.environ.get("ENGRAM_DATABASE_URL") or os.environ.get("ENGRAM_OWNER_DATABASE_URL")


def _app_dsn() -> str | None:
    return os.environ.get("ENGRAM_APP_DATABASE_URL")


async def _connect(url: str):
    import asyncpg

    from engram.migrations import normalize_asyncpg_url

    return await asyncpg.connect(normalize_asyncpg_url(url))


def _skip_if_no_rls_stack() -> None:
    if not _owner_dsn():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    if not _app_dsn():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")


@pytest.fixture
async def tenant_row():
    _skip_if_no_rls_stack()
    owner = await _connect(_owner_dsn())  # type: ignore[arg-type]
    tenant_id = str(uuid.uuid4())
    principal_id = str(uuid.uuid4())
    try:
        await owner.execute(
            "INSERT INTO tenants (id, name, slug) VALUES ($1, 'usage-mig-test', $2)",
            tenant_id,
            f"usage-mig-{tenant_id[:8]}",
        )
        await owner.execute(
            "INSERT INTO principals (id, tenant_id, name, type) VALUES ($1, $2, 'admin', 'admin')",
            principal_id,
            tenant_id,
        )
        yield {"tenant_id": tenant_id, "principal_id": principal_id}
    finally:
        await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await owner.close()


async def test_table_checks_and_indexes_exist():
    _skip_if_no_rls_stack()
    owner = await _connect(_owner_dsn())  # type: ignore[arg-type]
    try:
        exists = await owner.fetchval(
            "SELECT to_regclass('public.usage_events') IS NOT NULL"
        )
        assert exists

        indexes = {
            r["indexname"]
            for r in await owner.fetch(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'usage_events'"
            )
        }
        assert "idx_usage_events_tenant_created" in indexes
        assert "idx_usage_events_tenant_type_op_created" in indexes
        assert "idx_usage_events_tenant_principal_created" in indexes
        assert "idx_usage_events_dedupe" in indexes
        assert "idx_usage_events_candidate_outcome_resolution" in indexes
        assert "idx_usage_events_provider_class_created" in indexes

        checks = {
            r["conname"]
            for r in await owner.fetch(
                "SELECT conname FROM pg_constraint WHERE conrelid = 'usage_events'::regclass "
                "AND contype = 'c'"
            )
        }
        assert "chk_usage_events_input_count_nonneg" in checks
        assert "chk_usage_events_input_bytes_nonneg" in checks
        assert "chk_usage_events_prompt_tokens_nonneg" in checks
        assert "chk_usage_events_total_tokens_nonneg" in checks
        assert "chk_usage_events_latency_ms_nonneg" in checks
        assert "chk_usage_events_reported_cost_nonneg" in checks
        assert "chk_usage_events_provider_semantics" in checks
    finally:
        await owner.close()


async def test_provider_semantics_migration_is_idempotent():
    _skip_if_no_rls_stack()
    owner = await _connect(_owner_dsn())  # type: ignore[arg-type]
    sql = Path("migrations/019_provider_usage_semantics.sql").read_text()
    try:
        await owner.execute(sql)
        await owner.execute(sql)
    finally:
        await owner.close()


async def test_negative_values_rejected_by_check_constraints():
    _skip_if_no_rls_stack()
    owner = await _connect(_owner_dsn())  # type: ignore[arg-type]
    tenant_id = str(uuid.uuid4())
    try:
        await owner.execute(
            "INSERT INTO tenants (id, name, slug) VALUES ($1, 't', $2)",
            tenant_id,
            f"chk-{tenant_id[:8]}",
        )
        with pytest.raises(Exception):  # noqa: B017 - asyncpg raises a driver-specific error
            await owner.execute(
                "INSERT INTO usage_events (tenant_id, event_type, operation, status, input_bytes) "
                "VALUES ($1, 'provider.call', 'classification', 'succeeded', -1)",
                tenant_id,
            )
    finally:
        await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await owner.close()


async def test_provider_operation_semantics_constraints():
    _skip_if_no_rls_stack()
    owner = await _connect(_owner_dsn())  # type: ignore[arg-type]
    tenant_id = str(uuid.uuid4())
    try:
        await owner.execute(
            "INSERT INTO tenants (id, name, slug) VALUES ($1, 't', $2)",
            tenant_id,
            f"provider-sem-{tenant_id[:8]}",
        )
        with pytest.raises(Exception):  # noqa: B017
            await owner.execute(
                "INSERT INTO usage_events (tenant_id, event_type, operation, status, "
                "usage_class, external_call_attempted) VALUES "
                "($1, 'provider.call', 'classification', 'succeeded', 'request', false)",
                tenant_id,
            )
        with pytest.raises(Exception):  # noqa: B017
            await owner.execute(
                "INSERT INTO usage_events (tenant_id, event_type, operation, status, "
                "usage_class, external_call_attempted) VALUES "
                "($1, 'provider.call', 'classification', 'disabled', 'request', true)",
                tenant_id,
            )
        await owner.execute(
            "INSERT INTO usage_events (tenant_id, event_type, operation, status, "
            "usage_class, external_call_attempted) VALUES "
            "($1, 'provider.call', 'classification', 'failed', 'request', false)",
            tenant_id,
        )
    finally:
        await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await owner.close()


async def test_app_role_can_select_and_insert_own_tenant(tenant_row):
    app = await _connect(_app_dsn())  # type: ignore[arg-type]
    try:
        await app.execute(
            "SELECT set_config('app.tenant_id', $1, false)", tenant_row["tenant_id"]
        )
        await app.execute(
            "SELECT set_config('app.principal_id', $1, false)", tenant_row["principal_id"]
        )
        await app.execute(
            "INSERT INTO usage_events (tenant_id, principal_id, event_type, operation, status) "
            "VALUES ($1, $2, 'candidate.observed', 'process_memory_candidate', "
            "'accepted_for_processing')",
            tenant_row["tenant_id"],
            tenant_row["principal_id"],
        )
        count = await app.fetchval("SELECT count(*) FROM usage_events")
        assert count == 1
    finally:
        await app.close()


async def test_app_role_cannot_update_usage_events(tenant_row):
    owner = await _connect(_owner_dsn())  # type: ignore[arg-type]
    event_id = str(uuid.uuid4())
    await owner.execute(
        "INSERT INTO usage_events (id, tenant_id, principal_id, event_type, operation, status) "
        "VALUES ($1, $2, $3, 'candidate.observed', 'process_memory_candidate', "
        "'accepted_for_processing')",
        event_id,
        tenant_row["tenant_id"],
        tenant_row["principal_id"],
    )
    await owner.close()

    app = await _connect(_app_dsn())  # type: ignore[arg-type]
    try:
        await app.execute(
            "SELECT set_config('app.tenant_id', $1, false)", tenant_row["tenant_id"]
        )
        with pytest.raises(Exception):  # noqa: B017
            await app.execute(
                "UPDATE usage_events SET status = 'tampered' WHERE id = $1", event_id
            )
    finally:
        await app.close()


async def test_app_role_cannot_delete_usage_events(tenant_row):
    owner = await _connect(_owner_dsn())  # type: ignore[arg-type]
    event_id = str(uuid.uuid4())
    await owner.execute(
        "INSERT INTO usage_events (id, tenant_id, principal_id, event_type, operation, status) "
        "VALUES ($1, $2, $3, 'candidate.observed', 'process_memory_candidate', "
        "'accepted_for_processing')",
        event_id,
        tenant_row["tenant_id"],
        tenant_row["principal_id"],
    )
    await owner.close()

    app = await _connect(_app_dsn())  # type: ignore[arg-type]
    try:
        await app.execute(
            "SELECT set_config('app.tenant_id', $1, false)", tenant_row["tenant_id"]
        )
        with pytest.raises(Exception):  # noqa: B017
            await app.execute("DELETE FROM usage_events WHERE id = $1", event_id)
    finally:
        await app.close()


async def test_owner_can_report_across_tenants(tenant_row):
    """Owner/migration reporting bypasses RLS — used by `engram usage-report`."""
    owner = await _connect(_owner_dsn())  # type: ignore[arg-type]
    other_tenant = str(uuid.uuid4())
    try:
        await owner.execute(
            "INSERT INTO tenants (id, name, slug) VALUES ($1, 'other', $2)",
            other_tenant,
            f"other-{other_tenant[:8]}",
        )
        await owner.execute(
            "INSERT INTO usage_events (tenant_id, event_type, operation, status) "
            "VALUES ($1, 'candidate.observed', 'process_memory_candidate', "
            "'accepted_for_processing')",
            tenant_row["tenant_id"],
        )
        await owner.execute(
            "INSERT INTO usage_events (tenant_id, event_type, operation, status) "
            "VALUES ($1, 'candidate.observed', 'process_memory_candidate', "
            "'accepted_for_processing')",
            other_tenant,
        )
        count = await owner.fetchval(
            "SELECT count(*) FROM usage_events WHERE tenant_id = ANY($1::uuid[])",
            [tenant_row["tenant_id"], other_tenant],
        )
        assert count == 2
    finally:
        await owner.execute("DELETE FROM tenants WHERE id = $1", other_tenant)
        await owner.close()


async def test_metadata_insert_cannot_change_tenant_identity(tenant_row):
    """A caller-supplied tenant_id in the row must still be enforced by RLS —
    an app-role session scoped to tenant A cannot insert a usage_events row
    claiming tenant B, even by naming tenant B's id directly in the INSERT.
    """
    owner = await _connect(_owner_dsn())  # type: ignore[arg-type]
    other_tenant = str(uuid.uuid4())
    await owner.execute(
        "INSERT INTO tenants (id, name, slug) VALUES ($1, 'other', $2)",
        other_tenant,
        f"other-{other_tenant[:8]}",
    )
    await owner.close()

    app = await _connect(_app_dsn())  # type: ignore[arg-type]
    try:
        await app.execute(
            "SELECT set_config('app.tenant_id', $1, false)", tenant_row["tenant_id"]
        )
        await app.execute(
            "SELECT set_config('app.principal_id', $1, false)", tenant_row["principal_id"]
        )
        with pytest.raises(Exception):  # noqa: B017 - RLS WITH CHECK rejects the insert
            await app.execute(
                "INSERT INTO usage_events (tenant_id, event_type, operation, status) "
                "VALUES ($1, 'candidate.observed', 'process_memory_candidate', "
                "'accepted_for_processing')",
                other_tenant,
            )
    finally:
        await app.close()
        owner2 = await _connect(_owner_dsn())  # type: ignore[arg-type]
        await owner2.execute("DELETE FROM tenants WHERE id = $1", other_tenant)
        await owner2.close()
