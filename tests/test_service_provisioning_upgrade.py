"""Persisted migration-026-to-027 proof for service provisioning."""
# ruff: noqa: E501

from __future__ import annotations

import os
import secrets
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.cli import _run_init_db
from engram.migrations import discover_migrations, normalize_asyncpg_url
from engram.service_auth import (
    digest_service_secret,
    generate_service_credential,
    parse_service_credential,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.service_provisioning_postgres]


def _owner_url() -> str | None:
    return os.getenv("ENGRAM_OWNER_DATABASE_URL") or os.getenv("ENGRAM_DATABASE_URL")


def _with_database(url: str, database: str) -> str:
    prefix, _separator, _previous = url.rpartition("/")
    assert prefix
    return f"{prefix}/{database}"


def _provisioner_url(owner_url: str, database: str, password: str | None = None) -> str:
    host = owner_url.rsplit("@", 1)[1].rpartition("/")[0]
    credentials = "engram_provisioner" if password is None else f"engram_provisioner:{password}"
    return f"postgresql+asyncpg://{credentials}@{host}/{database}"


async def _upgrade_session(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    async with factory() as session:
        await session.execute(
            text(
                "SELECT set_config('app.tenant_id', "
                "(SELECT id::text FROM tenants ORDER BY created_at LIMIT 1), true), "
                "set_config('app.principal_id', "
                "(SELECT id::text FROM principals WHERE type='admin' ORDER BY created_at LIMIT 1), true)"
            )
        )
        yield session


async def test_persisted_migration_026_to_027_preserves_data_and_converges_role(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Use the production migration runner, never a hand-written 027 simulation."""
    import asyncpg

    owner_url = _owner_url()
    if owner_url is None:
        pytest.skip("requires owner PostgreSQL URL for persisted migration upgrade")
    database = f"provision_upgrade_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(normalize_asyncpg_url(owner_url))
    database_url = _with_database(owner_url, database)
    provisioner_url = _provisioner_url(owner_url, database)
    # Preserve the cluster-global role password exactly.  The disposable
    # database is isolated; PostgreSQL roles are not, so cleanup must be too.
    original_password = await admin.fetchval(
        "SELECT rolpassword FROM pg_authid WHERE rolname='engram_provisioner'"
    )
    created_database = False
    upgrade_engine = None
    provisioner_engine = None
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        created_database = True

        pre_027 = tmp_path / "pre-027-migrations"
        pre_027.mkdir()
        for migration in discover_migrations():
            if migration.name <= "026_context_receipts.sql":
                (pre_027 / migration.name).symlink_to(migration.resolve())
        assert await _run_init_db(database_url, migrations_dir=pre_027) == 0

        before = await asyncpg.connect(normalize_asyncpg_url(database_url))
        tenant_id, user_id, agent_id, workspace_id, item_id, api_key_id = (
            uuid.uuid4(),
            uuid.uuid4(),
            uuid.uuid4(),
            uuid.uuid4(),
            uuid.uuid4(),
            uuid.uuid4(),
        )
        try:
            await before.execute(
                "INSERT INTO tenants (id,name,slug) VALUES ($1,$2,$3)",
                tenant_id,
                "Persisted upgrade tenant",
                f"persisted-upgrade-{tenant_id.hex[:12]}",
            )
            await before.execute(
                "INSERT INTO principals (id,tenant_id,name,type) VALUES ($1,$2,$3,'user'),($4,$2,$5,'agent')",
                user_id,
                tenant_id,
                "Persisted upgrade user",
                agent_id,
                "Persisted upgrade agent",
            )
            await before.execute(
                "INSERT INTO workspaces (id,tenant_id,name,slug) VALUES ($1,$2,$3,$4)",
                workspace_id,
                tenant_id,
                "Persisted upgrade workspace",
                "persisted-upgrade-workspace",
            )
            await before.execute("INSERT INTO tenant_config (tenant_id) VALUES ($1)", tenant_id)
            await before.execute(
                "INSERT INTO memory_items (id,tenant_id,workspace_id,principal_id,content,content_hash,kind,"
                "visibility,review_status,source_type) VALUES ($1,$2,$3,$4,$5,$6,'fact','workspace','active','manual')",
                item_id,
                tenant_id,
                workspace_id,
                user_id,
                "persisted pre-upgrade memory",
                f"sha256:{item_id.hex}",
            )
            await before.execute(
                "INSERT INTO api_keys (id,tenant_id,principal_id,key_hash,scopes,label) "
                "VALUES ($1,$2,$3,$4,$5,$6)",
                api_key_id,
                tenant_id,
                user_id,
                "pre-upgrade-api-key-digest",
                ["read"],
                "persisted upgrade key",
            )
        finally:
            await before.close()

        # This is deliberately hostile legacy state.  Migration 027 must
        # converge it instead of relying on a fresh CREATE ROLE branch.
        await admin.execute(
            "ALTER ROLE engram_provisioner LOGIN SUPERUSER BYPASSRLS CREATEDB CREATEROLE REPLICATION INHERIT"
        )
        await admin.execute("GRANT engram TO engram_provisioner")
        await admin.execute("ALTER ROLE engram_provisioner PASSWORD NULL")
        with pytest.raises(asyncpg.InvalidPasswordError):
            await asyncpg.connect(normalize_asyncpg_url(provisioner_url))

        assert await _run_init_db(database_url) == 0
        after = await asyncpg.connect(normalize_asyncpg_url(database_url))
        try:
            assert (
                await after.fetchval(
                    "SELECT count(*) FROM schema_migrations WHERE filename='027_service_provisioning.sql'"
                )
                == 1
            )
            assert await after.fetchval("SELECT count(*) FROM tenants WHERE id=$1", tenant_id) == 1
            assert await after.fetchval("SELECT count(*) FROM principals WHERE id=$1", user_id) == 1
            assert (
                await after.fetchval("SELECT count(*) FROM principals WHERE id=$1", agent_id) == 1
            )
            assert (
                await after.fetchval("SELECT count(*) FROM workspaces WHERE id=$1", workspace_id)
                == 1
            )
            assert (
                await after.fetchval("SELECT count(*) FROM memory_items WHERE id=$1", item_id) == 1
            )
            assert (
                await after.fetchval("SELECT count(*) FROM api_keys WHERE id=$1", api_key_id) == 1
            )
            role = await after.fetchrow(
                "SELECT rolcanlogin,rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolreplication,rolinherit,"
                "(SELECT count(*) FROM pg_auth_members WHERE member=r.oid) memberships "
                "FROM pg_roles r WHERE rolname='engram_provisioner'"
            )
            assert role is not None
            assert tuple(role.values()) == (True, False, False, False, False, False, False, 0)
            assert (
                await after.fetchval(
                    "SELECT has_schema_privilege('engram_provisioner','public','CREATE')"
                )
                is False
            )
            assert (
                await after.fetchval("SELECT to_regclass('public.service_clients') IS NOT NULL")
                is True
            )
            assert (
                await after.fetchval(
                    "SELECT to_regprocedure('current_service_client_id()') IS NOT NULL"
                )
                is True
            )
        finally:
            await after.close()

        # Rerunning the same production command is migration tracking proof,
        # not a second direct execution of 027.
        assert await _run_init_db(database_url) == 0

        assigned_password = secrets.token_urlsafe(32)
        password_sql = await admin.fetchval(
            "SELECT format('ALTER ROLE engram_provisioner PASSWORD %L', $1::text)", assigned_password
        )
        assert isinstance(password_sql, str)
        await admin.execute(password_sql)
        password_hash = await admin.fetchval(
            "SELECT rolpassword FROM pg_authid WHERE rolname='engram_provisioner'"
        )
        monkeypatch.setenv("ENGRAM_PROVISIONER_DATABASE_URL", "environment-only-change")
        assert (
            await admin.fetchval(
                "SELECT rolpassword FROM pg_authid WHERE rolname='engram_provisioner'"
            )
            == password_hash
        )

        provisioner_url = _provisioner_url(owner_url, database, assigned_password)
        provisioner = await asyncpg.connect(normalize_asyncpg_url(provisioner_url))
        await provisioner.close()

        upgrade_engine = create_async_engine(database_url, poolclass=NullPool)
        provisioner_engine = create_async_engine(provisioner_url, poolclass=NullPool)
        upgrade_factory = async_sessionmaker(
            upgrade_engine, class_=AsyncSession, expire_on_commit=False
        )
        provisioner_factory = async_sessionmaker(
            provisioner_engine, class_=AsyncSession, expire_on_commit=False
        )
        import engram.db as db_module
        from engram.api.app import create_app
        from engram.config import settings

        monkeypatch.setattr(db_module, "provisioner_session_factory", provisioner_factory)
        monkeypatch.setattr(settings, "service_provisioning_enabled", True)
        app = create_app()

        async def upgraded_get_session() -> AsyncGenerator[AsyncSession, None]:
            async for session in _upgrade_session(upgrade_factory):
                yield session

        app.dependency_overrides[db_module.get_session] = upgraded_get_session
        credential = generate_service_credential()
        parsed = parse_service_credential(credential)
        client_id, credential_id = uuid.uuid4(), uuid.uuid4()
        async with upgrade_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO service_clients (id,slug,display_name,permissions) VALUES (:id,:slug,:name,:permissions)"
                ),
                {
                    "id": client_id,
                    "slug": f"upgrade-client-{client_id.hex[:12]}",
                    "name": "Upgrade client",
                    "permissions": ["tenant.provision", "principal.provision"],
                },
            )
            await session.execute(
                text(
                    "INSERT INTO service_client_credentials (id,service_client_id,key_id,secret_digest,digest_algorithm) VALUES (:id,:client_id,:key_id,:digest,'sha256')"
                ),
                {
                    "id": credential_id,
                    "client_id": client_id,
                    "key_id": parsed.key_id,
                    "digest": digest_service_secret(parsed.secret),
                },
            )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/ready")).status_code == 200
            response = await client.post(
                "/v1/service/provisioning/tenant-human",
                headers={
                    "Authorization": f"Bearer {credential}",
                    "Idempotency-Key": "persisted-upgrade-proof",
                },
                json={
                    "tenant": {
                        "external_ref": "upgrade-tenant",
                        "name": "Upgrade tenant",
                        "slug": "upgrade-tenant",
                    },
                    "human_principal": {"external_ref": "upgrade-human", "name": "Upgrade human"},
                },
            )
        assert response.status_code == 201
    finally:
        if provisioner_engine is not None:
            await provisioner_engine.dispose()
        if upgrade_engine is not None:
            await upgrade_engine.dispose()
        if original_password is not None:
            restore = await admin.fetchval(
                "SELECT format('ALTER ROLE engram_provisioner PASSWORD %L', $1::text)", original_password
            )
            if isinstance(restore, str):
                await admin.execute(restore)
        await admin.execute(
            "ALTER ROLE engram_provisioner LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT"
        )
        await admin.execute("REVOKE engram FROM engram_provisioner")
        if created_database:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1", database
            )
            await admin.execute(f'DROP DATABASE "{database}"')
        await admin.close()
