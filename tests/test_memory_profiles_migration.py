"""Real-PostgreSQL proofs for migration 022 memory-profile integrity."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from engram.migrations import normalize_asyncpg_url

MIGRATION_SQL = Path("migrations/022_memory_profiles.sql").read_text()


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


async def _seed_identity(owner, tenant: uuid.UUID, principal: uuid.UUID, suffix: str) -> None:
    await owner.execute(
        "INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $3)",
        tenant,
        f"profile-{suffix}",
        f"profile-{suffix}-{tenant.hex[:8]}",
    )
    await owner.execute(
        "INSERT INTO principals (id, tenant_id, name, type) VALUES ($1, $2, 'admin', 'admin')",
        principal,
        tenant,
    )


async def _seed_profile(
    owner,
    *,
    tenant: uuid.UUID,
    principal: uuid.UUID,
    slug: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    profile, revision = uuid.uuid4(), uuid.uuid4()
    async with owner.transaction():
        await owner.execute(
            "INSERT INTO memory_profiles "
            "(id, tenant_id, name, slug, created_by_principal_id) "
            "VALUES ($1, $2, $3, $4, $5)",
            profile,
            tenant,
            slug,
            slug,
            principal,
        )
        await owner.execute(
            "INSERT INTO memory_profile_revisions "
            "(id, tenant_id, profile_id, version, created_by_principal_id, reason) "
            "VALUES ($1, $2, $3, 1, $4, 'initial')",
            revision,
            tenant,
            profile,
            principal,
        )
        await owner.execute(
            "UPDATE memory_profiles SET active_revision_id = $1 WHERE id = $2",
            revision,
            profile,
        )
    return profile, revision


async def test_migration_022_applies_to_pre022_schema_and_reapplies_cleanly() -> None:
    _require_stack()
    owner = await _connect(_owner_dsn())  # type: ignore[arg-type]
    schema = f"profile_mig_{uuid.uuid4().hex}"
    try:
        await owner.execute(f'CREATE SCHEMA "{schema}"')
        await owner.execute(f'SET search_path TO "{schema}", public')
        await owner.execute(
            "CREATE TABLE tenants (id UUID PRIMARY KEY, name TEXT NOT NULL, slug TEXT UNIQUE)"
        )
        await owner.execute(
            "CREATE TABLE workspaces (id UUID PRIMARY KEY, tenant_id UUID NOT NULL "
            "REFERENCES tenants(id), name TEXT, slug TEXT)"
        )
        await owner.execute(
            "CREATE TABLE principals (id UUID PRIMARY KEY, tenant_id UUID NOT NULL "
            "REFERENCES tenants(id), name TEXT, type TEXT)"
        )
        await owner.execute(
            "CREATE TABLE api_keys (id UUID PRIMARY KEY DEFAULT uuid_generate_v4(), "
            "tenant_id UUID NOT NULL REFERENCES tenants(id), revoked_at TIMESTAMPTZ)"
        )

        await owner.execute(MIGRATION_SQL)
        # Simulate the broad privilege left by the earlier development copy;
        # reapplication must repair it as well as remain object-idempotent.
        await owner.execute("GRANT UPDATE ON memory_profiles TO engram_app")
        assert await owner.fetchval(
            "SELECT has_table_privilege('engram_app', 'memory_profiles', 'UPDATE')"
        )
        await owner.execute(MIGRATION_SQL)

        expected = {
            "fk_memory_profiles_tenant_creator",
            "fk_memory_profile_revisions_tenant_creator",
            "fk_memory_profile_events_tenant_actor",
            "fk_memory_profile_event_revision",
            "fk_memory_profiles_active_revision",
            "fk_api_keys_memory_profile",
        }
        actual = {
            row["conname"]
            for row in await owner.fetch(
                "SELECT conname FROM pg_constraint WHERE connamespace = $1::regnamespace",
                schema,
            )
        }
        assert expected <= actual
        for table in (
            "memory_profiles",
            "memory_profile_revisions",
            "memory_profile_workspace_grants",
            "memory_profile_events",
        ):
            security = await owner.fetchrow(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE oid = to_regclass($1)",
                f"{schema}.{table}",
            )
            assert dict(security) == {
                "relrowsecurity": True,
                "relforcerowsecurity": True,
            }
        assert not await owner.fetchval(
            "SELECT has_table_privilege('engram_app', 'memory_profiles', 'UPDATE')"
        )
        for column in ("active_revision_id", "disabled_at", "updated_at"):
            assert await owner.fetchval(
                "SELECT has_column_privilege('engram_app', 'memory_profiles', $1, 'UPDATE')",
                column,
            )
        for column in (
            "id",
            "tenant_id",
            "name",
            "slug",
            "description",
            "created_by_principal_id",
            "created_at",
        ):
            assert not await owner.fetchval(
                "SELECT has_column_privilege('engram_app', 'memory_profiles', $1, 'UPDATE')",
                column,
            )
    finally:
        await owner.execute("SET search_path TO public")
        await owner.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await owner.close()


async def test_audit_identity_and_revision_references_are_tenant_and_profile_safe() -> None:
    import asyncpg

    _require_stack()
    owner = await _connect(_owner_dsn())  # type: ignore[arg-type]
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    principal_a, principal_b = uuid.uuid4(), uuid.uuid4()
    try:
        await _seed_identity(owner, tenant_a, principal_a, "a")
        await _seed_identity(owner, tenant_b, principal_b, "b")
        profile_a, revision_a = await _seed_profile(
            owner, tenant=tenant_a, principal=principal_a, slug="alpha"
        )
        profile_a2, revision_a2 = await _seed_profile(
            owner, tenant=tenant_a, principal=principal_a, slug="alpha-two"
        )
        profile_b, revision_b = await _seed_profile(
            owner, tenant=tenant_b, principal=principal_b, slug="beta"
        )

        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await owner.execute(
                "INSERT INTO memory_profiles "
                "(tenant_id, name, slug, created_by_principal_id) "
                "VALUES ($1, 'bad', $2, $3)",
                tenant_a,
                f"bad-{uuid.uuid4().hex}",
                principal_b,
            )
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await owner.execute(
                "INSERT INTO memory_profile_revisions "
                "(tenant_id, profile_id, version, created_by_principal_id, reason) "
                "VALUES ($1, $2, 2, $3, 'bad creator')",
                tenant_a,
                profile_a,
                principal_b,
            )
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await owner.execute(
                "INSERT INTO memory_profile_events "
                "(tenant_id, profile_id, revision_id, actor_principal_id, "
                "event_type, reason) VALUES ($1, $2, $3, $4, 'revision_activated', 'bad actor')",
                tenant_a,
                profile_a,
                revision_a,
                principal_b,
            )

        for invalid_revision in (uuid.uuid4(), revision_b, revision_a2):
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await owner.execute(
                    "INSERT INTO memory_profile_events "
                    "(tenant_id, profile_id, revision_id, actor_principal_id, "
                    "event_type, reason) VALUES ($1, $2, $3, $4, "
                    "'revision_activated', 'bad revision')",
                    tenant_a,
                    profile_a,
                    invalid_revision,
                    principal_a,
                )

        event_id = await owner.fetchval(
            "INSERT INTO memory_profile_events "
            "(tenant_id, profile_id, revision_id, actor_principal_id, "
            "event_type, reason) VALUES ($1, $2, NULL, $3, 'profile_disabled', 'lifecycle') "
            "RETURNING id",
            tenant_a,
            profile_a,
            principal_a,
        )
        assert event_id is not None
        assert profile_a2 != profile_b
        assert revision_a != revision_b
    finally:
        await owner.execute("DELETE FROM tenants WHERE id = ANY($1::uuid[])", [tenant_a, tenant_b])
        await owner.close()


async def test_creator_deletion_preserves_history_and_app_cannot_mutate_attribution() -> None:
    import asyncpg

    _require_stack()
    owner = await _connect(_owner_dsn())  # type: ignore[arg-type]
    app = await _connect(_app_dsn())  # type: ignore[arg-type]
    tenant, creator, caller = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    workspace = uuid.uuid4()
    profile: uuid.UUID | None = None
    tenant_deleted = False
    try:
        await _seed_identity(owner, tenant, creator, "creator-delete")
        await owner.execute(
            "INSERT INTO principals (id, tenant_id, name, type) "
            "VALUES ($1, $2, 'lifecycle-admin', 'admin')",
            caller,
            tenant,
        )
        await owner.execute(
            "INSERT INTO workspaces (id, tenant_id, name, slug) "
            "VALUES ($1, $2, 'History', 'history')",
            workspace,
            tenant,
        )
        profile, revision = await _seed_profile(
            owner, tenant=tenant, principal=creator, slug="creator-history"
        )
        await owner.execute(
            "INSERT INTO memory_profile_workspace_grants "
            "(tenant_id, revision_id, workspace_id, can_read, can_write) "
            "VALUES ($1, $2, $3, true, true)",
            tenant,
            revision,
            workspace,
        )
        await owner.execute(
            "UPDATE memory_profile_revisions "
            "SET default_write_visibility = 'workspace', default_write_workspace_id = $1 "
            "WHERE id = $2",
            workspace,
            revision,
        )
        await owner.executemany(
            "INSERT INTO memory_profile_events "
            "(tenant_id, profile_id, revision_id, actor_principal_id, event_type, reason) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            [
                (tenant, profile, revision, creator, "profile_created", "initial"),
                (tenant, profile, None, creator, "profile_disabled", "lifecycle"),
            ],
        )

        await app.execute("SELECT set_config('app.tenant_id', $1, false)", str(tenant))
        await app.execute("SELECT set_config('app.principal_id', $1, false)", str(caller))
        for value in (None, caller):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await app.execute(
                    "UPDATE memory_profiles SET created_by_principal_id = $1 WHERE id = $2",
                    value,
                    profile,
                )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app.execute(
                "UPDATE memory_profile_revisions SET created_by_principal_id = NULL WHERE id = $1",
                revision,
            )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app.execute(
                "UPDATE memory_profile_events SET actor_principal_id = NULL WHERE profile_id = $1",
                profile,
            )

        assert (
            await app.execute(
                "UPDATE memory_profiles SET disabled_at = now(), updated_at = now() WHERE id = $1",
                profile,
            )
            == "UPDATE 1"
        )
        assert await app.fetchval(
            "SELECT disabled_at IS NOT NULL FROM memory_profiles WHERE id = $1", profile
        )
        assert (
            await app.execute(
                "UPDATE memory_profiles SET disabled_at = NULL, updated_at = now() WHERE id = $1",
                profile,
            )
            == "UPDATE 1"
        )

        # The profile immutability trigger fires during this FK action.  Its
        # narrow non-null -> NULL exception must allow all three attribution
        # references to clear while retaining the historical control plane.
        assert await owner.execute("DELETE FROM principals WHERE id = $1", creator) == "DELETE 1"
        profile_row = await owner.fetchrow(
            "SELECT created_by_principal_id, active_revision_id, disabled_at "
            "FROM memory_profiles WHERE id = $1",
            profile,
        )
        assert dict(profile_row) == {
            "created_by_principal_id": None,
            "active_revision_id": revision,
            "disabled_at": None,
        }
        assert (
            await owner.fetchval(
                "SELECT created_by_principal_id FROM memory_profile_revisions WHERE id = $1",
                revision,
            )
            is None
        )
        assert (
            await owner.fetchval(
                "SELECT count(*) FROM memory_profile_events "
                "WHERE profile_id = $1 AND actor_principal_id IS NULL",
                profile,
            )
            == 2
        )
        counts = await owner.fetchrow(
            "SELECT "
            "(SELECT count(*) FROM memory_profiles WHERE id = $1) AS profiles, "
            "(SELECT count(*) FROM memory_profile_revisions WHERE profile_id = $1) AS revisions, "
            "(SELECT count(*) FROM memory_profile_workspace_grants WHERE revision_id = $2) "
            "AS grants, "
            "(SELECT count(*) FROM memory_profile_events WHERE profile_id = $1) AS events",
            profile,
            revision,
        )
        assert dict(counts) == {"profiles": 1, "revisions": 1, "grants": 1, "events": 2}

        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await owner.execute("DELETE FROM workspaces WHERE id = $1", workspace)

        assert await owner.execute("DELETE FROM tenants WHERE id = $1", tenant) == "DELETE 1"
        tenant_deleted = True
        assert await owner.fetchval("SELECT count(*) FROM tenants WHERE id = $1", tenant) == 0
        assert (
            await owner.fetchval("SELECT count(*) FROM memory_profiles WHERE id = $1", profile) == 0
        )
        assert (
            await owner.fetchval(
                "SELECT count(*) FROM memory_profile_revisions WHERE id = $1", revision
            )
            == 0
        )
        assert (
            await owner.fetchval(
                "SELECT count(*) FROM memory_profile_workspace_grants WHERE revision_id = $1",
                revision,
            )
            == 0
        )
        assert (
            await owner.fetchval(
                "SELECT count(*) FROM memory_profile_events WHERE profile_id = $1", profile
            )
            == 0
        )
    finally:
        await app.close()
        if not tenant_deleted:
            await owner.execute("DELETE FROM tenants WHERE id = $1", tenant)
        await owner.close()


async def test_force_rls_app_privileges_and_key_binding_immutability_are_preserved() -> None:
    _require_stack()
    owner = await _connect(_owner_dsn())  # type: ignore[arg-type]
    app = await _connect(_app_dsn())  # type: ignore[arg-type]
    tenant, principal = uuid.uuid4(), uuid.uuid4()
    key_id = uuid.uuid4()
    try:
        await _seed_identity(owner, tenant, principal, "rls")
        profile, revision = await _seed_profile(
            owner, tenant=tenant, principal=principal, slug="rls-profile"
        )
        await owner.execute(
            "INSERT INTO api_keys "
            "(id, tenant_id, principal_id, memory_profile_id, scopes) "
            "VALUES ($1, $2, $3, $4, ARRAY['read'])",
            key_id,
            tenant,
            principal,
            profile,
        )
        await owner.execute(
            "INSERT INTO memory_profile_events "
            "(tenant_id, profile_id, revision_id, actor_principal_id, event_type, reason) "
            "VALUES ($1, $2, $3, $4, 'profile_created', 'initial')",
            tenant,
            profile,
            revision,
            principal,
        )

        await app.execute("SELECT set_config('app.tenant_id', $1, false)", str(tenant))
        await app.execute("SELECT set_config('app.principal_id', $1, false)", str(principal))
        assert await app.fetchval("SELECT count(*) FROM memory_profiles") == 1
        assert await app.fetchval("SELECT count(*) FROM memory_profile_events") == 1
        for table in (
            "memory_profiles",
            "memory_profile_revisions",
            "memory_profile_workspace_grants",
            "memory_profile_events",
        ):
            security = await owner.fetchrow(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = $1::regclass",
                table,
            )
            assert dict(security) == {
                "relrowsecurity": True,
                "relforcerowsecurity": True,
            }

        with pytest.raises(Exception):  # noqa: B017
            await app.execute("UPDATE memory_profile_events SET reason = 'tampered'")
        with pytest.raises(Exception):  # noqa: B017
            await app.execute("DELETE FROM memory_profile_events")
        with pytest.raises(Exception):  # noqa: B017
            await app.execute("UPDATE api_keys SET memory_profile_id = NULL WHERE id = $1", key_id)
        await app.execute("UPDATE api_keys SET revoked_at = now() WHERE id = $1", key_id)
        assert await owner.fetchval(
            "SELECT revoked_at IS NOT NULL FROM api_keys WHERE id = $1", key_id
        )
    finally:
        await app.close()
        await owner.execute("DELETE FROM tenants WHERE id = $1", tenant)
        await owner.close()
