"""Relationship-aware recall: bounded graph + tunnel expansion (ENG-AUD-012 / F19).

Semantic recall finds relevant memories; this module reconstructs the context
*surrounding* them, using two existing structures:

  * ``memory_edges`` — typed, directed, depth-1 relationships between memory
    items (derived_from, references, explains, contradicts, supports,
    depends_on, mentions).
  * ``tunnels`` — cross-wing/room links (see engram.models.Tunnel); a memory's
    "tunnel membership" is any tunnel whose source/target (wing, room)
    coordinates match the memory's own (wing, room).

Pipeline (called from engram.recall.execute_semantic_recall, between
semantic.search() and budget packing):

    semantic candidates (already scored, sorted desc)
        -> bounded seed selection (recall_semantic_expansion_seed_limit)
        -> graph expansion (depth 1, bounded, deterministic)
        -> tunnel expansion (bounded, deterministic)
        -> merge (dedupe by id, track origin + relationship metadata)
        -> relationship-aware rescoring
        -> ceiling truncation (recall_candidate_ceiling)

Every expanded candidate is re-filtered through the exact same trust
predicate semantic recall itself uses (tenant + read_eligibility_expression +
active/proposed review status + optional workspace scope) — expansion is
never an eligibility bypass. No recursive traversal: graph/tunnel neighbors
are found only for the original semantic seeds, never for neighbors of
neighbors.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from engram.config import settings
from engram.memory_access import read_eligibility_expression
from engram.memory_context import ResolvedMemoryContext
from engram.models import MemoryEdge, MemoryItem, Tunnel

# Recall-pipeline scoring version (distinct from engram.semantic.SEMANTIC_SCORING_VERSION,
# which covers /v1/search's own ranking, unaffected by this module). Bumped
# because execute_semantic_recall's final per-item score now blends
# relationship/tunnel/importance bonuses on top of the semantic score
# (requirement 6) — auditable via recall_logs.scoring_version.
RECALL_SCORING_VERSION = "semantic-v3"

# Static edge_type -> strength mapping (requirement 9), used when an edge row
# doesn't carry its own ``weight``. If the graph model gains real per-edge
# weights later, MemoryEdge.weight already takes precedence over this table.
EDGE_TYPE_WEIGHTS: dict[str, float] = {
    "derived_from": 0.9,  # strong
    "references": 0.6,  # medium
    "explains": 0.6,  # medium
    "supports": 0.6,  # medium
    "contradicts": 0.6,  # medium
    "depends_on": 0.6,  # medium
    "mentions": 0.3,  # weak
}
_DEFAULT_EDGE_WEIGHT = 0.5

# Review statuses eligible for expansion — identical to semantic recall's own
# gate (engram.recall._SEMANTIC_REVIEW_STATUSES). Disputed items follow the
# same governance as direct semantic recall: they are simply not included.
_EXPANSION_REVIEW_STATUSES = ("active", "proposed")


def effective_edge_weight(edge_type: str, weight: float | None) -> float:
    """Resolve an edge's relationship strength: stored weight, else static map."""
    if weight is not None:
        return weight
    return EDGE_TYPE_WEIGHTS.get(edge_type, _DEFAULT_EDGE_WEIGHT)


@dataclass
class _GraphLink:
    neighbor_id: UUID
    edge_type: str
    weight: float


@dataclass
class _TunnelLink:
    neighbor_id: UUID
    tunnel_label: str


@dataclass
class _MergedCandidate:
    item: MemoryItem
    origins: set[str] = field(default_factory=set)
    semantic_score: float | None = None
    semantic_reasons: list[str] = field(default_factory=list)
    graph_links: list[_GraphLink] = field(default_factory=list)
    tunnel_links: list[_TunnelLink] = field(default_factory=list)
    source_semantic_score: float = 0.0
    original: dict[str, Any] | None = None


