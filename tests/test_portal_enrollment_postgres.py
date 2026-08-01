"""Real HTTP and PostgreSQL certification for Portal installation enrollment."""
# ruff: noqa: E501

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import uuid
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from engram.api.app import create_app
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


async def _connect():  # type: ignore[no-untyped-def]
    import asyncpg

    owner_url = _owner_url()
    if owner_url is None:
        pytest.skip("requires an owner PostgreSQL URL")
    try:
        return await asyncpg.connect(normalize_asyncpg_url(owner_url))
    except Exception:
        pytest.skip("requires a reachable owner PostgreSQL database")


def _pairing_secret() -> str:
    return "engpair_" + secrets.token_urlsafe(32)


def _credential() -> dict[str, str]:
    return _credential_material()[1]


def _credential_material() -> tuple[str, dict[str, str]]:
    raw = generate_service_credential()
    parsed = parse_service_credential(raw)
    return raw, {
        "key_id": parsed.key_id,
        "secret_digest": digest_service_secret(parsed.secret),
    }


def _body(installation: uuid.UUID | None = None) -> dict[str, Any]:
    return {
        "installation_external_ref": str(installation or uuid.uuid4()),
        "provisioner": _credential(),
        "read_broker": _credential(),
        "review_broker": _credential(),
    }


def _body_with_material(
    installation: uuid.UUID | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    roles = ("provisioner", "read_broker", "review_broker")
    generated = {role: _credential_material() for role in roles}
    body: dict[str, Any] = {
        "installation_external_ref": str(installation or uuid.uuid4()),
        **{role: generated[role][1] for role in roles},
    }
    return body, {role: generated[role][0] for role in roles}


def _rotation_body(
    installation: str,
    *,
    generation: int = 1,
    rotation: uuid.UUID | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    body, credentials = _body_with_material(uuid.UUID(installation))
    body["expected_credential_generation"] = generation
    body["rotation_external_ref"] = str(rotation or uuid.uuid4())
    return body, credentials


def _configure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pairing_secret: str
) -> Path:
    from engram.config import settings

    path = tmp_path / f"pairing-{uuid.uuid4().hex}"
    path.write_text(pairing_secret + "\n", encoding="ascii")
    path.chmod(0o600)
    monkeypatch.setattr(settings, "portal_enrollment_enabled", True)
    monkeypatch.setattr(settings, "portal_enrollment_require_https", False)
    monkeypatch.setattr(settings, "portal_enrollment_secret_file", str(path))
    monkeypatch.setattr(settings, "service_provisioning_enabled", True)
    monkeypatch.setattr(settings, "delegation_enabled", True)
    monkeypatch.setattr(settings, "review_delegation_enabled", True)
    monkeypatch.setattr(settings, "delegation_max_ttl_seconds", 60)
    monkeypatch.setattr(settings, "review_delegation_max_ttl_seconds", 60)
    return path


def _headers(pairing_secret: str, idempotency_key: str = "portal-bootstrap-v1") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {pairing_secret}",
        "Idempotency-Key": idempotency_key,
    }


async def test_enrollment_http_success_replay_and_exact_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pairing_secret = _pairing_secret()
    _configure(monkeypatch, tmp_path, pairing_secret)
    body = _body()
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/service/portal-installation-enrollments",
            headers=_headers(pairing_secret),
            json=body,
        )
        replay = await client.post(
            "/v1/service/portal-installation-enrollments",
            headers=_headers(pairing_secret),
            json=body,
        )

    assert created.status_code == 201
    assert created.json() == {
        "status": "completed",
        "idempotency_replayed": False,
        "provisioner": "ready",
        "read_delegation": "ready",
        "review_delegation": "ready",
    }
    assert replay.status_code == 200
    assert replay.json()["idempotency_replayed"] is True
    assert replay.headers["idempotency-replayed"] == "true"
    assert "secret" not in created.text
    for response in (created, replay):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"
        assert response.headers["referrer-policy"] == "no-referrer"

    owner = await _connect()
    try:
        rows = await owner.fetch(
            "SELECT enrolled.role, client.permissions, credential.key_id, "
            "credential.secret_digest, credential.status "
            "FROM portal_installation_enrollments enrollment "
            "JOIN portal_installation_enrollment_clients enrolled "
            "ON enrolled.enrollment_id=enrollment.id "
            "JOIN service_clients client ON client.id=enrolled.service_client_id "
            "JOIN service_client_credentials credential "
            "ON credential.id=enrolled.service_credential_id "
            "WHERE enrollment.installation_external_ref=$1 ORDER BY enrolled.role",
            uuid.UUID(body["installation_external_ref"]),
        )
        assert len(rows) == 3
        by_role = {row["role"]: row for row in rows}
        assert by_role["provisioner"]["permissions"] == [
            "tenant.provision",
            "principal.provision",
            "workspace.provision",
            "agent.provision",
            "api_key.provision",
        ]
        assert by_role["read_broker"]["permissions"] == ["delegation.issue"]
        assert by_role["review_broker"]["permissions"] == ["delegation.review.issue"]
        assert all(row["status"] == "active" for row in rows)
        grants = await owner.fetch(
            "SELECT issuer.role issuer_role, owner.role owner_role, "
            "delegation_grant.authority_class, delegation_grant.max_ttl_seconds, "
            "delegation_grant.status "
            "FROM portal_installation_enrollments enrollment "
            "JOIN portal_installation_enrollment_clients issuer "
            "ON issuer.enrollment_id=enrollment.id "
            "JOIN service_delegation_grants delegation_grant "
            "ON delegation_grant.issuer_service_client_id=issuer.service_client_id "
            "JOIN portal_installation_enrollment_clients owner "
            "ON owner.service_client_id=delegation_grant.binding_owner_service_client_id "
            "WHERE enrollment.installation_external_ref=$1 "
            "ORDER BY delegation_grant.authority_class",
            uuid.UUID(body["installation_external_ref"]),
        )
        assert [tuple(row.values()) for row in grants] == [
            ("read_broker", "provisioner", "read", 60, "active"),
            ("review_broker", "provisioner", "review", 60, "active"),
        ]
        assert await owner.fetchval(
            "SELECT count(*) FROM portal_installation_enrollment_events event "
            "JOIN portal_installation_enrollments enrollment ON enrollment.id=event.enrollment_id "
            "WHERE enrollment.installation_external_ref=$1",
            uuid.UUID(body["installation_external_ref"]),
        ) == 1
    finally:
        await owner.close()


