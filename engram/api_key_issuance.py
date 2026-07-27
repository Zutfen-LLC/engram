"""Shared ordinary API-key issuance without transaction ownership."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from engram.auth import (
    DIGEST_ALGORITHM,
    assert_principal_credentialable,
    canonicalize_scopes,
    digest_api_key_secret,
    generate_api_key,
    parse_api_key,
)
from engram.memory_profiles import ActiveMemoryProfile, validate_key_binding
from engram.models import ApiKey


@dataclass(frozen=True)
class IssuedApiKey:
    api_key: ApiKey
    plaintext: str
    active_profile: ActiveMemoryProfile | None


async def issue_api_key(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID | None,
    scopes: list[str],
    label: str | None,
    memory_profile_id: uuid.UUID | None,
    profile_event_actor_principal_id: uuid.UUID | None = None,
    profile_event_reason: str = "API key issuance",
) -> IssuedApiKey:
    """Generate and insert one ordinary key; never commit or persist plaintext."""
    canonical_scopes = canonicalize_scopes(scopes)
    if principal_id is not None:
        await assert_principal_credentialable(
            session, tenant_id=tenant_id, principal_id=principal_id
        )
    active_profile = await validate_key_binding(session, tenant_id, memory_profile_id)

    plaintext = generate_api_key()
    parsed = parse_api_key(plaintext)
    assert parsed.key_id is not None
    created_at = datetime.now(UTC)
    api_key = ApiKey(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        principal_id=principal_id,
        memory_profile_id=memory_profile_id,
        key_hash=None,
        key_id=parsed.key_id,
        secret_digest=digest_api_key_secret(parsed.secret),
        digest_algorithm=DIGEST_ALGORITHM,
        scopes=canonical_scopes,
        label=label,
        created_at=created_at,
        revoked_at=None,
    )

    dialect_name = session.bind.dialect.name if session.bind else ""
    scopes_value: str | list[str] = (
        ",".join(canonical_scopes) if dialect_name == "sqlite" else canonical_scopes
    )
    params: dict[str, object] = {
        "id": str(api_key.id),
        "tenant_id": str(tenant_id),
        "principal_id": str(principal_id) if principal_id is not None else None,
        "key_id": parsed.key_id,
        "secret_digest": api_key.secret_digest,
        "digest_algorithm": DIGEST_ALGORITHM,
        "scopes": scopes_value,
        "label": label,
        "memory_profile_id": str(memory_profile_id) if memory_profile_id else None,
        "created_at": created_at,
    }
    if dialect_name == "sqlite":
        await session.execute(
            text(
                "INSERT INTO api_keys "
                "(id, tenant_id, principal_id, key_hash, key_id, secret_digest, "
                "digest_algorithm, scopes, label, created_at, revoked_at) "
                "VALUES (:id, :tenant_id, :principal_id, NULL, :key_id, :secret_digest, "
                ":digest_algorithm, :scopes, :label, :created_at, NULL)"
            ),
            params,
        )
    else:
        await session.execute(
            text(
                "INSERT INTO api_keys "
                "(id, tenant_id, principal_id, key_hash, key_id, secret_digest, "
                "digest_algorithm, scopes, label, memory_profile_id, created_at, revoked_at) "
                "VALUES (:id, :tenant_id, :principal_id, NULL, :key_id, :secret_digest, "
                ":digest_algorithm, :scopes, :label, :memory_profile_id, :created_at, NULL)"
            ),
            params,
        )

    if active_profile is not None:
        await session.execute(
            text(
                "INSERT INTO memory_profile_events "
                "(tenant_id, profile_id, revision_id, actor_principal_id, "
                "event_type, reason, details) "
                "VALUES (:tenant_id, :profile_id, :revision_id, :actor_principal_id, "
                "'profile_bound_at_key_issuance', :reason, "
                "jsonb_build_object('api_key_id', CAST(:api_key_id AS text), "
                "'label', CAST(:label AS text)))"
            ),
            {
                "tenant_id": str(tenant_id),
                "profile_id": str(active_profile.id),
                "revision_id": str(active_profile.revision_id),
                "actor_principal_id": (
                    str(profile_event_actor_principal_id)
                    if profile_event_actor_principal_id is not None
                    else None
                ),
                "reason": profile_event_reason,
                "api_key_id": str(api_key.id),
                "label": label,
            },
        )
    return IssuedApiKey(api_key=api_key, plaintext=plaintext, active_profile=active_profile)
