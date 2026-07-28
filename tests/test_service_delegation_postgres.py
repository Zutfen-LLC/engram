"""Real-PostgreSQL delegated grant, issue, use, revoke, race, and privilege proofs."""

from __future__ import annotations

import argparse
import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from engram.api.app import app
from engram.cli import _run_delegation_grant
from engram.config import settings
from engram.delegation_auth import generate_delegation_token, resolve_delegated_principal
from engram.migrations import normalize_asyncpg_url
from engram.service_auth import (
    digest_service_secret,
    generate_service_credential,
    parse_service_credential,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.service_provisioning_postgres]


async def _connect(url: str):  # type: ignore[no-untyped-def]
    import asyncpg

    return await asyncpg.connect(normalize_asyncpg_url(url))


@pytest.fixture
async def delegation_db() -> AsyncIterator[dict[str, Any]]:
    owner_url = os.getenv("ENGRAM_OWNER_DATABASE_URL") or os.getenv("ENGRAM_DATABASE_URL")
    provisioner_url = os.getenv("ENGRAM_PROVISIONER_DATABASE_URL")
    if not owner_url or not provisioner_url:
        pytest.skip("requires owner and provisioner PostgreSQL URLs")
    owner = await _connect(owner_url)
    provisioner = await _connect(provisioner_url)
    tag = uuid.uuid4().hex[:12]
    broker_id = uuid.uuid4()
    binding_owner_id = uuid.uuid4()
    broker_credential_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    tenant_binding_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    principal_binding_id = uuid.uuid4()
    grant_id = uuid.uuid4()
    broker_slug = f"delegation-broker-{tag}"
    owner_slug = f"delegation-owner-{tag}"
    tenant_ref = f"tenant-ref-{tag}"
    principal_ref = f"principal-ref-{tag}"
    broker_credential = generate_service_credential()
    parsed_credential = parse_service_credential(broker_credential)
    async with owner.transaction():
        await owner.execute(
            "INSERT INTO service_clients (id,slug,display_name,permissions) "
            "VALUES ($1,$2,$3,$4),($5,$6,$7,$8)",
            broker_id,
            broker_slug,
            f"Delegation broker {tag}",
            ["delegation.issue"],
            binding_owner_id,
            owner_slug,
            f"Delegation binding owner {tag}",
            [
                "tenant.provision",
                "principal.provision",
                "workspace.provision",
                "agent.provision",
                "api_key.provision",
            ],
        )
        await owner.execute(
            "INSERT INTO service_client_credentials "
            "(id,service_client_id,key_id,secret_digest,digest_algorithm) "
            "VALUES ($1,$2,$3,$4,'sha256')",
            broker_credential_id,
            broker_id,
            parsed_credential.key_id,
            digest_service_secret(parsed_credential.secret),
        )
        await owner.execute(
            "INSERT INTO tenants (id,name,slug) VALUES ($1,$2,$3)",
            tenant_id,
            f"Delegation tenant {tag}",
            f"delegation-tenant-{tag}",
        )
        await owner.execute(
            "INSERT INTO principals (id,tenant_id,name,type) VALUES ($1,$2,$3,'user')",
            principal_id,
            tenant_id,
            f"Delegation human {tag}",
        )
        await owner.execute(
            "INSERT INTO tenant_provisioning_bindings "
            "(id,service_client_id,external_ref,tenant_id) VALUES ($1,$2,$3,$4)",
            tenant_binding_id,
            binding_owner_id,
            tenant_ref,
            tenant_id,
        )
        await owner.execute(
            "INSERT INTO principal_provisioning_bindings "
            "(id,service_client_id,tenant_binding_id,tenant_id,external_ref,principal_id) "
            "VALUES ($1,$2,$3,$4,$5,$6)",
            principal_binding_id,
            binding_owner_id,
            tenant_binding_id,
            tenant_id,
            principal_ref,
            principal_id,
        )
        await owner.execute(
            "INSERT INTO service_delegation_grants "
            "(id,issuer_service_client_id,binding_owner_service_client_id,max_ttl_seconds) "
            "VALUES ($1,$2,$3,120)",
            grant_id,
            broker_id,
            binding_owner_id,
        )
    old_enabled = settings.delegation_enabled
    settings.delegation_enabled = True
    try:
        yield {
            "owner": owner,
            "provisioner": provisioner,
            "broker_id": broker_id,
            "binding_owner_id": binding_owner_id,
            "broker_credential_id": broker_credential_id,
            "broker_credential": broker_credential,
            "broker_slug": broker_slug,
            "owner_slug": owner_slug,
            "tenant_ref": tenant_ref,
            "principal_ref": principal_ref,
            "tenant_id": tenant_id,
            "principal_id": principal_id,
            "grant_id": grant_id,
        }
    finally:
        settings.delegation_enabled = old_enabled
        try:
            await owner.execute(
                "ALTER TABLE service_delegation_events "
                "DISABLE TRIGGER trg_service_delegation_events_append_only"
            )
            try:
                async with owner.transaction():
                    await owner.execute(
                        "DELETE FROM service_delegation_events "
                        "WHERE issuer_service_client_id=$1 "
                        "OR binding_owner_service_client_id=$2",
                        broker_id,
                        binding_owner_id,
                    )
                    await owner.execute(
                        "DELETE FROM service_delegation_idempotency "
                        "WHERE issuer_service_client_id=$1",
                        broker_id,
                    )
                    await owner.execute(
                        "DELETE FROM service_delegation_tokens "
                        "WHERE issuer_service_client_id=$1",
                        broker_id,
                    )
                    await owner.execute(
                        "DELETE FROM service_delegation_grants "
                        "WHERE issuer_service_client_id=$1 "
                        "AND binding_owner_service_client_id=$2",
                        broker_id,
                        binding_owner_id,
                    )
                    await owner.execute(
                        "DELETE FROM principal_provisioning_bindings "
                        "WHERE service_client_id=$1",
                        binding_owner_id,
                    )
                    await owner.execute(
                        "DELETE FROM tenant_provisioning_bindings "
                        "WHERE service_client_id=$1",
                        binding_owner_id,
                    )
                    await owner.execute(
                        "DELETE FROM service_client_credentials "
                        "WHERE service_client_id=ANY($1::uuid[])",
                        [broker_id, binding_owner_id],
                    )
                    await owner.execute(
                        "DELETE FROM service_clients WHERE id=ANY($1::uuid[])",
                        [broker_id, binding_owner_id],
                    )
                    await owner.execute(
                        "DELETE FROM principals WHERE id=$1", principal_id
                    )
                    await owner.execute("DELETE FROM tenants WHERE id=$1", tenant_id)
            finally:
                await owner.execute(
                    "ALTER TABLE service_delegation_events "
                    "ENABLE TRIGGER trg_service_delegation_events_append_only"
                )
        finally:
            await provisioner.close()
            await owner.close()


