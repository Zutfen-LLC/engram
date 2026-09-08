"""Conflict-preserving, diversity-aware candidate packing tests (issue #192).

The pure packer (``engram.recall_packing.pack_admitted_candidates``) is
pinned first, DB-free: known-root crowd-out prevention, deterministic
representatives, conflict co-pack precedence over redundant siblings, hard
budget boundaries, and mechanical omission reconciliation.

The integration half runs the real shadow-comparison surface against a live
PostgreSQL (skips without one, mirroring tests/test_recall_profile_semantic.py)
and pins the #192 contracts end to end: packing changes selection only —
never relevance/utility/admission/evidence — withheld or inaccessible
counterparts are neither resurrected nor identity-leaked, cross-tenant
relations cannot group, the packing relation load is one bounded query with
no provider call, the comparison stays read-only, and the legacy packet is
untouched while governed/exploratory remain uncertified and unservable.

Required-test numbering below refers to the issue #192 test matrix.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4, uuid5

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text

from engram import recall_packing
from engram.api.app import create_app
from engram.db import get_session
from engram.recall_packing import (
    RECALL_PACKING_VERSION,
    PackCandidate,
    PackingRelations,
    pack_admitted_candidates,
)
from tests.test_recall_profile_semantic import (  # noqa: F401
    _candidate_packet,
    _clean_db,
    _db_ok,
    _enable_tenant_shadow_policy,
    _enable_v2_selection,
    _get_test_session,
    _link_items,
    _make_expansion_only,
    _mk_tunnel,
    _patch_embeddings,
    _persist_v2_row,
    _recall_counts,
    _recall_log_count,
    _remember,
    _reset_embedding_provider,
    _shadow_compare,
    _skip_without_db,
    _test_engine,
    _update_item,
)


@pytest.fixture
def app():
    app = create_app()
    app.dependency_overrides[get_session] = _get_test_session
    return app


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---- pure packer fixtures ------------------------------------------------------

_PACK_NS = UUID("6f1922e0-0000-4000-8000-000000019200")


def _cid(name: str) -> UUID:
    return uuid5(_PACK_NS, name)


def _cand(
    name: str,
    content: str = "content",
    *,
    conflicts_with: UUID | None = None,
) -> PackCandidate:
    return PackCandidate(
        item_id=_cid(name),
        content=content,
        conflicts_with=conflicts_with,
    )


def _derived_family(*ids: UUID) -> PackingRelations:
    """One explicit derivation family: every member derives from the first."""
    root, *members = ids
    links: dict[UUID, frozenset[UUID]] = {root: frozenset(members)}
    for member in members:
        links[member] = frozenset({root})
    return PackingRelations(derived_links=links)


def _pack(
    candidates: list[PackCandidate],
    *,
    relations: PackingRelations | None = None,
    byte_budget: int | None = None,
    token_budget: int | None = None,
    item_budget: int | None = None,
) -> recall_packing.PackingResult:
    return pack_admitted_candidates(
        candidates,
        relations=relations or PackingRelations.empty(),
        byte_budget=byte_budget,
        token_budget=token_budget,
        item_budget=item_budget,
    )


def _assert_reconciles(
    candidates: list[PackCandidate], result: recall_packing.PackingResult
) -> None:
    """Required test 18: every admitted candidate is selected or omitted once."""
    assert len(result.selected) == len(set(result.selected))
    assert set(result.reasons) == set(result.selected)
    assert len(result.selected) + sum(result.omitted.values()) == len(candidates)


# ---- pure packer: known-root diversity (required tests 1-5, 30) ----------------


def test_known_root_family_yields_to_distinct_context() -> None:
    """Required test 1: under an item budget of 2, three same-root siblings
    (one explicit ``derived_from`` family) plus one distinct item select the
    family representative + the distinct item — never two same-root
    siblings."""
    a1 = _cand("a1", "alpha one")
    a2 = _cand("a2", "alpha two")
    a3 = _cand("a3", "alpha three")
    distinct = _cand("distinct", "distinct fact")
    relations = _derived_family(a1.item_id, a2.item_id, a3.item_id)
    result = _pack([a1, a2, a3, distinct], relations=relations, item_budget=2)

    assert result.selected == [a1.item_id, distinct.item_id]
    assert result.reasons[a1.item_id] == "ranked"
    assert result.reasons[distinct.item_id] == "ranked"
    assert result.omitted == {"redundant_known_root": 2}
    _assert_reconciles([a1, a2, a3, distinct], result)


def test_representative_selection_is_deterministic_by_rank() -> None:
    """Required test 2 (+30): the family representative is the highest-ranked
    member — list position is the rank the separated ranking produced — and
    identical inputs always produce identical outputs."""
    a1 = _cand("a1", "alpha one")
    a2 = _cand("a2", "alpha two")
    a3 = _cand("a3", "alpha three")
    distinct = _cand("distinct", "distinct fact")
    relations = _derived_family(a1.item_id, a2.item_id, a3.item_id)

    reordered = _pack([a2, a1, a3, distinct], relations=relations, item_budget=2)
    assert reordered.selected == [a2.item_id, distinct.item_id]

    original = _pack([a1, a2, a3, distinct], relations=relations, item_budget=2)
    again = _pack([a1, a2, a3, distinct], relations=relations, item_budget=2)
    assert again.selected == original.selected
    assert again.reasons == original.reasons
    assert again.omitted == original.omitted
    assert again.conflict_pairs_preserved == original.conflict_pairs_preserved
    assert again.summary() == original.summary()


def test_same_root_siblings_fill_remaining_space() -> None:
    """Required test 3: siblings are crowd-out protected, not forbidden —
    after distinct groups are represented they fill leftover capacity."""
    a1 = _cand("a1", "alpha one")
    a2 = _cand("a2", "alpha two")
    a3 = _cand("a3", "alpha three")
    distinct = _cand("distinct", "distinct fact")
    relations = _derived_family(a1.item_id, a2.item_id, a3.item_id)
    result = _pack([a1, a2, a3, distinct], relations=relations, item_budget=3)

    # Rendered order stays the rank order: the fill sibling ranks above the
    # distinct item even though it was selected after it.
    assert result.selected == [a1.item_id, a2.item_id, distinct.item_id]
    assert result.reasons[a1.item_id] == "ranked"
    assert result.reasons[distinct.item_id] == "ranked"
    assert result.reasons[a2.item_id] == "diversity_fill"
    assert result.omitted == {"redundant_known_root": 1}
    _assert_reconciles([a1, a2, a3, distinct], result)


def test_similar_items_without_root_identity_are_not_grouped() -> None:
    """Required test 4: no explicit root fact (no derived edge) means
    *unknown* — never grouped, never labeled redundant; ordinary rank/budget
    behavior applies even for identical canonical content (equal
    ``content_hash``), which is not a v1 root signal (see
    ``test_packer_input_surface_excludes_content_identity`` and the
    same-hash/different-scope integration regression)."""
    x = _cand("x", "looks very similar indeed")
    y = _cand("y", "looks very similar too!")
    result = _pack([x, y], item_budget=2)

    assert result.selected == [x.item_id, y.item_id]
    assert result.omitted == {}


def test_packer_input_surface_excludes_content_identity() -> None:
    """The packer's input surface carries no content-identity fact at all:
    ``PackCandidate`` is exactly ``{item_id, content, conflicts_with}``, so
    canonical content equality (``content_hash``) cannot even reach the
    grouping logic — known-root diversity is explicit ``derived_from``
    connectivity only."""
    assert set(PackCandidate.__dataclass_fields__) == {"item_id", "content", "conflicts_with"}


def test_derivation_chains_form_transitive_families() -> None:
    """Redundancy inputs compose via union-find over explicit ``derived_from``
    edges: a two-edge chain (A derives from B, B derives from C) is one
    family of three; a merely similar item with no explicit relation stays
    separate."""
    a = _cand("a", "derived child")
    b = _cand("b", "derived parent")
    c = _cand("c", "derived grandparent")
    similar = _cand("similar", "derived parent but no relation")
    relations = PackingRelations(
        derived_links={
            a.item_id: frozenset({b.item_id}),
            b.item_id: frozenset({a.item_id, c.item_id}),
            c.item_id: frozenset({b.item_id}),
        }
    )
    result = _pack([a, b, c, similar], relations=relations, item_budget=2)

    assert result.selected == [a.item_id, similar.item_id]
    assert result.omitted == {"redundant_known_root": 2}
    _assert_reconciles([a, b, c, similar], result)


def test_relation_metadata_changes_selection_only() -> None:
    """Required test 5 (+20): adding an explicit root relation changes which
    items are selected — and the packing result carries nothing but ids,
    reasons, and counts: no score, relevance, admission, or evidence surface
    exists on it to mutate."""
    a1 = _cand("a1", "alpha one")
    a2 = _cand("a2", "alpha two")
    distinct = _cand("distinct", "distinct fact")

    unlinked = _pack([a1, a2, distinct], item_budget=2)
    assert unlinked.selected == [a1.item_id, a2.item_id]

    relations = PackingRelations(
        derived_links={
            a1.item_id: frozenset({a2.item_id}),
            a2.item_id: frozenset({a1.item_id}),
        }
    )
    linked = _pack([a1, a2, distinct], relations=relations, item_budget=2)
    assert linked.selected == [a1.item_id, distinct.item_id]
    assert linked.omitted == {"redundant_known_root": 1}

    assert set(linked.__dataclass_fields__) == {
        "selected",
        "reasons",
        "omitted",
        "conflict_pairs_preserved",
    }


# ---- pure packer: conflict preservation (required tests 6-8, 11) ---------------


def test_conflict_pair_preserved_over_redundant_sibling() -> None:
    """Required test 6: both sides of an explicit conflict are selected even
    though a known-redundant sibling (explicit derivation family member)
    ranks between them."""
    side_a = _cand("side-a", "claim alpha")
    sibling = _cand("sibling", "claim alpha restated")
    side_c = _cand("side-c", "claim contra", conflicts_with=side_a.item_id)
    relations = _derived_family(side_a.item_id, sibling.item_id)
    result = _pack([side_a, sibling, side_c], relations=relations, item_budget=2)

    assert result.selected == [side_a.item_id, side_c.item_id]
    assert result.reasons[side_a.item_id] == "ranked"
    assert result.reasons[side_c.item_id] == "conflict_pair_preserved"
    assert result.omitted == {"redundant_known_root": 1}
    assert result.conflict_pairs_preserved == 1
    _assert_reconciles([side_a, sibling, side_c], result)


def test_conflict_preservation_is_rank_symmetric() -> None:
    """Required test 7: reversing which side ranks higher never erases the
    lower-ranked side; the rendered order stays the rank order."""
    side_a = _cand("side-a", "claim alpha")
    side_c = _cand("side-c", "claim contra", conflicts_with=side_a.item_id)
    result = _pack([side_c, side_a], item_budget=2)

    assert result.selected == [side_c.item_id, side_a.item_id]
    assert result.reasons[side_c.item_id] == "ranked"
    assert result.reasons[side_a.item_id] == "conflict_pair_preserved"
    assert result.conflict_pairs_preserved == 1


def test_conflict_preservation_emits_only_packing_reasons() -> None:
    """Required test 8 (packer facet): preservation is pure selection — the
    result's only per-item output is the bounded ``packing_reason``
    vocabulary; epistemic/evidence/admission fields are not packer inputs or
    outputs at all (the integration tests pin them end to end)."""
    side_a = _cand("side-a", "claim alpha")
    side_c = _cand("side-c", "claim contra", conflicts_with=side_a.item_id)
    result = _pack([side_a, side_c], item_budget=2)
    assert set(result.reasons.values()) <= {"ranked", "conflict_pair_preserved", "diversity_fill"}
    assert result.summary()["version"] == RECALL_PACKING_VERSION


def test_conflict_counterpart_budget_omission_is_bounded_and_honest() -> None:
    """Required test 11: when the hard byte budget cannot fit both sides the
    packet stays within budget and records ``conflict_counterpart_budget``
    instead of pretending the conflict was resolved."""
    side_a = _cand("side-a", "aaaa")
    sibling = _cand("sibling", "ss")
    side_c = _cand("side-c", "cc", conflicts_with=side_a.item_id)
    relations = _derived_family(side_a.item_id, sibling.item_id)
    result = _pack([side_a, sibling, side_c], relations=relations, byte_budget=5, item_budget=3)

    # 4 bytes fit; neither 2-byte item fits alongside (4+2 > 5).
    assert result.selected == [side_a.item_id]
    assert result.omitted == {
        "conflict_counterpart_budget": 1,
        "redundant_known_root": 1,
    }
    used = sum(
        cand.content.encode().__len__()
        for cand in [side_a, sibling, side_c]
        if cand.item_id in set(result.selected)
    )
    assert used <= 5
    _assert_reconciles([side_a, sibling, side_c], result)


def test_multiple_conflict_partners_co_packed_in_rank_order() -> None:
    """Several explicit counterparts of one selected side are co-packed in
    deterministic rank order while budget permits; each preserved pair
    counts once."""
    center = _cand("center", "center claim")
    first = _cand("first", "counter one", conflicts_with=center.item_id)
    second = _cand("second", "counter two", conflicts_with=center.item_id)
    result = _pack([center, first, second], item_budget=3)

    assert result.selected == [center.item_id, first.item_id, second.item_id]
    assert result.reasons[first.item_id] == "conflict_pair_preserved"
    assert result.reasons[second.item_id] == "conflict_pair_preserved"
    assert result.conflict_pairs_preserved == 2


# ---- pure packer: budget correctness (required tests 15-18) --------------------


def test_item_budget_exact_boundary_is_deterministic() -> None:
    """Required test 15: at the exact boundary the budget is fully used and
    the drop beyond it is the deterministically last-ranked item."""
    items = [_cand(f"i{n}", f"c{n}") for n in range(4)]
    exact = _pack(items, item_budget=4)
    assert exact.selected == [item.item_id for item in items]

    one_less = _pack(items, item_budget=3)
    assert one_less.selected == [item.item_id for item in items[:3]]
    assert one_less.omitted == {"budget": 1}
    assert _pack(items, item_budget=3).selected == one_less.selected
    _assert_reconciles(items, one_less)


def test_byte_budget_exact_boundary_never_overruns() -> None:
    """Required test 16: an exact byte boundary selects everything; one byte
    less deterministically skips the item(s) that no longer fit while smaller
    lower-ranked items still fill — and the budget is never exceeded."""
    b1 = _cand("b1", "aaaa")  # 4 bytes
    b2 = _cand("b2", "bb")  # 2 bytes
    b3 = _cand("b3", "cc")  # 2 bytes
    exact = _pack([b1, b2, b3], byte_budget=8)
    assert exact.selected == [b1.item_id, b2.item_id, b3.item_id]

    tight = _pack([b1, b2, b3], byte_budget=7)
    assert tight.selected == [b1.item_id, b2.item_id]
    assert tight.omitted == {"budget": 1}

    # An oversized representative is skipped, not breaked on: smaller items
    # behind it still fill the packet.
    oversized = _cand("oversized", "x" * 10)
    skip = _pack([oversized, b2, b3], byte_budget=5)
    assert skip.selected == [b2.item_id, b3.item_id]
    used = sum(
        cand.content.encode().__len__()
        for cand in [oversized, b2, b3]
        if cand.item_id in set(skip.selected)
    )
    assert used <= 5
    _assert_reconciles([oversized, b2, b3], skip)


def test_token_budget_boundary_is_deterministic() -> None:
    """Required test 17: token budgets use the rendered heuristic
    (``max(1, bytes // 4)``) with the same skip-not-break discipline."""
    big = _cand("big", "x" * 8)  # 2 tokens
    mid = _cand("mid", "y" * 4)  # 1 token
    tiny = _cand("tiny", "z")  # 1 token
    exact = _pack([big, mid, tiny], token_budget=4)
    assert exact.selected == [big.item_id, mid.item_id, tiny.item_id]

    tight = _pack([big, mid, tiny], token_budget=3)
    assert tight.selected == [big.item_id, mid.item_id]
    assert tight.omitted == {"budget": 1}
    _assert_reconciles([big, mid, tiny], tight)


def test_summary_is_bounded_counts_only() -> None:
    """Diagnostics boundedness: the packing summary is a fixed key set of
    counts — no ids, no counterpart identities, no rejected content."""
    side_a = _cand("side-a", "claim alpha")
    sibling = _cand("sibling", "restated")
    side_c = _cand("side-c", "contra", conflicts_with=side_a.item_id)
    relations = _derived_family(side_a.item_id, sibling.item_id)
    result = _pack([side_a, sibling, side_c], relations=relations, item_budget=2)
    summary = result.summary()

    assert set(summary) == {"version", "selected_count", "conflict_pairs_preserved", "omitted"}
    assert summary["version"] == RECALL_PACKING_VERSION
    assert summary["selected_count"] == 2
    assert summary["conflict_pairs_preserved"] == 1
    assert summary["omitted"] == {"redundant_known_root": 1}
    serialized = json.dumps(summary)
    for cand in [side_a, sibling, side_c]:
        assert str(cand.item_id) not in serialized
        assert cand.content not in serialized


# ---- integration fixtures ------------------------------------------------------


@pytest.fixture(autouse=True)
async def _clean_exp192_rows():
    """The shared ``_clean_db`` only sweeps exp190-% fixtures; this module's
    foreign principals/tenants (exp192-%) get their own sweep after each
    test, cascading any items/edges recorded against them."""
    yield
    if not await _db_ok():
        return
    async with _test_engine.begin() as conn:
        # Clear conflict linkages that point at this module's foreign items
        # before removing them (the FK has no cascade).
        await conn.execute(
            text(
                "UPDATE memory_items SET conflicts_with_item_id = NULL "
                "WHERE conflicts_with_item_id IN ("
                "  SELECT id FROM memory_items WHERE principal_id IN "
                "  (SELECT id FROM principals WHERE name LIKE 'exp192-%'))"
            )
        )
        await conn.execute(
            text(
                "DELETE FROM memory_items WHERE principal_id IN "
                "(SELECT id FROM principals WHERE name LIKE 'exp192-%')"
            )
        )
        await conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'exp192-%'"))
        await conn.execute(text("DELETE FROM principals WHERE name LIKE 'exp192-%'"))


async def _seed_packing_item(
    client: Any,
    content: str,
    *,
    importance: float = 0.5,
    root_hash: str | None = None,
    qualified: bool = True,
    **payload: Any,
) -> dict[str, Any]:
    """A live proposal for the candidate packets, V2-qualified by default.

    ``root_hash`` rewrites the durable content-hash identity BEFORE the V2
    row is persisted. It exists only for the same-hash/different-scope
    negative regression: equal canonical content must NOT group candidates
    (the dedup identity is scoped over tenant/workspace/principal, and
    content equality is not provenance). Known-root families in these tests
    are built exclusively from explicit ``derived_from`` edges — the only
    mechanically-known root identity v1 recognizes. ``qualified=False``
    leaves the item without a V2 row so governed recall withholds it.
    """
    item = await _remember(
        client, content, source_type="extraction", importance=importance, **payload
    )
    if root_hash is not None:
        await _update_item(item["id"], content_hash=root_hash)
    if qualified:
        await _persist_v2_row(item["id"])
    return item


def _packet_ids(packet: dict[str, Any]) -> list[str]:
    return [item["id"] for item in packet["items"]]


def _immutable_fields(item: dict[str, Any]) -> dict[str, Any]:
    """The per-item fields packing must never touch (#186/#188/#190 facts)."""
    return {
        "score": round(item["score"], 4),
        "relevance_score": item["relevance_score"],
        "utility_score": round(item["utility_score"], 6),
        "epistemic_state": item["epistemic_state"],
        "warning_codes": item["warning_codes"],
        "admission": item["admission"],
        "evidence": item["evidence"],
        "relationship": item.get("relationship"),
    }


async def _recall_semantic(client: Any) -> dict[str, Any]:
    resp = await client.post("/v1/recall", json={"mode": "semantic", "query": "semantic query"})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---- integration: known-root diversity end to end (required tests 1, 2, 30) ----


async def test_known_root_crowdout_prevented_end_to_end(client, monkeypatch) -> None:
    """Required test 1, before/after: rank-truncation (the legacy packet)
    fills the budget with same-root siblings; the governed candidate packet
    selects the family representative plus the distinct item and reports the
    deferred siblings."""
    await _skip_without_db()
    from engram.config import settings

    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    a1 = await _seed_packing_item(client, "semantic target alpha one", importance=0.9)
    a2 = await _seed_packing_item(client, "semantic target alpha two", importance=0.8)
    a3 = await _seed_packing_item(client, "semantic target alpha three", importance=0.7)
    distinct = await _seed_packing_item(client, "semantic query distinct fact", importance=0.6)
    # One explicit derivation family: a1 is the root both siblings derive from.
    await _link_items(a1["id"], a2["id"], "derived_from")
    await _link_items(a1["id"], a3["id"], "derived_from")
    family = {a1["id"], a2["id"], a3["id"]}

    shadow = await _shadow_compare(client, profiles=["governed"], item_budget=2)
    legacy, governed = shadow["legacy"], shadow["candidates"][0]

    # Before: the legacy packet spends both slots on the redundant family.
    assert len({item["id"] for item in legacy["items"]} & family) == 2
    # After: representative + distinct context, sibling deferral reported.
    assert _packet_ids(governed) == [a1["id"], distinct["id"]]
    assert governed["items"][0]["packing_reason"] == "ranked"
    assert governed["items"][1]["packing_reason"] == "ranked"
    assert governed["packing"]["version"] == RECALL_PACKING_VERSION
    assert governed["packing"]["selected_count"] == 2
    assert governed["packing"]["omitted"] == {"redundant_known_root": 2}
    assert a2["id"] not in _packet_ids(governed) and a3["id"] not in _packet_ids(governed)


async def test_packing_is_deterministic_and_counter_blind_end_to_end(client, monkeypatch) -> None:
    """Required tests 2, 22, 30: fixed state/config reproduces selected ids,
    rendered order, and packing metadata — and recall/exposure counters do
    not move the result (no popularity loop)."""
    await _skip_without_db()
    from engram.config import settings

    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    one = await _seed_packing_item(client, "semantic target repro one", importance=0.9)
    two = await _seed_packing_item(client, "semantic target repro two", importance=0.8)
    await _seed_packing_item(client, "semantic query repro distinct", importance=0.7)
    await _link_items(one["id"], two["id"], "derived_from")

    first = await _candidate_packet(client, item_budget=2)
    ids = _packet_ids(first)
    async with _test_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE memory_items SET recall_count = recall_count + 7, "
                "startup_recall_count = startup_recall_count + 5, "
                "last_recalled_at = now() "
                "WHERE id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": ids},
        )
    second = await _candidate_packet(client, item_budget=2)

    assert _packet_ids(second) == ids
    assert second["packing"] == first["packing"]
    assert [item["packing_reason"] for item in second["items"]] == [
        item["packing_reason"] for item in first["items"]
    ]
    assert second["packing"]["omitted"] == {"redundant_known_root": 1}


