# ruff: noqa: E501
"""Real-Postgres HTTP coverage for V2-BL-003A: agent/user/admin review and
verification authorization through the actual FastAPI routes.

Complements the pure-function matrix in ``tests/test_review_policy.py`` by
proving the same authority rules hold end-to-end — through
``POST /v1/items/{id}/review`` and ``POST /v1/items/{id}/verify`` — including
eligibility ordering (inaccessible items stay a non-disclosing 404 before any
transition authorization is revealed) and no-op/delegation consistency
(Problem 2: a no-op review request must still validate delegation).

Requires a live PostgreSQL with the v2 schema (migrations/001_init.sql);
skips automatically when no DB is reachable, mirroring
tests/test_actor_identity.py and tests/test_promotion.py.
"""

from __future__ import annotations

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


async def _seed_principal(tenant_id: str, name: str, ptype: str) -> str:
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
    review_status: str = "proposed",
    human_verified: bool = False,
) -> str:
    item_id = str(uuid4())
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO memory_items ("
                "id, tenant_id, principal_id, content, content_hash, kind, "
                "visibility, review_status, memory_confidence, source_trust, importance, "
                "source_type, human_verified"
                ") VALUES ("
                ":id, :tenant_id, :principal_id, :content, :content_hash, 'fact', "
                "'workspace', :review_status, 0.8, 0.7, 0.5, "
                "'manual', :human_verified"
                ")"
            ),
            {
                "id": item_id,
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "content": content,
                "content_hash": f"sha256:{uuid4().hex}",
                "review_status": review_status,
                "human_verified": human_verified,
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
                    "old_value, new_value, reason FROM item_events WHERE item_id = :id "
                    "ORDER BY created_at ASC, id ASC"
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
    """Tenant 1 {agent, user, admin, other_user} + Tenant 2 {outsider}."""
    tenant_1 = await _seed_tenant("RevAuthT1")
    tenant_2 = await _seed_tenant("RevAuthT2")
    agent = await _seed_principal(tenant_1, f"agent-{uuid4().hex[:8]}", "agent")
    other_agent = await _seed_principal(tenant_1, f"other-agent-{uuid4().hex[:8]}", "agent")
    user = await _seed_principal(tenant_1, f"user-{uuid4().hex[:8]}", "user")
    other_user = await _seed_principal(tenant_1, f"other-user-{uuid4().hex[:8]}", "user")
    admin = await _seed_principal(tenant_1, f"admin-{uuid4().hex[:8]}", "admin")
    outsider = await _seed_principal(tenant_2, f"outsider-{uuid4().hex[:8]}", "user")
    return {
        "tenant_1": tenant_1,
        "tenant_2": tenant_2,
        "agent": agent,
        "other_agent": other_agent,
        "user": user,
        "other_user": other_user,
        "admin": admin,
        "outsider": outsider,
    }


# ===========================================================================
# Agent authority
# ===========================================================================


async def test_agent_cannot_activate_proposed_item():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["agent"], content="a", review_status="proposed"
    )
    async with _make_client(s["tenant_1"], s["agent"]) as client:
        resp = await client.post(f"/v1/items/{item_id}/review", json={"review_status": "active"})
    assert resp.status_code == 403, resp.text


async def test_agent_cannot_reactivate_disputed_item():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["agent"], content="a", review_status="disputed"
    )
    async with _make_client(s["tenant_1"], s["agent"]) as client:
        resp = await client.post(f"/v1/items/{item_id}/review", json={"review_status": "active"})
    assert resp.status_code == 403, resp.text


async def test_agent_cannot_reactivate_rejected_item():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["agent"], content="a", review_status="rejected"
    )
    async with _make_client(s["tenant_1"], s["agent"]) as client:
        resp = await client.post(f"/v1/items/{item_id}/review", json={"review_status": "active"})
    assert resp.status_code == 403, resp.text


async def test_agent_cannot_reject_item():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["agent"], content="a", review_status="active"
    )
    async with _make_client(s["tenant_1"], s["agent"]) as client:
        resp = await client.post(f"/v1/items/{item_id}/review", json={"review_status": "rejected"})
    assert resp.status_code == 403, resp.text


