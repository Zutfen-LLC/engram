"""Pure contract tests for the separate service-credential authority domain."""

from __future__ import annotations

import itertools

import pytest

from engram.service_auth import (
    CANONICAL_SERVICE_PERMISSION_ORDER,
    canonicalize_service_permissions,
    digest_service_secret,
    generate_service_credential,
    parse_service_credential,
    validate_service_client_display_name,
    verify_service_secret,
)


def test_service_credential_is_strict_and_distinct_from_api_keys() -> None:
    credential = generate_service_credential()
    parsed = parse_service_credential(credential)

    assert credential.startswith("engsvc_")
    assert len(parsed.key_id) == 22
    assert verify_service_secret(parsed.secret, digest_service_secret(parsed.secret))
    with pytest.raises(ValueError):
        parse_service_credential("eng_" + credential)
    with pytest.raises(ValueError):
        parse_service_credential(credential + "=")
    with pytest.raises(ValueError):
        parse_service_credential("engsvc_" + "é" * 22 + "_" + parsed.secret)


def test_service_permissions_are_canonical_and_do_not_accept_duplicates() -> None:
    assert canonicalize_service_permissions(
        ["principal.provision", "tenant.provision"]
    ) == ["tenant.provision", "principal.provision"]
    with pytest.raises(ValueError, match="duplicate"):
        canonicalize_service_permissions(["tenant.provision", "tenant.provision"])
    with pytest.raises(ValueError, match="unknown"):
        canonicalize_service_permissions(["read"])


def test_extended_service_permissions_are_canonical() -> None:
    assert canonicalize_service_permissions(
        [
            "api_key.provision",
            "agent.provision",
            "workspace.provision",
            "principal.provision",
            "tenant.provision",
        ]
    ) == [
        "tenant.provision",
        "principal.provision",
        "workspace.provision",
        "agent.provision",
        "api_key.provision",
    ]


def test_every_nonempty_service_permission_subset_is_valid() -> None:
    permissions = list(CANONICAL_SERVICE_PERMISSION_ORDER)
    for size in range(1, len(permissions) + 1):
        for subset in itertools.combinations(permissions, size):
            assert canonicalize_service_permissions(list(reversed(subset))) == list(subset)


@pytest.mark.parametrize("value", ["", "   ", " name", "name ", "x" * 256])
def test_service_client_display_name_is_nonempty_bounded_and_unambiguous(value: str) -> None:
    with pytest.raises(ValueError, match="display name"):
        validate_service_client_display_name(value)


def test_service_client_display_name_preserves_valid_value() -> None:
    assert validate_service_client_display_name("Control Plane") == "Control Plane"
