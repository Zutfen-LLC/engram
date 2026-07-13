"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.exc import DataError, IntegrityError

from engram.api.errors import data_error_handler, integrity_error_handler


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


def create_app() -> FastAPI:
    app = FastAPI(
        title="Engram",
        description="Shared structured memory for multi-agent AI teams",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_exception_handler(IntegrityError, integrity_error_handler)
    app.add_exception_handler(DataError, data_error_handler)

    from engram.api.routes import (
        admin,
        agents,
        classify,
        diary,
        export,
        health,
        kg,
        memory,
        review,
        taxonomy,
    )

    app.include_router(health.router, tags=["health"])
    app.include_router(memory.router, prefix="/v1", tags=["memory"])
    app.include_router(agents.router, prefix="/v1", tags=["agents"])
    app.include_router(classify.router, prefix="/v1", tags=["classification"])
    app.include_router(review.router, prefix="/v1", tags=["review"])
    app.include_router(kg.router, prefix="/v1", tags=["knowledge-graph"])
    app.include_router(taxonomy.router, prefix="/v1", tags=["taxonomy"])
    app.include_router(diary.router, prefix="/v1", tags=["diary"])
    app.include_router(export.router, prefix="/v1", tags=["export"])
    app.include_router(admin.router, prefix="/v1", tags=["admin"])

    # V2-BL-004: every caller-facing route must declare an explicit scope
    # policy (or be marked exempt). Validated eagerly here so a route added
    # without one fails at import/startup time, not silently in production.
    from engram.api.scope_policy import build_custom_openapi, validate_scope_policy_completeness

    validate_scope_policy_completeness(app)
    app.openapi = lambda: build_custom_openapi(app)  # type: ignore[method-assign]

    return app


app = create_app()
