"""FastAPI application factory for the Control Plane.

Importing this module (and ``app``) is database-free and secret-free
(ENG-PORTAL-001A, alteration 6). Production-required configuration validation
runs at startup via the lifespan and from the CLI ``serve`` command — never at
import.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from engram_control_plane import __version__
from engram_control_plane.logging_config import RequestIdMiddleware, configure_logging
from engram_control_plane.security import unhandled_exception_handler


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from engram_control_plane.config import settings, validate_for_runtime

    # Configure logging first so any validation error is itself logged cleanly.
    configure_logging(settings.log_level)

    # Production-required validation at startup, not import.
    validate_for_runtime(settings)

    yield

    # Shutdown: dispose the runtime engine if one was created.
    from engram_control_plane.db import dispose_engine

    await dispose_engine()


def create_app() -> FastAPI:
    """Build the Control Plane FastAPI application.

    No database connection is opened and no secrets are required to build/import
    the app, so ``export-openapi`` and unit tests stay hermetic.
    """
    app = FastAPI(
        title="Engram Control Plane",
        description=(
            "Hosted SaaS control-plane boundary. This is scaffolding "
            "(ENG-PORTAL-001A): no production identity, billing, delegation, "
            "organization, or management behavior is implemented."
        ),
        version=__version__,
        lifespan=lifespan,
    )

    # Middleware: request id + structured access logging. No CORS is configured
    # (no wildcard origin); the Portal calls server-side and does not need CORS.
    app.add_middleware(RequestIdMiddleware)

    # Sanitized last-resort exception handler.
    app.add_exception_handler(Exception, unhandled_exception_handler)

    from engram_control_plane.api.routes import health, meta

    app.include_router(health.router, tags=["health"])
    app.include_router(meta.router, tags=["meta"])

    # Deterministic OpenAPI as the single source of truth for portal-sdk.
    from engram_control_plane.api.openapi import build_openapi

    app.openapi = lambda: build_openapi(app)  # type: ignore[method-assign]
    return app


app = create_app()
