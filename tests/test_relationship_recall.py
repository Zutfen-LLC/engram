"""Direct tests of engram.relationship_recall.expand_recall_candidates
(ENG-AUD-012 / F19) — bounded graph + tunnel expansion, merge, and
relationship-aware rescoring.

These call the expansion module directly against real seeded DB rows
(memory_items / memory_edges / tunnels), bypassing pgvector/embeddings
entirely, so behavior is deterministic and independent of vector-similarity
fixtures. End-to-end HTTP wiring through /v1/recall is covered separately in
tests/test_graph_recall.py and tests/test_tunnel_recall.py.

Requires a live PostgreSQL with the v2 schema + migration 009 applied. Skips
automatically when no DB is reachable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.auth import Principal
from engram.config import settings
from engram.memory_context import ResolvedMemoryContext, unrestricted_memory_context
from engram.models import MemoryEdge, MemoryItem, Tunnel, Workspace

_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


async def _db_ok() -> bool:
    try:
        async with _engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.fixture(autouse=True)
async def _clean_db():
    if not await _db_ok():
        return
    async with _engine.begin() as conn:
        await conn.execute(text("DELETE FROM memory_edges"))
        await conn.execute(text("DELETE FROM tunnels"))
        await conn.execute(text("DELETE FROM memory_embeddings"))
        await conn.execute(text("DELETE FROM memory_items"))
        await conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'other-%'"))
        await conn.execute(text("DELETE FROM principals WHERE name LIKE 'other-%'"))
        await conn.execute(text("DELETE FROM workspaces WHERE slug LIKE 'other-%'"))


@pytest.fixture(autouse=True)
def _reset_settings():
    snapshot = {
        "max_graph_neighbors_per_item": settings.max_graph_neighbors_per_item,
        "max_graph_expanded_items": settings.max_graph_expanded_items,
        "max_tunnel_neighbors_per_item": settings.max_tunnel_neighbors_per_item,
        "max_tunnel_additions": settings.max_tunnel_additions,
        "recall_candidate_ceiling": settings.recall_candidate_ceiling,
        "recall_semantic_expansion_seed_limit": settings.recall_semantic_expansion_seed_limit,
    }
    yield
    for key, value in snapshot.items():
        setattr(settings, key, value)


async def _default_tenant_principal(session: AsyncSession) -> tuple[str, str]:
    row = (
        await session.execute(
            text(
                "SELECT t.id::text AS tenant_id, p.id::text AS principal_id "
                "FROM tenants t "
                "JOIN principals p ON p.tenant_id = t.id AND p.name = 'admin' "
                "WHERE t.slug = 'default'"
            )
        )
    ).mappings().one()
    return row["tenant_id"], row["principal_id"]


async def _mk_item(
    session: AsyncSession,
    tenant_id: str,
    principal_id: str,
    *,
    content: str,
    wing: str | None = None,
    room: str | None = None,
    importance: float = 0.5,
    # ENG-SCOPE-001: visibility='workspace' requires a real workspace_id.
    # 'tenant' preserves this helper's original "generally visible within
    # the tenant, no membership setup needed" default intent.
    visibility: str = "tenant",
    review_status: str = "active",
    workspace_id: str | None = None,
) -> MemoryItem:
    item = MemoryItem(
        tenant_id=UUID(tenant_id),
        principal_id=UUID(principal_id),
        content=content,
        content_hash=f"h-{uuid4()}",
        kind="fact",
        wing=wing,
        room=room,
        importance=importance,
        visibility=visibility,
        review_status=review_status,
        workspace_id=UUID(workspace_id) if workspace_id else None,
    )
    session.add(item)
    await session.flush()
    return item


async def _mk_edge(
    session: AsyncSession,
    tenant_id: str,
    source: MemoryItem,
    target: MemoryItem,
    edge_type: str,
    *,
    weight: float | None = None,
) -> MemoryEdge:
    edge = MemoryEdge(
        tenant_id=UUID(tenant_id),
        source_item_id=source.id,
        target_item_id=target.id,
        edge_type=edge_type,
        weight=weight,
    )
    session.add(edge)
    await session.flush()
    return edge


async def _mk_tunnel(
    session: AsyncSession,
    tenant_id: str,
    *,
    source_wing: str,
    source_room: str | None,
    target_wing: str,
    target_room: str | None,
    label: str | None = None,
) -> Tunnel:
    tunnel = Tunnel(
        tenant_id=UUID(tenant_id),
        source_wing=source_wing,
        source_room=source_room,
        target_wing=target_wing,
        target_room=target_room,
        label=label,
    )
    session.add(tunnel)
    await session.flush()
    return tunnel


def _seed_dict(item: MemoryItem, score: float = 0.8) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "kind": item.kind,
        "content": item.content,
        "score": score,
        "review_status": item.review_status,
        "reasons": [f"semantic similarity {score:.2f}"],
        "warnings": [],
        "pinned": item.pinned,
        "importance": item.importance,
        "source_trust": item.source_trust,
        "memory_confidence": item.memory_confidence,
        "human_verified": item.human_verified,
        "distance": 0.0,
        "similarity_score": score,
        "trust_score": 1.0,
    }


def _by_id(items: list[dict[str, Any]], item_id: Any) -> dict[str, Any] | None:
    return next((i for i in items if i["id"] == str(item_id)), None)


def _origins(item: dict[str, Any]) -> set[str]:
    return set(item["origin"].split("+"))


async def _expand(
    session: AsyncSession,
    tenant_id: str,
    principal_id: str,
    seed_items: list[MemoryItem],
    item_by_id: dict[UUID, MemoryItem],
    *,
    workspace_id: str | None = None,
    scores: dict[UUID, float] | None = None,
    memory_context: ResolvedMemoryContext | None = None,
) -> list[dict[str, Any]]:
    from engram.relationship_recall import expand_recall_candidates

    scores = scores or {}
    semantic_items = [_seed_dict(i, scores.get(i.id, 0.8)) for i in seed_items]
    return await expand_recall_candidates(
        session,
        memory_context=memory_context
        or unrestricted_memory_context(
            Principal(tenant_id=tenant_id, principal_id=principal_id, scopes=("read",))
        ),
        workspace_id=workspace_id,
        semantic_items=semantic_items,
        item_by_id=item_by_id,
        now=datetime.now(UTC),
    )


# ---- graph expansion ----


async def test_graph_expansion_adds_neighbor_with_reason_and_bonus():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _session_factory() as session:
        tenant_id, principal_id = await _default_tenant_principal(session)
        target = await _mk_item(session, tenant_id, principal_id, content="target")
        linked = await _mk_item(session, tenant_id, principal_id, content="linked")
        await _mk_edge(session, tenant_id, target, linked, "derived_from")
        await session.commit()

        result = await _expand(
            session, tenant_id, principal_id, [target], {target.id: target, linked.id: linked}
        )

    linked_out = _by_id(result, linked.id)
    assert linked_out is not None
    assert _origins(linked_out) == {"graph"}
    assert any("linked via derived_from" in r for r in linked_out["reasons"])
    assert linked_out["relationship_bonus"] == 0.9


async def test_graph_expansion_no_duplicates():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _session_factory() as session:
        tenant_id, principal_id = await _default_tenant_principal(session)
        a = await _mk_item(session, tenant_id, principal_id, content="a")
        b = await _mk_item(session, tenant_id, principal_id, content="b")
        shared = await _mk_item(session, tenant_id, principal_id, content="shared")
        await _mk_edge(session, tenant_id, a, shared, "derived_from")
        await _mk_edge(session, tenant_id, b, shared, "references")
        await session.commit()

        result = await _expand(
            session,
            tenant_id,
            principal_id,
            [a, b],
            {a.id: a, b.id: b, shared.id: shared},
        )

    ids = [r["id"] for r in result]
    assert ids.count(str(shared.id)) == 1


async def test_graph_expansion_seed_to_seed_edge_enriches_without_duplicating():
    """Two semantic seeds linked by an edge should merge into a single
    'semantic+graph' row, not two separate rows."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _session_factory() as session:
        tenant_id, principal_id = await _default_tenant_principal(session)
        a = await _mk_item(session, tenant_id, principal_id, content="a")
        b = await _mk_item(session, tenant_id, principal_id, content="b")
        await _mk_edge(session, tenant_id, a, b, "supports")
        await session.commit()

        result = await _expand(session, tenant_id, principal_id, [a, b], {a.id: a, b.id: b})

    ids = [r["id"] for r in result]
    assert ids.count(str(b.id)) == 1
    b_out = _by_id(result, b.id)
    assert _origins(b_out) == {"semantic", "graph"}
    assert any("linked via supports" in r for r in b_out["reasons"])
    assert any("semantic similarity" in r for r in b_out["reasons"])


