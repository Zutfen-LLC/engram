"""Opaque, single-use delegated bearer authentication.

Delegated credentials are intentionally separate from ordinary ``eng_`` API
keys and ``engsvc_`` service credentials. Only the indexed key id and a SHA-256
digest of the random secret are persisted.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import string
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import text

from engram.auth import Principal

DELEGATION_PREFIX = "engd_"
DELEGATION_AUDIENCE = "engram-core"
DELEGATION_SCOPES = ("read",)
DELEGATION_MIN_TTL_SECONDS = 30
DELEGATION_MAX_TTL_SECONDS = 300
_KEY_ID_LENGTH = 22
_SECRET_LENGTH = 43
_BASE62 = string.digits + string.ascii_lowercase + string.ascii_uppercase
_TOKEN_RE = re.compile(r"^engd_([0-9A-Za-z]{22})_([A-Za-z0-9_-]{43})$")
_VISIBLE_ASCII = re.compile(r"^[\x21-\x7e]{1,255}$")
_IDEMPOTENCY = re.compile(r"^[\x21-\x7e]{1,128}$")
_REQUEST_ID = re.compile(r"^[\x21-\x7e]{1,128}$")


@dataclass(frozen=True)
class ParsedDelegationToken:
    key_id: str
    secret: str = field(repr=False)


@dataclass(frozen=True)
class DelegationTokenMaterial:
    token: str = field(repr=False)
    key_id: str
    secret_digest: str = field(repr=False)


def _random_base62(length: int) -> str:
    chars: list[str] = []
    limit = (256 // len(_BASE62)) * len(_BASE62)
    while len(chars) < length:
        value = secrets.token_bytes(1)[0]
        if value < limit:
            chars.append(_BASE62[value % len(_BASE62)])
    return "".join(chars)


def generate_delegation_token() -> DelegationTokenMaterial:
    key_id = _random_base62(_KEY_ID_LENGTH)
    secret = secrets.token_urlsafe(32)
    token = f"{DELEGATION_PREFIX}{key_id}_{secret}"
    return DelegationTokenMaterial(
        token=token,
        key_id=key_id,
        secret_digest=digest_delegation_secret(secret),
    )


def parse_delegation_token(token: str) -> ParsedDelegationToken:
    if not token.isascii() or len(token) != len(DELEGATION_PREFIX) + 22 + 1 + 43:
        raise ValueError("invalid delegated credential")
    match = _TOKEN_RE.fullmatch(token)
    if match is None:
        raise ValueError("invalid delegated credential")
    return ParsedDelegationToken(key_id=match.group(1), secret=match.group(2))


def digest_delegation_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def verify_delegation_secret(secret: str, expected_digest: str) -> bool:
    try:
        return hmac.compare_digest(digest_delegation_secret(secret), expected_digest)
    except (TypeError, ValueError, UnicodeEncodeError):
        return False


def validate_delegation_external_ref(value: str) -> str:
    if _VISIBLE_ASCII.fullmatch(value) is None:
        raise ValueError("external references must be visible ASCII from 1 through 255 characters")
    return value


def validate_delegation_idempotency_key(value: str | None) -> str:
    if value is None or _IDEMPOTENCY.fullmatch(value) is None:
        raise ValueError("Idempotency-Key must be visible ASCII from 1 through 128 characters")
    return value


def validate_delegation_ttl(ttl_seconds: int, configured_maximum: int) -> int:
    if not DELEGATION_MIN_TTL_SECONDS <= ttl_seconds <= configured_maximum:
        raise ValueError(
            f"ttl_seconds must be between {DELEGATION_MIN_TTL_SECONDS} and "
            f"{configured_maximum}"
        )
    return ttl_seconds


def sha256_bytes(value: str) -> bytes:
    return hashlib.sha256(value.encode("ascii")).digest()


def canonical_delegation_request_digest(
    *,
    issuer_service_client_id: uuid.UUID,
    grant_id: uuid.UUID,
    binding_owner_service_client_id: uuid.UUID,
    tenant_binding_id: uuid.UUID,
    principal_binding_id: uuid.UUID,
    delegation_external_ref: str,
    ttl_seconds: int,
) -> bytes:
    payload = {
        "audience": DELEGATION_AUDIENCE,
        "binding_owner_service_client_id": str(binding_owner_service_client_id),
        "delegation_external_ref": delegation_external_ref,
        "grant_id": str(grant_id),
        "issuer_service_client_id": str(issuer_service_client_id),
        "principal_binding_id": str(principal_binding_id),
        "schema_version": 1,
        "scopes": list(DELEGATION_SCOPES),
        "single_use": True,
        "tenant_binding_id": str(tenant_binding_id),
        "ttl_seconds": ttl_seconds,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).digest()


def request_id_or_generated(candidate: str | None) -> str:
    if candidate is not None and _REQUEST_ID.fullmatch(candidate) is not None:
        return candidate
    return str(uuid.uuid4())


def delegated_unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or revoked API key",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _append_denial(
    session: Any,
    token_row: Any,
    *,
    request_id: str,
    reason_code: str,
) -> None:
    await session.execute(
        text(
            "INSERT INTO service_delegation_events "
            "(event_type,outcome,issuer_service_client_id,issuer_credential_id,"
            "binding_owner_service_client_id,grant_id,delegation_token_id,tenant_id,"
            "principal_id,request_id,reason_code,details) "
            "VALUES ('delegation.denied','failure',:issuer,:credential,:owner,:grant,"
            ":token,:tenant,:principal,:request_id,:reason,'{}'::jsonb)"
        ),
        {
            "issuer": token_row.issuer_service_client_id,
            "credential": token_row.issuer_credential_id,
            "owner": token_row.binding_owner_service_client_id,
            "grant": token_row.grant_id,
            "token": token_row.id,
            "tenant": token_row.tenant_id,
            "principal": token_row.principal_id,
            "request_id": request_id,
            "reason": reason_code,
        },
    )


async def resolve_delegated_principal(token: str, *, request_id: str | None) -> Principal:
    """Consume a delegated token atomically and return its bound human principal."""
    try:
        parsed = parse_delegation_token(token)
    except ValueError:
        raise delegated_unauthorized() from None

    from engram.db import owner_session_factory

    effective_request_id = request_id_or_generated(request_id)
    resolved: Principal | None = None
    denied = False
    try:
        async with owner_session_factory() as session, session.begin():
            token_row = (
                await session.execute(
                    text(
                        "SELECT id,grant_id,issuer_service_client_id,issuer_credential_id,"
                        "binding_owner_service_client_id,tenant_binding_id,"
                        "principal_binding_id,tenant_id,principal_id,secret_digest,status,"
                        "expires_at,scopes,audience FROM service_delegation_tokens "
                        "WHERE key_id=:key_id FOR UPDATE"
                    ),
                    {"key_id": parsed.key_id},
                )
            ).first()
            if token_row is None:
                denied = True
            elif not verify_delegation_secret(parsed.secret, token_row.secret_digest):
                await _append_denial(
                    session,
                    token_row,
                    request_id=effective_request_id,
                    reason_code="invalid_secret",
                )
                denied = True
            elif token_row.status != "active":
                await _append_denial(
                    session,
                    token_row,
                    request_id=effective_request_id,
                    reason_code="token_not_active",
                )
                denied = True
            elif token_row.expires_at <= datetime.now(UTC):
                await session.execute(
                    text(
                        "UPDATE service_delegation_tokens SET status='revoked',"
                        "revoked_at=now(),revocation_reason='expired' WHERE id=:id"
                    ),
                    {"id": token_row.id},
                )
                await _append_denial(
                    session, token_row, request_id=effective_request_id, reason_code="expired"
                )
                denied = True
            else:
                authority = (
                    await session.execute(
                        text(
                            "SELECT g.status AS grant_status,"
                            "g.issuer_service_client_id AS grant_issuer,"
                            "g.binding_owner_service_client_id AS grant_owner,"
                            "i.status AS issuer_status,i.permissions,o.status AS owner_status,"
                            "c.status AS credential_status,c.expires_at AS credential_expires_at,"
                            "c.service_client_id AS credential_issuer,"
                            "tb.service_client_id AS tenant_owner,tb.tenant_id AS bound_tenant,"
                            "pb.service_client_id AS principal_owner,"
                            "pb.tenant_binding_id AS principal_tenant_binding,"
                            "pb.tenant_id AS principal_tenant,pb.principal_id AS bound_principal,"
                            "p.type AS principal_type,p.internal_key "
                            "FROM service_delegation_grants g "
                            "JOIN service_clients i ON i.id=:issuer "
                            "JOIN service_clients o ON o.id=:owner "
                            "JOIN service_client_credentials c ON c.id=:credential "
                            "JOIN tenant_provisioning_bindings tb ON tb.id=:tenant_binding "
                            "JOIN principal_provisioning_bindings pb ON pb.id=:principal_binding "
                            "JOIN principals p ON p.id=:principal "
                            "WHERE g.id=:grant "
                            "FOR UPDATE OF g,i,o,c,tb,pb,p"
                        ),
                        {
                            "issuer": token_row.issuer_service_client_id,
                            "owner": token_row.binding_owner_service_client_id,
                            "credential": token_row.issuer_credential_id,
                            "tenant_binding": token_row.tenant_binding_id,
                            "principal_binding": token_row.principal_binding_id,
                            "principal": token_row.principal_id,
                            "grant": token_row.grant_id,
                        },
                    )
                ).first()
                now = datetime.now(UTC)
                permissions: list[str] = (
                    list(authority.permissions) if authority is not None else []
                )
                valid = (
                    authority is not None
                    and authority.grant_status == "active"
                    and authority.grant_issuer == token_row.issuer_service_client_id
                    and authority.grant_owner == token_row.binding_owner_service_client_id
                    and authority.issuer_status == "active"
                    and authority.owner_status == "active"
                    and authority.credential_status == "active"
                    and authority.credential_issuer == token_row.issuer_service_client_id
                    and (
                        authority.credential_expires_at is None
                        or authority.credential_expires_at > now
                    )
                    and "delegation.issue" in permissions
                    and authority.tenant_owner == token_row.binding_owner_service_client_id
                    and authority.bound_tenant == token_row.tenant_id
                    and authority.principal_owner == token_row.binding_owner_service_client_id
                    and authority.principal_tenant_binding == token_row.tenant_binding_id
                    and authority.principal_tenant == token_row.tenant_id
                    and authority.bound_principal == token_row.principal_id
                    and authority.principal_type == "user"
                    and authority.internal_key is None
                    and list(token_row.scopes) == ["read"]
                    and token_row.audience == DELEGATION_AUDIENCE
                )
                if not valid:
                    await session.execute(
                        text(
                            "UPDATE service_delegation_tokens SET status='revoked',"
                            "revoked_at=now(),revocation_reason='authority_invalidated' "
                            "WHERE id=:id AND status='active'"
                        ),
                        {"id": token_row.id},
                    )
                    await _append_denial(
                        session,
                        token_row,
                        request_id=effective_request_id,
                        reason_code="authority_invalid",
                    )
                    denied = True
                else:
                    await session.execute(
                        text(
                            "UPDATE service_delegation_tokens SET status='used',used_at=now() "
                            "WHERE id=:id AND status='active'"
                        ),
                        {"id": token_row.id},
                    )
                    await session.execute(
                        text(
                            "INSERT INTO service_delegation_events "
                            "(event_type,outcome,issuer_service_client_id,issuer_credential_id,"
                            "binding_owner_service_client_id,grant_id,delegation_token_id,"
                            "tenant_id,principal_id,request_id,reason_code,details) VALUES "
                            "('delegation.used','success',:issuer,:credential,:owner,:grant,"
                            ":token,:tenant,:principal,:request_id,'authenticated',"
                            "jsonb_build_object('scope_read',true,'single_use',true))"
                        ),
                        {
                            "issuer": token_row.issuer_service_client_id,
                            "credential": token_row.issuer_credential_id,
                            "owner": token_row.binding_owner_service_client_id,
                            "grant": token_row.grant_id,
                            "token": token_row.id,
                            "tenant": token_row.tenant_id,
                            "principal": token_row.principal_id,
                            "request_id": effective_request_id,
                        },
                    )
                    resolved = Principal(
                        tenant_id=str(token_row.tenant_id),
                        principal_id=str(token_row.principal_id),
                        scopes=DELEGATION_SCOPES,
                        internal_key=None,
                        api_key_id=None,
                        delegated_token_id=str(token_row.id),
                        delegation_grant_id=str(token_row.grant_id),
                    )
    except HTTPException:
        raise
    except Exception:
        raise delegated_unauthorized() from None

    if denied or resolved is None:
        raise delegated_unauthorized()
    return resolved
