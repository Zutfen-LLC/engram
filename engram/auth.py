"""API key authentication and authorization for Engram.

There are two API-key formats:

* **New format** (created from ENG-AUD-003 onward): ``eng_<key_id>_<secret>``.
  The ``key_id`` is looked up by a unique partial index, so verification is
  O(1): one indexed query by ``key_id`` plus a constant-time digest check. No
  bcrypt, no full-table scan. The high-entropy ``secret`` is verified against a
  fast deterministic digest (sha256) — appropriate because API keys are random
  secrets, NOT human passwords. A short-TTL in-process cache avoids the lookup
  on repeated requests with the same key.

* **Legacy format** (``eng_<random>``, bcrypt-hashed): kept working through a
  transitional fallback. Because bcrypt salts its hashes, a legacy key cannot be
  looked up by value, so verification scans the legacy rows and bcrypt-checks
  each one. This path is O(n·bcrypt) and exists ONLY for keys created before
  ENG-AUD-003; it should be removed in a future cleanup once legacy keys are
  rotated out.

When ``ENGRAM_AUTH_ENABLED=false`` (default, dev mode) auth is bypassed and
the default tenant/admin principal from the seed migration is used — preserving
the Phase 1A behavior. When enabled, a ``Bearer`` token is required and resolves
to ``(tenant_id, principal_id, scopes)``.

This module imports only the session *factory* from ``engram.db`` (via a lazy
accessor) so that ``db.get_session`` can depend on the resolved principal
without creating an import cycle.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import string
import time
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

# New-format keys: ``eng_<key_id>_<secret>``. key_id is base62 (no ``_`` or ``-``
# so it is unambiguous to parse) and ~16 random bytes of entropy; the secret is
# 32 url-safe random bytes (256 bits), comparable to the legacy key material.
_KEY_ID_LENGTH = 22  # base62 chars (~130 bits ≈ 16 random bytes)
_SECRET_RANDOM_BYTES = 32

# Deterministic digest used for new-format key secrets.
DIGEST_ALGORITHM = "sha256"

# Paths that never require authentication.
_EXEMPT_PATHS: frozenset[str] = frozenset({"/health", "/ready"})

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    """The authenticated caller — tenant + principal + granted scopes."""

    tenant_id: str
    principal_id: str
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class ParsedApiKey:
    """Result of parsing a presented API key token.

    ``is_legacy`` is True for old ``eng_<random>`` tokens (no internal
    underscore); a new-format ``eng_<key_id>_<secret>`` token carries its
    ``key_id``. Note a legacy token whose random segment happens to contain an
    underscore parses as new-format; the resolver handles that by falling back
    to the legacy bcrypt scan when a key_id lookup misses (see
    ``get_current_principal``).
    """

    key_id: str | None
    secret: str
    is_legacy: bool


# --- Key generation / hashing -----------------------------------------------


# base62 alphabet (digits + lower + upper): compact, URL-safe, and free of ``_``
# and ``-`` so a key_id never contains a separator that could confuse parsing.
_BASE62_ALPHABET = string.digits + string.ascii_lowercase + string.ascii_uppercase
# Largest multiple of the alphabet length below 256, for unbiased rejection
# sampling when mapping random bytes onto the base62 alphabet.
_BASE62_ACCEPT_LIMIT = (256 // len(_BASE62_ALPHABET)) * len(_BASE62_ALPHABET)


def _random_base62(length: int) -> str:
    """Return ``length`` unbiased base62 characters."""
    chars: list[str] = []
    while len(chars) < length:
        byte = secrets.token_bytes(1)[0]
        if byte < _BASE62_ACCEPT_LIMIT:
            chars.append(_BASE62_ALPHABET[byte % len(_BASE62_ALPHABET)])
    return "".join(chars)


def generate_api_key() -> str:
    """Return a new plaintext API key (``eng_<key_id>_<secret>``).

    The ``key_id`` (base62, no separators) is the indexed lookup key; the
    ``secret`` is the high-entropy random material verified against the stored
    digest. Only the digest is persisted — never the plaintext or the secret.
    """
    key_id = _random_base62(_KEY_ID_LENGTH)
    secret = secrets.token_urlsafe(_SECRET_RANDOM_BYTES)
    return f"{_KEY_PREFIX}{key_id}_{secret}"


def parse_api_key(token: str) -> ParsedApiKey:
    """Parse a presented token into a :class:`ParsedApiKey`.

    Raises ``ValueError`` for tokens that do not start with ``eng_`` or have no
    key material. A token with an internal underscore parses as new-format
    (``key_id`` = the segment before the first underscore, ``secret`` = the
    remainder); otherwise it is legacy.
    """
    if not token.startswith(_KEY_PREFIX):
        raise ValueError("API key must start with 'eng_'")
    rest = token[len(_KEY_PREFIX):]
    if not rest:
        raise ValueError("API key has no key material")
    if "_" in rest:
        key_id, secret = rest.split("_", 1)
        if not key_id or not secret:
            raise ValueError("malformed API key")
        return ParsedApiKey(key_id=key_id, secret=secret, is_legacy=False)
    return ParsedApiKey(key_id=None, secret=rest, is_legacy=True)


def digest_api_key_secret(secret: str) -> str:
    """Return the deterministic digest of a new-format key secret (sha256 hex).

    API keys are high-entropy random secrets, not human passwords, so a fast
    deterministic digest is appropriate (unlike bcrypt, which exists to slow
    down offline guessing of low-entropy passwords). Do not reuse this helper
    for anything password-like.
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def verify_api_key_secret(secret: str, stored_digest: str) -> bool:
    """Constant-time check of a presented secret against its stored digest."""
    try:
        return hmac.compare_digest(digest_api_key_secret(secret), stored_digest)
    except (TypeError, ValueError):
        return False