def _eligible_items_stmt(
    ids: set[UUID],
    *,
    memory_context: ResolvedMemoryContext,
    workspace_id: str | None,
) -> Select[tuple[MemoryItem]]:
    stmt = select(MemoryItem).where(
        MemoryItem.id.in_(ids),
        MemoryItem.valid_to.is_(None),
        MemoryItem.review_status.in_(_EXPANSION_REVIEW_STATUSES),
        read_eligibility_expression(memory_context),
    )
    if workspace_id is not None:
        stmt = stmt.where(MemoryItem.workspace_id == workspace_id)
    return stmt


async def _fetch_graph_neighbors(
    session: AsyncSession,
    *,
    memory_context: ResolvedMemoryContext,
    workspace_id: str | None,
    seed_ids: list[UUID],
) -> dict[UUID, list[_GraphLink]]:
    """Depth-1, bounded, deterministic graph expansion.

    Returns eligible neighbor_id -> list of links (a neighbor can be reached
    via more than one edge/seed; all are kept for explainability, but scoring
    uses only the strongest — see :func:`_relationship_bonus`).
    """
    if not seed_ids:
        return {}

    seed_id_set = set(seed_ids)
    stmt = select(MemoryEdge).where(
        MemoryEdge.tenant_id == memory_context.tenant_id,
        or_(
            MemoryEdge.source_item_id.in_(seed_id_set),
            MemoryEdge.target_item_id.in_(seed_id_set),
        ),
    )
    edges = list((await session.execute(stmt)).scalars().all())

    # Resolve every potential neighbor through the complete item predicate
    # before applying the per-seed cap. Otherwise high-weight ineligible
    # neighbors could consume the bounded window and displace eligible ones.
    potential_neighbor_ids: set[UUID] = set()
    for edge in edges:
        if edge.source_item_id in seed_id_set:
            potential_neighbor_ids.add(edge.target_item_id)
        if edge.target_item_id in seed_id_set:
            potential_neighbor_ids.add(edge.source_item_id)
    if not potential_neighbor_ids:
        return {}
    eligible_ids = {
        row.id
        for row in (
            await session.execute(
                _eligible_items_stmt(
                    potential_neighbor_ids,
                    memory_context=memory_context,
                    workspace_id=workspace_id,
                )
            )
        ).scalars()
    }

    # Group candidate (edge, neighbor_id) pairs per seed, bounded per seed to
    # max_graph_neighbors_per_item (requirement 11: a highly-connected node
    # must not dominate). Deterministic order: strongest weight first, then
    # edge_type, then neighbor id. A neighbor that is itself another semantic
    # seed is still recorded here — it must not lose its relationship bonus
    # just because it was already found semantically (requirement 5: origin
    # tags like "semantic+graph") — it simply doesn't count against the
    # max_graph_expanded_items budget below, since it's not a *new* addition.
    per_seed: dict[UUID, list[tuple[float, str, UUID]]] = defaultdict(list)
    for edge in edges:
        weight = effective_edge_weight(edge.edge_type, edge.weight)
        if edge.source_item_id in seed_id_set and edge.target_item_id in eligible_ids:
            per_seed[edge.source_item_id].append((weight, edge.edge_type, edge.target_item_id))
        if edge.target_item_id in seed_id_set and edge.source_item_id in eligible_ids:
            per_seed[edge.target_item_id].append((weight, edge.edge_type, edge.source_item_id))

    candidate_links: dict[UUID, list[_GraphLink]] = defaultdict(list)
    for seed_id in seed_ids:
        bucket = sorted(per_seed.get(seed_id, []), key=lambda t: (-t[0], t[1], str(t[2])))
        for weight, edge_type, neighbor_id in bucket[: settings.max_graph_neighbors_per_item]:
            candidate_links[neighbor_id].append(
                _GraphLink(neighbor_id=neighbor_id, edge_type=edge_type, weight=weight)
            )

    if not candidate_links:
        return {}

    # Existing semantic seeds are enriched unconditionally (no budget cost —
    # they're already part of the result set). Only genuinely new neighbors
    # compete for the max_graph_expanded_items cap, strongest first
    # (requirement 8: bounded graph additions).
    linked_ids = set(candidate_links)
    seed_neighbor_ids = linked_ids & seed_id_set
    new_neighbor_ids = sorted(
        linked_ids - seed_id_set,
        key=lambda nid: (
            -max(link.weight for link in candidate_links[nid]),
            str(nid),
        ),
    )[: settings.max_graph_expanded_items]

    return {nid: candidate_links[nid] for nid in (*seed_neighbor_ids, *new_neighbor_ids)}


