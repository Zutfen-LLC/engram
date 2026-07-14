"""End-to-end HTTP smoke tests for graph expansion in semantic recall
(ENG-AUD-012 / F19) — confirms /v1/recall wiring, real trust enforcement, and
real embeddings end to end. Bounded/deterministic/dedup/weight-ordering
behavior is covered more precisely (without vector-similarity confounds) in
tests/test_relationship_recall.py, which calls the expansion module directly.

Requires a live PostgreSQL with the v2 schema + migration 009 applied. Skips
automatically when no DB is reachable, mirroring test_semantic_recall.py.
Embeddings are deterministic fakes so CI never depends on OpenAI.
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
        await conn.execute(text("DELETE FROM memory_embeddings"))
        await conn.execute(text("DELETE FROM memory_items"))
        await conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'other-%'"))
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
_DISTRACTOR_VEC = [0.0, 1.0] + [0.0] * 1534
_TARGET_PREFIXES = ("semantic target", "semantic query")


def _fake_embedding_for(text_value: str) -> list[float]:
    if text_value.startswith(_TARGET_PREFIXES):
        return _TARGET_VEC
    return _DISTRACTOR_VEC


def _patch_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_embedding(
        text_value: str, *_args: object, **_kwargs: object
    ) -> list[float] | None:
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


async def _add_edge(
    source_id: str,
    target_id: str,
    edge_type: str,
    *,
    weight: float | None = None,
) -> None:
    async with _session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO memory_edges (id, tenant_id, source_item_id, target_item_id, "
                "edge_type, weight) "
                "SELECT :id, tenant_id, :source, :target, :edge_type, :weight "
                "FROM memory_items WHERE id = :source"
            ),
            {
                "id": str(uuid4()),
                "source": source_id,
                "target": target_id,
                "edge_type": edge_type,
                "weight": weight,
            },
        )
        await session.commit()


async def _recall_semantic(client: AsyncClient, query: str = "semantic query") -> dict[str, Any]:
    resp = await client.post("/v1/recall", json={"mode": "semantic", "query": query})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _by_id(body: dict[str, Any], item_id: str) -> dict[str, Any] | None:
    return next((i for i in body["items"] if i["id"] == item_id), None)


def _has_origin(item: dict[str, Any], tag: str) -> bool:
    return tag in item["origin"].split("+")


# ---- end-to-end wiring ----


async def test_graph_expansion_wired_into_semantic_recall(client, monkeypatch):
    """A graph-linked memory reachable via an edge is surfaced with a
    relationship reason, and the pipeline still returns the direct semantic
    hit too."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)

    target = await _remember(client, "semantic target")
    linked = await _remember(client, "distractor linked memory")
    await _add_edge(target["id"], linked["id"], "derived_from")

    body = await _recall_semantic(client)
    ids = {i["id"] for i in body["items"]}
    assert target["id"] in ids
    assert linked["id"] in ids

    linked_item = _by_id(body, linked["id"])
    assert _has_origin(linked_item, "graph")
    assert any("linked via derived_from" in r for r in linked_item["reasons"])


async def test_graph_expansion_no_duplicate_rows_when_neighbor_is_also_semantic_hit(
    client, monkeypatch
):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)

    a = await _remember(client, "semantic target alpha")
    b = await _remember(client, "semantic target beta")
    await _add_edge(a["id"], b["id"], "references")

    body = await _recall_semantic(client)
    ids = [i["id"] for i in body["items"]]
    assert ids.count(b["id"]) == 1

    b_item = _by_id(body, b["id"])
    assert _has_origin(b_item, "semantic")
    assert _has_origin(b_item, "graph")
    assert any("linked via references" in r for r in b_item["reasons"])
    assert any("semantic similarity" in r for r in b_item["reasons"])


# ---- trust ----


async def test_graph_expansion_hides_private_memory_of_other_principal(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)

    target = await _remember(client, "semantic target")
    other_id = str(uuid4())
    async with _session_factory() as session:
        row = (
            await session.execute(
                text("SELECT tenant_id FROM memory_items WHERE id = :id"), {"id": target["id"]}
            )
        ).mappings().one()
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
                "kind, review_status, visibility) "
                "VALUES (:id, :tid, :pid, 'private secret of other principal', :hash, 'fact', "
                "'active', 'private')"
            ),
            {"id": private_id, "tid": row["tenant_id"], "pid": other_id, "hash": f"h-{private_id}"},
        )
        await session.commit()
    await _add_edge(target["id"], private_id, "derived_from")

    body = await _recall_semantic(client)
    ids = {i["id"] for i in body["items"]}
    assert private_id not in ids


async def test_graph_expansion_excludes_disputed_neighbor(client, monkeypatch):
    """Disputed items follow the same governance as direct semantic recall
    (which does not include disputed items at all)."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)

    target = await _remember(client, "semantic target")
    disputed = await _remember(client, "distractor disputed neighbor")
    async with _session_factory() as session:
        await session.execute(
            text("UPDATE memory_items SET review_status = 'disputed' WHERE id = :id"),
            {"id": disputed["id"]},
        )
        await session.commit()
    await _add_edge(target["id"], disputed["id"], "contradicts")

    body = await _recall_semantic(client)
    ids = {i["id"] for i in body["items"]}
    assert disputed["id"] not in ids


async def test_graph_expansion_cross_tenant_impossible(client, monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "openai"
    _patch_embeddings(monkeypatch)

    target = await _remember(client, "semantic target")
    async with _session_factory() as session:
        row = (
            await session.execute(
                text("SELECT tenant_id FROM memory_items WHERE id = :id"), {"id": target["id"]}
            )
        ).mappings().one()
        own_tenant_id = row["tenant_id"]

        other_tenant_id = str(uuid4())
        other_principal_id = str(uuid4())
        other_item_id = str(uuid4())
        await session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'Other Tenant', :slug)"),
            {"id": other_tenant_id, "slug": f"other-{other_tenant_id[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES (:pid, :tid, 'other-tenant-agent', 'agent')"
            ),
            {"pid": other_principal_id, "tid": other_tenant_id},
        )
        await session.execute(
            text(
                "INSERT INTO memory_items (id, tenant_id, principal_id, content, content_hash, "
                "kind, review_status) "
                "VALUES (:id, :tid, :pid, 'other tenant secret', :hash, 'fact', 'active')"
            ),
            {
                "id": other_item_id,
                "tid": other_tenant_id,
                "pid": other_principal_id,
                "hash": f"h-{other_item_id}",
            },
        )
        # The edge row lives in the caller's own tenant, but eligibility
        # re-filters every expanded candidate by tenant_id, so a
        # cross-tenant target is a silent no-op, never a leak.
        await session.execute(
            text(
                "INSERT INTO memory_edges (id, tenant_id, source_item_id, target_item_id, "
                "edge_type) VALUES (:id, :tid, :src, :tgt, 'derived_from')"
            ),
            {
                "id": str(uuid4()),
                "tid": own_tenant_id,
                "src": target["id"],
                "tgt": other_item_id,
            },
        )
        await session.commit()

    body = await _recall_semantic(client)
    ids = {i["id"] for i in body["items"]}
    assert other_item_id not in ids
    assert target["id"] in ids
