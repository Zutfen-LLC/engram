"""Write-time conflict detection.

Checks new content against existing active items for semantic similarity,
then determines if the relationship is duplicate, refine, or contradict.

Skeleton — implementation in Phase 1B (T09).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID


async def detect_conflict(
    content: str,
    embedding: list[float] | None,
    tenant_id: UUID,
    workspace_id: UUID | None,
    kind: str,
) -> dict[str, Any] | None:
    """Check for conflicts against existing active items.

    Returns None if no conflict, or:
    {
        "type": "duplicate" | "refine" | "contradict",
        "existing_item_id": UUID,
        "similarity": float,
        "reason": str,
    }
    """
    # TODO (T09): implement similarity check + classifier determination
    raise NotImplementedError("conflict detection not yet implemented")
