"""Pure delegated-credential and dispatch contracts."""

from __future__ import annotations

import re
import uuid

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

from engram.auth import Principal, get_current_principal
from engram.config import Settings, settings
from engram.delegation_auth import (
    canonical_delegation_request_digest,
    generate_delegation_token,
    parse_delegation_token,
    validate_delegation_external_ref,
    validate_delegation_ttl,
    verify_delegation_secret,
)


def test_delegation_token_grammar_digest_and_secret_safe_repr() -> None:
    material = generate_delegation_token()
    parsed = parse_delegation_token(material.token)

    assert material.token.startswith("engd_")
    assert len(parsed.key_id) == 22
    assert len(parsed.secret) == 43
    assert verify_delegation_secret(parsed.secret, material.secret_digest)
    assert parsed.secret not in repr(parsed)
    assert material.token not in repr(material)
    assert material.secret_digest not in repr(material)


def test_every_generated_delegation_credential_is_valid_bearer_material() -> None:
    bearer_token = re.compile(r"^[A-Za-z0-9\-._~+/]+={0,}$")
    for _ in range(100):
        material = generate_delegation_token()
        assert bearer_token.fullmatch(material.token)
        assert "*" not in material.token


@pytest.mark.parametrize(
    "token",
    [
        "engd_",
        "engd_" + "a" * 22 + "*" + "b" * 43,
        "engd_" + "é" * 22 + "*" + "b" * 43,
        "engd_" + "a" * 22 + "_" + "b" * 42,
        "eng_" + "a" * 22 + "_" + "b" * 43,
    ],
)
def test_delegation_token_parser_is_strict(token: str) -> None:
    with pytest.raises(ValueError, match="invalid delegated credential"):
        parse_delegation_token(token)


def test_delegation_canonical_request_digest_is_deterministic_and_sensitive() -> None:
    values = {
        "issuer_service_client_id": uuid.uuid4(),
        "grant_id": uuid.uuid4(),
        "binding_owner_service_client_id": uuid.uuid4(),
        "tenant_binding_id": uuid.uuid4(),
        "principal_binding_id": uuid.uuid4(),
        "delegation_external_ref": "delegation-ref",
        "ttl_seconds": 60,
    }
    first = canonical_delegation_request_digest(**values)
    second = canonical_delegation_request_digest(**values)
    assert first == second
    assert len(first) == 32
    assert first != canonical_delegation_request_digest(**{**values, "ttl_seconds": 61})


@pytest.mark.parametrize("value", ["", "contains space", "é", "x" * 256])
def test_delegation_external_refs_reject_non_visible_or_unbounded_values(value: str) -> None:
    with pytest.raises(ValueError, match="external references"):
        validate_delegation_external_ref(value)


def test_delegation_ttl_is_bounded_by_configuration() -> None:
    assert validate_delegation_ttl(30, 120) == 30
    assert validate_delegation_ttl(120, 120) == 120
    with pytest.raises(ValueError, match="between 30 and 120"):
        validate_delegation_ttl(121, 120)


@pytest.mark.asyncio
async def test_malformed_delegated_token_never_enters_legacy_fallback(monkeypatch) -> None:
    async def forbidden_legacy(_token: str) -> Principal | None:
        raise AssertionError("delegated material entered legacy fallback")

    monkeypatch.setattr("engram.auth._resolve_legacy_key", forbidden_legacy)
    settings.auth_enabled = True
    settings.delegation_enabled = True
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/whoami",
            "headers": [],
            "query_string": b"",
            "server": ("test", 80),
            "scheme": "http",
        }
    )
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials="engd_malformed",
    )
    with pytest.raises(HTTPException) as exc:
        await get_current_principal(request, credentials)
    assert exc.value.status_code == 401


def test_delegation_settings_validate_ttl_and_umbrella() -> None:
    Settings(
        service_provisioning_enabled=True,
        provisioner_database_url="postgresql+asyncpg://example/engram",
        delegation_enabled=True,
        delegation_default_ttl_seconds=30,
        delegation_max_ttl_seconds=30,
    )
    with pytest.raises(ValueError, match="service_provisioning_enabled"):
        Settings(delegation_enabled=True, service_provisioning_enabled=False)
    with pytest.raises(ValueError, match="delegation_max_ttl_seconds"):
        Settings(delegation_max_ttl_seconds=301)