async def _issue(
    proof: dict[str, Any],
    *,
    external_ref: str,
    idempotency_digest: bytes,
    request_digest: bytes,
    ttl_seconds: int = 60,
    request_id: str | None = None,
) -> tuple[Any, Any]:
    material = generate_delegation_token()
    provisioner = proof["provisioner"]
    async with provisioner.transaction():
        await provisioner.execute(
            "SELECT set_config('app.service_client_id',$1,true)",
            str(proof["broker_id"]),
        )
        row = await provisioner.fetchrow(
            "SELECT * FROM issue_service_delegation("
            "$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)",
            proof["broker_credential_id"],
            proof["owner_slug"],
            proof["tenant_ref"],
            proof["principal_ref"],
            external_ref,
            idempotency_digest,
            request_digest,
            material.key_id,
            material.secret_digest,
            ttl_seconds,
            request_id or f"request-{uuid.uuid4().hex}",
        )
    assert row is not None
    return material, row


async def test_issue_replay_storage_and_single_use(delegation_db) -> None:  # type: ignore[no-untyped-def]
    proof = delegation_db
    external_ref = f"delegation-{uuid.uuid4().hex}"
    material, issued = await _issue(
        proof,
        external_ref=external_ref,
        idempotency_digest=b"i" * 32,
        request_digest=b"r" * 32,
    )
    assert dict(issued) == {
        "created": True,
        "idempotency_replayed": False,
        "credential_secret_available": True,
        "delegation_token_id": issued["delegation_token_id"],
        "issued_at": issued["issued_at"],
        "expires_at": issued["expires_at"],
        "error_code": None,
    }
    stored = await proof["owner"].fetchrow(
        "SELECT key_id,secret_digest,status,scopes,audience FROM service_delegation_tokens "
        "WHERE id=$1",
        issued["delegation_token_id"],
    )
    assert stored is not None
    assert stored["key_id"] == material.key_id
    assert stored["secret_digest"] == material.secret_digest
    assert material.token not in str(dict(stored))
    assert stored["scopes"] == ["read"]
    assert stored["audience"] == "engram-core"

    _discarded, replay = await _issue(
        proof,
        external_ref=external_ref,
        idempotency_digest=b"i" * 32,
        request_digest=b"r" * 32,
    )
    assert replay["created"] is False
    assert replay["idempotency_replayed"] is True
    assert replay["credential_secret_available"] is False
    assert replay["delegation_token_id"] == issued["delegation_token_id"]

    principal = await resolve_delegated_principal(material.token, request_id="single-use-first")
    assert principal.tenant_id == str(proof["tenant_id"])
    assert principal.principal_id == str(proof["principal_id"])
    assert principal.scopes == ("read",)
    assert principal.api_key_id is None
    assert principal.memory_profile_id is None
    with pytest.raises(HTTPException) as exc:
        await resolve_delegated_principal(material.token, request_id="single-use-second")
    assert exc.value.status_code == 401
    event_counts = await proof["owner"].fetch(
        "SELECT event_type,count(*) AS count FROM service_delegation_events "
        "WHERE delegation_token_id=$1 GROUP BY event_type ORDER BY event_type",
        issued["delegation_token_id"],
    )
    assert [tuple(row.values()) for row in event_counts] == [
        ("delegation.denied", 1),
        ("delegation.idempotent_replay", 1),
        ("delegation.issued", 1),
        ("delegation.used", 1),
    ]


async def test_reconciliation_conflicts_and_event_counts(
    delegation_db,  # type: ignore[no-untyped-def]
) -> None:
    proof = delegation_db
    external_ref = f"event-sequence-{uuid.uuid4().hex}"
    first_key = os.urandom(32)
    first_request_id = f"event-issued-{uuid.uuid4().hex}"
    _material, issued = await _issue(
        proof,
        external_ref=external_ref,
        idempotency_digest=first_key,
        request_digest=os.urandom(32),
        request_id=first_request_id,
    )

    replay_request_id = f"event-replay-{uuid.uuid4().hex}"
    _discarded, replayed = await _issue(
        proof,
        external_ref=external_ref,
        idempotency_digest=first_key,
        request_digest=os.urandom(32),
        request_id=replay_request_id,
    )
    assert replayed["idempotency_replayed"] is True

    reconciliation_request_id = f"event-reconcile-{uuid.uuid4().hex}"
    _discarded, reconciled = await _issue(
        proof,
        external_ref=external_ref,
        idempotency_digest=os.urandom(32),
        request_digest=os.urandom(32),
        request_id=reconciliation_request_id,
    )
    assert reconciled["created"] is False
    assert reconciled["idempotency_replayed"] is False
    assert reconciled["delegation_token_id"] == issued["delegation_token_id"]

    idempotency_conflict_request_id = f"event-idem-conflict-{uuid.uuid4().hex}"
    changed_external_ref = f"changed-{uuid.uuid4().hex}"
    _discarded, idempotency_conflict = await _issue(
        proof,
        external_ref=changed_external_ref,
        idempotency_digest=first_key,
        request_digest=os.urandom(32),
        request_id=idempotency_conflict_request_id,
    )
    assert idempotency_conflict["error_code"] == "IDEMPOTENCY_KEY_REUSED"

    external_conflict_request_id = f"event-external-conflict-{uuid.uuid4().hex}"
    _discarded, external_conflict = await _issue(
        proof,
        external_ref=external_ref,
        idempotency_digest=os.urandom(32),
        request_digest=os.urandom(32),
        ttl_seconds=30,
        request_id=external_conflict_request_id,
    )
    assert external_conflict["error_code"] == "DELEGATION_EXTERNAL_REF_CONFLICT"

    rows = await proof["owner"].fetch(
        "SELECT request_id,event_type,outcome FROM service_delegation_events "
        "WHERE request_id=ANY($1::text[]) ORDER BY request_id",
        [
            first_request_id,
            replay_request_id,
            reconciliation_request_id,
            idempotency_conflict_request_id,
            external_conflict_request_id,
        ],
    )
    assert {(row["request_id"], row["event_type"], row["outcome"]) for row in rows} == {
        (first_request_id, "delegation.issued", "success"),
        (replay_request_id, "delegation.idempotent_replay", "success"),
        (reconciliation_request_id, "delegation.resolved_existing", "success"),
        (idempotency_conflict_request_id, "delegation.conflict", "failure"),
        (external_conflict_request_id, "delegation.conflict", "failure"),
    }
    assert await proof["owner"].fetchval(
        "SELECT count(*) FROM service_delegation_tokens "
        "WHERE issuer_service_client_id=$1",
        proof["broker_id"],
    ) == 1
    assert await proof["owner"].fetchval(
        "SELECT count(*) FROM service_delegation_idempotency "
        "WHERE issuer_service_client_id=$1",
        proof["broker_id"],
    ) == 2
    assert await proof["owner"].fetchval(
        "SELECT count(*) FROM service_delegation_tokens WHERE external_ref=$1",
        changed_external_ref,
    ) == 0


