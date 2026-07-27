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

pytestmark = [pytest.mark.asyncio, pytest.mark.service_provisioning_postgres]


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
            await owner.execute(
                "DELETE FROM service_provisioning_events WHERE service_client_id=$1", client_id
            )
            await owner.execute(
                "DELETE FROM service_agent_key_idempotency WHERE service_client_id=$1", client_id
            )
            await owner.execute(
                "DELETE FROM service_workspace_agent_idempotency "
                "WHERE service_client_id=$1",
                client_id,
            )
            await owner.execute(
                "DELETE FROM agent_api_key_provisioning_bindings "
                "WHERE service_client_id=$1",
                client_id,
            )
            await owner.execute(
                "DELETE FROM workspace_provisioning_bindings WHERE service_client_id=$1",
                client_id,
            )
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
        (
            "UPDATE service_client_credentials SET status='revoked', revoked_at=now() WHERE id=$1",
            stack["credential_id"],
        ),
        (
            "UPDATE service_client_credentials SET expires_at=$2 WHERE id=$1",
            stack["credential_id"],
            datetime.now(UTC) - timedelta(seconds=1),
        ),
        (
            "UPDATE service_clients SET status='disabled', disabled_at=now() WHERE id=$1",
            stack["client_id"],
        ),
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
            "UPDATE service_clients SET status='active', disabled_at=NULL WHERE id=$1",
            stack["client_id"],
        )


async def test_valid_ordinary_and_service_credentials_cannot_cross_authority_domains(
    service_stack, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """Each credential is valid in its own authority domain, never the other."""
    from engram.auth import (
        DIGEST_ALGORITHM,
        digest_api_key_secret,
        generate_api_key,
        parse_api_key,
        reset_principal_cache,
    )
    from engram.config import settings

    stack = service_stack
    owner = stack["owner"]
    tenant_id, principal_id, key_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ordinary_key = generate_api_key()
    parsed = parse_api_key(ordinary_key)
    assert parsed.key_id is not None
    monkeypatch.setattr(settings, "auth_enabled", True)
    reset_principal_cache()
    await owner.execute(
        "INSERT INTO tenants (id,name,slug) VALUES ($1,$2,$3)",
        tenant_id,
        "Cross-auth ordinary tenant",
        f"cross-auth-{stack['tag']}",
    )
    await owner.execute(
        "INSERT INTO principals (id,tenant_id,name,type) VALUES ($1,$2,$3,'user')",
        principal_id,
        tenant_id,
        "Cross-auth ordinary user",
    )
    await owner.execute(
        "INSERT INTO api_keys (id,tenant_id,principal_id,key_hash,key_id,secret_digest,"
        "digest_algorithm,scopes,label) VALUES ($1,$2,$3,NULL,$4,$5,$6,$7,$8)",
        key_id,
        tenant_id,
        principal_id,
        parsed.key_id,
        digest_api_key_secret(parsed.secret),
        DIGEST_ALGORITHM,
        ["admin"],
        "cross-auth-proof",
    )
    try:
        ordinary_response = await stack["client"].post(
            "/v1/service/provisioning/tenant-human",
            headers={
                "Authorization": f"Bearer {ordinary_key}",
                "Idempotency-Key": f"cross-auth-{stack['tag']}",
            },
            json=_body(stack["tag"]),
        )
        assert ordinary_response.status_code == 401
        assert ordinary_response.json()["detail"]["code"] == "SERVICE_UNAUTHORIZED"
        for table in (
            "tenant_provisioning_bindings",
            "principal_provisioning_bindings",
            "service_provisioning_idempotency",
            "service_provisioning_events",
        ):
            assert (
                await owner.fetchval(
                    f"SELECT count(*) FROM {table} WHERE service_client_id=$1", stack["client_id"]
                )
                == 0
            )
        assert (
            await owner.fetchval(
                "SELECT last_used_at IS NULL FROM service_client_credentials WHERE id=$1",
                stack["credential_id"],
            )
            is True
        )

        # Auth dependencies run before these routes' payload work; malformed
        # payloads therefore cannot turn a valid service credential into an
        # ordinary principal or leak a route-specific credential detail.
        ordinary_routes = (
            ("GET", "/v1/items"),
            ("POST", "/v1/remember"),
            ("GET", "/v1/review/queue"),
            ("GET", "/v1/export/cca"),
            ("GET", "/v1/context-receipts"),
            ("POST", "/v1/admin/workspaces"),
            ("POST", "/v1/agents"),
            ("POST", "/v1/admin/api-keys"),
            ("POST", "/v1/admin/tenants"),
        )
        for method, path in ordinary_routes:
            response = await stack["client"].request(
                method,
                path,
                headers={"Authorization": f"Bearer {stack['credential']}"},
                json={} if method == "POST" else None,
            )
            assert response.status_code == 401, (method, path, response.text)
            assert "engsvc_" not in response.text
    finally:
        reset_principal_cache()
        await owner.execute("DELETE FROM api_keys WHERE id=$1", key_id)
        await owner.execute("DELETE FROM tenants WHERE id=$1", tenant_id)


async def test_provisioner_session_reads_real_service_credential(service_stack) -> None:  # type: ignore[no-untyped-def]
    stack = service_stack
    async with require_provisioner_session_factory()() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT current_user, c.id FROM service_client_credentials c "
                        "WHERE c.id = :credential_id"
                    ),
                    {"credential_id": stack["credential_id"]},
                )
            )
            .mappings()
            .one()
        )
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
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM tenant_provisioning_bindings WHERE service_client_id=$1",
            stack["client_id"],
        )
        == 1
    )
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM principal_provisioning_bindings WHERE service_client_id=$1",
            stack["client_id"],
        )
        == 1
    )
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM tenant_config WHERE tenant_id=$1 AND active AND auto_promote_evidence_enabled",
            uuid.UUID(tenant_id),
        )
        == 1
    )
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM memory_kinds WHERE tenant_id=$1", uuid.UUID(tenant_id)
        )
        == 9
    )
    assert (
        await owner.fetchval(
            "SELECT last_used_at IS NOT NULL FROM service_client_credentials WHERE id=$1",
            stack["credential_id"],
        )
        is True
    )

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
    for event_type in (
        "tenant.provisioned",
        "principal.provisioned",
        "provisioning.idempotent_replay",
        "provisioning.resolved_existing",
    ):
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