async def test_agent_cannot_archive_another_principals_proposal():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"],
        principal_id=s["other_agent"],
        content="a",
        review_status="proposed",
    )
    async with _make_client(s["tenant_1"], s["agent"]) as client:
        resp = await client.post(f"/v1/items/{item_id}/review", json={"review_status": "archived"})
    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize("initial", ["active", "disputed"])
async def test_agent_cannot_archive_active_or_disputed_item(initial: str):
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["agent"], content="a", review_status=initial
    )
    async with _make_client(s["tenant_1"], s["agent"]) as client:
        resp = await client.post(f"/v1/items/{item_id}/review", json={"review_status": "archived"})
    assert resp.status_code == 403, resp.text


async def test_agent_can_dispute_proposed_item():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["other_agent"], content="a", review_status="proposed"
    )
    async with _make_client(s["tenant_1"], s["agent"]) as client:
        resp = await client.post(f"/v1/items/{item_id}/review", json={"review_status": "disputed"})
    assert resp.status_code == 200, resp.text
    assert (await _item_row(item_id))["review_status"] == "disputed"


async def test_agent_can_dispute_active_item():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["other_agent"], content="a", review_status="active"
    )
    async with _make_client(s["tenant_1"], s["agent"]) as client:
        resp = await client.post(f"/v1/items/{item_id}/review", json={"review_status": "disputed"})
    assert resp.status_code == 200, resp.text
    assert (await _item_row(item_id))["review_status"] == "disputed"


async def test_agent_can_archive_own_proposed_item():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["agent"], content="a", review_status="proposed"
    )
    async with _make_client(s["tenant_1"], s["agent"]) as client:
        resp = await client.post(f"/v1/items/{item_id}/review", json={"review_status": "archived"})
    assert resp.status_code == 200, resp.text
    assert (await _item_row(item_id))["review_status"] == "archived"


async def test_agent_cannot_verify_item():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["other_agent"], content="a", review_status="active"
    )
    async with _make_client(s["tenant_1"], s["agent"]) as client:
        resp = await client.post(f"/v1/items/{item_id}/verify", json={})
    assert resp.status_code == 403, resp.text


# ===========================================================================
# User authority
# ===========================================================================


@pytest.mark.parametrize("initial", ["proposed", "disputed"])
async def test_user_can_activate(initial: str):
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["agent"], content="a", review_status=initial
    )
    async with _make_client(s["tenant_1"], s["user"]) as client:
        resp = await client.post(f"/v1/items/{item_id}/review", json={"review_status": "active"})
    assert resp.status_code == 200, resp.text
    assert (await _item_row(item_id))["review_status"] == "active"


@pytest.mark.parametrize("initial", ["proposed", "active", "disputed"])
async def test_user_can_reject(initial: str):
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["agent"], content="a", review_status=initial
    )
    async with _make_client(s["tenant_1"], s["user"]) as client:
        resp = await client.post(f"/v1/items/{item_id}/review", json={"review_status": "rejected"})
    assert resp.status_code == 200, resp.text
    assert (await _item_row(item_id))["review_status"] == "rejected"


@pytest.mark.parametrize("initial", ["proposed", "active", "disputed"])
async def test_user_can_archive_eligible_items(initial: str):
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["agent"], content="a", review_status=initial
    )
    async with _make_client(s["tenant_1"], s["user"]) as client:
        resp = await client.post(f"/v1/items/{item_id}/review", json={"review_status": "archived"})
    assert resp.status_code == 200, resp.text
    assert (await _item_row(item_id))["review_status"] == "archived"


async def test_user_can_reactivate_rejected_item():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["agent"], content="a", review_status="rejected"
    )
    async with _make_client(s["tenant_1"], s["user"]) as client:
        resp = await client.post(f"/v1/items/{item_id}/review", json={"review_status": "active"})
    assert resp.status_code == 200, resp.text
    assert (await _item_row(item_id))["review_status"] == "active"


async def test_user_cannot_restore_archived_item():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["agent"], content="a", review_status="archived"
    )
    async with _make_client(s["tenant_1"], s["user"]) as client:
        resp = await client.post(f"/v1/items/{item_id}/review", json={"review_status": "active"})
    assert resp.status_code == 403, resp.text
    assert (await _item_row(item_id))["review_status"] == "archived"