@pytest.mark.parametrize("mode", ["same_request", "same_external", "changed_request"])
async def test_concurrent_issue_serializes_without_duplicate_tokens(
    delegation_db, mode: str  # type: ignore[no-untyped-def]
) -> None:
    proof = delegation_db
    provisioner_url = os.environ["ENGRAM_PROVISIONER_DATABASE_URL"]
    shared_external_ref = f"concurrent-issue-{uuid.uuid4().hex}"
    shared_idempotency_digest = os.urandom(32)

    async def attempt(number: int) -> tuple[Any, Any]:
        conn = await _connect(provisioner_url)
        material = generate_delegation_token()
        external_ref = (
            f"{shared_external_ref}-{number}"
            if mode == "changed_request"
            else shared_external_ref
        )
        idempotency_digest = (
            os.urandom(32) if mode == "same_external" else shared_idempotency_digest
        )
        try:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.service_client_id',$1,true)",
                    str(proof["broker_id"]),
                )
                row = await conn.fetchrow(
                    "SELECT * FROM issue_service_delegation("
                    "$1,$2,$3,$4,$5,$6,$7,$8,$9,60,$10)",
                    proof["broker_credential_id"],
                    proof["owner_slug"],
                    proof["tenant_ref"],
                    proof["principal_ref"],
                    external_ref,
                    idempotency_digest,
                    os.urandom(32),
                    material.key_id,
                    material.secret_digest,
                    f"concurrent-issue-{mode}-{number}-{uuid.uuid4().hex}",
                )
            assert row is not None
            return material, row
        finally:
            await conn.close()

    first, second = await asyncio.gather(attempt(1), attempt(2))
    rows = [first[1], second[1]]
    assert sum(bool(row["created"]) for row in rows) == 1
    if mode == "same_request":
        assert sum(bool(row["idempotency_replayed"]) for row in rows) == 1
    elif mode == "same_external":
        assert all(row["error_code"] is None for row in rows)
        assert sum(
            not row["created"] and not row["idempotency_replayed"] for row in rows
        ) == 1
    else:
        assert sum(row["error_code"] == "IDEMPOTENCY_KEY_REUSED" for row in rows) == 1
    assert await proof["owner"].fetchval(
        "SELECT count(*) FROM service_delegation_tokens "
        "WHERE issuer_service_client_id=$1",
        proof["broker_id"],
    ) == 1
    winner_material = first[0] if first[1]["created"] else second[0]
    principal = await resolve_delegated_principal(
        winner_material.token, request_id=f"concurrent-issue-use-{mode}"
    )
    assert principal.principal_id == str(proof["principal_id"])


async def test_ambiguous_response_replay_revoke_and_reissue(
    delegation_db,  # type: ignore[no-untyped-def]
) -> None:
    proof = delegation_db
    unknown_external_ref = f"ambiguous-unknown-{uuid.uuid4().hex}"
    first_key = os.urandom(32)
    first_material, first = await _issue(
        proof,
        external_ref=unknown_external_ref,
        idempotency_digest=first_key,
        request_digest=os.urandom(32),
    )
    _discarded, replay = await _issue(
        proof,
        external_ref=unknown_external_ref,
        idempotency_digest=first_key,
        request_digest=os.urandom(32),
    )
    assert replay["credential_secret_available"] is False
    assert replay["delegation_token_id"] == first["delegation_token_id"]

    async with proof["provisioner"].transaction():
        await proof["provisioner"].execute(
            "SELECT set_config('app.service_client_id',$1,true)",
            str(proof["broker_id"]),
        )
        revoked = await proof["provisioner"].fetchrow(
            "SELECT * FROM revoke_service_delegation($1,$2,$3,$4,$5,$6,$7)",
            proof["broker_credential_id"],
            proof["owner_slug"],
            proof["tenant_ref"],
            proof["principal_ref"],
            unknown_external_ref,
            "response_uncertain",
            f"ambiguous-revoke-{uuid.uuid4().hex}",
        )
    assert revoked is not None
    assert tuple(revoked.values()) == ("revoked", True, None)
    with pytest.raises(HTTPException):
        await resolve_delegated_principal(
            first_material.token, request_id="ambiguous-old-token-denied"
        )

    replacement_material, replacement = await _issue(
        proof,
        external_ref=f"ambiguous-replacement-{uuid.uuid4().hex}",
        idempotency_digest=os.urandom(32),
        request_digest=os.urandom(32),
    )
    assert replacement["created"] is True
    principal = await resolve_delegated_principal(
        replacement_material.token, request_id="ambiguous-replacement-used"
    )
    assert principal.principal_id == str(proof["principal_id"])


async def test_concurrent_use_has_exactly_one_success(delegation_db) -> None:  # type: ignore[no-untyped-def]
    proof = delegation_db
    material, issued = await _issue(
        proof,
        external_ref=f"race-{uuid.uuid4().hex}",
        idempotency_digest=os.urandom(32),
        request_digest=os.urandom(32),
    )

    async def attempt(number: int) -> bool:
        try:
            await resolve_delegated_principal(
                material.token, request_id=f"concurrent-use-{number}"
            )
            return True
        except HTTPException:
            return False

    outcomes = await asyncio.gather(attempt(1), attempt(2))
    assert sorted(outcomes) == [False, True]