def hash_api_key(plaintext: str) -> str:
    """Bcrypt-hash a plaintext key for storage. Returns a ``str`` hash.

    LEGACY: used only by the transitional fallback path and pre-AUD-003 key
    creation. New keys use :func:`digest_api_key_secret` instead.
    """
    return bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt()).decode()


def verify_api_key(plaintext: str, key_hash: str) -> bool:
    """Constant-time check of a plaintext key against a stored bcrypt hash.

    LEGACY: only the transitional fallback path calls this.
    """
    try:
        return bcrypt.checkpw(plaintext.encode(), key_hash.encode())
    except (ValueError, TypeError):
        return False


# --- Short-TTL principal cache (new-format keys only) -----------------------


class _PrincipalCache:
    """In-process TTL cache of resolved principals for new-format keys.

    The cache key is ``(key_id, digest)`` where ``digest`` is the sha256 of the
    presented secret — never the raw token. Only successful verifications are
    cached; failures always re-query. Revocation takes effect after at most the
    configured TTL. Access is single-threaded per event loop (one uvicorn
    worker), so a plain dict suffices.
    """

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], tuple[Principal, float]] = {}

    def get(self, key_id: str, secret_digest: str) -> Principal | None:
        ttl = settings.api_key_cache_ttl_seconds
        if ttl <= 0:
            return None
        entry = self._store.get((key_id, secret_digest))
        if entry is None:
            return None
        principal, expires_at = entry
        if time.monotonic() >= expires_at:
            self._store.pop((key_id, secret_digest), None)
            return None
        return principal

    def put(self, key_id: str, secret_digest: str, principal: Principal) -> None:
        ttl = settings.api_key_cache_ttl_seconds
        if ttl <= 0:
            return
        max_size = settings.api_key_cache_max_size
        self._store[(key_id, secret_digest)] = (principal, time.monotonic() + ttl)
        if len(self._store) > max_size:
            # Evict the ~10% soonest-expiring entries to amortize the sort.
            evict_count = max(1, len(self._store) - max_size + max_size // 10)
            for key, _ in sorted(self._store.items(), key=lambda kv: kv[1][1])[:evict_count]:
                self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()


_principal_cache = _PrincipalCache()


def reset_principal_cache() -> None:
    """Drop all cached principals (test/helper hook)."""
    _principal_cache.clear()


# --- Session factory accessor (breaks import cycle with engram.db) -----------


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Lazy accessor — avoids a module-level import cycle with engram.db.

    Returns the *owner* session factory. Principal/key resolution must see
    ``principals``/``api_keys`` across ALL tenants (it does not yet know which
    tenant a key belongs to), so it must bypass RLS — which the non-owner app
    role cannot. The owner role (a superuser in the default deployment) bypasses
    RLS, making this lookup correct and keeping the resolved tenant/principal
    out of the RLS-protected path until the request session applies it.
    """
    from engram.db import owner_session_factory

    return owner_session_factory


# --- Principal resolution ----------------------------------------------------


def _parse_scopes(raw_scopes: str | list[str]) -> list[str]:
    """Normalize the scopes column into a list.

    Postgres returns TEXT[] (list); SQLite-based tests return TEXT (str). The
    ``api_keys.scopes`` column is NOT NULL, so ``raw_scopes`` is never None in
    practice, but it is typed loosely to satisfy the two drivers.
    """
    if isinstance(raw_scopes, str):
        return [s for s in raw_scopes.split(",") if s]
    return list(raw_scopes)


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


async def _resolve_new_format_key(parsed: ParsedApiKey) -> Principal | None:
    """Resolve a new-format key via O(1) indexed lookup + digest verification.

    Returns the resolved :class:`Principal` on success. Returns ``None`` when no
    row matches the ``key_id`` (so the caller can fall back to the legacy scan —
    a legacy token whose random segment contained an underscore parses as
    new-format and must still be tried against bcrypt). Raises 401 when a
    genuine new-format key is found but its secret digest does not match (a hard
    failure: do not fall back to the legacy scan for a presented wrong secret).
    """
    assert parsed.key_id is not None  # new-format parse always populates key_id
    secret_digest = digest_api_key_secret(parsed.secret)

    cached = _principal_cache.get(parsed.key_id, secret_digest)
    if cached is not None:
        return cached

    async with _get_session_factory()() as session:
        row = (
            await session.execute(
                text(
                    "SELECT CAST(tenant_id AS TEXT) AS tenant_id, "
                    "       CAST(principal_id AS TEXT) AS principal_id, "
                    "       scopes AS scopes, "
                    "       secret_digest AS secret_digest "
                    "FROM api_keys "
                    "WHERE key_id = :kid AND revoked_at IS NULL"
                ),
                {"kid": parsed.key_id},
            )
        ).first()

    if row is None:
        return None

    if not verify_api_key_secret(parsed.secret, row.secret_digest):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    principal = Principal(
        tenant_id=row.tenant_id,
        principal_id=row.principal_id or "",
        scopes=tuple(_parse_scopes(row.scopes)),
    )
    _principal_cache.put(parsed.key_id, secret_digest, principal)
    return principal


async def _resolve_legacy_key(token: str) -> Principal | None:
    """Resolve a legacy ``eng_<random>`` bcrypt key (transitional fallback).

    LEGACY / TRANSITIONAL — exists only for keys created before ENG-AUD-003.
    Because bcrypt salts its hashes, a legacy key cannot be looked up by value,
    so this scans the legacy rows (``key_id IS NULL``) and bcrypt-checks each.
    It is O(n·bcrypt) by design of bcrypt; it must be removed in a future major
    migration or explicit cleanup task once legacy keys are rotated out.

    Scoped to ``key_id IS NULL`` so it never re-scans new-format rows.
    """
    async with _get_session_factory()() as session:
        result = await session.execute(
            text(
                "SELECT CAST(tenant_id AS TEXT) AS tenant_id, "
                "       CAST(principal_id AS TEXT) AS principal_id, "
                "       key_hash AS key_hash, "
                "       scopes AS scopes "
                "FROM api_keys "
                "WHERE key_id IS NULL AND revoked_at IS NULL"
            )
        )
        for row in result:
            if verify_api_key(token, row.key_hash):
                return Principal(
                    tenant_id=row.tenant_id,
                    principal_id=row.principal_id or "",
                    scopes=tuple(_parse_scopes(row.scopes)),
                )
    return None


async def get_current_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),  # noqa: B008
) -> Principal:
    """FastAPI dependency — resolve the caller from the Authorization header.

    - Health endpoints (``/health``, ``/ready``) are always exempt.
    - Auth disabled → default tenant/admin with all scopes.
    - Auth enabled  → a Bearer token is required.

    New-format tokens (``eng_<key_id>_<secret>``) resolve with a single indexed
    lookup and a constant-time digest check — no scan, no bcrypt. A token whose
    ``key_id`` is not found falls through to the legacy bcrypt scan, because a
    legacy token whose random segment contained an underscore parses as
    new-format and must still be tried against the legacy rows.

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

    try:
        parsed = parse_api_key(token)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key format",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None

    if not parsed.is_legacy:
        principal = await _resolve_new_format_key(parsed)
        if principal is not None:
            return principal
        # key_id not found — may be a legacy key whose random segment contained
        # an underscore. Fall through to the legacy bcrypt scan.

    principal = await _resolve_legacy_key(token)
    if principal is not None:
        return principal

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