@pytest.mark.parametrize("change", ["digest", "installation", "idempotency"])
async def test_changed_enrollment_conflicts_without_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, change: str
) -> None:
    pairing_secret = _pairing_secret()
    _configure(monkeypatch, tmp_path, pairing_secret)
    body = _body()
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        first = await client.post(
            "/v1/service/portal-installation-enrollments",
            headers=_headers(pairing_secret),
            json=body,
        )
        changed = {**body}
        headers = _headers(pairing_secret)
        if change == "digest":
            changed = {**body, "read_broker": _credential()}
        elif change == "installation":
            changed = {**body, "installation_external_ref": str(uuid.uuid4())}
        else:
            headers = _headers(pairing_secret, "different-idempotency")
        conflict = await client.post(
            "/v1/service/portal-installation-enrollments", headers=headers, json=changed
        )
    assert first.status_code == 201
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "ENROLLMENT_CONFLICT"

    owner = await _connect()
    try:
        assert await owner.fetchval(
            "SELECT count(*) FROM portal_installation_enrollments "
            "WHERE installation_external_ref=$1",
            uuid.UUID(body["installation_external_ref"]),
        ) == 1
        enrolled_id = await owner.fetchval(
            "SELECT id FROM portal_installation_enrollments WHERE installation_external_ref=$1",
            uuid.UUID(body["installation_external_ref"]),
        )
        assert await owner.fetchval(
            "SELECT count(*) FROM portal_installation_enrollment_clients WHERE enrollment_id=$1",
            enrolled_id,
        ) == 3
    finally:
        await owner.close()


async def test_concurrent_identical_enrollment_creates_one_authority_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pairing_secret = _pairing_secret()
    _configure(monkeypatch, tmp_path, pairing_secret)
    body = _body()
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        responses = await asyncio.gather(
            *(
                client.post(
                    "/v1/service/portal-installation-enrollments",
                    headers=_headers(pairing_secret),
                    json=body,
                )
                for _ in range(4)
            )
        )
    assert sorted(response.status_code for response in responses) == [200, 200, 200, 201]
    assert sum(response.json()["idempotency_replayed"] is False for response in responses) == 1


async def test_concurrent_conflicting_enrollment_has_one_bounded_winner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pairing_secret = _pairing_secret()
    _configure(monkeypatch, tmp_path, pairing_secret)
    installation = uuid.uuid4()
    bodies = [_body(installation), _body(installation)]
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        responses = await asyncio.gather(
            *(
                client.post(
                    "/v1/service/portal-installation-enrollments",
                    headers=_headers(pairing_secret),
                    json=body,
                )
                for body in bodies
            )
        )
    assert sorted(response.status_code for response in responses) == [201, 409]
    assert all("secret" not in response.text for response in responses)


