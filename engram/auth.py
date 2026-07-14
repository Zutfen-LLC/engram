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
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from engram.config import settings

Scope = Literal["read", "write", "review", "export", "admin"]
VALID_SCOPES: frozenset[str] = frozenset({"read", "write", "review", "export", "admin"})

# Canonical serialization/persistence order (V2-BL-004). Independent of the
# order scopes were requested in — issuance always persists/returns scopes in
# this order so stored rows and API responses are deterministic.
CANONICAL_SCOPE_ORDER: tuple[Scope, ...] = ("read", "write", "review", "export", "admin")


def canonicalize_scopes(scopes: Iterable[str]) -> list[str]:
    """Validate, dedupe, and canonically order a requested scope list.

    Raises ``ValueError`` naming every unknown scope string rather than
    silently dropping or "correcting" it — issuance must fail loudly on a
    typo (e.g. ``"reviews"``) instead of treating it as a harmless no-op.
    An empty input returns an empty list (an explicitly scopeless key is
    valid; this function never substitutes a default).
    """
    granted = set(scopes)
    unknown = sorted(granted - VALID_SCOPES)
    if unknown:
        raise ValueError(f"unknown scope(s): {', '.join(unknown)}")
    return [s for s in CANONICAL_SCOPE_ORDER if s in granted]

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
    """The authenticated caller — tenant + principal + granted scopes.

    ``internal_key`` carries the server-owned internal identity marker from the
    ``principals`` row (V2-BL-003B). It is ``None`` for every ordinary
    principal. A non-null ``internal_key`` marks a trusted internal actor that
    must never authenticate via an API key — :func:`is_internal` enforces this
    invariant at every authentication path.
    """

    tenant_id: str
    principal_id: str
    scopes: tuple[str, ...]
    internal_key: str | None = None

    @property
    def is_internal(self) -> bool:
        """True when this is a server-owned internal principal.

        Internal principals are non-credentialable: they can never authenticate
        through an API key, even if a key row is inserted manually. Trusted
        operations select them via server code, not authentication.
        """
        return self.internal_key is not None

    def has_scope(self, required: str) -> bool:
        """Whether this principal's granted scopes satisfy ``required``.

        ``admin`` is a super-scope and satisfies every valid requirement
        (V2-BL-004). Unknown scope strings present in ``self.scopes`` (e.g.
        historical rows predating scope validation) never confer authority —
        only an exact match or ``admin`` does.
        """
        return "admin" in self.scopes or required in self.scopes


def principal_has_scope(principal: Principal, required: Scope) -> bool:
    """Centralized scope-evaluation rule (V2-BL-004). See :meth:`Principal.has_scope`."""
    return principal.has_scope(required)


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
        # Legacy url-safe random material may itself start or end with ``_``.
        # Those tokens are ambiguous with a malformed new-format token, but
        # treating them as legacy is safe: resolution still requires an exact
        # bcrypt match against a pre-existing legacy row. Rejecting them here
        # probabilistically locks out valid pre-AUD-003 credentials.
        if not key_id or not secret:
            return ParsedApiKey(key_id=None, secret=rest, is_legacy=True)
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
    """Look up the seed default tenant/admin (auth-disabled path).

    The seed admin principal has ``internal_key = NULL`` (an ordinary admin),
    so this path never resolves an internal principal. The internal_key is
    selected and asserted to confirm the invariant — a fail-closed guard
    against a future seed change.
    """
    row = (
        await session.execute(
            text(
                "SELECT CAST(t.id AS TEXT) AS tenant_id, "
                "       CAST(p.id AS TEXT) AS principal_id, "
                "       p.internal_key AS internal_key "
                "FROM tenants t "
                "JOIN principals p "
                "  ON p.tenant_id = t.id AND p.name = :principal "
                "WHERE t.slug = :slug"
            ),
            {"slug": "default", "principal": "admin"},
        )
    ).one()
    if row.internal_key is not None:
        raise RuntimeError(
            "seed default principal has an internal_key — auth disabled path is unsafe"
        )
    return Principal(
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        scopes=("read", "write", "admin", "export"),
        internal_key=None,
    )


