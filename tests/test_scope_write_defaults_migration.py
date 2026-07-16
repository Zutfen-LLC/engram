"""PostgreSQL migration and RLS proof for ENG-SCOPE-001 (migration 021).

The pre-021 proof runs in an isolated schema so the canonical CI database is
never observable without its workspace-visibility CHECK. Separate tests prove
the migrated public schema from owner and non-owner application-role views.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from engram.migrations import normalize_asyncpg_url

_MIGRATION_SQL = Path("migrations/021_scope_write_defaults.sql").read_text()
_CONSTRAINT_NAME = "chk_memitems_workspace_visibility_requires_workspace"


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


async def _insert_legacy_item(owner, *, item_id) -> None:
    await owner.execute(
        "INSERT INTO memory_items (id, workspace_id, visibility) VALUES ($1, NULL, 'workspace')",
        item_id,
    )


async def test_migration_021_normalizes_legacy_rows_idempotently_and_enforces_invariant() -> None:
    _require_stack()
    owner = await _connect(_owner_dsn())  # type: ignore[arg-type]
    schema = f"scope_mig_{uuid.uuid4().hex}"
    item_id = uuid.uuid4()
    try:
        await owner.execute(f'CREATE SCHEMA "{schema}"')
        await owner.execute(f'SET search_path TO "{schema}"')
        await owner.execute("CREATE TABLE workspaces (id UUID PRIMARY KEY)")
        await owner.execute(
            "CREATE TABLE memory_items ("
            "id UUID PRIMARY KEY, workspace_id UUID REFERENCES workspaces(id) ON DELETE SET NULL, "
            "visibility TEXT NOT NULL DEFAULT 'workspace')"
        )
        await owner.execute(
            "CREATE TABLE item_events ("
            "item_id UUID NOT NULL, event_type TEXT NOT NULL, field_name TEXT, "
            "old_value TEXT, new_value TEXT, actor_principal_id UUID, reason TEXT)"
        )
        await owner.execute("ALTER TABLE memory_items ENABLE ROW LEVEL SECURITY")
        await owner.execute("ALTER TABLE memory_items FORCE ROW LEVEL SECURITY")
        await owner.execute("GRANT SELECT, INSERT ON memory_items TO engram_app")
        await _insert_legacy_item(owner, item_id=item_id)

        # --- First application: normalizes the row, writes exactly one event. ---
        await owner.execute(_MIGRATION_SQL)

        row = await owner.fetchrow(
            "SELECT visibility, workspace_id FROM memory_items WHERE id = $1", item_id
        )
        assert row["visibility"] == "tenant"
        assert row["workspace_id"] is None

        events = await owner.fetch(
            "SELECT event_type, field_name, old_value, new_value, actor_principal_id, reason "
            "FROM item_events WHERE item_id = $1",
            item_id,
        )
        assert len(events) == 1
        event = events[0]
        assert event["event_type"] == "visibility_change"
        assert event["field_name"] == "visibility"
        assert event["old_value"] == "workspace"
        assert event["new_value"] == "tenant"
        assert event["actor_principal_id"] is None
        assert "ENG-SCOPE-001" in event["reason"]

        # --- Reapplication is a no-op: no new event, row unchanged. ---
        await owner.execute(_MIGRATION_SQL)
        events_again = await owner.fetch(
            "SELECT event_type FROM item_events WHERE item_id = $1", item_id
        )
        assert len(events_again) == 1
        row_again = await owner.fetchrow(
            "SELECT visibility, workspace_id FROM memory_items WHERE id = $1", item_id
        )
        assert row_again["visibility"] == "tenant"
        assert row_again["workspace_id"] is None

        # --- Database default is private. ---
        default_row = await owner.fetchrow(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name = 'memory_items' AND column_name = 'visibility'"
        )
        assert default_row["column_default"] is not None
        assert "private" in default_row["column_default"]

        # --- CHECK constraint exists, is validated, and blocks a fresh violation. ---
        constraint_row = await owner.fetchrow(
            "SELECT convalidated FROM pg_constraint "
            "WHERE conname = $1 AND conrelid = 'memory_items'::regclass",
            _CONSTRAINT_NAME,
        )
        assert constraint_row is not None
        assert constraint_row["convalidated"] is True

        with pytest.raises(Exception):  # noqa: B017
            await _insert_legacy_item(owner, item_id=uuid.uuid4())

        # --- Existing SET NULL FK became restrictive and stays so on reapply. ---
        fk = await owner.fetchrow(
            "SELECT confdeltype FROM pg_constraint WHERE contype = 'f' "
            "AND conrelid = 'memory_items'::regclass"
        )
        assert fk["confdeltype"] in (b"a", b"r", "a", "r")
        await owner.execute(_MIGRATION_SQL)
        assert await owner.fetchval(
            "SELECT count(*) FROM pg_constraint WHERE contype = 'f' "
            "AND conrelid = 'memory_items'::regclass"
        ) == 1

        # --- FORCE RLS is unchanged. ---
        security = await owner.fetchrow(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE oid = 'memory_items'::regclass"
        )
        assert security["relrowsecurity"] is True
        assert security["relforcerowsecurity"] is True
        assert await owner.fetchval(
            "SELECT has_table_privilege('engram_app', 'memory_items', 'SELECT, INSERT')"
        ) is True
    finally:
        await owner.execute("SET search_path TO public")
        await owner.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await owner.close()


async def test_app_role_cannot_insert_workspace_null_visibility_row() -> None:
    """The old-app write shape intentionally fails after the maintenance migration.

    This is proof of mixed-version incompatibility, not a rolling-upgrade
    capability: operators must drain old writers before applying migration 021.
    """
    import asyncpg

    _require_stack()
    owner = await _connect(_owner_dsn())  # type: ignore[arg-type]
    app = await _connect(_app_dsn())  # type: ignore[arg-type]
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    try:
        await owner.execute(
            "INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $3)",
            tenant,
            "scope-mig-app",
            f"scope-mig-app-{tenant.hex[:8]}",
        )
        await owner.execute(
            "INSERT INTO principals (id, tenant_id, name, type) VALUES ($1, $2, 'admin', 'admin')",
            principal,
            tenant,
        )
        await app.execute("SELECT set_config('app.tenant_id', $1, false)", str(tenant))
        await app.execute("SELECT set_config('app.principal_id', $1, false)", str(principal))

        with pytest.raises(asyncpg.CheckViolationError) as exc_info:
            await app.execute(
                "INSERT INTO memory_items ("
                "id, tenant_id, workspace_id, principal_id, content, content_hash, kind, "
                "visibility, review_status, memory_confidence, source_trust, importance, "
                "source_type, valid_from"
                ") VALUES ($1,$2,NULL,$3,'app role violation','sha256:app-role-violation',"
                "'fact','workspace','active',0.8,0.8,0.5,'manual', now())",
                uuid.uuid4(),
                tenant,
                principal,
            )
        assert _CONSTRAINT_NAME in str(exc_info.value)

        # Grants are unchanged: the app role can still write ordinary items.
        good_id = uuid.uuid4()
        await app.execute(
            "INSERT INTO memory_items ("
            "id, tenant_id, workspace_id, principal_id, content, content_hash, kind, "
            "visibility, review_status, memory_confidence, source_trust, importance, "
            "source_type, valid_from"
            ") VALUES ($1,$2,NULL,$3,'app role ok','sha256:app-role-ok',"
            "'fact','private','active',0.8,0.8,0.5,'manual', now())",
            good_id,
            tenant,
            principal,
        )
        assert await app.fetchval(
            "SELECT visibility FROM memory_items WHERE id = $1", good_id
        ) == "private"
    finally:
        await app.close()
        await owner.execute("DELETE FROM tenants WHERE id = $1", tenant)
        await owner.close()


async def test_workspace_memory_association_restricts_delete_and_empty_workspace_deletes() -> None:
    import asyncpg

    _require_stack()
    owner = await _connect(_owner_dsn())  # type: ignore[arg-type]
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    occupied_workspace = uuid.uuid4()
    empty_workspace = uuid.uuid4()
    item_id = uuid.uuid4()
    try:
        await owner.execute(
            "INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $3)",
            tenant,
            "scope-fk",
            f"scope-fk-{tenant.hex[:8]}",
        )
        await owner.execute(
            "INSERT INTO principals (id, tenant_id, name, type) "
            "VALUES ($1, $2, 'writer', 'user')",
            principal,
            tenant,
        )
        await owner.executemany(
            "INSERT INTO workspaces (id, tenant_id, name, slug) VALUES ($1, $2, $3, $4)",
            [
                (occupied_workspace, tenant, "occupied", f"occupied-{tenant.hex[:8]}"),
                (empty_workspace, tenant, "empty", f"empty-{tenant.hex[:8]}"),
            ],
        )
        await owner.execute(
            "INSERT INTO memory_items ("
            "id, tenant_id, workspace_id, principal_id, content, content_hash, kind, "
            "visibility, review_status, memory_confidence, source_trust, importance, "
            "source_type, valid_from"
            ") VALUES ($1,$2,$3,$4,'workspace lifecycle','sha256:scope-fk',"
            "'fact','workspace','active',0.8,0.8,0.5,'manual',now())",
            item_id,
            tenant,
            occupied_workspace,
            principal,
        )

        with pytest.raises(asyncpg.ForeignKeyViolationError) as exc_info:
            await owner.execute("DELETE FROM workspaces WHERE id = $1", occupied_workspace)
        assert "fk_memory_items_workspace_restrict" in str(exc_info.value)

        row = await owner.fetchrow(
            "SELECT workspace_id, visibility FROM memory_items WHERE id = $1", item_id
        )
        assert row["workspace_id"] == occupied_workspace
        assert row["visibility"] == "workspace"

        deleted = await owner.execute("DELETE FROM workspaces WHERE id = $1", empty_workspace)
        assert deleted == "DELETE 1"
        assert await owner.fetchval(
            "SELECT count(*) FROM workspaces WHERE id = $1", empty_workspace
        ) == 0

        # Reapplication retains one restrictive FK and does not weaken RLS or grants.
        await owner.execute(_MIGRATION_SQL)
        fk_rows = await owner.fetch(
            "SELECT c.conname, c.confdeltype FROM pg_constraint c "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
            "WHERE c.contype = 'f' AND c.conrelid = 'memory_items'::regclass "
            "AND a.attname = 'workspace_id'"
        )
        assert len(fk_rows) == 1
        assert fk_rows[0]["confdeltype"] in (b"a", b"r", "a", "r")
        security = await owner.fetchrow(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE oid = 'memory_items'::regclass"
        )
        assert tuple(security) == (True, True)
        assert await owner.fetchval(
            "SELECT has_table_privilege("
            "'engram_app', 'memory_items', 'SELECT, INSERT, UPDATE, DELETE')"
        ) is True
    finally:
        await owner.execute("DELETE FROM memory_items WHERE id = $1", item_id)
        await owner.execute("DELETE FROM tenants WHERE id = $1", tenant)
        await owner.close()