@pytest.mark.parametrize(
    "changed_path",
    (("tenant", "name"), ("tenant", "slug"), ("human_principal", "name")),
)
async def test_idempotency_and_immutable_identity_conflicts_are_distinct(
    service_stack, changed_path: tuple[str, str]
) -> None:  # type: ignore[no-untyped-def]
    stack = service_stack
    original = _body(stack["tag"])
    key = f"identity-{stack['tag']}-{changed_path[0]}-{changed_path[1]}"
    created = await stack["client"].post(
        "/v1/service/provisioning/tenant-human", headers=_headers(stack, key), json=original
    )
    assert created.status_code == 201
    changed = _body(stack["tag"])
    changed[changed_path[0]][changed_path[1]] = (
        f"other-{stack['tag']}" if changed_path[1] == "slug" else "Changed immutable name"
    )
    reused = await stack["client"].post(
        "/v1/service/provisioning/tenant-human", headers=_headers(stack, key), json=changed
    )
    assert reused.status_code == 409
    assert reused.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    immutable = await stack["client"].post(
        "/v1/service/provisioning/tenant-human",
        headers=_headers(stack, f"new-{key}"),
        json=changed,
    )
    assert immutable.status_code == 409
    assert immutable.json()["detail"]["code"] == "PROVISIONING_CONFLICT"
    assert (
        await stack["owner"].fetchval(
            "SELECT count(*) FROM service_provisioning_idempotency WHERE service_client_id=$1",
            stack["client_id"],
        )
        == 1
    )


