"""API key authentication and authorization for Engram.

When ``ENGRAM_AUTH_ENABLED=false`` (default, dev mode) auth is bypassed and
the default tenant/admin principal from the seed migration is used — preserving
the Phase 1A behavior. When enabled, a ``Bearer`` token is required; it is
looked up as a bcrypt-hashed key in ``api_keys`` and resolves to
``(tenant_id, principal_id, scopes)``.

This module imports only the session *factory* from ``engram.db`` (via a lazy
accessor) so that ``db.get_session`` can depend on the resolved principal
without creating an import cycle.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from engram.config import settings

Scope = Literal["read", "write", "admin", "export"]
VALID_SCOPES: frozenset[str] = frozenset({"read", "write", "admin", "export"})

_KEY_PREFIX = "eng_"
_KEY_RANDOM_BYTES = 32

# Paths that never require authentication.
_EXEMPT_PATHS: frozenset[str] = frozenset({"/health", "/ready"})

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    """The authenticated caller — tenant + principal + granted scopes."""

    tenant_id: str
    principal_id: str
    scopes: tuple[str, ...]


# --- Key generation / hashing ------------------------------------------------


def generate_api_key() -> str:
    """Return a new plaintext API key (``eng_<random>``)."""
    return _KEY_PREFIX + secrets.token_urlsafe(_KEY_RANDOM_BYTES)


def hash_api_key(plaintext: str) -> str:
    """Bcrypt-hash a plaintext key for storage. Returns a ``str`` hash."""
    return bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt()).decode()


def verify_api_key(plaintext: str, key_hash: str) -> bool:
    """Constant-time check of a plaintext key against a stored bcrypt hash."""
    try:
        return bcrypt.checkpw(plaintext.encode(), key_hash.encode())
    except (ValueError, TypeError):
        return False


# --- Session factory accessor (breaks import cycle with engram.db) -----------


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Lazy accessor — avoids a module-level import cycle with engram.db."""
    from engram.db import async_session_factory

    return async_session_factory


# --- Principal resolution ----------------------------------------------------


async def _resolve_default_principal(session: AsyncSession) -> Principal:
    """Look up the seed default tenant/admin (auth-disabled path)."""
    row = (
        await session.execute(
            text(
                "SELECT CAST(t.id AS TEXT) AS tenant_id, "
                "       CAST(p.id AS TEXT) AS principal_id "
                "FROM tenants t "
                "JOIN principals p "
                "  ON p.tenant_id = t.id AND p.name = :principal "
                "WHERE t.slug = :slug"
            ),
            {"slug": "default", "principal": "admin"},
        )
    ).one()
    return Principal(
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        scopes=("read", "write", "admin", "export"),
    )


async def get_current_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),  # noqa: B008
) -> Principal:
    """FastAPI dependency — resolve the caller from the Authorization header.

    - Health endpoints (``/health``, ``/ready``) are always exempt.
    - Auth disabled → default tenant/admin with all scopes.
    - Auth enabled  → Bearer token must match a non-revoked bcrypt-hashed key.

    Opens its own short-lived DB session for the key lookup so it does not
    create a dependency-cycle with ``db.get_session`` (which consumes the
    resolved principal to set RLS context).
    """
    # Health endpoints are always exempt — resolve default principal.
    if request.url.path in _EXEMPT_PATHS:
        async with _get_session_factory()() as session:
            return await _resolve_default_principal(session)

    if not settings.auth_enabled:
        async with _get_session_factory()() as session:
            return await _resolve_default_principal(session)

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    if not token.startswith(_KEY_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key format",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # API keys are not queryable by hash (bcrypt salts). We load all
    # non-revoked keys and check each. Raw SQL (not ORM) so SQLite-based
    # tests work without the Postgres ARRAY type.
    async with _get_session_factory()() as session:
        result = await session.execute(
            text(
                "SELECT CAST(tenant_id AS TEXT) AS tenant_id, "
                "       CAST(principal_id AS TEXT) AS principal_id, "
                "       key_hash AS key_hash, "
                "       scopes AS scopes "
                "FROM api_keys WHERE revoked_at IS NULL"
            )
        )
        for row in result:
            if verify_api_key(token, row.key_hash):
                raw_scopes = row.scopes
                # Postgres returns TEXT[] (list); SQLite test returns TEXT (str).
                if isinstance(raw_scopes, str):
                    scope_list = [s for s in raw_scopes.split(",") if s]
                else:
                    scope_list = list(raw_scopes)
                return Principal(
                    tenant_id=row.tenant_id,
                    principal_id=row.principal_id or "",
                    scopes=tuple(scope_list),
                )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or revoked API key",
        headers={"WWW-Authenticate": "Bearer"},
    )


# --- Scope / membership enforcement -----------------------------------------


def require_scopes(*required: str) -> Callable[..., Awaitable[Principal]]:
    """Dependency factory — enforces that the caller holds every scope.

    Usage::

        @router.post(..., dependencies=[Depends(require_scopes("admin"))])
    """

    missing = set(required) - VALID_SCOPES
    if missing:
        raise ValueError(f"Unknown scope(s): {missing}")

    async def _check(pr: Principal = Depends(get_current_principal)) -> Principal:  # noqa: B008
        granted = set(pr.scopes)
        if not all(s in granted for s in required):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires scope(s): {', '.join(required)}",
            )
        return pr

    return _check


async def check_workspace_membership(
    session: AsyncSession,
    *,
    principal_id: str,
    workspace_id: str,
) -> bool:
    """Return True iff ``principal_id`` is a member of ``workspace_id``."""
    result = await session.execute(
        text(
            "SELECT 1 FROM workspace_members "
            "WHERE principal_id = :pid AND workspace_id = :wid"
        ),
        {"pid": principal_id, "wid": workspace_id},
    )
    return result.first() is not None
