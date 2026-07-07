"""Embedding management for memory_items."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from engram.config import settings
from engram.models import MemoryEmbedding

_EMBEDDING_MODEL = "text-embedding-3-small"


async def generate_embedding(text: str) -> list[float] | None:
    """Generate an embedding vector for ``text`` or return ``None`` when disabled."""
    if settings.embedding_provider == "none":
        return None
    if settings.embedding_provider != "openai":
        raise ValueError(f"unsupported embedding provider: {settings.embedding_provider!r}")

    try:
        from openai import AsyncOpenAI
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError(
            "openai package is required when ENGRAM_EMBEDDING_PROVIDER=openai"
        ) from exc

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    response = await client.embeddings.create(model=_EMBEDDING_MODEL, input=text)
    return [float(value) for value in response.data[0].embedding]


async def create_embedding_placeholder(
    session: AsyncSession,
    memory_item_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> MemoryEmbedding:
    """Insert a pending memory_embeddings row to be updated once the vector is ready."""
    placeholder = MemoryEmbedding(
        memory_item_id=memory_item_id,
        tenant_id=tenant_id,
        embedding_model=_EMBEDDING_MODEL,
        embedding_dim=settings.embedding_dim,
        embedding=None,
        embedding_status="pending",
    )
    session.add(placeholder)
    return placeholder