async def test_disabled_malformed_duplicate_and_arbitrary_requests_do_not_mutate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from engram.config import settings

    pairing_secret = _pairing_secret()
    _configure(monkeypatch, tmp_path, pairing_secret)
    app = create_app()
    body = _body()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        monkeypatch.setattr(settings, "portal_enrollment_enabled", False)
        disabled = await client.post(
            "/v1/service/portal-installation-enrollments",
            headers=_headers(pairing_secret),
            json=body,
        )
        monkeypatch.setattr(settings, "portal_enrollment_enabled", True)
        malformed = await client.post(
            "/v1/service/portal-installation-enrollments",
            headers=_headers("engsvc_" + "A" * 43),
            json=body,
        )
        duplicate = await client.post(
            "/v1/service/portal-installation-enrollments",
            headers=[
                ("Authorization", f"Bearer {pairing_secret}"),
                ("Authorization", f"Bearer {pairing_secret}"),
                ("Idempotency-Key", "duplicate-auth"),
            ],
            json=body,
        )
        arbitrary = await client.post(
            "/v1/service/portal-installation-enrollments",
            headers=_headers(pairing_secret),
            json={**body, "permissions": ["admin"], "slug": "caller-selected", "ttl": 300},
        )
    assert disabled.status_code == 404
    assert malformed.status_code == 401
    assert duplicate.status_code == 401
    assert arbitrary.status_code == 422

    owner = await _connect()
    try:
        assert await owner.fetchval(
            "SELECT count(*) FROM portal_installation_enrollments "
            "WHERE installation_external_ref=$1",
            uuid.UUID(body["installation_external_ref"]),
        ) == 0
    finally:
        await owner.close()


@pytest.mark.parametrize(
    "stage",
    [
        "service_clients",
        "service_client_credentials",
        "service_delegation_grants",
        "portal_installation_enrollments",
        "portal_installation_enrollment_clients",
        "portal_installation_enrollment_events",
    ],
)
async def test_failure_after_each_creation_stage_rolls_back_all_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stage: str
) -> None:
    pairing_secret = _pairing_secret()
    _configure(monkeypatch, tmp_path, pairing_secret)
    body = _body()
    owner = await _connect()
    trigger_name = f"test_portal_failure_{uuid.uuid4().hex}"
    function_name = f"{trigger_name}_fn"
    try:
        await owner.execute(
            f"CREATE FUNCTION {function_name}() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'injected enrollment failure'; END; "
            "$$ LANGUAGE plpgsql"
        )
        await owner.execute(
            f"CREATE TRIGGER {trigger_name} AFTER INSERT ON {stage} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION {function_name}()"
        )
        async with AsyncClient(
            transport=ASGITransport(app=create_app()), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/service/portal-installation-enrollments",
                headers=_headers(pairing_secret),
                json=body,
            )
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "ENROLLMENT_UNAVAILABLE"
        assert "injected" not in response.text
        slug_prefix = "portal-" + body["installation_external_ref"].replace("-", "") + "-%"
        assert await owner.fetchval(
            "SELECT count(*) FROM service_clients WHERE slug LIKE $1", slug_prefix
        ) == 0
        assert await owner.fetchval(
            "SELECT count(*) FROM portal_installation_enrollments "
            "WHERE installation_external_ref=$1",
            uuid.UUID(body["installation_external_ref"]),
        ) == 0
    finally:
        await owner.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON {stage}")
        await owner.execute(f"DROP FUNCTION IF EXISTS {function_name}()")
        await owner.close()


async def test_database_rejects_authority_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import asyncpg

    pairing_secret = _pairing_secret()
    _configure(monkeypatch, tmp_path, pairing_secret)
    body = _body()
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/service/portal-installation-enrollments",
            headers=_headers(pairing_secret),
            json=body,
        )
    assert response.status_code == 201

    owner = await _connect()
    try:
        clients = await owner.fetch(
            "SELECT enrolled.role, enrolled.service_client_id, enrolled.service_credential_id "
            "FROM portal_installation_enrollments enrollment "
            "JOIN portal_installation_enrollment_clients enrolled "
            "ON enrolled.enrollment_id=enrollment.id "
            "WHERE enrollment.installation_external_ref=$1",
            uuid.UUID(body["installation_external_ref"]),
        )
        by_role = {row["role"]: row for row in clients}
        with pytest.raises(asyncpg.CheckViolationError):
            async with owner.transaction():
                await owner.execute(
                    "UPDATE service_clients SET permissions=$2 WHERE id=$1",
                    by_role["provisioner"]["service_client_id"],
                    ["tenant.provision"],
                )
        with pytest.raises(asyncpg.CheckViolationError):
            async with owner.transaction():
                await owner.execute(
                    "INSERT INTO service_client_credentials "
                    "(service_client_id,key_id,secret_digest) VALUES ($1,$2,$3)",
                    by_role["read_broker"]["service_client_id"],
                    generate_service_credential().split("_")[1],
                    "f" * 64,
                )
        with pytest.raises(asyncpg.CheckViolationError):
            async with owner.transaction():
                await owner.execute(
                    "UPDATE service_delegation_grants SET max_ttl_seconds=59 "
                    "WHERE issuer_service_client_id=$1",
                    by_role["read_broker"]["service_client_id"],
                )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await owner.execute(
                "UPDATE portal_installation_enrollments SET request_digest=$2 "
                "WHERE installation_external_ref=$1",
                uuid.UUID(body["installation_external_ref"]),
                b"x" * 32,
            )
    finally:
        await owner.close()