async def test_unbound_slug_is_never_adopted_or_bound(service_stack) -> None:  # type: ignore[no-untyped-def]
    stack = service_stack
    body = _body(stack["tag"])
    unrelated = uuid.uuid4()
    await stack["owner"].execute(
        "INSERT INTO tenants (id,name,slug) VALUES ($1,$2,$3)",
        unrelated,
        "Unrelated tenant",
        body["tenant"]["slug"],
    )
    try:
        response = await stack["client"].post(
            "/v1/service/provisioning/tenant-human",
            headers=_headers(stack, f"slug-conflict-{stack['tag']}"),
            json=body,
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "TENANT_SLUG_CONFLICT"
        assert (
            await stack["owner"].fetchval(
                "SELECT count(*) FROM tenant_provisioning_bindings WHERE tenant_id=$1", unrelated
            )
            == 0
        )
        assert (
            await stack["owner"].fetchval(
                "SELECT count(*) FROM service_provisioning_idempotency WHERE service_client_id=$1",
                stack["client_id"],
            )
            == 0
        )
    finally:
        await stack["owner"].execute("DELETE FROM tenants WHERE id=$1", unrelated)


@pytest.mark.parametrize(
    "payload",
    (
        {"tenant": {"id": str(uuid.uuid4())}},
        {"tenant": {"workspace": "forbidden"}},
        {"human_principal": {"type": "agent"}},
        {"human_principal": {"internal_key": "forbidden"}},
        {"agent": {"name": "forbidden"}},
        {"api_key": {"scope": "forbidden"}},
        {"memory_profile": {"slug": "forbidden"}},
    ),
)
async def test_forbidden_or_invalid_public_fields_are_sanitized(
    service_stack, payload: dict[str, Any]
) -> None:  # type: ignore[no-untyped-def]
    stack = service_stack
    body = _body(stack["tag"])
    for key, value in payload.items():
        if key in body and isinstance(value, dict):
            body[key].update(value)
        else:
            body[key] = value
    response = await stack["client"].post(
        "/v1/service/provisioning/tenant-human",
        headers={"Authorization": f"Bearer {stack['credential']}"},
        json=body,
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_REQUEST"
    _assert_service_headers(response)


@pytest.mark.parametrize(
    "body",
    (
        None,
        {
            "tenant": {"external_ref": "non-ascii-\u00e9", "name": "Tenant", "slug": "tenant"},
            "human_principal": {"external_ref": "human", "name": "Human"},
        },
        {
            "tenant": {"external_ref": "tenant", "name": "\t", "slug": "tenant"},
            "human_principal": {"external_ref": "human", "name": "Human"},
        },
    ),
)
async def test_missing_or_invalid_request_contract_is_sanitized(
    service_stack, body: dict[str, Any] | None
) -> None:  # type: ignore[no-untyped-def]
    stack = service_stack
    response = await stack["client"].post(
        "/v1/service/provisioning/tenant-human",
        headers=(
            {"Authorization": f"Bearer {stack['credential']}"}
            if body is None
            else _headers(stack, f"invalid-contract-{uuid.uuid4()}")
        ),
        json=_body(stack["tag"]) if body is None else body,
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_REQUEST"
    _assert_service_headers(response)


async def test_locked_authority_revalidation_rejects_changed_state(service_stack) -> None:  # type: ignore[no-untyped-def]
    stack = service_stack
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=stack["credential"])
    identity = await get_current_service_client(credentials)
    await stack["owner"].execute(
        "UPDATE service_clients SET permissions=ARRAY['tenant.provision'] WHERE id=$1",
        stack["client_id"],
    )
    async with require_provisioner_session_factory()() as session, session.begin():
        with pytest.raises(HTTPException) as exc_info:
            await lock_and_validate_service_authority(
                session, identity, ("tenant.provision", "principal.provision")
            )
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["code"] == "SERVICE_UNAUTHORIZED"


@pytest.mark.parametrize(
    "transition",
    ("revoked", "disabled", "expired", "reassigned", "permissions_removed"),
)
async def test_preliminary_service_auth_is_revalidated_under_locks(
    service_stack, transition: str
) -> None:  # type: ignore[no-untyped-def]
    """Real preliminary authentication never authorizes a changed credential."""
    stack = service_stack
    owner = stack["owner"]
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=stack["credential"])
    identity = await get_current_service_client(credentials)
    second_client: uuid.UUID | None = None
    try:
        if transition == "revoked":
            await owner.execute(
                "UPDATE service_client_credentials SET status='revoked', revoked_at=now() WHERE id=$1",
                stack["credential_id"],
            )
        elif transition == "disabled":
            await owner.execute(
                "UPDATE service_clients SET status='disabled', disabled_at=now() WHERE id=$1",
                stack["client_id"],
            )
        elif transition == "expired":
            await owner.execute(
                "UPDATE service_client_credentials SET expires_at=$2 WHERE id=$1",
                stack["credential_id"],
                datetime.now(UTC) - timedelta(seconds=1),
            )
        elif transition == "reassigned":
            second_client = uuid.uuid4()
            await owner.execute(
                "INSERT INTO service_clients (id,slug,display_name,permissions) VALUES ($1,$2,$3,$4)",
                second_client,
                f"reassigned-{stack['tag']}",
                "Reassigned proof",
                ["tenant.provision", "principal.provision"],
            )
            await owner.execute(
                "UPDATE service_client_credentials SET service_client_id=$2 WHERE id=$1",
                stack["credential_id"],
                second_client,
            )
        else:
            await owner.execute(
                "UPDATE service_clients SET permissions=ARRAY['tenant.provision'] WHERE id=$1",
                stack["client_id"],
            )
        async with require_provisioner_session_factory()() as session, session.begin():
            with pytest.raises(HTTPException) as exc_info:
                await lock_and_validate_service_authority(
                    session, identity, ("tenant.provision", "principal.provision")
                )
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["code"] == "SERVICE_UNAUTHORIZED"
        for table in (
            "tenant_provisioning_bindings",
            "principal_provisioning_bindings",
            "service_provisioning_idempotency",
            "service_provisioning_events",
        ):
            assert (
                await owner.fetchval(
                    f"SELECT count(*) FROM {table} WHERE service_client_id=$1", stack["client_id"]
                )
                == 0
            )
    finally:
        if transition == "reassigned":
            await owner.execute(
                "UPDATE service_client_credentials SET service_client_id=$2 WHERE id=$1",
                stack["credential_id"],
                stack["client_id"],
            )
            assert second_client is not None
            await owner.execute("DELETE FROM service_clients WHERE id=$1", second_client)
        elif transition == "revoked":
            await owner.execute(
                "UPDATE service_client_credentials SET status='active', revoked_at=NULL WHERE id=$1",
                stack["credential_id"],
            )
        elif transition == "disabled":
            await owner.execute(
                "UPDATE service_clients SET status='active', disabled_at=NULL WHERE id=$1",
                stack["client_id"],
            )
        elif transition == "expired":
            await owner.execute(
                "UPDATE service_client_credentials SET expires_at=NULL WHERE id=$1",
                stack["credential_id"],
            )
        else:
            await owner.execute(
                "UPDATE service_clients SET permissions=$2 WHERE id=$1",
                stack["client_id"],
                ["tenant.provision", "principal.provision"],
            )


@pytest.mark.parametrize("permissions", (["principal.provision"], ["tenant.provision"]))
async def test_missing_preliminary_service_permission_is_forbidden(
    service_stack, permissions: list[str]
) -> None:  # type: ignore[no-untyped-def]
    stack = service_stack
    await stack["owner"].execute(
        "UPDATE service_clients SET permissions=$2 WHERE id=$1", stack["client_id"], permissions
    )
    response = await stack["client"].post(
        "/v1/service/provisioning/tenant-human",
        headers=_headers(stack, f"missing-permission-{uuid.uuid4()}"),
        json=_body(stack["tag"]),
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "SERVICE_FORBIDDEN"
    _assert_service_headers(response)


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
    assert (
        await stack["owner"].fetchval(
            "SELECT count(*) FROM tenant_provisioning_bindings WHERE service_client_id=$1",
            stack["client_id"],
        )
        == 1
    )
    assert (
        await stack["owner"].fetchval(
            "SELECT count(*) FROM principal_provisioning_bindings WHERE service_client_id=$1",
            stack["client_id"],
        )
        == 1
    )
    expected_idempotency = 1 if keys[0] == keys[1] else 2
    assert (
        await stack["owner"].fetchval(
            "SELECT count(*) FROM service_provisioning_idempotency WHERE service_client_id=$1",
            stack["client_id"],
        )
        == expected_idempotency
    )


@pytest.mark.parametrize("field", ("name", "slug", "principal_name"))
async def test_concurrent_conflicting_requests_have_one_success_and_one_bounded_conflict(
    service_stack, field: str
) -> None:  # type: ignore[no-untyped-def]
    stack = service_stack
    first_body = _body(stack["tag"])
    second_body = _body(stack["tag"])
    if field == "principal_name":
        second_body["human_principal"]["name"] = "Different human name"
    else:
        second_body["tenant"][field] = (
            f"different-{stack['tag']}" if field == "slug" else "Different tenant name"
        )
    start = asyncio.Event()

    async def invoke(body: dict[str, Any], suffix: str):
        await start.wait()
        return await stack["client"].post(
            "/v1/service/provisioning/tenant-human",
            headers=_headers(stack, f"conflicting-{field}-{suffix}-{stack['tag']}"),
            json=body,
        )

    first, second = (
        asyncio.create_task(invoke(first_body, "one")),
        asyncio.create_task(invoke(second_body, "two")),
    )
    start.set()
    responses = await asyncio.gather(first, second)
    assert sorted(response.status_code for response in responses) == [201, 409]
    loser = next(response for response in responses if response.status_code == 409)
    assert loser.json()["detail"]["code"] == "PROVISIONING_CONFLICT"
    assert (
        await stack["owner"].fetchval(
            "SELECT count(*) FROM service_provisioning_events "
            "WHERE service_client_id=$1 AND event_type='provisioning.conflict'",
            stack["client_id"],
        )
        == 1
    )


async def test_independent_service_clients_isolate_same_external_references(
    service_stack,
) -> None:  # type: ignore[no-untyped-def]
    """The service-client ID is part of every reconciliation identity and lock."""
    stack = service_stack
    owner = stack["owner"]
    second_client_id, second_credential_id = uuid.uuid4(), uuid.uuid4()
    second_credential = generate_service_credential()
    second_parsed = parse_service_credential(second_credential)
    second_http = AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")
    second_tenant_ids: list[uuid.UUID] = []
    await owner.execute(
        "INSERT INTO service_clients (id,slug,display_name,permissions) VALUES ($1,$2,$3,$4)",
        second_client_id,
        f"independent-{stack['tag']}",
        "Independent service proof",
        ["tenant.provision", "principal.provision"],
    )
    await owner.execute(
        "INSERT INTO service_client_credentials "
        "(id,service_client_id,key_id,secret_digest,digest_algorithm) VALUES ($1,$2,$3,$4,'sha256')",
        second_credential_id,
        second_client_id,
        second_parsed.key_id,
        digest_service_secret(second_parsed.secret),
    )
    try:
        first_body = _body(stack["tag"])
        second_body = _body(
            stack["tag"], name="Independent tenant", slug=f"independent-{stack['tag']}"
        )
        second_body["human_principal"]["name"] = "Independent human"
        start = asyncio.Event()

        async def first_request():  # type: ignore[no-untyped-def]
            await start.wait()
            return await stack["client"].post(
                "/v1/service/provisioning/tenant-human",
                headers=_headers(stack, f"independent-one-{stack['tag']}"),
                json=first_body,
            )

        async def second_request():  # type: ignore[no-untyped-def]
            await start.wait()
            return await second_http.post(
                "/v1/service/provisioning/tenant-human",
                headers={
                    "Authorization": f"Bearer {second_credential}",
                    "Idempotency-Key": f"independent-two-{stack['tag']}",
                },
                json=second_body,
            )

        first, second = asyncio.create_task(first_request()), asyncio.create_task(second_request())
        start.set()
        first_response, second_response = await asyncio.gather(first, second)
        assert {first_response.status_code, second_response.status_code} == {201}
        assert first_response.json()["tenant"]["id"] != second_response.json()["tenant"]["id"]
        assert first_response.json()["principal"]["id"] != second_response.json()["principal"]["id"]
        second_tenant_ids = [uuid.UUID(second_response.json()["tenant"]["id"])]
        for client_id in (stack["client_id"], second_client_id):
            assert (
                await owner.fetchval(
                    "SELECT count(*) FROM tenant_provisioning_bindings WHERE service_client_id=$1",
                    client_id,
                )
                == 1
            )
            assert (
                await owner.fetchval(
                    "SELECT count(*) FROM principal_provisioning_bindings WHERE service_client_id=$1",
                    client_id,
                )
                == 1
            )
            assert (
                await owner.fetchval(
                    "SELECT count(*) FROM service_provisioning_idempotency WHERE service_client_id=$1",
                    client_id,
                )
                == 1
            )
    finally:
        await second_http.aclose()
        async with owner.transaction():
            await owner.execute(
                "DELETE FROM service_provisioning_events WHERE service_client_id=$1",
                second_client_id,
            )
            await owner.execute(
                "DELETE FROM service_provisioning_idempotency WHERE service_client_id=$1",
                second_client_id,
            )
            await owner.execute(
                "DELETE FROM principal_provisioning_bindings WHERE service_client_id=$1",
                second_client_id,
            )
            await owner.execute(
                "DELETE FROM tenant_provisioning_bindings WHERE service_client_id=$1",
                second_client_id,
            )
            await owner.execute(
                "DELETE FROM service_client_credentials WHERE id=$1", second_credential_id
            )
            await owner.execute("DELETE FROM service_clients WHERE id=$1", second_client_id)
            for tenant_id in second_tenant_ids:
                await owner.execute("DELETE FROM tenants WHERE id=$1", tenant_id)


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
        assert (
            await stack["owner"].fetchval(
                f"SELECT count(*) FROM {table} WHERE service_client_id=$1", stack["client_id"]
            )
            == 0
        )


@pytest.mark.parametrize(
    "stage",
    (
        "tenant_inserted",
        "tenant_binding_inserted",
        "memory_kinds_initialized",
        "tenant_config_initialized",
        "principal_inserted",
        "principal_binding_inserted",
        "idempotency_inserted",
        "tenant_success_event_inserted",
        "principal_success_event_inserted",
        "credential_last_used_updated",
    ),
)
async def test_every_provisioning_mutation_stage_rolls_back_completely(
    service_stack, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:  # type: ignore[no-untyped-def]
    """Unexpected failures never leave a partially-provisioned resource set."""
    import engram.provisioning as provisioning

    stack = service_stack
    before_last_used = await stack["owner"].fetchval(
        "SELECT last_used_at FROM service_client_credentials WHERE id=$1", stack["credential_id"]
    )

    async def fail_at(actual_stage: str) -> None:
        if actual_stage == stage:
            raise RuntimeError("injected provisioning stage failure")

    monkeypatch.setattr(provisioning, "provisioning_stage", fail_at)
    body = _body(stack["tag"])
    response = await stack["client"].post(
        "/v1/service/provisioning/tenant-human",
        headers=_headers(stack, f"rollback-{stage}-{stack['tag']}"),
        json=body,
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "SERVICE_UNAVAILABLE"
    assert "injected" not in response.text
    _assert_service_headers(response)

    owner = stack["owner"]
    for table in (
        "tenant_provisioning_bindings",
        "principal_provisioning_bindings",
        "service_provisioning_idempotency",
        "service_provisioning_events",
    ):
        assert (
            await owner.fetchval(
                f"SELECT count(*) FROM {table} WHERE service_client_id=$1", stack["client_id"]
            )
            == 0
        )
    assert (
        await owner.fetchval("SELECT count(*) FROM tenants WHERE slug=$1", body["tenant"]["slug"])
        == 0
    )
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM principals p JOIN tenants t ON t.id=p.tenant_id WHERE t.slug=$1",
            body["tenant"]["slug"],
        )
        == 0
    )
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM tenant_config c JOIN tenants t ON t.id=c.tenant_id WHERE t.slug=$1",
            body["tenant"]["slug"],
        )
        == 0
    )
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM memory_kinds k JOIN tenants t ON t.id=k.tenant_id WHERE t.slug=$1",
            body["tenant"]["slug"],
        )
        == 0
    )
    assert (
        await owner.fetchval(
            "SELECT last_used_at FROM service_client_credentials WHERE id=$1",
            stack["credential_id"],
        )
        == before_last_used
    )


