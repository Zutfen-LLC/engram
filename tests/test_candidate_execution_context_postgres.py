"""Real-PostgreSQL RLS, privilege, and relational-integrity proof for
``candidate_ingest_executions`` (migration 025).

These tests connect as the non-owner ``engram_app`` role to prove, against real
PostgreSQL, that execution-context provenance is tenant-isolated and
least-privilege (SELECT/INSERT only), with tenant-safe composite foreign keys.
They skip without the Compose real-PostgreSQL stack (see ``make compose-ci``).
"""

from __future__ import annotations

import contextlib
import os
import uuid
from typing import Any

import pytest

# asyncpg is imported lazily so the module imports without a live DB.


def _denied(exc: BaseException) -> bool:
    """True for a PostgreSQL privilege/RLS rejection (42501 or an RLS violation)."""
    import asyncpg

    if isinstance(exc, asyncpg.PostgresError):
        sqlstate = getattr(exc, "sqlstate", None) or ""
        # 42501 insufficient_privilege; 23000/23514 for RLS WITH CHECK violations.
        return sqlstate in {"42501", "23000", "23514"}
    return False


def _owner_dsn() -> str | None:
    return os.environ.get("ENGRAM_DATABASE_URL") or os.environ.get("ENGRAM_OWNER_DATABASE_URL")


def _app_dsn() -> str | None:
    return os.environ.get("ENGRAM_APP_DATABASE_URL")


async def _connect(url: str) -> Any:
    import asyncpg

    from engram.migrations import normalize_asyncpg_url

    return await asyncpg.connect(normalize_asyncpg_url(url))


def _skip_if_no_stack() -> None:
    if not _owner_dsn():
        pytest.skip("requires ENGRAM_DATABASE_URL (owner) for setup")
    if not _app_dsn():
        pytest.skip("requires ENGRAM_APP_DATABASE_URL (non-owner app role)")


async def _owner_with_025() -> Any:
    _skip_if_no_stack()
    owner = await _connect(_owner_dsn())  # type: ignore[arg-type]
    if await owner.fetchval("SELECT to_regclass('candidate_ingest_executions')") is None:
        await owner.close()
        pytest.skip("requires migration 025")
    return owner


async def test_rls_enabled_and_forced() -> None:
    owner = await _owner_with_025()
    try:
        row = await owner.fetchrow(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = 'candidate_ingest_executions'"
        )
        assert row is not None
        assert row["relrowsecurity"] is True
        assert row["relforcerowsecurity"] is True
    finally:
        await owner.close()


async def test_app_role_has_no_bypassrls_and_owns_no_table() -> None:
    owner = await _owner_with_025()
    try:
        role = await owner.fetchrow(
            "SELECT rolbypassrls, rolsuper FROM pg_roles WHERE rolname = 'engram_app'"
        )
        assert role is not None
        assert role["rolbypassrls"] is False
        assert role["rolsuper"] is False
        # The app role must not own the table (owner bypass requires ownership).
        owner_role = await owner.fetchval(
            "SELECT tableowner FROM pg_tables WHERE tablename = 'candidate_ingest_executions'"
        )
        assert owner_role != "engram_app"
    finally:
        await owner.close()