async def _fetch_tunnel_neighbors(
    session: AsyncSession,
    *,
    memory_context: ResolvedMemoryContext,
    workspace_id: str | None,
    seed_items: list[MemoryItem],
    exclude_ids: set[UUID],
) -> dict[UUID, list[_TunnelLink]]:
    """Bounded, deterministic tunnel expansion.

    A seed's tunnel membership is any ``Tunnel`` row whose source or target
    (wing, room) matches the seed's own (wing, room); the *other* endpoint of
    that tunnel names the neighboring (wing, room) to pull bounded items from.
    No full tunnel scan: each matched (wing, room) is fetched with its own
    small LIMIT query, not a wing-wide table scan.
    """
    wings = {item.wing for item in seed_items if item.wing}
    if not wings:
        return {}

    tunnel_stmt = (
        select(Tunnel)
        .where(
            Tunnel.tenant_id == memory_context.tenant_id,
            or_(Tunnel.source_wing.in_(wings), Tunnel.target_wing.in_(wings)),
        )
        .order_by(Tunnel.created_at.asc(), Tunnel.id.asc())
    )
    tunnels = list((await session.execute(tunnel_stmt)).scalars().all())
    if not tunnels:
        return {}

    # (target_wing, target_room) -> label, deduped across seeds/tunnels.
    targets: dict[tuple[str, str | None], str] = {}
    for item in seed_items:
        if not item.wing:
            continue
        for tunnel in tunnels:
            label = tunnel.label or f"{tunnel.source_wing}<->{tunnel.target_wing}"
            if tunnel.source_wing == item.wing and (
                tunnel.source_room is None or tunnel.source_room == item.room
            ):
                targets[(tunnel.target_wing, tunnel.target_room)] = label
            if tunnel.target_wing == item.wing and (
                tunnel.target_room is None or tunnel.target_room == item.room
            ):
                targets[(tunnel.source_wing, tunnel.source_room)] = label

    if not targets:
        return {}

    candidate_links: dict[UUID, list[_TunnelLink]] = defaultdict(list)
    remaining = settings.max_tunnel_additions
    for (target_wing, target_room), label in sorted(
        targets.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")
    ):
        if remaining <= 0:
            break
        filters: list[Any] = [
            MemoryItem.wing == target_wing,
            MemoryItem.valid_to.is_(None),
            MemoryItem.review_status.in_(_EXPANSION_REVIEW_STATUSES),
            read_eligibility_expression(memory_context),
        ]
        if target_room is not None:
            filters.append(MemoryItem.room == target_room)
        if exclude_ids:
            filters.append(MemoryItem.id.notin_(exclude_ids))
        if workspace_id is not None:
            filters.append(MemoryItem.workspace_id == workspace_id)

        stmt = (
            select(MemoryItem)
            .where(*filters)
            .order_by(
                MemoryItem.importance.desc(), MemoryItem.created_at.desc(), MemoryItem.id.asc()
            )
            .limit(min(settings.max_tunnel_neighbors_per_item, remaining))
        )

        rows = list((await session.execute(stmt)).scalars().all())
        for row in rows:
            if row.id in candidate_links:
                continue
            candidate_links[row.id].append(_TunnelLink(neighbor_id=row.id, tunnel_label=label))
            remaining -= 1

    return dict(candidate_links)


def _relationship_bonus(links: list[_GraphLink]) -> float:
    """Strongest edge wins — a node with many weak edges shouldn't outscore
    one strong, directly relevant edge (requirement 11)."""
    if not links:
        return 0.0
    return max(link.weight for link in links)


