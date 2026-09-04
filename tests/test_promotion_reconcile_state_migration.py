"""Real-PostgreSQL migration, RLS, privilege, and downgrade proof for
``promotion_reconcile_state``, terminal suppression, global tenant scheduling,
and their bounded indexes
(migration 034, ENG-PROMOTION-003B4 / issue #155).

These tests connect as the non-owner ``engram_app`` role to prove, against
real PostgreSQL, that the reconciliation backstop's scheduler-only state is:

- tenant isolated (FORCE RLS; another tenant can neither read nor move this
  tenant's cursor/epoch/diagnostics);
- least-privilege (SELECT/INSERT/UPDATE only; DELETE denied);
- protected by a downgrade that refuses to run while pending/running
  ``promotion.reconcile`` chain work still depends on the cursor position.

They skip without the Compose real-PostgreSQL stack (see ``make compose-ci``).
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import pytest

_DB_SKIP_REASON = "requires a live PostgreSQL with the v2 schema"


def _owner_dsn() -> str | None:
    return os.environ.get("ENGRAM_DATABASE_URL") or os.environ.get(
        "ENGRAM_OWNER_DATABASE_URL"
    )


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


async def _owner_with_034() -> Any:
    _skip_if_no_stack()
    owner = await _connect(_owner_dsn())  # type: ignore[arg-type]
    if await owner.fetchval("SELECT to_regclass('promotion_reconcile_state')") is None:
        await owner.close()
        pytest.skip("requires migration 034")
    return owner


def _denied(exc: BaseException) -> bool:
    """True for a PostgreSQL privilege/RLS rejection."""
    import asyncpg

    if isinstance(exc, asyncpg.PostgresError):
        sqlstate = getattr(exc, "sqlstate", None) or ""
        return sqlstate in {"42501", "23000", "23514"}
    return False


async def _seed_tenant(owner: Any, *, tenant_id: uuid.UUID, label: str) -> None:
    await owner.execute(
        "INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $3)",
        tenant_id,
        label,
        f"{label.lower()}-{tenant_id.hex[:8]}",
    )


async def _insert_state(owner: Any, *, tenant_id: uuid.UUID) -> None:
    await owner.execute(
        "INSERT INTO promotion_reconcile_state "
        "(tenant_id, cursor_epoch, kind_policy_revision, last_wrapped) "
        "VALUES ($1, 1, 2, FALSE)",
        tenant_id,
    )


def _downgrade_sql() -> str:
    root = Path(__file__).resolve().parent.parent
    return (root / "migrations" / "downgrades" / "034_promotion_reconcile_backstop.sql").read_text()


# ─── Tests ─────────────────────────────────────────────────────────────


async def test_rls_enabled_and_forced() -> None:
    owner = await _owner_with_034()
    try:
        row = await owner.fetchrow(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = 'promotion_reconcile_state'"
        )
        assert row is not None
        assert row["relrowsecurity"] is True
        assert row["relforcerowsecurity"] is True
        policy = await owner.fetchval(
            "SELECT count(*) FROM pg_policies "
            "WHERE tablename = 'promotion_reconcile_state' "
            "AND policyname = 'tenant_isolation_promotion_reconcile_state'"
        )
        assert policy == 1
        terminal = await owner.fetchrow(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = 'promotion_reconcile_terminal'"
        )
        assert terminal is not None
        assert terminal["relrowsecurity"] is True
        assert terminal["relforcerowsecurity"] is True
    finally:
        await owner.close()


async def test_rotation_index_present() -> None:
    owner = await _owner_with_034()
    try:
        index = await owner.fetchrow(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'idx_memitems_proposed_rotation'"
        )
        assert index is not None
        definition = index["indexdef"]
        assert "memory_items" in definition
        assert "created_at" in definition
        assert "proposed" in definition
        job_index = await owner.fetchval(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'idx_jobs_reconcile_item_state'"
        )
        assert job_index is not None
        assert "memory_item_id" in job_index
        assert "run_after" in job_index
    finally:
        await owner.close()


async def test_cursor_columns_nullable_for_clean_resets() -> None:
    owner = await _owner_with_034()
    try:
        # NULL cursor = "next pass reads from the head" — the reset semantics
        # #164's NOT NULL cursor cannot express.
        tenant_id = uuid.uuid4()
        await _seed_tenant(owner, tenant_id=tenant_id, label="NullableCursor")
        await owner.execute(
            "INSERT INTO promotion_reconcile_state (tenant_id, last_wrapped) "
            "VALUES ($1, FALSE)",
            tenant_id,
        )
        row = await owner.fetchrow(
            "SELECT cursor_created_at, cursor_item_id, cursor_epoch "
            "FROM promotion_reconcile_state WHERE tenant_id = $1",
            tenant_id,
        )
        assert row["cursor_created_at"] is None
        assert row["cursor_item_id"] is None
        assert row["cursor_epoch"] == 0
        await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
    finally:
        await owner.close()


async def test_request_chain_state_is_tenant_scoped_and_content_free() -> None:
    owner = await _owner_with_034()
    try:
        tenant_id = uuid.uuid4()
        await _seed_tenant(owner, tenant_id=tenant_id, label="RequestChain")
        await owner.execute(
            "INSERT INTO promotion_reconcile_chains "
            "(tenant_id, reason, trigger_id, status) VALUES ($1, $2, $3, 'completed')",
            tenant_id,
            "provider_recovery",
            "request-1",
        )
        row = await owner.fetchrow(
            "SELECT cursor_created_at, cursor_item_id, status FROM promotion_reconcile_chains "
            "WHERE tenant_id = $1 AND reason = $2 AND trigger_id = $3",
            tenant_id,
            "provider_recovery",
            "request-1",
        )
        assert row["cursor_created_at"] is None
        assert row["cursor_item_id"] is None
        assert row["status"] == "completed"
        assert await owner.fetchval(
            "SELECT relforcerowsecurity FROM pg_class "
            "WHERE relname = 'promotion_reconcile_chains'"
        ) is True
        await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
    finally:
        await owner.close()


async def test_app_role_privileges_least_privilege() -> None:
    owner = await _owner_with_034()
    app = await _connect(_app_dsn())  # type: ignore[arg-type]
    tenant_id = uuid.uuid4()
    try:
        await _seed_tenant(owner, tenant_id=tenant_id, label="Privs")
        await app.execute("SELECT set_config('app.tenant_id', $1, false)", str(tenant_id))
        # INSERT/SELECT/UPDATE allowed under RLS.
        await app.execute(
            "INSERT INTO promotion_reconcile_state "
            "(tenant_id, cursor_epoch, kind_policy_revision, last_wrapped) "
            "VALUES ($1, 0, 0, FALSE)",
            tenant_id,
        )
        await app.execute(
            "UPDATE promotion_reconcile_state "
            "SET cursor_created_at = now(), cursor_item_id = $1, "
            "cursor_epoch = cursor_epoch + 1 WHERE tenant_id = $2",
            uuid.uuid4(),
            tenant_id,
        )
        epoch = await app.fetchval(
            "SELECT cursor_epoch FROM promotion_reconcile_state WHERE tenant_id = $1",
            tenant_id,
        )
        assert epoch == 1
        # DELETE denied: scheduler bookkeeping is not app-erasable.
        with pytest.raises(BaseException) as excinfo:
            await app.execute(
                "DELETE FROM promotion_reconcile_state WHERE tenant_id = $1", tenant_id
            )
        assert _denied(excinfo.value)
        assert await owner.fetchval(
            "SELECT has_table_privilege('engram_app', "
            "'promotion_reconcile_scheduler_state', 'SELECT')"
        ) is False
        assert await owner.fetchval(
            "SELECT has_table_privilege('engram_app', "
            "'promotion_reconcile_scheduler_state', 'INSERT')"
        ) is False
    finally:
        await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await owner.close()
        await app.close()


async def test_tenant_isolation_of_reconcile_state() -> None:
    owner = await _owner_with_034()
    app = await _connect(_app_dsn())  # type: ignore[arg-type]
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    try:
        await _seed_tenant(owner, tenant_id=tenant_a, label="ReconA")
        await _seed_tenant(owner, tenant_id=tenant_b, label="ReconB")
        await _insert_state(owner, tenant_id=tenant_a)
        await _insert_state(owner, tenant_id=tenant_b)

        await app.execute("SELECT set_config('app.tenant_id', $1, false)", str(tenant_a))
        rows = await app.fetch(
            "SELECT tenant_id FROM promotion_reconcile_state"
        )
        assert [row["tenant_id"] for row in rows] == [tenant_a]

        # Tenant A cannot move tenant B's epoch or read its diagnostics.
        updated = await app.execute(
            "UPDATE promotion_reconcile_state SET cursor_epoch = 99 "
            "WHERE tenant_id = $1",
            tenant_b,
        )
        assert updated == "UPDATE 0"
        assert (
            await app.fetchval(
                "SELECT kind_policy_revision FROM promotion_reconcile_state "
                "WHERE tenant_id = $1",
                tenant_b,
            )
            is None
        )

        with pytest.raises(BaseException) as excinfo:
            await app.execute(
                "INSERT INTO promotion_reconcile_state (tenant_id) VALUES ($1)",
                tenant_b,
            )
        assert _denied(excinfo.value)

        # A session-wide empty GUC (indistinguishable from missing for the
        # two-argument current_setting) exposes zero rows.
        await app.execute("SELECT set_config('app.tenant_id', '', false)")
        assert await app.fetch("SELECT * FROM promotion_reconcile_state") == []
    finally:
        await owner.execute("DELETE FROM tenants WHERE id IN ($1, $2)", tenant_a, tenant_b)
        await owner.close()
        await app.close()


async def test_downgrade_fails_safe_with_live_reconcile_work() -> None:
    """The downgrade refuses to drop the state while chain work is live.

    A pending/running ``promotion.reconcile`` job's bounded continuation
    depends on the cursor position; discarding it mid-chain could strand or
    duplicate coverage. Dead/succeeded history does not block the downgrade.
    """
    owner = await _owner_with_034()
    tenant_id = uuid.uuid4()
    try:
        await _seed_tenant(owner, tenant_id=tenant_id, label="DowngradeGuard")
        # The guard is global over live reconciliation work: own the queue
        # state for the duration of this test.
        await owner.execute("DELETE FROM jobs WHERE job_type = 'promotion.reconcile'")
        job_id = uuid.uuid4()
        await owner.execute(
            "INSERT INTO jobs (id, tenant_id, job_type, status, priority, "
            "run_after, attempts, max_attempts, payload) VALUES ("
            "$1, $2, 'promotion.reconcile', 'pending', 100, now(), 0, 5, "
            "'{\"reason\": \"backstop\"}'::jsonb)",
            job_id,
            tenant_id,
        )
        with pytest.raises(BaseException) as excinfo:
            await owner.execute(_downgrade_sql())
        # 55006 = object_in_use (the downgrade's fail-safe guard).
        assert getattr(excinfo.value, "sqlstate", None) in {"55006", "P0001"}
        # The state table survived.
        assert await owner.fetchval("SELECT to_regclass('promotion_reconcile_state')") == (
            "promotion_reconcile_state"
        )
        # With the chain work gone, the same script succeeds deterministically
        # (and migration 034 can be re-applied to restore the substrate).
        await owner.execute("DELETE FROM jobs WHERE id = $1", job_id)
        await owner.execute(_downgrade_sql())
        assert await owner.fetchval("SELECT to_regclass('promotion_reconcile_state')") is None
        assert await owner.fetchval("SELECT to_regclass('idx_memitems_proposed_rotation')") is None
        with open(
            Path(__file__).resolve().parent.parent
            / "migrations"
            / "034_promotion_reconcile_backstop.sql"
        ) as migration_file:
            await owner.execute(migration_file.read())
        assert await owner.fetchval(
            "SELECT to_regclass('promotion_reconcile_state')"
        ) == "promotion_reconcile_state"
    finally:
        await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await owner.close()
