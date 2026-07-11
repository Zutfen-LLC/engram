"""Health and readiness endpoints."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from engram.auth import HEALTH_EXEMPT_SCOPE, READY_EXEMPT_SCOPE
from engram.db import get_session

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

        return {
            "status": "ready",
            "database": "connected",
            "pgvector": pgvector_version,
        }
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "database": str(e)},
        )