def _score_candidate(candidate: _MergedCandidate) -> float:
    semantic_component = (
        candidate.semantic_score
        if candidate.semantic_score is not None
        else candidate.source_semantic_score
    )
    relationship_bonus = _relationship_bonus(candidate.graph_links)
    tunnel_bonus = 1.0 if candidate.tunnel_links else 0.0
    importance_bonus = candidate.item.importance

    score = (
        semantic_component * settings.relationship_score_weight_semantic
        + relationship_bonus * settings.relationship_score_weight_relationship
        + tunnel_bonus * settings.relationship_score_weight_tunnel
        + importance_bonus * settings.relationship_score_weight_importance
    )
    return round(score, 4)


def _build_reasons_and_warnings(candidate: _MergedCandidate) -> tuple[list[str], list[str]]:
    reasons: list[str] = list(candidate.semantic_reasons)
    seen_edge_types: set[str] = set()
    for link in candidate.graph_links:
        if link.edge_type in seen_edge_types:
            continue
        seen_edge_types.add(link.edge_type)
        reasons.append(f"linked via {link.edge_type}")
    seen_labels: set[str] = set()
    for tlink in candidate.tunnel_links:
        if tlink.tunnel_label in seen_labels:
            continue
        seen_labels.add(tlink.tunnel_label)
        reasons.append(f'same tunnel "{tlink.tunnel_label}"')

    warnings: list[str] = []
    if candidate.item.review_status == "proposed":
        warnings.append("unreviewed")
    return reasons, warnings


def _origin_label(origins: set[str]) -> str:
    return "+".join(sorted(origins, key=lambda o: {"semantic": 0, "graph": 1, "tunnel": 2}[o]))