async def _provision_onboarding_tenant(stack: dict[str, Any]) -> dict[str, Any]:
    body = _body(stack["tag"])
    response = await stack["client"].post(
        "/v1/service/provisioning/tenant-human",
        headers=_headers(stack, f"tenant-onboarding-{stack['tag']}"),
        json=body,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _workspace_agent_body(stack: dict[str, Any]) -> dict[str, Any]:
    return {
        "tenant_external_ref": f"tenant-ref-{stack['tag']}",
        "workspace": {
            "external_ref": f"workspace-ref-{stack['tag']}",
            "name": f"Workspace {stack['tag']}",
            "slug": f"workspace-{stack['tag']}",
        },
        "agent": {
            "external_ref": f"agent-ref-{stack['tag']}",
            "name": f"Agent {stack['tag']}",
        },
    }


async def _provision_workspace_agent(
    stack: dict[str, Any], idempotency_key: str
) -> Any:
    return await stack["client"].post(
        "/v1/service/provisioning/workspace-agent",
        headers=_headers(stack, idempotency_key),
        json=_workspace_agent_body(stack),
    )


def _agent_key_body(
    stack: dict[str, Any],
    *,
    external_ref: str,
    replaces_external_ref: str | None = None,
) -> dict[str, Any]:
    return {
        "tenant_external_ref": f"tenant-ref-{stack['tag']}",
        "workspace_external_ref": f"workspace-ref-{stack['tag']}",
        "agent_external_ref": f"agent-ref-{stack['tag']}",
        "api_key": {
            "external_ref": external_ref,
            "label": "service agent",
            "replaces_external_ref": replaces_external_ref,
        },
    }


async def test_workspace_agent_creation_replay_and_reconciliation_are_stable(
    service_stack,
) -> None:  # type: ignore[no-untyped-def]
    stack = service_stack
    tenant = await _provision_onboarding_tenant(stack)
    key = f"workspace-agent-{stack['tag']}"
    created = await _provision_workspace_agent(stack, key)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["created"] == {"workspace": True, "agent": True, "membership": True}
    assert body["idempotency_replayed"] is False
    assert body["workspace"]["tenant_id"] == tenant["tenant"]["id"]
    assert body["agent"]["tenant_id"] == tenant["tenant"]["id"]
    assert body["agent"]["type"] == "agent"
    assert body["membership"]["role"] == "member"
    assert body["membership"]["workspace_id"] == body["workspace"]["id"]
    assert body["membership"]["principal_id"] == body["agent"]["id"]
    _assert_service_headers(created)

    replay = await _provision_workspace_agent(stack, key)
    assert replay.status_code == 200
    assert replay.json()["idempotency_replayed"] is True
    assert replay.headers["idempotency-replayed"] == "true"
    for resource in ("workspace", "agent", "membership"):
        assert replay.json()[resource]["id"] == body[resource]["id"]

    reconciled = await _provision_workspace_agent(
        stack, f"workspace-agent-reconcile-{stack['tag']}"
    )
    assert reconciled.status_code == 200
    assert reconciled.json()["created"] == {
        "workspace": False,
        "agent": False,
        "membership": False,
    }
    assert reconciled.json()["idempotency_replayed"] is False

    owner = stack["owner"]
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM workspace_provisioning_bindings "
            "WHERE service_client_id=$1",
            stack["client_id"],
        )
        == 1
    )
    agent_row = await owner.fetchrow(
        "SELECT p.type,p.internal_key,m.role "
        "FROM principal_provisioning_bindings b "
        "JOIN principals p ON p.id=b.principal_id "
        "JOIN workspace_members m ON m.principal_id=p.id "
        "WHERE b.service_client_id=$1 AND p.type='agent'",
        stack["client_id"],
    )
    assert agent_row is not None
    assert dict(agent_row) == {"type": "agent", "internal_key": None, "role": "member"}