async def test_rotation_http_replay_status_and_credential_cutover(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials

    from engram.service_auth import get_current_service_client

    pairing_secret = _pairing_secret()
    _configure(monkeypatch, tmp_path, pairing_secret)
    body, old_credentials = _body_with_material()
    rotation, new_credentials = _rotation_body(body["installation_external_ref"])
    rotation_headers = _headers(pairing_secret, "portal-rotation-v2")
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        enrolled = await client.post(
            "/v1/service/portal-installation-enrollments",
            headers=_headers(pairing_secret),
            json=body,
        )
        rotated = await client.post(
            "/v1/service/portal-installation-enrollments/rotate-credentials",
            headers=rotation_headers,
            json=rotation,
        )
        replay = await client.post(
            "/v1/service/portal-installation-enrollments/rotate-credentials",
            headers=rotation_headers,
            json=rotation,
        )
        enrollment_status = await client.get(
            "/v1/service/portal-installation-enrollments/"
            + body["installation_external_ref"],
            headers={"Authorization": f"Bearer {pairing_secret}"},
        )

    assert enrolled.status_code == 201
    assert rotated.status_code == 201
    assert rotated.json() == {
        "status": "active",
        "credential_generation": 2,
        "idempotency_replayed": False,
    }
    assert replay.status_code == 200
    assert replay.json() == {
        "status": "active",
        "credential_generation": 2,
        "idempotency_replayed": True,
    }
    assert replay.headers["idempotency-replayed"] == "true"
    assert enrollment_status.status_code == 200
    assert enrollment_status.json() == {
        "status": "active",
        "credential_generation": 2,
        "provisioner": "ready",
        "read_delegation": "ready",
        "review_delegation": "ready",
    }
    for forbidden in ("key_id", "secret_digest", "permissions", "grant_id"):
        assert forbidden not in rotated.text
        assert forbidden not in enrollment_status.text

    for credential in old_credentials.values():
        with pytest.raises(HTTPException) as raised:
            await get_current_service_client(
                HTTPAuthorizationCredentials(scheme="Bearer", credentials=credential)
            )
        assert raised.value.status_code == 401
    expected_permissions = {
        "provisioner": (
            "tenant.provision",
            "principal.provision",
            "workspace.provision",
            "agent.provision",
            "api_key.provision",
        ),
        "read_broker": ("delegation.issue",),
        "review_broker": ("delegation.review.issue",),
    }
    for role, credential in new_credentials.items():
        identity = await get_current_service_client(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=credential)
        )
        assert identity.permissions == expected_permissions[role]

    installation_slug = body["installation_external_ref"].replace("-", "")
    provision_request = {
        "tenant": {
            "external_ref": f"rotation-tenant-{uuid.uuid4().hex}",
            "name": "Rotation authority test tenant",
            "slug": f"rotation-{uuid.uuid4().hex}",
        },
        "human_principal": {
            "external_ref": f"rotation-human-{uuid.uuid4().hex}",
            "name": "Rotation authority test owner",
        },
    }
    read_request = {
        "binding_owner_service_client_slug": (
            f"portal-{installation_slug}-provisioner"
        ),
        "tenant_external_ref": "missing-tenant",
        "principal_external_ref": "missing-principal",
        "delegation_external_ref": f"rotation-read-{uuid.uuid4().hex}",
        "ttl_seconds": 60,
    }
    review_request = {
        **read_request,
        "delegation_external_ref": f"rotation-review-{uuid.uuid4().hex}",
        "purpose": {"kind": "review.queue"},
    }
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        old_provisioner = await client.post(
            "/v1/service/provisioning/tenant-human",
            headers={
                "Authorization": f"Bearer {old_credentials['provisioner']}",
                "Idempotency-Key": "old-provisioner-after-rotation",
            },
            json=provision_request,
        )
        new_provisioner = await client.post(
            "/v1/service/provisioning/tenant-human",
            headers={
                "Authorization": f"Bearer {new_credentials['provisioner']}",
            },
            json=provision_request,
        )
        old_read = await client.post(
            "/v1/service/delegations",
            headers={
                "Authorization": f"Bearer {old_credentials['read_broker']}",
                "Idempotency-Key": "old-read-after-rotation",
            },
            json=read_request,
        )
        new_read = await client.post(
            "/v1/service/delegations",
            headers={
                "Authorization": f"Bearer {new_credentials['read_broker']}",
                "Idempotency-Key": "new-read-after-rotation",
            },
            json=read_request,
        )
        old_review = await client.post(
            "/v1/service/review-delegations",
            headers={
                "Authorization": f"Bearer {old_credentials['review_broker']}",
                "Idempotency-Key": "old-review-after-rotation",
            },
            json=review_request,
        )
        new_review = await client.post(
            "/v1/service/review-delegations",
            headers={
                "Authorization": f"Bearer {new_credentials['review_broker']}",
                "Idempotency-Key": "new-review-after-rotation",
            },
            json=review_request,
        )
        wrong_domain = await client.post(
            "/v1/service/delegations",
            headers={
                "Authorization": f"Bearer {new_credentials['review_broker']}",
                "Idempotency-Key": "wrong-domain-after-rotation",
            },
            json=read_request,
        )
    assert old_provisioner.status_code == 401
    assert new_provisioner.status_code == 422
    assert old_read.status_code == 401
    assert new_read.status_code == 404
    assert old_review.status_code == 401
    assert new_review.status_code == 404
    assert wrong_domain.status_code == 403

    owner = await _connect()
    try:
        enrollment_id = await owner.fetchval(
            "SELECT id FROM portal_installation_enrollments "
            "WHERE installation_external_ref=$1",
            uuid.UUID(body["installation_external_ref"]),
        )
        rows = await owner.fetch(
            "SELECT enrolled.role,enrolled.credential_generation,client.slug,"
            "client.display_name,client.permissions,credential.status,"
            "credential.portal_enrollment_revocation_reason "
            "FROM portal_installation_enrollment_clients enrolled "
            "JOIN service_clients client ON client.id=enrolled.service_client_id "
            "JOIN service_client_credentials credential "
            "ON credential.service_client_id=client.id "
            "WHERE enrolled.enrollment_id=$1 ORDER BY enrolled.role,credential.created_at",
            enrollment_id,
        )
        assert len(rows) == 6
        assert sum(row["status"] == "active" for row in rows) == 3
        assert sum(row["status"] == "revoked" for row in rows) == 3
        assert all(row["credential_generation"] == 2 for row in rows)
        assert all(
            row["portal_enrollment_revocation_reason"] == "credential_rotation"
            for row in rows
            if row["status"] == "revoked"
        )
        assert await owner.fetchval(
            "SELECT count(*) FROM portal_installation_enrollment_events "
            "WHERE enrollment_id=$1",
            enrollment_id,
        ) == 2
        assert await owner.fetchval(
            "SELECT count(*) FROM service_delegation_grants grant_row "
            "JOIN portal_installation_enrollment_clients enrolled "
            "ON enrolled.service_client_id=grant_row.issuer_service_client_id "
            "WHERE enrolled.enrollment_id=$1 AND grant_row.status='active' "
            "AND grant_row.max_ttl_seconds=60",
            enrollment_id,
        ) == 2
    finally:
        await owner.close()