async def test_issue_and_revoke_roll_back_with_caller_transaction(
    delegation_db,  # type: ignore[no-untyped-def]
) -> None:
    proof = delegation_db
    provisioner = proof["provisioner"]
    owner = proof["owner"]
    rollback_external_ref = f"rollback-issue-{uuid.uuid4().hex}"
    rollback_idempotency_digest = os.urandom(32)
    rollback_request_id = f"rollback-issue-{uuid.uuid4().hex}"
    material = generate_delegation_token()
    previous_last_used_at = await owner.fetchval(
        "SELECT last_used_at FROM service_client_credentials WHERE id=$1",
        proof["broker_credential_id"],
    )

    with pytest.raises(RuntimeError, match="force issue rollback"):
        async with provisioner.transaction():
            await provisioner.execute(
                "SELECT set_config('app.service_client_id',$1,true)",
                str(proof["broker_id"]),
            )
            issued = await provisioner.fetchrow(
                "SELECT * FROM issue_service_delegation("
                "$1,$2,$3,$4,$5,$6,$7,$8,$9,60,$10)",
                proof["broker_credential_id"],
                proof["owner_slug"],
                proof["tenant_ref"],
                proof["principal_ref"],
                rollback_external_ref,
                rollback_idempotency_digest,
                os.urandom(32),
                material.key_id,
                material.secret_digest,
                rollback_request_id,
            )
            assert issued is not None
            assert issued["created"] is True
            raise RuntimeError("force issue rollback")

    assert await owner.fetchval(
        "SELECT count(*) FROM service_delegation_tokens WHERE external_ref=$1",
        rollback_external_ref,
    ) == 0
    assert await owner.fetchval(
        "SELECT count(*) FROM service_delegation_idempotency WHERE key_digest=$1",
        rollback_idempotency_digest,
    ) == 0
    assert await owner.fetchval(
        "SELECT count(*) FROM service_delegation_events WHERE request_id=$1",
        rollback_request_id,
    ) == 0
    assert await owner.fetchval(
        "SELECT last_used_at FROM service_client_credentials WHERE id=$1",
        proof["broker_credential_id"],
    ) == previous_last_used_at

    revoke_external_ref = f"rollback-revoke-{uuid.uuid4().hex}"
    _material, committed_issue = await _issue(
        proof,
        external_ref=revoke_external_ref,
        idempotency_digest=os.urandom(32),
        request_digest=os.urandom(32),
    )
    revoke_request_id = f"rollback-revoke-{uuid.uuid4().hex}"
    with pytest.raises(RuntimeError, match="force revoke rollback"):
        async with provisioner.transaction():
            await provisioner.execute(
                "SELECT set_config('app.service_client_id',$1,true)",
                str(proof["broker_id"]),
            )
            revoked = await provisioner.fetchrow(
                "SELECT * FROM revoke_service_delegation($1,$2,$3,$4,$5,$6,$7)",
                proof["broker_credential_id"],
                proof["owner_slug"],
                proof["tenant_ref"],
                proof["principal_ref"],
                revoke_external_ref,
                "operator_action",
                revoke_request_id,
            )
            assert revoked is not None
            assert revoked["disposition"] == "revoked"
            raise RuntimeError("force revoke rollback")

    assert await owner.fetchval(
        "SELECT status FROM service_delegation_tokens WHERE id=$1",
        committed_issue["delegation_token_id"],
    ) == "active"
    assert await owner.fetchval(
        "SELECT count(*) FROM service_delegation_events WHERE request_id=$1",
        revoke_request_id,
    ) == 0


async def test_database_failpoints_leave_no_partial_issue_or_revoke_state(
    delegation_db,  # type: ignore[no-untyped-def]
) -> None:
    import asyncpg

    proof = delegation_db
    owner = proof["owner"]
    baseline_last_used_at = await owner.fetchval(
        "SELECT last_used_at FROM service_client_credentials WHERE id=$1",
        proof["broker_credential_id"],
    )
    issue_stages = (
        ("token", "service_delegation_tokens", "INSERT"),
        ("idempotency", "service_delegation_idempotency", "INSERT"),
        ("event", "service_delegation_events", "INSERT"),
        ("credential", "service_client_credentials", "UPDATE"),
    )
    for stage, table, operation in issue_stages:
        function_name = f"engram_test_fail_delegation_{stage}"
        trigger_name = f"trg_engram_test_fail_delegation_{stage}"
        await owner.execute(
            f"CREATE FUNCTION {function_name}() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'delegation rollback injection'; END; "
            "$$ LANGUAGE plpgsql"
        )
        await owner.execute(
            f"CREATE TRIGGER {trigger_name} AFTER {operation} ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {function_name}()"
        )
        external_ref = f"failpoint-{stage}-{uuid.uuid4().hex}"
        idempotency_digest = os.urandom(32)
        request_id = f"failpoint-{stage}-{uuid.uuid4().hex}"
        try:
            with pytest.raises(asyncpg.PostgresError):
                await _issue(
                    proof,
                    external_ref=external_ref,
                    idempotency_digest=idempotency_digest,
                    request_digest=os.urandom(32),
                    request_id=request_id,
                )
        finally:
            await owner.execute(f"DROP TRIGGER {trigger_name} ON {table}")
            await owner.execute(f"DROP FUNCTION {function_name}()")
        assert await owner.fetchval(
            "SELECT count(*) FROM service_delegation_tokens WHERE external_ref=$1",
            external_ref,
        ) == 0
        assert await owner.fetchval(
            "SELECT count(*) FROM service_delegation_idempotency WHERE key_digest=$1",
            idempotency_digest,
        ) == 0
        assert await owner.fetchval(
            "SELECT count(*) FROM service_delegation_events WHERE request_id=$1",
            request_id,
        ) == 0
        assert await owner.fetchval(
            "SELECT last_used_at FROM service_client_credentials WHERE id=$1",
            proof["broker_credential_id"],
        ) == baseline_last_used_at

    revoke_external_ref = f"failpoint-revoke-{uuid.uuid4().hex}"
    _material, issued = await _issue(
        proof,
        external_ref=revoke_external_ref,
        idempotency_digest=os.urandom(32),
        request_digest=os.urandom(32),
    )
    await owner.execute(
        "CREATE FUNCTION engram_test_fail_delegation_revoke() RETURNS trigger AS $$ "
        "BEGIN RAISE EXCEPTION 'delegation rollback injection'; END; "
        "$$ LANGUAGE plpgsql"
    )
    await owner.execute(
        "CREATE TRIGGER trg_engram_test_fail_delegation_revoke "
        "AFTER UPDATE ON service_delegation_tokens FOR EACH ROW "
        "EXECUTE FUNCTION engram_test_fail_delegation_revoke()"
    )
    revoke_request_id = f"failpoint-revoke-{uuid.uuid4().hex}"
    try:
        with pytest.raises(asyncpg.PostgresError):
            async with proof["provisioner"].transaction():
                await proof["provisioner"].execute(
                    "SELECT set_config('app.service_client_id',$1,true)",
                    str(proof["broker_id"]),
                )
                await proof["provisioner"].fetchrow(
                    "SELECT * FROM revoke_service_delegation($1,$2,$3,$4,$5,$6,$7)",
                    proof["broker_credential_id"],
                    proof["owner_slug"],
                    proof["tenant_ref"],
                    proof["principal_ref"],
                    revoke_external_ref,
                    "operator_action",
                    revoke_request_id,
                )
    finally:
        await owner.execute(
            "DROP TRIGGER trg_engram_test_fail_delegation_revoke "
            "ON service_delegation_tokens"
        )
        await owner.execute("DROP FUNCTION engram_test_fail_delegation_revoke()")
    assert await owner.fetchval(
        "SELECT status FROM service_delegation_tokens WHERE id=$1",
        issued["delegation_token_id"],
    ) == "active"
    assert await owner.fetchval(
        "SELECT count(*) FROM service_delegation_events WHERE request_id=$1",
        revoke_request_id,
    ) == 0