async def test_agent_key_secret_is_once_only_and_replacement_is_atomic(
    service_stack,
) -> None:  # type: ignore[no-untyped-def]
    stack = service_stack
    await _provision_onboarding_tenant(stack)
    workspace_response = await _provision_workspace_agent(
        stack, f"workspace-agent-for-key-{stack['tag']}"
    )
    assert workspace_response.status_code == 201

    first_ref = f"key-ref-{stack['tag']}"
    first_idempotency = f"agent-key-{stack['tag']}"
    issued = await stack["client"].post(
        "/v1/service/provisioning/agent-api-key",
        headers=_headers(stack, first_idempotency),
        json=_agent_key_body(stack, external_ref=first_ref),
    )
    assert issued.status_code == 201, issued.text
    issued_body = issued.json()
    plaintext = issued_body["key"]
    assert isinstance(plaintext, str) and plaintext.startswith("eng_")
    assert issued_body["created"] is True
    assert issued_body["replacement"] is False
    assert issued_body["credential_secret_available"] is True
    assert issued_body["api_key"]["scopes"] == ["read", "write"]
    assert issued_body["api_key"]["memory_profile_id"] is None

    stored = await stack["owner"].fetchrow(
        "SELECT key_id,secret_digest,key_hash,scopes,memory_profile_id,revoked_at "
        "FROM api_keys WHERE id=$1",
        uuid.UUID(issued_body["api_key"]["id"]),
    )
    assert stored is not None
    assert stored["key_id"] and stored["secret_digest"]
    assert stored["key_hash"] is None
    assert stored["scopes"] == ["read", "write"]
    assert stored["memory_profile_id"] is None
    assert plaintext not in str(dict(stored))

    replay = await stack["client"].post(
        "/v1/service/provisioning/agent-api-key",
        headers=_headers(stack, first_idempotency),
        json=_agent_key_body(stack, external_ref=first_ref),
    )
    assert replay.status_code == 200
    assert replay.json()["api_key"]["id"] == issued_body["api_key"]["id"]
    assert replay.json()["created"] is False
    assert replay.json()["credential_secret_available"] is False
    assert replay.json()["key"] is None
    assert replay.headers["idempotency-replayed"] == "true"

    replacement_ref = f"replacement-ref-{stack['tag']}"
    replaced = await stack["client"].post(
        "/v1/service/provisioning/agent-api-key",
        headers=_headers(stack, f"replacement-idem-{stack['tag']}"),
        json=_agent_key_body(
            stack,
            external_ref=replacement_ref,
            replaces_external_ref=first_ref,
        ),
    )
    assert replaced.status_code == 201, replaced.text
    replacement_body = replaced.json()
    assert replacement_body["created"] is True
    assert replacement_body["replacement"] is True
    assert replacement_body["key"].startswith("eng_")
    assert replacement_body["key"] != plaintext

    rows = await stack["owner"].fetch(
        "SELECT b.status,b.replaces_binding_id,k.revoked_at "
        "FROM agent_api_key_provisioning_bindings b "
        "JOIN api_keys k ON k.id=b.api_key_id "
        "WHERE b.service_client_id=$1 ORDER BY b.created_at",
        stack["client_id"],
    )
    assert len(rows) == 2
    assert rows[0]["status"] == "replaced"
    assert rows[0]["revoked_at"] is not None
    assert rows[1]["status"] == "active"
    assert rows[1]["replaces_binding_id"] is not None
    assert rows[1]["revoked_at"] is None
    assert (
        await stack["owner"].fetchval(
            "SELECT count(*) FROM agent_api_key_provisioning_bindings "
            "WHERE service_client_id=$1 AND status='active'",
            stack["client_id"],
        )
        == 1
    )

    replacement_replay = await stack["client"].post(
        "/v1/service/provisioning/agent-api-key",
        headers=_headers(stack, f"replacement-idem-{stack['tag']}"),
        json=_agent_key_body(
            stack,
            external_ref=replacement_ref,
            replaces_external_ref=first_ref,
        ),
    )
    assert replacement_replay.status_code == 200
    assert replacement_replay.json()["key"] is None
    assert replacement_replay.json()["credential_secret_available"] is False