async def test_app_role_can_select_and_insert_but_not_update_or_delete() -> None:
    owner = await _owner_with_025()
    app = await _connect(_app_dsn())  # type: ignore[arg-type]
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    ingest_id = uuid.uuid4()
    inserted = False
    try:
        await owner.execute(
            "INSERT INTO tenants (id, name, slug) VALUES ($1, 'rls-proof', 'rls-proof')",
            tenant_id,
        )
        await owner.execute(
            "INSERT INTO principals (id, tenant_id, name, type) "
            "VALUES ($1, $2, 'admin', 'admin')",
            principal_id,
            tenant_id,
        )
        await owner.execute(
            "INSERT INTO tenant_config (tenant_id, config_version, active) "
            "VALUES ($1, 'v1', TRUE)",
            tenant_id,
        )
        await owner.execute(
            "INSERT INTO candidate_ingests (id, tenant_id, principal_id, "
            "source_type, content_hash, memory_context_version) "
            "VALUES ($1, $2, $3, 'manual', 'sha256:rls-proof', 'legacy-unprofiled-v0')",
            ingest_id,
            tenant_id,
            principal_id,
        )

        await app.execute("SELECT set_config('app.tenant_id', $1, false)", str(tenant_id))

        # Same-tenant INSERT succeeds. The execution row is 1:1 with its ingest
        # (ingest_id is both PK and FK to candidate_ingests.id).
        await app.execute(
            "INSERT INTO candidate_ingest_executions "
            "(ingest_id, tenant_id, memory_context_version) "
            "VALUES ($1, $2, 'legacy-unprofiled-v0')",
            ingest_id,
            tenant_id,
        )
        inserted = True

        # Same-tenant SELECT sees the row.
        seen = await app.fetchval(
            "SELECT count(*) FROM candidate_ingest_executions WHERE ingest_id = $1", ingest_id
        )
        assert seen == 1

        # UPDATE must be denied: app role lacks the privilege.
        import asyncpg

        with pytest.raises(asyncpg.PostgresError) as exc_info:
            await app.execute(
                "UPDATE candidate_ingest_executions SET memory_context_version = 'x' "
                "WHERE ingest_id = $1",
                ingest_id,
            )
        assert _denied(exc_info.value)

        # DELETE must be denied: app role lacks the privilege.
        with pytest.raises(asyncpg.PostgresError) as exc_info:
            await app.execute(
                "DELETE FROM candidate_ingest_executions WHERE ingest_id = $1", ingest_id
            )
        assert _denied(exc_info.value)
    finally:
        if inserted:
            with contextlib.suppress(Exception):
                await owner.execute(
                    "DELETE FROM candidate_ingest_executions WHERE ingest_id = $1", ingest_id
                )
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM candidate_ingests WHERE id = $1", ingest_id)
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await app.close()
        await owner.close()


async def test_cross_tenant_select_and_insert_blocked() -> None:
    owner = await _owner_with_025()
    app = await _connect(_app_dsn())  # type: ignore[arg-type]
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    principal_a = uuid.uuid4()
    principal_b = uuid.uuid4()
    ingest_a = uuid.uuid4()
    ingest_b = uuid.uuid4()
    try:
        for tid, pid in ((tenant_a, principal_a), (tenant_b, principal_b)):
            await owner.execute(
                "INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $3)",
                tid,
                f"tenant-{tid}",
                f"tenant-{tid}",
            )
            await owner.execute(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES ($1, $2, 'admin', 'admin')",
                pid,
                tid,
            )
            await owner.execute(
                "INSERT INTO tenant_config (tenant_id, config_version, active) "
                "VALUES ($1, 'v1', TRUE)",
                tid,
            )
        for tid, iid, pid in (
            (tenant_a, ingest_a, principal_a),
            (tenant_b, ingest_b, principal_b),
        ):
            await owner.execute(
                "INSERT INTO candidate_ingests (id, tenant_id, principal_id, "
                "source_type, content_hash, memory_context_version) "
                "VALUES ($1, $2, $3, 'manual', 'sha256:cross', 'legacy-unprofiled-v0')",
                iid,
                tid,
                pid,
            )
        await owner.execute(
            "INSERT INTO candidate_ingest_executions "
            "(ingest_id, tenant_id, memory_context_version) "
            "VALUES ($1, $2, 'legacy-unprofiled-v0')",
            ingest_b,
            tenant_b,
        )

        # Scoped to tenant A: tenant B's execution row is invisible.
        await app.execute("SELECT set_config('app.tenant_id', $1, false)", str(tenant_a))
        assert (
            await app.fetchval(
                "SELECT count(*) FROM candidate_ingest_executions WHERE ingest_id = $1", ingest_b
            )
            == 0
        )

        # Cross-tenant INSERT (tenant A context, tenant B row) rejected by the
        # RLS WITH CHECK before any row lands. Uses the valid tenant-B ingest so
        # only RLS — not an FK shape error — is the rejection cause.
        import asyncpg

        with pytest.raises(asyncpg.PostgresError) as exc_info:
            await app.execute(
                "INSERT INTO candidate_ingest_executions "
                "(ingest_id, tenant_id, memory_context_version) "
                "VALUES ($1, $2, 'legacy-unprofiled-v0')",
                ingest_b,
                tenant_b,
            )
        assert _denied(exc_info.value)
        # The rejected row did not land (still exactly the owner-created one).
        assert (
            await owner.fetchval(
                "SELECT count(*) FROM candidate_ingest_executions WHERE ingest_id = $1",
                ingest_b,
            )
            == 1
        )
    finally:
        with contextlib.suppress(Exception):
            await owner.execute(
                "DELETE FROM candidate_ingest_executions WHERE ingest_id = $1", ingest_b
            )
        with contextlib.suppress(Exception):
            await owner.execute(
                "DELETE FROM candidate_ingests WHERE id = ANY($1::uuid[])",
                [ingest_a, ingest_b],
            )
        with contextlib.suppress(Exception):
            await owner.execute(
                "DELETE FROM tenants WHERE id = ANY($1::uuid[])", [tenant_a, tenant_b]
            )
        await app.close()
        await owner.close()


