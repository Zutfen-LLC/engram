# ruff: noqa: E501
"""Regression coverage for shared memory-item read eligibility (ENG-AUD-001).

Exercises the single predicate in ``engram.memory_access`` across every read
path that can return memory-item content: startup recall, semantic recall,
keyword/semantic/hybrid search, item listing, and item detail. Requires a
live PostgreSQL with the v2 schema (migrations/001_init.sql); skips
automatically when no DB is reachable, mirroring tests/test_search.py.

Leakage cases covered:
  - principal A's private memory is invisible to principal B (same tenant)
    via startup recall, keyword search, semantic search, item list, item detail.
  - workspace memory is visible to a member and not to a non-member.
  - an explicit workspace request does not bypass membership.
  - tenant/public memory remains visible to any principal in the tenant.
  - tenant/public memory is still blocked across tenants.
  - hybrid search doesn't leak ineligible memories via either branch.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram import recall as recall_mod
from engram.api.app import create_app
from engram.api.routes import memory as memory_routes
from engram.config import settings
from engram.db import _DEFAULT_PRINCIPAL_NAME, _DEFAULT_TENANT_SLUG, get_session

_test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_test_session_factory = async_sessionmaker(
    _test_engine, class_=AsyncSession, expire_on_commit=False
)


async def _db_ok() -> bool:
    try:
        async with _test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.fixture(autouse=True)
async def _clean_db():
    if not await _db_ok():
        return
    async with _test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM feedback_events"))
        await conn.execute(text("DELETE FROM recall_logs"))
        await conn.execute(text("DELETE FROM item_events"))
        await conn.execute(text("DELETE FROM kg_triples"))
        await conn.execute(text("DELETE FROM memory_embeddings"))
        await conn.execute(text("DELETE FROM memory_items"))
        await conn.execute(text("DELETE FROM workspace_members"))
        await conn.execute(text("DELETE FROM workspaces WHERE slug != 'general'"))
        await conn.execute(text("DELETE FROM tenants WHERE slug != 'default'"))


@pytest.fixture(autouse=True)
def _reset_embedding_provider():
    original = settings.embedding_provider
    yield
    settings.embedding_provider = original


@pytest.fixture(autouse=True)
def _use_test_read_session_factory():
    """Point startup recall's read-oriented session (ENG-AUD-011) at this
    file's own NullPool factory instead of the real pooled app engine.

    A pooled connection bound to one test's event loop can get reused by a
    later test on a different loop → asyncpg "another operation is in
    progress" (SQLAlchemy async pool + per-test event loops). Same pattern as
    test_promotion.py's async_session_factory/owner_session_factory patch.
    """
    import engram.db as db_module

    original = db_module.read_session_factory
    db_module.read_session_factory = _test_session_factory
    yield
    db_module.read_session_factory = original


async def _default_tenant_id() -> str:
    async with _test_session_factory() as session:
        row = (
            await session.execute(
                text("SELECT id::text AS id FROM tenants WHERE slug = :slug"),
                {"slug": _DEFAULT_TENANT_SLUG},
            )
        ).mappings().one()
    return str(row["id"])


async def _default_principal_id(tenant_id: str) -> str:
    async with _test_session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id::text AS id FROM principals "
                    "WHERE tenant_id = :tid AND name = :name"
                ),
                {"tid": tenant_id, "name": _DEFAULT_PRINCIPAL_NAME},
            )
        ).mappings().one()
    return str(row["id"])


async def _seed_principal(tenant_id: str, name: str) -> str:
    # type='user' so manual writes default to review_status='active'
    # (engram.api.routes.memory._resolve_trust_defaults) — these tests are
    # about visibility eligibility, not review-status gating.
    principal_id = str(uuid4())
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES (:id, :tid, :name, 'user')"
            ),
            {"id": principal_id, "tid": tenant_id, "name": name},
        )
        await session.commit()
    return principal_id


async def _seed_tenant(name: str) -> tuple[str, str]:
    """Create a brand-new tenant with its own admin principal + tenant_config."""
    tenant_id = str(uuid4())
    principal_id = str(uuid4())
    async with _test_session_factory() as session:
        await session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
            {"id": tenant_id, "name": name, "slug": f"{name.lower()}-{tenant_id[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES (:id, :tid, 'admin', 'admin')"
            ),
            {"id": principal_id, "tid": tenant_id},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_config (tenant_id, config_version, active) "
                "VALUES (:tid, 'v1', TRUE)"
            ),
            {"tid": tenant_id},
        )
        await session.commit()
    return tenant_id, principal_id


async def _seed_workspace(tenant_id: str, name: str) -> str:
    workspace_id = str(uuid4())
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO workspaces (id, tenant_id, name, slug) "
                "VALUES (:id, :tid, :name, :slug)"
            ),
            {"id": workspace_id, "tid": tenant_id, "name": name, "slug": name.lower()},
        )
        await session.commit()
    return workspace_id


async def _add_member(workspace_id: str, principal_id: str) -> None:
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO workspace_members (id, workspace_id, principal_id, role) "
                "VALUES (:id, :wid, :pid, 'member')"
            ),
            {"id": str(uuid4()), "wid": workspace_id, "pid": principal_id},
        )
        await session.commit()


def _make_client(tenant_id: str, principal_id: str) -> AsyncClient:
    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        async with _test_session_factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
            )
            await session.execute(
                text("SELECT set_config('app.principal_id', :pid, true)"), {"pid": principal_id}
            )
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = _override_get_session
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def _remember(
    client: AsyncClient, content: str, *, visibility: str = "workspace", **payload: object
) -> dict[str, object]:
    body: dict[str, object] = {
        "content": content,
        "source_type": "manual",
        "visibility": visibility,
    }
    body.update(payload)
    resp = await client.post("/v1/remember", json=body)
    assert resp.status_code == 201, resp.text
    # ENG-AUD-008: drain embedding.generate so the embedding is ready before any
    # semantic search in the test.
    await _drain_jobs()
    return resp.json()


async def _drain_jobs(max_iterations: int = 10) -> None:
    """Process queued embedding.generate jobs until empty (ENG-AUD-008)."""
    from engram.worker import process_one_job

    for _ in range(max_iterations):
        processed = await process_one_job(
            worker_id="test",
            session_factory=_test_session_factory,
            app_session_factory=_test_session_factory,
            job_types=["embedding.generate"],
        )
        if not processed:
            return


def _patch_embeddings(monkeypatch: pytest.MonkeyPatch, target_prefix: str) -> None:
    target_vec = [1.0] + [0.0] * 1535
    distractor_vec = [0.0, 1.0] + [0.0] * 1534

    async def fake_embedding(text_value: str) -> list[float] | None:
        return target_vec if text_value.startswith(target_prefix) else distractor_vec

    import engram.embeddings as embeddings_mod

    monkeypatch.setattr(recall_mod, "generate_embedding", fake_embedding)
    monkeypatch.setattr(memory_routes, "generate_embedding", fake_embedding)
    monkeypatch.setattr(embeddings_mod, "generate_embedding", fake_embedding)


# ---- 1. private memory: principal A owns it, principal B (same tenant) cannot see it ----


async def test_private_memory_invisible_to_other_principal_startup_recall(monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id = await _default_tenant_id()
    principal_a = await _seed_principal(tenant_id, f"agent-a-{uuid4().hex[:8]}")
    principal_b = await _seed_principal(tenant_id, f"agent-b-{uuid4().hex[:8]}")

    client_a = _make_client(tenant_id, principal_a)
    client_b = _make_client(tenant_id, principal_b)
    try:
        settings.embedding_provider = "none"
        created = await _remember(client_a, "principal A private secret", visibility="private")

        resp_a = await client_a.post("/v1/recall", json={"mode": "startup"})
        ids_a = {i["id"] for i in resp_a.json()["items"]}
        assert created["id"] in ids_a

        resp_b = await client_b.post("/v1/recall", json={"mode": "startup"})
        ids_b = {i["id"] for i in resp_b.json()["items"]}
        assert created["id"] not in ids_b
    finally:
        await client_a.aclose()
        await client_b.aclose()


async def test_private_memory_invisible_to_other_principal_keyword_search():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id = await _default_tenant_id()
    principal_a = await _seed_principal(tenant_id, f"agent-a-{uuid4().hex[:8]}")
    principal_b = await _seed_principal(tenant_id, f"agent-b-{uuid4().hex[:8]}")

    client_a = _make_client(tenant_id, principal_a)
    client_b = _make_client(tenant_id, principal_b)
    try:
        settings.embedding_provider = "none"
        await _remember(client_a, "keyword private alpha secret", visibility="private")

        resp_a = await client_a.post(
            "/v1/search", json={"query": "keyword private alpha", "mode": "keyword"}
        )
        assert resp_a.json()["total"] == 1

        resp_b = await client_b.post(
            "/v1/search", json={"query": "keyword private alpha", "mode": "keyword"}
        )
        assert resp_b.json()["total"] == 0
    finally:
        await client_a.aclose()
        await client_b.aclose()


async def test_private_memory_invisible_to_other_principal_semantic_search(monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id = await _default_tenant_id()
    principal_a = await _seed_principal(tenant_id, f"agent-a-{uuid4().hex[:8]}")
    principal_b = await _seed_principal(tenant_id, f"agent-b-{uuid4().hex[:8]}")

    client_a = _make_client(tenant_id, principal_a)
    client_b = _make_client(tenant_id, principal_b)
    try:
        settings.embedding_provider = "openai"
        _patch_embeddings(monkeypatch, "semantic private")

        await _remember(client_a, "semantic private target", visibility="private")

        resp_a = await client_a.post(
            "/v1/search", json={"query": "semantic private query", "mode": "semantic"}
        )
        assert resp_a.json()["total"] == 1

        resp_b = await client_b.post(
            "/v1/search", json={"query": "semantic private query", "mode": "semantic"}
        )
        assert resp_b.json()["total"] == 0
    finally:
        await client_a.aclose()
        await client_b.aclose()


async def test_private_memory_invisible_to_other_principal_item_list_and_detail():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id = await _default_tenant_id()
    principal_a = await _seed_principal(tenant_id, f"agent-a-{uuid4().hex[:8]}")
    principal_b = await _seed_principal(tenant_id, f"agent-b-{uuid4().hex[:8]}")

    client_a = _make_client(tenant_id, principal_a)
    client_b = _make_client(tenant_id, principal_b)
    try:
        settings.embedding_provider = "none"
        created = await _remember(client_a, "private list detail secret", visibility="private")
        item_id = created["id"]

        list_a = await client_a.get("/v1/items")
        assert any(i["id"] == item_id for i in list_a.json()["items"])
        detail_a = await client_a.get(f"/v1/items/{item_id}")
        assert detail_a.status_code == 200

        list_b = await client_b.get("/v1/items")
        assert not any(i["id"] == item_id for i in list_b.json()["items"])
        detail_b = await client_b.get(f"/v1/items/{item_id}")
        assert detail_b.status_code == 404
        assert detail_b.json()["detail"] == "Item not found"
    finally:
        await client_a.aclose()
        await client_b.aclose()


# ---- 2. workspace visibility ----


async def test_workspace_memory_visible_to_member_not_nonmember():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id = await _default_tenant_id()
    principal_owner = await _seed_principal(tenant_id, f"owner-{uuid4().hex[:8]}")
    principal_member = await _seed_principal(tenant_id, f"member-{uuid4().hex[:8]}")
    principal_outsider = await _seed_principal(tenant_id, f"outsider-{uuid4().hex[:8]}")
    workspace_id = await _seed_workspace(tenant_id, f"ws-{uuid4().hex[:8]}")
    await _add_member(workspace_id, principal_owner)
    await _add_member(workspace_id, principal_member)

    client_owner = _make_client(tenant_id, principal_owner)
    client_member = _make_client(tenant_id, principal_member)
    client_outsider = _make_client(tenant_id, principal_outsider)
    try:
        settings.embedding_provider = "none"
        created = await _remember(
            client_owner,
            "workspace scoped memory",
            visibility="workspace",
            workspace=(await _workspace_slug(workspace_id)),
        )
        item_id = created["id"]

        detail_member = await client_member.get(f"/v1/items/{item_id}")
        assert detail_member.status_code == 200

        detail_outsider = await client_outsider.get(f"/v1/items/{item_id}")
        assert detail_outsider.status_code == 404
    finally:
        await client_owner.aclose()
        await client_member.aclose()
        await client_outsider.aclose()


async def _workspace_slug(workspace_id: str) -> str:
    async with _test_session_factory() as session:
        row = (
            await session.execute(
                text("SELECT slug FROM workspaces WHERE id = :id"), {"id": workspace_id}
            )
        ).mappings().one()
    return str(row["slug"])


async def test_explicit_workspace_recall_and_search_do_not_bypass_membership(monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id = await _default_tenant_id()
    principal_owner = await _seed_principal(tenant_id, f"owner-{uuid4().hex[:8]}")
    principal_outsider = await _seed_principal(tenant_id, f"outsider-{uuid4().hex[:8]}")
    workspace_id = await _seed_workspace(tenant_id, f"ws-{uuid4().hex[:8]}")
    await _add_member(workspace_id, principal_owner)
    slug = await _workspace_slug(workspace_id)

    client_owner = _make_client(tenant_id, principal_owner)
    client_outsider = _make_client(tenant_id, principal_outsider)
    try:
        settings.embedding_provider = "none"
        created = await _remember(
            client_owner, "explicit workspace fact", visibility="workspace", workspace=slug
        )

        # Owner: explicit workspace startup recall sees it.
        resp_owner = await client_owner.post(
            "/v1/recall", json={"mode": "startup", "workspace": slug}
        )
        assert created["id"] in {i["id"] for i in resp_owner.json()["items"]}

        # Outsider: explicit workspace request must not bypass membership.
        resp_outsider = await client_outsider.post(
            "/v1/recall", json={"mode": "startup", "workspace": slug}
        )
        assert resp_outsider.json()["item_count"] == 0
        assert created["id"] not in {i["id"] for i in resp_outsider.json()["items"]}
    finally:
        await client_owner.aclose()
        await client_outsider.aclose()


# ---- 3. tenant/public visibility ----


async def test_tenant_visible_memory_visible_to_other_principal_same_tenant():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id = await _default_tenant_id()
    principal_a = await _seed_principal(tenant_id, f"agent-a-{uuid4().hex[:8]}")
    principal_b = await _seed_principal(tenant_id, f"agent-b-{uuid4().hex[:8]}")

    client_a = _make_client(tenant_id, principal_a)
    client_b = _make_client(tenant_id, principal_b)
    try:
        settings.embedding_provider = "none"
        created = await _remember(client_a, "tenant wide memory", visibility="tenant")

        list_b = await client_b.get("/v1/items")
        assert any(i["id"] == created["id"] for i in list_b.json()["items"])
        detail_b = await client_b.get(f"/v1/items/{created['id']}")
        assert detail_b.status_code == 200
    finally:
        await client_a.aclose()
        await client_b.aclose()


async def test_cross_tenant_visibility_blocked_for_tenant_and_public():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_a = await _default_tenant_id()
    principal_a = await _seed_principal(tenant_a, f"agent-a-{uuid4().hex[:8]}")
    tenant_b, principal_b = await _seed_tenant(f"TenantB-{uuid4().hex[:8]}")

    client_a = _make_client(tenant_a, principal_a)
    client_b = _make_client(tenant_b, principal_b)
    try:
        settings.embedding_provider = "none"
        created_tenant = await _remember(client_a, "tenant A tenant-visible fact", visibility="tenant")
        created_public = await _remember(client_a, "tenant A public fact", visibility="public")

        # Tenant B's caller must not see tenant A's memories under any visibility.
        list_b = await client_b.get("/v1/items")
        ids_b = {i["id"] for i in list_b.json()["items"]}
        assert created_tenant["id"] not in ids_b
        assert created_public["id"] not in ids_b

        detail_tenant = await client_b.get(f"/v1/items/{created_tenant['id']}")
        assert detail_tenant.status_code == 404
        detail_public = await client_b.get(f"/v1/items/{created_public['id']}")
        assert detail_public.status_code == 404

        search_b = await client_b.post(
            "/v1/search", json={"query": "tenant A", "mode": "keyword"}
        )
        assert search_b.json()["total"] == 0
    finally:
        await client_a.aclose()
        await client_b.aclose()


# ---- 4. hybrid search leakage ----


async def test_hybrid_search_does_not_leak_private_memory_via_either_branch(monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id = await _default_tenant_id()
    principal_a = await _seed_principal(tenant_id, f"agent-a-{uuid4().hex[:8]}")
    principal_b = await _seed_principal(tenant_id, f"agent-b-{uuid4().hex[:8]}")

    client_a = _make_client(tenant_id, principal_a)
    client_b = _make_client(tenant_id, principal_b)
    try:
        settings.embedding_provider = "openai"
        _patch_embeddings(monkeypatch, "hybrid private")

        await _remember(
            client_a, "hybrid private keyword match", visibility="private"
        )
        await _remember(
            client_a, "hybrid private semantic only", visibility="private"
        )
        # A tenant-visible distractor so the search isn't trivially empty.
        await _remember(client_a, "hybrid tenant visible fact", visibility="tenant")

        resp_b = await client_b.post(
            "/v1/search", json={"query": "hybrid private", "mode": "hybrid", "limit": 10}
        )
        assert resp_b.status_code == 200
        contents = [r["content"] for r in resp_b.json()["results"]]
        assert "hybrid private keyword match" not in contents
        assert "hybrid private semantic only" not in contents
    finally:
        await client_a.aclose()
        await client_b.aclose()


# ---- item list pagination + detail (moved from test_items.py: now requires
#      RLS tenant/principal context) ----


async def test_items_cursor_pagination_and_filters():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id = await _default_tenant_id()
    principal_id = await _default_principal_id(tenant_id)
    workspace_id = await _seed_workspace(tenant_id, f"alpha-{uuid4().hex[:8]}")
    beta_workspace_id = await _seed_workspace(tenant_id, f"beta-{uuid4().hex[:8]}")
    await _add_member(workspace_id, principal_id)
    await _add_member(beta_workspace_id, principal_id)
    alpha_slug = await _workspace_slug(workspace_id)

    client = _make_client(tenant_id, principal_id)
    try:
        settings.embedding_provider = "none"
        await _remember(client, "older", wing="west", room="hall", workspace=alpha_slug)
        await _remember(client, "middle", wing="east", room="lobby", workspace=alpha_slug)
        newest = await _remember(
            client, "newest", wing="east", room="lobby", importance=0.9, workspace=alpha_slug
        )
        await client.patch(f"/v1/items/{newest['id']}", json={"pinned": True})
        await _remember(
            client,
            "proposed",
            workspace=alpha_slug,
            source_type="extraction",
        )
        await _remember(
            client,
            "beta item",
            wing="east",
            room="lobby",
            workspace=await _workspace_slug(beta_workspace_id),
            source_type="extraction",
        )

        filtered = await client.get(
            "/v1/items",
            params={
                "workspace": alpha_slug,
                "kind": "fact",
                "wing": "east",
                "room": "lobby",
                "limit": 10,
            },
        )
        assert filtered.status_code == 200
        contents = [item["content"] for item in filtered.json()["items"]]
        assert "proposed" not in contents
        assert "beta item" not in contents
        assert all(item["wing"] == "east" and item["room"] == "lobby" for item in filtered.json()["items"])

        page1 = await client.get("/v1/items", params={"workspace": alpha_slug, "limit": 1})
        assert page1.status_code == 200
        assert page1.json()["next_cursor"]
    finally:
        await client.aclose()


async def test_get_item_detail_includes_events_and_kg():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id = await _default_tenant_id()
    principal_id = await _default_principal_id(tenant_id)
    client = _make_client(tenant_id, principal_id)
    try:
        settings.embedding_provider = "none"
        created = await _remember(client, "detail with kg", visibility="tenant")
        item_id = created["id"]

        await client.patch(f"/v1/items/{item_id}", json={"wing": "north", "reason": "retag"})

        async with _test_session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO kg_triples (id, tenant_id, subject, predicate, object, "
                    "source_item_id, confidence, review_status) VALUES ("
                    ":id, :tid, 'engram', 'relates_to', 'memory', :item_id, 0.9, 'active')"
                ),
                {"id": str(uuid4()), "tid": tenant_id, "item_id": item_id},
            )
            await session.commit()

        response = await client.get(f"/v1/items/{item_id}")
        assert response.status_code == 200
        payload = response.json()
        assert payload["item"]["content"] == "detail with kg"
        # /v1/remember always writes a 'classification' event; the patch above
        # adds a 'metadata_patch' event on top.
        event_types = {e["event_type"] for e in payload["item_events"]}
        assert "metadata_patch" in event_types
        assert len(payload["linked_kg_facts"]) == 1
    finally:
        await client.aclose()


async def test_get_item_missing_returns_404():
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id = await _default_tenant_id()
    principal_id = await _default_principal_id(tenant_id)
    client = _make_client(tenant_id, principal_id)
    try:
        resp = await client.get(f"/v1/items/{uuid4()}")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Item not found"
    finally:
        await client.aclose()
