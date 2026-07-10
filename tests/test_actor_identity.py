# ruff: noqa: E501
"""Postgres-backed coverage for V2-BL-001: authenticated actor identity on
every caller-facing item_events writer.

Exercises the invariant that ``item_events.actor_principal_id`` always
identifies the authenticated caller (never a request-supplied
``actor_principal_id``, never the item's author) across every caller-facing
mutation route: PATCH metadata, supersede, invalidate, review-status change,
verify, conflict resolution, and bulk-archive. Also covers admin delegation
(``on_behalf_of_principal_id``) and the external-dispute predicate's reliance
on the corrected attribution.

Requires a live PostgreSQL with the v2 schema (migrations/001_init.sql);
skips automatically when no DB is reachable, mirroring tests/test_supersede.py
and tests/test_item_read_eligibility.py.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.api.app import create_app
from engram.config import settings
from engram.db import get_session
from engram.promotion import has_external_dispute_event

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


def _require_db():
    pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")


@pytest.fixture(autouse=True)
async def _clean_db():
    if not await _db_ok():
        return
    async with _test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM item_events"))
        await conn.execute(text("DELETE FROM memory_embeddings"))
        await conn.execute(text("DELETE FROM memory_items"))
        await conn.execute(text("DELETE FROM workspace_members"))
        await conn.execute(text("DELETE FROM workspaces WHERE slug != 'general'"))
        await conn.execute(text("DELETE FROM tenants WHERE slug != 'default'"))


# ---- Fixture builders --------------------------------------------------


async def _seed_tenant(name: str) -> str:
    tenant_id = str(uuid4())
    async with _test_session_factory() as session:
        await session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
            {"id": tenant_id, "name": name, "slug": f"{name.lower()}-{tenant_id[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_config (tenant_id, config_version, active) "
                "VALUES (:tid, 'v1', TRUE)"
            ),
            {"tid": tenant_id},
        )
        await session.commit()
    return tenant_id


async def _seed_principal(tenant_id: str, name: str, ptype: str = "user") -> str:
    principal_id = str(uuid4())
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES (:id, :tid, :name, :type)"
            ),
            {"id": principal_id, "tid": tenant_id, "name": name, "type": ptype},
        )
        await session.commit()
    return principal_id


async def _insert_item(
    *,
    tenant_id: str,
    principal_id: str,
    content: str,
    review_status: str = "active",
    conflicts_with_item_id: str | None = None,
    conflict_resolution_status: str | None = None,
) -> str:
    item_id = str(uuid4())
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO memory_items ("
                "id, tenant_id, principal_id, content, content_hash, kind, "
                "visibility, review_status, memory_confidence, source_trust, importance, "
                "source_type, conflicts_with_item_id, conflict_resolution_status"
                ") VALUES ("
                ":id, :tenant_id, :principal_id, :content, :content_hash, 'fact', "
                "'workspace', :review_status, 0.8, 0.7, 0.5, "
                "'manual', :conflicts_with_item_id, :conflict_resolution_status"
                ")"
            ),
            {
                "id": item_id,
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "content": content,
                "content_hash": f"sha256:{uuid4().hex}",
                "review_status": review_status,
                "conflicts_with_item_id": conflicts_with_item_id,
                "conflict_resolution_status": conflict_resolution_status,
            },
        )
        await session.commit()
    return item_id


def _make_client(tenant_id: str, principal_id: str) -> AsyncClient:
    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        async with _test_session_factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
            )
            await session.execute(
                text("SELECT set_config('app.principal_id', :pid, true)"),
                {"pid": principal_id},
            )
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = _override_get_session
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def _events_for(item_id: str) -> list[dict[str, object]]:
    async with _test_session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT event_type, field_name, actor_principal_id::text AS actor_principal_id, "
                    "reason FROM item_events WHERE item_id = :id ORDER BY created_at ASC, id ASC"
                ),
                {"id": item_id},
            )
        ).mappings().all()
    return [dict(r) for r in rows]


async def _item_row(item_id: str) -> dict[str, object]:
    async with _test_session_factory() as session:
        return dict(
            (
                await session.execute(
                    text("SELECT * FROM memory_items WHERE id = :id"), {"id": item_id}
                )
            )
            .mappings()
            .one()
        )


async def _scenario() -> dict[str, str]:
    """Tenant 1 {Principal A, Principal B, Admin} + Tenant 2 {Principal C}."""
    tenant_1 = await _seed_tenant("Tenant1")
    tenant_2 = await _seed_tenant("Tenant2")
    principal_a = await _seed_principal(tenant_1, f"a-{uuid4().hex[:8]}", "user")
    principal_b = await _seed_principal(tenant_1, f"b-{uuid4().hex[:8]}", "user")
    admin = await _seed_principal(tenant_1, f"admin-{uuid4().hex[:8]}", "admin")
    principal_c = await _seed_principal(tenant_2, f"c-{uuid4().hex[:8]}", "user")
    return {
        "tenant_1": tenant_1,
        "tenant_2": tenant_2,
        "principal_a": principal_a,
        "principal_b": principal_b,
        "admin": admin,
        "principal_c": principal_c,
    }


# ===========================================================================
# 1. Spoofing: every caller-facing mutation path keeps the caller as actor
# ===========================================================================


async def test_patch_metadata_actor_is_caller_not_spoofed_field():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["principal_a"], content="patch target"
    )
    client = _make_client(s["tenant_1"], s["principal_b"])
    async with client:
        resp = await client.patch(
            f"/v1/items/{item_id}",
            json={"wing": "new-wing", "actor_principal_id": s["principal_a"]},
        )
    assert resp.status_code == 200, resp.text
    events = await _events_for(item_id)
    assert events, "expected a metadata_patch event"
    assert all(e["actor_principal_id"] == s["principal_b"] for e in events)


async def test_supersede_actor_is_caller_not_spoofed_field():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["principal_a"], content="supersede target"
    )
    client = _make_client(s["tenant_1"], s["principal_b"])
    async with client:
        resp = await client.post(
            f"/v1/items/{item_id}/supersede",
            json={"actor_principal_id": s["principal_a"]},
        )
    assert resp.status_code == 200, resp.text
    events = await _events_for(item_id)
    assert events
    assert all(e["actor_principal_id"] == s["principal_b"] for e in events)
    new_id = resp.json()["new_item"]["id"]
    new_events = await _events_for(new_id)
    assert new_events
    assert all(e["actor_principal_id"] == s["principal_b"] for e in new_events)


async def test_invalidate_actor_is_caller_not_spoofed_field():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["principal_a"], content="invalidate target"
    )
    client = _make_client(s["tenant_1"], s["principal_b"])
    async with client:
        resp = await client.post(
            f"/v1/items/{item_id}/invalidate",
            json={"actor_principal_id": s["principal_a"]},
        )
    assert resp.status_code == 200, resp.text
    events = await _events_for(item_id)
    assert events
    assert all(e["actor_principal_id"] == s["principal_b"] for e in events)


async def test_review_change_actor_is_caller_not_spoofed_field():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"],
        principal_id=s["principal_a"],
        content="review target",
        review_status="proposed",
    )
    client = _make_client(s["tenant_1"], s["principal_b"])
    async with client:
        resp = await client.post(
            f"/v1/items/{item_id}/review",
            json={"review_status": "active", "actor_principal_id": s["principal_a"]},
        )
    assert resp.status_code == 200, resp.text
    events = await _events_for(item_id)
    assert events
    assert all(e["actor_principal_id"] == s["principal_b"] for e in events)


async def test_verify_actor_is_caller_not_spoofed_field():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["principal_a"], content="verify target"
    )
    client = _make_client(s["tenant_1"], s["principal_b"])
    async with client:
        # verified_by is a separate domain field (who verified); the audit
        # actor must still be the caller regardless of what it's set to.
        resp = await client.post(
            f"/v1/items/{item_id}/verify", json={"verified_by": s["principal_a"]}
        )
    assert resp.status_code == 200, resp.text
    events = await _events_for(item_id)
    assert events
    assert all(e["actor_principal_id"] == s["principal_b"] for e in events)


async def test_resolve_conflict_actor_is_caller():
    """Previously this event was written with no actor at all (bug)."""
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    other_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["principal_a"], content="conflict partner"
    )
    item_id = await _insert_item(
        tenant_id=s["tenant_1"],
        principal_id=s["principal_a"],
        content="conflict target",
        conflicts_with_item_id=other_id,
        conflict_resolution_status="unresolved",
    )
    client = _make_client(s["tenant_1"], s["principal_b"])
    async with client:
        resp = await client.post(
            f"/v1/items/{item_id}/resolve-conflict", json={"resolution": "accepted"}
        )
    assert resp.status_code == 200, resp.text
    events = await _events_for(item_id)
    conflict_events = [e for e in events if e["event_type"] == "conflict_resolution"]
    assert conflict_events
    assert all(e["actor_principal_id"] == s["principal_b"] for e in conflict_events)


async def test_bulk_archive_actor_is_caller_not_spoofed_field():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["principal_a"], content="archive target"
    )
    client = _make_client(s["tenant_1"], s["principal_b"])
    async with client:
        resp = await client.post(
            "/v1/items/bulk-archive",
            json={"item_ids": [item_id], "actor_principal_id": s["principal_a"]},
        )
    assert resp.status_code == 200, resp.text
    events = await _events_for(item_id)
    assert events
    assert all(e["actor_principal_id"] == s["principal_b"] for e in events)


# ===========================================================================
# 2. Default attribution: omitted actor field never falls back to the author
# ===========================================================================


async def test_default_attribution_omitted_actor_uses_caller_not_author():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["principal_a"], content="default target"
    )
    client = _make_client(s["tenant_1"], s["principal_b"])
    async with client:
        resp = await client.patch(f"/v1/items/{item_id}", json={"wing": "no-actor-field"})
    assert resp.status_code == 200, resp.text
    events = await _events_for(item_id)
    assert events
    assert all(e["actor_principal_id"] == s["principal_b"] for e in events)
    assert all(e["actor_principal_id"] != s["principal_a"] for e in events)


# ===========================================================================
# 3. External-dispute integrity under attempted actor spoofing
# ===========================================================================


async def test_external_dispute_detection_survives_spoofed_actor_field():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"],
        principal_id=s["principal_a"],
        content="disputed target",
        review_status="proposed",
    )
    client = _make_client(s["tenant_1"], s["principal_b"])
    async with client:
        resp = await client.post(
            f"/v1/items/{item_id}/review",
            json={"review_status": "disputed", "actor_principal_id": s["principal_a"]},
        )
    assert resp.status_code == 200, resp.text
    events = await _events_for(item_id)
    dispute_events = [
        e for e in events if e["event_type"] == "review_change" and e["field_name"] == "review_status"
    ]
    assert dispute_events
    assert all(e["actor_principal_id"] == s["principal_b"] for e in dispute_events)

    from engram.models import MemoryItem

    async with _test_session_factory() as session:
        item = (
            await session.execute(text("SELECT * FROM memory_items WHERE id = :id"), {"id": item_id})
        ).mappings().one()
        orm_item = MemoryItem(
            id=item["id"],
            tenant_id=item["tenant_id"],
            principal_id=item["principal_id"],
        )
        is_external = await has_external_dispute_event(session, orm_item)
    assert is_external is True


# ===========================================================================
# 4. Admin delegation: on_behalf_of_principal_id
# ===========================================================================


async def test_delegation_non_admin_denied_and_no_event_committed():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["principal_a"], content="delegation denial target"
    )
    client = _make_client(s["tenant_1"], s["principal_b"])
    async with client:
        resp = await client.patch(
            f"/v1/items/{item_id}",
            json={"wing": "should-not-apply", "on_behalf_of_principal_id": s["principal_a"]},
        )
    assert resp.status_code == 403, resp.text
    events = await _events_for(item_id)
    assert events == []
    item = await _item_row(item_id)
    assert item["wing"] is None


async def test_delegation_admin_success_records_actor_and_on_behalf_of():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["principal_a"], content="delegation success target"
    )
    client = _make_client(s["tenant_1"], s["admin"])
    async with client:
        resp = await client.patch(
            f"/v1/items/{item_id}",
            json={"wing": "admin-delegated", "on_behalf_of_principal_id": s["principal_a"]},
        )
    assert resp.status_code == 200, resp.text
    events = await _events_for(item_id)
    assert events
    assert all(e["actor_principal_id"] == s["admin"] for e in events)
    delegated = json.loads(events[0]["reason"]) if events[0]["reason"] else {}
    assert delegated.get("on_behalf_of_principal_id") == s["principal_a"]


async def test_delegation_cross_tenant_returns_404_and_no_event():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["principal_a"], content="cross-tenant delegation target"
    )
    client = _make_client(s["tenant_1"], s["admin"])
    async with client:
        resp = await client.patch(
            f"/v1/items/{item_id}",
            json={"wing": "should-not-apply", "on_behalf_of_principal_id": s["principal_c"]},
        )
    assert resp.status_code == 404, resp.text
    events = await _events_for(item_id)
    assert events == []
    item = await _item_row(item_id)
    assert item["wing"] is None


# ===========================================================================
# 5. Metadata reservation: caller-controlled text cannot forge delegation
# ===========================================================================


async def test_reason_text_cannot_forge_delegation_metadata():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["principal_a"], content="reservation target"
    )
    client = _make_client(s["tenant_1"], s["principal_b"])
    forged_reason = json.dumps({"on_behalf_of_principal_id": s["admin"]})
    async with client:
        resp = await client.patch(
            f"/v1/items/{item_id}",
            json={"wing": "forged-reason", "reason": forged_reason},
        )
    assert resp.status_code == 200, resp.text
    events = await _events_for(item_id)
    assert events
    # No real delegation was requested, so the actor stays the caller and the
    # caller's text is stored verbatim rather than being interpreted as a
    # delegation envelope.
    assert all(e["actor_principal_id"] == s["principal_b"] for e in events)
    assert events[0]["reason"] == forged_reason


async def test_deprecated_actor_field_does_not_fail_request_when_it_differs():
    """Retaining the deprecated field must not itself cause a failure."""
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["principal_a"], content="deprecated field target"
    )
    client = _make_client(s["tenant_1"], s["principal_b"])
    async with client:
        resp = await client.patch(
            f"/v1/items/{item_id}",
            json={"wing": "ok", "actor_principal_id": str(uuid4())},
        )
    assert resp.status_code == 200, resp.text