async def test_rotation_conflicts_and_concurrency_are_bounded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pairing_secret = _pairing_secret()
    _configure(monkeypatch, tmp_path, pairing_secret)
    body = _body()
    rotation, _ = _rotation_body(body["installation_external_ref"])
    headers = _headers(pairing_secret, "concurrent-rotation")
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        assert (
            await client.post(
                "/v1/service/portal-installation-enrollments",
                headers=_headers(pairing_secret),
                json=body,
            )
        ).status_code == 201
        responses = await asyncio.gather(
            *(
                client.post(
                    "/v1/service/portal-installation-enrollments/rotate-credentials",
                    headers=headers,
                    json=rotation,
                )
                for _ in range(4)
            )
        )
        changed = {**rotation, "read_broker": _credential()}
        conflict = await client.post(
            "/v1/service/portal-installation-enrollments/rotate-credentials",
            headers=headers,
            json=changed,
        )
        stale, _ = _rotation_body(body["installation_external_ref"], generation=1)
        stale_response = await client.post(
            "/v1/service/portal-installation-enrollments/rotate-credentials",
            headers=_headers(pairing_secret, "stale-rotation"),
            json=stale,
        )

    assert sorted(response.status_code for response in responses) == [200, 200, 200, 201]
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "ROTATION_CONFLICT"
    assert stale_response.status_code == 409
    assert stale_response.json()["detail"]["code"] == "STALE_CREDENTIAL_GENERATION"
    assert all("secret" not in response.text for response in [*responses, conflict, stale_response])

    owner = await _connect()
    try:
        enrollment_id = await owner.fetchval(
            "SELECT id FROM portal_installation_enrollments "
            "WHERE installation_external_ref=$1",
            uuid.UUID(body["installation_external_ref"]),
        )
        assert await owner.fetchval(
            "SELECT credential_generation FROM portal_installation_enrollments WHERE id=$1",
            enrollment_id,
        ) == 2
        assert await owner.fetchval(
            "SELECT count(*) FROM service_client_credentials credential "
            "JOIN portal_installation_enrollment_clients enrolled "
            "ON enrolled.service_client_id=credential.service_client_id "
            "WHERE enrolled.enrollment_id=$1",
            enrollment_id,
        ) == 6
        assert await owner.fetchval(
            "SELECT count(*) FROM portal_installation_enrollment_events "
            "WHERE enrollment_id=$1",
            enrollment_id,
        ) == 2
    finally:
        await owner.close()


