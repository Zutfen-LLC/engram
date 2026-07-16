# ruff: noqa: E501
"""Adversarial proof for review reporting and KG resource eligibility.

Unlike the ordinary route tests, every request here authenticates with a real
API key and runs on the non-owner application role.  The owner role is used
only to arrange and inspect durable state.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import aliased

from engram.api.app import create_app
from engram.auth import (
    DIGEST_ALGORITHM,
    digest_api_key_secret,
    generate_api_key,
    parse_api_key,
    reset_principal_cache,
)
from engram.config import settings
from engram.db import apply_rls_context
from engram.memory_access import eligibility_expression, eligibility_sql
from engram.models import MemoryItem

pytestmark = pytest.mark.asyncio


def _owner_url() -> str | None:
    return os.getenv("ENGRAM_OWNER_DATABASE_URL") or os.getenv("ENGRAM_DATABASE_URL")


def _app_url() -> str | None:
    return os.getenv("ENGRAM_APP_DATABASE_URL")


@pytest.fixture
async def corpus(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict[str, Any]]:
    owner_url, app_url = _owner_url(), _app_url()
    if not owner_url or not app_url:
        pytest.skip("requires a live PostgreSQL with the v2 schema")
    owner = create_async_engine(owner_url)
    app_engine = create_async_engine(app_url)
    owner_factory = async_sessionmaker(owner, class_=AsyncSession, expire_on_commit=False)
    app_factory = async_sessionmaker(app_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with owner.connect() as conn:
            await conn.execute(text("SELECT 1"))
        async with app_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        await owner.dispose()
        await app_engine.dispose()
        pytest.skip("requires a live PostgreSQL with the v2 schema")

    tag = uuid.uuid4().hex[:10]
    ids: dict[str, Any] = {"tag": tag, "owner": owner, "app_engine": app_engine}
    ids["ta"], ids["tb"] = uuid.uuid4(), uuid.uuid4()
    ids["shared"], ids["restricted"], ids["ws_b"] = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    principals = {
        "user": (uuid.uuid4(), "user", ["read", "review", "write"]),
        "admin": (uuid.uuid4(), "admin", ["read", "admin"]),
        "agent_a": (uuid.uuid4(), "agent", ["read", "review", "write"]),
        "agent_b": (uuid.uuid4(), "agent", ["read", "write"]),
        "system_a": (uuid.uuid4(), "system", ["read", "write"]),
        "system_b": (uuid.uuid4(), "system", ["read", "write"]),
        "agent_review": (uuid.uuid4(), "agent", ["read", "review"]),
        "no_write": (uuid.uuid4(), "agent", ["read", "review"]),
        "other_tenant": (uuid.uuid4(), "user", ["read", "review", "write"]),
    }
    ids["principals"] = {name: value[0] for name, value in principals.items()}
    ids["types"] = {name: value[1] for name, value in principals.items()}
    ids["keys"] = {}
    ids["items"] = {}
    ids["triples"] = {}

    async with owner.begin() as conn:
        for tid, label in ((ids["ta"], "a"), (ids["tb"], "b")):
            await conn.execute(text("INSERT INTO tenants (id,name,slug) VALUES (:id,:n,:s)"), {"id": tid, "n": f"integrity-{label}-{tag}", "s": f"integrity-{label}-{tag}"})
            await conn.execute(text("INSERT INTO tenant_config (tenant_id,config_version,active) VALUES (:id,'v1',true)"), {"id": tid})
        for name, (pid, ptype, _scopes) in principals.items():
            tid = ids["tb"] if name == "other_tenant" else ids["ta"]
            await conn.execute(text("INSERT INTO principals (id,tenant_id,name,type) VALUES (:id,:tid,:n,:t)"), {"id": pid, "tid": tid, "n": f"{name}-{tag}", "t": ptype})
        for wid, tid, slug in ((ids["shared"], ids["ta"], f"shared-{tag}"), (ids["restricted"], ids["ta"], f"restricted-{tag}"), (ids["ws_b"], ids["tb"], f"b-{tag}")):
            await conn.execute(text("INSERT INTO workspaces (id,tenant_id,name,slug) VALUES (:id,:tid,:s,:s)"), {"id": wid, "tid": tid, "s": slug})
        for wid, who in ((ids["shared"], "user"), (ids["shared"], "agent_a"), (ids["restricted"], "agent_b"), (ids["ws_b"], "other_tenant")):
            await conn.execute(text("INSERT INTO workspace_members (id,workspace_id,principal_id) VALUES (:id,:wid,:pid)"), {"id": uuid.uuid4(), "wid": wid, "pid": ids["principals"][who]})
        for name, (pid, _ptype, scopes) in principals.items():
            token = generate_api_key()
            parsed = parse_api_key(token)
            assert parsed.key_id
            ids["keys"][name] = token
            tid = ids["tb"] if name == "other_tenant" else ids["ta"]
            await conn.execute(text("INSERT INTO api_keys (id,tenant_id,principal_id,key_id,secret_digest,digest_algorithm,scopes,label) VALUES (:id,:tid,:pid,:kid,:digest,:algorithm,:scopes,:label)"), {"id": uuid.uuid4(), "tid": tid, "pid": pid, "kid": parsed.key_id, "digest": digest_api_key_secret(parsed.secret), "algorithm": DIGEST_ALGORITHM, "scopes": scopes, "label": f"integrity-{tag}-{name}"})

    async def item(name: str, *, owner_name: str = "agent_a", visibility: str = "private", workspace: uuid.UUID | None = None, status: str = "active", confidence: float = 0.8, stale: bool = True, tenant_b: bool = False) -> uuid.UUID:
        iid = uuid.uuid4()
        tid = ids["tb"] if tenant_b else ids["ta"]
        pid = ids["principals"]["other_tenant" if tenant_b else owner_name]
        async with owner.begin() as conn:
            await conn.execute(text("INSERT INTO memory_items (id,tenant_id,workspace_id,principal_id,content,content_hash,kind,visibility,review_status,memory_confidence,source_trust,importance,source_type,valid_from,last_recalled_at) VALUES (:id,:tid,:wid,:pid,:content,:hash,:kind,:visibility,:status,:confidence,.8,.5,'manual',:vf,:lr)"), {"id": iid, "tid": tid, "wid": workspace, "pid": pid, "content": f"{tag}:{name}", "hash": f"sha256:{iid.hex}", "kind": "decision" if confidence < .4 else "fact", "visibility": visibility, "status": status, "confidence": confidence, "vf": datetime.now(UTC) - timedelta(days=120), "lr": None if stale else datetime.now(UTC)})
        ids["items"][name] = iid
        return iid

    async def triple(name: str, source: uuid.UUID | None, *, author: str | None = "agent_a", tenant_b: bool = False, valid_from: datetime | None = None) -> uuid.UUID:
        kid = uuid.uuid4()
        async with owner.begin() as conn:
            await conn.execute(text("INSERT INTO kg_triples (id,tenant_id,principal_id,subject,predicate,object,source_item_id,review_status,valid_from) VALUES (:id,:tid,:pid,:s,:p,:o,:source,'active',:vf)"), {"id": kid, "tid": ids["tb"] if tenant_b else ids["ta"], "pid": ids["principals"].get(author) if author else None, "s": f"entity-{tag}", "p": "rel", "o": f"{name}-{tag}", "source": source, "vf": valid_from or datetime.now(UTC) - timedelta(days=1)})
        ids["triples"][name] = kid
        return kid

    ids["item"] = item
    ids["triple"] = triple
    import engram.db as db_module

    monkeypatch.setattr(db_module, "async_session_factory", app_factory)
    monkeypatch.setattr(db_module, "read_session_factory", app_factory)
    monkeypatch.setattr(db_module, "owner_session_factory", owner_factory)
    settings.auth_enabled = True
    reset_principal_cache()
    ids["client"] = AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")
    try:
        yield ids
    finally:
        await ids["client"].aclose()
        async with owner.begin() as conn:
            await conn.execute(text("DROP TRIGGER IF EXISTS integrity_fail_event ON item_events"))
            await conn.execute(text("DROP FUNCTION IF EXISTS integrity_fail_event()"))
            await conn.execute(text("DROP TRIGGER IF EXISTS integrity_fail_triple ON kg_triples"))
            await conn.execute(text("DROP FUNCTION IF EXISTS integrity_fail_triple()"))
            await conn.execute(text("DELETE FROM api_keys WHERE label LIKE :p"), {"p": f"integrity-{tag}-%"})
            await conn.execute(text("DELETE FROM item_events WHERE actor_principal_id IN (SELECT id FROM principals WHERE tenant_id IN (:a,:b))"), {"a": ids["ta"], "b": ids["tb"]})
            await conn.execute(text("DELETE FROM tenants WHERE id IN (:a,:b)"), {"a": ids["ta"], "b": ids["tb"]})
        await owner.dispose()
        await app_engine.dispose()


def _h(c: dict[str, Any], actor: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {c['keys'][actor]}"}


async def test_role_force_rls_and_unfiltered_cross_tenant_backstop(corpus: dict[str, Any]) -> None:
    c = corpus
    async with c["app_engine"].connect() as conn:
        role = (await conn.execute(text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"))).one()
        assert role == (False, False)
        forced = (await conn.execute(text("SELECT relname,relforcerowsecurity FROM pg_class WHERE relname IN ('memory_items','kg_triples','item_events')"))).all()
        assert dict(forced) == {"memory_items": True, "kg_triples": True, "item_events": True}
    cross = await c["item"]("cross", tenant_b=True, visibility="public")
    factory = async_sessionmaker(c["app_engine"], class_=AsyncSession)
    async with factory() as session:
        await apply_rls_context(session, tenant_id=c["ta"], principal_id=c["principals"]["user"])
        # Deliberately no tenant predicate: FORCE RLS is the proof.
        assert (await session.execute(text("SELECT id FROM memory_items WHERE id=:id"), {"id": cross})).first() is None


async def test_review_conflict_visibility_scope_never_widens(corpus: dict[str, Any]) -> None:
    c = corpus
    eligible = await c["item"]("conflict-visible", owner_name="user", visibility="private")
    shared = await c["item"]("counter-shared", workspace=c["shared"], visibility="workspace")
    private = await c["item"]("counter-private", owner_name="agent_b", visibility="private")
    restricted = await c["item"]("counter-restricted", owner_name="agent_b", workspace=c["restricted"], visibility="workspace")
    hidden_item = await c["item"]("hidden-conflict", owner_name="agent_b", visibility="private")
    async with c["owner"].begin() as conn:
        for iid, other in ((eligible, shared), (await c["item"]("private-pair", owner_name="user", visibility="private"), private), (await c["item"]("workspace-pair", owner_name="user", visibility="private"), restricted), (hidden_item, shared)):
            await conn.execute(text("UPDATE memory_items SET conflicts_with_item_id=:other,conflict_type='contradiction',conflict_resolution_status='unresolved' WHERE id=:id"), {"id": iid, "other": other})
    for actor in ("user", "agent_review", "admin"):
        response = await c["client"].get("/v1/review/conflicts", headers=_h(c, actor))
        assert response.status_code == 200
        body = response.json()
        visible = {row["id"] for row in body["items"]}
        expected = {str(eligible)} if actor == "user" else set()
        assert visible == expected
        assert body["total"] == len(expected)
        assert str(private) not in response.text and str(restricted) not in response.text


async def test_stale_stats_and_workspace_filters_are_caller_relative(corpus: dict[str, Any]) -> None:
    c = corpus
    expected = {
        await c["item"]("own-stale", owner_name="user", visibility="private", confidence=.2),
        await c["item"]("shared-stale", workspace=c["shared"], visibility="workspace", confidence=.5),
        # ENG-SCOPE-001: workspace='workspace' with NULL workspace_id is
        # unrepresentable after migration 021 — 'tenant' is the truthful
        # post-migration label for what this row used to mean.
        await c["item"]("workspace-null", workspace=None, visibility="tenant", confidence=.8),
        await c["item"]("tenant-stale", visibility="tenant"),
        await c["item"]("public-stale", visibility="public"),
    }
    await c["item"]("other-private", owner_name="agent_b", visibility="private", confidence=.2)
    await c["item"]("restricted", owner_name="agent_b", workspace=c["restricted"], visibility="workspace", confidence=.5)
    await c["item"]("fresh", owner_name="user", visibility="private", stale=False)
    await c["item"]("rejected", owner_name="user", visibility="private", status="rejected")
    for actor in ("user", "agent_review", "admin"):
        stale = (await c["client"].get("/v1/review/stale", headers=_h(c, actor))).json()
        actor_ids = {uuid.UUID(row["id"]) for row in stale["items"]}
        if actor == "user":
            assert actor_ids == expected
        stats = (await c["client"].get("/v1/review/stats", headers=_h(c, actor))).json()
        assert stats["total"] == sum(stats["by_review_status"].values())
        assert set(stats["by_confidence"]) == {"low", "medium", "high"}
    shared = (await c["client"].get("/v1/review/stale", params={"workspace": f"shared-{c['tag']}"}, headers=_h(c, "user"))).json()
    assert {row["id"] for row in shared["items"]} == {str(c["items"]["shared-stale"])}
    for workspace in (f"restricted-{c['tag']}", "unknown-workspace"):
        assert (await c["client"].get("/v1/review/stale", params={"workspace": workspace}, headers=_h(c, "user"))).json()["total"] == 0


async def test_kg_query_timeline_direction_predicate_as_of_and_orphans(corpus: dict[str, Any]) -> None:
    c = corpus
    sources = {
        "own": await c["item"]("kg-own", owner_name="user", visibility="private"),
        "shared": await c["item"]("kg-shared", workspace=c["shared"], visibility="workspace"),
        "null": await c["item"]("kg-null", visibility="tenant"),
        "tenant": await c["item"]("kg-tenant", visibility="tenant"),
        "public": await c["item"]("kg-public", visibility="public"),
        "private": await c["item"]("kg-private", owner_name="agent_b", visibility="private"),
        "restricted": await c["item"]("kg-restricted", owner_name="agent_b", workspace=c["restricted"], visibility="workspace"),
    }
    visible = {await c["triple"](name, source) for name, source in list(sources.items())[:5]}
    hidden = {await c["triple"](name, source) for name, source in list(sources.items())[5:]}
    hidden.add(await c["triple"]("orphan", None))
    cross_source = await c["item"]("kg-cross", tenant_b=True, visibility="public")
    hidden.add(await c["triple"]("cross", cross_source, tenant_b=True, author="other_tenant"))
    for direction in ("outbound", "both"):
        response = await c["client"].get("/v1/kg/query", params={"entity": f"entity-{c['tag']}", "direction": direction, "predicate": "rel"}, headers=_h(c, "user"))
        assert response.status_code == 200
        assert {uuid.UUID(row["id"]) for row in response.json()} == visible
        assert not any(str(value) in response.text for value in hidden)
    assert (await c["client"].get("/v1/kg/query", params={"entity": f"entity-{c['tag']}", "direction": "inbound"}, headers=_h(c, "user"))).json() == []
    timeline = (await c["client"].get("/v1/kg/timeline", params={"entity": f"entity-{c['tag']}"}, headers=_h(c, "user"))).json()
    assert {uuid.UUID(row["id"]) for row in timeline["facts"]} == visible
    assert timeline["total"] == len(visible)
    assert (await c["client"].get("/v1/kg/query", params={"entity": "x", "as_of": "' OR 1=1 --"}, headers=_h(c, "user"))).status_code == 422


async def test_invalidation_authz_uuid_isolation_idempotency_and_audit(corpus: dict[str, Any]) -> None:
    c = corpus
    source = await c["item"]("invalidate-source", owner_name="user", visibility="tenant")
    own = await c["triple"]("duplicate", source, author="agent_a")
    other = await c["triple"]("duplicate", source, author="agent_b")
    null_author = await c["triple"]("duplicate", source, author=None)
    denied = await c["client"].post("/v1/kg/invalidate", json={"triple_id": str(other)}, headers=_h(c, "agent_a"))
    assert denied.status_code == 403
    assert (await c["client"].post("/v1/kg/invalidate", json={"triple_id": str(null_author)}, headers=_h(c, "agent_a"))).status_code == 403
    assert (await c["client"].post("/v1/kg/invalidate", json={"subject": "x", "predicate": "rel", "object": "y"}, headers=_h(c, "agent_a"))).status_code == 422
    first = await c["client"].post("/v1/kg/invalidate", json={"triple_id": str(own), "reason": "first reason"}, headers=_h(c, "agent_a"))
    assert first.status_code == 200 and first.json()["status"] == "invalidated" and first.json()["count"] == 1
    second = await c["client"].post("/v1/kg/invalidate", json={"triple_id": str(own), "reason": "must not persist"}, headers=_h(c, "agent_a"))
    assert second.json()["status"] == "unchanged" and second.json()["event"] is None
    async with c["owner"].connect() as conn:
        rows = (await conn.execute(text("SELECT item_id,event_type,field_name,actor_principal_id,reason,new_value FROM item_events WHERE event_type='kg_invalidate' AND new_value LIKE :needle"), {"needle": f"%{own}%"})).mappings().all()
        assert len(rows) == 1
        event = rows[0]
        assert event["item_id"] == source and event["actor_principal_id"] == c["principals"]["agent_a"]
        assert event["field_name"] == "kg_triple.valid_to" and event["reason"] == "first reason"
        details = json.loads(event["new_value"])
        assert details["triple_id"] == str(own) and details["previous_valid_to"] is None
        states = dict((await conn.execute(text("SELECT id,valid_to FROM kg_triples WHERE id IN (:a,:b,:n)"), {"a": own, "b": other, "n": null_author})).all())
        assert states[own] is not None and states[other] is None and states[null_author] is None
    # Human/admin override records the real caller, including NULL authors.
    allowed = await c["client"].post("/v1/kg/invalidate", json={"triple_id": str(null_author)}, headers=_h(c, "admin"))
    assert allowed.status_code == 200


async def test_invalidation_non_disclosure_scope_and_concurrency(corpus: dict[str, Any]) -> None:
    c = corpus
    private = await c["item"]("hidden-invalidation", owner_name="agent_b", visibility="private")
    hidden = await c["triple"]("hidden-invalidation", private, author="agent_b")
    for target in (hidden, uuid.uuid4(), await c["triple"]("orphan-invalidation", None)):
        assert (await c["client"].post("/v1/kg/invalidate", json={"triple_id": str(target)}, headers=_h(c, "user"))).status_code == 404
    visible = await c["item"]("concurrent-source", visibility="tenant")
    target = await c["triple"]("concurrent", visible, author="agent_b")
    assert (await c["client"].post("/v1/kg/invalidate", json={"triple_id": str(target)}, headers=_h(c, "no_write"))).status_code == 403
    results = await asyncio.gather(*[
        c["client"].post("/v1/kg/invalidate", json={"triple_id": str(target), "reason": actor}, headers=_h(c, actor))
        for actor in ("user", "admin")
    ])
    assert [r.status_code for r in results] == [200, 200]
    assert sorted(r.json()["status"] for r in results) == ["invalidated", "unchanged"]
    winner = next(actor for actor, response in zip(("user", "admin"), results, strict=True) if response.json()["status"] == "invalidated")
    async with c["owner"].connect() as conn:
        rows = (await conn.execute(text("SELECT actor_principal_id FROM item_events WHERE event_type='kg_invalidate' AND new_value LIKE :needle"), {"needle": f"%{target}%"})).all()
        assert rows == [(c["principals"][winner],)]


async def test_event_failure_rolls_back_and_trigger_cleanup_restores_service(corpus: dict[str, Any]) -> None:
    c = corpus
    source = await c["item"]("rollback-source", visibility="tenant")
    target = await c["triple"]("rollback", source, author="agent_a")
    async with c["owner"].begin() as conn:
        await conn.execute(text("CREATE FUNCTION integrity_fail_event() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.event_type='kg_invalidate' THEN RAISE EXCEPTION 'injected event failure'; END IF; RETURN NEW; END $$"))
        await conn.execute(text("CREATE TRIGGER integrity_fail_event BEFORE INSERT ON item_events FOR EACH ROW EXECUTE FUNCTION integrity_fail_event()"))
    with pytest.raises(SQLAlchemyError):
        await c["client"].post("/v1/kg/invalidate", json={"triple_id": str(target)}, headers=_h(c, "agent_a"))
    async with c["owner"].begin() as conn:
        assert (await conn.execute(text("SELECT valid_to FROM kg_triples WHERE id=:id"), {"id": target})).scalar_one() is None
        assert (await conn.execute(text("SELECT count(*) FROM item_events WHERE event_type='kg_invalidate' AND new_value LIKE :needle"), {"needle": f"%{target}%"})).scalar_one() == 0
        await conn.execute(text("DROP TRIGGER integrity_fail_event ON item_events"))
        await conn.execute(text("DROP FUNCTION integrity_fail_event()"))
    assert (await c["client"].post("/v1/kg/invalidate", json={"triple_id": str(target)}, headers=_h(c, "agent_a"))).status_code == 200


async def test_eligibility_expression_sql_and_alias_parity(corpus: dict[str, Any]) -> None:
    c = corpus
    for name, kwargs in (
        ("private-owner", {"owner_name": "user", "visibility": "private"}),
        ("private-other", {"owner_name": "agent_b", "visibility": "private"}),
        ("workspace-member", {"workspace": c["shared"], "visibility": "workspace"}),
        ("workspace-nonmember", {"workspace": c["restricted"], "visibility": "workspace", "owner_name": "agent_b"}),
        # ENG-SCOPE-001: visibility='workspace' with NULL workspace_id is no
        # longer a constructible row (DB CHECK constraint) — the equivalent
        # "workspace-null-parity" case from before this slice is covered
        # instead by tests/test_memory_access_unit.py, which exercises it as
        # a manually-constructed non-Postgres row precisely because real
        # Postgres now rejects it outright.
        ("tenant-parity", {"visibility": "tenant"}),
        ("public-parity", {"visibility": "public"}),
    ):
        await c["item"](name, **kwargs)
    factory = async_sessionmaker(c["app_engine"], class_=AsyncSession)
    async with factory() as session:
        await apply_rls_context(session, tenant_id=c["ta"], principal_id=c["principals"]["user"])
        orm = set((await session.execute(select(MemoryItem.id).where(eligibility_expression(c["principals"]["user"])))).scalars())
        raw = set((await session.execute(text(f"SELECT id FROM memory_items WHERE {eligibility_sql()}"), {"caller_principal_id": str(c["principals"]["user"])})).scalars())
        counterpart = aliased(MemoryItem)
        alias_ids = set((await session.execute(select(counterpart.id).where(eligibility_expression(c["principals"]["user"], item_entity=counterpart)))).scalars())
        assert orm == raw == alias_ids
