"""Pure tests for the separate Portal enrollment credential boundary."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from starlette.requests import Request

from engram.portal_enrollment_auth import (
    PortalEnrollmentGuard,
    digest_portal_enrollment_credential,
    parse_portal_enrollment_credential,
)


def _request(*headers: tuple[bytes, bytes], scheme: str = "https") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/service/portal-installation-enrollments",
            "headers": list(headers),
            "scheme": scheme,
            "server": ("test", 443),
            "client": ("127.0.0.1", 1),
            "query_string": b"",
        }
    )


def _secret_file(path: Path, credential: str) -> None:
    path.write_text(credential + "\n", encoding="ascii")
    path.chmod(0o600)


def test_portal_enrollment_credential_grammar_is_separate() -> None:
    token = "engpair_" + "A" * 43
    assert parse_portal_enrollment_credential(token) == token
    assert len(digest_portal_enrollment_credential(token)) == 32
    for invalid in (
        "eng_" + "A" * 43,
        "engsvc_" + "A" * 43,
        "engd_" + "A" * 43,
        "engdr_" + "A" * 43,
        token + "=",
        "engpair_" + "é" * 43,
    ):
        with pytest.raises(ValueError):
            parse_portal_enrollment_credential(invalid)


async def test_guard_reads_exact_mode_0600_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from engram.config import settings

    credential = "engpair_" + "B" * 43
    secret_path = tmp_path / "pairing-secret"
    _secret_file(secret_path, credential)
    monkeypatch.setattr(settings, "portal_enrollment_enabled", True)
    monkeypatch.setattr(settings, "portal_enrollment_require_https", True)
    monkeypatch.setattr(settings, "portal_enrollment_secret_file", str(secret_path))

    identity = await PortalEnrollmentGuard()(
        _request((b"authorization", f"Bearer {credential}".encode("ascii")))
    )
    assert identity.secret_digest == digest_portal_enrollment_credential(credential)


@pytest.mark.parametrize("mode", [0o400, 0o640, 0o644])
async def test_guard_rejects_any_mode_other_than_0600(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    from engram.config import settings

    credential = "engpair_" + "C" * 43
    secret_path = tmp_path / "pairing-secret"
    _secret_file(secret_path, credential)
    secret_path.chmod(mode)
    monkeypatch.setattr(settings, "portal_enrollment_enabled", True)
    monkeypatch.setattr(settings, "portal_enrollment_require_https", False)
    monkeypatch.setattr(settings, "portal_enrollment_secret_file", str(secret_path))

    with pytest.raises(Exception) as raised:
        await PortalEnrollmentGuard()(
            _request(
                (b"authorization", f"Bearer {credential}".encode("ascii")), scheme="http"
            )
        )
    assert getattr(raised.value, "status_code", None) == 401


async def test_guard_rejects_duplicate_authorization_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from engram.config import settings

    credential = "engpair_" + "D" * 43
    secret_path = tmp_path / "pairing-secret"
    _secret_file(secret_path, credential)
    monkeypatch.setattr(settings, "portal_enrollment_enabled", True)
    monkeypatch.setattr(settings, "portal_enrollment_require_https", False)
    monkeypatch.setattr(settings, "portal_enrollment_secret_file", str(secret_path))
    header = f"Bearer {credential}".encode("ascii")

    with pytest.raises(Exception) as raised:
        await PortalEnrollmentGuard()(
            _request((b"authorization", header), (b"authorization", header), scheme="http")
        )
    assert getattr(raised.value, "status_code", None) == 401


async def test_guard_rejects_symlink_secret_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from engram.config import settings

    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("platform does not support no-follow file opens")
    credential = "engpair_" + "E" * 43
    target = tmp_path / "target"
    link = tmp_path / "link"
    _secret_file(target, credential)
    link.symlink_to(target)
    monkeypatch.setattr(settings, "portal_enrollment_enabled", True)
    monkeypatch.setattr(settings, "portal_enrollment_require_https", False)
    monkeypatch.setattr(settings, "portal_enrollment_secret_file", str(link))

    with pytest.raises(Exception) as raised:
        await PortalEnrollmentGuard()(
            _request(
                (b"authorization", f"Bearer {credential}".encode("ascii")), scheme="http"
            )
        )
    assert getattr(raised.value, "status_code", None) == 401