async def test_missing_tenant_context_leaks_nothing() -> None:
    owner = await _owner_with_025()
    app = await _connect(_app_dsn())  # type: ignore[arg-type]
    try:
        # No app.tenant_id set at all -> policy must filter to zero rows, not raise.
        total = await app.fetchval("SELECT count(*) FROM candidate_ingest_executions")
        assert total == 0
    finally:
        await app.close()
        await owner.close()


async def test_relational_integrity_tenant_safe_foreign_keys() -> None:
    owner = await _owner_with_025()
    try:
        # The tenant FK and the composite ingest FK exist. The principal is not
        # stored on the execution row (it is derived from the ingest), so there
        # is no principal FK here.
        fks = {
            row["conname"]
            for row in await owner.fetch(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'candidate_ingest_executions'::regclass AND contype = 'f'"
            )
        }
        assert "fk_candidate_ingest_executions_tenant" in fks
        assert "fk_candidate_ingest_executions_ingest" in fks
        assert "fk_candidate_ingest_executions_api_key" in fks
        assert "fk_candidate_ingest_executions_profile_revision" in fks
        # No principal FK or legacy principal column remains.
        assert "fk_candidate_ingest_executions_tenant_principal" not in fks
        assert "candidate_ingest_executions_principal_id_fkey" not in fks
        columns = {
            row["column_name"]
            for row in await owner.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'candidate_ingest_executions'"
            )
        }
        assert "principal_id" not in columns

        # profile-pair CHECK: both null or both set.
        check = await owner.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'candidate_ingest_executions'::regclass "
            "AND conname = 'chk_candidate_ingest_executions_profile_pair'"
        )
        assert check is not None
        assert "memory_profile_id IS NULL" in check

        # One ingest -> at most one execution row (PRIMARY KEY on ingest_id).
        pk = await owner.fetchval(
            "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid "
            "WHERE c.relname = 'candidate_ingest_executions' AND i.indisprimary"
        )
        assert pk == 1
    finally:
        await owner.close()