# ---- integration: conflict preservation end to end (required tests 6-8) --------


async def test_conflict_pair_preserved_end_to_end(client, monkeypatch) -> None:
    """Required tests 6, 7, 8, before/after: two admitted explicitly
    conflicting items are co-packed even though a known-redundant sibling
    ranks between them; the legacy packet erases the lower side. Reversing
    which side ranks higher preserves the pair symmetrically, and neither
    side's scores/admission/evidence move."""
    await _skip_without_db()
    from engram.config import settings

    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    side_a = await _seed_packing_item(client, "semantic target conflict alpha", importance=0.9)
    sibling = await _seed_packing_item(client, "semantic target conflict sibling", importance=0.85)
    side_c = await _seed_packing_item(
        client, "semantic query conflict contra", importance=0.5, qualified=False
    )
    await _link_items(side_a["id"], sibling["id"], "derived_from")
    # The linkage must exist before the V2 rows persist: conflict state feeds
    # the #158 decision hash, so a later linkage would stale the rows.
    await _update_item(side_c["id"], conflicts_with_item_id=side_a["id"])
    await _persist_v2_row(side_c["id"])

    shadow = await _shadow_compare(client, profiles=["governed"], item_budget=2)
    legacy, governed = shadow["legacy"], shadow["candidates"][0]

    # Before: rank-truncation keeps the sibling and erases the conflict side.
    assert side_c["id"] not in _packet_ids(legacy)
    # After: the pair is preserved, the sibling defers to it.
    assert _packet_ids(governed) == [side_a["id"], side_c["id"]]
    assert governed["items"][0]["packing_reason"] == "ranked"
    assert governed["items"][1]["packing_reason"] == "conflict_pair_preserved"
    assert governed["packing"]["conflict_pairs_preserved"] == 1
    assert governed["packing"]["omitted"] == {"redundant_known_root": 1}

    # Same state, wider budget: everything fits — the co-packed item's
    # immutable fields are identical whether it was selected by rank or by
    # the conflict obligation (packing changed selection only).
    wide = await _candidate_packet(client, item_budget=3)
    by_id = {item["id"]: item for item in wide["items"]}
    assert _packet_ids(wide) == [side_a["id"], sibling["id"], side_c["id"]]
    packed_by_obligation = next(item for item in governed["items"] if item["id"] == side_c["id"])
    assert _immutable_fields(packed_by_obligation) == _immutable_fields(by_id[side_c["id"]])

    # Required test 7: reverse the ranks — the (now lower-ranked) alpha side
    # is still preserved.
    await _update_item(side_a["id"], importance=0.3)
    await _update_item(side_c["id"], importance=0.95)
    reversed_packet = await _candidate_packet(client, item_budget=2)
    assert set(_packet_ids(reversed_packet)) == {side_a["id"], side_c["id"]}
    reasons = {item["id"]: item["packing_reason"] for item in reversed_packet["items"]}
    assert reasons[side_c["id"]] == "ranked"
    assert reasons[side_a["id"]] == "conflict_pair_preserved"
    assert reversed_packet["packing"]["conflict_pairs_preserved"] == 1


