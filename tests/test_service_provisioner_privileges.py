"""Database-level least-privilege and service-context RLS proofs."""
# ruff: noqa: E501

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from engram.migrations import normalize_asyncpg_url

pytestmark = [pytest.mark.asyncio, pytest.mark.service_provisioning_postgres]


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
    first_principal, second_principal = uuid.uuid4(), uuid.uuid4()
    first_principal_binding, second_principal_binding = uuid.uuid4(), uuid.uuid4()
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
            for principal_id, tenant_id, name in (
                (first_principal, first_tenant, "one"),
                (second_principal, second_tenant, "two"),
            ):
                await owner.execute(
                    "INSERT INTO principals (id,tenant_id,name,type) VALUES ($1,$2,$3,'user')",
                    principal_id,
                    tenant_id,
                    f"Privilege principal {name} {tag}",
                )
            for binding_id, client_id, tenant_binding_id, tenant_id, principal_id, suffix in (
                (
                    first_principal_binding,
                    first_client,
                    first_binding,
                    first_tenant,
                    first_principal,
                    "one",
                ),
                (
                    second_principal_binding,
                    second_client,
                    second_binding,
                    second_tenant,
                    second_principal,
                    "two",
                ),
            ):
                await owner.execute(
                    "INSERT INTO principal_provisioning_bindings "
                    "(id,service_client_id,tenant_binding_id,tenant_id,external_ref,principal_id) "
                    "VALUES ($1,$2,$3,$4,$5,$6)",
                    binding_id,
                    client_id,
                    tenant_binding_id,
                    tenant_id,
                    f"principal-ref-{suffix}-{tag}",
                    principal_id,
                )
            for client_id, tenant_binding_id, principal_binding_id in (
                (first_client, first_binding, first_principal_binding),
                (second_client, second_binding, second_principal_binding),
            ):
                await owner.execute(
                    "INSERT INTO service_provisioning_idempotency "
                    "(service_client_id,key_digest,request_digest,tenant_binding_id,principal_binding_id) "
                    "VALUES ($1,$2,$3,$4,$5)",
                    client_id,
                    b"k" * 32,
                    b"r" * 32,
                    tenant_binding_id,
                    principal_binding_id,
                )
                await owner.execute(
                    "INSERT INTO service_provisioning_events "
                    "(service_client_id,event_type,outcome,request_id) VALUES ($1,'tenant.provisioned','success',$2)",
                    client_id,
                    f"privilege-event-{client_id.hex[:8]}",
                )
        yield {
            "owner": owner,
            "provisioner": provisioner,
            "first_client": first_client,
            "second_client": second_client,
            "first_tenant": first_tenant,
            "second_tenant": second_tenant,
            "first_principal": first_principal,
            "second_principal": second_principal,
        }
    finally:
        await provisioner.close()
        async with owner.transaction():
            await owner.execute(
                "DELETE FROM service_provisioning_events WHERE service_client_id IN ($1,$2)",
                first_client,
                second_client,
            )
            await owner.execute(
                "DELETE FROM service_provisioning_idempotency WHERE service_client_id IN ($1,$2)",
                first_client,
                second_client,
            )
            await owner.execute(
                "DELETE FROM principal_provisioning_bindings WHERE service_client_id IN ($1,$2)",
                first_client,
                second_client,
            )
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
            await owner.execute(
                "DELETE FROM service_clients WHERE id IN ($1,$2)", first_client, second_client
            )
            await owner.execute(
                "DELETE FROM tenants WHERE id IN ($1,$2)", first_tenant, second_tenant
            )
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
    assert (
        await conn.fetchval("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")
        is False
    )
    assert (
        await conn.fetchval("SELECT has_table_privilege(current_user, 'memory_items', 'SELECT')")
        is False
    )
    assert (
        await conn.fetchval("SELECT has_table_privilege(current_user, 'service_clients', 'UPDATE')")
        is False
    )
    assert (
        await conn.fetchval(
            "SELECT has_column_privilege(current_user, 'service_clients', 'updated_at', 'UPDATE')"
        )
        is True
    )
    for table, allowed_column in (
        ("service_clients", "updated_at"),
        ("service_client_credentials", "last_used_at"),
    ):
        writable = await conn.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='public' "
            "AND table_name=$1 AND has_column_privilege(current_user, $1, column_name, 'UPDATE')",
            table,
        )
        assert [row["column_name"] for row in writable] == [allowed_column]
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await conn.execute(
            "UPDATE service_clients SET status='disabled' WHERE id=$1", proof["first_client"]
        )
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await conn.execute("SELECT * FROM memory_items")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await conn.execute("CREATE TABLE prohibited_provisioner_table (id int)")
    for statement in (
        "ALTER TABLE service_clients ADD COLUMN prohibited int",
        "DROP TABLE service_clients",
        "CREATE FUNCTION prohibited_provisioner_function() RETURNS int LANGUAGE sql AS 'SELECT 1'",
    ):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(statement)


