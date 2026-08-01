"""Authentication for the fixed Portal installation enrollment boundary."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import stat
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from engram.config import settings
from engram.service_auth import ServicePolicy

_TOKEN_RE = re.compile(r"^engpair_[A-Za-z0-9_-]{43}$")
_AUTHORIZATION_HEADER = b"authorization"


@dataclass(frozen=True)
class PortalEnrollmentIdentity:
    secret_digest: bytes


def parse_portal_enrollment_credential(token: str) -> str:
    """Validate the separate ``engpair_`` credential grammar."""
    if not token.isascii() or len(token) > 128 or _TOKEN_RE.fullmatch(token) is None:
        raise ValueError("invalid portal enrollment credential")
    return token


def digest_portal_enrollment_credential(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii")).digest()


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "ENROLLMENT_UNAUTHORIZED", "message": "Enrollment denied"},
        headers={"WWW-Authenticate": "Bearer"},
    )


def _read_configured_credential() -> str:
    configured_path = settings.portal_enrollment_secret_file
    if configured_path is None:
        raise ValueError("portal enrollment secret file is not configured")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(configured_path, flags)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or metadata.st_size > 256
        ):
            raise ValueError("portal enrollment secret file has unsafe metadata")
        raw = os.read(descriptor, 257)
    finally:
        os.close(descriptor)
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("portal enrollment secret file is not ASCII") from exc
    if text.endswith("\n"):
        text = text[:-1]
    if "\n" in text or "\r" in text:
        raise ValueError("portal enrollment secret file must contain one credential")
    return parse_portal_enrollment_credential(text)


def _request_credential(request: Request) -> str:
    values = [
        value
        for name, value in request.scope.get("headers", ())
        if name.lower() == _AUTHORIZATION_HEADER
    ]
    if len(values) != 1:
        raise ValueError("exactly one Authorization header is required")
    try:
        header = values[0].decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("Authorization header is not ASCII") from exc
    scheme, separator, token = header.partition(" ")
    if not separator or scheme.lower() != "bearer" or " " in token:
        raise ValueError("invalid Authorization header")
    return parse_portal_enrollment_credential(token)


class PortalEnrollmentGuard:
    """Authenticate one configured installation credential."""

    policy = ServicePolicy(
        permissions=(),
        description="Fixed Portal installation enrollment.",
        auth_class="portal-enrollment",
    )

    async def __call__(self, request: Request) -> PortalEnrollmentIdentity:
        if not settings.portal_enrollment_enabled:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "ENROLLMENT_DISABLED", "message": "Enrollment is disabled"},
            )
        if (
            settings.portal_enrollment_require_https
            and not settings.portal_development_setup
            and request.url.scheme != "https"
        ):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "ENROLLMENT_TRANSPORT_UNAVAILABLE",
                    "message": "Enrollment transport is unavailable",
                },
            )
        try:
            supplied = _request_credential(request)
            configured = _read_configured_credential()
        except (OSError, ValueError):
            raise _unauthorized() from None
        if not hmac.compare_digest(supplied, configured):
            raise _unauthorized()
        return PortalEnrollmentIdentity(
            secret_digest=digest_portal_enrollment_credential(configured)
        )


PORTAL_ENROLLMENT = PortalEnrollmentGuard()