async def test_conflict_fields_unchanged_by_packing_decisions(client, monkeypatch) -> None:
    """Required tests 5, 19, 20, 21: how an item is *selected* — by rank, by
    conflict obligation, or crowded out entirely — changes none of its own
    facts. Identical DB state is evaluated at three budgets so the same
    items land in different packing slots; their scores, relevance,
    utility, admission, evidence, epistemic state, and warning codes must
    be identical across all three.

    (A true add/remove-linkage before/after is impossible by design: the
    conflict linkage feeds the #158 V2 decision hash, so changing it
    re-admits through a different — stale — V2 row. The pure
    ``test_relation_metadata_changes_selection_only`` pins the
    relation-metadata facet structurally: the packing result has no score
    or evidence surface to mutate.)"""
    await _skip_without_db()
    from engram.config import settings

    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    side_a = await _seed_packing_item(client, "semantic target immutable alpha", importance=0.9)
    side_c = await _seed_packing_item(
        client, "semantic query immutable contra", importance=0.5, qualified=False
    )
    filler = await _seed_packing_item(
        client, "semantic query immutable filler", importance=0.7, qualified=False
    )
    # Linkage first, then the V2 rows: conflict state feeds the #158
    # decision hash (a later linkage would stale the row and misattribute
    # the selection change to admission instead of packing).
    await _update_item(side_c["id"], conflicts_with_item_id=side_a["id"])
    await _persist_v2_row(side_c["id"])
    await _persist_v2_row(filler["id"])

    # Budget 3: everything selected by rank (side_a 1st, filler 2nd, side_c
    # 3rd — the pair also co-packs, but every group fits anyway).
    wide = await _candidate_packet(client, item_budget=3)
    assert _packet_ids(wide) == [side_a["id"], filler["id"], side_c["id"]]
    wide_fields = {item["id"]: _immutable_fields(item) for item in wide["items"]}

    # Budget 2: the conflict pair survives (side_c co-packed); the
    # unlinked, higher-ranked filler loses its slot to the obligation.
    packed = await _candidate_packet(client, item_budget=2)
    assert set(_packet_ids(packed)) == {side_a["id"], side_c["id"]}
    reasons = {item["id"]: item["packing_reason"] for item in packed["items"]}
    assert reasons[side_a["id"]] == "ranked"
    assert reasons[side_c["id"]] == "conflict_pair_preserved"
    assert packed["packing"]["omitted"] == {"budget": 1}

    # Budget 1: only side_a fits; side_c is omitted with the bounded
    # counterpart-budget reason (never a resolution) and the filler by
    # plain budget.
    narrow = await _candidate_packet(client, item_budget=1)
    assert _packet_ids(narrow) == [side_a["id"]]
    assert narrow["packing"]["omitted"] == {"conflict_counterpart_budget": 1, "budget": 1}

    # The packing slot never touched an item's own facts.
    for packet in (packed, narrow):
        for item in packet["items"]:
            assert _immutable_fields(item) == wide_fields[item["id"]]


