"""Conflict-preserving, diversity-aware packing for candidate recall packets.

The final selection stage of the #160 candidate pipeline (issue #192 /
ENG-RECALL-003E), operating strictly *after* admission (#186), relationship
expansion (#190), and the separated relevance/utility ranking:

    eligible corpus -> exact V2 admission -> retrieval + expansion
      -> separated ranking -> conflict/diversity-aware packing  <- this module
      -> byte/token/item budgets -> shadow packet

The legacy profile never enters this module — its rank-then-truncate packing
(``recall._enforce_semantic_budget``) is byte-for-byte compatibility and stays
untouched. Candidate (governed/exploratory) packets are computed only by the
read-only shadow comparison surface, so this packing is shadow-only too.

Doctrine (each rule is structural in :func:`pack_admitted_candidates`):

* **Packing never changes admission or evidence state.** The packer consumes
  already-admitted, already-ranked items and adds only a per-item
  ``packing_reason`` plus bounded packet-level counts; it cannot rewrite
  relevance, utility, epistemic state, risk, warning codes, admission
  decisions, or the ``evidence`` block.
* **Known shared origin may suppress redundancy; unknown independence may
  not.** Known-root diversity uses explicit durable derivation relationships
  already present among admitted candidates — union-find over explicit
  ``derived_from`` edges, and nothing else. Canonical content equality (an
  equal ``content_hash``) proves only equal canonicalized text, never shared
  provenance/root identity — the write-path dedup index is scoped duplicate
  prevention, not a global root assertion — so it is not a v1 root signal.
  Vector similarity alone is relevance, never root identity: two merely
  similar items are never grouped, and two items with no explicit
  shared-root fact are *unknown*, never "independent" (independence/quorum
  semantics belong to #161, which stays the sole owner of
  evidence-root-aware corroboration).
* **Conflict preservation is representation, not resolution.** When both
  sides of an explicit conflict (``conflicts_with_item_id`` linkage or a
  ``contradicts`` edge) are admitted candidates, selecting one side creates a
  co-pack obligation for the other — subject to the same hard budgets. The
  packer never decides which side is true and never mutates either side's
  epistemic state; a budget-impossible co-pack is *recorded*
  (``conflict_counterpart_budget``), never silently resolved.
* **No popularity loop.** Recall/exposure counters are not inputs; only the
  existing rank order (which excludes them) is.
* **Budgets stay hard.** Selection uses the exact rendered accounting of the
  semantic packet path (``len(content.encode())`` bytes,
  ``max(1, bytes // 4)`` tokens) with the same skip-not-break discipline —
  item budget ends selection, oversized items are skipped while smaller
  lower-ranked ones still fit.
* **Determinism is mandatory.** Fixed admitted set (in rank order), fixed
  relations, and fixed budgets reproduce the same selected ids, rendered
  order, per-item reasons, and omission counts. There is no global scalar
  packing score and no randomness.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engram.models import MemoryEdge
from engram.semantic_budget import semantic_item_byte_count, semantic_item_token_cost

# Contract identity for the packing stage. Mirrored into every candidate
# packet's ``packing`` summary; bump/branch only with an ADR (see
# docs/adr-160-recall-profiles.md).
RECALL_PACKING_VERSION: Final[Literal["recall-packing-v1"]] = "recall-packing-v1"

# Why one *selected* item occupies packet capacity (bounded vocabulary).
PackingReason = Literal["ranked", "conflict_pair_preserved", "diversity_fill"]

# Why one *admitted* item was omitted from the packet (bounded vocabulary).
OMIT_REDUNDANT_KNOWN_ROOT: Final[Literal["redundant_known_root"]] = "redundant_known_root"
OMIT_CONFLICT_COUNTERPART_BUDGET: Final[Literal["conflict_counterpart_budget"]] = (
    "conflict_counterpart_budget"
)
OMIT_BUDGET: Final[Literal["budget"]] = "budget"

# The only edge types with packing semantics: ``derived_from`` is the explicit
# derivation family; ``contradicts`` is the explicit tension linkage. Every
# other edge type (supports, references, …) is relevance-only (issue #190) and
# must not influence packing.
_PACKING_EDGE_TYPES: Final[tuple[str, ...]] = ("derived_from", "contradicts")


@dataclass(frozen=True)
class PackCandidate:
    """One admitted candidate as the pure packer sees it.

    List position carries the rank: the caller passes candidates in the exact
    deterministic order the separated ranking produced (signal rank desc,
    then the ranking's stable tie-breaks), and the packer never re-sorts.
    ``conflicts_with`` is the item's own durable identity fact
    (``memory_items.conflicts_with_item_id``), not a packer derivation.
    Content-identity inputs such as ``content_hash`` are deliberately absent:
    canonical content equality is not a v1 root signal (see module doctrine).
    """

    item_id: UUID
    content: str
    conflicts_with: UUID | None = None


@dataclass(frozen=True)
class PackingRelations:
    """Explicit, mechanically-known packing relations among admitted items.

    Both maps are symmetric adjacency (an edge binds its two endpoints
    regardless of direction). They are built *only* from edges whose two
    endpoints are both inside the already-admitted candidate set, so a link
    to a foreign, private, or otherwise inaccessible item simply never
    appears — packing cannot become a discovery channel and cannot leak the
    existence of an inaccessible sibling.
    """

    derived_links: Mapping[UUID, frozenset[UUID]] = field(default_factory=dict)
    contradicts_links: Mapping[UUID, frozenset[UUID]] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> PackingRelations:
        return cls(derived_links={}, contradicts_links={})


async def load_packing_relations(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    item_ids: set[UUID],
) -> PackingRelations:
    """Bulk-load the explicit derivation/contradiction edges among candidates.

    ONE bounded query (constant query count regardless of candidate volume)
    scoped to the tenant and to edges whose *both endpoints* lie in the
    admitted candidate id set — the set that already passed read/RLS/
    workspace eligibility. Edges touching anything outside that set are
    invisible here, which is the entire privacy story: a cross-tenant edge,
    a private sibling, or an out-of-workspace counterpart can neither
    influence selection nor surface in diagnostics.
    """
    if not item_ids:
        return PackingRelations.empty()
    rows = await session.execute(
        select(
            MemoryEdge.source_item_id,
            MemoryEdge.target_item_id,
            MemoryEdge.edge_type,
        ).where(
            MemoryEdge.tenant_id == tenant_id,
            MemoryEdge.edge_type.in_(_PACKING_EDGE_TYPES),
            MemoryEdge.source_item_id.in_(item_ids),
            MemoryEdge.target_item_id.in_(item_ids),
        )
    )
    derived: dict[UUID, set[UUID]] = {}
    contradicts: dict[UUID, set[UUID]] = {}
    for source_id, target_id, edge_type in rows.all():
        bucket = derived if edge_type == "derived_from" else contradicts
        bucket.setdefault(source_id, set()).add(target_id)
        bucket.setdefault(target_id, set()).add(source_id)
    return PackingRelations(
        derived_links={key: frozenset(values) for key, values in derived.items()},
        contradicts_links={key: frozenset(values) for key, values in contradicts.items()},
    )


@dataclass
class PackingResult:
    """The deterministic outcome of one packing run.

    ``selected`` is in rendered order — the rank order the caller passed,
    restricted to selected items. ``reasons``/``omitted`` are the bounded
    per-item and per-reason accounting that :meth:`summary` publishes.
    """

    selected: list[UUID]
    reasons: dict[UUID, PackingReason]
    omitted: dict[str, int]
    conflict_pairs_preserved: int

    def summary(self) -> dict[str, Any]:
        """The bounded packet-level packing block (issue #192 diagnostics).

        Fixed key set, counts only: no rejected-candidate content, no item
        ids, no counterpart identities — nothing that could leak an
        inaccessible sibling through aggregate diagnostics.
        """
        return {
            "version": RECALL_PACKING_VERSION,
            "selected_count": len(self.selected),
            "conflict_pairs_preserved": self.conflict_pairs_preserved,
            "omitted": dict(sorted(self.omitted.items())),
        }


class _UnionFind:
    """Minimal deterministic union-find over candidate ids."""

    def __init__(self, ids: Iterable[UUID]) -> None:
        self._parent = {item_id: item_id for item_id in ids}

    def find(self, item_id: UUID) -> UUID:
        root = item_id
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item_id] != root:
            self._parent[item_id], item_id = root, self._parent[item_id]
        return root

    def union(self, left: UUID, right: UUID) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root == right_root:
            return
        # Deterministic tie-break: the lexicographically-smaller root wins.
        if str(right_root) < str(left_root):
            left_root, right_root = right_root, left_root
        self._parent[right_root] = left_root


def _known_root_groups(
    candidates: Sequence[PackCandidate], relations: PackingRelations
) -> dict[UUID, UUID]:
    """Map every candidate to its mechanically-known redundancy group root.

    Exactly one durable fact creates a group: an explicit ``derived_from``
    edge between two admitted items (transitively, chains of such edges).
    Canonical content equality is not provenance — an equal ``content_hash``
    across workspace/principal scope boundaries proves only equal
    canonicalized text, and the write-path dedup index is scoped duplicate
    prevention, not a root assertion — so it never groups.

    Semantically similar-but-distinct items land in different groups by
    construction; ``unknown`` is never coerced to either redundant or
    independent.
    """
    groups = _UnionFind(cand.item_id for cand in candidates)
    for item_id, neighbors in sorted(
        relations.derived_links.items(), key=lambda pair: str(pair[0])
    ):
        for neighbor_id in sorted(neighbors, key=str):
            groups.union(item_id, neighbor_id)
    return {cand.item_id: groups.find(cand.item_id) for cand in candidates}


def _conflict_adjacency(
    candidates: Sequence[PackCandidate], relations: PackingRelations
) -> dict[UUID, frozenset[UUID]]:
    """Symmetric explicit-conflict adjacency among the admitted candidates.

    Sources: the durable ``memory_items.conflicts_with_item_id`` linkage and
    explicit ``contradicts`` edges — each restricted to counterparts that are
    themselves admitted candidates in this packet. A linkage pointing at a
    withheld, unretrieved, or inaccessible item produces no obligation and
    no diagnostic trace: its existence is not the caller's to learn here.
    """
    present = {cand.item_id for cand in candidates}
    adjacency: dict[UUID, set[UUID]] = {}
    for cand in candidates:
        if (
            cand.conflicts_with is not None
            and cand.conflicts_with != cand.item_id
            and cand.conflicts_with in present
        ):
            adjacency.setdefault(cand.item_id, set()).add(cand.conflicts_with)
            adjacency.setdefault(cand.conflicts_with, set()).add(cand.item_id)
    for item_id, neighbors in relations.contradicts_links.items():
        if item_id not in present:
            continue
        for neighbor_id in neighbors:
            if neighbor_id in present:
                adjacency.setdefault(item_id, set()).add(neighbor_id)
                adjacency.setdefault(neighbor_id, set()).add(item_id)
    return {key: frozenset(values) for key, values in adjacency.items()}


def _rendered_size(content: str) -> tuple[int, int]:
    """The rendered byte/token accounting the semantic packet path uses.

    One definition shared by every budget decision in this module so the
    packer's arithmetic stays mechanically identical to
    ``recall._enforce_semantic_budget`` (item content bytes; the fixed
    ``bytes // 4`` token heuristic with a 1-token floor).
    """
    return semantic_item_byte_count(content), semantic_item_token_cost(content)


def pack_admitted_candidates(
    candidates: Sequence[PackCandidate],
    *,
    relations: PackingRelations,
    byte_budget: int | None,
    token_budget: int | None,
    item_budget: int | None,
) -> PackingResult:
    """Deterministically pack ranked admitted candidates into a bounded packet.

    Phased lexicographic packing — no global scalar score, no score mutation:

    1. **Representation** (rank order): select the first-seen member of each
       not-yet-represented known-root group (``ranked``); group members met
       again are *deferred* while distinct groups still compete. Selecting
       one side of an explicit conflict immediately attempts to co-pack its
       admitted counterpart (``conflict_pair_preserved``) — ahead of any
       lower-ranked redundancy — under the same hard budgets.
    2. **Fill** (rank order): every remaining admitted candidate, deferred
       siblings included, fills leftover capacity (``diversity_fill``).
       Known-root siblings are crowd-out protected, never forbidden.

    Budget discipline matches ``recall._enforce_semantic_budget`` exactly —
    the item budget ends selection; an item that would overflow the byte or
    token budget is skipped while smaller lower-ranked items still fit —
    measured with the same rendered accounting the semantic packet path
    uses. Omission accounting reconciles mechanically: every admitted
    candidate is either selected (in ``reasons``) or counted once in
    ``omitted`` with this precedence: an unselected item whose explicit
    conflict opposite was selected is ``conflict_counterpart_budget``; an
    unselected item deferred as a known-root sibling is
    ``redundant_known_root``; everything else is ``budget``.
    """
    ranked = list(candidates)
    by_id = {cand.item_id: cand for cand in ranked}
    group_of = _known_root_groups(ranked, relations)
    conflicts = _conflict_adjacency(ranked, relations)
    # Conflict partners are attempted in rank order (deterministic when
    # budget permits only some of several partners).
    rank_position = {cand.item_id: index for index, cand in enumerate(ranked)}
    partners_by_id: dict[UUID, list[UUID]] = {
        cand.item_id: sorted(
            conflicts.get(cand.item_id, ()), key=lambda other: rank_position[other]
        )
        for cand in ranked
    }

    selected: set[UUID] = set()
    reasons: dict[UUID, PackingReason] = {}
    represented: set[UUID] = set()
    deferred: set[UUID] = set()
    bytes_used = 0
    tokens_used = 0

    def item_room() -> bool:
        return item_budget is None or len(selected) < item_budget

    def fits(cand: PackCandidate) -> bool:
        cand_bytes, cand_tokens = _rendered_size(cand.content)
        if byte_budget is not None and bytes_used + cand_bytes > byte_budget:
            return False
        return not (token_budget is not None and tokens_used + cand_tokens > token_budget)

    def take(cand: PackCandidate, reason: PackingReason) -> None:
        nonlocal bytes_used, tokens_used
        cand_bytes, cand_tokens = _rendered_size(cand.content)
        selected.add(cand.item_id)
        reasons[cand.item_id] = reason
        represented.add(group_of[cand.item_id])
        bytes_used += cand_bytes
        tokens_used += cand_tokens

    # Phase 1 — representation + conflict co-pack, in rank order. Deferral is
    # marked before any budget check: a known-root sibling met while its
    # group is already represented was deprioritized by the diversity
    # doctrine itself — including when a conflict co-pack spent the budget
    # before the sibling's turn — so its omission is redundancy deferral,
    # never a bare budget fact.
    for cand in ranked:
        if group_of[cand.item_id] in represented:
            deferred.add(cand.item_id)
            continue
        if not item_room():
            break
        if not fits(cand):
            # Skip-not-break: an oversized representative is passed over;
            # a smaller group member met later can still represent it.
            continue
        take(cand, "ranked")
        for partner_id in partners_by_id[cand.item_id]:
            if not item_room():
                break
            if partner_id in selected:
                continue
            partner = by_id[partner_id]
            if fits(partner):
                take(partner, "conflict_pair_preserved")

    # Phase 2 — deterministic fill from the existing ranked order.
    for cand in ranked:
        if not item_room():
            break
        if cand.item_id in selected:
            continue
        if fits(cand):
            take(cand, "diversity_fill")

    omitted: dict[str, int] = {
        OMIT_BUDGET: 0,
        OMIT_REDUNDANT_KNOWN_ROOT: 0,
        OMIT_CONFLICT_COUNTERPART_BUDGET: 0,
    }
    for cand in ranked:
        if cand.item_id in selected:
            continue
        if any(partner in selected for partner in conflicts.get(cand.item_id, ())):
            omitted[OMIT_CONFLICT_COUNTERPART_BUDGET] += 1
        elif cand.item_id in deferred:
            omitted[OMIT_REDUNDANT_KNOWN_ROOT] += 1
        else:
            omitted[OMIT_BUDGET] += 1
    omitted = {key: count for key, count in omitted.items() if count}

    pairs = {
        frozenset((cand.item_id, partner_id))
        for cand in ranked
        for partner_id in conflicts.get(cand.item_id, ())
    }
    preserved = sum(1 for pair in pairs if pair <= selected)

    # Rendered order is the rank order restricted to selected items.
    rendered = [cand.item_id for cand in ranked if cand.item_id in selected]
    return PackingResult(
        selected=rendered,
        reasons=reasons,
        omitted=omitted,
        conflict_pairs_preserved=preserved,
    )


__all__ = [
    "OMIT_BUDGET",
    "OMIT_CONFLICT_COUNTERPART_BUDGET",
    "OMIT_REDUNDANT_KNOWN_ROOT",
    "PackCandidate",
    "PackingReason",
    "PackingRelations",
    "PackingResult",
    "RECALL_PACKING_VERSION",
    "load_packing_relations",
    "pack_admitted_candidates",
]