async def test_concurrent_conflicting_rotations_have_one_winner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pairing_secret = _pairing_secret()
    _configure(monkeypatch, tmp_path, pairing_secret)
    body = _body()
    rotation_ref = uuid.uuid4()
    rotations = [
        _rotation_body(body["installation_external_ref"], rotation=rotation_ref)[0]
        for _ in range(2)
    ]
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        assert (
            await client.post(
                "/v1/service/portal-installation-enrollments",
                headers=_headers(pairing_secret),
                json=body,
            )
        ).status_code == 201
        responses = await asyncio.gather(
            *(
                client.post(
                    "/v1/service/portal-installation-enrollments/rotate-credentials",
                    headers=_headers(pairing_secret, "conflicting-rotation"),
                    json=rotation,
                )
                for rotation in rotations
            )
        )
    assert sorted(response.status_code for response in responses) == [201, 409]
    assert next(
        response for response in responses if response.status_code == 409
    ).json()["detail"]["code"] == "ROTATION_CONFLICT"


@pytest.mark.parametrize(
    "stage",
    [
        "replacement_provisioner",
        "replacement_read_broker",
        "replacement_review_broker",
        "prior_credential_revocation",
        "current_pointer_update",
        "generation_update",
        "rotation_event",
    ],
)
async def test_rotation_failure_at_each_stage_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stage: str,
) -> None:
    pairing_secret = _pairing_secret()
    _configure(monkeypatch, tmp_path, pairing_secret)
    body = _body()
    rotation, _ = _rotation_body(body["installation_external_ref"])
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        assert (
            await client.post(
                "/v1/service/portal-installation-enrollments",
                headers=_headers(pairing_secret),
                json=body,
            )
        ).status_code == 201

    owner = await _connect()
    trigger_name = f"test_rotation_failure_{uuid.uuid4().hex}"
    function_name = f"{trigger_name}_fn"
    table = "service_client_credentials"
    trigger_action = "AFTER UPDATE OF status"
    function_body = "RAISE EXCEPTION 'injected rotation failure';"
    if stage.startswith("replacement_"):
        role = stage.removeprefix("replacement_")
        trigger_action = "AFTER INSERT"
        function_body = (
            "IF NEW.label LIKE 'Portal credential generation %' AND EXISTS ("
            "SELECT 1 FROM portal_installation_enrollment_clients "
            f"WHERE service_client_id=NEW.service_client_id AND role='{role}'"
            ") THEN RAISE EXCEPTION 'injected rotation failure'; END IF;"
        )
    elif stage == "prior_credential_revocation":
        function_body = (
            "IF NEW.portal_enrollment_revocation_reason='credential_rotation' "
            "THEN RAISE EXCEPTION 'injected rotation failure'; END IF;"
        )
    elif stage == "current_pointer_update":
        table = "portal_installation_enrollment_clients"
        trigger_action = "AFTER UPDATE OF service_credential_id"
    elif stage == "generation_update":
        table = "portal_installation_enrollments"
        trigger_action = "AFTER UPDATE OF credential_generation"
    else:
        table = "portal_installation_enrollment_events"
        trigger_action = "AFTER INSERT"
        function_body = (
            "IF NEW.event_type='enrollment.credentials_rotated' "
            "THEN RAISE EXCEPTION 'injected rotation failure'; END IF;"
        )
    try:
        await owner.execute(
            f"CREATE FUNCTION {function_name}() RETURNS trigger AS $$ "
            f"BEGIN {function_body} RETURN NEW; END; $$ LANGUAGE plpgsql"
        )
        await owner.execute(
            f"CREATE TRIGGER {trigger_name} {trigger_action} ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {function_name}()"
        )
        async with AsyncClient(
            transport=ASGITransport(app=create_app()), base_url="http://test"
        ) as client:
            failed = await client.post(
                "/v1/service/portal-installation-enrollments/rotate-credentials",
                headers=_headers(pairing_secret, f"rollback-{stage}"),
                json=rotation,
            )
        assert failed.status_code == 503
        assert failed.json()["detail"]["code"] == "ENROLLMENT_UNAVAILABLE"
        assert "injected" not in failed.text

        enrollment_id = await owner.fetchval(
            "SELECT id FROM portal_installation_enrollments "
            "WHERE installation_external_ref=$1",
            uuid.UUID(body["installation_external_ref"]),
        )
        assert await owner.fetchval(
            "SELECT credential_generation FROM portal_installation_enrollments WHERE id=$1",
            enrollment_id,
        ) == 1
        assert await owner.fetchval(
            "SELECT count(*) FROM service_client_credentials credential "
            "JOIN portal_installation_enrollment_clients enrolled "
            "ON enrolled.service_client_id=credential.service_client_id "
            "WHERE enrolled.enrollment_id=$1",
            enrollment_id,
        ) == 3
        assert await owner.fetchval(
            "SELECT count(*) FROM service_client_credentials credential "
            "JOIN portal_installation_enrollment_clients enrolled "
            "ON enrolled.service_credential_id=credential.id "
            "WHERE enrolled.enrollment_id=$1 AND credential.status='active' "
            "AND enrolled.credential_generation=1",
            enrollment_id,
        ) == 3
        assert await owner.fetchval(
            "SELECT count(*) FROM portal_installation_enrollment_events "
            "WHERE enrollment_id=$1",
            enrollment_id,
        ) == 1
    finally:
        await owner.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table}")
        await owner.execute(f"DROP FUNCTION IF EXISTS {function_name}()")
        await owner.close()


