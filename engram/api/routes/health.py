"""Health and readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from engram.db import get_session

router = APIRouter()


@router.get("/health")
async def health():
    """Liveness probe."""
    return {"status": "ok"}


@router.get("/ready")
async def readiness(session: AsyncSession = Depends(get_session)):
    """Readiness probe — checks DB connectivity."""
    try:
        result = await session.execute(text("SELECT 1"))
        result.scalar()
        return {"status": "ready", "database": "connected"}
    except Exception as e:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "database": str(e)},
        )
