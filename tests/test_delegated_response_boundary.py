"""Credential-class and HTTP contract tests for delegated response boundaries."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request as StarletteRequest

from engram.api.app import create_app
from engram.api.service_boundary import effective_request_id, is_delegated_request
from engram.auth import Principal, get_current_principal
from engram.config import settings
from engram.db import get_session
from engram.memory_context import resolve_memory_context, unrestricted_memory_context

_TENANT_ID = str(uuid.uuid4())
_PRINCIPAL_ID = str(uuid.uuid4())


def _request(*headers: tuple[bytes, bytes]) -> StarletteRequest:
    return StarletteRequest(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("client", 123),
            "root_path": "",
            "path": "/v1/items",
            "raw_path": b"/v1/items",
            "query_string": b"",
            "headers": list(headers),
        }
    )


@pytest.mark.parametrize(
    "authorization",
    [
        b"Bearer engd_malformed",
        b"bearer engd_malformed",
        b"BEARER engd_malformed",
    ],
)
def test_delegated_bearer_classification(authorization: bytes) -> None:
    request = _request((b"authorization", authorization))

    assert is_delegated_request(request)
    assert request.scope.get("state", {}) == {}


@pytest.mark.parametrize(
    "headers",
    [
        (),
        ((b"authorization", b"Bearer eng_ordinary"),),
        ((b"authorization", b"Bearer engsvc_service"),),
        ((b"authorization", b"Basic engd_malformed"),),
        ((b"x-unrelated", b"Bearer engd_malformed"),),
    ],
)
def test_delegated_classification_rejects_other_credential_domains(
    headers: tuple[tuple[bytes, bytes], ...],
) -> None:
    assert not is_delegated_request(_request(*headers))


def test_duplicate_authorization_classifies_any_delegated_bearer_attempt() -> None:
    request = _request(
        (b"authorization", b"Bearer eng_ordinary"),
        (b"authorization", b"Bearer engd_malformed"),
    )

    assert is_delegated_request(request)


@pytest.mark.parametrize(
    "candidate",
    [None, "", "contains whitespace", "\x1fcontrol", "x" * 129, "non-ascii-\N{SNOWMAN}"],
)
def test_invalid_request_ids_are_replaced_by_uuids(candidate: str | None) -> None:
    generated = effective_request_id(candidate)

    assert str(uuid.UUID(generated)) == generated
    assert generated != candidate


def test_valid_visible_ascii_request_id_is_preserved() -> None:
    assert effective_request_id("caller-request.123:/[]") == "caller-request.123:/[]"


class _Result:
    def mappings(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return []


class _Session:
    async def scalar(self, _statement: Any) -> str:
        return "user"

    async def execute(self, _statement: Any, _parameters: dict[str, Any] | None = None) -> _Result:
        return _Result()


def _principal(*, scopes: tuple[str, ...] = ("read",)) -> Principal:
    return Principal(
        tenant_id=_TENANT_ID,
        principal_id=_PRINCIPAL_ID,
        scopes=scopes,  # type: ignore[arg-type]
    )


def _assert_delegated_headers(response: Any, request_id: str | None = None) -> str:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"
    effective = response.headers["x-request-id"]
    if request_id is None:
        assert str(uuid.UUID(effective)) == effective
    else:
        assert effective == request_id
    return effective


async def _boundary_client(*, scopes: tuple[str, ...] = ("read",)) -> AsyncIterator[AsyncClient]:
    app = create_app()
    principal = _principal(scopes=scopes)
    session = _Session()

    async def override_principal() -> Principal:
        return principal

    async def override_session() -> AsyncIterator[_Session]:
        yield session

    async def override_context():  # type: ignore[no-untyped-def]
        return unrestricted_memory_context(principal)

    @app.get("/boundary-unhandled")
    async def boundary_unhandled() -> None:
        raise RuntimeError("private exception detail")

    app.dependency_overrides[get_current_principal] = override_principal
    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[resolve_memory_context] = override_context
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_delegated_successes_have_complete_response_contract() -> None:
    request_id = "delegated-success-correlation"
    async for client in _boundary_client():
        whoami = await client.get(
            "/whoami",
            headers={
                "Authorization": "Bearer engd_malformed",
                "X-Request-ID": request_id,
            },
        )
        items = await client.get(
            "/v1/items",
            headers={"Authorization": "bearer engd_malformed"},
        )

    assert whoami.status_code == 200
    _assert_delegated_headers(whoami, request_id)
    assert items.status_code == 200
    assert items.json() == {
        "items": [],
        "count": 0,
        "next_cursor": None,
        "cursor": None,
    }
    _assert_delegated_headers(items)


async def test_delegated_validation_scope_routing_and_method_failures_have_headers() -> None:
    async for client in _boundary_client():
        validation = await client.get(
            "/v1/items/not-a-uuid",
            headers={"Authorization": "Bearer engd_malformed"},
        )
        missing = await client.get(
            "/not-a-route",
            headers={"Authorization": "Bearer engd_malformed"},
        )
        method = await client.post(
            "/whoami",
            headers={"Authorization": "Bearer engd_malformed"},
        )
    async for client in _boundary_client(scopes=()):
        scope_denial = await client.get(
            "/v1/items",
            headers={"Authorization": "Bearer engd_malformed"},
        )

    assert validation.status_code == 422
    assert scope_denial.status_code == 403
    assert missing.status_code == 404
    assert method.status_code == 405
    for response in (validation, scope_denial, missing, method):
        _assert_delegated_headers(response)


async def test_malformed_and_duplicate_delegated_credentials_fail_with_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "delegation_enabled", True)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        malformed = await client.get(
            "/whoami",
            headers={"Authorization": "Bearer engd_malformed"},
        )
        duplicate = await client.get(
            "/whoami",
            headers=[
                ("Authorization", "Bearer eng_ordinary"),
                ("Authorization", "Bearer engd_malformed"),
            ],
        )

    assert malformed.status_code == 401
    assert duplicate.status_code == 401
    _assert_delegated_headers(malformed)
    _assert_delegated_headers(duplicate)


async def test_delegated_unhandled_failure_is_bounded_and_private() -> None:
    async for client in _boundary_client():
        response = await client.get(
            "/boundary-unhandled",
            headers={"Authorization": "Bearer engd_malformed"},
        )

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "code": "DELEGATED_REQUEST_UNAVAILABLE",
            "message": "Request unavailable",
        }
    }
    assert "private exception detail" not in response.text
    _assert_delegated_headers(response)


async def test_ordinary_api_key_health_and_readiness_responses_remain_unmodified() -> None:
    async for client in _boundary_client():
        ordinary = await client.get(
            "/v1/items",
            headers={"Authorization": "Bearer eng_ordinary"},
        )
        health = await client.get("/health")
        readiness = await client.get("/ready")

    assert ordinary.status_code == 200
    assert health.status_code == 200
    for response in (ordinary, health, readiness):
        assert "cache-control" not in response.headers
        assert "pragma" not in response.headers
        assert "referrer-policy" not in response.headers
        assert "x-request-id" not in response.headers