@pytest.mark.parametrize("same_idempotency_key", [True, False])
async def test_concurrent_workspace_agent_requests_converge(
    service_stack: dict[str, Any],
    same_idempotency_key: bool,
) -> None:
    stack = service_stack
    await _provision_onboarding_tenant(stack)
    first_key = f"workspace-concurrent-{stack['tag']}"
    second_key = (
        first_key if same_idempotency_key else f"workspace-concurrent-other-{stack['tag']}"
    )

    responses = await asyncio.gather(
        _provision_workspace_agent(stack, first_key),
        _provision_workspace_agent(stack, second_key),
    )

    assert sorted(response.status_code for response in responses) == [200, 201]
    response_bodies = [response.json() for response in responses]
    for resource in ("workspace", "agent", "membership"):
        assert len({body[resource]["id"] for body in response_bodies}) == 1
    assert sum(body["idempotency_replayed"] for body in response_bodies) == (
        1 if same_idempotency_key else 0
    )
    assert sum(body["created"]["workspace"] for body in response_bodies) == 1
    assert sum(body["created"]["agent"] for body in response_bodies) == 1
    assert sum(body["created"]["membership"] for body in response_bodies) == 1

    owner = stack["owner"]
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM workspace_provisioning_bindings "
            "WHERE service_client_id=$1",
            stack["client_id"],
        )
        == 1
    )
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM principal_provisioning_bindings b "
            "JOIN principals p ON p.id=b.principal_id "
            "WHERE b.service_client_id=$1 AND p.type='agent'",
            stack["client_id"],
        )
        == 1
    )
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM workspace_members m "
            "JOIN principal_provisioning_bindings b ON b.principal_id=m.principal_id "
            "WHERE b.service_client_id=$1",
            stack["client_id"],
        )
        == 1
    )
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM service_workspace_agent_idempotency "
            "WHERE service_client_id=$1",
            stack["client_id"],
        )
        == (1 if same_idempotency_key else 2)
    )


