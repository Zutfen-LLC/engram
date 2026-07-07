"""Health and readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from engram.db import get_session

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}


@router.get("/ready", response_model=None)
async def readiness(
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, str] | JSONResponse:
    """Readiness probe — checks DB connectivity AND RLS context.

    get_session sets the RLS context (app.tenant_id / app.principal_id) before
    yielding, so a successful execute here implies both DB connectivity and a
    resolvable RLS context. We return 503 if either fails.
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
        return {"status": "ready", "database": "connected"}
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "database": str(e)},
        )