async def test_use_revoke_race_has_only_legal_serializations(
    delegation_db,  # type: ignore[no-untyped-def]
) -> None:
    proof = delegation_db
    external_ref = f"use-revoke-{uuid.uuid4().hex}"
    material, issued = await _issue(
        proof,
        external_ref=external_ref,
        idempotency_digest=os.urandom(32),
        request_digest=os.urandom(32),
    )
    provisioner_url = os.environ["ENGRAM_PROVISIONER_DATABASE_URL"]

    async def use() -> bool:
        try:
            await resolve_delegated_principal(material.token, request_id="use-revoke-use")
            return True
        except HTTPException:
            return False

    async def revoke() -> str:
        conn = await _connect(provisioner_url)
        try:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.service_client_id',$1,true)",
                    str(proof["broker_id"]),
                )
                row = await conn.fetchrow(
                    "SELECT * FROM revoke_service_delegation($1,$2,$3,$4,$5,$6,$7)",
                    proof["broker_credential_id"],
                    proof["owner_slug"],
                    proof["tenant_ref"],
                    proof["principal_ref"],
                    external_ref,
                    "operator_action",
                    "use-revoke-revoke",
                )
            assert row is not None
            return str(row["disposition"])
        finally:
            await conn.close()

    used, disposition = await asyncio.gather(use(), revoke())
    assert (used, disposition) in {
        (True, "already_used"),
        (False, "revoked"),
    }


async def test_revoke_committed_while_use_waits_on_token_lock_wins(
    delegation_db,  # type: ignore[no-untyped-def]
) -> None:
    proof = delegation_db
    material, issued = await _issue(
        proof,
        external_ref=f"blocked-use-{uuid.uuid4().hex}",
        idempotency_digest=os.urandom(32),
        request_digest=os.urandom(32),
    )
    owner_url = os.environ["ENGRAM_OWNER_DATABASE_URL"]
    locker = await _connect(owner_url)
    transaction = locker.transaction()
    await transaction.start()
    committed = False
    try:
        await locker.fetchrow(
            "SELECT id FROM service_delegation_tokens WHERE id=$1 FOR UPDATE",
            issued["delegation_token_id"],
        )
        use_task = asyncio.create_task(
            resolve_delegated_principal(
                material.token, request_id="blocked-use-after-revoke"
            )
        )
        await asyncio.sleep(0.05)
        assert not use_task.done()
        await locker.execute(
            "UPDATE service_delegation_tokens SET status='revoked',"
            "revoked_at=now(),revocation_reason='operator_action' WHERE id=$1",
            issued["delegation_token_id"],
        )
        await transaction.commit()
        committed = True
        with pytest.raises(HTTPException) as exc:
            await use_task
        assert exc.value.status_code == 401
    finally:
        if not committed:
            await transaction.rollback()
        await locker.close()
    assert await proof["owner"].fetchval(
        "SELECT status FROM service_delegation_tokens WHERE id=$1",
        issued["delegation_token_id"],
    ) == "revoked"
    assert await proof["owner"].fetchval(
        "SELECT count(*) FROM service_delegation_events "
        "WHERE request_id='blocked-use-after-revoke' "
        "AND event_type='delegation.denied'"
    ) == 1


async def test_locked_issue_rechecks_credential_and_grant_after_preliminary_auth(
    delegation_db,  # type: ignore[no-untyped-def]
) -> None:
    from fastapi.security import HTTPAuthorizationCredentials

    from engram.service_auth import get_current_service_client

    proof = delegation_db
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer", credentials=proof["broker_credential"]
    )
    identity = await get_current_service_client(credentials)
    assert identity.id == proof["broker_id"]

    await proof["owner"].execute(
        "UPDATE service_client_credentials SET status='revoked',revoked_at=now() "
        "WHERE id=$1",
        proof["broker_credential_id"],
    )
    _discarded, credential_denied = await _issue(
        proof,
        external_ref=f"post-auth-credential-{uuid.uuid4().hex}",
        idempotency_digest=os.urandom(32),
        request_digest=os.urandom(32),
    )
    assert credential_denied["error_code"] == "SERVICE_UNAUTHORIZED"
    await proof["owner"].execute(
        "UPDATE service_client_credentials SET status='active',revoked_at=NULL "
        "WHERE id=$1",
        proof["broker_credential_id"],
    )

    identity = await get_current_service_client(credentials)
    assert identity.id == proof["broker_id"]
    await proof["owner"].execute(
        "UPDATE service_delegation_grants SET status='revoked',revoked_at=now(),"
        "updated_at=now(),revocation_reason='operator_action' WHERE id=$1",
        proof["grant_id"],
    )
    _discarded, grant_denied = await _issue(
        proof,
        external_ref=f"post-auth-grant-{uuid.uuid4().hex}",
        idempotency_digest=os.urandom(32),
        request_digest=os.urandom(32),
    )
    assert grant_denied["error_code"] == "DELEGATION_GRANT_NOT_FOUND"
    assert await proof["owner"].fetchval(
        "SELECT count(*) FROM service_delegation_tokens "
        "WHERE issuer_service_client_id=$1",
        proof["broker_id"],
    ) == 0