async def test_api_key_deletion_nulls_only_api_key_id() -> None:
    owner = await _owner_with_025()
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    ingest_id = uuid.uuid4()
    api_key_id = uuid.uuid4()
    try:
        await owner.execute(
            "INSERT INTO tenants (id, name, slug) VALUES ($1, 'delkey', 'delkey')", tenant_id
        )
        await owner.execute(
            "INSERT INTO principals (id, tenant_id, name, type) "
            "VALUES ($1, $2, 'admin', 'admin')",
            principal_id,
            tenant_id,
        )
        await owner.execute(
            "INSERT INTO tenant_config (tenant_id, config_version, active) "
            "VALUES ($1, 'v1', TRUE)",
            tenant_id,
        )
        await owner.execute(
            "INSERT INTO api_keys (id, tenant_id, principal_id, scopes, key_hash) "
            "VALUES ($1, $2, $3, ARRAY['admin']::text[], 'delkey-hash')",
            api_key_id,
            tenant_id,
            principal_id,
        )
        await owner.execute(
            "INSERT INTO candidate_ingests (id, tenant_id, principal_id, "
            "source_type, content_hash, memory_context_version) "
            "VALUES ($1, $2, $3, 'manual', 'sha256:delkey', 'memory-context-v2')",
            ingest_id,
            tenant_id,
            principal_id,
        )
        await owner.execute(
            "INSERT INTO candidate_ingest_executions "
            "(ingest_id, tenant_id, api_key_id, memory_context_version) "
            "VALUES ($1, $2, $3, 'memory-context-v2')",
            ingest_id,
            tenant_id,
            api_key_id,
        )
        # Deleting the API key nulls only api_key_id; the execution row survives.
        await owner.execute("DELETE FROM api_keys WHERE id = $1", api_key_id)
        row = await owner.fetchrow(
            "SELECT api_key_id, tenant_id, ingest_id "
            "FROM candidate_ingest_executions WHERE ingest_id = $1",
            ingest_id,
        )
        assert row is not None
        assert row["api_key_id"] is None
        assert row["tenant_id"] == tenant_id
        assert row["ingest_id"] == ingest_id
    finally:
        with contextlib.suppress(Exception):
            await owner.execute(
                "DELETE FROM candidate_ingest_executions WHERE ingest_id = $1", ingest_id
            )
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM candidate_ingests WHERE id = $1", ingest_id)
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await owner.close()


async def test_candidate_ingest_deletion_cascades_execution_row() -> None:
    owner = await _owner_with_025()
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    ingest_id = uuid.uuid4()
    try:
        await owner.execute(
            "INSERT INTO tenants (id, name, slug) VALUES ($1, 'cascade', 'cascade')", tenant_id
        )
        await owner.execute(
            "INSERT INTO principals (id, tenant_id, name, type) "
            "VALUES ($1, $2, 'admin', 'admin')",
            principal_id,
            tenant_id,
        )
        await owner.execute(
            "INSERT INTO tenant_config (tenant_id, config_version, active) "
            "VALUES ($1, 'v1', TRUE)",
            tenant_id,
        )
        await owner.execute(
            "INSERT INTO candidate_ingests (id, tenant_id, principal_id, "
            "source_type, content_hash, memory_context_version) "
            "VALUES ($1, $2, $3, 'manual', 'sha256:cascade', 'legacy-unprofiled-v0')",
            ingest_id,
            tenant_id,
            principal_id,
        )
        await owner.execute(
            "INSERT INTO candidate_ingest_executions "
            "(ingest_id, tenant_id, memory_context_version) "
            "VALUES ($1, $2, 'legacy-unprofiled-v0')",
            ingest_id,
            tenant_id,
        )
        assert (
            await owner.fetchval(
                "SELECT count(*) FROM candidate_ingest_executions WHERE ingest_id = $1", ingest_id
            )
            == 1
        )
        await owner.execute("DELETE FROM candidate_ingests WHERE id = $1", ingest_id)
        assert (
            await owner.fetchval(
                "SELECT count(*) FROM candidate_ingest_executions WHERE ingest_id = $1", ingest_id
            )
            == 0
        )
    finally:
        with contextlib.suppress(Exception):
            await owner.execute(
                "DELETE FROM candidate_ingest_executions WHERE ingest_id = $1", ingest_id
            )
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM candidate_ingests WHERE id = $1", ingest_id)
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await owner.close()