async def test_rotation_and_termination_functions_are_owner_only() -> None:
    import asyncpg

    owner = await _connect()
    try:
        assert await owner.fetchval(
            "SELECT has_function_privilege(current_user, "
            "'rotate_portal_installation_credentials(bytea,uuid,integer,uuid,bytea,bytea,"
            "text,text,text,text,text,text,text)', 'EXECUTE')"
        )
        assert await owner.fetchval(
            "SELECT has_function_privilege(current_user, "
            "'terminate_portal_installation_enrollment(uuid,text,text)', 'EXECUTE')"
        )
    finally:
        await owner.close()

    for environment_name in ("ENGRAM_APP_DATABASE_URL", "ENGRAM_PROVISIONER_DATABASE_URL"):
        role_url = os.getenv(environment_name)
        if role_url is None:
            pytest.skip(f"requires {environment_name}")
        role_connection = await asyncpg.connect(normalize_asyncpg_url(role_url))
        try:
            assert not await role_connection.fetchval(
                "SELECT has_function_privilege(current_user, "
                "'rotate_portal_installation_credentials(bytea,uuid,integer,uuid,bytea,"
                "bytea,text,text,text,text,text,text,text)', 'EXECUTE')"
            )
            assert not await role_connection.fetchval(
                "SELECT has_function_privilege(current_user, "
                "'terminate_portal_installation_enrollment(uuid,text,text)', 'EXECUTE')"
            )
        finally:
            await role_connection.close()


async def test_owner_cli_termination_is_terminal_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from engram.cli import (
        _run_delegation_grant,
        _run_portal_enrollment,
        _run_service_client,
    )

    owner_url = _owner_url()
    if owner_url is None:
        pytest.skip("requires an owner PostgreSQL URL")
    pairing_secret = _pairing_secret()
    _configure(monkeypatch, tmp_path, pairing_secret)
    body = _body()
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        assert (
            await client.post(
                "/v1/service/portal-installation-enrollments",
                headers=_headers(pairing_secret),
                json=body,
            )
        ).status_code == 201

    terminate_args = argparse.Namespace(
        portal_enrollment_command="terminate",
        installation=body["installation_external_ref"],
        reason="security_incident",
        json=False,
    )
    assert await _run_portal_enrollment(terminate_args, owner_url) == 0
    assert capsys.readouterr().out.strip() == "terminated"
    assert await _run_portal_enrollment(terminate_args, owner_url) == 0
    assert capsys.readouterr().out.strip() == "already terminated"

    owner = await _connect()
    try:
        enrollment = await owner.fetchrow(
            "SELECT id,status,credential_generation,termination_reason "
            "FROM portal_installation_enrollments WHERE installation_external_ref=$1",
            uuid.UUID(body["installation_external_ref"]),
        )
        assert enrollment is not None
        assert tuple(enrollment.values())[1:] == ("terminated", 1, "security_incident")
        assert await owner.fetchval(
            "SELECT count(*) FROM service_client_credentials credential "
            "JOIN portal_installation_enrollment_clients enrolled "
            "ON enrolled.service_client_id=credential.service_client_id "
            "WHERE enrolled.enrollment_id=$1 AND credential.status='active'",
            enrollment["id"],
        ) == 0
        assert await owner.fetchval(
            "SELECT count(*) FROM service_clients client "
            "JOIN portal_installation_enrollment_clients enrolled "
            "ON enrolled.service_client_id=client.id "
            "WHERE enrolled.enrollment_id=$1 AND client.status='active'",
            enrollment["id"],
        ) == 0
        assert await owner.fetchval(
            "SELECT count(*) FROM service_delegation_grants grant_row "
            "JOIN portal_installation_enrollment_clients enrolled "
            "ON enrolled.service_client_id=grant_row.issuer_service_client_id "
            "WHERE enrolled.enrollment_id=$1 AND grant_row.status='active'",
            enrollment["id"],
        ) == 0
        assert await owner.fetchval(
            "SELECT count(*) FROM portal_installation_enrollment_events "
            "WHERE enrollment_id=$1 AND event_type='enrollment.terminated'",
            enrollment["id"],
        ) == 1
        enrolled_slugs = await owner.fetch(
            "SELECT enrolled.role,client.slug FROM service_clients client "
            "JOIN portal_installation_enrollment_clients enrolled "
            "ON enrolled.service_client_id=client.id "
            "WHERE enrolled.enrollment_id=$1",
            enrollment["id"],
        )
    finally:
        await owner.close()

    slug_by_role = {row["role"]: row["slug"] for row in enrolled_slugs}
    disable_args = argparse.Namespace(
        service_client_command="disable", client=slug_by_role["provisioner"]
    )
    assert await _run_service_client(disable_args, owner_url) == 1
    assert "portal-enrollment terminate" in capsys.readouterr().err
    grant_args = argparse.Namespace(
        delegation_grant_command="revoke",
        issuer=slug_by_role["read_broker"],
        binding_owner=slug_by_role["provisioner"],
        authority_class="read",
        reason="security_incident",
        json=False,
    )
    assert await _run_delegation_grant(grant_args, owner_url) == 1
    assert "portal-enrollment terminate" in capsys.readouterr().err

    rotation, _ = _rotation_body(body["installation_external_ref"])
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        enrollment_replay = await client.post(
            "/v1/service/portal-installation-enrollments",
            headers=_headers(pairing_secret),
            json=body,
        )
        denied_rotation = await client.post(
            "/v1/service/portal-installation-enrollments/rotate-credentials",
            headers=_headers(pairing_secret, "terminal-rotation"),
            json=rotation,
        )
        terminal_status = await client.get(
            "/v1/service/portal-installation-enrollments/"
            + body["installation_external_ref"],
            headers={"Authorization": f"Bearer {pairing_secret}"},
        )
    assert enrollment_replay.status_code == 409
    assert enrollment_replay.json()["detail"]["code"] == "ENROLLMENT_TERMINATED"
    assert denied_rotation.status_code == 409
    assert denied_rotation.json()["detail"]["code"] == "ENROLLMENT_TERMINATED"
    assert terminal_status.json() == {
        "status": "terminated",
        "credential_generation": 1,
        "provisioner": "not_ready",
        "read_delegation": "not_ready",
        "review_delegation": "not_ready",
    }


