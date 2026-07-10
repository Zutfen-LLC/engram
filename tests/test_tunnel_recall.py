"""End-to-end HTTP smoke tests for tunnel expansion in semantic recall
(ENG-AUD-012 / F19) — confirms /v1/recall wiring and real trust enforcement.
Bounded/deterministic behavior is covered more precisely (without
vector-similarity confounds) in tests/test_relationship_recall.py, which
calls the expansion module directly.

Requires a live PostgreSQL with the v2 schema + migration 009 applied. Skips
automatically when no DB is reachable, mirroring test_semantic_recall.py.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.api.app import create_app
from engram.api.routes import memory as memory_routes
from engram.config import settings
from engram.db import get_session

_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


async def _db_ok() -> bool:
    try:
        async with _engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _get_test_session() -> AsyncSession:
    async with _session_factory() as session:
        from engram.db import _DEFAULT_PRINCIPAL_NAME, _DEFAULT_TENANT_SLUG, apply_rls_context

        row = (
            (
                await session.execute(
                    text(
                        "SELECT t.id::text AS tenant_id, p.id::text AS principal_id "
                        "FROM tenants t "
                        "JOIN principals p ON p.tenant_id = t.id AND p.name = :principal "
                        "WHERE t.slug = :slug"
                    ),
                    {"slug": _DEFAULT_TENANT_SLUG, "principal": _DEFAULT_PRINCIPAL_NAME},
                )
            )
            .mappings()
            .one()
        )
        await apply_rls_context(
            session, tenant_id=row["tenant_id"], principal_id=row["principal_id"]
        )
        yield session


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


@pytest.fixture(autouse=True)
async def _clean_db():
    if not await _db_ok():
        return
    async with _engine.begin() as conn:
        await conn.execute(text("DELETE FROM feedback_events"))
        await conn.execute(text("DELETE FROM recall_logs"))
        await conn.execute(text("DELETE FROM memory_edges"))
        await conn.execute(text("DELETE FROM tunnels"))
        await conn.execute(text("DELETE FROM memory_embeddings"))
        await conn.execute(text("DELETE FROM memory_items"))
        await conn.execute(text("DELETE FROM principals WHERE name LIKE 'other-agent-%'"))


@pytest.fixture(autouse=True)
def _reset_settings():
    original_provider = settings.embedding_provider
    original_conflict = settings.conflict_check_on_write
    settings.conflict_check_on_write = False
    yield
    settings.embedding_provider = original_provider
    settings.conflict_check_on_write = original_conflict


_TARGET_VEC = [1.0] + [0.0] * 1535
# Cosine similarity to target ~0.9 — ranks strictly ahead of "far" items, so
# a handful of fillers plus a small item_budget (fetch_limit = item_budget *
# 3) deterministically excludes "far" items from the raw semantic candidate
# pool — they can then only be reached via tunnel expansion.
_NEAR_VEC = [0.9, 0.4359] + [0.0] * 1534
_FAR_VEC = [0.0, 1.0] + [0.0] * 1534
_TARGET_PREFIXES = ("semantic target", "semantic query")
_NEAR_PREFIX = "filler near"


def _fake_embedding_for(text_value: str) -> list[float]:
    if text_value.startswith(_TARGET_PREFIXES):
        return _TARGET_VEC
    if text_value.startswith(_NEAR_PREFIX):
        return _NEAR_VEC
    return _FAR_VEC


def _patch_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_embedding(text_value: str) -> list[float] | None:
        return _fake_embedding_for(text_value)

    import engram.embeddings as embeddings_mod
    from engram import recall as recall_mod

    monkeypatch.setattr(recall_mod, "generate_embedding", fake_embedding)
    monkeypatch.setattr(memory_routes, "generate_embedding", fake_embedding)
    monkeypatch.setattr(embeddings_mod, "generate_embedding", fake_embedding)


async def _remember(client: AsyncClient, content: str, **payload: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"content": content, "source_type": "manual"}
    body.update(payload)
    resp = await client.post("/v1/remember", json=body)
    assert resp.status_code == 201, resp.text
    await _drain_jobs()
    return resp.json()


async def _drain_jobs(max_iterations: int = 10) -> None:
    from engram.worker import process_one_job

    for _ in range(max_iterations):
        processed = await process_one_job(
            worker_id="test",
            session_factory=_session_factory,
            app_session_factory=_session_factory,
            job_types=["embedding.generate"],
        )
        if not processed:
            return


async def _create_tunnel(client: AsyncClient, **payload: Any) -> dict[str, Any]:
    resp = await client.post("/v1/tunnels", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _recall_semantic(
    client: AsyncClient, query: str = "semantic query", *, item_budget: int | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"mode": "semantic", "query": query}
    if item_budget is not None:
        body["item_budget"] = item_budget
    resp = await client.post("/v1/recall", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _add_fillers(client: AsyncClient, count: int = 6) -> None:
    """Fillers strictly outrank 'far' items in nearest-neighbor order, so a
    small item_budget's fetch_limit never reaches into the far/distant pool.
    """
    for i in range(count):
        await _remember(client, f"filler near item {i}", wing="Filler", room="filler")


def _by_id(body: dict[str, Any], item_id: str) -> dict[str, Any] | None:
    return next((i for i in body["items"] if i["id"] == item_id), None)


def _has_origin(item: dict[str, Any], tag: str) -> bool:
    return tag in item["origin"].split("+")


# ---- tunnel expansion ----


async def test_tunnel_expansion_returns_neighboring_tunnel_memory(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)

    target = await _remember(client, "semantic target", wing="ProjectAtlas", room="decisions")
    neighbor = await _remember(
        client, "far tunnel neighbor", wing="ProjectAtlasOps", room="runbooks"
    )
    await _create_tunnel(
        client,
        source_wing="ProjectAtlas",
        source_room="decisions",
        target_wing="ProjectAtlasOps",
        target_room="runbooks",
        label="Atlas",
    )
    await _add_fillers(client)

    body = await _recall_semantic(client, item_budget=2)
    ids = {i["id"] for i in body["items"]}
    assert target["id"] in ids
    assert neighbor["id"] in ids

    neighbor_item = _by_id(body, neighbor["id"])
    assert _has_origin(neighbor_item, "tunnel")
    assert any('same tunnel "Atlas"' in r for r in neighbor_item["reasons"])
    # Reached only via the tunnel, not directly semantically similar to the
    # query — the differentiator this feature is meant to deliver.
    assert not _has_origin(neighbor_item, "semantic")


async def test_tunnel_expansion_whole_wing_when_tunnel_room_unset(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)

    await _remember(client, "semantic target", wing="ProjectAtlas", room="decisions")
    neighbor = await _remember(
        client, "far wing wide neighbor", wing="ProjectAtlasOps", room="anything"
    )
    await _create_tunnel(
        client,
        source_wing="ProjectAtlas",
        source_room="decisions",
        target_wing="ProjectAtlasOps",
        target_room=None,
        label="Atlas Wing Wide",
    )
    await _add_fillers(client)

    body = await _recall_semantic(client, item_budget=2)
    ids = {i["id"] for i in body["items"]}
    assert neighbor["id"] in ids


async def test_tunnel_expansion_no_tunnel_no_expansion(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)

    await _remember(client, "semantic target", wing="Isolated", room="alone")
    unrelated = await _remember(
        client, "far totally unrelated wing", wing="SomewhereElse", room="alone"
    )
    await _add_fillers(client)

    body = await _recall_semantic(client, item_budget=2)
    ids = {i["id"] for i in body["items"]}
    assert unrelated["id"] not in ids


# ---- trust ----


async def test_tunnel_expansion_hides_private_memory_of_other_principal(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)

    await _remember(client, "semantic target", wing="ProjectAtlas", room="decisions")
    await _create_tunnel(
        client,
        source_wing="ProjectAtlas",
        source_room="decisions",
        target_wing="ProjectAtlasOps",
        target_room="runbooks",
        label="Atlas",
    )

    async with _session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT t.id::text AS tenant_id FROM tenants t WHERE t.slug = 'default'"
                )
            )
        ).mappings().one()
        other_id = str(uuid4())
        await session.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES (:pid, :tid, :pname, 'agent')"
            ),
            {"pid": other_id, "tid": row["tenant_id"], "pname": f"other-agent-{other_id[:8]}"},
        )
        private_id = str(uuid4())
        await session.execute(
            text(
                "INSERT INTO memory_items (id, tenant_id, principal_id, content, content_hash, "
                "kind, review_status, visibility, wing, room) "
                "VALUES (:id, :tid, :pid, 'private tunnel-adjacent secret', :hash, 'fact', "
                "'active', 'private', 'ProjectAtlasOps', 'runbooks')"
            ),
            {
                "id": private_id,
                "tid": row["tenant_id"],
                "pid": other_id,
                "hash": f"h-{private_id}",
            },
        )
        await session.commit()

    body = await _recall_semantic(client)
    ids = {i["id"] for i in body["items"]}
    assert private_id not in ids


async def test_tunnel_expansion_respects_workspace_restriction(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)

    await _remember(client, "semantic target", wing="ProjectAtlas", room="decisions")
    await _create_tunnel(
        client,
        source_wing="ProjectAtlas",
        source_room="decisions",
        target_wing="ProjectAtlasOps",
        target_room="runbooks",
        label="Atlas",
    )

    async with _session_factory() as session:
        row = (
            await session.execute(
                text("SELECT id::text AS tenant_id FROM tenants WHERE slug = 'default'")
            )
        ).mappings().one()
        other_ws_id = str(uuid4())
        await session.execute(
            text(
                "INSERT INTO workspaces (id, tenant_id, name, slug) "
                "VALUES (:id, :tid, 'restricted-ws', :slug)"
            ),
            {"id": other_ws_id, "tid": row["tenant_id"], "slug": f"restricted-{other_ws_id[:8]}"},
        )
        restricted_id = str(uuid4())
        await session.execute(
            text(
                "INSERT INTO memory_items (id, tenant_id, principal_id, workspace_id, content, "
                "content_hash, kind, review_status, visibility, wing, room) "
                "SELECT :id, :tid, p.id, :wsid, 'workspace-restricted tunnel neighbor', :hash, "
                "'fact', 'active', 'workspace', 'ProjectAtlasOps', 'runbooks' "
                "FROM principals p WHERE p.tenant_id = :tid AND p.name = 'admin'"
            ),
            {
                "id": restricted_id,
                "tid": row["tenant_id"],
                "wsid": other_ws_id,
                "hash": f"h-{restricted_id}",
            },
        )
        await session.commit()

    body = await _recall_semantic(client)
    ids = {i["id"] for i in body["items"]}
    assert restricted_id not in ids
