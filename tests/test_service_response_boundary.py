"""HTTP-level contract tests for the service-client response boundary."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from engram.api.app import create_app
from engram.service_auth import ServiceClientIdentity, get_current_service_client

SERVICE_PATH = "/v1/service/provisioning/tenant-human"


def _assert_service_headers(response, expected_request_id: str | None = None) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"
    request_id = response.headers["x-request-id"]
    if expected_request_id is not None:
        assert request_id == expected_request_id
    else:
        assert str(uuid.UUID(request_id)) == request_id


async def test_service_auth_failure_has_complete_response_contract(monkeypatch) -> None:
    from engram.config import settings

    monkeypatch.setattr(settings, "service_provisioning_enabled", True)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            SERVICE_PATH,
            headers={"Idempotency-Key": "request-1", "X-Request-ID": "caller-request-1"},
            json={},
        )

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "SERVICE_UNAUTHORIZED"
    assert response.headers["www-authenticate"] == "Bearer"
    _assert_service_headers(response, "caller-request-1")


async def test_service_validation_and_invalid_request_id_are_sanitized(monkeypatch) -> None:
    from engram.config import settings

    monkeypatch.setattr(settings, "service_provisioning_enabled", True)
    app = create_app()
    identity = ServiceClientIdentity(
        uuid.uuid4(), uuid.uuid4(), ("tenant.provision", "principal.provision")
    )

    async def service_identity() -> ServiceClientIdentity:
        return identity

    app.dependency_overrides[get_current_service_client] = service_identity
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            SERVICE_PATH,
            headers={"Idempotency-Key": "request-2", "X-Request-ID": "x" * 129},
            json={"tenant": {"external_ref": "tenant-1"}},
        )

    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "INVALID_REQUEST", "message": "Invalid request"}}
    _assert_service_headers(response)


async def test_service_route_sanitizes_unexpected_failures(monkeypatch) -> None:
    from engram.api.routes import service_provisioning
    from engram.config import settings

    monkeypatch.setattr(settings, "service_provisioning_enabled", True)
    app = create_app()
    identity = ServiceClientIdentity(
        uuid.uuid4(), uuid.uuid4(), ("tenant.provision", "principal.provision")
    )

    async def service_identity() -> ServiceClientIdentity:
        return identity

    def unavailable_factory():
        raise RuntimeError("postgresql://secret@database/internal")

    app.dependency_overrides[get_current_service_client] = service_identity
    monkeypatch.setattr(
        service_provisioning, "require_provisioner_session_factory", unavailable_factory
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            SERVICE_PATH,
            headers={"Idempotency-Key": "request-3"},
            json={
                "tenant": {"external_ref": "tenant-3", "name": "Tenant 3", "slug": "tenant-3"},
                "human_principal": {"external_ref": "human-3", "name": "Human 3"},
            },
        )

    assert response.status_code == 503
    assert response.json() == {
        "detail": {"code": "SERVICE_UNAVAILABLE", "message": "Provisioning is unavailable"}
    }
    assert "secret" not in response.text
    _assert_service_headers(response)


async def test_ordinary_routes_are_not_changed_by_service_boundary() -> None:
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert "cache-control" not in response.headers
    assert "x-request-id" not in response.headers