@pytest.mark.parametrize("same_idempotency_key", [True, False])
async def test_concurrent_agent_key_replacements_issue_one_secret(
    service_stack: dict[str, Any],
    same_idempotency_key: bool,
) -> None:
    stack = service_stack
    await _provision_onboarding_tenant(stack)
    workspace_response = await _provision_workspace_agent(
        stack, f"workspace-for-concurrent-key-{stack['tag']}"
    )
    assert workspace_response.status_code == 201
    first_ref = f"concurrent-original-{stack['tag']}"
    initial = await stack["client"].post(
        "/v1/service/provisioning/agent-api-key",
        headers=_headers(stack, f"concurrent-original-idem-{stack['tag']}"),
        json=_agent_key_body(stack, external_ref=first_ref),
    )
    assert initial.status_code == 201

    replacement_ref = f"concurrent-replacement-{stack['tag']}"
    first_key = f"concurrent-replacement-idem-{stack['tag']}"
    second_key = (
        first_key
        if same_idempotency_key
        else f"concurrent-replacement-other-{stack['tag']}"
    )
    request_body = _agent_key_body(
        stack,
        external_ref=replacement_ref,
        replaces_external_ref=first_ref,
    )
    responses = await asyncio.gather(
        stack["client"].post(
            "/v1/service/provisioning/agent-api-key",
            headers=_headers(stack, first_key),
            json=request_body,
        ),
        stack["client"].post(
            "/v1/service/provisioning/agent-api-key",
            headers=_headers(stack, second_key),
            json=request_body,
        ),
    )

    assert sorted(response.status_code for response in responses) == [200, 201]
    response_bodies = [response.json() for response in responses]
    assert len({body["api_key"]["id"] for body in response_bodies}) == 1
    assert sum(body["created"] for body in response_bodies) == 1
    assert sum(body["credential_secret_available"] for body in response_bodies) == 1
    assert sum(body["key"] is not None for body in response_bodies) == 1
    assert sum(body["idempotency_replayed"] for body in response_bodies) == (
        1 if same_idempotency_key else 0
    )

    owner = stack["owner"]
    rows = await owner.fetch(
        "SELECT b.status,k.revoked_at "
        "FROM agent_api_key_provisioning_bindings b "
        "JOIN api_keys k ON k.id=b.api_key_id "
        "WHERE b.service_client_id=$1 ORDER BY b.created_at",
        stack["client_id"],
    )
    assert len(rows) == 2
    assert sum(row["status"] == "active" and row["revoked_at"] is None for row in rows) == 1
    assert sum(row["status"] == "replaced" and row["revoked_at"] is not None for row in rows) == 1
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM service_agent_key_idempotency "
            "WHERE service_client_id=$1",
            stack["client_id"],
        )
        == (2 if same_idempotency_key else 3)
    )


async def _raise_at_onboarding_stage(current_stage: str, target_stage: str) -> None:
    if current_stage == target_stage:
        raise RuntimeError("injected onboarding rollback proof")


async def _onboarding_baseline(stack: dict[str, Any]) -> tuple[Any, int]:
    return (
        await stack["owner"].fetchval(
            "SELECT last_used_at FROM service_client_credentials WHERE id=$1",
            stack["credential_id"],
        ),
        await stack["owner"].fetchval(
            "SELECT count(*) FROM service_provisioning_events WHERE service_client_id=$1",
            stack["client_id"],
        ),
    )


async def _assert_onboarding_baseline(
    stack: dict[str, Any], baseline: tuple[Any, int]
) -> None:
    assert (
        await stack["owner"].fetchval(
            "SELECT last_used_at FROM service_client_credentials WHERE id=$1",
            stack["credential_id"],
        )
        == baseline[0]
    )
    assert (
        await stack["owner"].fetchval(
            "SELECT count(*) FROM service_provisioning_events WHERE service_client_id=$1",
            stack["client_id"],
        )
        == baseline[1]
    )


