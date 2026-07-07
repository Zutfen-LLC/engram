"""Write-time conflict detection.

Checks new content against existing active items for semantic similarity,
then determines if the relationship is duplicate, refine, or contradict.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any
from uuid import UUID

from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from sqlalchemy import and_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from engram.config import settings
from engram.models import MemoryEmbedding, MemoryItem

# Cosine similarity above this triggers conflict classification.
_SIMILARITY_THRESHOLD = 0.85
_EMBEDDING_MODEL = "text-embedding-3-small"

# source_trust at or above this counts as "high authority" for auto-supersession.
_HIGH_SOURCE_TRUST = 0.8
# Classifier confidence at or above this permits auto-supersession.
_HIGH_CLASSIFIER_CONFIDENCE = 0.8


class ConflictVerdict(StrEnum):
    DUPLICATE = "duplicate"
    REFINE = "refine"
    CONTRADICT = "contradict"


class ConflictAction(StrEnum):
    """What the caller should do in response to a detected conflict."""

    DEDUP = "dedup"
    AUTO_SUPERSEDE = "auto_supersede"
    PROPOSED_SUPERSEDE = "proposed_supersede"
    FLAG_CONTRADICTION = "flag_contradiction"
    FLAG_SCOPE_OVERLAP = "flag_scope_overlap"


class ConflictResult(BaseModel):
    """Outcome of checking a new item against existing active items."""

    verdict: ConflictVerdict
    action: ConflictAction
    existing_item_id: UUID
    similarity: float
    classifier_confidence: float
    conflict_type: str | None
    reason: str
    provenance: dict[str, Any] = Field(default_factory=dict)


_VERDICT_MAP: dict[str, ConflictVerdict] = {
    "duplicate": ConflictVerdict.DUPLICATE,
    "refine": ConflictVerdict.REFINE,
    "contradict": ConflictVerdict.CONTRADICT,
}


async def detect_conflicts(
    new_item: MemoryItem,
    session: AsyncSession,
) -> ConflictResult | None:
    """Check ``new_item`` against active items in scope for semantic conflicts.

    Scope = same (tenant, workspace, kind). Tenant scoping is enforced by RLS;
    workspace and kind are filtered explicitly. Returns ``None`` when no similar
    active item is found above the similarity threshold, or when the new item
    has no usable embedding.
    """
    # 1. Load the new item's embedding vector.
    query_embedding = (
        await session.execute(
            select(MemoryEmbedding.embedding).where(
                MemoryEmbedding.memory_item_id == new_item.id,
                MemoryEmbedding.embedding_model == _EMBEDDING_MODEL,
                MemoryEmbedding.embedding.is_not(None),
            )
        )
    ).scalar_one_or_none()

    if query_embedding is None:
        return None

    # 2. Find the nearest active item in scope (excluding self).
    await session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
    distance = MemoryEmbedding.embedding.cosine_distance(query_embedding)
    stmt = (
        select(
            MemoryItem.id.label("id"),
            MemoryItem.content.label("content"),
            MemoryItem.source_trust.label("source_trust"),
            distance.label("distance"),
        )
        .select_from(MemoryEmbedding)
        .join(
            MemoryItem,
            and_(
                MemoryItem.id == MemoryEmbedding.memory_item_id,
                MemoryItem.tenant_id == MemoryEmbedding.tenant_id,
            ),
        )
        .where(
            MemoryItem.kind == new_item.kind,
            MemoryItem.review_status == "active",
            MemoryItem.valid_to.is_(None),
            MemoryItem.id != new_item.id,
            MemoryEmbedding.embedding_model == _EMBEDDING_MODEL,
            MemoryEmbedding.embedding.is_not(None),
        )
    )
    if new_item.workspace_id is None:
        stmt = stmt.where(MemoryItem.workspace_id.is_(None))
    else:
        stmt = stmt.where(MemoryItem.workspace_id == new_item.workspace_id)
    stmt = stmt.order_by(distance.asc()).limit(1)

    row = (await session.execute(stmt)).mappings().one_or_none()
    if row is None:
        return None

    similarity = 1.0 - float(row["distance"] or 0.0)
    if similarity <= _SIMILARITY_THRESHOLD:
        return None

    # 3. Classify the relationship (duplicate / refine / contradict).
    existing_trust = float(row["source_trust"])
    verdict, confidence, reason, provenance = await _classify_relationship(
        old_content=str(row["content"]),
        new_content=new_item.content,
        similarity=similarity,
    )

    # 4. Resolve the action based on verdict + authority hierarchy.
    action, conflict_type = _resolve_action(
        verdict=verdict,
        new_trust=new_item.source_trust,
        old_trust=existing_trust,
        classifier_confidence=confidence,
    )

    return ConflictResult(
        verdict=verdict,
        action=action,
        existing_item_id=UUID(str(row["id"])),
        similarity=similarity,
        classifier_confidence=confidence,
        conflict_type=conflict_type,
        reason=reason,
        provenance=provenance,
    )


def _resolve_action(
    verdict: ConflictVerdict,
    new_trust: float,
    old_trust: float,
    classifier_confidence: float,
) -> tuple[ConflictAction, str | None]:
    """Map a verdict + authority comparison to an action and conflict_type."""
    if verdict is ConflictVerdict.DUPLICATE:
        return ConflictAction.DEDUP, "duplicate"

    if verdict is ConflictVerdict.CONTRADICT:
        return ConflictAction.FLAG_CONTRADICTION, "contradiction"

    # refine — conditional supersession based on authority hierarchy.
    if new_trust < old_trust:
        return ConflictAction.FLAG_SCOPE_OVERLAP, "scope_overlap"

    if new_trust >= _HIGH_SOURCE_TRUST and classifier_confidence >= _HIGH_CLASSIFIER_CONFIDENCE:
        return ConflictAction.AUTO_SUPERSEDE, None

    return ConflictAction.PROPOSED_SUPERSEDE, "stale"


async def _classify_relationship(
    old_content: str,
    new_content: str,
    similarity: float,
) -> tuple[ConflictVerdict, float, str, dict[str, Any]]:
    """Classify how new content relates to existing content."""
    if settings.classification_provider != "openai":
        return _classify_relationship_fallback(similarity)
    try:
        return await _classify_relationship_llm(old_content, new_content, similarity)
    except Exception as exc:  # pragma: no cover - defensive fallback
        verdict, confidence, reason, _ = _classify_relationship_fallback(similarity)
        provenance = {"provider": "openai", "mode": "fallback", "error": str(exc)}
        return verdict, confidence, reason, provenance


def _classify_relationship_fallback(
    similarity: float,
) -> tuple[ConflictVerdict, float, str, dict[str, Any]]:
    """Heuristic fallback when no LLM is available.

    Without an LLM we cannot reliably distinguish refine from contradict.
    Near-identical embeddings (>0.97) are treated as duplicates; everything
    else defaults to refine with low confidence (never auto-supersedes).
    """
    provenance: dict[str, Any] = {
        "provider": settings.classification_provider or "none",
        "mode": "heuristic",
    }
    if similarity >= 0.97:
        return (
            ConflictVerdict.DUPLICATE,
            0.9,
            "Near-identical embeddings (heuristic).",
            provenance,
        )
    return (
        ConflictVerdict.REFINE,
        0.5,
        "Semantically similar; cannot refine/contradict without LLM, defaulting to refine.",
        provenance,
    )


async def _classify_relationship_llm(
    old_content: str,
    new_content: str,
    similarity: float,
) -> tuple[ConflictVerdict, float, str, dict[str, Any]]:
    """LLM-backed classification of the relationship between two items."""
    prompt = json.dumps(
        {
            "task": (
                "Compare two memory items. Does the new content duplicate, "
                "refine, or contradict the existing content?"
            ),
            "definitions": {
                "duplicate": "Same information, possibly reworded.",
                "refine": "Updates, extends, or corrects the existing content.",
                "contradict": "Directly conflicts with the existing content.",
            },
            "existing_content": old_content,
            "new_content": new_content,
            "semantic_similarity": round(similarity, 4),
            "output_schema": {
                "verdict": "duplicate | refine | contradict",
                "confidence": "number 0.0-1.0",
                "reason": "str",
            },
            "constraints": ["Return valid JSON only."],
        },
        ensure_ascii=False,
        indent=2,
    )

    client = (
        AsyncOpenAI()
        if settings.openai_api_key is None
        else AsyncOpenAI(api_key=settings.openai_api_key)
    )
    response = await client.chat.completions.create(
        model=settings.classification_model,
        messages=[
            {"role": "system", "content": "Return only valid JSON."},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
    )
    message = response.choices[0].message.content or "{}"
    payload = json.loads(message)
    if not isinstance(payload, dict):
        raise ValueError("conflict classification response was not a JSON object")

    verdict = _parse_verdict(payload.get("verdict"))
    try:
        confidence = float(payload.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))
    reason = str(payload.get("reason") or "").strip() or f"LLM verdict: {verdict.value}"

    provenance: dict[str, Any] = {
        "provider": "openai",
        "mode": "llm",
        "model": settings.classification_model,
        "llm_payload": payload,
    }
    return verdict, confidence, reason, provenance


def _parse_verdict(value: Any) -> ConflictVerdict:
    """Parse the LLM's verdict string, defaulting to refine on ambiguity."""
    normalized = str(value or "").strip().lower()
    return _VERDICT_MAP.get(normalized, ConflictVerdict.REFINE)
