"""Export endpoints — CCA ledger projection to git-friendly JSON.

GET /v1/export/cca renders active doctrine/decision/invariant/preference items
in the cca_lite_memory_packet@v1 format, with Engram trust fields attached so
the exported JSON is reviewable like code in a git repo.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from engram.auth import EXPORT_SCOPE
from engram.authority import authority_label
from engram.canonicalize import canonicalize
from engram.db import get_session

router = APIRouter()

# Kinds included in the CCA ledger projection (mirrors the cca_ledger DB view).
_CCA_KINDS = ("doctrine", "decision", "invariant", "preference")


@router.get("/export/cca", response_model=None, dependencies=[Depends(EXPORT_SCOPE)])
async def export_cca(
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    """Export the CCA ledger as git-friendly JSON (cca_lite_memory_packet@v1).

    Selects active items (``valid_to IS NULL``) with ``kind IN (doctrine,
    decision, invariant, preference)`` and renders them in the CCA lite
    memory-packet format, enriched with Engram trust fields.
    """
    kind_list = ", ".join(f"'{k}'" for k in _CCA_KINDS)
    sql = text(
        "SELECT id, kind, content, content_hash, "
        "source_type, source_session, wing, room, "
        "review_status, memory_confidence, source_trust, authority, human_verified, "
        "valid_from "
        f"FROM memory_items WHERE kind IN ({kind_list}) AND valid_to IS NULL "
        "ORDER BY valid_from ASC"
    )
    result = await session.execute(sql)
    entries: list[dict[str, Any]] = []
    for row in result.mappings().all():
        content = row["content"]
        entries.append(
            {
                "id": str(row["id"]),
                "kind": row["kind"],
                "text": content,
                "source": row["source_type"],
                "session_id": row["source_session"] or "",
                "captured_at": _iso(row["valid_from"]),
                "canonical_text": canonicalize(content),
                "content_hash": row["content_hash"],
                # Trust fields (Engram extension over baseline CCA packet)
                "review_status": row["review_status"],
                "memory_confidence": row["memory_confidence"],
                "source_trust": row["source_trust"],
                "authority": row["authority"],
                "authority_label": authority_label(int(row["authority"])),
                "human_verified": bool(row["human_verified"]),
                # Taxonomy
                "wing": row["wing"],
                "room": row["room"],
            }
        )

    return {
        "kind": "cca_lite_memory_packet@v1",
        "meta": {
            "source": "engram",
            "as_of": datetime.now(UTC).isoformat(),
            "entry_count": len(entries),
        },
        "entries": entries,
    }


def _iso(value: Any) -> str:
    """Render a datetime or string as ISO-8601."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
