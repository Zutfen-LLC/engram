"""Real PostgreSQL certification for service-authenticated provisioning."""
# ruff: noqa: E501

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from engram.api.app import create_app
from engram.db import require_provisioner_session_factory
from engram.migrations import normalize_asyncpg_url
from engram.service_auth import (
    digest_service_secret,
    generate_service_credential,
    get_current_service_client,
    lock_and_validate_service_authority,
    parse_service_credential,
)

pytestmark = pytest.mark.asyncio


def _owner_url() -> str | None:
    return os.getenv("ENGRAM_OWNER_DATABASE_URL") or os.getenv("ENGRAM_DATABASE_URL")


def _provisioner_url() -> str | None:
    return os.getenv("ENGRAM_PROVISIONER_DATABASE_URL")


async def _connect(url: str):  # type: ignore[no-untyped-def]
    import asyncpg

    return await asyncpg.connect(normalize_asyncpg_url(url))


def _body(tag: str, *, name: str | None = None, slug: str | None = None) -> dict[str, Any]:
    return {
        "tenant": {
            "external_ref": f"tenant-ref-{tag}",
            "name": name or f"Tenant {tag}",
            "slug": slug or f"tenant-{tag}",
        },
        "human_principal": {"external_ref": f"human-ref-{tag}", "name": f"Human {tag}"},
    }


def _assert_service_headers(response, request_id: str | None = None) -> None:  # type: ignore[no-untyped-def]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"
    if request_id is not None:
        assert response.headers["x-request-id"] == request_id
    else:
        assert uuid.UUID(response.headers["x-request-id"])


@pytest.fixture
async def service_stack(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict[str, Any]]:
    """A unique real service client plus owner inspection connection."""
    owner_url, provisioner_url = _owner_url(), _provisioner_url()
    if not owner_url or not provisioner_url:
        pytest.skip("requires owner and provisioner PostgreSQL URLs")
    try:
        owner = await _connect(owner_url)
        provisioner = await _connect(provisioner_url)
    except Exception:
        pytest.skip("requires migrated PostgreSQL with the provisioner role")
    await provisioner.close()

    from engram.config import settings

    monkeypatch.setattr(settings, "service_provisioning_enabled", True)
    tag = uuid.uuid4().hex[:16]
    slug = f"service-proof-{tag}"
    credential = generate_service_credential()
    parsed = parse_service_credential(credential)
    client_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    await owner.execute(
        "INSERT INTO service_clients (id, slug, display_name, permissions) VALUES ($1, $2, $3, $4)",
        client_id,
        slug,
        f"Service proof {tag}",
        ["tenant.provision", "principal.provision"],
    )
    await owner.execute(
        "INSERT INTO service_client_credentials "
        "(id, service_client_id, key_id, secret_digest, digest_algorithm) "
        "VALUES ($1, $2, $3, $4, 'sha256')",
        credential_id,
        client_id,
        parsed.key_id,
        digest_service_secret(parsed.secret),
    )
    client = AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")
    stack: dict[str, Any] = {
        "owner": owner,
        "client": client,
        "client_id": client_id,
        "credential_id": credential_id,
        "credential": credential,
        "tag": tag,
    }
    try:
        yield stack
    finally:
        await client.aclose()
        tenant_ids = await owner.fetch(
            "SELECT tenant_id FROM tenant_provisioning_bindings WHERE service_client_id=$1",
            client_id,
        )
        async with owner.transaction():
            await owner.execute("DELETE FROM service_provisioning_events WHERE service_client_id=$1", client_id)
            await owner.execute(
                "DELETE FROM service_provisioning_idempotency WHERE service_client_id=$1", client_id
            )
            await owner.execute(
                "DELETE FROM principal_provisioning_bindings WHERE service_client_id=$1", client_id
            )
            await owner.execute(
                "DELETE FROM tenant_provisioning_bindings WHERE service_client_id=$1", client_id
            )
            await owner.execute(
                "DELETE FROM service_client_credentials WHERE service_client_id=$1", client_id
            )
            await owner.execute("DELETE FROM service_clients WHERE id=$1", client_id)
            for row in tenant_ids:
                await owner.execute("DELETE FROM tenants WHERE id=$1", row["tenant_id"])
        await owner.close()


def _headers(stack: dict[str, Any], key: str, request_id: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {stack['credential']}",
        "Idempotency-Key": key,
    }
    if request_id is not None:
        headers["X-Request-ID"] = request_id
    return headers