async def expand_recall_candidates(
    session: AsyncSession,
    *,
    memory_context: ResolvedMemoryContext,
    workspace_id: str | None,
    semantic_items: list[dict[str, Any]],
    item_by_id: dict[UUID, MemoryItem],
    now: datetime,
) -> list[dict[str, Any]]:
    """Graph + tunnel expansion, merge, and relationship-aware rescoring.

    ``semantic_items`` are the already-scored/enriched semantic recall dicts
    (see engram.recall.execute_semantic_recall), sorted descending by
    semantic score. ``item_by_id`` supplies the backing ``MemoryItem`` rows
    for those same candidates (wing/room/importance/etc.).

    Returns a new list of response dicts (same shape as ``semantic_items``,
    plus relationship metadata) — merged, rescored, sorted descending by the
    new relationship-aware score, truncated to ``recall_candidate_ceiling``.
    Callers still run this through the normal budget packer unchanged.
    """
    if not settings.relationship_expansion_enabled or not semantic_items:
        return semantic_items

    seeds = semantic_items[: settings.recall_semantic_expansion_seed_limit]
    seed_ids = [UUID(c["id"]) for c in seeds]
    seed_id_set = set(seed_ids)

    merged: dict[UUID, _MergedCandidate] = {}
    for cand in seeds:
        item_id = UUID(cand["id"])
        item = item_by_id.get(item_id)
        if item is None:
            continue
        merged[item_id] = _MergedCandidate(
            item=item,
            origins={"semantic"},
            semantic_score=float(cand["score"]),
            semantic_reasons=list(cand.get("reasons", [])),
            source_semantic_score=float(cand["score"]),
            original=cand,
        )

    graph_neighbors = await _fetch_graph_neighbors(
        session,
        memory_context=memory_context,
        workspace_id=workspace_id,
        seed_ids=seed_ids,
    )
    if graph_neighbors:
        item_rows = {
            row.id: row
            for row in (
                await session.execute(
                    select(MemoryItem).where(
                        MemoryItem.id.in_(graph_neighbors.keys()),
                        read_eligibility_expression(memory_context),
                    )
                )
            ).scalars()
        }
        # Semantic-component fallback for expansion-only candidates: the best
        # score among the seeds they were expanded from. Individual seed
        # attribution is not tracked per neighbor, so the conservative choice
        # is the strongest seed score overall — see module docstring.
        best_seed_score = max((float(c["score"]) for c in seeds), default=0.0)
        for neighbor_id, links in graph_neighbors.items():
            item = item_rows.get(neighbor_id)
            if item is None:
                continue
            entry = merged.setdefault(neighbor_id, _MergedCandidate(item=item))
            entry.origins.add("graph")
            entry.graph_links.extend(links)
            entry.source_semantic_score = max(entry.source_semantic_score, best_seed_score)

    seed_items = [item_by_id[sid] for sid in seed_ids if sid in item_by_id]
    # Only exclude the semantic seeds themselves — graph-expanded neighbors
    # are still fetchable here so a node reachable via both graph and tunnel
    # gets the combined "graph+tunnel" origin (requirement 5) rather than
    # being silently skipped.
    tunnel_neighbors = await _fetch_tunnel_neighbors(
        session,
        memory_context=memory_context,
        workspace_id=workspace_id,
        seed_items=seed_items,
        exclude_ids=seed_id_set,
    )
    if tunnel_neighbors:
        item_rows = {
            row.id: row
            for row in (
                await session.execute(
                    select(MemoryItem).where(
                        MemoryItem.id.in_(tunnel_neighbors.keys()),
                        read_eligibility_expression(memory_context),
                    )
                )
            ).scalars()
        }
        best_seed_score = max((float(c["score"]) for c in seeds), default=0.0)
        for neighbor_id, tlinks in tunnel_neighbors.items():
            item = item_rows.get(neighbor_id)
            if item is None:
                continue
            entry = merged.setdefault(neighbor_id, _MergedCandidate(item=item))
            entry.origins.add("tunnel")
            entry.tunnel_links.extend(tlinks)
            entry.source_semantic_score = max(entry.source_semantic_score, best_seed_score)

    # Any semantic_items beyond the seed window pass through untouched,
    # appended after the merged/rescored seed window (still eligible — they
    # were already filtered by semantic.search()).
    tail_items = semantic_items[len(seeds):]

    scored: list[dict[str, Any]] = []
    for candidate in merged.values():
        reasons, warnings = _build_reasons_and_warnings(candidate)
        score = _score_candidate(candidate)
        item = candidate.item

        if candidate.original is not None:
            # Genuine semantic candidate (possibly also graph/tunnel-linked):
            # preserve its distance/similarity_score/trust_score fields.
            out = dict(candidate.original)
        else:
            out = {
                "id": str(item.id),
                "kind": item.kind,
                "content": item.content,
                "review_status": item.review_status,
                "pinned": item.pinned,
                "importance": item.importance,
                "source_trust": item.source_trust,
                "memory_confidence": item.memory_confidence,
                "human_verified": item.human_verified,
                # Additive served-decision fields (ENG-CONTEXT-001): keep
                # newly-expanded (graph/tunnel-only) items field-aligned with
                # genuine semantic candidates (which inherit these via
                # ``dict(candidate.original)`` above).
                "authority": item.authority,
                "visibility": item.visibility,
                "workspace_id": str(item.workspace_id) if item.workspace_id else None,
                "conflict_type": item.conflict_type,
                "conflict_resolution_status": item.conflict_resolution_status,
                "distance": None,
                "similarity_score": None,
                "trust_score": None,
            }

        out["score"] = score
        out["reasons"] = reasons
        out["warnings"] = warnings
        out["origin"] = _origin_label(candidate.origins)
        out["semantic_score"] = candidate.semantic_score
        out["relationship_bonus"] = _relationship_bonus(candidate.graph_links)
        out["tunnel_bonus"] = 1.0 if candidate.tunnel_links else 0.0
        scored.append(out)

    scored.sort(key=lambda d: d["score"], reverse=True)
    scored = scored[: settings.recall_candidate_ceiling]

    return scored + tail_items