async def test_withheld_conflict_counterpart_not_resurrected(client, monkeypatch) -> None:
    """Required test 9: a counterpart the exact V2 surface withholds stays
    withheld — packing never resurrects it and reports no counterpart
    obligation for an unadmitted item."""
    await _skip_without_db()
    from engram.config import settings

    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    side_a = await _seed_packing_item(
        client, "semantic target withheld counterpart", importance=0.9
    )
    distinct = await _seed_packing_item(client, "semantic query withheld distinct", importance=0.6)
    withheld = await _seed_packing_item(
        client, "semantic query withheld contra", importance=0.5, qualified=False
    )
    await _update_item(withheld["id"], conflicts_with_item_id=side_a["id"])

    governed = await _candidate_packet(client, item_budget=2)
    assert _packet_ids(governed) == [side_a["id"], distinct["id"]]
    # Withheld by admission, not by packing: the diagnostic owns it.
    diagnostics = {d["item_id"] for d in governed["admission_diagnostics"]}
    assert withheld["id"] in diagnostics
    assert governed["packing"]["omitted"] == {}


async def test_inaccessible_conflict_counterpart_not_leaked(client, monkeypatch) -> None:
    """Required tests 10, 24: a counterpart the caller cannot read (another
    principal's private item) is neither selected nor identity-leaked — its
    id appears nowhere in the response and selection is identical to the
    no-linkage baseline."""
    await _skip_without_db()
    from engram.config import settings

    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    side_a = await _seed_packing_item(
        client, "semantic target private counterpart", importance=0.9, qualified=False
    )
    distinct = await _seed_packing_item(client, "semantic query private distinct", importance=0.6)
    async with _test_engine.begin() as conn:
        tenant_id = await conn.scalar(
            text("SELECT tenant_id::text FROM memory_items WHERE id = :id"),
            {"id": side_a["id"]},
        )
        foreign_principal = str(uuid4())
        await conn.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES (:pid, :tid, :pname, 'agent')"
            ),
            {
                "pid": foreign_principal,
                "tid": tenant_id,
                "pname": f"exp192-private-{foreign_principal[:8]}",
            },
        )
        foreign_id = str(uuid4())
        await conn.execute(
            text(
                "INSERT INTO memory_items (id, tenant_id, principal_id, content, "
                "content_hash, kind, visibility, review_status) "
                "VALUES (:id, :tid, :pid, 'private counterpart secret', :h, "
                "'fact', 'private', 'proposed')"
            ),
            {"id": foreign_id, "tid": tenant_id, "pid": foreign_principal, "h": f"h-{uuid4()}"},
        )
        # The linkage exists before side_a's V2 row persists (conflict state
        # feeds the #158 decision hash), yet it can never create a visible
        # obligation: the counterpart is not this caller's to read.
        await conn.execute(
            text("UPDATE memory_items SET conflicts_with_item_id = :fid WHERE id = :id"),
            {"fid": foreign_id, "id": side_a["id"]},
        )
    await _persist_v2_row(side_a["id"])

    governed = await _candidate_packet(client, item_budget=2)
    # Selection is exactly what a no-linkage packet would serve, and the
    # inaccessible counterpart is nowhere in the payload.
    assert _packet_ids(governed) == [side_a["id"], distinct["id"]]
    assert governed["packing"]["omitted"] == {}
    assert governed["packing"]["conflict_pairs_preserved"] == 0
    assert foreign_id not in json.dumps(governed)