async def test_enrollment_evidence_refuses_downgrade_atomically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pairing_secret = _pairing_secret()
    _configure(monkeypatch, tmp_path, pairing_secret)
    body = _body()
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/service/portal-installation-enrollments",
            headers=_headers(pairing_secret),
            json=body,
        )
    assert response.status_code == 201

    downgrade_sql = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "downgrades"
        / "031_portal_installation_enrollment.sql"
    ).read_text(encoding="utf-8")
    owner = await _connect()
    try:
        with pytest.raises(Exception, match="requires empty portal enrollment"):
            await owner.execute(downgrade_sql)
        assert await owner.fetchval(
            "SELECT to_regclass('portal_installation_enrollments') IS NOT NULL"
        )
        assert await owner.fetchval(
            "SELECT count(*) FROM portal_installation_enrollments "
            "WHERE installation_external_ref=$1",
            uuid.UUID(body["installation_external_ref"]),
        ) == 1
    finally:
        await owner.close()


async def test_clean_030_to_031_downgrade_and_reupgrade(
    tmp_path: Path,
) -> None:
    import asyncpg

    from tests.test_service_provisioning_upgrade import (
        _restore_role_state,
        _role_state,
        _with_database,
    )

    owner_url = _owner_url()
    if owner_url is None:
        pytest.skip("requires an owner PostgreSQL URL")
    database = f"portal_enrollment_upgrade_{uuid.uuid4().hex}"
    try:
        admin = await asyncpg.connect(normalize_asyncpg_url(owner_url))
    except Exception:
        pytest.skip("requires a reachable owner PostgreSQL database")
    original_role = await _role_state(admin)
    created_database = False
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        created_database = True
        database_url = _with_database(owner_url, database)
        through_030 = tmp_path / "through-030-migrations"
        through_030.mkdir()
        for migration in discover_migrations():
            if migration.name <= "030_service_review_delegation.sql":
                (through_030 / migration.name).symlink_to(migration.resolve())
        assert await _run_init_db(database_url, migrations_dir=through_030) == 0

        connection = await asyncpg.connect(normalize_asyncpg_url(database_url))
        try:
            assert await connection.fetchval(
                "SELECT to_regclass('portal_installation_enrollments')"
            ) is None
        finally:
            await connection.close()

        assert await _run_init_db(database_url) == 0
        connection = await asyncpg.connect(normalize_asyncpg_url(database_url))
        try:
            assert await connection.fetchval(
                "SELECT to_regclass('portal_installation_enrollments') IS NOT NULL"
            )
            downgrade_sql = (
                Path(__file__).resolve().parents[1]
                / "migrations"
                / "downgrades"
                / "031_portal_installation_enrollment.sql"
            ).read_text(encoding="utf-8")
            await connection.execute(downgrade_sql)
            await connection.execute(
                "DELETE FROM schema_migrations "
                "WHERE filename='031_portal_installation_enrollment.sql'"
            )
            assert await connection.fetchval(
                "SELECT to_regclass('portal_installation_enrollments')"
            ) is None
        finally:
            await connection.close()

        assert await _run_init_db(database_url) == 0
        connection = await asyncpg.connect(normalize_asyncpg_url(database_url))
        try:
            assert await connection.fetchval(
                "SELECT to_regclass('portal_installation_enrollments') IS NOT NULL"
            )
            assert await connection.fetchval(
                "SELECT count(*) FROM schema_migrations "
                "WHERE filename='031_portal_installation_enrollment.sql'"
            ) == 1
        finally:
            await connection.close()
    finally:
        if created_database:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1",
                database,
            )
            await admin.execute(f'DROP DATABASE "{database}"')
        await _restore_role_state(admin, original_role)
        await admin.close()