@pytest.mark.parametrize("initial", ["proposed", "active", "disputed"])
async def test_user_can_verify_eligible_items(initial: str):
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["agent"], content="a", review_status=initial
    )
    async with _make_client(s["tenant_1"], s["user"]) as client:
        resp = await client.post(f"/v1/items/{item_id}/verify", json={})
    assert resp.status_code == 200, resp.text
    row = await _item_row(item_id)
    assert row["human_verified"] is True
    assert str(row["verified_by"]) == s["user"]
    # Verification never changes review_status.
    assert row["review_status"] == initial


# ===========================================================================
# Administrator authority
# ===========================================================================


@pytest.mark.parametrize(
    ("initial", "target"),
    [
        ("proposed", "active"),
        ("disputed", "active"),
        ("rejected", "active"),
        ("proposed", "disputed"),
        ("active", "disputed"),
        ("proposed", "rejected"),
        ("active", "rejected"),
        ("disputed", "rejected"),
        ("proposed", "archived"),
        ("active", "archived"),
        ("disputed", "archived"),
    ],
)
async def test_admin_can_perform_all_governed_transitions(initial: str, target: str):
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["agent"], content="a", review_status=initial
    )
    async with _make_client(s["tenant_1"], s["admin"]) as client:
        resp = await client.post(f"/v1/items/{item_id}/review", json={"review_status": target})
    assert resp.status_code == 200, resp.text
    assert (await _item_row(item_id))["review_status"] == target


async def test_admin_can_restore_archived_item():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["agent"], content="a", review_status="archived"
    )
    async with _make_client(s["tenant_1"], s["admin"]) as client:
        resp = await client.post(f"/v1/items/{item_id}/review", json={"review_status": "active"})
    assert resp.status_code == 200, resp.text
    assert (await _item_row(item_id))["review_status"] == "active"


async def test_admin_can_verify_eligible_item():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["agent"], content="a", review_status="proposed"
    )
    async with _make_client(s["tenant_1"], s["admin"]) as client:
        resp = await client.post(f"/v1/items/{item_id}/verify", json={})
    assert resp.status_code == 200, resp.text
    row = await _item_row(item_id)
    assert row["human_verified"] is True
    assert str(row["verified_by"]) == s["admin"]


async def test_admin_delegated_review_does_not_change_actor():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["agent"], content="a", review_status="proposed"
    )
    async with _make_client(s["tenant_1"], s["admin"]) as client:
        resp = await client.post(
            f"/v1/items/{item_id}/review",
            json={"review_status": "active", "on_behalf_of_principal_id": s["user"]},
        )
    assert resp.status_code == 200, resp.text
    events = await _events_for(item_id)
    assert events
    assert all(e["actor_principal_id"] == s["admin"] for e in events)


async def test_admin_delegated_verify_does_not_change_verified_by():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["agent"], content="a", review_status="active"
    )
    async with _make_client(s["tenant_1"], s["admin"]) as client:
        resp = await client.post(
            f"/v1/items/{item_id}/verify",
            json={"on_behalf_of_principal_id": s["user"]},
        )
    assert resp.status_code == 200, resp.text
    row = await _item_row(item_id)
    # verified_by is the authenticated admin (the actor), never the delegate.
    assert str(row["verified_by"]) == s["admin"]
    assert str(row["verified_by"]) != s["user"]


# ===========================================================================
# Eligibility ordering: inaccessible items stay a non-disclosing 404
# ===========================================================================


async def test_nonexistent_item_returns_404():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    async with _make_client(s["tenant_1"], s["user"]) as client:
        resp = await client.post(
            f"/v1/items/{uuid4()}/review", json={"review_status": "active"}
        )
    assert resp.status_code == 404, resp.text


async def test_cross_tenant_item_returns_404_not_403_or_409():
    """A cross-tenant item id must 404 the same way a structurally-invalid or
    forbidden transition would look different (403/409) — proving the caller
    cannot distinguish "doesn't exist" from "exists but disallowed"."""
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_2"], principal_id=s["outsider"], content="cross-tenant", review_status="proposed"
    )
    async with _make_client(s["tenant_1"], s["user"]) as client:
        # Even a request that would be a 200 (user activating proposed) inside
        # the correct tenant must 404 across tenants.
        resp = await client.post(f"/v1/items/{item_id}/review", json={"review_status": "active"})
    assert resp.status_code == 404, resp.text
    events = await _events_for(item_id)
    assert events == []
    row = await _item_row(item_id)
    assert row["review_status"] == "proposed"


