"""Self-service agent principal and API-key management.

These endpoints let any authenticated user with ``write`` scope manage agent
principals within their own tenant — no ``admin`` scope required. Tenant
isolation is enforced by RLS: a caller can only ever see and touch their own
tenant's principals and keys, because the RLS context (``app.tenant_id``) is
set from the authenticated API key before any query runs.

The admin endpoints (``/v1/admin/principals``, ``/v1/admin/api-keys``) remain
for cross-tenant platform operators. These self-service endpoints are the
user-facing layer that will power hosted onboarding and multi-agent fleets on
a self-hosted instance.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from engram.auth import (
    DIGEST_ALGORITHM,
    READ_SCOPE,
    WRITE_SCOPE,
    canonicalize_scopes,
    digest_api_key_secret,
    generate_api_key,
    get_current_principal,
    parse_api_key,
    validate_principal_name,
    validate_principal_type,
)
from engram.auth import (
    Principal as AuthPrincipal,
)
from engram.db import get_session
from engram.models import Principal as PrincipalModel

router = APIRouter()


# --- Schemas -----------------------------------------------------------------


class AgentCreate(BaseModel):
    """Create a new agent principal in the caller's tenant."""

    name: str = Field(min_length=1, max_length=255)
    # Optional scopes for the auto-generated API key. Defaults to read+write
    # — the minimum useful set for an agent that reads and writes memory.
    scopes: list[str] = Field(default=["read", "write"])
    label: str | None = None

    def validated_scopes(self) -> list[str]:
        return canonicalize_scopes(self.scopes)


class AgentOut(BaseModel):
    """Public representation of an agent principal (no secrets)."""

    id: uuid.UUID
    name: str
    type: str
    created_at: datetime


class AgentCreated(AgentOut):
    """Response after agent creation — includes the one-time plaintext key."""

    key: str  # plaintext, shown exactly once
    key_id: uuid.UUID
    scopes: list[str]
    label: str | None


class AgentListOut(BaseModel):
    """Listing entry — never exposes key material."""

    id: uuid.UUID
    name: str
    type: str
    created_at: datetime


# --- Endpoints ---------------------------------------------------------------


@router.post(
    "/agents",
    response_model=AgentCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Create an agent principal and return a one-time API key",
    dependencies=[Depends(WRITE_SCOPE)],
)
async def create_agent(
    body: AgentCreate,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    caller: AuthPrincipal = Depends(get_current_principal),  # noqa: B008
) -> AgentCreated:
    """Create a new agent principal in the caller's tenant and issue a scoped
    API key for it.

    The tenant is inferred from the authenticated API key — the caller cannot
    specify a different tenant. The returned ``key`` is the plaintext API key;
    it is shown exactly once and must be stored by the caller.
    """
    tenant_id = uuid.UUID(caller.tenant_id)

    try:
        validate_principal_name(body.name)
        validate_principal_type("agent")
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    scopes = body.validated_scopes()

    # Create the agent principal.
    principal = PrincipalModel(
        tenant_id=tenant_id,
        name=body.name,
        type="agent",
        created_at=datetime.now(UTC),
    )
    session.add(principal)
    await session.flush()  # get principal.id

    # Issue a scoped API key for the new agent.
    plaintext = generate_api_key()
    parsed = parse_api_key(plaintext)
    assert parsed.key_id is not None
    secret_digest = digest_api_key_secret(parsed.secret)
    key_label = body.label or f"agent:{body.name}"
    key_id_uuid = uuid.uuid4()
    # Detect SQLite (test path) vs Postgres (production). On Postgres,
    # asyncpg natively binds Python lists to TEXT[] columns. SQLite needs
    # a comma-separated string (auth._parse_scopes handles both).
    dialect_name = session.bind.dialect.name if session.bind else ""
    is_sqlite = dialect_name == "sqlite"
    scopes_value: str | list[str] = ",".join(scopes) if is_sqlite else scopes

    await session.execute(
        text(
            "INSERT INTO api_keys "
            "  (id, tenant_id, principal_id, key_hash, key_id, secret_digest, "
            "   digest_algorithm, scopes, label, created_at, revoked_at) "
            "VALUES (:id, :tid, :pid, NULL, :kid, :sd, :da, :sc, :lbl, :ts, NULL)"
        ),
        {
            "id": str(key_id_uuid),
            "tid": str(tenant_id),
            "pid": str(principal.id),
            "kid": parsed.key_id,
            "sd": secret_digest,
            "da": DIGEST_ALGORITHM,
            "sc": scopes_value,
            "lbl": key_label,
            "ts": datetime.now(UTC),
        },
    )
    await session.commit()
    await session.refresh(principal)

    return AgentCreated(
        id=principal.id,
        name=principal.name,
        type=principal.type,
        created_at=principal.created_at,
        key=plaintext,
        key_id=key_id_uuid,
        scopes=scopes,
        label=key_label,
    )


@router.get(
    "/agents",
    response_model=list[AgentListOut],
    summary="List agent principals in the caller's tenant",
    dependencies=[Depends(READ_SCOPE)],
)
async def list_agents(
    session: AsyncSession = Depends(get_session),  # noqa: B008
    caller: AuthPrincipal = Depends(get_current_principal),  # noqa: B008
) -> list[AgentListOut]:
    """List all agent-type principals in the caller's tenant.

    Non-agent principals (users, admins, system principals) are excluded.
    RLS ensures the caller only sees their own tenant's principals.
    """
    result = await session.execute(
        select(PrincipalModel)
        .where(
            PrincipalModel.type == "agent",
            PrincipalModel.tenant_id == uuid.UUID(caller.tenant_id),
        )
        .order_by(PrincipalModel.created_at.desc())
    )
    return [
        AgentListOut(
            id=p.id, name=p.name, type=p.type, created_at=p.created_at
        )
        for p in result.scalars()
    ]


@router.delete(
    "/agents/{agent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an agent — invalidates all its API keys",
    dependencies=[Depends(WRITE_SCOPE)],
)
async def delete_agent(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    caller: AuthPrincipal = Depends(get_current_principal),  # noqa: B008
) -> None:
    """Revoke an agent principal by setting ``revoked_at`` on all its API keys.

    The principal row itself is retained for audit/history (memory items
    reference it by ``principal_id``). All API keys for the agent are
    immediately revoked, so the agent can no longer authenticate. RLS ensures
    a caller can only revoke agents within their own tenant.
    """
    result = await session.execute(
        select(PrincipalModel).where(
            PrincipalModel.id == agent_id,
            PrincipalModel.type == "agent",
            PrincipalModel.tenant_id == uuid.UUID(caller.tenant_id),
        )
    )
    agent = result.scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")

    # Revoke all non-revoked keys for this agent.
    now = datetime.now(UTC)
    await session.execute(
        text(
            "UPDATE api_keys SET revoked_at = :ts "
            "WHERE principal_id = :pid AND revoked_at IS NULL"
        ),
        {"ts": now, "pid": str(agent_id)},
    )
    await session.commit()