@pytest.mark.parametrize(
    "invalidation",
    [
        "grant_revoked",
        "issuer_disabled",
        "binding_owner_disabled",
        "credential_revoked",
        "credential_expired",
        "token_expired",
    ],
)
async def test_authority_invalidation_denies_unused_token(
    delegation_db, invalidation: str  # type: ignore[no-untyped-def]
) -> None:
    proof = delegation_db
    material, issued = await _issue(
        proof,
        external_ref=f"invalidation-{invalidation}-{uuid.uuid4().hex}",
        idempotency_digest=os.urandom(32),
        request_digest=os.urandom(32),
    )
    owner = proof["owner"]
    if invalidation == "grant_revoked":
        await owner.execute(
            "UPDATE service_delegation_grants SET status='revoked',revoked_at=now(),"
            "updated_at=now(),revocation_reason='operator_action' WHERE id=$1",
            proof["grant_id"],
        )
    elif invalidation == "issuer_disabled":
        await owner.execute(
            "UPDATE service_clients SET status='disabled',disabled_at=now() WHERE id=$1",
            proof["broker_id"],
        )
    elif invalidation == "binding_owner_disabled":
        await owner.execute(
            "UPDATE service_clients SET status='disabled',disabled_at=now() WHERE id=$1",
            proof["binding_owner_id"],
        )
    elif invalidation == "credential_revoked":
        await owner.execute(
            "UPDATE service_client_credentials SET status='revoked',revoked_at=now() "
            "WHERE id=$1",
            proof["broker_credential_id"],
        )
    elif invalidation == "credential_expired":
        await owner.execute(
            "UPDATE service_client_credentials SET expires_at=now()-interval '1 second' "
            "WHERE id=$1",
            proof["broker_credential_id"],
        )
    else:
        await owner.execute(
            "UPDATE service_delegation_tokens SET "
            "issued_at=now()-interval '120 seconds',"
            "expires_at=now()-interval '60 seconds' WHERE id=$1",
            issued["delegation_token_id"],
        )

    with pytest.raises(HTTPException) as exc:
        await resolve_delegated_principal(
            material.token, request_id=f"invalidation-{invalidation}"
        )
    assert exc.value.status_code == 401
    if invalidation == "token_expired":
        row = await owner.fetchrow(
            "SELECT status,revocation_reason FROM service_delegation_tokens WHERE id=$1",
            issued["delegation_token_id"],
        )
        assert row is not None
        assert tuple(row.values()) == ("revoked", "expired")


async def test_authority_separation_human_subject_and_binding_integrity(
    delegation_db,  # type: ignore[no-untyped-def]
) -> None:
    import asyncpg

    proof = delegation_db
    owner = proof["owner"]
    assert await owner.fetchval(
        "SELECT permissions FROM service_clients WHERE id=$1", proof["broker_id"]
    ) == ["delegation.issue"]
    assert await owner.fetchval(
        "SELECT permissions FROM service_clients WHERE id=$1",
        proof["binding_owner_id"],
    ) == [
        "tenant.provision",
        "principal.provision",
        "workspace.provision",
        "agent.provision",
        "api_key.provision",
    ]
    owner_credential = generate_service_credential()
    owner_parsed = parse_service_credential(owner_credential)
    owner_credential_id = uuid.uuid4()
    await owner.execute(
        "INSERT INTO service_client_credentials "
        "(id,service_client_id,key_id,secret_digest,digest_algorithm) "
        "VALUES ($1,$2,$3,$4,'sha256')",
        owner_credential_id,
        proof["binding_owner_id"],
        owner_parsed.key_id,
        digest_service_secret(owner_parsed.secret),
    )
    owner_material = generate_delegation_token()
    async with proof["provisioner"].transaction():
        await proof["provisioner"].execute(
            "SELECT set_config('app.service_client_id',$1,true)",
            str(proof["binding_owner_id"]),
        )
        owner_issue = await proof["provisioner"].fetchrow(
            "SELECT * FROM issue_service_delegation("
            "$1,$2,$3,$4,$5,$6,$7,$8,$9,60,$10)",
            owner_credential_id,
            proof["broker_slug"],
            proof["tenant_ref"],
            proof["principal_ref"],
            f"owner-cannot-issue-{uuid.uuid4().hex}",
            os.urandom(32),
            os.urandom(32),
            owner_material.key_id,
            owner_material.secret_digest,
            f"owner-cannot-issue-{uuid.uuid4().hex}",
        )
    assert owner_issue is not None
    assert owner_issue["error_code"] == "SERVICE_FORBIDDEN"

    await owner.execute(
        "UPDATE service_delegation_grants SET status='revoked',revoked_at=now(),"
        "updated_at=now(),revocation_reason='operator_action' WHERE id=$1",
        proof["grant_id"],
    )
    _discarded, no_grant = await _issue(
        proof,
        external_ref=f"no-grant-{uuid.uuid4().hex}",
        idempotency_digest=os.urandom(32),
        request_digest=os.urandom(32),
    )
    assert no_grant["error_code"] == "DELEGATION_GRANT_NOT_FOUND"
    assert await owner.fetchval(
        "SELECT count(*) FROM service_delegation_tokens "
        "WHERE issuer_service_client_id=$1",
        proof["broker_id"],
    ) == 0
    with pytest.raises(
        asyncpg.PostgresError,
        match="revoked delegation grants cannot be reactivated",
    ):
        await owner.execute(
            "UPDATE service_delegation_grants SET status='active',revoked_at=NULL,"
            "updated_at=now(),revocation_reason=NULL WHERE id=$1",
            proof["grant_id"],
        )
    replacement_grant_id = uuid.uuid4()
    await owner.execute(
        "INSERT INTO service_delegation_grants "
        "(id,issuer_service_client_id,binding_owner_service_client_id,max_ttl_seconds) "
        "VALUES ($1,$2,$3,120)",
        replacement_grant_id,
        proof["broker_id"],
        proof["binding_owner_id"],
    )
    proof["grant_id"] = replacement_grant_id

    for principal_type in ("admin", "agent", "system"):
        await owner.execute(
            "UPDATE principals SET type=$2,internal_key=NULL WHERE id=$1",
            proof["principal_id"],
            principal_type,
        )
        _discarded, rejected = await _issue(
            proof,
            external_ref=f"subject-{principal_type}-{uuid.uuid4().hex}",
            idempotency_digest=os.urandom(32),
            request_digest=os.urandom(32),
        )
        assert rejected["error_code"] == "DELEGATION_SUBJECT_INVALID"
    await owner.execute(
        "UPDATE principals SET type='system',internal_key='delegation_test_internal' "
        "WHERE id=$1",
        proof["principal_id"],
    )
    _discarded, internal_rejected = await _issue(
        proof,
        external_ref=f"subject-internal-{uuid.uuid4().hex}",
        idempotency_digest=os.urandom(32),
        request_digest=os.urandom(32),
    )
    assert internal_rejected["error_code"] == "DELEGATION_SUBJECT_INVALID"
    await owner.execute(
        "UPDATE principals SET type='user',internal_key=NULL WHERE id=$1",
        proof["principal_id"],
    )

    material, _issued = await _issue(
        proof,
        external_ref=f"binding-integrity-{uuid.uuid4().hex}",
        idempotency_digest=os.urandom(32),
        request_digest=os.urandom(32),
    )
    await owner.execute(
        "UPDATE principals SET type='admin' WHERE id=$1", proof["principal_id"]
    )
    with pytest.raises(HTTPException) as exc:
        await resolve_delegated_principal(
            material.token, request_id="binding-integrity-denied"
        )
    assert exc.value.status_code == 401
    assert await owner.fetchval(
        "SELECT count(*) FROM service_delegation_events "
        "WHERE request_id='binding-integrity-denied' "
        "AND event_type='delegation.denied'"
    ) == 1
    await owner.execute(
        "UPDATE principals SET type='user' WHERE id=$1", proof["principal_id"]
    )