# ---- integration: direct + expanded candidates (required tests 12-14) ----------


async def test_conflict_preservation_direct_vs_graph_expanded(client, monkeypatch) -> None:
    """Required tests 12, 14: an explicit ``contradicts`` edge preserves a
    direct item and its graph-expanded (expansion-only) counterpart, and the
    #190 origin-merge relationship block survives packing unchanged."""
    await _skip_without_db()
    from engram.config import settings

    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    direct = await _seed_packing_item(
        client, "semantic target direct conflict side", importance=0.9
    )
    # A second admitted direct seed whose supports edge points AT the
    # conflict side, so the direct item carries the #190 semantic+graph
    # merged-origin block through enrichment.
    supporting_seed = await _seed_packing_item(
        client, "semantic target supporting seed", importance=0.3
    )
    counterpart = await _seed_packing_item(
        client, "graph counterpart of direct side", importance=0.5
    )
    await _make_expansion_only(counterpart["id"])
    await _link_items(supporting_seed["id"], direct["id"], "supports")
    await _link_items(direct["id"], counterpart["id"], "contradicts")

    governed = await _candidate_packet(client, item_budget=3)
    assert set(_packet_ids(governed)) == {direct["id"], counterpart["id"], supporting_seed["id"]}
    reasons = {item["id"]: item["packing_reason"] for item in governed["items"]}
    assert reasons[direct["id"]] == "ranked"
    assert reasons[counterpart["id"]] == "conflict_pair_preserved"
    assert reasons[supporting_seed["id"]] == "ranked"
    assert governed["packing"]["conflict_pairs_preserved"] == 1
    assert governed["packing"]["omitted"] == {}

    # Required test 14: the #190 origin-merge blocks survive packing
    # untouched — the counterpart is graph-origin-only, the direct hit
    # carries its merged semantic+graph origins with the supporting edge.
    expanded_item = next(item for item in governed["items"] if item["id"] == counterpart["id"])
    relationship = expanded_item["relationship"]
    assert relationship["version"] == "relationship-relevance-v1"
    assert list(relationship["origins"]) == ["graph"]
    direct_item = next(item for item in governed["items"] if item["id"] == direct["id"])
    direct_relationship = direct_item["relationship"]
    assert list(direct_relationship["origins"]) == ["semantic", "graph"]
    assert direct_relationship["graph_edge_types"] == ["supports"]

    # A tighter budget drops the lowest-ranked supporting seed by plain
    # budget while the pair stays preserved — and neither merged-origin
    # block nor any immutable field moved.
    tight = await _candidate_packet(client, item_budget=2)
    assert set(_packet_ids(tight)) == {direct["id"], counterpart["id"]}
    assert tight["packing"]["omitted"] == {"budget": 1}
    tight_by_id = {item["id"]: item for item in tight["items"]}
    assert _immutable_fields(tight_by_id[counterpart["id"]]) == _immutable_fields(expanded_item)
    assert _immutable_fields(tight_by_id[direct["id"]]) == _immutable_fields(direct_item)


