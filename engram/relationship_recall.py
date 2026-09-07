"""Relationship-aware recall: bounded graph + tunnel expansion (ENG-AUD-012 / F19).

Semantic recall finds relevant memories; this module reconstructs the context
*surrounding* them, using two existing structures:

  * ``memory_edges`` — typed, directed, depth-1 relationships between memory
    items (derived_from, references, explains, contradicts, supports,
    depends_on, mentions).
  * ``tunnels`` — cross-wing/room links (see engram.models.Tunnel); a memory's
    "tunnel membership" is any tunnel whose source/target (wing, room)
    coordinates match the memory's own (wing, room).

Two consumers share the bounded discovery mechanics:

* **Legacy expansion** (:func:`expand_recall_candidates`, ENG-AUD-012) — the
  compatibility path ``POST /v1/recall`` serves today:

    semantic candidates (already scored, sorted desc)
        -> bounded seed selection (recall_semantic_expansion_seed_limit)
        -> graph expansion (depth 1, bounded, deterministic)
        -> tunnel expansion (bounded, deterministic)
        -> merge (dedupe by id, track origin + relationship metadata)
        -> relationship-aware rescoring (legacy blend, importance included)
        -> ceiling truncation (recall_candidate_ceiling)

* **Candidate-profile expansion** (issue #190 / ENG-RECALL-003D,
  :func:`discover_candidate_neighbors` +
  :func:`compute_relationship_relevance`) — the admission-first path the
  governed/exploratory shadow profiles use. Discovery is the *same* bounded
  depth-1 mechanics under the *same* hard boundaries, but it never scores or
  admits anything: only V2-admitted direct items may seed it, every expanded
  neighbor is admitted independently by the caller through the exact #158
  surface decision, and relevance is computed by the versioned pure helper
  that excludes importance/trust/confidence/verification/review/exposure
  inputs entirely (utility is applied separately, after admission).

Every expanded candidate is re-filtered through read eligibility (tenant +
read_eligibility_expression + workspace scope + a review-window corpus
predicate) — expansion is never an eligibility bypass. No recursive
traversal: graph/tunnel neighbors are found only for the original seeds,
never for neighbors of neighbors.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal
from uuid import UUID

from sqlalchemy import ColumnElement, Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from engram.config import settings
from engram.memory_access import read_eligibility_expression
from engram.memory_context import ResolvedMemoryContext
from engram.models import MemoryEdge, MemoryItem, Tunnel
from engram.recall_signals import live_proposal_expression

# Recall-pipeline scoring version (distinct from engram.semantic.SEMANTIC_SCORING_VERSION,
# which covers /v1/search's own ranking, unaffected by this module). Bumped
# because execute_semantic_recall's final per-item score now blends
# relationship/tunnel/importance bonuses on top of the semantic score
# (requirement 6) — auditable via recall_logs.scoring_version.
RECALL_SCORING_VERSION = "semantic-v3"

# Contract identity of the candidate-profile (V2-bound) relationship-relevance
# algorithm (issue #190 / ENG-RECALL-003D). Distinct from the legacy blend
# above: importance is excluded (utility, applied after admission), and
# graph/tunnel contribution is bounded to [0, 1]. Later #162 certification
# artifacts reference this version to identify exactly which algorithm
# produced a candidate packet's relevance values.
RELATIONSHIP_RELEVANCE_VERSION: Final[Literal["relationship-relevance-v1"]] = (
    "relationship-relevance-v1"
)

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
    # The seed this link was reached from. Informational for the legacy blend
    # (which never reads it); the candidate-profile path (issue #190) uses it
    # for precise per-neighbor source-seed relevance attribution.
    seed_id: UUID | None = None


@dataclass
class _TunnelLink:
    neighbor_id: UUID
    tunnel_label: str
    # A seed whose tunnel membership exposed the (wing, room) this neighbor
    # was pulled from (None on the frozen legacy path, whose blend never
    # reads it). The candidate-profile path (issue #190) uses it for precise
    # per-neighbor source-seed relevance attribution — mirrors
    # ``_GraphLink.seed_id``.
    seed_id: UUID | None = None


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
    corpus_eligibility: ColumnElement[bool] | None = None,
) -> Select[tuple[MemoryItem]]:
    """The complete neighbor predicate: tenant/read eligibility plus the
    consumer's review-window corpus predicate.

    ``corpus_eligibility=None`` is the legacy window (active + proposed).
    The candidate-profile path passes the V2 live-proposal predicate
    (``recall_signals.live_proposal_expression``) — the exact window its own
    direct retrieval uses, so discovery can neither admit, widen, nor hide
    policy-relevant state the direct path would have shown (issue #190: a
    prefilter is allowed only under that invariant; the exact V2 decision
    stays authoritative).
    """
    stmt = select(MemoryItem).where(
        MemoryItem.id.in_(ids),
        MemoryItem.valid_to.is_(None),
        (
            corpus_eligibility
            if corpus_eligibility is not None
            else MemoryItem.review_status.in_(_EXPANSION_REVIEW_STATUSES)
        ),
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
    corpus_eligibility: ColumnElement[bool] | None = None,
    enrichment_ids: set[UUID] | None = None,
) -> dict[UUID, list[_GraphLink]]:
    """Depth-1, bounded, deterministic graph expansion.

    Returns eligible neighbor_id -> list of links (a neighbor can be reached
    via more than one edge/seed; all are kept for explainability, but scoring
    uses only the strongest — see :func:`_relationship_bonus`).

    ``enrichment_ids`` (the candidate-profile path passes the whole direct
    candidate window, seeds included) splits two concerns the legacy path
    never had: links to already-evaluated items are *enrichment* — recorded
    for origin-merging at zero capacity cost, since the caller already
    admission-evaluated them and never re-evaluates them here — while only
    genuinely new neighbor ids compete for ``max_graph_neighbors_per_item``
    and ``max_graph_expanded_items``. Without that split, a direct candidate
    (including one the exact V2 surface withheld) could occupy a bounded
    graph slot purely because it was semantically near the query and suppress
    a genuinely new expansion candidate (issue #190).
    """
    if not seed_ids:
        return {}
    enrichment = enrichment_ids or None

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
                    corpus_eligibility=corpus_eligibility,
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
    # On the candidate-profile path (enrichment set present) that zero-cost
    # treatment extends to every already-evaluated direct candidate, and the
    # per-seed cap counts only genuinely new neighbors.
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
        if enrichment is None:
            selected = bucket[: settings.max_graph_neighbors_per_item]
        else:
            selected = []
            new_taken = 0
            for weight, edge_type, neighbor_id in bucket:
                if neighbor_id in enrichment:
                    selected.append((weight, edge_type, neighbor_id))
                elif new_taken < settings.max_graph_neighbors_per_item:
                    selected.append((weight, edge_type, neighbor_id))
                    new_taken += 1
        for weight, edge_type, neighbor_id in selected:
            candidate_links[neighbor_id].append(
                _GraphLink(
                    neighbor_id=neighbor_id, edge_type=edge_type, weight=weight, seed_id=seed_id
                )
            )

    if not candidate_links:
        return {}

    # Existing semantic seeds are enriched unconditionally (no budget cost —
    # they're already part of the result set). Only genuinely new neighbors
    # compete for the max_graph_expanded_items cap, strongest first
    # (requirement 8: bounded graph additions). With an enrichment set, the
    # unconditional tier is the whole already-evaluated direct window; the
    # caps then bound genuinely new neighbors only.
    linked_ids = set(candidate_links)
    if enrichment is None:
        enriched_ids = linked_ids & seed_id_set
        capped_ids = linked_ids - seed_id_set
    else:
        enriched_ids = linked_ids & enrichment
        capped_ids = linked_ids - enrichment
    new_neighbor_ids = sorted(
        capped_ids,
        key=lambda nid: (
            -max(link.weight for link in candidate_links[nid]),
            str(nid),
        ),
    )[: settings.max_graph_expanded_items]

    return {nid: candidate_links[nid] for nid in (*enriched_ids, *new_neighbor_ids)}


@dataclass(frozen=True)
class _TunnelTarget:
    """One (wing, room) target tunnels expose, with every seed that reaches it.

    ``label`` keeps the legacy dedup semantics (last matching seed/tunnel
    write wins). ``seed_ids`` accumulates *all* seeds whose tunnel membership
    exposes the target, because every item pulled from that target is
    genuinely reachable from each of them — the provenance the
    candidate-profile path binds into ``_TunnelLink.seed_id`` so tunnel
    relevance attributes only seeds that actually reached an item, never an
    unrelated packet-level best seed (issue #190).
    """

    label: str
    seed_ids: frozenset[UUID]


async def _tunnel_targets(
    session: AsyncSession,
    *,
    memory_context: ResolvedMemoryContext,
    seed_items: list[MemoryItem],
) -> dict[tuple[str, str | None], _TunnelTarget]:
    """Resolve the (wing, room) targets tunnels expose for these seeds.

    A seed's tunnel membership is any ``Tunnel`` row whose source or target
    (wing, room) matches the seed's own (wing, room); the *other* endpoint of
    that tunnel names the neighboring (wing, room) to pull items from.
    Returns ``(target_wing, target_room) -> label + the seeds that reach it``,
    deduped across seeds/tunnels. Shared by the legacy and candidate-profile
    fetchers so the two can never disagree about tunnel topology; the frozen
    legacy fetcher ignores the seed attribution (its blend never reads it).
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

    targets: dict[tuple[str, str | None], _TunnelTarget] = {}
    for item in seed_items:
        if not item.wing:
            continue
        for tunnel in tunnels:
            label = tunnel.label or f"{tunnel.source_wing}<->{tunnel.target_wing}"
            if tunnel.source_wing == item.wing and (
                tunnel.source_room is None or tunnel.source_room == item.room
            ):
                key = (tunnel.target_wing, tunnel.target_room)
                prior = targets.get(key)
                targets[key] = _TunnelTarget(
                    label=label,
                    seed_ids=(prior.seed_ids if prior is not None else frozenset())
                    | {item.id},
                )
            if tunnel.target_wing == item.wing and (
                tunnel.target_room is None or tunnel.target_room == item.room
            ):
                key = (tunnel.source_wing, tunnel.source_room)
                prior = targets.get(key)
                targets[key] = _TunnelTarget(
                    label=label,
                    seed_ids=(prior.seed_ids if prior is not None else frozenset())
                    | {item.id},
                )
    return targets


async def _fetch_tunnel_neighbors(
    session: AsyncSession,
    *,
    memory_context: ResolvedMemoryContext,
    workspace_id: str | None,
    seed_items: list[MemoryItem],
    exclude_ids: set[UUID],
) -> dict[UUID, list[_TunnelLink]]:
    """Bounded, deterministic tunnel expansion (legacy window/ordering).

    Each matched (wing, room) is fetched with its own small LIMIT query, not
    a wing-wide table scan. Discovery order is importance-first — a utility
    ordering the legacy compatibility path has always used.
    """
    targets = await _tunnel_targets(session, memory_context=memory_context, seed_items=seed_items)
    if not targets:
        return {}

    candidate_links: dict[UUID, list[_TunnelLink]] = defaultdict(list)
    remaining = settings.max_tunnel_additions
    for (target_wing, target_room), target in sorted(
        targets.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")
    ):
        label = target.label
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


def _add_candidate_tunnel_links(
    links_by_neighbor: dict[UUID, list[_TunnelLink]],
    neighbor_id: UUID,
    label: str,
    seed_ids: list[UUID],
) -> bool:
    """Record one tunnel link per reaching seed, deduped by (label, seed_id).

    Returns whether the neighbor was linked for the first time — the signal
    the bounded new-neighbor budget keys on: re-seeing an already-linked
    neighbor through another target adds source attribution, never budget.
    """
    links = links_by_neighbor[neighbor_id]
    newly_linked = not links
    for seed_id in seed_ids:
        link = _TunnelLink(neighbor_id=neighbor_id, tunnel_label=label, seed_id=seed_id)
        if link not in links:
            links.append(link)
    return newly_linked


async def _fetch_candidate_tunnel_neighbors(
    session: AsyncSession,
    *,
    memory_context: ResolvedMemoryContext,
    workspace_id: str | None,
    seed_items: list[MemoryItem],
    exclude_ids: set[UUID],
    enrichment_ids: set[UUID] | None = None,
) -> dict[UUID, list[_TunnelLink]]:
    """Bounded, deterministic tunnel discovery for V2-bound profiles (#190).

    Same tunnel topology and the same hard boundaries (tenant/read
    eligibility, explicit workspace restriction, live-proposal corpus
    window, per-target and total caps), but the discovery order is
    importance-free (``created_at desc, id asc``): utility signals may order
    only already-admitted items, never influence which neighbors a candidate
    profile discovers within its bounded window.

    Source-seed attribution (the tunnel analogue of ``_GraphLink.seed_id``):
    every neighbor pulled from a target (wing, room) is linked once per
    admitted seed whose tunnel membership exposed that target, deduped by
    (label, seed_id) — so relationship relevance can attribute exactly the
    seeds that actually reached the item, never an unrelated packet-level
    best seed (issue #190).

    ``enrichment_ids`` (the whole direct candidate window) receives the same
    direct-vs-new split the graph fetcher applies: already-evaluated direct
    items sitting in a tunneled (wing, room) collect tunnel-origin metadata
    for origin-merging at zero budget cost — no second admission, no
    consumption of the new-neighbor tunnel caps — while genuinely new
    neighbors keep competing inside the bounded per-target/total windows
    (issue #190: ``semantic+tunnel`` and ``semantic+graph+tunnel`` origins
    must be representable). Enrichment queries apply the identical
    eligibility predicates, so tunnel visibility is never broadened.

    Deliberately a near-sibling of :func:`_fetch_tunnel_neighbors` rather
    than a parameterized shared helper: the legacy fetcher is frozen for
    byte-compatibility, and its importance-first ordering is exactly the
    utility ordering the candidate contract forbids — the two policies are
    kept in sibling functions so neither can regress the other.
    """
    targets = await _tunnel_targets(session, memory_context=memory_context, seed_items=seed_items)
    if not targets:
        return {}

    candidate_links: dict[UUID, list[_TunnelLink]] = defaultdict(list)
    remaining = settings.max_tunnel_additions
    for (target_wing, target_room), target in sorted(
        targets.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")
    ):
        label = target.label
        # Deterministic attribution order: seed ids ascending.
        target_seed_ids = sorted(target.seed_ids)
        if enrichment_ids:
            # Direct-item enrichment: bounded by the already-evaluated direct
            # window itself, so it needs no LIMIT and never touches the
            # new-neighbor budget. Same predicates as the discovery query —
            # enrichment is not an eligibility bypass.
            enrich_filters: list[Any] = [
                MemoryItem.id.in_(enrichment_ids),
                MemoryItem.wing == target_wing,
                live_proposal_expression(),
                read_eligibility_expression(memory_context),
            ]
            if target_room is not None:
                enrich_filters.append(MemoryItem.room == target_room)
            if workspace_id is not None:
                enrich_filters.append(MemoryItem.workspace_id == workspace_id)
            enrich_stmt = (
                select(MemoryItem)
                .where(*enrich_filters)
                .order_by(MemoryItem.created_at.desc(), MemoryItem.id.asc())
            )
            for row in (await session.execute(enrich_stmt)).scalars().all():
                _add_candidate_tunnel_links(candidate_links, row.id, label, target_seed_ids)
        if remaining <= 0:
            # Enrichment is budget-free, so later targets still get their
            # enrichment pass; with no enrichment set the rest of this loop
            # body is a no-op from here on, exactly like the legacy break.
            continue
        filters: list[Any] = [
            MemoryItem.wing == target_wing,
            live_proposal_expression(),
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
            .order_by(MemoryItem.created_at.desc(), MemoryItem.id.asc())
            .limit(min(settings.max_tunnel_neighbors_per_item, remaining))
        )

        rows = list((await session.execute(stmt)).scalars().all())
        for row in rows:
            if _add_candidate_tunnel_links(candidate_links, row.id, label, target_seed_ids):
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


# ---- candidate-profile relationship relevance (issue #190 / ENG-RECALL-003D) ----
#
# Relationship is a *relevance* signal only. It can never make a memory
# trusted, epistemically supported, review-approved, or admissible — those
# are the exact V2 surface decision's job, resolved independently per item.


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


@dataclass(frozen=True)
class RelationshipRelevance:
    """Structured, versioned relationship-aware relevance for one item.

    The deterministic explanation block candidate-profile items carry when
    relationship expansion reached them (issue #190): origin decomposition,
    the direct semantic relevance when the item was also a direct hit, the
    source-seed relevance that justified expansion when it was not, the
    strongest graph contribution with its edge types, tunnel labels, the
    per-component contributions, and the final bounded ``relevance_score``
    the candidate profile ranks with.
    """

    version: str
    origins: tuple[str, ...]
    direct: bool
    direct_semantic_score: float | None
    source_seed_score: float
    graph_contribution: float
    graph_edge_types: tuple[str, ...]
    tunnel_labels: tuple[str, ...]
    relevance_score: float
    components: dict[str, float]

    def payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "origins": list(self.origins),
            "direct": self.direct,
            "direct_semantic_score": (
                round(self.direct_semantic_score, 4)
                if self.direct_semantic_score is not None
                else None
            ),
            "source_seed_score": round(self.source_seed_score, 4),
            "graph_contribution": round(self.graph_contribution, 4),
            "graph_edge_types": list(self.graph_edge_types),
            "tunnel_labels": list(self.tunnel_labels),
            "relevance_score": self.relevance_score,
            "components": {key: round(value, 4) for key, value in self.components.items()},
        }


def compute_relationship_relevance(
    *,
    direct_semantic_score: float | None,
    source_seed_score: float,
    graph_links: Sequence[tuple[str, float]] = (),
    tunnel_labels: Sequence[str] = (),
) -> RelationshipRelevance:
    """The one pure, versioned relationship-relevance helper (issue #190).

    Combines the item's own direct semantic relevance (when it was a direct
    hit) with the bounded graph/tunnel contribution derived from the seeds
    that reached it, reusing the relationship weighting configuration with
    the importance term removed::

        blend   = w_semantic * semantic + w_graph * strongest_edge
                  + w_tunnel * tunnel_membership
        relevance = clamp01(max(direct_score or 0, blend))

    Invariants (mechanically pinned by unit tests):

    * importance, source trust, memory confidence, human verification,
      review state, exposure counters, and epistemic/risk state are not
      inputs — they cannot move relationship relevance by construction;
    * graph/tunnel contribution is bounded to ``[0, 1]`` (per-edge weights
      are clamped, so a stored weight > 1 cannot inflate it);
    * a direct hit's relevance is at least its own semantic similarity
      (``max`` floor) — being additionally relationship-linked can never
      demote it, and an unlinked direct item's relevance is exactly its
      similarity, byte-compatible with the pre-#190 signal contract;
    * a relationship can only derive relevance from the source-seed score
      and its own bounded contribution — never create relevance outside
      that contract;
    * identical inputs produce identical relevance (deterministic, and the
      weight sum w_semantic + w_graph + w_tunnel < 1 keeps the blend in
      range even at maximal inputs).
    """
    graph_links = tuple(graph_links)
    tunnel_labels = tuple(tunnel_labels)
    graph_contribution = max((_clamp01(weight) for _, weight in graph_links), default=0.0)
    tunnel_membership = 1.0 if tunnel_labels else 0.0
    semantic_component = (
        direct_semantic_score if direct_semantic_score is not None else source_seed_score
    )
    components = {
        "semantic": settings.relationship_score_weight_semantic * semantic_component,
        "graph": settings.relationship_score_weight_relationship * graph_contribution,
        "tunnel": settings.relationship_score_weight_tunnel * tunnel_membership,
    }
    blend = sum(components.values())
    direct_floor = direct_semantic_score if direct_semantic_score is not None else 0.0
    relevance_score = round(_clamp01(max(direct_floor, blend)), 4)

    origins: list[str] = []
    if direct_semantic_score is not None:
        origins.append("semantic")
    if graph_links:
        origins.append("graph")
    if tunnel_labels:
        origins.append("tunnel")
    return RelationshipRelevance(
        version=RELATIONSHIP_RELEVANCE_VERSION,
        origins=tuple(origins),
        direct=direct_semantic_score is not None,
        direct_semantic_score=direct_semantic_score,
        source_seed_score=source_seed_score,
        graph_contribution=graph_contribution,
        graph_edge_types=tuple(sorted({edge_type for edge_type, _ in graph_links})),
        tunnel_labels=tuple(sorted(set(tunnel_labels))),
        relevance_score=relevance_score,
        components=components,
    )


# ---- candidate-profile neighbor discovery (issue #190) ----


@dataclass
class CandidateNeighborDiscovery:
    """Bounded graph+tunnel neighbor discovery for one candidate packet.

    ``graph_links`` / ``tunnel_links`` include already-evaluated direct
    candidates reachable from an admitted seed (origin-merging enrichment —
    zero capacity cost, never re-admitted). Every link carries the admitted
    seed it was reached from (``seed_id`` — the exact provenance
    relationship relevance attributes, never a packet-level best seed).
    ``neighbor_items`` carries the backing ``MemoryItem`` rows for every
    *genuinely new* discovered id only
    (never the seeds or the enrichment window) — fetched in one bounded bulk
    query with read-eligibility defense in depth. Discovery confers no
    admission: every discovered neighbor must be independently admitted by
    the caller through the exact V2 surface.
    """

    graph_links: dict[UUID, list[_GraphLink]]
    tunnel_links: dict[UUID, list[_TunnelLink]]
    neighbor_items: dict[UUID, MemoryItem]


async def discover_candidate_neighbors(
    session: AsyncSession,
    *,
    memory_context: ResolvedMemoryContext,
    workspace_id: str | None,
    seed_ids: list[UUID],
    seed_items: list[MemoryItem],
    exclude_ids: set[UUID],
    enrichment_ids: set[UUID] | None = None,
) -> CandidateNeighborDiscovery:
    """Admission-first bounded neighbor discovery for a V2-bound profile.

    Runs AFTER the caller admitted the direct candidates (the seeds) through
    the exact V2 surface — a withheld direct hit can never seed discovery
    because it is never passed in here. Mechanics are the shared bounded
    graph/tunnel core: depth-1 only, per-seed and total graph caps, total
    tunnel cap, same-tenant edges/tunnels only, read eligibility, explicit
    workspace restriction with no unscoped fallback, and the V2
    live-proposal corpus window as the discovery prefilter (the exact window
    the profile's own direct retrieval uses — it can never widen or hide
    policy-relevant candidate state).

    ``exclude_ids`` (the direct candidate window) are excluded from tunnel
    fetches and from the returned neighbor rows: they were already
    admission-evaluated as direct candidates, so expansion never
    re-evaluates or double-diagnoses them. ``enrichment_ids`` (normally the
    same direct window) additionally lets those already-evaluated items
    collect graph/tunnel origin metadata from admitted seeds — the
    direct-vs-new split that keeps every bounded window reserved for
    genuinely new neighbors (issue #190).
    """
    graph_links = await _fetch_graph_neighbors(
        session,
        memory_context=memory_context,
        workspace_id=workspace_id,
        seed_ids=seed_ids,
        corpus_eligibility=live_proposal_expression(),
        enrichment_ids=enrichment_ids,
    )
    tunnel_links = await _fetch_candidate_tunnel_neighbors(
        session,
        memory_context=memory_context,
        workspace_id=workspace_id,
        seed_items=seed_items,
        exclude_ids=exclude_ids,
        enrichment_ids=enrichment_ids,
    )

    discovered_ids = (set(graph_links) | set(tunnel_links)) - set(seed_ids) - exclude_ids
    if enrichment_ids:
        discovered_ids -= enrichment_ids
    neighbor_items: dict[UUID, MemoryItem] = {}
    if discovered_ids:
        neighbor_stmt = select(MemoryItem).where(
            MemoryItem.id.in_(discovered_ids),
            live_proposal_expression(),
            read_eligibility_expression(memory_context),
        )
        if workspace_id is not None:
            # Same explicit-restriction defense in depth the discovery
            # queries enforce — never an unscoped fallback.
            neighbor_stmt = neighbor_stmt.where(MemoryItem.workspace_id == workspace_id)
        rows = await session.execute(neighbor_stmt)
        neighbor_items = {row.id: row for row in rows.scalars().all()}
    return CandidateNeighborDiscovery(
        graph_links=graph_links,
        tunnel_links=tunnel_links,
        neighbor_items=neighbor_items,
    )


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
