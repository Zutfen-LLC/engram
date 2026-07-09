"""Write-time conflict detection.

Checks new content against existing active items for semantic similarity,
then determines if the relationship is duplicate, refine, or contradict.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from engram.config import settings
from engram.embeddings import EMBEDDING_MODEL as _EMBEDDING_MODEL
from engram.models import MemoryEmbedding, MemoryItem

# Cosine similarity above this triggers conflict classification.
_SIMILARITY_THRESHOLD = 0.85

# Default top-k plausible candidates considered by the promotion-time conflict
# recheck (engram.promotion). Bounded so a recheck never scans the full corpus.
_PROMOTION_CANDIDATE_K = 5

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


def authority_allows_supersession(*, new_trust: float, old_trust: float) -> bool:
    """Whether a new item's source authority may supersede an existing one.

    Authority is derived from ``source_trust`` (design §4: explicit_user >
    trusted_import > trusted_agent > untrusted_agent > inferred). Equal-or-higher
    authority may supersede; strictly lower authority may not — a lower-authority
    source must never silently replace a higher-authority memory. This is the
    single canonical comparison used by both write-time conflict resolution
    (:func:`_resolve_action`) and explicit supersession
    (``POST /items/{id}/supersede``).
    """
    return new_trust >= old_trust


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
    if not authority_allows_supersession(new_trust=new_trust, old_trust=old_trust):
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


# ---- Promotion-time conflict recheck (top-k candidates, ENG-AUD-007) -------
#
# Write-time detect_conflicts() above only ever looks at the single nearest
# same-kind/workspace embedding. That is too narrow for a promotion-time
# recheck: the nearest neighbour by embedding distance is not always the item
# that actually conflicts (e.g. two items can be highly similar in general
# phrasing but about different subjects, while the *actual* duplicate/refine
# candidate is second or third nearest). The functions below fetch up to `k`
# plausible candidates instead of just one, and fall back to a structural
# heuristic (subject/kind matching) when the item has no embedding at all —
# so disabling embeddings does not silently disable conflict checking.


@dataclass
class ConflictCandidate:
    """One plausible active-item conflict candidate for a promotion recheck."""

    id: UUID
    content: str
    source_trust: float
    content_hash: str
    # Cosine similarity when found via embeddings; None for heuristic matches
    # (no embedding-based score is available in that mode).
    similarity: float | None


@dataclass
class PromotionConflictCheck:
    """Result of a blocked promotion-time conflict recheck."""

    conflicting_item_id: UUID
    verdict: str
    reason: str
    used_embeddings: bool


async def find_promotion_conflict_candidates(
    session: AsyncSession,
    item: MemoryItem,
    *,
    k: int | None = None,
) -> list[ConflictCandidate]:
    """Top-k plausible active-item conflict candidates for ``item``.

    Scope mirrors write-time ``detect_conflicts()``: same tenant (RLS-enforced
    by the caller's session), same ``kind``, and the same workspace when
    ``item`` is workspace-scoped — a tenant/public-scoped item (``workspace_id
    IS NULL``) is compared tenant-wide rather than narrowed to one workspace,
    so a tenant-wide conflict is never missed.

    Prefers embedding cosine similarity, ordered nearest-first, when ``item``
    has a stored embedding. When it does not (``embedding_provider='none'`` or
    the row predates embedding generation), falls back to a conservative
    structural heuristic: active items in scope that share ``subject_type`` +
    ``subject_id`` or share ``subject_name``, and whose ``content_hash``
    differs from ``item``'s (an exact-content match is never a conflict). The
    heuristic has no notion of semantic similarity — it will miss conflicts
    that don't share an explicit subject field and can never distinguish
    "compatible detail" from "disputed claim"; callers must treat any
    heuristic match conservatively (see ``check_promotion_conflict``).

    Always bounded to at most ``k`` rows (default
    ``settings.promotion_conflict_candidate_k``, 5) so a recheck never scans
    the full corpus.
    """
    resolved_k = k if k is not None else settings.promotion_conflict_candidate_k

    query_embedding = (
        await session.execute(
            select(MemoryEmbedding.embedding).where(
                MemoryEmbedding.memory_item_id == item.id,
                MemoryEmbedding.embedding_model == _EMBEDDING_MODEL,
                MemoryEmbedding.embedding.is_not(None),
            )
        )
    ).scalar_one_or_none()

    if query_embedding is not None:
        return await _find_candidates_by_embedding(session, item, query_embedding, resolved_k)
    return await _find_candidates_by_heuristic(session, item, resolved_k)


async def _find_candidates_by_embedding(
    session: AsyncSession,
    item: MemoryItem,
    query_embedding: Any,
    k: int,
) -> list[ConflictCandidate]:
    await session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
    distance = MemoryEmbedding.embedding.cosine_distance(query_embedding)
    stmt = (
        select(
            MemoryItem.id.label("id"),
            MemoryItem.content.label("content"),
            MemoryItem.source_trust.label("source_trust"),
            MemoryItem.content_hash.label("content_hash"),
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
            MemoryItem.kind == item.kind,
            MemoryItem.review_status == "active",
            MemoryItem.valid_to.is_(None),
            MemoryItem.id != item.id,
            MemoryEmbedding.embedding_model == _EMBEDDING_MODEL,
            MemoryEmbedding.embedding.is_not(None),
        )
    )
    if item.workspace_id is None:
        stmt = stmt.where(MemoryItem.workspace_id.is_(None))
    else:
        stmt = stmt.where(MemoryItem.workspace_id == item.workspace_id)
    stmt = stmt.order_by(distance.asc()).limit(k)

    rows = (await session.execute(stmt)).mappings().all()
    candidates: list[ConflictCandidate] = []
    for row in rows:
        similarity = 1.0 - float(row["distance"] or 0.0)
        if similarity <= _SIMILARITY_THRESHOLD:
            continue
        candidates.append(
            ConflictCandidate(
                id=UUID(str(row["id"])),
                content=str(row["content"]),
                source_trust=float(row["source_trust"]),
                content_hash=str(row["content_hash"]),
                similarity=similarity,
            )
        )
    return candidates


async def _find_candidates_by_heuristic(
    session: AsyncSession,
    item: MemoryItem,
    k: int,
) -> list[ConflictCandidate]:
    """Non-embedding fallback candidate search (see docstring above)."""
    subject_clauses = []
    if item.subject_type is not None and item.subject_id is not None:
        subject_clauses.append(
            and_(
                MemoryItem.subject_type == item.subject_type,
                MemoryItem.subject_id == item.subject_id,
            )
        )
    if item.subject_name is not None:
        subject_clauses.append(MemoryItem.subject_name == item.subject_name)

    if not subject_clauses:
        # No subject fields to match against — the heuristic has nothing
        # reliable to key on, so it returns no candidates rather than
        # guessing from kind alone (which would be too noisy).
        return []

    stmt = (
        select(
            MemoryItem.id.label("id"),
            MemoryItem.content.label("content"),
            MemoryItem.source_trust.label("source_trust"),
            MemoryItem.content_hash.label("content_hash"),
        )
        .where(
            MemoryItem.tenant_id == item.tenant_id,
            MemoryItem.kind == item.kind,
            MemoryItem.review_status == "active",
            MemoryItem.valid_to.is_(None),
            MemoryItem.id != item.id,
            or_(*subject_clauses),
        )
    )
    if item.workspace_id is None:
        stmt = stmt.where(MemoryItem.workspace_id.is_(None))
    else:
        stmt = stmt.where(MemoryItem.workspace_id == item.workspace_id)
    stmt = stmt.order_by(MemoryItem.created_at.desc()).limit(k)

    rows = (await session.execute(stmt)).mappings().all()
    return [
        ConflictCandidate(
            id=UUID(str(row["id"])),
            content=str(row["content"]),
            source_trust=float(row["source_trust"]),
            content_hash=str(row["content_hash"]),
            similarity=None,
        )
        for row in rows
        if str(row["content_hash"]) != item.content_hash
    ]


async def check_promotion_conflict(
    session: AsyncSession,
    item: MemoryItem,
    *,
    k: int | None = None,
) -> PromotionConflictCheck | None:
    """Conservative promotion-time conflict recheck (design.md §3 Path A).

    Re-runs conflict detection against up to ``k`` plausible active-item
    candidates (:func:`find_promotion_conflict_candidates`) instead of relying
    solely on ``conflict_resolution_status`` as it stood at write time — a
    memory can go from "no conflict" to "conflicts with a later write" without
    ever being touched again itself.

    Embedding-backed candidates are classified the same way write-time
    detection is (duplicate / refine / contradict): a ``CONTRADICT`` verdict,
    or a ``REFINE`` the candidate item's new content may not out-rank by
    authority (:func:`authority_allows_supersession`), blocks promotion.
    ``DUPLICATE`` and authority-eligible ``REFINE`` are not promotion blockers
    (the item can safely supersede/coexist once active).

    Heuristic (non-embedding) candidates are always treated as a block. There
    is no LLM or embedding signal to tell "same subject, compatible detail"
    apart from "same subject, disputed claim" in that mode, so — per this
    slice's conservative design — any structural match withholds promotion
    rather than risk auto-promoting a real conflict.

    Returns ``None`` when no candidate blocks promotion.
    """
    candidates = await find_promotion_conflict_candidates(session, item, k=k)
    for candidate in candidates:
        if candidate.similarity is not None:
            verdict, _confidence, reason, _provenance = await _classify_relationship(
                old_content=candidate.content,
                new_content=item.content,
                similarity=candidate.similarity,
            )
            if verdict is ConflictVerdict.CONTRADICT:
                return PromotionConflictCheck(candidate.id, verdict.value, reason, True)
            if verdict is ConflictVerdict.REFINE and not authority_allows_supersession(
                new_trust=item.source_trust, old_trust=candidate.source_trust
            ):
                return PromotionConflictCheck(candidate.id, verdict.value, reason, True)
            # DUPLICATE, or authority-eligible REFINE: not a promotion blocker.
        else:
            return PromotionConflictCheck(
                candidate.id,
                "structural_conflict",
                "non-embedding fallback: same subject/kind with differing content",
                False,
            )
    return None