@pytest.mark.parametrize(
    "stage",
    [
        "workspace_inserted",
        "workspace_binding_inserted",
        "agent_inserted",
        "principal_binding_inserted",
        "workspace_membership_inserted",
        "workspace_agent_idempotency_inserted",
        "workspace_agent_success_events_inserted",
        "credential_last_used_updated",
    ],
)
async def test_workspace_agent_rolls_back_at_every_mutation_stage(
    service_stack: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    stack = service_stack
    await _provision_onboarding_tenant(stack)
    baseline = await _onboarding_baseline(stack)

    async def fail_at_stage(current_stage: str) -> None:
        await _raise_at_onboarding_stage(current_stage, stage)

    monkeypatch.setattr("engram.service_onboarding.onboarding_stage", fail_at_stage)
    response = await _provision_workspace_agent(
        stack, f"workspace-rollback-{stage}-{stack['tag']}"
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "SERVICE_UNAVAILABLE"
    owner = stack["owner"]
    for table in (
        "workspace_provisioning_bindings",
        "service_workspace_agent_idempotency",
    ):
        assert (
            await owner.fetchval(
                f"SELECT count(*) FROM {table} WHERE service_client_id=$1",
                stack["client_id"],
            )
            == 0
        )
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM principal_provisioning_bindings b "
            "JOIN principals p ON p.id=b.principal_id "
            "WHERE b.service_client_id=$1 AND p.type='agent'",
            stack["client_id"],
        )
        == 0
    )
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM workspaces WHERE slug=$1",
            _workspace_agent_body(stack)["workspace"]["slug"],
        )
        == 0
    )
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM principals p "
            "JOIN tenant_provisioning_bindings b ON b.tenant_id=p.tenant_id "
            "WHERE b.service_client_id=$1 AND p.type='agent'",
            stack["client_id"],
        )
        == 0
    )
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM workspace_members m "
            "JOIN principal_provisioning_bindings b ON b.principal_id=m.principal_id "
            "WHERE b.service_client_id=$1",
            stack["client_id"],
        )
        == 0
    )
    await _assert_onboarding_baseline(stack, baseline)


@pytest.mark.parametrize(
    "stage",
    [
        "api_key_inserted",
        "api_key_binding_inserted",
        "agent_key_idempotency_inserted",
        "agent_key_success_event_inserted",
        "credential_last_used_updated",
    ],
)
async def test_initial_agent_key_rolls_back_at_every_mutation_stage(
    service_stack: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    stack = service_stack
    await _provision_onboarding_tenant(stack)
    workspace_response = await _provision_workspace_agent(
        stack, f"workspace-for-key-rollback-{stack['tag']}"
    )
    assert workspace_response.status_code == 201
    baseline = await _onboarding_baseline(stack)

    async def fail_at_stage(current_stage: str) -> None:
        await _raise_at_onboarding_stage(current_stage, stage)

    monkeypatch.setattr("engram.service_onboarding.onboarding_stage", fail_at_stage)
    response = await stack["client"].post(
        "/v1/service/provisioning/agent-api-key",
        headers=_headers(stack, f"initial-key-rollback-{stage}-{stack['tag']}"),
        json=_agent_key_body(stack, external_ref=f"initial-rollback-{stack['tag']}"),
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "SERVICE_UNAVAILABLE"
    owner = stack["owner"]
    for table in (
        "agent_api_key_provisioning_bindings",
        "service_agent_key_idempotency",
    ):
        assert (
            await owner.fetchval(
                f"SELECT count(*) FROM {table} WHERE service_client_id=$1",
                stack["client_id"],
            )
            == 0
        )
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM api_keys k "
            "JOIN principal_provisioning_bindings b ON b.principal_id=k.principal_id "
            "WHERE b.service_client_id=$1",
            stack["client_id"],
        )
        == 0
    )
    await _assert_onboarding_baseline(stack, baseline)


@pytest.mark.parametrize(
    "stage",
    [
        "prior_api_key_revoked",
        "prior_api_key_binding_replaced",
        "replacement_api_key_inserted",
        "api_key_binding_inserted",
        "agent_key_idempotency_inserted",
        "agent_key_success_event_inserted",
        "credential_last_used_updated",
    ],
)
async def test_agent_key_replacement_rolls_back_at_every_mutation_stage(
    service_stack: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    stack = service_stack
    await _provision_onboarding_tenant(stack)
    workspace_response = await _provision_workspace_agent(
        stack, f"workspace-for-replacement-rollback-{stack['tag']}"
    )
    assert workspace_response.status_code == 201
    first_ref = f"replacement-rollback-original-{stack['tag']}"
    initial = await stack["client"].post(
        "/v1/service/provisioning/agent-api-key",
        headers=_headers(stack, f"replacement-rollback-original-idem-{stack['tag']}"),
        json=_agent_key_body(stack, external_ref=first_ref),
    )
    assert initial.status_code == 201
    original_api_key_id = uuid.UUID(initial.json()["api_key"]["id"])
    baseline = await _onboarding_baseline(stack)

    async def fail_at_stage(current_stage: str) -> None:
        await _raise_at_onboarding_stage(current_stage, stage)

    monkeypatch.setattr("engram.service_onboarding.onboarding_stage", fail_at_stage)
    response = await stack["client"].post(
        "/v1/service/provisioning/agent-api-key",
        headers=_headers(stack, f"replacement-key-rollback-{stage}-{stack['tag']}"),
        json=_agent_key_body(
            stack,
            external_ref=f"replacement-rollback-new-{stack['tag']}",
            replaces_external_ref=first_ref,
        ),
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "SERVICE_UNAVAILABLE"
    owner = stack["owner"]
    binding_rows = await owner.fetch(
        "SELECT status,replaced_at,api_key_id "
        "FROM agent_api_key_provisioning_bindings WHERE service_client_id=$1",
        stack["client_id"],
    )
    assert len(binding_rows) == 1
    assert dict(binding_rows[0]) == {
        "status": "active",
        "replaced_at": None,
        "api_key_id": original_api_key_id,
    }
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM api_keys k "
            "JOIN principal_provisioning_bindings b ON b.principal_id=k.principal_id "
            "WHERE b.service_client_id=$1",
            stack["client_id"],
        )
        == 1
    )
    assert (
        await owner.fetchval(
            "SELECT revoked_at FROM api_keys WHERE id=$1", original_api_key_id
        )
        is None
    )
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM service_agent_key_idempotency "
            "WHERE service_client_id=$1",
            stack["client_id"],
        )
        == 1
    )
    await _assert_onboarding_baseline(stack, baseline)