async def test_graph_expansion_depth_never_exceeds_one():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _session_factory() as session:
        tenant_id, principal_id = await _default_tenant_principal(session)
        a = await _mk_item(session, tenant_id, principal_id, content="a")
        b = await _mk_item(session, tenant_id, principal_id, content="b")
        c = await _mk_item(session, tenant_id, principal_id, content="c")
        await _mk_edge(session, tenant_id, a, b, "derived_from")
        await _mk_edge(session, tenant_id, b, c, "derived_from")
        await session.commit()

        result = await _expand(
            session, tenant_id, principal_id, [a], {a.id: a, b.id: b, c.id: c}
        )

    ids = {r["id"] for r in result}
    assert str(b.id) in ids
    assert str(c.id) not in ids


async def test_graph_expansion_respects_max_neighbors_per_item():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.max_graph_neighbors_per_item = 2
    settings.max_graph_expanded_items = 20
    async with _session_factory() as session:
        tenant_id, principal_id = await _default_tenant_principal(session)
        target = await _mk_item(session, tenant_id, principal_id, content="target")
        neighbors = []
        item_by_id = {target.id: target}
        for i in range(5):
            n = await _mk_item(session, tenant_id, principal_id, content=f"n{i}")
            neighbors.append(n)
            item_by_id[n.id] = n
            await _mk_edge(session, tenant_id, target, n, "mentions", weight=0.3 + i * 0.01)
        await session.commit()

        result = await _expand(session, tenant_id, principal_id, [target], item_by_id)

    graph_ids = {r["id"] for r in result if "graph" in _origins(r)}
    assert len(graph_ids) == 2
    # Strongest weights win — deterministic ordering.
    assert graph_ids == {str(neighbors[4].id), str(neighbors[3].id)}


