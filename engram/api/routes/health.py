"""Health and readiness endpoints."""

from __future__ import annotations

import re
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from engram.auth import (
    HEALTH_EXEMPT_SCOPE,
    READ_SCOPE,
    READY_EXEMPT_SCOPE,
    Principal,
    get_current_principal,
)
from engram.config import settings
from engram.db import get_session
from engram.models import Principal as PrincipalModel

router = APIRouter()

# pgvector >= 0.8 is required for iterative_scan (used by filtered HNSW queries
# in semantic recall/search). Older versions boot fine but semantic reads 500 at
# runtime, so readiness must fail early on a too-old extension.
PGVECTOR_MINIMUM: tuple[int, int] = (0, 8)


def pgvector_version_satisfies(
    extversion: str | None, minimum: tuple[int, int] = PGVECTOR_MINIMUM
) -> bool:
    """Return True iff a pgvector ``extversion`` string meets ``minimum``.

    ``extversion`` is the value reported by ``pg_extension.extversion`` for the
    ``vector`` extension (e.g. ``"0.8.0"``, ``"0.7.4"``). ``None`` or an
    unparseable string means the extension is missing/broken → not satisfied.

    Only the leading ``major.minor`` is compared; dev/build suffixes are ignored.
    """
    if not extversion:
        return False
    match = re.match(r"\s*(\d+)\.(\d+)", extversion)
    if not match:
        return False
    parsed = (int(match.group(1)), int(match.group(2)))
    return parsed >= minimum


@router.get("/health", dependencies=[Depends(HEALTH_EXEMPT_SCOPE)])
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}


