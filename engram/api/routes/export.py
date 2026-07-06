"""Export endpoints — CCA ledger projection to git-friendly JSON.

Skeleton — implementation in Phase 1.
"""

from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter

router = APIRouter()


@router.get("/export/cca", response_model=None)
async def export_cca() -> NoReturn:
    """Export CCA ledger (memory_items with doctrine/decision/invariant/preference kind).

    Returns a JSON packet compatible with the Zutfen CCA ledger format,
    suitable for committing to a git repo for human review.
    """
    raise NotImplementedError
