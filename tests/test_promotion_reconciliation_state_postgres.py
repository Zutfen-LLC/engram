"""Real-PostgreSQL migration, RLS, and privilege proof for
``promotion_reconciliation_state`` (migration 032, ENG-PROMOTION-003B).

These tests connect as the non-owner ``engram_app`` role to prove, against
real PostgreSQL, that the persisted fair-rotation cursor for the bounded
startup promotion scan is:

- tenant isolated (FORCE RLS; a missing ``app.tenant_id`` GUC exposes zero
  rows; another tenant can neither read nor move this tenant's cursor);
- least-privilege (SELECT/INSERT/UPDATE only; DELETE denied);
- owned by the migration role, not the app role.

They skip without the Compose real-PostgreSQL stack (see ``make compose-ci``).
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
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


async def _owner_with_032() -> Any:
    _skip_if_no_stack()
    owner = await _connect(_owner_dsn())  # type: ignore[arg-type]
    if await owner.fetchval("SELECT to_regclass('promotion_reconciliation_state')") is None:
        await owner.close()
        pytest.skip("requires migration 032")
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


async def _owner_insert_cursor(owner: Any, *, tenant_id: uuid.UUID) -> uuid.UUID:
    item_id = uuid.uuid4()
    await owner.execute(
        "INSERT INTO promotion_reconciliation_state "
        "(tenant_id, cursor_created_at, cursor_item_id) "
        "VALUES ($1, $2, $3)",
        tenant_id,
        datetime.now(UTC),
        item_id,
    )
    return item_id


def _clear_tenant(owner: Any, tenant_id: uuid.UUID) -> Any:
    return owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)


# ─── Tests ─────────────────────────────────────────────────────────────


async def test_rls_enabled_and_forced() -> None:
    owner = await _owner_with_032()
    try:
        row = await owner.fetchrow(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = 'promotion_reconciliation_state'"
        )
        assert row is not None
        assert row["relrowsecurity"] is True
        assert row["relforcerowsecurity"] is True
        policy = await owner.fetchval(
            "SELECT count(*) FROM pg_policies "
            "WHERE tablename = 'promotion_reconciliation_state' "
            "AND policyname = 'tenant_isolation_promotion_reconciliation_state'"
        )
        assert policy == 1
    finally:
        await owner.close()


async def test_app_role_owns_no_table_and_has_no_bypassrls() -> None:
    owner = await _owner_with_032()
    try:
        role = await owner.fetchrow(
            "SELECT rolbypassrls, rolsuper FROM pg_roles WHERE rolname = 'engram_app'"
        )
        assert role is not None
        assert role["rolbypassrls"] is False
        assert role["rolsuper"] is False
        owner_role = await owner.fetchval(
            "SELECT tableowner FROM pg_tables "
            "WHERE tablename = 'promotion_reconciliation_state'"
        )
        assert owner_role != "engram_app"
    finally:
        await owner.close()


async def test_tenant_isolation_of_cursor() -> None:
    """The app role sees and advances only its own tenant's cursor row.

    Another tenant's cursor is invisible (reads and updates match zero
    rows), an insert naming another tenant is rejected by the RLS WITH
    CHECK, and a missing GUC exposes nothing.
    """
    owner = await _owner_with_032()
    app = await _connect(_app_dsn())  # type: ignore[arg-type]
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    try:
        await _seed_tenant(owner, tenant_id=tenant_a, label="CursorA")
        await _seed_tenant(owner, tenant_id=tenant_b, label="CursorB")
        cursor_a_item = await _owner_insert_cursor(owner, tenant_id=tenant_a)
        await _owner_insert_cursor(owner, tenant_id=tenant_b)

        # Scoped to tenant A: sees exactly its own cursor row.
        await app.execute("SELECT set_config('app.tenant_id', $1, false)", str(tenant_a))
        rows = await app.fetch("SELECT tenant_id, cursor_item_id "
                               "FROM promotion_reconciliation_state")
        assert len(rows) == 1
        assert rows[0]["tenant_id"] == tenant_a
        assert rows[0]["cursor_item_id"] == cursor_a_item

        # Advancing its own cursor is allowed.
        new_item = uuid.uuid4()
        await app.execute(
            "UPDATE promotion_reconciliation_state "
            "SET cursor_created_at = now(), cursor_item_id = $1 "
            "WHERE tenant_id = $2",
            new_item,
            tenant_a,
        )
        rows = await app.fetch("SELECT cursor_item_id FROM promotion_reconciliation_state")
        assert rows[0]["cursor_item_id"] == new_item

        # Tenant B's row is invisible: targeted read returns nothing and a
        # targeted update matches zero rows.
        assert await app.fetchval(
            "SELECT cursor_item_id FROM promotion_reconciliation_state "
            "WHERE tenant_id = $1",
            tenant_b,
        ) is None
        updated = await app.execute(
            "UPDATE promotion_reconciliation_state SET cursor_item_id = $1 "
            "WHERE tenant_id = $2",
            uuid.uuid4(),
            tenant_b,
        )
        assert updated == "UPDATE 0"

        # Claiming another tenant's cursor via INSERT violates WITH CHECK.
        with pytest.raises(BaseException) as excinfo:
            await app.execute(
                "INSERT INTO promotion_reconciliation_state "
                "(tenant_id, cursor_created_at, cursor_item_id) "
                "VALUES ($1, now(), $2)",
                tenant_b,
                uuid.uuid4(),
            )
        assert _denied(excinfo.value)

        # A GUC matching no tenant (here: empty string) exposes zero rows.
        # Session-scoped reset: a transaction-local set_config would vanish
        # with asyncpg's implicit per-statement transaction.
        await app.execute("SELECT set_config('app.tenant_id', '', false)")
        assert await app.fetchval("SELECT count(*) FROM promotion_reconciliation_state") == 0
    finally:
        await app.close()
        await _clear_tenant(owner, tenant_a)
        await _clear_tenant(owner, tenant_b)
        await owner.close()


async def test_app_role_cannot_delete_cursor() -> None:
    owner = await _owner_with_032()
    app = await _connect(_app_dsn())  # type: ignore[arg-type]
    tenant_id = uuid.uuid4()
    try:
        await _seed_tenant(owner, tenant_id=tenant_id, label="CursorDel")
        await _owner_insert_cursor(owner, tenant_id=tenant_id)
        await app.execute("SELECT set_config('app.tenant_id', $1, false)", str(tenant_id))
        with pytest.raises(BaseException) as excinfo:
            await app.execute("DELETE FROM promotion_reconciliation_state")
        assert _denied(excinfo.value)
    finally:
        await app.close()
        await _clear_tenant(owner, tenant_id)
        await owner.close()
