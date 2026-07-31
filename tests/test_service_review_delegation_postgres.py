"""Real-PostgreSQL proofs for purpose-bound delegated review authority."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from engram.api.app import app
from engram.cli import _run_delegation_grant
from engram.config import settings
from engram.delegation_auth import (
    ReviewPurpose,
    canonical_review_queue_purpose,
    canonical_review_transition_purpose,
    generate_review_delegation_token,
)
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
async def review_delegation_db() -> AsyncIterator[dict[str, Any]]:
    owner_url = os.getenv("ENGRAM_OWNER_DATABASE_URL") or os.getenv("ENGRAM_DATABASE_URL")
    provisioner_url = os.getenv("ENGRAM_PROVISIONER_DATABASE_URL")
    if not owner_url or not provisioner_url:
        pytest.skip("requires owner and provisioner PostgreSQL URLs")
    owner = await _connect(owner_url)
    provisioner = await _connect(provisioner_url)
    tag = uuid.uuid4().hex[:12]
    broker_id = uuid.uuid4()
    binding_owner_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    tenant_binding_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    other_principal_id = uuid.uuid4()
    principal_binding_id = uuid.uuid4()
    review_grant_id = uuid.uuid4()
    broker_slug = f"review-broker-{tag}"
    owner_slug = f"review-owner-{tag}"
    tenant_ref = f"review-tenant-{tag}"
    principal_ref = f"review-principal-{tag}"
    broker_credential = generate_service_credential()
    parsed = parse_service_credential(broker_credential)
    async with owner.transaction():
        await owner.execute(
            "INSERT INTO service_clients (id,slug,display_name,permissions) "
            "VALUES ($1,$2,$3,$4),($5,$6,$7,$8)",
            broker_id,
            broker_slug,
            f"Review broker {tag}",
            ["delegation.review.issue"],
            binding_owner_id,
            owner_slug,
            f"Review owner {tag}",
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
            credential_id,
            broker_id,
            parsed.key_id,
            digest_service_secret(parsed.secret),
        )
        await owner.execute(
            "INSERT INTO tenants (id,name,slug) VALUES ($1,$2,$3)",
            tenant_id,
            f"Review tenant {tag}",
            f"review-tenant-{tag}",
        )
        await owner.execute(
            "INSERT INTO tenant_config (tenant_id) VALUES ($1)",
            tenant_id,
        )
        await owner.execute(
            "INSERT INTO principals (id,tenant_id,name,type) "
            "VALUES ($1,$2,$3,'user'),($4,$2,$5,'user')",
            principal_id,
            tenant_id,
            f"Review human {tag}",
            other_principal_id,
            f"Other review human {tag}",
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
            "(id,issuer_service_client_id,binding_owner_service_client_id,"
            "authority_class,max_ttl_seconds) VALUES ($1,$2,$3,'review',60)",
            review_grant_id,
            broker_id,
            binding_owner_id,
        )
    previous = (
        settings.auth_enabled,
        settings.service_provisioning_enabled,
        settings.review_delegation_enabled,
    )
    settings.auth_enabled = True
    settings.service_provisioning_enabled = True
    settings.review_delegation_enabled = True
    try:
        yield {
            "owner": owner,
            "provisioner": provisioner,
            "owner_url": owner_url,
            "provisioner_url": provisioner_url,
            "broker_id": broker_id,
            "binding_owner_id": binding_owner_id,
            "credential_id": credential_id,
            "broker_credential": broker_credential,
            "broker_slug": broker_slug,
            "owner_slug": owner_slug,
            "tenant_id": tenant_id,
            "tenant_binding_id": tenant_binding_id,
            "tenant_ref": tenant_ref,
            "principal_id": principal_id,
            "other_principal_id": other_principal_id,
            "principal_binding_id": principal_binding_id,
            "principal_ref": principal_ref,
            "review_grant_id": review_grant_id,
        }
    finally:
        (
            settings.auth_enabled,
            settings.service_provisioning_enabled,
            settings.review_delegation_enabled,
        ) = previous
        try:
            await owner.execute(
                "ALTER TABLE service_delegation_events "
                "DISABLE TRIGGER trg_service_delegation_events_append_only"
            )
            try:
                async with owner.transaction():
                    await owner.execute(
                        "DELETE FROM item_events WHERE tenant_id=$1",
                        tenant_id,
                    )
                    await owner.execute(
                        "DELETE FROM memory_items WHERE tenant_id=$1",
                        tenant_id,
                    )
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
                        "DELETE FROM tenant_config WHERE tenant_id=$1",
                        tenant_id,
                    )
                    await owner.execute(
                        "DELETE FROM service_client_credentials "
                        "WHERE service_client_id=$1",
                        broker_id,
                    )
                    await owner.execute(
                        "DELETE FROM service_clients WHERE id=ANY($1::uuid[])",
                        [broker_id, binding_owner_id],
                    )
                    await owner.execute(
                        "DELETE FROM principals WHERE tenant_id=$1",
                        tenant_id,
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
    purpose: ReviewPurpose,
    external_ref: str | None = None,
    idempotency_digest: bytes | None = None,
) -> tuple[Any, Any, str]:
    material = generate_review_delegation_token()
    resolved_external_ref = external_ref or f"review-token-{uuid.uuid4().hex}"
    async with proof["provisioner"].transaction():
        await proof["provisioner"].execute(
            "SELECT set_config('app.service_client_id',$1,true)",
            str(proof["broker_id"]),
        )
        row = await proof["provisioner"].fetchrow(
            "SELECT * FROM issue_service_review_delegation("
            "$1,$2,$3,$4,$5,$6,$7,$8,$9,30,$10,$11,$12,$13,$14)",
            proof["credential_id"],
            proof["owner_slug"],
            proof["tenant_ref"],
            proof["principal_ref"],
            resolved_external_ref,
            idempotency_digest or os.urandom(32),
            os.urandom(32),
            material.key_id,
            material.secret_digest,
            f"review-issue-{uuid.uuid4().hex}",
            purpose.name,
            purpose.digest,
            purpose.target_item_id,
            purpose.target_review_status,
        )
    assert row is not None
    return material, row, resolved_external_ref


async def _insert_item(
    proof: dict[str, Any],
    *,
    review_status: str = "proposed",
    principal_id: uuid.UUID | None = None,
    visibility: str = "private",
    content: str | None = None,
) -> uuid.UUID:
    item_id = uuid.uuid4()
    await proof["owner"].execute(
        "INSERT INTO memory_items "
        "(id,tenant_id,principal_id,content,content_hash,kind,visibility,"
        "review_status,source_type) VALUES ($1,$2,$3,$4,$5,'fact',$6,$7,'manual')",
        item_id,
        proof["tenant_id"],
        principal_id or proof["principal_id"],
        content or f"review item {item_id}",
        f"sha256:{item_id.hex}",
        visibility,
        review_status,
    )
    return item_id


async def _enable_read_authority(proof: dict[str, Any]) -> uuid.UUID:
    read_grant_id = uuid.uuid4()
    await proof["owner"].execute(
        "UPDATE service_clients SET permissions=$2 WHERE id=$1",
        proof["broker_id"],
        ["delegation.issue", "delegation.review.issue"],
    )
    await proof["owner"].execute(
        "INSERT INTO service_delegation_grants "
        "(id,issuer_service_client_id,binding_owner_service_client_id,"
        "authority_class,max_ttl_seconds) VALUES ($1,$2,$3,'read',60)",
        read_grant_id,
        proof["broker_id"],
        proof["binding_owner_id"],
    )
    return read_grant_id


def _headers(token: str, request_id: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if request_id is not None:
        headers["X-Request-ID"] = request_id
    return headers


def _assert_sensitive_headers(response: Any, request_id: str | None = None) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"
    if request_id is None:
        assert str(uuid.UUID(response.headers["x-request-id"])) == response.headers[
            "x-request-id"
        ]
    else:
        assert response.headers["x-request-id"] == request_id


async def test_review_service_issue_replay_storage_revoke_and_class_isolation(
    review_delegation_db: dict[str, Any],
) -> None:
    proof = review_delegation_db
    external_ref = f"http-review-{uuid.uuid4().hex}"
    idempotency_key = f"http-review-{uuid.uuid4().hex}"
    body = {
        "binding_owner_service_client_slug": proof["owner_slug"],
        "tenant_external_ref": proof["tenant_ref"],
        "principal_external_ref": proof["principal_ref"],
        "delegation_external_ref": external_ref,
        "purpose": {"kind": "review.queue"},
        "ttl_seconds": 30,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            "/v1/service/review-delegations",
            headers={
                "Authorization": f"Bearer {proof['broker_credential']}",
                "Idempotency-Key": idempotency_key,
            },
            json=body,
        )
        assert created.status_code == 201
        payload = created.json()
        token = payload.pop("token")
        assert isinstance(token, str)
        assert payload["created"] is True
        assert payload["credential_secret_available"] is True
        assert payload["scopes"] == ["review"]
        assert payload["audience"] == "engram-core"
        assert payload["single_use"] is True
        _assert_sensitive_headers(created)

        replay = await client.post(
            "/v1/service/review-delegations",
            headers={
                "Authorization": f"Bearer {proof['broker_credential']}",
                "Idempotency-Key": idempotency_key,
            },
            json=body,
        )
        assert replay.status_code == 200
        assert replay.json()["created"] is False
        assert replay.json()["idempotency_replayed"] is True
        assert replay.json()["credential_secret_available"] is False
        assert replay.json()["token"] is None

        reconciled = await client.post(
            "/v1/service/review-delegations",
            headers={
                "Authorization": f"Bearer {proof['broker_credential']}",
                "Idempotency-Key": f"reconcile-{uuid.uuid4().hex}",
            },
            json=body,
        )
        assert reconciled.status_code == 200
        assert reconciled.json()["created"] is False
        assert reconciled.json()["idempotency_replayed"] is False
        assert reconciled.json()["token"] is None

        revoked = await client.post(
            "/v1/service/review-delegations/revoke",
            headers={"Authorization": f"Bearer {proof['broker_credential']}"},
            json={
                "binding_owner_service_client_slug": proof["owner_slug"],
                "tenant_external_ref": proof["tenant_ref"],
                "principal_external_ref": proof["principal_ref"],
                "delegation_external_ref": external_ref,
                "reason": "operator_action",
            },
        )
        assert revoked.status_code == 200
        assert revoked.json() == {"disposition": "revoked", "revoked": True}

    stored = await proof["owner"].fetchrow(
        "SELECT authority_class,scopes,audience,purpose_name,purpose_digest,"
        "target_item_id,target_review_status,key_id,secret_digest "
        "FROM service_delegation_tokens WHERE external_ref=$1",
        external_ref,
    )
    assert stored is not None
    assert stored["authority_class"] == "review"
    assert stored["scopes"] == ["review"]
    assert stored["audience"] == "engram-core"
    assert stored["purpose_name"] == "review.queue"
    assert len(stored["purpose_digest"]) == 32
    assert stored["target_item_id"] is None
    assert stored["target_review_status"] is None
    assert token not in json.dumps(dict(stored), default=str)
    assert "reason" not in dict(stored)

    read_grant_id = uuid.uuid4()
    await proof["owner"].execute(
        "UPDATE service_clients SET permissions=$2 WHERE id=$1",
        proof["broker_id"],
        ["delegation.issue", "delegation.review.issue"],
    )
    await proof["owner"].execute(
        "INSERT INTO service_delegation_grants "
        "(id,issuer_service_client_id,binding_owner_service_client_id,"
        "authority_class,max_ttl_seconds) VALUES ($1,$2,$3,'read',60)",
        read_grant_id,
        proof["broker_id"],
        proof["binding_owner_id"],
    )
    grants = await proof["owner"].fetch(
        "SELECT authority_class FROM service_delegation_grants "
        "WHERE issuer_service_client_id=$1 AND binding_owner_service_client_id=$2 "
        "AND status='active' ORDER BY authority_class",
        proof["broker_id"],
        proof["binding_owner_id"],
    )
    assert [row["authority_class"] for row in grants] == ["read", "review"]


async def test_review_service_revoke_nonexistent_is_truthful_not_found(
    review_delegation_db: dict[str, Any],
) -> None:
    proof = review_delegation_db
    external_ref = f"missing-review-{uuid.uuid4().hex}"
    before_tokens = await proof["owner"].fetch(
        "SELECT id,status,revocation_reason FROM service_delegation_tokens "
        "WHERE issuer_service_client_id=$1 ORDER BY id",
        proof["broker_id"],
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/service/review-delegations/revoke",
            headers={"Authorization": f"Bearer {proof['broker_credential']}"},
            json={
                "binding_owner_service_client_slug": proof["owner_slug"],
                "tenant_external_ref": proof["tenant_ref"],
                "principal_external_ref": proof["principal_ref"],
                "delegation_external_ref": external_ref,
                "reason": "operator_action",
            },
        )
    assert response.status_code == 200
    assert response.json() == {"disposition": "not_found", "revoked": False}
    assert response.json().get("detail", {}).get("code") != "REVIEW_DELEGATION_UNAVAILABLE"
    _assert_sensitive_headers(response)

    after_tokens = await proof["owner"].fetch(
        "SELECT id,status,revocation_reason FROM service_delegation_tokens "
        "WHERE issuer_service_client_id=$1 ORDER BY id",
        proof["broker_id"],
    )
    assert after_tokens == before_tokens
    events = await proof["owner"].fetch(
        "SELECT event_type,outcome,issuer_service_client_id,issuer_credential_id,"
        "binding_owner_service_client_id,grant_id,delegation_token_id,tenant_id,"
        "principal_id,authority_class,purpose_name,reason_code,"
        "external_tenant_ref_digest,external_principal_ref_digest,"
        "external_delegation_ref_digest,details "
        "FROM service_delegation_events "
        "WHERE issuer_service_client_id=$1 AND reason_code='operator_action'",
        proof["broker_id"],
    )
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "delegation.resolved_existing"
    assert event["outcome"] == "success"
    assert event["issuer_service_client_id"] == proof["broker_id"]
    assert event["issuer_credential_id"] == proof["credential_id"]
    assert event["binding_owner_service_client_id"] == proof["binding_owner_id"]
    assert event["grant_id"] == proof["review_grant_id"]
    assert event["delegation_token_id"] is None
    assert event["tenant_id"] is None
    assert event["principal_id"] is None
    assert event["authority_class"] == "review"
    assert event["purpose_name"] is None
    assert event["external_tenant_ref_digest"] == hashlib.sha256(
        proof["tenant_ref"].encode()
    ).digest()
    assert event["external_principal_ref_digest"] == hashlib.sha256(
        proof["principal_ref"].encode()
    ).digest()
    assert event["external_delegation_ref_digest"] == hashlib.sha256(
        external_ref.encode()
    ).digest()
    assert json.loads(event["details"]) == {"disposition": "not_found"}


async def test_cross_class_idempotency_conflict_uses_existing_token_attribution(
    review_delegation_db: dict[str, Any],
) -> None:
    proof = review_delegation_db
    read_grant_id = await _enable_read_authority(proof)
    previous_delegation_enabled = settings.delegation_enabled
    settings.delegation_enabled = True
    idempotency_key = f"cross-class-{uuid.uuid4().hex}"
    read_external_ref = f"read-cross-class-{uuid.uuid4().hex}"
    review_external_ref = f"review-cross-class-{uuid.uuid4().hex}"
    headers = {
        "Authorization": f"Bearer {proof['broker_credential']}",
        "Idempotency-Key": idempotency_key,
    }
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            read_response = await client.post(
                "/v1/service/delegations",
                headers=headers,
                json={
                    "binding_owner_service_client_slug": proof["owner_slug"],
                    "tenant_external_ref": proof["tenant_ref"],
                    "principal_external_ref": proof["principal_ref"],
                    "delegation_external_ref": read_external_ref,
                    "ttl_seconds": 30,
                },
            )
            assert read_response.status_code == 201
            review_response = await client.post(
                "/v1/service/review-delegations",
                headers=headers,
                json={
                    "binding_owner_service_client_slug": proof["owner_slug"],
                    "tenant_external_ref": proof["tenant_ref"],
                    "principal_external_ref": proof["principal_ref"],
                    "delegation_external_ref": review_external_ref,
                    "purpose": {"kind": "review.queue"},
                    "ttl_seconds": 30,
                },
            )
    finally:
        settings.delegation_enabled = previous_delegation_enabled

    assert review_response.status_code == 409
    assert review_response.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert await proof["owner"].fetchval(
        "SELECT count(*) FROM service_delegation_tokens "
        "WHERE issuer_service_client_id=$1 AND authority_class='review'",
        proof["broker_id"],
    ) == 0
    read_token = await proof["owner"].fetchrow(
        "SELECT id,issuer_service_client_id,binding_owner_service_client_id,"
        "grant_id,tenant_id,principal_id,authority_class,purpose_name "
        "FROM service_delegation_tokens WHERE external_ref=$1",
        read_external_ref,
    )
    assert read_token is not None
    assert read_token["grant_id"] == read_grant_id
    event = await proof["owner"].fetchrow(
        "SELECT issuer_service_client_id,binding_owner_service_client_id,"
        "grant_id,delegation_token_id,tenant_id,principal_id,authority_class,"
        "purpose_name,reason_code FROM service_delegation_events "
        "WHERE delegation_token_id=$1 AND event_type='delegation.conflict'",
        read_token["id"],
    )
    assert event is not None
    assert event["reason_code"] == "idempotency_key_reused"
    for field in (
        "issuer_service_client_id",
        "binding_owner_service_client_id",
        "grant_id",
        "tenant_id",
        "principal_id",
        "authority_class",
        "purpose_name",
    ):
        assert event[field] == read_token[field]


async def test_delegation_event_token_attribution_is_database_enforced(
    review_delegation_db: dict[str, Any],
) -> None:
    import asyncpg

    proof = review_delegation_db
    read_grant_id = await _enable_read_authority(proof)
    _material, issued, _external_ref = await _issue(
        proof,
        purpose=canonical_review_queue_purpose(),
    )
    token = await proof["owner"].fetchrow(
        "SELECT id,issuer_service_client_id,issuer_credential_id,"
        "binding_owner_service_client_id,grant_id,tenant_id,principal_id,"
        "authority_class,purpose_name FROM service_delegation_tokens WHERE id=$1",
        issued["delegation_token_id"],
    )
    assert token is not None
    other_tenant_id = await proof["owner"].fetchval(
        "SELECT id FROM tenants WHERE id<>$1 ORDER BY created_at LIMIT 1",
        proof["tenant_id"],
    )
    assert other_tenant_id is not None
    mismatches = (
        {"authority_class": "read", "purpose_name": None},
        {"grant_id": read_grant_id},
        {"purpose_name": "review.transition"},
        {"tenant_id": other_tenant_id},
        {"principal_id": proof["other_principal_id"]},
    )
    for mismatch in mismatches:
        values = dict(token)
        values.update(mismatch)
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await proof["owner"].execute(
                "INSERT INTO service_delegation_events "
                "(event_type,outcome,issuer_service_client_id,issuer_credential_id,"
                "binding_owner_service_client_id,grant_id,delegation_token_id,"
                "tenant_id,principal_id,authority_class,purpose_name,request_id,"
                "reason_code,details) VALUES "
                "('delegation.conflict','failure',$1,$2,$3,$4,$5,$6,$7,$8,$9,"
                "$10,'direct_consistency_check','{\"disposition\":\"conflict\"}'::jsonb)",
                values["issuer_service_client_id"],
                values["issuer_credential_id"],
                values["binding_owner_service_client_id"],
                values["grant_id"],
                values["id"],
                values["tenant_id"],
                values["principal_id"],
                values["authority_class"],
                values["purpose_name"],
                f"event-consistency-{uuid.uuid4().hex}",
            )
    with pytest.raises(asyncpg.CheckViolationError):
        await proof["owner"].execute(
            "INSERT INTO service_delegation_events "
            "(event_type,outcome,issuer_service_client_id,issuer_credential_id,"
            "binding_owner_service_client_id,grant_id,delegation_token_id,"
            "tenant_id,principal_id,authority_class,purpose_name,request_id,"
            "reason_code,details) VALUES "
            "('delegation_grant.created','success',$1,$2,$3,$4,$5,$6,$7,"
            "'review',NULL,$8,'direct_consistency_check',"
            "'{\"disposition\":\"conflict\"}'::jsonb)",
            token["issuer_service_client_id"],
            token["issuer_credential_id"],
            token["binding_owner_service_client_id"],
            token["grant_id"],
            token["id"],
            token["tenant_id"],
            token["principal_id"],
            f"event-consistency-{uuid.uuid4().hex}",
        )


async def test_review_queue_is_fixed_visible_and_purpose_confined(
    review_delegation_db: dict[str, Any],
) -> None:
    proof = review_delegation_db
    visible_ids = {
        await _insert_item(proof, content=f"visible proposal {number}")
        for number in range(51)
    }
    await _insert_item(
        proof,
        principal_id=proof["other_principal_id"],
        content="ineligible private proposal",
    )
    await _insert_item(proof, review_status="active", content="active item")
    material, issued, _external_ref = await _issue(
        proof,
        purpose=canonical_review_queue_purpose(),
    )
    assert issued["created"] is True
    request_id = f"queue-{uuid.uuid4().hex}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/v1/review/queue",
            headers=_headers(material.token, request_id),
        )
        assert response.status_code == 200
        _assert_sensitive_headers(response, request_id)
        payload = response.json()
        assert len(payload) == 50
        assert {uuid.UUID(item["id"]) for item in payload} <= visible_ids
        assert all("delegated_review_token_id" not in item for item in payload)

        replay = await client.get(
            "/v1/review/queue",
            headers=_headers(material.token),
        )
        assert replay.status_code == 401
        _assert_sensitive_headers(replay)

        query_material, _row, _ref = await _issue(
            proof,
            purpose=canonical_review_queue_purpose(),
        )
        query_denied = await client.get(
            "/v1/review/queue?limit=1",
            headers=_headers(query_material.token),
        )
        assert query_denied.status_code == 401
        assert query_denied.json()["detail"] == "Invalid or revoked API key"
        _assert_sensitive_headers(query_denied)
        correct_after_mismatch = await client.get(
            "/v1/review/queue",
            headers=_headers(query_material.token),
        )
        assert correct_after_mismatch.status_code == 401

        route_material, route_row, _ref = await _issue(
            proof,
            purpose=canonical_review_queue_purpose(),
        )
        route_denied = await client.get(
            "/whoami",
            headers=_headers(route_material.token),
        )
        assert route_denied.status_code == 401
        _assert_sensitive_headers(route_denied)

        sample_item = next(iter(visible_ids))
        confined_requests: tuple[tuple[str, str, dict[str, Any] | None], ...] = (
            ("GET", "/v1/review/stats", None),
            ("GET", "/v1/review/stale", None),
            ("GET", "/v1/review/conflicts", None),
            ("GET", f"/v1/items/{sample_item}", None),
            ("POST", "/v1/recall", {"mode": "startup"}),
            ("POST", "/v1/search", {"mode": "keyword", "query": "visible"}),
            ("POST", "/v1/remember", {"content": "must not be stored"}),
            ("POST", f"/v1/items/{sample_item}/verify", {}),
            (
                "POST",
                f"/v1/items/{sample_item}/resolve-conflict",
                {"resolution": "accepted"},
            ),
            ("GET", "/v1/export/cca", None),
            ("GET", "/v1/admin/tenants", None),
            ("POST", "/v1/service/review-delegations", {}),
        )
        for method, path, request_body in confined_requests:
            confined_material, _row, _ref = await _issue(
                proof,
                purpose=canonical_review_queue_purpose(),
            )
            denied = await client.request(
                method,
                path,
                headers=_headers(confined_material.token),
                json=request_body,
            )
            assert denied.status_code in {401, 403, 404, 405, 422}
            _assert_sensitive_headers(denied)
    mismatch = await proof["owner"].fetchrow(
        "SELECT status,revocation_reason FROM service_delegation_tokens WHERE id=$1",
        route_row["delegation_token_id"],
    )
    assert tuple(mismatch.values()) == ("revoked", "purpose_mismatch")


@pytest.mark.parametrize("target_status", ["active", "rejected"])
async def test_review_transition_is_single_use_and_has_internal_attribution(
    review_delegation_db: dict[str, Any], target_status: str
) -> None:
    import asyncpg

    proof = review_delegation_db
    item_id = await _insert_item(proof, visibility="tenant")
    reason = f"explicit {target_status} decision"
    purpose = canonical_review_transition_purpose(
        item_id=str(item_id),
        review_status=target_status,
        reason=reason,
    )
    material, issued, _external_ref = await _issue(proof, purpose=purpose)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/v1/items/{item_id}/review",
            headers=_headers(material.token),
            json={"review_status": target_status, "reason": reason},
        )
        assert response.status_code == 200
        _assert_sensitive_headers(response)
        serialized = json.dumps(response.json(), sort_keys=True)
        for forbidden in (
            "delegated_review_token_id",
            "delegated_review_grant_id",
            "delegated_review_authority_class",
            "delegated_review_purpose",
            "purpose_digest",
            proof["broker_slug"],
        ):
            assert forbidden not in serialized

    assert await proof["owner"].fetchval(
        "SELECT review_status FROM memory_items WHERE id=$1",
        item_id,
    ) == target_status
    event = await proof["owner"].fetchrow(
        "SELECT actor_principal_id,delegated_review_token_id,delegated_review_grant_id,"
        "delegated_review_authority_class,delegated_review_purpose,reason "
        "FROM item_events WHERE item_id=$1 AND event_type='review_change'",
        item_id,
    )
    assert event is not None
    assert event["actor_principal_id"] == proof["principal_id"]
    assert event["delegated_review_token_id"] == issued["delegation_token_id"]
    assert event["delegated_review_grant_id"] == proof["review_grant_id"]
    assert event["delegated_review_authority_class"] == "review"
    assert event["delegated_review_purpose"] == "review.transition"
    assert event["reason"] == reason
    assert await proof["owner"].fetchval(
        "SELECT status FROM service_delegation_tokens WHERE id=$1",
        issued["delegation_token_id"],
    ) == "used"
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await proof["owner"].execute(
            "INSERT INTO item_events "
            "(item_id,tenant_id,memory_context_version,event_type,field_name,"
            "new_value,actor_principal_id,delegated_review_token_id,"
            "delegated_review_grant_id,delegated_review_authority_class,"
            "delegated_review_purpose) VALUES "
            "($1,$2,'legacy-unprofiled-v0','review_change','review_status',$3,"
            "$4,$5,$6,'review','review.transition')",
            item_id,
            proof["tenant_id"],
            target_status,
            proof["other_principal_id"],
            issued["delegation_token_id"],
            proof["review_grant_id"],
        )
    wrong_item_id = await _insert_item(proof, visibility="tenant")
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await proof["owner"].execute(
            "INSERT INTO item_events "
            "(item_id,tenant_id,memory_context_version,event_type,field_name,"
            "new_value,actor_principal_id,delegated_review_token_id,"
            "delegated_review_grant_id,delegated_review_authority_class,"
            "delegated_review_purpose) VALUES "
            "($1,$2,'legacy-unprofiled-v0','review_change','review_status',$3,"
            "$4,$5,$6,'review','review.transition')",
            wrong_item_id,
            proof["tenant_id"],
            target_status,
            proof["principal_id"],
            issued["delegation_token_id"],
            proof["review_grant_id"],
        )
    wrong_status = "rejected" if target_status == "active" else "active"
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await proof["owner"].execute(
            "INSERT INTO item_events "
            "(item_id,tenant_id,memory_context_version,event_type,field_name,"
            "new_value,actor_principal_id,delegated_review_token_id,"
            "delegated_review_grant_id,delegated_review_authority_class,"
            "delegated_review_purpose) VALUES "
            "($1,$2,'legacy-unprofiled-v0','review_change','review_status',$3,"
            "$4,$5,$6,'review','review.transition')",
            item_id,
            proof["tenant_id"],
            wrong_status,
            proof["principal_id"],
            issued["delegation_token_id"],
            proof["review_grant_id"],
        )
    _queue_material, queue_issued, _queue_ref = await _issue(
        proof,
        purpose=canonical_review_queue_purpose(),
    )
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await proof["owner"].execute(
            "INSERT INTO item_events "
            "(item_id,tenant_id,memory_context_version,event_type,field_name,"
            "new_value,actor_principal_id,delegated_review_token_id,"
            "delegated_review_grant_id,delegated_review_authority_class,"
            "delegated_review_purpose) VALUES "
            "($1,$2,'legacy-unprofiled-v0','review_change','review_status',$3,"
            "$4,$5,$6,'review','review.transition')",
            item_id,
            proof["tenant_id"],
            target_status,
            proof["principal_id"],
            queue_issued["delegation_token_id"],
            proof["review_grant_id"],
        )


async def test_transition_purpose_mismatches_revoke_without_memory_mutation(
    review_delegation_db: dict[str, Any],
) -> None:
    proof = review_delegation_db
    item_id = await _insert_item(proof, visibility="tenant")
    other_item_id = await _insert_item(proof, visibility="tenant")
    expected = {"review_status": "active", "reason": "exact reason"}
    cases: tuple[tuple[str, bytes, str], ...] = (
        (f"/v1/items/{other_item_id}/review", json.dumps(expected).encode(), "application/json"),
        (
            f"/v1/items/{item_id}/review?debug=1",
            json.dumps(expected).encode(),
            "application/json",
        ),
        (
            f"/v1/items/{str(item_id).upper()}/review",
            json.dumps(expected).encode(),
            "application/json",
        ),
        (
            f"/v1/items/{item_id}/review",
            json.dumps({"review_status": "rejected", "reason": "exact reason"}).encode(),
            "application/json",
        ),
        (
            f"/v1/items/{item_id}/review",
            json.dumps({"review_status": "active", "reason": "changed"}).encode(),
            "application/json",
        ),
        (
            f"/v1/items/{item_id}/review",
            b'{"review_status":"active","reason":"exact reason","review_notes":"x"}',
            "application/json",
        ),
        (
            f"/v1/items/{item_id}/review",
            b'{"review_status":"active","review_status":"active","reason":"exact reason"}',
            "application/json",
        ),
        (
            f"/v1/items/{item_id}/review",
            json.dumps(expected).encode(),
            "application/json; charset=ascii",
        ),
        (
            f"/v1/items/{item_id}/review",
            json.dumps(expected).encode(),
            "text/json",
        ),
        (
            f"/v1/items/{item_id}/review",
            b'{"review_status":',
            "application/json",
        ),
        (
            f"/v1/items/{item_id}/review",
            b'{"review_status":"active","reason":"\xff"}',
            "application/json",
        ),
        (
            f"/v1/items/{item_id}/review",
            b"x" * 4097,
            "application/json",
        ),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path, body, content_type in cases:
            purpose = canonical_review_transition_purpose(
                item_id=str(item_id),
                review_status="active",
                reason="exact reason",
            )
            material, issued, _external_ref = await _issue(proof, purpose=purpose)
            response = await client.post(
                path,
                headers=_headers(material.token) | {"Content-Type": content_type},
                content=body,
            )
            assert response.status_code == 401
            assert response.json()["detail"] == "Invalid or revoked API key"
            _assert_sensitive_headers(response)
            row = await proof["owner"].fetchrow(
                "SELECT status,revocation_reason FROM service_delegation_tokens WHERE id=$1",
                issued["delegation_token_id"],
            )
            assert tuple(row.values()) == ("revoked", "purpose_mismatch")
            assert await proof["owner"].fetchval(
                "SELECT count(*) FROM service_delegation_events "
                "WHERE delegation_token_id=$1 AND event_type='delegation.denied' "
                "AND reason_code='purpose_mismatch'",
                issued["delegation_token_id"],
            ) == 1
            retry = await client.post(
                f"/v1/items/{item_id}/review",
                headers=_headers(material.token),
                json=expected,
            )
            assert retry.status_code == 401
            assert retry.json()["detail"] == "Invalid or revoked API key"
    assert await proof["owner"].fetchval(
        "SELECT review_status FROM memory_items WHERE id=$1",
        item_id,
    ) == "proposed"
    assert await proof["owner"].fetchval(
        "SELECT count(*) FROM item_events WHERE item_id=$1",
        item_id,
    ) == 0


async def test_matching_missing_stale_and_opposing_transition_race_are_bounded(
    review_delegation_db: dict[str, Any],
) -> None:
    proof = review_delegation_db
    missing_id = uuid.uuid4()
    missing_purpose = canonical_review_transition_purpose(
        item_id=str(missing_id),
        review_status="active",
        reason=None,
    )
    missing_material, missing_row, _ref = await _issue(
        proof,
        purpose=missing_purpose,
    )
    stale_id = await _insert_item(proof, visibility="tenant")
    stale_purpose = canonical_review_transition_purpose(
        item_id=str(stale_id),
        review_status="active",
        reason=None,
    )
    stale_material, stale_row, _ref = await _issue(proof, purpose=stale_purpose)
    await proof["owner"].execute(
        "UPDATE memory_items SET review_status='active' WHERE id=$1",
        stale_id,
    )
    race_id = await _insert_item(proof, visibility="tenant")
    active_material, active_row, _ref = await _issue(
        proof,
        purpose=canonical_review_transition_purpose(
            item_id=str(race_id),
            review_status="active",
            reason=None,
        ),
    )
    rejected_material, rejected_row, _ref = await _issue(
        proof,
        purpose=canonical_review_transition_purpose(
            item_id=str(race_id),
            review_status="rejected",
            reason=None,
        ),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.post(
            f"/v1/items/{missing_id}/review",
            headers=_headers(missing_material.token),
            json={"review_status": "active"},
        )
        assert missing.status_code == 404
        _assert_sensitive_headers(missing)

        stale = await client.post(
            f"/v1/items/{stale_id}/review",
            headers=_headers(stale_material.token),
            json={"review_status": "active"},
        )
        assert stale.status_code == 409
        assert stale.json()["detail"] == "review item is no longer proposed"
        _assert_sensitive_headers(stale)

        async def transition(token: str, status: str):  # type: ignore[no-untyped-def]
            return await client.post(
                f"/v1/items/{race_id}/review",
                headers=_headers(token),
                json={"review_status": status},
            )

        active_response, rejected_response = await asyncio.gather(
            transition(active_material.token, "active"),
            transition(rejected_material.token, "rejected"),
        )
    assert sorted(
        [active_response.status_code, rejected_response.status_code]
    ) == [200, 409]
    assert await proof["owner"].fetchval(
        "SELECT count(*) FROM item_events "
        "WHERE item_id=$1 AND event_type='review_change'",
        race_id,
    ) == 1
    statuses = await proof["owner"].fetch(
        "SELECT id,status FROM service_delegation_tokens WHERE id=ANY($1::uuid[])",
        [
            missing_row["delegation_token_id"],
            stale_row["delegation_token_id"],
            active_row["delegation_token_id"],
            rejected_row["delegation_token_id"],
        ],
    )
    assert {row["status"] for row in statuses} == {"used"}


async def test_review_permission_loss_is_terminal_and_does_not_revoke_read_tokens(
    review_delegation_db: dict[str, Any],
) -> None:
    proof = review_delegation_db
    review_material, review_row, _ref = await _issue(
        proof,
        purpose=canonical_review_queue_purpose(),
    )
    read_grant = uuid.uuid4()
    await proof["owner"].execute(
        "UPDATE service_clients SET permissions=$2 WHERE id=$1",
        proof["broker_id"],
        ["delegation.issue", "delegation.review.issue"],
    )
    await proof["owner"].execute(
        "INSERT INTO service_delegation_grants "
        "(id,issuer_service_client_id,binding_owner_service_client_id,"
        "authority_class,max_ttl_seconds) VALUES ($1,$2,$3,'read',60)",
        read_grant,
        proof["broker_id"],
        proof["binding_owner_id"],
    )
    from engram.delegation_auth import generate_delegation_token

    read_material = generate_delegation_token()
    async with proof["provisioner"].transaction():
        await proof["provisioner"].execute(
            "SELECT set_config('app.service_client_id',$1,true)",
            str(proof["broker_id"]),
        )
        read_row = await proof["provisioner"].fetchrow(
            "SELECT * FROM issue_service_delegation("
            "$1,$2,$3,$4,$5,$6,$7,$8,$9,30,$10)",
            proof["credential_id"],
            proof["owner_slug"],
            proof["tenant_ref"],
            proof["principal_ref"],
            f"class-isolation-{uuid.uuid4().hex}",
            os.urandom(32),
            os.urandom(32),
            read_material.key_id,
            read_material.secret_digest,
            f"class-isolation-{uuid.uuid4().hex}",
        )
    assert read_row is not None and read_row["created"] is True
    await proof["owner"].execute(
        "UPDATE service_clients SET permissions=$2 WHERE id=$1",
        proof["broker_id"],
        ["delegation.issue"],
    )
    review_status = await proof["owner"].fetchrow(
        "SELECT status,revocation_reason FROM service_delegation_tokens WHERE id=$1",
        review_row["delegation_token_id"],
    )
    assert tuple(review_status.values()) == ("revoked", "authority_invalidated")
    assert await proof["owner"].fetchval(
        "SELECT status FROM service_delegation_tokens WHERE id=$1",
        read_row["delegation_token_id"],
    ) == "active"
    await proof["owner"].execute(
        "UPDATE service_clients SET permissions=$2 WHERE id=$1",
        proof["broker_id"],
        ["delegation.issue", "delegation.review.issue"],
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        denied = await client.get(
            "/v1/review/queue",
            headers=_headers(review_material.token),
        )
    assert denied.status_code == 401
    assert await proof["owner"].fetchval(
        "SELECT status FROM service_delegation_tokens WHERE id=$1",
        review_row["delegation_token_id"],
    ) == "revoked"


async def test_read_only_authority_cannot_issue_or_revoke_review_tokens(
    review_delegation_db: dict[str, Any],
) -> None:
    proof = review_delegation_db
    await proof["owner"].execute(
        "UPDATE service_clients SET permissions=ARRAY['delegation.issue'] WHERE id=$1",
        proof["broker_id"],
    )
    material, denied, external_ref = await _issue(
        proof,
        purpose=canonical_review_queue_purpose(),
    )
    assert material.token
    assert denied["created"] is False
    assert denied["error_code"] == "SERVICE_FORBIDDEN"
    assert await proof["owner"].fetchval(
        "SELECT count(*) FROM service_delegation_tokens WHERE external_ref=$1",
        external_ref,
    ) == 0
    async with proof["provisioner"].transaction():
        await proof["provisioner"].execute(
            "SELECT set_config('app.service_client_id',$1,true)",
            str(proof["broker_id"]),
        )
        revoked = await proof["provisioner"].fetchrow(
            "SELECT * FROM revoke_service_review_delegation($1,$2,$3,$4,$5,$6,$7)",
            proof["credential_id"],
            proof["owner_slug"],
            proof["tenant_ref"],
            proof["principal_ref"],
            external_ref,
            "operator_action",
            f"review-revoke-denied-{uuid.uuid4().hex}",
        )
    assert revoked is not None
    assert revoked["error_code"] == "SERVICE_FORBIDDEN"


async def test_review_grant_cli_lists_revokes_and_recreates_class(
    review_delegation_db: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    proof = review_delegation_db
    list_args = argparse.Namespace(
        delegation_grant_command="list",
        issuer=proof["broker_slug"],
        authority_class="review",
        json=True,
    )
    assert await _run_delegation_grant(list_args, proof["owner_url"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 1
    assert listed[0]["authority_class"] == "review"

    revoke_args = argparse.Namespace(
        delegation_grant_command="revoke",
        issuer=proof["broker_slug"],
        binding_owner=proof["owner_slug"],
        authority_class="review",
        reason="operator_action",
        json=True,
    )
    assert await _run_delegation_grant(revoke_args, proof["owner_url"]) == 0
    revoked = json.loads(capsys.readouterr().out)
    assert revoked["authority_class"] == "review"
    assert revoked["revoked"] is True

    create_args = argparse.Namespace(
        delegation_grant_command="create",
        issuer=proof["broker_slug"],
        binding_owner=proof["owner_slug"],
        authority_class="review",
        max_ttl_seconds=60,
        reason="operator_action",
        json=True,
    )
    assert await _run_delegation_grant(create_args, proof["owner_url"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["authority_class"] == "review"
    assert created["created"] is True


async def test_review_authority_refuses_downgrade_atomically(
    review_delegation_db: dict[str, Any],
) -> None:
    import asyncpg

    proof = review_delegation_db
    item_id = await _insert_item(proof, visibility="tenant")
    material, _issued, _external_ref = await _issue(
        proof,
        purpose=canonical_review_transition_purpose(
            item_id=str(item_id),
            review_status="active",
            reason="downgrade evidence",
        ),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/v1/items/{item_id}/review",
            headers=_headers(material.token),
            json={"review_status": "active", "reason": "downgrade evidence"},
        )
    assert response.status_code == 200
    assert await proof["owner"].fetchval(
        "SELECT count(*) FROM item_events "
        "WHERE item_id=$1 AND delegated_review_token_id IS NOT NULL",
        item_id,
    ) == 1
    downgrade_sql = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "downgrades"
        / "030_service_review_delegation.sql"
    ).read_text()
    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
        await proof["owner"].execute(downgrade_sql)
    assert await proof["owner"].fetchval(
        "SELECT EXISTS ("
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='service_delegation_tokens' "
        "AND column_name='authority_class')"
    )
    assert await proof["owner"].fetchval(
        "SELECT to_regprocedure("
        "'issue_service_review_delegation(uuid,text,text,text,text,bytea,bytea,"
        "text,text,integer,text,text,bytea,uuid,text)') IS NOT NULL"
    )
    assert await proof["owner"].fetchval(
        "SELECT authority_class FROM service_delegation_grants WHERE id=$1",
        proof["review_grant_id"],
    ) == "review"


async def test_provisioner_has_only_narrow_review_function_execution(
    review_delegation_db: dict[str, Any],
) -> None:
    import asyncpg

    proof = review_delegation_db
    generic_signature = (
        "issue_service_delegation_by_class(text,text,uuid,text,text,text,text,"
        "bytea,bytea,text,text,integer,text,text,bytea,uuid,text)"
    )
    assert not await proof["owner"].fetchval(
        "SELECT has_function_privilege('engram_provisioner',$1,'EXECUTE')",
        generic_signature,
    )
    assert await proof["owner"].fetchval(
        "SELECT has_function_privilege("
        "'engram_provisioner',"
        "'issue_service_review_delegation(uuid,text,text,text,text,bytea,bytea,"
        "text,text,integer,text,text,bytea,uuid,text)','EXECUTE')"
    )
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await proof["provisioner"].execute(
            "UPDATE service_delegation_tokens SET status='revoked'"
        )
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await proof["provisioner"].execute(
            "CREATE TABLE prohibited_review_table (id integer)"
        )
