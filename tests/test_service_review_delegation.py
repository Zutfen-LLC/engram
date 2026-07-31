"""Pure contracts for purpose-bound delegated review."""

from __future__ import annotations

import json
import re
import uuid

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from starlette.requests import Request

from engram.api.app import app
from engram.api.routes.service_review_delegation import ReviewDelegationIssueRequest
from engram.auth import Principal, get_current_principal
from engram.config import Settings, settings
from engram.delegation_auth import (
    canonical_review_queue_purpose,
    canonical_review_transition_purpose,
    generate_review_delegation_token,
    parse_delegation_token,
    parse_review_delegation_token,
    review_purpose_from_request,
    verify_delegation_secret,
)


def _request(
    *,
    method: str,
    raw_path: bytes,
    query: bytes = b"",
    body: bytes = b"",
    content_type: bytes | None = None,
) -> Request:
    sent = False

    async def receive():  # type: ignore[no-untyped-def]
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    headers = [] if content_type is None else [(b"content-type", content_type)]
    return Request(
        {
            "type": "http",
            "method": method,
            "scheme": "http",
            "server": ("test", 80),
            "client": ("client", 123),
            "root_path": "",
            "path": raw_path.decode("ascii"),
            "raw_path": raw_path,
            "query_string": query,
            "headers": headers,
        },
        receive,
    )


def test_review_token_grammar_is_strict_and_separate() -> None:
    material = generate_review_delegation_token()
    parsed = parse_review_delegation_token(material.token)

    assert material.token.startswith("engdr_")
    assert len(parsed.key_id) == 22
    assert len(parsed.secret) == 43
    assert verify_delegation_secret(parsed.secret, material.secret_digest)
    assert parsed.secret not in repr(parsed)
    assert material.token not in repr(material)
    with pytest.raises(ValueError, match="invalid delegated credential"):
        parse_delegation_token(material.token)


def test_every_generated_review_credential_is_valid_bearer_material() -> None:
    bearer_token = re.compile(r"^[A-Za-z0-9\-._~+/]+={0,}$")
    for _ in range(100):
        assert bearer_token.fullmatch(generate_review_delegation_token().token)


@pytest.mark.parametrize(
    "token",
    [
        "engdr_",
        "engdr_" + "a" * 22 + "*" + "b" * 43,
        "engdr_" + "a" * 22 + "_" + "b" * 42,
        "engd_" + "a" * 22 + "_" + "b" * 43,
        "eng_" + "a" * 22 + "_" + "b" * 43,
        "engsvc_" + "a" * 22 + "_" + "b" * 43,
    ],
    ids=[
        "empty-review-token",
        "invalid-review-token-character",
        "short-review-token-secret",
        "read-delegation-domain",
        "api-key-domain",
        "service-credential-domain",
    ],
)
def test_review_token_parser_rejects_other_domains_and_malformed_values(token: str) -> None:
    with pytest.raises(ValueError, match="invalid delegated review credential"):
        parse_review_delegation_token(token)


def test_review_purpose_digests_are_deterministic_and_sensitive() -> None:
    item_id = str(uuid.uuid4())
    queue = canonical_review_queue_purpose()
    first = canonical_review_transition_purpose(
        item_id=item_id,
        review_status="active",
        reason=None,
    )
    omitted_equivalent = canonical_review_transition_purpose(
        item_id=item_id,
        review_status="active",
        reason=None,
    )
    rejected = canonical_review_transition_purpose(
        item_id=item_id,
        review_status="rejected",
        reason=None,
    )
    reason = canonical_review_transition_purpose(
        item_id=item_id,
        review_status="active",
        reason="exact reason ",
    )

    assert len(queue.digest) == 32
    assert first.digest == omitted_equivalent.digest
    assert first.digest != rejected.digest
    assert first.digest != reason.digest
    assert first.digest != canonical_review_transition_purpose(
        item_id=str(uuid.uuid4()),
        review_status="active",
        reason=None,
    ).digest


@pytest.mark.parametrize(
    "item_id",
    [
        uuid.uuid4().hex,
        str(uuid.uuid4()).upper(),
        "not-a-uuid",
    ],
)
def test_transition_purpose_requires_canonical_item_id(item_id: str) -> None:
    with pytest.raises(ValueError, match="canonical"):
        canonical_review_transition_purpose(
            item_id=item_id,
            review_status="active",
            reason=None,
        )


