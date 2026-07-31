"""Real HTTP and PostgreSQL certification for Portal installation enrollment."""
# ruff: noqa: E501

from __future__ import annotations

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
    raw = generate_service_credential()
    parsed = parse_service_credential(raw)
    return {"key_id": parsed.key_id, "secret_digest": digest_service_secret(parsed.secret)}


def _body(installation: uuid.UUID | None = None) -> dict[str, Any]:
    return {
        "installation_external_ref": str(installation or uuid.uuid4()),
        "provisioner": _credential(),
        "read_broker": _credential(),
        "review_broker": _credential(),
    }


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