async def test_profile_filter_precedes_graph_and_tunnel_neighbor_caps():
    """An ineligible stronger neighbor cannot consume either bounded window."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.max_graph_neighbors_per_item = 1
    settings.max_graph_expanded_items = 1
    settings.max_tunnel_neighbors_per_item = 1
    settings.max_tunnel_additions = 1
    async with _session_factory() as session:
        tenant_id, principal_id = await _default_tenant_principal(session)
        workspace_a = Workspace(
            tenant_id=UUID(tenant_id), name="Other allowed", slug=f"other-a-{uuid4().hex}"
        )
        workspace_b = Workspace(
            tenant_id=UUID(tenant_id), name="Other denied", slug=f"other-b-{uuid4().hex}"
        )
        session.add_all((workspace_a, workspace_b))
        await session.flush()

        seed = await _mk_item(
            session,
            tenant_id,
            principal_id,
            content="profile seed",
            wing="ProfileSource",
            room="seed",
        )
        graph_allowed = await _mk_item(
            session,
            tenant_id,
            principal_id,
            content="graph allowed",
            workspace_id=str(workspace_a.id),
        )
        graph_denied = await _mk_item(
            session,
            tenant_id,
            principal_id,
            content="graph denied",
            workspace_id=str(workspace_b.id),
        )
        tunnel_allowed = await _mk_item(
            session,
            tenant_id,
            principal_id,
            content="tunnel allowed",
            wing="ProfileTarget",
            room="neighbors",
            workspace_id=str(workspace_a.id),
            importance=0.1,
        )
        tunnel_denied = await _mk_item(
            session,
            tenant_id,
            principal_id,
            content="tunnel denied",
            wing="ProfileTarget",
            room="neighbors",
            workspace_id=str(workspace_b.id),
            importance=1.0,
        )
        await _mk_edge(session, tenant_id, seed, graph_allowed, "mentions", weight=0.1)
        await _mk_edge(session, tenant_id, seed, graph_denied, "mentions", weight=1.0)
        await _mk_tunnel(
            session,
            tenant_id,
            source_wing="ProfileSource",
            source_room="seed",
            target_wing="ProfileTarget",
            target_room="neighbors",
        )
        await session.commit()

        context = ResolvedMemoryContext(
            version="memory-context-v2",
            tenant_id=UUID(tenant_id),
            principal_id=UUID(principal_id),
            api_key_id=uuid4(),
            memory_profile_id=uuid4(),
            memory_profile_revision_id=uuid4(),
            memory_profile_slug="profile-cap-test",
            memory_profile_version=1,
            include_private=False,
            include_tenant=True,
            include_public=False,
            readable_workspace_ids=frozenset({workspace_a.id}),
            allow_tenant_write=True,
            allow_public_write=False,
            default_write_visibility="private",
            default_write_workspace_id=None,
            writable_workspace_ids=frozenset({workspace_a.id}),
        )
        result = await _expand(
            session,
            tenant_id,
            principal_id,
            [seed],
            {
                item.id: item
                for item in (
                    seed,
                    graph_allowed,
                    graph_denied,
                    tunnel_allowed,
                    tunnel_denied,
                )
            },
            memory_context=context,
        )

    result_ids = {row["id"] for row in result}
    assert str(graph_allowed.id) in result_ids
    assert str(tunnel_allowed.id) in result_ids
    assert str(graph_denied.id) not in result_ids
    assert str(tunnel_denied.id) not in result_ids


async def test_graph_expansion_respects_overall_cap():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.max_graph_neighbors_per_item = 10
    settings.max_graph_expanded_items = 3
    async with _session_factory() as session:
        tenant_id, principal_id = await _default_tenant_principal(session)
        target = await _mk_item(session, tenant_id, principal_id, content="target")
        item_by_id = {target.id: target}
        for i in range(6):
            n = await _mk_item(session, tenant_id, principal_id, content=f"n{i}")
            item_by_id[n.id] = n
            await _mk_edge(session, tenant_id, target, n, "mentions")
        await session.commit()

        result = await _expand(session, tenant_id, principal_id, [target], item_by_id)

    graph_ids = {r["id"] for r in result if "graph" in _origins(r)}
    assert len(graph_ids) == 3


async def test_graph_expansion_never_revisits_same_node_from_two_seeds():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _session_factory() as session:
        tenant_id, principal_id = await _default_tenant_principal(session)
        a = await _mk_item(session, tenant_id, principal_id, content="a")
        b = await _mk_item(session, tenant_id, principal_id, content="b")
        shared = await _mk_item(session, tenant_id, principal_id, content="shared")
        await _mk_edge(session, tenant_id, a, shared, "derived_from")
        await _mk_edge(session, tenant_id, b, shared, "references")
        await session.commit()

        result = await _expand(
            session, tenant_id, principal_id, [a, b], {a.id: a, b.id: b, shared.id: shared}
        )

    ids = [r["id"] for r in result]
    assert ids.count(str(shared.id)) == 1


# ---- relationship weights ----


async def test_strong_relationship_outranks_weak_relationship():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _session_factory() as session:
        tenant_id, principal_id = await _default_tenant_principal(session)
        target = await _mk_item(session, tenant_id, principal_id, content="target")
        strong = await _mk_item(session, tenant_id, principal_id, content="strong", importance=0.5)
        weak = await _mk_item(session, tenant_id, principal_id, content="weak", importance=0.5)
        await _mk_edge(session, tenant_id, target, strong, "derived_from")  # strong = 0.9
        await _mk_edge(session, tenant_id, target, weak, "mentions")  # weak = 0.3
        await session.commit()

        result = await _expand(
            session,
            tenant_id,
            principal_id,
            [target],
            {target.id: target, strong.id: strong, weak.id: weak},
        )

    strong_out = _by_id(result, strong.id)
    weak_out = _by_id(result, weak.id)
    assert strong_out["score"] > weak_out["score"]
    assert strong_out["relationship_bonus"] > weak_out["relationship_bonus"]


# ---- tunnel expansion ----


async def test_tunnel_expansion_adds_bounded_neighbor_with_reason():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _session_factory() as session:
        tenant_id, principal_id = await _default_tenant_principal(session)
        target = await _mk_item(
            session, tenant_id, principal_id, content="target", wing="Atlas", room="decisions"
        )
        neighbor = await _mk_item(
            session, tenant_id, principal_id, content="neighbor", wing="AtlasOps", room="runbooks"
        )
        await _mk_tunnel(
            session,
            tenant_id,
            source_wing="Atlas",
            source_room="decisions",
            target_wing="AtlasOps",
            target_room="runbooks",
            label="Atlas",
        )
        await session.commit()

        result = await _expand(
            session, tenant_id, principal_id, [target], {target.id: target, neighbor.id: neighbor}
        )

    neighbor_out = _by_id(result, neighbor.id)
    assert neighbor_out is not None
    assert _origins(neighbor_out) == {"tunnel"}
    assert any('same tunnel "Atlas"' in r for r in neighbor_out["reasons"])
    assert neighbor_out["tunnel_bonus"] == 1.0


async def test_tunnel_expansion_no_matching_tunnel_no_addition():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _session_factory() as session:
        tenant_id, principal_id = await _default_tenant_principal(session)
        target = await _mk_item(
            session, tenant_id, principal_id, content="target", wing="Isolated", room="alone"
        )
        unrelated = await _mk_item(
            session, tenant_id, principal_id, content="unrelated", wing="Elsewhere", room="alone"
        )
        await session.commit()

        result = await _expand(
            session,
            tenant_id,
            principal_id,
            [target],
            {target.id: target, unrelated.id: unrelated},
        )

    ids = {r["id"] for r in result}
    assert str(unrelated.id) not in ids


async def test_tunnel_expansion_whole_wing_when_tunnel_room_is_none():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _session_factory() as session:
        tenant_id, principal_id = await _default_tenant_principal(session)
        target = await _mk_item(
            session, tenant_id, principal_id, content="target", wing="Atlas", room="decisions"
        )
        neighbor = await _mk_item(
            session, tenant_id, principal_id, content="neighbor", wing="AtlasOps", room="anything"
        )
        await _mk_tunnel(
            session,
            tenant_id,
            source_wing="Atlas",
            source_room="decisions",
            target_wing="AtlasOps",
            target_room=None,
            label="Atlas Wing Wide",
        )
        await session.commit()

        result = await _expand(
            session, tenant_id, principal_id, [target], {target.id: target, neighbor.id: neighbor}
        )

    ids = {r["id"] for r in result}
    assert str(neighbor.id) in ids


async def test_tunnel_expansion_respects_max_additions():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.max_tunnel_additions = 2
    settings.max_tunnel_neighbors_per_item = 10
    async with _session_factory() as session:
        tenant_id, principal_id = await _default_tenant_principal(session)
        target = await _mk_item(
            session, tenant_id, principal_id, content="target", wing="Atlas", room="decisions"
        )
        item_by_id = {target.id: target}
        for i in range(5):
            n = await _mk_item(
                session, tenant_id, principal_id, content=f"n{i}", wing="AtlasOps", room="runbooks"
            )
            item_by_id[n.id] = n
        await _mk_tunnel(
            session,
            tenant_id,
            source_wing="Atlas",
            source_room="decisions",
            target_wing="AtlasOps",
            target_room="runbooks",
            label="Atlas",
        )
        await session.commit()

        result = await _expand(session, tenant_id, principal_id, [target], item_by_id)

    tunnel_ids = {r["id"] for r in result if "tunnel" in _origins(r)}
    assert len(tunnel_ids) == 2


# ---- trust ----


async def test_expansion_hides_private_item_of_other_principal():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _session_factory() as session:
        tenant_id, principal_id = await _default_tenant_principal(session)
        target = await _mk_item(session, tenant_id, principal_id, content="target")
        other_id = str(uuid4())
        await session.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES (:pid, :tid, :pname, 'agent')"
            ),
            {"pid": other_id, "tid": tenant_id, "pname": f"other-agent-{other_id[:8]}"},
        )
        private_item = await _mk_item(
            session, tenant_id, other_id, content="private secret", visibility="private"
        )
        await _mk_edge(session, tenant_id, target, private_item, "derived_from")
        await session.commit()

        result = await _expand(
            session,
            tenant_id,
            principal_id,
            [target],
            {target.id: target, private_item.id: private_item},
        )

    ids = {r["id"] for r in result}
    assert str(private_item.id) not in ids


async def test_expansion_excludes_disputed_neighbor():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _session_factory() as session:
        tenant_id, principal_id = await _default_tenant_principal(session)
        target = await _mk_item(session, tenant_id, principal_id, content="target")
        disputed = await _mk_item(
            session, tenant_id, principal_id, content="disputed", review_status="disputed"
        )
        await _mk_edge(session, tenant_id, target, disputed, "contradicts")
        await session.commit()

        result = await _expand(
            session, tenant_id, principal_id, [target], {target.id: target, disputed.id: disputed}
        )

    ids = {r["id"] for r in result}
    assert str(disputed.id) not in ids


async def test_expansion_respects_workspace_restriction():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _session_factory() as session:
        tenant_id, principal_id = await _default_tenant_principal(session)
        other_ws_id = str(uuid4())
        await session.execute(
            text(
                "INSERT INTO workspaces (id, tenant_id, name, slug) "
                "VALUES (:id, :tid, 'other-ws', :slug)"
            ),
            {"id": other_ws_id, "tid": tenant_id, "slug": f"other-ws-{other_ws_id[:8]}"},
        )
        target = await _mk_item(session, tenant_id, principal_id, content="target")
        restricted = await _mk_item(
            session,
            tenant_id,
            principal_id,
            content="restricted",
            visibility="workspace",
            workspace_id=other_ws_id,
        )
        await _mk_edge(session, tenant_id, target, restricted, "derived_from")
        await session.commit()

        # No explicit workspace scoping (workspace_id=None) still enforces
        # membership via eligibility_expression for visibility='workspace'
        # items whose workspace_id names a real workspace the caller isn't in.
        result = await _expand(
            session,
            tenant_id,
            principal_id,
            [target],
            {target.id: target, restricted.id: restricted},
        )

    ids = {r["id"] for r in result}
    assert str(restricted.id) not in ids


async def test_expansion_cross_tenant_neighbor_excluded():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _session_factory() as session:
        tenant_id, principal_id = await _default_tenant_principal(session)
        target = await _mk_item(session, tenant_id, principal_id, content="target")

        other_tenant_id = str(uuid4())
        other_principal_id = str(uuid4())
        await session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'Other Tenant', :slug)"),
            {"id": other_tenant_id, "slug": f"other-{other_tenant_id[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES (:pid, :tid, 'other-tenant-admin', 'admin')"
            ),
            {"pid": other_principal_id, "tid": other_tenant_id},
        )
        other_item = await _mk_item(
            session, other_tenant_id, other_principal_id, content="other tenant secret"
        )
        # Edge row itself lives in the caller's own tenant (RLS-owned), but its
        # target belongs to a different tenant — eligibility re-filters by
        # tenant_id, so this can never leak.
        await session.execute(
            text(
                "INSERT INTO memory_edges (id, tenant_id, source_item_id, target_item_id, "
                "edge_type) VALUES (:id, :tid, :src, :tgt, 'derived_from')"
            ),
            {"id": str(uuid4()), "tid": tenant_id, "src": target.id, "tgt": other_item.id},
        )
        await session.commit()

        result = await _expand(
            session,
            tenant_id,
            principal_id,
            [target],
            {target.id: target, other_item.id: other_item},
        )

    ids = {r["id"] for r in result}
    assert str(other_item.id) not in ids
    assert str(target.id) in ids


# ---- combined expansion / candidate limits ----


async def test_combined_graph_and_tunnel_deterministic_ordering():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _session_factory() as session:
        tenant_id, principal_id = await _default_tenant_principal(session)
        target = await _mk_item(
            session, tenant_id, principal_id, content="target", wing="Atlas", room="decisions"
        )
        graph_neighbor = await _mk_item(session, tenant_id, principal_id, content="graph n")
        tunnel_neighbor = await _mk_item(
            session, tenant_id, principal_id, content="tunnel n", wing="AtlasOps", room="runbooks"
        )
        await _mk_edge(session, tenant_id, target, graph_neighbor, "derived_from")
        await _mk_tunnel(
            session,
            tenant_id,
            source_wing="Atlas",
            source_room="decisions",
            target_wing="AtlasOps",
            target_room="runbooks",
            label="Atlas",
        )
        await session.commit()

        item_by_id = {
            target.id: target,
            graph_neighbor.id: graph_neighbor,
            tunnel_neighbor.id: tunnel_neighbor,
        }
        result_a = await _expand(session, tenant_id, principal_id, [target], item_by_id)
        result_b = await _expand(session, tenant_id, principal_id, [target], item_by_id)

    order_a = [r["id"] for r in result_a]
    order_b = [r["id"] for r in result_b]
    assert order_a == order_b
    assert str(graph_neighbor.id) in order_a
    assert str(tunnel_neighbor.id) in order_a


async def test_graph_and_tunnel_combo_on_same_node():
    """A node reachable via both graph and tunnel gets a combined origin."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    async with _session_factory() as session:
        tenant_id, principal_id = await _default_tenant_principal(session)
        target = await _mk_item(
            session, tenant_id, principal_id, content="target", wing="Atlas", room="decisions"
        )
        both = await _mk_item(
            session, tenant_id, principal_id, content="both", wing="AtlasOps", room="runbooks"
        )
        await _mk_edge(session, tenant_id, target, both, "derived_from")
        await _mk_tunnel(
            session,
            tenant_id,
            source_wing="Atlas",
            source_room="decisions",
            target_wing="AtlasOps",
            target_room="runbooks",
            label="Atlas",
        )
        await session.commit()

        result = await _expand(
            session, tenant_id, principal_id, [target], {target.id: target, both.id: both}
        )

    both_out = _by_id(result, both.id)
    assert _origins(both_out) == {"graph", "tunnel"}


async def test_overall_candidate_ceiling_truncates_merged_set():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.max_graph_neighbors_per_item = 20
    settings.max_graph_expanded_items = 20
    settings.recall_candidate_ceiling = 5
    async with _session_factory() as session:
        tenant_id, principal_id = await _default_tenant_principal(session)
        target = await _mk_item(session, tenant_id, principal_id, content="target")
        item_by_id = {target.id: target}
        for i in range(10):
            n = await _mk_item(session, tenant_id, principal_id, content=f"n{i}")
            item_by_id[n.id] = n
            await _mk_edge(session, tenant_id, target, n, "mentions")
        await session.commit()

        result = await _expand(session, tenant_id, principal_id, [target], item_by_id)

    assert len(result) == 5
