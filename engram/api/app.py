"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import DataError, IntegrityError

from engram.api.errors import data_error_handler, integrity_error_handler
from engram.api.service_boundary import (
    ReviewDelegationRequestMiddleware,
    SensitiveResponseBoundaryMiddleware,
    service_request_validation_handler,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Startup: validate embedding config if enabled.
    from engram.config import settings

    if settings.embedding_provider != "none":
        if not settings.openai_api_key:
            import logging

            logging.getLogger("engram").warning(
                "ENGRAM_EMBEDDING_PROVIDER is '%s' but ENGRAM_OPENAI_API_KEY is not set. "
                "Semantic search will fail silently. Run 'engram setup-embeddings' to diagnose.",
                settings.embedding_provider,
            )
        elif not settings.openai_base_url:
            import logging

            logging.getLogger("engram").warning(
                "ENGRAM_EMBEDDING_PROVIDER is '%s' but ENGRAM_OPENAI_BASE_URL is not set. "
                "The OpenAI SDK will default to api.openai.com — if you are using "
                "OpenRouter, DeepInfra, or another provider, semantic search will fail with 401. "
                "Run 'engram setup-embeddings' to diagnose.",
                settings.embedding_provider,
            )
    yield
    # Shutdown: clean up resources
    from engram.db import provisioner_engine

    if provisioner_engine is not None:
        await provisioner_engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Engram",
        description="Shared structured memory for multi-agent AI teams",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_exception_handler(IntegrityError, integrity_error_handler)
    app.add_exception_handler(DataError, data_error_handler)
    app.add_exception_handler(RequestValidationError, service_request_validation_handler)
    app.add_middleware(ReviewDelegationRequestMiddleware)
    app.add_middleware(SensitiveResponseBoundaryMiddleware)

    from engram.api.routes import (
        admin,
        agents,
        classify,
        context_receipts,
        diary,
        export,
        extract,
        health,
        kg,
        memory,
        memory_profiles,
        portal_enrollment,
        review,
        service_delegation,
        service_provisioning,
        service_review_delegation,
        taxonomy,
        telemetry,
    )

    app.include_router(health.router, tags=["health"])
    app.include_router(memory.router, prefix="/v1", tags=["memory"])
    app.include_router(agents.router, prefix="/v1", tags=["agents"])
    app.include_router(memory_profiles.router, prefix="/v1", tags=["memory-profiles"])
    app.include_router(extract.router, prefix="/v1", tags=["extraction"])
    app.include_router(classify.router, prefix="/v1", tags=["classification"])
    app.include_router(review.router, prefix="/v1", tags=["review"])
    app.include_router(kg.router, prefix="/v1", tags=["knowledge-graph"])
    app.include_router(taxonomy.router, prefix="/v1", tags=["taxonomy"])
    app.include_router(diary.router, prefix="/v1", tags=["diary"])
    app.include_router(context_receipts.router, prefix="/v1", tags=["context-receipts"])
    app.include_router(export.router, prefix="/v1", tags=["export"])
    app.include_router(admin.router, prefix="/v1", tags=["admin"])
    app.include_router(telemetry.router, prefix="/v1", tags=["telemetry"])
    app.include_router(service_provisioning.router, prefix="/v1", tags=["service-provisioning"])
    app.include_router(portal_enrollment.router, prefix="/v1", tags=["portal-enrollment"])
    app.include_router(service_delegation.router, prefix="/v1", tags=["service-delegation"])
    app.include_router(
        service_review_delegation.router,
        prefix="/v1",
        tags=["service-review-delegation"],
    )

    # V2-BL-004: every caller-facing route must declare an explicit scope
    # policy (or be marked exempt). Validated eagerly here so a route added
    # without one fails at import/startup time, not silently in production.
    from engram.api.scope_policy import build_custom_openapi, validate_scope_policy_completeness

    validate_scope_policy_completeness(app)
    app.openapi = lambda: build_custom_openapi(app)  # type: ignore[method-assign]

    return app


app = create_app()