async def _resolve_new_format_key(parsed: ParsedApiKey) -> Principal | None:
    """Resolve a new-format key via O(1) indexed lookup + digest verification.

    Returns the resolved :class:`Principal` on success. Returns ``None`` when no
    row matches the ``key_id`` (so the caller can fall back to the legacy scan —
    a legacy token whose random segment contained an underscore parses as
    new-format and must still be tried against bcrypt). Raises 401 when a
    genuine new-format key is found but its secret digest does not match (a hard
    failure: do not fall back to the legacy scan for a presented wrong secret),
    or when the resolved principal is an internal (non-credentialable) actor.

    Fail-closed for internal principals (V2-BL-003B): if the key row resolves to
    a principal with ``internal_key IS NOT NULL``, authentication fails with a
    normal 401 — no Principal object carrying trusted authority is returned, and
    no success cache entry is created. This protects against direct DB key
    insertion, older app versions, and missed issuance paths.
    """
    assert parsed.key_id is not None  # new-format parse always populates key_id
    secret_digest = digest_api_key_secret(parsed.secret)

    cached = _principal_cache.get(parsed.key_id, secret_digest)
    if cached is not None:
        if cached.is_internal:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or revoked API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return cached

    async with _get_session_factory()() as session:
        row = (
            await session.execute(
                text(
                    "SELECT CAST(api_keys.tenant_id AS TEXT) AS tenant_id, "
                    "       CAST(api_keys.principal_id AS TEXT) AS principal_id, "
                    "       api_keys.scopes AS scopes, "
                    "       api_keys.secret_digest AS secret_digest, "
                    "       (SELECT p.internal_key FROM principals p "
                    "        WHERE p.id = api_keys.principal_id) AS principal_internal_key "
                    "FROM api_keys "
                    "WHERE api_keys.key_id = :kid AND api_keys.revoked_at IS NULL"
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

    # Fail-closed: an internal principal must never authenticate via API key,
    # even if a key row was inserted manually. Return a normal auth failure
    # (not a distinct error) so the existence of an internal principal is not
    # disclosed.
    if _row_is_internal(row.principal_internal_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    principal = Principal(
        tenant_id=row.tenant_id,
        principal_id=row.principal_id or "",
        scopes=tuple(_parse_scopes(row.scopes)),
        internal_key=None,
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

    Fail-closed for internal principals (V2-BL-003B): if a matching key resolves
    to a principal with ``internal_key IS NOT NULL``, this path returns ``None``
    (fall through to 401) — no Principal object carrying trusted authority is
    returned. The check runs after bcrypt verification so a wrong secret still
    fails fast at the digest/hash check.
    """
    async with _get_session_factory()() as session:
        result = await session.execute(
            text(
                "SELECT CAST(api_keys.tenant_id AS TEXT) AS tenant_id, "
                "       CAST(api_keys.principal_id AS TEXT) AS principal_id, "
                "       api_keys.key_hash AS key_hash, "
                "       api_keys.scopes AS scopes, "
                "       (SELECT p.internal_key FROM principals p "
                "        WHERE p.id = api_keys.principal_id) AS principal_internal_key "
                "FROM api_keys "
                "WHERE api_keys.key_id IS NULL AND api_keys.revoked_at IS NULL"
            )
        )
        for row in result:
            if verify_api_key(token, row.key_hash):
                # Fail-closed: internal principals cannot authenticate via API
                # key. Access defensively — the LEFT JOIN may produce NULL.
                internal_key = row.principal_internal_key if row.principal_internal_key else None
                if _row_is_internal(internal_key):
                    return None
                return Principal(
                    tenant_id=row.tenant_id,
                    principal_id=row.principal_id or "",
                    scopes=tuple(_parse_scopes(row.scopes)),
                    internal_key=None,
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


@dataclass(frozen=True)
class ScopePolicy:
    """Declarative scope requirement for one route (V2-BL-004).

    ``all_of`` (AND) and ``any_of`` (OR) may both be set (e.g. a route could
    require one baseline scope plus one of a set of privileged scopes), though
    in practice every route in this codebase uses exactly one of the two.
    ``exempt`` marks a route as having no scope requirement at all (only
    ``/health`` and ``/ready``). ``conditional`` is documentation-only metadata
    surfaced in OpenAPI for routes whose *effective* requirement depends on
    request/DB state beyond what a static guard can express (e.g. the mixed
    review-transition endpoint) — the actual conditional enforcement lives in
    application code, not here.
    """

    all_of: tuple[Scope, ...] = ()
    any_of: tuple[Scope, ...] = ()
    exempt: bool = False
    description: str | None = None
    conditional: dict[str, str] | None = None


def _validate_scope_set(scopes: tuple[str, ...]) -> None:
    unknown = set(scopes) - VALID_SCOPES
    if unknown:
        raise ValueError(f"Unknown scope(s): {sorted(unknown)}")


class ScopeGuard:
    """FastAPI dependency enforcing a non-exempt :class:`ScopePolicy`.

    Introspectable (``.policy``) so the OpenAPI extension and the route-
    completeness test can derive their view of the world from the exact same
    object used at runtime — there is no second, hand-maintained table of
    route scopes anywhere else.
    """

    def __init__(
        self,
        *,
        all_of: tuple[Scope, ...] = (),
        any_of: tuple[Scope, ...] = (),
        description: str | None = None,
        conditional: dict[str, str] | None = None,
    ) -> None:
        _validate_scope_set(all_of)
        _validate_scope_set(any_of)
        if not all_of and not any_of:
            raise ValueError("ScopeGuard requires all_of or any_of")
        self.policy = ScopePolicy(
            all_of=tuple(all_of),
            any_of=tuple(any_of),
            description=description,
            conditional=conditional,
        )

    async def __call__(
        self, principal: Principal = Depends(get_current_principal)  # noqa: B008
    ) -> Principal:
        if self.policy.all_of:
            missing = [s for s in self.policy.all_of if not principal.has_scope(s)]
            if missing:
                label = "scope" if len(missing) == 1 else "scopes"
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Requires {label}: {', '.join(missing)}",
                )
        if self.policy.any_of and not any(
            principal.has_scope(s) for s in self.policy.any_of
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of scopes: {', '.join(self.policy.any_of)}",
            )
        return principal


class ExemptScopeGuard:
    """Marks a route as explicitly exempt from scope enforcement.

    Deliberately takes no parameters — it never resolves a principal and
    never touches the database. This is what keeps ``/health`` a true,
    dependency-free liveness probe while still giving the route-completeness
    test an introspectable policy object to find (every route must have
    exactly one; only ``/health``/``/ready`` may be exempt).
    """

    def __init__(self, *, reason: str) -> None:
        self.policy = ScopePolicy(exempt=True, description=reason)

    async def __call__(self) -> None:
        return None


def require_scopes(*required: Scope) -> ScopeGuard:
    """Dependency factory — enforces that the caller holds every scope.

    Usage::

        @router.post(..., dependencies=[Depends(require_scopes("admin"))])
    """
    return ScopeGuard(all_of=tuple(required))


def require_any_scope(*required: Scope) -> ScopeGuard:
    """Dependency factory — enforces that the caller holds at least one scope."""
    return ScopeGuard(any_of=tuple(required))


# --- Shared scope-guard constants (V2-BL-004) --------------------------------
#
# Route modules import these rather than constructing their own guards, so
# every route sharing a requirement uses the exact same object — this is what
# lets `/v1/admin/*` be verified as sharing "one canonical runtime guard" and
# keeps the OpenAPI extension free of hand-duplicated scope strings.

READ_SCOPE = ScopeGuard(all_of=("read",), description="Ordinary read/retrieval operations.")
WRITE_SCOPE = ScopeGuard(all_of=("write",), description="Ordinary data creation and mutation.")
REVIEW_SCOPE = ScopeGuard(
    all_of=("review",), description="Review-domain access and privileged review actions."
)
EXPORT_SCOPE = ScopeGuard(all_of=("export",), description="Export operations.")
ADMIN_SCOPE = ScopeGuard(all_of=("admin",), description="Administrative operations.")
WRITE_OR_REVIEW_SCOPE = ScopeGuard(
    any_of=("write", "review"),
    description=(
        "Base admission for the mixed review-transition endpoint. Collaborative "
        "actions (dispute, self-withdrawal) need only `write`; privileged review "
        "decisions additionally require `review` — enforced in the route handler "
        "after item eligibility is resolved."
    ),
    conditional={"privileged_review_transitions": "review"},
)

HEALTH_EXEMPT_SCOPE = ExemptScopeGuard(reason="liveness probe")
READY_EXEMPT_SCOPE = ExemptScopeGuard(reason="readiness probe")


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


# --- Key-issuance validation (V2-BL-003B) -----------------------------------


def _row_is_internal(internal_key: str | None) -> bool:
    """Shared predicate: a DB-resolved row is internal iff internal_key is non-null.

    All DB-resolve paths (new-format, legacy) route through this helper so the
    definition of "internal" has one source of truth, matching
    :attr:`Principal.is_internal`. If the definition evolves, only this
    helper and the ``Principal`` property need updating.
    """
    return internal_key is not None


# Reserved display-name prefix for server-owned internal principals. Caller-
# facing principal creation rejects any name starting with this prefix so an
# administrator cannot create a row that impersonates an internal actor.
INTERNAL_DISPLAY_NAME_PREFIX = "__engram_internal_review__"

# Allowed principal types for caller-facing creation (mirrors the DB CHECK
# constraint in migrations/001_init.sql). Validated in Python so the admin API
# returns a clear 422 rather than relying solely on the database constraint.
VALID_PRINCIPAL_TYPES: frozenset[str] = frozenset({"agent", "user", "system", "admin"})


class InternalPrincipalCredentialError(Exception):
    """Raised when key issuance targets an internal (non-credentialable) principal."""

    def __init__(self) -> None:
        super().__init__("cannot issue API keys for internal principals")


def validate_principal_name(name: str) -> None:
    """Reject caller-supplied principal names that use the reserved internal prefix.

    Ordinary principals named ``system`` remain allowed — the name is no longer
    security-sensitive (V2-BL-003B). Only names beginning with the server-owned
    internal prefix are rejected, since those could impersonate an internal
    actor's display name.
    """
    if name.startswith(INTERNAL_DISPLAY_NAME_PREFIX):
        raise ValueError(
            f"principal names starting with {INTERNAL_DISPLAY_NAME_PREFIX!r} are reserved "
            "for server-owned internal identities"
        )


def validate_principal_type(ptype: str) -> None:
    """Validate principal type against the allowed vocabulary.

    Mirrors the DB CHECK constraint but returns a clear error before hitting the
    database. ``system`` is allowed here — a caller may create an ordinary
    ``system``-type principal (with ``internal_key = NULL``); it is not trusted
    unless the server assigns an internal_key.
    """
    if ptype not in VALID_PRINCIPAL_TYPES:
        raise ValueError(
            f"principal type must be one of {sorted(VALID_PRINCIPAL_TYPES)}, got {ptype!r}"
        )


async def assert_principal_credentialable(
    session: AsyncSession,
    *,
    tenant_id: str | UUID,
    principal_id: str | UUID,
) -> None:
    """Reusable validation: reject API-key issuance for internal principals.

    Resolves the principal inside the caller's permitted tenant context (RLS-
    scoped session) and raises :class:`InternalPrincipalCredentialError` if the
    principal has ``internal_key IS NOT NULL``. A non-existent or cross-tenant
    principal ID resolves to ``None`` (no row) — the caller maps that to a 404
    or 409, not an internal-principal disclosure.

    All key-issuance paths (admin API, bootstrap-key, shared service functions)
    must route through this check so a single authority enforces the invariant.
    """
    row = (
        await session.execute(
            text(
                "SELECT internal_key "
                "FROM principals "
                "WHERE id = :pid AND tenant_id = :tid"
            ),
            {"pid": str(principal_id), "tid": str(tenant_id)},
        )
    ).first()

    if row is None:
        # Not found in this tenant — caller maps to 404/409. Not an internal
        # principal disclosure (no row means no internal_key).
        return

    if row.internal_key is not None:
        raise InternalPrincipalCredentialError()
