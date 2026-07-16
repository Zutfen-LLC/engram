"""PostgreSQL migration and RLS proof for ENG-SCOPE-001 (migration 021).

Follows the pattern of tests/test_candidate_ingests_migration.py: re-executes
the migration SQL directly against the already-migrated CI database (owner
role), so it proves the file's own idempotency regardless of whether
``schema_migrations`` already recorded it. Since the CHECK constraint this
migration adds is already live by the time these tests run (applied via
docker-entrypoint-initdb.d on container start), the legacy-row scenario is
simulated by temporarily dropping the constraint, inserting a pre-migration-
shaped row, then re-running the migration to normalize it and recreate the
constraint.
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


async def _insert_legacy_item(owner, *, tenant, principal, item_id, content, chash) -> None:
    await owner.execute(
        "INSERT INTO memory_items ("
        "id, tenant_id, workspace_id, principal_id, content, content_hash, kind, "
        "visibility, review_status, memory_confidence, source_trust, importance, "
        "source_type, valid_from"
        ") VALUES ($1,$2,NULL,$3,$4,$5,'fact','workspace','active',0.8,0.8,0.5,'manual', now())",
        item_id,
        tenant,
        principal,
        content,
        chash,
    )


async def test_migration_021_normalizes_legacy_rows_idempotently_and_enforces_invariant() -> None:
    _require_stack()
    owner = await _connect(_owner_dsn())  # type: ignore[arg-type]
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    item_id = uuid.uuid4()
    try:
        await owner.execute(
            "INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $3)",
            tenant,
            "scope-mig",
            f"scope-mig-{tenant.hex[:8]}",
        )
        await owner.execute(
            "INSERT INTO principals (id, tenant_id, name, type) VALUES ($1, $2, 'admin', 'admin')",
            principal,
            tenant,
        )

        # Simulate pre-migration legacy state: drop the (already-applied)
        # constraint so a workspace-visible/NULL-workspace row can be
        # inserted, matching what genuinely-historical rows look like.
        await owner.execute(
            f"ALTER TABLE memory_items DROP CONSTRAINT IF EXISTS {_CONSTRAINT_NAME}"
        )
        await _insert_legacy_item(
            owner,
            tenant=tenant,
            principal=principal,
            item_id=item_id,
            content="legacy workspace-null content",
            chash="sha256:scope-mig-legacy",
        )

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
            "SELECT id FROM item_events WHERE item_id = $1", item_id
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
            "SELECT convalidated FROM pg_constraint WHERE conname = $1", _CONSTRAINT_NAME
        )
        assert constraint_row is not None
        assert constraint_row["convalidated"] is True

        with pytest.raises(Exception):  # noqa: B017
            await _insert_legacy_item(
                owner,
                tenant=tenant,
                principal=principal,
                item_id=uuid.uuid4(),
                content="new violation attempt",
                chash="sha256:scope-mig-new-violation",
            )

        # --- FORCE RLS is unchanged. ---
        security = await owner.fetchrow(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE oid = 'memory_items'::regclass"
        )
        assert security["relrowsecurity"] is True
        assert security["relforcerowsecurity"] is True
    finally:
        await owner.execute("DELETE FROM tenants WHERE id = $1", tenant)
        await owner.close()


async def test_app_role_cannot_insert_workspace_null_visibility_row() -> None:
    """The non-owner application role is blocked by the same CHECK constraint."""
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

        with pytest.raises(Exception):  # noqa: B017
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
