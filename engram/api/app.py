"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Startup: could warm connection pool, check DB readiness
    yield
    # Shutdown: clean up resources


def create_app() -> FastAPI:
    app = FastAPI(
        title="Engram",
        description="Shared structured memory for multi-agent AI teams",
        version="0.1.0",
        lifespan=lifespan,
    )

    from engram.api.routes import classify, diary, export, health, kg, memory, review, taxonomy

    app.include_router(health.router, tags=["health"])
    app.include_router(memory.router, prefix="/v1", tags=["memory"])
    app.include_router(classify.router, prefix="/v1", tags=["classification"])
    app.include_router(review.router, prefix="/v1", tags=["review"])
    app.include_router(kg.router, prefix="/v1", tags=["knowledge-graph"])
    app.include_router(taxonomy.router, prefix="/v1", tags=["taxonomy"])
    app.include_router(diary.router, prefix="/v1", tags=["diary"])
    app.include_router(export.router, prefix="/v1", tags=["export"])

    return app


app = create_app()