async def _mk_member_workspace() -> str:
    """A fresh workspace in the default tenant with the seeded admin as a
    member: workspace-visible items there sit inside the caller's read
    boundary while remaining a different dedup scope (tenant + workspace +
    principal + hash), so identical content hashes are legal durable rows."""
    async with _test_engine.begin() as conn:
        tenant_id = await conn.scalar(text("SELECT id::text FROM tenants WHERE slug = 'default'"))
        admin_id = await conn.scalar(
            text(
                "SELECT p.id::text FROM principals p JOIN tenants t ON t.id = p.tenant_id "
                "WHERE t.slug = 'default' AND p.name = 'admin'"
            )
        )
        workspace_id = str(uuid4())
        workspace_slug = f"exp192-ws-{workspace_id[:8]}"
        await conn.execute(
            text(
                "INSERT INTO workspaces (id, tenant_id, name, slug) "
                "VALUES (:id, :tid, 'exp192 ws', :slug)"
            ),
            {"id": workspace_id, "tid": tenant_id, "slug": workspace_slug},
        )
        await conn.execute(
            text(
                "INSERT INTO workspace_members (id, workspace_id, principal_id, role) "
                "VALUES (gen_random_uuid(), :ws, :pid, 'member')"
            ),
            {"ws": workspace_id, "pid": admin_id},
        )
        return workspace_slug


async def test_known_root_diversity_direct_and_tunnel_expanded(client, monkeypatch) -> None:
    """Required test 13: a known-root family spanning a direct hit and an
    expansion-only sibling in another workspace — linked by an explicit
    ``derived_from`` edge, the only mechanically-known root identity —
    yields its extra members to distinct context."""
    await _skip_without_db()
    from engram.config import settings

    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    direct = await _seed_packing_item(
        client,
        "semantic target tunnel family direct",
        importance=0.9,
        wing="PackFar",
        room="dst",
    )
    # The sibling lives in another workspace (of which the caller is a
    # member) and is expansion-only (no embedding): the explicit
    # ``derived_from`` edge below is both its graph reachability and its
    # durable root identity — equal canonical content plays no part.
    workspace_slug = await _mk_member_workspace()
    tunnel_sibling = await _seed_packing_item(
        client,
        "tunnel family sibling",
        importance=0.5,
        wing="PackSrc",
        room="s",
        workspace=workspace_slug,
        visibility="workspace",
    )
    distinct = await _seed_packing_item(client, "semantic query tunnel distinct", importance=0.6)
    await _make_expansion_only(tunnel_sibling["id"])
    await _mk_tunnel("PackSrc", "PackFar")
    await _link_items(direct["id"], tunnel_sibling["id"], "derived_from")

    governed = await _candidate_packet(client, item_budget=2)
    assert set(_packet_ids(governed)) == {direct["id"], distinct["id"]}
    assert governed["packing"]["omitted"] == {"redundant_known_root": 1}
    reasons = {item["id"]: item["packing_reason"] for item in governed["items"]}
    assert set(reasons.values()) == {"ranked"}