@pytest.mark.parametrize("reason", ["", "x" * 501, "contains\x00nul"])
def test_transition_purpose_rejects_invalid_reason(reason: str) -> None:
    with pytest.raises(ValueError, match="reason"):
        canonical_review_transition_purpose(
            item_id=str(uuid.uuid4()),
            review_status="active",
            reason=reason,
        )


async def test_actual_queue_and_transition_requests_reconstruct_purpose() -> None:
    queue = await review_purpose_from_request(
        _request(method="GET", raw_path=b"/v1/review/queue")
    )
    item_id = str(uuid.uuid4())
    body = json.dumps(
        {"review_status": "active", "reason": None},
        separators=(",", ":"),
    ).encode()
    transition = await review_purpose_from_request(
        _request(
            method="POST",
            raw_path=f"/v1/items/{item_id}/review".encode(),
            body=body,
            content_type=b"application/json; charset=utf-8",
        )
    )

    assert queue == canonical_review_queue_purpose()
    assert transition == canonical_review_transition_purpose(
        item_id=item_id,
        review_status="active",
        reason=None,
    )


@pytest.mark.parametrize(
    ("query", "body", "content_type"),
    [
        (b"limit=50", b"", None),
        (b"", b'{"review_status":"active","review_status":"rejected"}', b"application/json"),
        (b"", b'{"review_status":"active","review_notes":"forbidden"}', b"application/json"),
        (b"", b'["active"]', b"application/json"),
        (b"", b'{"review_status":"active"}', b"text/json"),
        (b"", b"x" * 4097, b"application/json"),
    ],
)
async def test_actual_request_rejects_unbound_shapes(
    query: bytes,
    body: bytes,
    content_type: bytes | None,
) -> None:
    item_id = str(uuid.uuid4())
    with pytest.raises(ValueError):
        await review_purpose_from_request(
            _request(
                method="POST",
                raw_path=f"/v1/items/{item_id}/review".encode(),
                query=query,
                body=body,
                content_type=content_type,
            )
        )


def test_issue_request_forbids_caller_selected_authority_material() -> None:
    valid = {
        "binding_owner_service_client_slug": "binding-owner",
        "tenant_external_ref": "tenant-ref",
        "principal_external_ref": "principal-ref",
        "delegation_external_ref": "delegation-ref",
        "purpose": {"kind": "review.queue"},
    }
    ReviewDelegationIssueRequest.model_validate(valid)
    for field in ("scopes", "audience", "method", "path", "digest"):
        with pytest.raises(ValidationError):
            ReviewDelegationIssueRequest.model_validate({**valid, field: "forbidden"})
    for invalid_ttl in ("30", 30.0, True):
        with pytest.raises(ValidationError):
            ReviewDelegationIssueRequest.model_validate(
                {**valid, "ttl_seconds": invalid_ttl}
            )


def test_review_delegation_settings_are_default_off_and_bounded() -> None:
    configured = Settings(
        service_provisioning_enabled=True,
        provisioner_database_url="postgresql+asyncpg://example/engram",
        review_delegation_enabled=True,
        review_delegation_default_ttl_seconds=30,
        review_delegation_max_ttl_seconds=60,
    )
    assert configured.review_delegation_enabled
    with pytest.raises(ValueError, match="service_provisioning_enabled"):
        Settings(
            service_provisioning_enabled=False,
            review_delegation_enabled=True,
        )
    with pytest.raises(ValueError, match="review_delegation_max_ttl_seconds"):
        Settings(review_delegation_max_ttl_seconds=61)


async def test_malformed_review_token_never_enters_ordinary_or_read_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden_ordinary(_token: str) -> Principal | None:
        raise AssertionError("review material entered ordinary-key fallback")

    async def forbidden_read(_token: str, *, request_id: str | None) -> Principal:
        raise AssertionError(f"review material entered read fallback: {request_id}")

    monkeypatch.setattr("engram.auth._resolve_legacy_key", forbidden_ordinary)
    monkeypatch.setattr(
        "engram.delegation_auth.resolve_delegated_principal",
        forbidden_read,
    )
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "review_delegation_enabled", True)
    request = _request(method="GET", raw_path=b"/v1/review/queue")
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials="engdr_malformed",
    )
    with pytest.raises(HTTPException) as exc:
        await get_current_principal(request, credentials)
    assert exc.value.status_code == 401


async def test_ordinary_invalid_json_behavior_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "auth_enabled", False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/v1/items/{uuid.uuid4()}/review",
            headers={"Content-Type": "application/json"},
            content=b'{"review_status":',
        )
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