async def test_explicit_revoke_and_permission_removal_invalidate(
    delegation_db,  # type: ignore[no-untyped-def]
) -> None:
    proof = delegation_db
    external_ref = f"revoke-{uuid.uuid4().hex}"
    material, issued = await _issue(
        proof,
        external_ref=external_ref,
        idempotency_digest=os.urandom(32),
        request_digest=os.urandom(32),
    )
    provisioner = proof["provisioner"]
    async with provisioner.transaction():
        await provisioner.execute(
            "SELECT set_config('app.service_client_id',$1,true)",
            str(proof["broker_id"]),
        )
        revoked = await provisioner.fetchrow(
            "SELECT * FROM revoke_service_delegation($1,$2,$3,$4,$5,$6,$7)",
            proof["broker_credential_id"],
            proof["owner_slug"],
            proof["tenant_ref"],
            proof["principal_ref"],
            external_ref,
            "operator_action",
            f"revoke-request-{uuid.uuid4().hex}",
        )
    assert revoked is not None
    assert revoked["disposition"] == "revoked"
    assert await proof["owner"].fetchval(
        "SELECT count(*) FROM service_delegation_events "
        "WHERE delegation_token_id=$1 AND event_type='delegation.revoked'",
        issued["delegation_token_id"],
    ) == 1
    with pytest.raises(HTTPException):
        await resolve_delegated_principal(material.token, request_id="explicitly-revoked")

    permission_material, _issued = await _issue(
        proof,
        external_ref=f"permission-{uuid.uuid4().hex}",
        idempotency_digest=os.urandom(32),
        request_digest=os.urandom(32),
    )
    await proof["owner"].execute(
        "UPDATE service_clients SET permissions=$2 WHERE id=$1",
        proof["broker_id"],
        ["tenant.provision"],
    )
    with pytest.raises(HTTPException) as exc:
        await resolve_delegated_principal(
            permission_material.token, request_id="permission-removed"
        )
    assert exc.value.status_code == 401


async def test_provisioner_has_function_execute_but_no_delegation_table_dml(
    delegation_db,  # type: ignore[no-untyped-def]
) -> None:
    conn = delegation_db["provisioner"]
    role = await conn.fetchrow(
        "SELECT rolcanlogin,rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,"
        "rolreplication,rolinherit FROM pg_roles WHERE rolname=current_user"
    )
    assert role is not None
    assert tuple(role.values()) == (True, False, False, False, False, False, False)
    assert await conn.fetchval(
        "SELECT count(*) FROM pg_auth_members membership "
        "JOIN pg_roles member ON member.oid=membership.member "
        "WHERE member.rolname=current_user"
    ) == 0
    assert await conn.fetchval(
        "SELECT has_schema_privilege(current_user,'public','USAGE')"
    )
    assert not await conn.fetchval(
        "SELECT has_schema_privilege(current_user,'public','CREATE')"
    )
    assert await conn.fetchval(
        "SELECT count(*) FROM pg_class relation "
        "JOIN pg_roles owner ON owner.oid=relation.relowner "
        "JOIN pg_namespace namespace ON namespace.oid=relation.relnamespace "
        "WHERE namespace.nspname='public' AND owner.rolname=current_user"
    ) == 0
    assert await conn.fetchval(
        "SELECT count(*) FROM pg_proc function "
        "JOIN pg_roles owner ON owner.oid=function.proowner "
        "JOIN pg_namespace namespace ON namespace.oid=function.pronamespace "
        "WHERE namespace.nspname='public' AND owner.rolname=current_user"
    ) == 0
    for table in (
        "service_delegation_grants",
        "service_delegation_tokens",
        "service_delegation_idempotency",
        "service_delegation_events",
    ):
        assert await conn.fetchval(
            "SELECT has_table_privilege(current_user,$1,'SELECT')", table
        ) is False
        assert await conn.fetchval(
            "SELECT has_table_privilege(current_user,$1,'INSERT')", table
        ) is False
        assert await conn.fetchval(
            "SELECT has_table_privilege(current_user,$1,'UPDATE')", table
        ) is False
        assert await conn.fetchval(
            "SELECT has_table_privilege(current_user,$1,'DELETE')", table
        ) is False
    assert await conn.fetchval(
        "SELECT has_function_privilege(current_user,"
        "'issue_service_delegation(uuid,text,text,text,text,bytea,bytea,text,text,"
        "integer,text)','EXECUTE')"
    )
    assert await conn.fetchval(
        "SELECT has_function_privilege(current_user,"
        "'revoke_service_delegation(uuid,text,text,text,text,text,text)','EXECUTE')"
    )
    function_acl = await conn.fetch(
        "SELECT proname,"
        "has_function_privilege('engram_app',function.oid,'EXECUTE') AS app_execute,"
        "has_function_privilege(current_user,function.oid,'EXECUTE') AS provisioner_execute "
        "FROM pg_proc function "
        "JOIN pg_namespace namespace ON namespace.oid=function.pronamespace "
        "WHERE namespace.nspname='public' AND proname=ANY($1::text[]) "
        "ORDER BY proname",
        [
            "enforce_service_delegation_grant_history",
            "issue_service_delegation",
            "reject_service_delegation_event_mutation",
            "revoke_service_delegation",
            "validate_service_delegation_token_subject",
        ],
    )
    assert [tuple(row.values()) for row in function_acl] == [
        ("enforce_service_delegation_grant_history", False, False),
        ("issue_service_delegation", False, True),
        ("reject_service_delegation_event_mutation", False, False),
        ("revoke_service_delegation", False, True),
        ("validate_service_delegation_token_subject", False, False),
    ]