async def test_same_hash_different_scope_is_not_a_known_root(client, monkeypatch) -> None:
    """Same-hash/different-scope regression (#192's boundary with #161): two
    admitted candidates with an identical durable ``content_hash`` in
    different workspaces and NO explicit derivation relation are NOT one
    redundancy family. Equal canonical content proves only equal
    canonicalized text — the root relationship stays unknown — so under a
    budget where both high-ranked same-content items would normally occupy
    the packet, both are selected by ordinary rank and the distinct item
    loses to the budget alone, never to a fabricated
    ``redundant_known_root``."""
    await _skip_without_db()
    from engram.config import settings

    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    workspace_slug = await _mk_member_workspace()
    # A ranks first, B (same rewritten hash, different workspace) ranks
    # second, C is the lower-ranked distinct item; no edges anywhere.
    a = await _seed_packing_item(
        client, "semantic target same hash alpha", importance=0.9, root_hash="pack-neg-h"
    )
    b = await _seed_packing_item(
        client,
        "semantic target same hash sibling",
        importance=0.8,
        root_hash="pack-neg-h",
        workspace=workspace_slug,
        visibility="workspace",
    )
    c = await _seed_packing_item(client, "semantic query same hash distinct", importance=0.6)

    governed = await _candidate_packet(client, item_budget=2)
    # NOT [A, C] with B omitted as redundant_known_root: same hash is not a
    # root fact, so A and B stay ungrouped and both take slots by rank.
    assert _packet_ids(governed) == [a["id"], b["id"]]
    assert c["id"] not in _packet_ids(governed)
    assert governed["packing"]["omitted"] == {"budget": 1}
    assert "redundant_known_root" not in governed["packing"]["omitted"]
    reasons = {item["id"]: item["packing_reason"] for item in governed["items"]}
    assert reasons == {a["id"]: "ranked", b["id"]: "ranked"}
    assert governed["packing"]["conflict_pairs_preserved"] == 0
    # Reconciliation over the three admitted candidates.
    packing = governed["packing"]
    assert packing["selected_count"] + sum(packing["omitted"].values()) == 3


# ---- integration: security / RLS / scope (required tests 23, 24) ---------------