@pytest.mark.parametrize(
    "table",
    (
        "memory_items",
        "memory_embeddings",
        "recall_logs",
        "context_receipts",
        "api_keys",
        "workspaces",
        "usage_events",
        "classification_runs",
    ),
)
async def test_provisioner_cannot_read_non_provisioning_data(role_proof, table: str) -> None:  # type: ignore[no-untyped-def]
    """Every denial uses a savepoint so one expected error cannot mask another."""
    import asyncpg

    conn = role_proof["provisioner"]
    async with conn.transaction():
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            # Let the expected error leave the savepoint so it rolls back;
            # catching it *inside* the savepoint would make asyncpg attempt a
            # commit of an aborted transaction.
            async with conn.transaction():
                await conn.fetch(f"SELECT * FROM {table} LIMIT 1")


async def test_provisioner_cannot_mutate_authority_or_provisioning_history(role_proof) -> None:  # type: ignore[no-untyped-def]
    import asyncpg

    proof = role_proof
    conn = proof["provisioner"]
    denied = (
        ("UPDATE service_clients SET slug='changed' WHERE id=$1", proof["first_client"]),
        (
            "UPDATE service_clients SET permissions=ARRAY['tenant.provision'] WHERE id=$1",
            proof["first_client"],
        ),
        ("UPDATE service_clients SET status='disabled' WHERE id=$1", proof["first_client"]),
        ("UPDATE service_clients SET display_name='changed' WHERE id=$1", proof["first_client"]),
        (
            "UPDATE service_client_credentials SET key_id='changed' WHERE service_client_id=$1",
            proof["first_client"],
        ),
        (
            "UPDATE service_client_credentials SET secret_digest='0' WHERE service_client_id=$1",
            proof["first_client"],
        ),
        (
            "UPDATE service_client_credentials SET digest_algorithm='none' WHERE service_client_id=$1",
            proof["first_client"],
        ),
        (
            "UPDATE service_client_credentials SET status='revoked' WHERE service_client_id=$1",
            proof["first_client"],
        ),
        (
            "UPDATE service_client_credentials SET expires_at=now() WHERE service_client_id=$1",
            proof["first_client"],
        ),
        (
            "UPDATE service_client_credentials SET service_client_id=$2 WHERE service_client_id=$1",
            (proof["first_client"], proof["second_client"]),
        ),
        ("UPDATE tenants SET name='changed' WHERE id=$1", proof["first_tenant"]),
        ("DELETE FROM tenants WHERE id=$1", proof["first_tenant"]),
        ("UPDATE principals SET name='changed' WHERE id=$1", proof["first_principal"]),
        ("DELETE FROM principals WHERE id=$1", proof["first_principal"]),
        (
            "DELETE FROM tenant_provisioning_bindings WHERE service_client_id=$1",
            proof["first_client"],
        ),
        (
            "UPDATE tenant_provisioning_bindings SET external_ref='changed' WHERE service_client_id=$1",
            proof["first_client"],
        ),
        (
            "DELETE FROM principal_provisioning_bindings WHERE service_client_id=$1",
            proof["first_client"],
        ),
        (
            "UPDATE principal_provisioning_bindings SET external_ref='changed' WHERE service_client_id=$1",
            proof["first_client"],
        ),
        (
            "UPDATE service_provisioning_idempotency SET request_digest=$2 WHERE service_client_id=$1",
            (proof["first_client"], b"x" * 32),
        ),
        (
            "DELETE FROM service_provisioning_idempotency WHERE service_client_id=$1",
            proof["first_client"],
        ),
        (
            "UPDATE service_provisioning_events SET outcome='failure' WHERE service_client_id=$1",
            proof["first_client"],
        ),
        (
            "DELETE FROM service_provisioning_events WHERE service_client_id=$1",
            proof["first_client"],
        ),
    )
    async with conn.transaction():
        await conn.execute(
            "SELECT set_config('app.service_client_id', $1, true)", str(proof["first_client"])
        )
        for statement, value in denied:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with conn.transaction():
                    await conn.execute(
                        statement, *(value if isinstance(value, tuple) else (value,))
                    )


async def test_service_context_rls_hides_other_clients_and_events_are_append_only(
    role_proof,
) -> None:  # type: ignore[no-untyped-def]
    import asyncpg

    proof = role_proof
    conn = proof["provisioner"]
    async with conn.transaction():
        await conn.execute(
            "SELECT set_config('app.service_client_id', $1, true)", str(proof["first_client"])
        )
        visible = await conn.fetch(
            "SELECT service_client_id, tenant_id FROM tenant_provisioning_bindings ORDER BY tenant_id"
        )
        assert [(row["service_client_id"], row["tenant_id"]) for row in visible] == [
            (proof["first_client"], proof["first_tenant"])
        ]
        assert await conn.fetchval("SELECT count(*) FROM principals") == 1
        assert await conn.fetchval("SELECT count(*) FROM principal_provisioning_bindings") == 1
        assert await conn.fetchval("SELECT count(*) FROM service_provisioning_idempotency") == 1
        assert await conn.fetchval("SELECT count(*) FROM service_provisioning_events") == 1
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await conn.execute("UPDATE service_provisioning_events SET outcome='failure'")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await conn.execute("DELETE FROM service_provisioning_events")