async def test_cross_tenant_verify_returns_404():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_2"], principal_id=s["outsider"], content="cross-tenant verify", review_status="active"
    )
    async with _make_client(s["tenant_1"], s["admin"]) as client:
        resp = await client.post(f"/v1/items/{item_id}/verify", json={})
    assert resp.status_code == 404, resp.text
    row = await _item_row(item_id)
    assert row["human_verified"] is False


async def test_inaccessible_item_no_mutation_or_event():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    async with _make_client(s["tenant_1"], s["admin"]) as client:
        resp = await client.post(
            f"/v1/items/{uuid4()}/review", json={"review_status": "rejected"}
        )
    assert resp.status_code == 404, resp.text
    # Nothing to assert on item_events for a nonexistent item id beyond the
    # absence of any row referencing it — this is implicit since the id was
    # never inserted; the meaningful proof is the 404 with no exception raised
    # by the transition-authorization path (which never ran).


# ===========================================================================
# No-op and delegation consistency (Problem 2)
# ===========================================================================


async def test_valid_noop_returns_success_with_null_event():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["agent"], content="a", review_status="active"
    )
    async with _make_client(s["tenant_1"], s["user"]) as client:
        resp = await client.post(f"/v1/items/{item_id}/review", json={"review_status": "active"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["event"] is None
    events = await _events_for(item_id)
    assert events == []


async def test_noop_writes_no_duplicate_event_and_no_field_mutation():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["agent"], content="a", review_status="active"
    )
    before = await _item_row(item_id)
    async with _make_client(s["tenant_1"], s["user"]) as client:
        resp = await client.post(
            f"/v1/items/{item_id}/review",
            json={"review_status": "active", "review_notes": "should not apply"},
        )
    assert resp.status_code == 200, resp.text
    after = await _item_row(item_id)
    assert await _events_for(item_id) == []
    # review_notes is only applied on an actual (non-noop) transition write.
    assert after["review_notes"] == before["review_notes"]


async def test_nonadmin_noop_with_on_behalf_of_returns_403():
    """A non-admin cannot ride a same-state no-op past delegation validation
    (Problem 2 / V2-BL-003A)."""
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["agent"], content="a", review_status="active"
    )
    async with _make_client(s["tenant_1"], s["user"]) as client:
        resp = await client.post(
            f"/v1/items/{item_id}/review",
            json={"review_status": "active", "on_behalf_of_principal_id": s["other_user"]},
        )
    assert resp.status_code == 403, resp.text
    assert await _events_for(item_id) == []
    assert (await _item_row(item_id))["review_status"] == "active"


async def test_noop_with_invalid_represented_principal_retains_404():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["agent"], content="a", review_status="active"
    )
    async with _make_client(s["tenant_1"], s["admin"]) as client:
        resp = await client.post(
            f"/v1/items/{item_id}/review",
            json={"review_status": "active", "on_behalf_of_principal_id": str(uuid4())},
        )
    assert resp.status_code == 404, resp.text
    assert await _events_for(item_id) == []


async def test_noop_with_cross_tenant_represented_principal_returns_404():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["agent"], content="a", review_status="active"
    )
    async with _make_client(s["tenant_1"], s["admin"]) as client:
        resp = await client.post(
            f"/v1/items/{item_id}/review",
            json={"review_status": "active", "on_behalf_of_principal_id": s["outsider"]},
        )
    assert resp.status_code == 404, resp.text
    assert await _events_for(item_id) == []


async def test_valid_delegated_admin_noop_writes_no_event():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["agent"], content="a", review_status="active"
    )
    async with _make_client(s["tenant_1"], s["admin"]) as client:
        resp = await client.post(
            f"/v1/items/{item_id}/review",
            json={"review_status": "active", "on_behalf_of_principal_id": s["user"]},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["event"] is None
    assert await _events_for(item_id) == []