async def test_cross_tenant_relation_cannot_group(client, monkeypatch) -> None:
    """Required test 23: a derived-from edge row recorded under a foreign
    tenant can neither group two same-tenant candidates into one root family
    nor affect any packing diagnostic."""
    await _skip_without_db()
    from engram.config import settings

    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    a1 = await _seed_packing_item(client, "semantic target foreign edge one", importance=0.9)
    a2 = await _seed_packing_item(client, "semantic target foreign edge two", importance=0.8)
    distinct = await _seed_packing_item(
        client, "semantic query foreign edge distinct", importance=0.7
    )

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from engram.models import MemoryEdge, Principal, Tenant

    factory = async_sessionmaker(_test_engine, expire_on_commit=False)
    async with factory() as session:
        tenant = Tenant(name="exp192 other", slug=f"exp192-{uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        principal = Principal(tenant_id=tenant.id, name="exp192-agent", type="agent")
        session.add(principal)
        await session.flush()
        # Same two endpoints, but the edge claims the foreign tenant.
        session.add(
            MemoryEdge(
                tenant_id=tenant.id,
                source_item_id=UUID(a1["id"]),
                target_item_id=UUID(a2["id"]),
                edge_type="derived_from",
            )
        )
        await session.commit()
    assert await _item_tenant(a1["id"]) is not None  # a1 itself is untouched

    # The foreign-tenant edge is invisible: no grouping — both candidates
    # pack by rank (the third, distinct candidate simply loses on budget).
    ungrouped = await _candidate_packet(client, item_budget=2)
    assert _packet_ids(ungrouped) == [a1["id"], a2["id"]]
    assert ungrouped["packing"]["omitted"] == {"budget": 1}

    # The same edge under the caller's tenant groups them: distinct context
    # takes the second slot instead.
    await _link_items(a1["id"], a2["id"], "derived_from")
    grouped = await _candidate_packet(client, item_budget=2)
    assert _packet_ids(grouped) == [a1["id"], distinct["id"]]
    assert grouped["packing"]["omitted"] == {"redundant_known_root": 1}


async def _item_tenant(item_id: str) -> str | None:
    async with _test_engine.begin() as conn:
        return await conn.scalar(
            text("SELECT tenant_id::text FROM memory_items WHERE id = :id"), {"id": item_id}
        )


# ---- integration: performance / boundedness (required tests 25, 26) ------------


async def test_packing_adds_no_provider_call(client, monkeypatch) -> None:
    """Required test 25: packing introduces no provider call — the whole
    comparison still makes exactly one (the shared query embedding)."""
    await _skip_without_db()
    from engram.config import settings

    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    import engram.embeddings as embeddings_mod
    from engram import recall as recall_mod
    from engram.api.routes import memory as memory_routes
    from tests.test_recall_profile_semantic import _fake_embedding_for

    one = await _seed_packing_item(client, "semantic target provider pack one", importance=0.9)
    two = await _seed_packing_item(client, "semantic target provider pack two", importance=0.8)
    await _seed_packing_item(client, "semantic query provider pack distinct", importance=0.7)
    await _link_items(one["id"], two["id"], "derived_from")

    provider_calls = {"count": 0}

    async def counting_embedding(text_value: str, *_args: object, **_kwargs: object):
        provider_calls["count"] += 1
        return _fake_embedding_for(text_value)

    monkeypatch.setattr(recall_mod, "generate_embedding", counting_embedding)
    monkeypatch.setattr(memory_routes, "generate_embedding", counting_embedding)
    monkeypatch.setattr(embeddings_mod, "generate_embedding", counting_embedding)

    await _shadow_compare(client, profiles=["governed", "exploratory"])
    assert provider_calls["count"] == 1


async def test_packing_relation_loading_is_one_bounded_query(client, monkeypatch) -> None:
    """Required test 26: the packing relation load is a single bulk edge
    query — memory_edges statement count stays constant as the admitted
    candidate count grows (no N+1, no graph walk)."""
    await _skip_without_db()
    from engram.config import settings

    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()
    monkeypatch.setattr(settings, "relationship_expansion_enabled", False)

    seed = await _seed_packing_item(client, "semantic target bounded pack seed", importance=0.9)

    edge_queries = {"count": 0}

    def _count_edges(conn, cursor, statement, parameters, context, executemany):
        # SELECTs only — the fixture's own INSERT INTO memory_edges rows are
        # not reads.
        if "memory_edges" in statement and statement.lstrip().upper().startswith("SELECT"):
            edge_queries["count"] += 1

    event.listen(_test_engine.sync_engine, "before_cursor_execute", _count_edges)
    try:
        few = await _candidate_packet(client, item_budget=5)
        assert few["packing"]["selected_count"] == 1
        few_queries = edge_queries["count"]

        for i in range(11):
            extra = await _seed_packing_item(
                client,
                f"semantic target bounded pack extra {i:02d}",
                importance=0.5,
            )
            await _link_items(seed["id"], extra["id"], "derived_from")
        many = await _candidate_packet(client, item_budget=5)
        assert many["packing"]["selected_count"] == 5
    finally:
        event.remove(_test_engine.sync_engine, "before_cursor_execute", _count_edges)

    # One packing edge query per candidate packet, constant in candidate
    # volume. (With expansion disabled, nothing else touches memory_edges.)
    assert few_queries == 1
    assert edge_queries["count"] - few_queries == 1


# ---- integration: read-only / compatibility / rollout (required tests 27-29) ---


async def test_packing_shadow_remains_read_only(client, monkeypatch) -> None:
    """Required test 27: the packing comparison writes nothing — no
    recall_logs row, no exposure counters."""
    await _skip_without_db()
    from engram.config import settings

    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    one = await _seed_packing_item(client, "semantic target readonly pack one", importance=0.9)
    two = await _seed_packing_item(client, "semantic target readonly pack two", importance=0.8)
    await _link_items(one["id"], two["id"], "derived_from")
    packet = await _candidate_packet(client, item_budget=1)
    ids = _packet_ids(packet)

    logs_before = await _recall_log_count()
    counts_before = await _recall_counts(ids)
    again = await _candidate_packet(client, item_budget=1)
    assert _packet_ids(again) == ids
    assert await _recall_log_count() == logs_before
    assert await _recall_counts(ids) == counts_before


async def test_legacy_packet_untouched_by_packing(client, monkeypatch) -> None:
    """Required test 28: the legacy packet is byte-compatible — no packing
    summary, no per-item packing fields, identical output across runs, and
    the authoritative /v1/recall response still matches it."""
    await _skip_without_db()
    from engram.config import settings

    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    one = await _seed_packing_item(client, "semantic target legacy pack one", importance=0.9)
    two = await _seed_packing_item(client, "semantic target legacy pack two", importance=0.8)
    await _link_items(one["id"], two["id"], "derived_from")

    shadow1 = await _shadow_compare(client, profiles=["governed"], item_budget=2)
    shadow2 = await _shadow_compare(client, profiles=["governed"], item_budget=2)
    legacy1, legacy2 = shadow1["legacy"], shadow2["legacy"]

    assert legacy1["packing"] is None
    assert all("packing_reason" not in item for item in legacy1["items"])
    assert legacy1 == legacy2

    served = await _recall_semantic(client)
    assert _packet_ids(served) == _packet_ids(legacy1)
    assert all("packing_reason" not in item for item in served["items"])


async def test_candidate_profiles_remain_uncertified_and_unservable(client, monkeypatch) -> None:
    """Required test 29: production stays legacy-only — the certification
    constant is unchanged and an explicitly requested candidate profile is
    refused by ordinary recall."""
    await _skip_without_db()
    from engram.config import settings

    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    await _seed_packing_item(client, "semantic target uncertified", importance=0.9)

    shadow = await _shadow_compare(client, profiles=["governed"])
    assert shadow["certified_serving_profiles"] == ["legacy"]
    assert shadow["authoritative_profile"] == "legacy"

    resp = await client.post(
        "/v1/recall",
        json={"mode": "semantic", "query": "semantic query", "recall_profile": "governed"},
    )
    assert resp.status_code == 422


async def test_budget_and_accounting_reconcile_end_to_end(client, monkeypatch) -> None:
    """Required tests 15-18 (integration facet): hard budgets hold and the
    packing accounting reconciles with the served packet — selected_count
    equals item_count, and selected + omitted equals the admitted count the
    admission diagnostics imply."""
    await _skip_without_db()
    from engram.config import settings

    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)
    await _enable_tenant_shadow_policy()

    one = await _seed_packing_item(client, "semantic target reconcile one", importance=0.9)
    two = await _seed_packing_item(client, "semantic target reconcile two", importance=0.8)
    await _seed_packing_item(client, "semantic query reconcile distinct", importance=0.7)
    await _link_items(one["id"], two["id"], "derived_from")
    withheld = await _seed_packing_item(
        client, "semantic query reconcile withheld", importance=0.6, qualified=False
    )

    governed = await _candidate_packet(client, item_budget=2)
    packing = governed["packing"]
    assert packing["selected_count"] == governed["item_count"] == 2
    assert governed["byte_count"] <= governed["effective_byte_budget"]
    admitted_total = len(governed["items"]) + sum(packing["omitted"].values())
    withheld_count = sum(
        1 for d in governed["admission_diagnostics"] if d["item_id"] == withheld["id"]
    )
    # 3 admitted candidates (representative + deferred sibling + distinct)
    # and the withheld item counted once by admission, never by packing.
    assert admitted_total == 3
    assert withheld_count == 1
    assert packing["omitted"] == {"redundant_known_root": 1}

    # Exact item-budget boundary: item_count lands exactly on the budget.
    exact = await _candidate_packet(client, item_budget=3)
    assert exact["item_count"] == 3
    assert exact["packing"]["selected_count"] == 3
    assert exact["packing"]["omitted"] == {}