async def test_service_api_contract_and_scope_denials(
    delegation_db,  # type: ignore[no-untyped-def]
) -> None:
    proof = delegation_db
    settings.auth_enabled = True

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        provisioning_denied = await client.post(
            "/v1/service/provisioning/tenant-human",
            headers={
                "Authorization": f"Bearer {proof['broker_credential']}",
                "Idempotency-Key": f"broker-cannot-provision-{uuid.uuid4().hex}",
            },
            json={
                "tenant": {
                    "external_ref": f"broker-tenant-{uuid.uuid4().hex}",
                    "name": "Broker must not provision",
                    "slug": f"broker-must-not-provision-{uuid.uuid4().hex[:12]}",
                },
                "human_principal": {
                    "external_ref": f"broker-human-{uuid.uuid4().hex}",
                    "name": "Broker must not provision",
                },
            },
        )
        assert provisioning_denied.status_code == 403

        async def issue() -> str:
            idempotency_key = f"idem-{uuid.uuid4().hex}"
            body = {
                "binding_owner_service_client_slug": proof["owner_slug"],
                "tenant_external_ref": proof["tenant_ref"],
                "principal_external_ref": proof["principal_ref"],
                "delegation_external_ref": f"api-delegation-{uuid.uuid4().hex}",
                "ttl_seconds": 60,
            }
            headers = {
                "Authorization": f"Bearer {proof['broker_credential']}",
                "Idempotency-Key": idempotency_key,
                "X-Request-ID": f"api-issue-{uuid.uuid4().hex}",
            }
            response = await client.post(
                "/v1/service/delegations",
                headers=headers,
                json=body,
            )
            assert response.status_code == 201, response.text
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["pragma"] == "no-cache"
            assert response.headers["referrer-policy"] == "no-referrer"
            assert response.headers["idempotency-replayed"] == "false"
            payload = response.json()
            assert payload["created"] is True
            assert payload["credential_secret_available"] is True
            assert payload["scopes"] == ["read"]
            assert payload["audience"] == "engram-core"
            assert payload["single_use"] is True
            assert isinstance(payload["token"], str)

            replay = await client.post(
                "/v1/service/delegations", headers=headers, json=body
            )
            assert replay.status_code == 200
            assert replay.headers["idempotency-replayed"] == "true"
            assert replay.json()["created"] is False
            assert replay.json()["credential_secret_available"] is False
            assert replay.json()["token"] is None
            assert replay.json()["expires_at"] == payload["expires_at"]

            reconciliation_headers = {
                **headers,
                "Idempotency-Key": f"idem-{uuid.uuid4().hex}",
            }
            reconciliation = await client.post(
                "/v1/service/delegations",
                headers=reconciliation_headers,
                json=body,
            )
            assert reconciliation.status_code == 200
            assert reconciliation.headers["idempotency-replayed"] == "false"
            assert reconciliation.json()["created"] is False
            assert reconciliation.json()["token"] is None
            assert reconciliation.json()["expires_at"] == payload["expires_at"]
            return payload["token"]

        read_token = await issue()
        whoami = await client.get(
            "/whoami", headers={"Authorization": f"Bearer {read_token}"}
        )
        assert whoami.status_code == 200
        assert whoami.json()["principal_id"] == str(proof["principal_id"])
        assert whoami.json()["tenant_id"] == str(proof["tenant_id"])
        assert whoami.json()["scopes"] == ["read"]
        assert whoami.json()["api_key_id"] is None

        denial_matrix = (
            ("POST", "/v1/remember"),
            ("GET", "/v1/review/queue"),
            ("GET", "/v1/export/cca"),
            ("GET", "/v1/admin/memory-kinds"),
        )
        for method, path in denial_matrix:
            token = await issue()
            denied = await client.request(
                method,
                path,
                headers={"Authorization": f"Bearer {token}"},
                json={"content": "delegated write must not execute"}
                if method == "POST"
                else None,
            )
            assert denied.status_code == 403, (method, path, denied.text)
            replay = await client.get(
                "/whoami", headers={"Authorization": f"Bearer {token}"}
            )
            assert replay.status_code == 401

        service_token = await issue()
        service_denied = await client.post(
            "/v1/service/delegations/revoke",
            headers={"Authorization": f"Bearer {service_token}"},
            json={
                "binding_owner_service_client_slug": proof["owner_slug"],
                "tenant_external_ref": proof["tenant_ref"],
                "principal_external_ref": proof["principal_ref"],
                "delegation_external_ref": "irrelevant",
                "reason": "operator_action",
            },
        )
        assert service_denied.status_code == 401


async def test_owner_cli_grant_replay_revoke_and_replacement(
    delegation_db,  # type: ignore[no-untyped-def]
) -> None:
    proof = delegation_db
    database_url = os.environ["ENGRAM_OWNER_DATABASE_URL"]
    broker_slug = await proof["owner"].fetchval(
        "SELECT slug FROM service_clients WHERE id=$1", proof["broker_id"]
    )

    def command(name: str, ttl: int | None = None) -> argparse.Namespace:
        return argparse.Namespace(
            delegation_grant_command=name,
            issuer=broker_slug,
            binding_owner=proof["owner_slug"],
            max_ttl_seconds=ttl,
            reason="operator_action",
            json=True,
        )

    replay = command("create", 120)
    assert await _run_delegation_grant(replay, database_url) == 0

    changed = command("create", 60)
    assert await _run_delegation_grant(changed, database_url) == 1

    revoke = command("revoke")
    assert await _run_delegation_grant(revoke, database_url) == 0
    assert await _run_delegation_grant(revoke, database_url) == 0

    replacement = command("create", 60)
    assert await _run_delegation_grant(replacement, database_url) == 0
    assert (
        await proof["owner"].fetchval(
            "SELECT count(*) FROM service_delegation_grants "
            "WHERE issuer_service_client_id=$1 "
            "AND binding_owner_service_client_id=$2",
            proof["broker_id"],
            proof["binding_owner_id"],
        )
        == 2
    )
    grant_events = await proof["owner"].fetch(
        "SELECT event_type,count(*) AS count FROM service_delegation_events "
        "WHERE issuer_service_client_id=$1 "
        "AND event_type LIKE 'delegation_grant.%' "
        "GROUP BY event_type ORDER BY event_type",
        proof["broker_id"],
    )
    assert [tuple(row.values()) for row in grant_events] == [
        ("delegation_grant.created", 1),
        ("delegation_grant.revoked", 1),
    ]