async def test_real_service_auth_failures_are_generic_and_immediate(service_stack) -> None:  # type: ignore[no-untyped-def]
    stack = service_stack
    owner, client = stack["owner"], stack["client"]
    body = _body(stack["tag"])
    malformed = "eng_not_a_service_credential"
    wrong_secret = stack["credential"][:-1] + ("A" if stack["credential"][-1] != "A" else "B")
    unknown = "engsvc_" + "z" * 22 + "_" + "A" * 43

    for token in (malformed, wrong_secret, unknown):
        response = await client.post(
            "/v1/service/provisioning/tenant-human",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": f"auth-{uuid.uuid4()}"},
            json=body,
        )
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "SERVICE_UNAUTHORIZED"
        assert response.headers["www-authenticate"] == "Bearer"
        _assert_service_headers(response)

    transitions = (
        ("UPDATE service_client_credentials SET status='revoked', revoked_at=now() WHERE id=$1", stack["credential_id"]),
        ("UPDATE service_client_credentials SET expires_at=$2 WHERE id=$1", stack["credential_id"], datetime.now(UTC) - timedelta(seconds=1)),
        ("UPDATE service_clients SET status='disabled', disabled_at=now() WHERE id=$1", stack["client_id"]),
    )
    for transition in transitions:
        await owner.execute(*transition)
        response = await client.post(
            "/v1/service/provisioning/tenant-human",
            headers=_headers(stack, f"auth-{uuid.uuid4()}"),
            json=body,
        )
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "SERVICE_UNAUTHORIZED"
        _assert_service_headers(response)
        await owner.execute(
            "UPDATE service_client_credentials SET status='active', revoked_at=NULL, expires_at=NULL WHERE id=$1",
            stack["credential_id"],
        )
        await owner.execute(
            "UPDATE service_clients SET status='active', disabled_at=NULL WHERE id=$1", stack["client_id"]
        )


async def test_provisioner_session_reads_real_service_credential(service_stack) -> None:  # type: ignore[no-untyped-def]
    stack = service_stack
    async with require_provisioner_session_factory()() as session:
        row = (
            await session.execute(
                text(
                    "SELECT current_user, c.id FROM service_client_credentials c "
                    "WHERE c.id = :credential_id"
                ),
                {"credential_id": stack["credential_id"]},
            )
        ).mappings().one()
    assert row["current_user"] == "engram_provisioner"
    assert row["id"] == stack["credential_id"]


async def test_real_provisioning_contract_replay_resolution_and_audit(service_stack) -> None:  # type: ignore[no-untyped-def]
    stack = service_stack
    owner, client, tag = stack["owner"], stack["client"], stack["tag"]
    body, key = _body(tag), f"idem-{tag}"
    first = await client.post(
        "/v1/service/provisioning/tenant-human",
        headers=_headers(stack, key, "caller-request-id"),
        json=body,
    )
    assert first.status_code == 201
    _assert_service_headers(first, "caller-request-id")
    first_body = first.json()
    tenant_id, principal_id = first_body["tenant"]["id"], first_body["principal"]["id"]
    assert first_body["principal"]["tenant_id"] == tenant_id
    assert first_body["principal"]["type"] == "user"
    assert await owner.fetchval(
        "SELECT count(*) FROM tenant_provisioning_bindings WHERE service_client_id=$1", stack["client_id"]
    ) == 1
    assert await owner.fetchval(
        "SELECT count(*) FROM principal_provisioning_bindings WHERE service_client_id=$1", stack["client_id"]
    ) == 1
    assert await owner.fetchval(
        "SELECT count(*) FROM tenant_config WHERE tenant_id=$1 AND active AND auto_promote_evidence_enabled",
        uuid.UUID(tenant_id),
    ) == 1
    assert await owner.fetchval("SELECT count(*) FROM memory_kinds WHERE tenant_id=$1", uuid.UUID(tenant_id)) == 9
    assert await owner.fetchval(
        "SELECT last_used_at IS NOT NULL FROM service_client_credentials WHERE id=$1", stack["credential_id"]
    ) is True

    replay = await client.post(
        "/v1/service/provisioning/tenant-human", headers=_headers(stack, key), json=body
    )
    assert replay.status_code == 200
    assert replay.headers["idempotency-replayed"] == "true"
    assert replay.json()["tenant"]["id"] == tenant_id
    assert replay.json()["principal"]["id"] == principal_id
    _assert_service_headers(replay)

    resolved = await client.post(
        "/v1/service/provisioning/tenant-human", headers=_headers(stack, f"new-{tag}"), json=body
    )
    assert resolved.status_code == 200
    assert resolved.json()["tenant"]["id"] == tenant_id
    assert resolved.json()["principal"]["id"] == principal_id
    for event_type in ("tenant.provisioned", "principal.provisioned", "provisioning.idempotent_replay", "provisioning.resolved_existing"):
        event = await owner.fetchrow(
            "SELECT external_tenant_ref_digest, external_principal_ref_digest, details::text "
            "FROM service_provisioning_events WHERE service_client_id=$1 AND event_type=$2 "
            "ORDER BY created_at DESC LIMIT 1",
            stack["client_id"],
            event_type,
        )
        assert event is not None
        assert "tenant-ref" not in event["details"]
        assert event["external_tenant_ref_digest"] is not None
        assert event["external_principal_ref_digest"] is not None

    conflict_body = _body(tag, name="Changed tenant")
    conflict = await client.post(
        "/v1/service/provisioning/tenant-human",
        headers=_headers(stack, f"conflict-{tag}"),
        json=conflict_body,
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "PROVISIONING_CONFLICT"
    _assert_service_headers(conflict)
    conflict_event = await owner.fetchrow(
        "SELECT outcome, reason_code, external_tenant_ref_digest, external_principal_ref_digest, details::text "
        "FROM service_provisioning_events WHERE service_client_id=$1 AND event_type='provisioning.conflict' "
        "ORDER BY created_at DESC LIMIT 1",
        stack["client_id"],
    )
    assert conflict_event is not None
    assert conflict_event["outcome"] == "failure"
    assert conflict_event["reason_code"] == "PROVISIONING_CONFLICT"
    assert conflict_event["external_tenant_ref_digest"] is not None
    assert conflict_event["external_principal_ref_digest"] is not None
    assert f"tenant-ref-{tag}" not in conflict_event["details"]
    events = await owner.fetch(
        "SELECT service_client_id, credential_id, tenant_id, principal_id, event_type, outcome, request_id, "
        "reason_code, external_tenant_ref_digest, external_principal_ref_digest, details::text "
        "FROM service_provisioning_events WHERE service_client_id=$1",
        stack["client_id"],
    )
    serialized_events = "\n".join(str(dict(event)) for event in events)
    for forbidden in (
        body["tenant"]["external_ref"],
        body["human_principal"]["external_ref"],
        body["tenant"]["name"],
        body["human_principal"]["name"],
        key,
        stack["credential"],
    ):
        assert forbidden not in serialized_events


async def test_locked_authority_revalidation_rejects_changed_state(service_stack) -> None:  # type: ignore[no-untyped-def]
    stack = service_stack
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=stack["credential"])
    identity = await get_current_service_client(credentials)
    await stack["owner"].execute(
        "UPDATE service_clients SET permissions=ARRAY['tenant.provision'] WHERE id=$1", stack["client_id"]
    )
    async with require_provisioner_session_factory()() as session, session.begin():
        with pytest.raises(HTTPException) as exc_info:
            await lock_and_validate_service_authority(
                session, identity, ("tenant.provision", "principal.provision")
            )
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["code"] == "SERVICE_UNAUTHORIZED"