@router.get("/ready", response_model=None, dependencies=[Depends(READY_EXEMPT_SCOPE)])
async def readiness(
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, str | None] | JSONResponse:
    """Readiness probe — checks DB connectivity, RLS context, and pgvector.

    ``get_session`` sets the RLS context (``app.tenant_id`` /
    ``app.principal_id``) before yielding, so a successful execute here implies
    both DB connectivity and a resolvable RLS context. We additionally assert
    the installed pgvector extension meets the minimum version (>= 0.8) so that
    semantic recall/search cannot 500 at runtime on a too-old extension. We
    return 503 if any check fails.
    """
    try:
        result = await session.execute(text("SELECT current_setting('app.tenant_id', true)"))
        tenant_id = result.scalar()
        # If RLS context wasn't set (e.g. seed data missing), tenant_id is NULL.
        if not tenant_id:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    "database": "connected",
                    "rls": "no_tenant_context",
                },
            )

        ext_result = await session.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
        pgvector_version = ext_result.scalar()
        if not pgvector_version_satisfies(pgvector_version):
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    "database": "connected",
                    "rls": "ok",
                    "pgvector": pgvector_version or "missing",
                    "minimum_required": f"{PGVECTOR_MINIMUM[0]}.{PGVECTOR_MINIMUM[1]}.0",
                    "reason": (
                        f"pgvector >= {PGVECTOR_MINIMUM[0]}.{PGVECTOR_MINIMUM[1]} required "
                        "(iterative_scan for semantic recall)"
                    ),
                },
            )

        if settings.service_provisioning_enabled:
            from engram.db import require_provisioner_session_factory

            try:
                async with require_provisioner_session_factory()() as provisioner:
                    role = (
                        await provisioner.execute(
                            text(
                                "SELECT r.rolname, r.rolcanlogin, r.rolsuper, r.rolbypassrls, "
                                "r.rolcreatedb, r.rolcreaterole, r.rolreplication, r.rolinherit, "
                                "EXISTS (SELECT 1 FROM pg_auth_members m "
                                "WHERE m.member = r.oid) AS prohibited_membership, "
                                "has_schema_privilege(current_user, 'public', 'CREATE') "
                                "AS schema_create "
                                "FROM pg_roles r WHERE r.rolname = current_user"
                            )
                        )
                    ).mappings().first()
                    tables = await provisioner.execute(
                        text(
                            "SELECT count(*) FROM information_schema.tables "
                            "WHERE table_schema = 'public' "
                            "AND table_name IN ('service_clients', 'service_client_credentials', "
                            "'tenant_provisioning_bindings', 'principal_provisioning_bindings', "
                            "'workspace_provisioning_bindings', "
                            "'agent_api_key_provisioning_bindings', "
                            "'service_provisioning_idempotency', "
                            "'service_workspace_agent_idempotency', "
                            "'service_agent_key_idempotency', "
                            "'service_provisioning_events')"
                        )
                    )
                    expected_role = settings.provisioner_database_role
                    invalid_role = (
                        role is None
                        or role["rolname"] != expected_role
                        or not role["rolcanlogin"]
                        or role["rolsuper"]
                        or role["rolbypassrls"]
                        or role["rolcreatedb"]
                        or role["rolcreaterole"]
                        or role["rolreplication"]
                        or role["rolinherit"]
                        or role["prohibited_membership"]
                        or role["schema_create"]
                    )
                    function = await provisioner.execute(
                        text(
                            "SELECT to_regprocedure('current_service_client_id()') IS NOT NULL "
                            "AND to_regprocedure("
                            "'current_service_client_has_permission(text)') IS NOT NULL "
                            "AND to_regprocedure("
                            "'assert_service_agent_api_key_binding(uuid)') IS NOT NULL "
                            "AND to_regprocedure("
                            "'enforce_service_agent_key_binding()') IS NOT NULL "
                            "AND to_regprocedure("
                            "'enforce_provisioner_api_key_binding()') IS NOT NULL"
                        )
                    )
                    if invalid_role or tables.scalar() != 10 or not function.scalar():
                        return JSONResponse(
                            status_code=503,
                            content={"status": "not_ready", "provisioning": "misconfigured"},
                        )
                    if settings.delegation_enabled:
                        delegation_objects = await provisioner.scalar(
                            text(
                                "SELECT "
                                "to_regclass('service_delegation_grants') IS NOT NULL "
                                "AND to_regclass('service_delegation_tokens') IS NOT NULL "
                                "AND to_regclass('service_delegation_idempotency') IS NOT NULL "
                                "AND to_regclass('service_delegation_events') IS NOT NULL "
                                "AND to_regprocedure("
                                "'issue_service_delegation(uuid,text,text,text,text,bytea,"
                                "bytea,text,text,integer,text)') IS NOT NULL "
                                "AND to_regprocedure("
                                "'revoke_service_delegation(uuid,text,text,text,text,text,text)') "
                                "IS NOT NULL"
                            )
                        )
                        if not delegation_objects:
                            return JSONResponse(
                                status_code=503,
                                content={
                                    "status": "not_ready",
                                    "delegation": "misconfigured",
                                },
                            )
            except Exception:
                return JSONResponse(
                    status_code=503,
                    content={"status": "not_ready", "provisioning": "unavailable"},
                )

        return {
            "status": "ready",
            "database": "connected",
            "pgvector": pgvector_version,
        }
    except Exception as e:
        # Never return str(e): a driver/connection exception can embed a DSN,
        # hostname, username, or password. Only the categorical outcome and
        # the exception's type name (never its message) are safe to expose.
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "database": "unavailable",
                "error_type": type(e).__name__,
            },
        )


@router.get("/whoami", response_model=None, dependencies=[Depends(READ_SCOPE)])
async def whoami(
    principal: Principal = Depends(get_current_principal),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, object] | JSONResponse:
    """Return the caller's resolved principal and tenant.

    Lets API clients discover their own tenant_id from their API key,
    without needing to pass it as a query parameter or know it out of band.
    """
    principal_type = await session.scalar(
        select(PrincipalModel.type).where(
            PrincipalModel.id == UUID(principal.principal_id),
            PrincipalModel.tenant_id == UUID(principal.tenant_id),
        )
    )
    if principal_type is None:
        # Authentication already resolved this principal. Missing backing state
        # is therefore an integrity failure, never a caller-selectable fallback.
        return JSONResponse(
            status_code=503,
            content={"detail": "authenticated principal record unavailable"},
        )

    memory_profile: dict[str, object] | None = None
    if principal.memory_profile_id is not None:
        memory_profile = {
            "id": principal.memory_profile_id,
            "slug": principal.memory_profile_slug,
            "active_revision_id": principal.memory_profile_revision_id,
            "version": principal.memory_profile_version,
        }
    return {
        "principal_id": principal.principal_id,
        "principal_type": principal_type,
        "tenant_id": principal.tenant_id,
        "scopes": list(principal.scopes),
        "api_key_id": principal.api_key_id,
        "memory_profile": memory_profile,
    }
