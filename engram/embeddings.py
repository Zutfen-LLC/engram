"""Embedding management — placeholder creation deferred to T05.

In Phase 1A the embedding provider is ``none``; we insert a pending row so
T05 can pick it up and backfill the vector asynchronously.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from engram.config import settings
from engram.models import MemoryEmbedding

# Model name used for placeholder rows. T05 will replace this with the real
# provider's model identifier when it backfills the vector.
_PLACEHOLDER_MODEL = "pending"


async def create_embedding_placeholder(
    session: AsyncSession,
    memory_item_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> MemoryEmbedding:
    """Insert a ``memory_embeddings`` row with ``embedding_status='pending'``.

    The vector column stays NULL — T05 fills it via the configured provider.
    """
    placeholder = MemoryEmbedding(
        memory_item_id=memory_item_id,
        tenant_id=tenant_id,
        embedding_model=_PLACEHOLDER_MODEL,
        embedding_dim=settings.embedding_dim,
        embedding=None,
        embedding_status="pending",
    )
    session.add(placeholder)
    return placeholder