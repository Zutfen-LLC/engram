"""Pure contract tests for the separate service-credential authority domain."""

from __future__ import annotations

import pytest

from engram.service_auth import (
    canonicalize_service_permissions,
    digest_service_secret,
    generate_service_credential,
    parse_service_credential,
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
