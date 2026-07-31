"""Authentication for narrowly-authorized external service clients.

Service credentials are intentionally a separate grammar and authority domain
from tenant principals and ``eng_`` API keys.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import string
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from engram.config import settings

ServicePermission = Literal[
    "tenant.provision",
    "principal.provision",
    "workspace.provision",
    "agent.provision",
    "api_key.provision",
    "delegation.issue",
    "delegation.review.issue",
]
VALID_SERVICE_PERMISSIONS: frozenset[str] = frozenset(
    {
        "tenant.provision",
        "principal.provision",
        "workspace.provision",
        "agent.provision",
        "api_key.provision",
        "delegation.issue",
        "delegation.review.issue",
    }
)
CANONICAL_SERVICE_PERMISSION_ORDER: tuple[ServicePermission, ...] = (
    "tenant.provision",
    "principal.provision",
    "workspace.provision",
    "agent.provision",
    "api_key.provision",
    "delegation.issue",
    "delegation.review.issue",
)
_PREFIX = "engsvc_"
_KEY_ID_LENGTH = 22
_SECRET_LENGTH = 43  # urlsafe base64 encoding of 32 random bytes without padding
_BASE62 = string.digits + string.ascii_lowercase + string.ascii_uppercase
_TOKEN_RE = re.compile(r"^engsvc_([0-9A-Za-z]{22})_([A-Za-z0-9_-]{43})$")
_SERVICE_CLIENT_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,99}$")
_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class ParsedServiceCredential:
    key_id: str
    secret: str


@dataclass(frozen=True)
class ServiceClientIdentity:
    id: UUID
    credential_id: UUID
    permissions: tuple[str, ...]

    def has_permission(self, permission: str) -> bool:
        return permission in self.permissions


@dataclass(frozen=True)
class ServicePolicy:
    permissions: tuple[str, ...]
    # These fields deliberately mirror ScopePolicy's surface so the complete
    # route-policy inventory can continue to inspect every authentication class
    # uniformly. They are always empty: service permissions are not scopes.
    all_of: tuple[str, ...] = ()
    any_of: tuple[str, ...] = ()
    exempt: bool = False
    description: str | None = None
    auth_class: str = "service-client"


def _random_base62(length: int) -> str:
    chars: list[str] = []
    limit = (256 // len(_BASE62)) * len(_BASE62)
    while len(chars) < length:
        value = secrets.token_bytes(1)[0]
        if value < limit:
            chars.append(_BASE62[value % len(_BASE62)])
    return "".join(chars)


def generate_service_credential() -> str:
    return f"{_PREFIX}{_random_base62(_KEY_ID_LENGTH)}_{secrets.token_urlsafe(32)}"


def parse_service_credential(token: str) -> ParsedServiceCredential:
    # Explicit ASCII check avoids surprising Unicode regex behavior and makes
    # malformed credentials indistinguishable without throwing implementation
    # exceptions out of the auth boundary.
    if not token.isascii() or len(token) > 256:
        raise ValueError("invalid service credential")
    match = _TOKEN_RE.fullmatch(token)
    if match is None:
        raise ValueError("invalid service credential")
    return ParsedServiceCredential(key_id=match.group(1), secret=match.group(2))


def digest_service_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def verify_service_secret(secret: str, expected_digest: str) -> bool:
    return hmac.compare_digest(digest_service_secret(secret), expected_digest)


def canonicalize_service_permissions(permissions: list[str]) -> list[str]:
    values = set(permissions)
    unknown = sorted(values - VALID_SERVICE_PERMISSIONS)
    if unknown:
        raise ValueError(f"unknown service permission(s): {', '.join(unknown)}")
    if len(values) != len(permissions):
        raise ValueError("duplicate service permissions are not allowed")
    if not values:
        raise ValueError("at least one service permission is required")
    return [permission for permission in CANONICAL_SERVICE_PERMISSION_ORDER if permission in values]


def validate_service_client_display_name(value: str) -> str:
    """Validate the immutable operator-facing service-client name.

    Names are stored verbatim, so leading/trailing whitespace is rejected
    rather than silently normalized into an ambiguous database identity.
    """
    if not value or value != value.strip() or len(value) > 255:
        raise ValueError(
            "service client display name must be 1 through 255 non-whitespace characters"
        )
    return value


def validate_service_client_slug(value: str) -> str:
    """Validate the database-backed service-client identifier before connecting."""
    if _SERVICE_CLIENT_SLUG_RE.fullmatch(value) is None:
        raise ValueError("invalid service client slug")
    return value


def validate_service_credential_label(value: str | None) -> str | None:
    """Keep rotate-key labels within their persisted VARCHAR boundary."""
    if value is not None and len(value) > 255:
        raise ValueError("service credential label is too long")
    return value


def validate_service_credential_expiry(value: str | None) -> str | None:
    """Reject malformed ISO-8601 expiry values before any credential is minted."""
    if value is None:
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid service credential expiry") from exc
    return value


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "SERVICE_UNAUTHORIZED", "message": "Service authentication failed"},
        headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def service_unauthorized_exception() -> HTTPException:
    """Generic service auth failure reused by preliminary and locked checks."""
    return _unauthorized()


async def get_current_service_client(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),  # noqa: B008
) -> ServiceClientIdentity:
    """Resolve an active service credential without using principal auth."""
    if not settings.service_provisioning_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "SERVICE_UNAVAILABLE", "message": "Provisioning is unavailable"},
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()
    try:
        parsed = parse_service_credential(credentials.credentials)
    except ValueError:
        raise _unauthorized() from None

    from engram.db import require_provisioner_session_factory

    try:
        async with require_provisioner_session_factory()() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT c.id AS credential_id, c.service_client_id, c.secret_digest, "
                        "c.status AS credential_status, c.expires_at, s.status AS client_status, "
                        "s.permissions FROM service_client_credentials c "
                        "JOIN service_clients s ON s.id = c.service_client_id "
                        "WHERE c.key_id = :key_id"
                    ),
                    {"key_id": parsed.key_id},
                )
            ).mappings().first()
    except HTTPException:
        raise
    except Exception:
        # Never include database/driver detail in an authentication response.
        raise _unauthorized() from None
    if (
        row is None
        or row["credential_status"] != "active"
        or row["client_status"] != "active"
        or (row["expires_at"] is not None and row["expires_at"] <= datetime.now(UTC))
        or not verify_service_secret(parsed.secret, row["secret_digest"])
    ):
        raise _unauthorized()
    raw_permissions = row["permissions"]
    permissions = tuple(raw_permissions if isinstance(raw_permissions, list) else [])
    return ServiceClientIdentity(
        id=UUID(str(row["service_client_id"])),
        credential_id=UUID(str(row["credential_id"])),
        permissions=permissions,
    )


async def lock_and_validate_service_authority(
    session: AsyncSession,
    identity: ServiceClientIdentity,
    required_permissions: tuple[str, ...],
) -> ServiceClientIdentity:
    """Revalidate the credential/client under row locks in the write transaction.

    The preliminary dependency intentionally remains a cheap generic reject.
    This second check is authoritative: revocation, disablement, expiry,
    reassignment, and permission changes that occur after preliminary auth
    serialize against this locked read before provisioning can mutate state.
    """
    row = (
        await session.execute(
            text(
                "SELECT c.id AS credential_id, c.service_client_id, c.status AS credential_status, "
                "c.expires_at, s.id AS client_id, s.status AS client_status, s.permissions "
                "FROM service_client_credentials c "
                "JOIN service_clients s ON s.id = c.service_client_id "
                # PostgreSQL requires UPDATE privilege for this lock. The
                # migration grants only an inert timestamp column on clients
                # and last_used_at on credentials; neither permits changing
                # service authority, but both permit this serialization point.
                "WHERE c.id = :credential_id FOR UPDATE OF c, s"
            ),
            {"credential_id": identity.credential_id},
        )
    ).mappings().first()
    if (
        row is None
        or UUID(str(row["credential_id"])) != identity.credential_id
        or UUID(str(row["service_client_id"])) != identity.id
        or UUID(str(row["client_id"])) != identity.id
        or row["credential_status"] != "active"
        or row["client_status"] != "active"
        or (row["expires_at"] is not None and row["expires_at"] <= datetime.now(UTC))
    ):
        raise service_unauthorized_exception()
    raw_permissions = row["permissions"]
    permissions = tuple(raw_permissions if isinstance(raw_permissions, list) else [])
    if not all(permission in permissions for permission in required_permissions):
        raise service_unauthorized_exception()
    return ServiceClientIdentity(
        id=identity.id,
        credential_id=identity.credential_id,
        permissions=permissions,
    )


class ServicePermissionGuard:
    """An introspectable service authorization dependency for route policy checks."""

    def __init__(self, *permissions: ServicePermission) -> None:
        canonicalize_service_permissions(list(permissions))
        if permissions == ("delegation.issue",):
            description = "External control-plane read delegation."
        elif permissions == ("delegation.review.issue",):
            description = "External control-plane purpose-bound review delegation."
        else:
            description = "External control-plane provisioning."
        self.policy = ServicePolicy(
            permissions=tuple(permissions), description=description
        )

    async def __call__(
        self,
        identity: ServiceClientIdentity = Depends(get_current_service_client),  # noqa: B008
    ) -> ServiceClientIdentity:
        if not all(identity.has_permission(permission) for permission in self.policy.permissions):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "SERVICE_FORBIDDEN", "message": "Service permission denied"},
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            )
        return identity


PROVISION_TENANT_HUMAN = ServicePermissionGuard("tenant.provision", "principal.provision")
PROVISION_WORKSPACE_AGENT = ServicePermissionGuard("workspace.provision", "agent.provision")
PROVISION_AGENT_API_KEY = ServicePermissionGuard("api_key.provision")
ISSUE_DELEGATION = ServicePermissionGuard("delegation.issue")
ISSUE_REVIEW_DELEGATION = ServicePermissionGuard("delegation.review.issue")
