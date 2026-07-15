"""PostgreSQL migration and RLS proof for server-owned candidate ingests."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from engram.migrations import normalize_asyncpg_url


def _owner_dsn() -> str | None:
    return os.environ.get("ENGRAM_OWNER_DATABASE_URL") or os.environ.get("ENGRAM_DATABASE_URL")


def _app_dsn() -> str | None:
    return os.environ.get("ENGRAM_APP_DATABASE_URL")


async def _connect(url: str):
    import asyncpg

    return await asyncpg.connect(normalize_asyncpg_url(url))


def _require_stack() -> None:
    if not _owner_dsn() or not _app_dsn():
        pytest.skip("requires owner and app PostgreSQL URLs")


async def test_migration_020_is_idempotent_and_has_security_contract() -> None:
    _require_stack()
    owner = await _connect(_owner_dsn())  # type: ignore[arg-type]
    try:
        sql = Path("migrations/020_candidate_ingests.sql").read_text()
        await owner.execute(sql)
        await owner.execute(sql)
        security = await owner.fetchrow(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE oid = 'candidate_ingests'::regclass"
        )
        assert security is not None
        assert security["relrowsecurity"] is True
        assert security["relforcerowsecurity"] is True
        indexes = {
            row["indexname"]
            for row in await owner.fetch(
                "SELECT indexname FROM pg_indexes WHERE tablename IN "
                "('candidate_ingests', 'usage_events', 'classification_runs')"
            )
        }
        assert {
            "idx_candidate_ingests_tenant_created",
            "idx_candidate_ingests_tenant_principal_created",
            "idx_usage_events_tenant_ingest_type_created",
            "idx_classification_runs_ingest",
        } <= indexes
        assert await owner.fetchval(
            "SELECT has_table_privilege('engram_app', 'candidate_ingests', 'SELECT, INSERT')"
        )
        assert not await owner.fetchval(
            "SELECT has_table_privilege('engram_app', 'candidate_ingests', 'UPDATE')"
        )
        assert not await owner.fetchval(
            "SELECT has_table_privilege('engram_app', 'candidate_ingests', 'DELETE')"
        )
    finally:
        await owner.close()


async def test_candidate_ingest_rls_immutability_and_references() -> None:
    _require_stack()
    owner = await _connect(_owner_dsn())  # type: ignore[arg-type]
    app = await _connect(_app_dsn())  # type: ignore[arg-type]
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    principal_a, principal_b = uuid.uuid4(), uuid.uuid4()
    ingest_a, ingest_b = uuid.uuid4(), uuid.uuid4()
    try:
        for tenant, principal, suffix in (
            (tenant_a, principal_a, "a"),
            (tenant_b, principal_b, "b"),
        ):
            await owner.execute(
                "INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $3)",
                tenant,
                f"ingest-{suffix}",
                f"ingest-{suffix}-{tenant.hex[:8]}",
            )
            await owner.execute(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES ($1, $2, 'admin', 'admin')",
                principal,
                tenant,
            )
        await owner.execute(
            "INSERT INTO candidate_ingests "
            "(id, tenant_id, principal_id, source_type, content_hash) "
            "VALUES ($1, $2, $3, 'manual', 'hash-a'), "
            "($4, $5, $6, 'manual', 'hash-b')",
            ingest_a,
            tenant_a,
            principal_a,
            ingest_b,
            tenant_b,
            principal_b,
        )
        await app.execute("SELECT set_config('app.tenant_id', $1, false)", str(tenant_a))
        await app.execute("SELECT set_config('app.principal_id', $1, false)", str(principal_a))
        assert await app.fetchval("SELECT count(*) FROM candidate_ingests") == 1
        assert await app.fetchval(
            "SELECT count(*) FROM candidate_ingests WHERE id = $1", ingest_b
        ) == 0
        with pytest.raises(Exception):  # noqa: B017
            await app.execute(
                "INSERT INTO candidate_ingests "
                "(tenant_id, principal_id, source_type, content_hash) "
                "VALUES ($1, $2, 'manual', 'cross-tenant')",
                tenant_b,
                principal_b,
            )
        with pytest.raises(Exception):  # noqa: B017
            await app.execute(
                "INSERT INTO candidate_ingests "
                "(tenant_id, principal_id, source_type, content_hash) "
                "VALUES ($1, $2, 'manual', 'cross-principal')",
                tenant_a,
                principal_b,
            )
        with pytest.raises(Exception):  # noqa: B017
            await app.execute(
                "UPDATE candidate_ingests SET source_type = 'import' WHERE id = $1", ingest_a
            )
        with pytest.raises(Exception):  # noqa: B017
            await app.execute("DELETE FROM candidate_ingests WHERE id = $1", ingest_a)
        await owner.execute(
            "INSERT INTO usage_events "
            "(tenant_id, principal_id, ingest_id, event_type, operation, status) "
            "VALUES ($1, $2, $3, 'candidate.observed', "
            "'process_memory_candidate', 'accepted_for_processing')",
            tenant_a,
            principal_a,
            ingest_a,
        )
        with pytest.raises(Exception):  # noqa: B017
            await owner.execute(
                "INSERT INTO usage_events "
                "(tenant_id, principal_id, ingest_id, event_type, operation, status) "
                "VALUES ($1, $2, $3, 'candidate.observed', "
                "'process_memory_candidate', 'accepted_for_processing')",
                tenant_a,
                principal_a,
                ingest_b,
            )
    finally:
        await app.close()
        await owner.execute("DELETE FROM tenants WHERE id = ANY($1::uuid[])", [tenant_a, tenant_b])
        await owner.close()