async def test_database_rejects_ambiguous_service_client_display_names(service_stack) -> None:  # type: ignore[no-untyped-def]
    import asyncpg

    stack = service_stack
    for suffix, display_name in (("blank", "   "), ("leading", " Control")):
        with pytest.raises(asyncpg.CheckViolationError):
            await stack["owner"].execute(
                "INSERT INTO service_clients (slug,display_name,permissions) VALUES ($1,$2,$3)",
                f"invalid-name-{suffix}-{stack['tag']}",
                display_name,
                ["tenant.provision"],
            )


@pytest.mark.parametrize("keys", [("same", "same"), ("first", "second")])
async def test_concurrent_provisioning_creates_one_resource_set(service_stack, keys) -> None:  # type: ignore[no-untyped-def]
    """Independent ASGI requests exercise advisory-lock serialization on PostgreSQL."""
    stack = service_stack
    body = _body(stack["tag"])
    start = asyncio.Event()

    async def invoke(key_suffix: str):  # type: ignore[no-untyped-def]
        await start.wait()
        return await stack["client"].post(
            "/v1/service/provisioning/tenant-human",
            headers=_headers(stack, f"concurrent-{key_suffix}-{stack['tag']}"),
            json=body,
        )

    first = asyncio.create_task(invoke(keys[0]))
    second = asyncio.create_task(invoke(keys[1]))
    start.set()
    responses = await asyncio.gather(first, second)
    assert {response.status_code for response in responses} <= {200, 201}
    assert 201 in {response.status_code for response in responses}
    assert await stack["owner"].fetchval(
        "SELECT count(*) FROM tenant_provisioning_bindings WHERE service_client_id=$1", stack["client_id"]
    ) == 1
    assert await stack["owner"].fetchval(
        "SELECT count(*) FROM principal_provisioning_bindings WHERE service_client_id=$1", stack["client_id"]
    ) == 1
    expected_idempotency = 1 if keys[0] == keys[1] else 2
    assert await stack["owner"].fetchval(
        "SELECT count(*) FROM service_provisioning_idempotency WHERE service_client_id=$1", stack["client_id"]
    ) == expected_idempotency


async def test_unexpected_initialization_failure_rolls_back_every_resource(
    service_stack, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    import engram.provisioning as provisioning

    async def fail_initialization(*_args: object) -> None:
        raise RuntimeError("injected initialization failure")

    stack = service_stack
    monkeypatch.setattr(provisioning, "initialize_tenant", fail_initialization)
    response = await stack["client"].post(
        "/v1/service/provisioning/tenant-human",
        headers=_headers(stack, f"rollback-{stack['tag']}"),
        json=_body(stack["tag"]),
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "SERVICE_UNAVAILABLE"
    _assert_service_headers(response)
    for table in (
        "tenant_provisioning_bindings",
        "principal_provisioning_bindings",
        "service_provisioning_idempotency",
        "service_provisioning_events",
    ):
        assert await stack["owner"].fetchval(
            f"SELECT count(*) FROM {table} WHERE service_client_id=$1", stack["client_id"]
        ) == 0
