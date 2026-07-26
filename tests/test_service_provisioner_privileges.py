"""Database-level least-privilege and service-context RLS proofs."""
# ruff: noqa: E501

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from engram.migrations import normalize_asyncpg_url

pytestmark = pytest.mark.asyncio


async def _connect(url: str):  # type: ignore[no-untyped-def]
    import asyncpg

    return await asyncpg.connect(normalize_asyncpg_url(url))


@pytest.fixture
async def role_proof() -> AsyncIterator[dict[str, Any]]:
    owner_url = os.getenv("ENGRAM_OWNER_DATABASE_URL") or os.getenv("ENGRAM_DATABASE_URL")
    provisioner_url = os.getenv("ENGRAM_PROVISIONER_DATABASE_URL")
    if not owner_url or not provisioner_url:
        pytest.skip("requires owner and provisioner PostgreSQL URLs")
    try:
        owner, provisioner = await _connect(owner_url), await _connect(provisioner_url)
    except Exception:
        pytest.skip("requires a migrated provisioner PostgreSQL role")

    tag = uuid.uuid4().hex[:16]
    first_client, second_client = uuid.uuid4(), uuid.uuid4()
    first_tenant, second_tenant = uuid.uuid4(), uuid.uuid4()
    first_binding, second_binding = uuid.uuid4(), uuid.uuid4()
    try:
        async with owner.transaction():
            for client_id, suffix in ((first_client, "one"), (second_client, "two")):
                await owner.execute(
                    "INSERT INTO service_clients (id,slug,display_name,permissions) VALUES ($1,$2,$3,$4)",
                    client_id,
                    f"privilege-{suffix}-{tag}",
                    f"Privilege {suffix} {tag}",
                    ["tenant.provision", "principal.provision"],
                )
                await owner.execute(
                    "INSERT INTO service_client_credentials "
                    "(service_client_id,key_id,secret_digest,digest_algorithm) "
                    "VALUES ($1,$2,$3,'sha256')",
                    client_id,
                    f"{suffix}{tag[:18]}",
                    "a" * 64,
                )
            for tenant_id, suffix in ((first_tenant, "one"), (second_tenant, "two")):
                await owner.execute(
                    "INSERT INTO tenants (id,name,slug) VALUES ($1,$2,$3)",
                    tenant_id,
                    f"Privilege tenant {suffix}",
                    f"privilege-tenant-{suffix}-{tag}",
                )
            await owner.execute(
                "INSERT INTO tenant_provisioning_bindings (id,service_client_id,external_ref,tenant_id) "
                "VALUES ($1,$2,$3,$4)",
                first_binding,
                first_client,
                f"ref-one-{tag}",
                first_tenant,
            )
            await owner.execute(
                "INSERT INTO tenant_provisioning_bindings (id,service_client_id,external_ref,tenant_id) "
                "VALUES ($1,$2,$3,$4)",
                second_binding,
                second_client,
                f"ref-two-{tag}",
                second_tenant,
            )
        yield {
            "owner": owner,
            "provisioner": provisioner,
            "first_client": first_client,
            "second_client": second_client,
            "first_tenant": first_tenant,
            "second_tenant": second_tenant,
        }
    finally:
        await provisioner.close()
        async with owner.transaction():
            await owner.execute(
                "DELETE FROM tenant_provisioning_bindings WHERE service_client_id IN ($1,$2)",
                first_client,
                second_client,
            )
            await owner.execute(
                "DELETE FROM service_client_credentials WHERE service_client_id IN ($1,$2)",
                first_client,
                second_client,
            )
            await owner.execute("DELETE FROM service_clients WHERE id IN ($1,$2)", first_client, second_client)
            await owner.execute("DELETE FROM tenants WHERE id IN ($1,$2)", first_tenant, second_tenant)
        await owner.close()


async def test_provisioner_role_posture_and_narrow_grants(role_proof) -> None:  # type: ignore[no-untyped-def]
    import asyncpg

    proof = role_proof
    conn = proof["provisioner"]
    role = await conn.fetchrow(
        "SELECT current_user AS current_user, r.rolcanlogin, r.rolsuper, r.rolbypassrls, "
        "r.rolcreatedb, r.rolcreaterole, r.rolreplication, r.rolinherit, "
        "(SELECT count(*) FROM pg_auth_members WHERE member=r.oid) AS memberships "
        "FROM pg_roles r WHERE r.rolname=current_user"
    )
    assert role is not None
    assert dict(role) == {
        "current_user": "engram_provisioner",
        "rolcanlogin": True,
        "rolsuper": False,
        "rolbypassrls": False,
        "rolcreatedb": False,
        "rolcreaterole": False,
        "rolreplication": False,
        "rolinherit": False,
        "memberships": 0,
    }
    assert await conn.fetchval("SELECT has_schema_privilege(current_user, 'public', 'CREATE')") is False
    assert await conn.fetchval(
        "SELECT has_table_privilege(current_user, 'memory_items', 'SELECT')"
    ) is False
    assert await conn.fetchval(
        "SELECT has_table_privilege(current_user, 'service_clients', 'UPDATE')"
    ) is False
    assert await conn.fetchval(
        "SELECT has_column_privilege(current_user, 'service_clients', 'updated_at', 'UPDATE')"
    ) is True
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await conn.execute("UPDATE service_clients SET status='disabled' WHERE id=$1", proof["first_client"])
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await conn.execute("SELECT * FROM memory_items")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await conn.execute("CREATE TABLE prohibited_provisioner_table (id int)")


async def test_service_context_rls_hides_other_clients_and_events_are_append_only(role_proof) -> None:  # type: ignore[no-untyped-def]
    import asyncpg

    proof = role_proof
    conn = proof["provisioner"]
    async with conn.transaction():
        await conn.execute("SELECT set_config('app.service_client_id', $1, true)", str(proof["first_client"]))
        visible = await conn.fetch(
            "SELECT service_client_id, tenant_id FROM tenant_provisioning_bindings ORDER BY tenant_id"
        )
        assert [(row["service_client_id"], row["tenant_id"]) for row in visible] == [
            (proof["first_client"], proof["first_tenant"])
        ]
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await conn.execute("UPDATE service_provisioning_events SET outcome='failure'")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await conn.execute("DELETE FROM service_provisioning_events")
