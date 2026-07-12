"""Purpose-specific, immutable, non-credentialable internal principals."""

from __future__ import annotations

import secrets
import uuid

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from engram.auth import INTERNAL_DISPLAY_NAME_PREFIX

REVIEW_AUTOMATION_INTERNAL_KEY = "review_automation"
CONFLICT_AUTOMATION_INTERNAL_KEY = "conflict_automation"
CLASSIFICATION_AUTOMATION_INTERNAL_KEY = "classification_automation"

_ALLOWED_INTERNAL_KEYS = frozenset(
    {
        REVIEW_AUTOMATION_INTERNAL_KEY,
        CONFLICT_AUTOMATION_INTERNAL_KEY,
        CLASSIFICATION_AUTOMATION_INTERNAL_KEY,
    }
)
_MAX_NAME_GENERATION_RETRIES = 8
_LOOKUP = text(
    "SELECT id, tenant_id::text AS tenant_id, type, internal_key FROM principals "
    "WHERE tenant_id = :tenant_id AND internal_key = :internal_key"
)
_INSERT = text(
    "INSERT INTO principals (tenant_id, name, type, internal_key) "
    "VALUES (:tenant_id, :name, 'system', :internal_key) RETURNING id"
)


class InternalActorInvariantError(RuntimeError):
    """The canonical internal identity is missing or malformed."""


def _verify(row: object, tenant_id: str, internal_key: str) -> uuid.UUID:
    if str(row.tenant_id) != tenant_id:  # type: ignore[attr-defined]
        raise InternalActorInvariantError("internal actor tenant mismatch")
    if row.type != "system":  # type: ignore[attr-defined]
        raise InternalActorInvariantError("internal actor must have type 'system'")
    if row.internal_key != internal_key:  # type: ignore[attr-defined]
        raise InternalActorInvariantError("internal actor key mismatch")
    return uuid.UUID(str(row.id))  # type: ignore[attr-defined]


async def resolve_internal_system_actor(
    session: AsyncSession, *, tenant_id: uuid.UUID | str, internal_key: str
) -> uuid.UUID:
    """Resolve one canonical actor by immutable tenant/key, creating it safely."""
    if internal_key not in _ALLOWED_INTERNAL_KEYS:
        raise InternalActorInvariantError(f"unsupported internal actor key: {internal_key!r}")
    tid = str(tenant_id)
    params = {"tenant_id": tid, "internal_key": internal_key}
    row = (await session.execute(_LOOKUP, params)).first()
    if row is not None:
        return _verify(row, tid, internal_key)

    for attempt in range(_MAX_NAME_GENERATION_RETRIES):
        try:
            async with session.begin_nested():
                await session.execute(
                    _INSERT,
                    {
                        **params,
                        "name": f"{INTERNAL_DISPLAY_NAME_PREFIX}:{secrets.token_urlsafe(16)}",
                    },
                )
        except IntegrityError:
            row = (await session.execute(_LOOKUP, params)).first()
            if row is not None:
                return _verify(row, tid, internal_key)
            if attempt == _MAX_NAME_GENERATION_RETRIES - 1:
                raise InternalActorInvariantError(
                    f"could not create internal actor for tenant {tid}"
                ) from None
            continue
        row = (await session.execute(_LOOKUP, params)).first()
        if row is not None:
            return _verify(row, tid, internal_key)
    raise InternalActorInvariantError(f"could not resolve internal actor for tenant {tid}")
