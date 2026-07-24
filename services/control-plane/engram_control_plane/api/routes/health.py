"""Health (liveness) and readiness endpoints.

``GET /health`` is database-free liveness (ENG-PORTAL-001A). ``GET /ready``
verifies the dedicated Control Plane database is reachable and that its Alembic
revision is at head; it returns a sanitized 503 otherwise. Readiness reads
``alembic_version`` (the runtime role is granted SELECT on it) but never runs
migrations — migration execution is owner-only.

Explicit Pydantic response models give the OpenAPI contract named component
schemas, which the portal-sdk generated types derive strict TypeScript types
from.
"""

from __future__ import annotations

from typing import Protocol

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from engram_control_plane import __version__
from engram_control_plane.config import settings
from engram_control_plane.db import check_readiness
from engram_control_plane.security import sanitized_error

router = APIRouter()


class HealthResponse(BaseModel):
    """Database-free liveness response."""

    status: str
    service: str
    version: str
    build_sha: str


class ReadyResponse(BaseModel):
    """Readiness response (200 path)."""

    status: str
    database: str
    migration: str


def _health_body() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service="engram-control-plane",
        version=__version__,
        build_sha=settings.build_sha,
    )


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness probe — database-free."""
    return _health_body()


class _AsyncSessionCM(Protocol):
    """Minimal async context-manager shape a ready session must satisfy."""

    async def __aenter__(self) -> AsyncSession: ...

    async def __aexit__(self, *exc: object) -> None: ...


def _ready_session() -> _AsyncSessionCM:
    """Resolve an async session context manager, or raise if no DB configured.

    Returns the session produced by the async session factory (an async context
    manager). Kept as a plain function so tests can substitute a context manager
    without awaiting the factory call.
    """
    from engram_control_plane.db import get_session_factory

    return get_session_factory()()  # type: ignore[return-value]


@router.get("/ready", response_model=ReadyResponse)
async def ready(request: Request) -> ReadyResponse | JSONResponse:
    """Readiness probe — dedicated DB reachable + Alembic at head.

    Returns 200 only when the Control Plane database is reachable and its
    Alembic revision is at head; otherwise a sanitized 503.
    """
    if not settings.database_url:
        # No DB configured (dev/scaffold). Honest status, not a crash.
        return sanitized_error(
            request,
            status_code=503,
            error="not_ready",
            detail="database_not_configured",
        )
    try:
        async with _ready_session() as session:
            result = await check_readiness(session)
    except Exception:
        # Bound the failure log: log type only, never the connection error text
        # (which may include credentials). The full traceback goes to logs via
        # the exception handler, scrubbed by the redaction filter.
        import logging

        logging.getLogger("engram-control-plane").warning(
            "ready_failed error_type=%s", "DatabaseConnectionError"
        )
        return sanitized_error(
            request,
            status_code=503,
            error="not_ready",
            detail="database_unreachable",
        )

    if result.get("migration") != "at_head":
        return sanitized_error(
            request,
            status_code=503,
            error="not_ready",
            detail=f"migration_{result.get('migration')}",
        )

    return ReadyResponse(
        status="ready",
        database=result["database"],
        migration=result["migration"],
    )
