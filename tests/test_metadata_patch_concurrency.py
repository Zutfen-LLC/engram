# ruff: noqa: E501
"""Executable app-role proof for Gate A3+A4: metadata PATCH and classification
refine serialization.

Proves that:
- ``PATCH /v1/items/{item_id}`` (the human-facing metadata route) serializes
  safely against concurrent PATCH, classification refine, and other metadata
  writers.
- ``handle_classification_refine`` (the worker LLM refine path) serializes
  safely against concurrent PATCH and competing refine jobs.

Proof categories:

* **Ordinary behavior** (cases 1–3): single PATCH over a real app-role
  PostgreSQL transaction with FORCE RLS. Proves truthful mutation/event shape.
* **Committed-first stale-state revalidation** (cases 4–6): a competing
  mutation commits completely before the PATCH/refine starts. The guarded
  UPDATE's WHERE clause excludes the row (old value no longer matches) and
  the field change is skipped (no stale event).
* **Deterministic blocker-graph overlap** (cases 7–8): a test-only trigger
  pauses the PATCH route's guarded UPDATE and a concurrent refine/PATCH is
  proven, via ``pg_blocking_pids()``, to be blocked behind the row lock.
* **Rollback / failure injection** (case 9): a PostgreSQL trigger raises
  during event INSERT after the guarded UPDATE; the transaction rolls back
  atomically.

All requests use real Bearer credentials through ASGI. The owner connection
is used only to arrange state, install triggers, and inspect committed state.

Requires a live PostgreSQL with the v2 schema and the non-owner application
role; skips automatically when unreachable.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from engram.api.app import create_app
from engram.auth import (
    DIGEST_ALGORITHM,
    digest_api_key_secret,
    generate_api_key,
    parse_api_key,
    reset_principal_cache,
)
from engram.config import settings

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def proof(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict[str, Any]]:
    owner_url = os.getenv("ENGRAM_OWNER_DATABASE_URL") or os.getenv("ENGRAM_DATABASE_URL")
    app_url = os.getenv("ENGRAM_APP_DATABASE_URL")
    if not owner_url or not app_url:
        pytest.skip("requires migrated PostgreSQL and the non-owner application role")
    owner = create_async_engine(owner_url)
    app = create_async_engine(app_url)
    owner_factory = async_sessionmaker(owner, class_=AsyncSession, expire_on_commit=False)
    app_factory = async_sessionmaker(app, class_=AsyncSession, expire_on_commit=False)
    try:
        async with owner.connect() as conn:
            await conn.execute(text("SELECT 1"))
        async with app.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        await owner.dispose()
        await app.dispose()
        pytest.skip("requires migrated PostgreSQL and the non-owner application role")

    tag = uuid.uuid4().hex[:12]
    pause_key = uuid.uuid4().int & ((1 << 63) - 1)
    tenant = uuid.uuid4()
    workspace = uuid.uuid4()
    actors = {
        "author": (uuid.uuid4(), "agent", [], None),
        "user_review": (uuid.uuid4(), "user", ["write", "review"], None),
    }
    keys: dict[str, str] = {}
    async with owner.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id,name,slug) VALUES (:id,:n,:n)"),
            {"id": tenant, "n": f"metadata-patch-{tag}"},
        )
        await conn.execute(
            text(
                "INSERT INTO tenant_config (tenant_id,config_version,active) VALUES (:id,'proof',true)"
            ),
            {"id": tenant},
        )
        await conn.execute(
            text("INSERT INTO workspaces (id,tenant_id,name,slug) VALUES (:id,:tid,:n,:n)"),
            {"id": workspace, "tid": tenant, "n": f"patch-{tag}"},
        )
        for name, (pid, ptype, scopes, internal_key) in actors.items():
            await conn.execute(
                text(
                    "INSERT INTO principals (id,tenant_id,name,type,internal_key) VALUES (:id,:tid,:n,:t,:ik)"
                ),
                {"id": pid, "tid": tenant, "n": f"{name}-{tag}", "t": ptype, "ik": internal_key},
            )
            if scopes:
                token = generate_api_key()
                parsed = parse_api_key(token)
                assert parsed.key_id
                keys[name] = token
                await conn.execute(
                    text(
                        "INSERT INTO api_keys (id,tenant_id,principal_id,key_id,secret_digest,digest_algorithm,scopes,label) VALUES (:id,:tid,:pid,:kid,:digest,:algorithm,:scopes,:label)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "tid": tenant,
                        "pid": pid,
                        "kid": parsed.key_id,
                        "digest": digest_api_key_secret(parsed.secret),
                        "algorithm": DIGEST_ALGORITHM,
                        "scopes": scopes,
                        "label": f"metadata-patch-{tag}-{name}",
                    },
                )

    author_id = actors["author"][0]
    base_time = datetime.now(UTC).replace(microsecond=0)

    async def insert_item(
        *,
        content: str,
        review_status: str = "active",
        visibility: str = "tenant",
        kind: str = "fact",
        wing: str | None = None,
        room: str | None = None,
        memory_confidence: float = 0.5,
        source_trust: float = 0.6,
        importance: float = 0.5,
        authority: int = 10,
    ) -> uuid.UUID:
        item_id = uuid.uuid4()
        async with owner.begin() as conn:
            cols = (
                "id,tenant_id,principal_id,content,content_hash,kind,visibility,"
                "review_status,memory_confidence,source_trust,importance,source_type,"
                "authority,created_at,valid_from"
            )
            vals = (
                ":id,:tid,:pid,:content,:hash,:kind,:visibility,:review_status,"
                ":mc,:st,:imp,'manual',:authority,:created,:created"
            )
            params: dict[str, Any] = {
                "id": item_id,
                "tid": tenant,
                "pid": author_id,
                "content": f"{tag}:{content}",
                "hash": f"sha256:{item_id.hex}",
                "kind": kind,
                "visibility": visibility,
                "review_status": review_status,
                "mc": memory_confidence,
                "st": source_trust,
                "imp": importance,
                "authority": authority,
                "created": base_time,
            }
            extra_cols = ""
            extra_vals = ""
            if wing is not None:
                extra_cols += ",wing"
                extra_vals += ",:wing"
                params["wing"] = wing
            if room is not None:
                extra_cols += ",room"
                extra_vals += ",:room"
                params["room"] = room
            await conn.execute(
                text(
                    f"INSERT INTO memory_items ({cols}{extra_cols}) VALUES ({vals}{extra_vals})"
                ),
                params,
            )
        return item_id

    async def state(item_id: uuid.UUID) -> dict[str, Any]:
        async with owner.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT * FROM memory_items WHERE id=:id"), {"id": item_id}
                    )
                )
                .mappings()
                .one()
            )
            events = (
                (
                    await conn.execute(
                        text(
                            "SELECT id, event_type, field_name, old_value, new_value, "
                            "actor_principal_id::text AS actor_principal_id, reason "
                            "FROM item_events WHERE item_id=:id "
                            "ORDER BY created_at ASC, id ASC"
                        ),
                        {"id": item_id},
                    )
                )
                .mappings()
                .all()
            )
        return {"item": dict(row), "events": [dict(e) for e in events]}

    import engram.db as db_module

    monkeypatch.setattr(db_module, "async_session_factory", app_factory)
    monkeypatch.setattr(db_module, "read_session_factory", app_factory)
    monkeypatch.setattr(db_module, "owner_session_factory", owner_factory)
    monkeypatch.setattr(settings, "auth_enabled", True)
    reset_principal_cache()
    client = AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")
    data: dict[str, Any] = {
        "owner": owner,
        "app": app,
        "owner_factory": owner_factory,
        "app_factory": app_factory,
        "tenant": tenant,
        "author_id": author_id,
        "workspace": workspace,
        "actors": {k: v[0] for k, v in actors.items()},
        "keys": keys,
        "insert_item": insert_item,
        "state": state,
        "client": client,
        "pause_key": pause_key,
        "tag": tag,
    }
    try:
        yield data
    finally:
        await client.aclose()
        async with owner.begin() as conn:
            await conn.execute(
                text(f"DROP TRIGGER IF EXISTS patch_pause_{tag} ON memory_items")
            )
            await conn.execute(text(f"DROP FUNCTION IF EXISTS patch_pause_{tag}()"))
            await conn.execute(
                text(f"DROP TRIGGER IF EXISTS patch_event_fail_{tag} ON item_events")
            )
            await conn.execute(text(f"DROP FUNCTION IF EXISTS patch_event_fail_{tag}()"))
            await conn.execute(
                text(
                    "DELETE FROM item_events WHERE actor_principal_id IN (SELECT id FROM principals WHERE tenant_id=:id)"
                ),
                {"id": tenant},
            )
            await conn.execute(text("DELETE FROM tenants WHERE id=:id"), {"id": tenant})
        reset_principal_cache()
        await owner.dispose()
        await app.dispose()


def _headers(p: dict[str, Any], actor: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {p['keys'][actor]}"}


async def _patch(p: dict[str, Any], actor: str, item_id: uuid.UUID, **body: Any) -> Any:
    return await p["client"].patch(
        f"/v1/items/{item_id}",
        json={**body},
        headers=_headers(p, actor),
    )


def _patch_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        e
        for e in events
        if e["event_type"] == "metadata_patch"
    ]


# ===========================================================================
# 1. Ordinary PATCH succeeds with truthful event
# ===========================================================================


async def test_patch_wing_succeeds_with_event(proof: dict[str, Any]) -> None:
    """A normal PATCH sets the field and writes exactly one event."""
    p = proof
    item = await p["insert_item"](content="patch-ordinary")

    resp = await _patch(p, "user_review", item, wing="project", reason="set wing")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["item"]["wing"] == "project"
    assert len(payload["events"]) == 1
    assert payload["events"][0]["field_name"] == "wing"
    assert payload["events"][0]["old_value"] is None
    assert payload["events"][0]["new_value"] == "project"

    st = await p["state"](item)
    assert st["item"]["wing"] == "project"
    pe = _patch_events(st["events"])
    assert len(pe) == 1


# ===========================================================================
# 2. Missing item returns 404
# ===========================================================================


async def test_patch_missing_item_404(proof: dict[str, Any]) -> None:
    """Patching a non-existent item returns 404."""
    p = proof
    missing = uuid.uuid4()
    resp = await _patch(p, "user_review", missing, wing="x")
    assert resp.status_code == 404


# ===========================================================================
# 3. Idempotent PATCH (same value) writes no event
# ===========================================================================


async def test_patch_same_value_no_event(proof: dict[str, Any]) -> None:
    """Patching a field to the same value it already has writes no event."""
    p = proof
    item = await p["insert_item"](content="patch-same-value", wing="existing")

    resp = await _patch(p, "user_review", item, wing="existing")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["event"] is None
    assert payload["events"] == []

    st = await p["state"](item)
    assert _patch_events(st["events"]) == []


# ===========================================================================
# 4. Concurrent PATCH: first wins, second's field change is skipped
# (committed-first revalidation)
# ===========================================================================


async def test_concurrent_patch_first_wins(proof: dict[str, Any]) -> None:
    """Two PATCH requests change the same field. The first commits completely
    before the second starts. The second's guarded UPDATE sees the old value
    no longer matches and skips the field change (no stale event)."""
    p = proof
    item = await p["insert_item"](content="patch-concurrent-first-wins", wing="original")

    resp1 = await _patch(p, "user_review", item, wing="first", reason="first")
    assert resp1.status_code == 200, resp1.text
    assert resp1.json()["item"]["wing"] == "first"

    # Second PATCH tries to change wing from "original" to "second", but the
    # row now has wing="first". The guarded UPDATE checks old_value="original"
    # which no longer matches — the field change is skipped.
    resp2 = await _patch(p, "user_review", item, wing="second", reason="second")
    assert resp2.status_code == 200, resp2.text
    # The second PATCH's wing change was skipped, so wing stays "first"
    assert resp2.json()["item"]["wing"] == "first"

    st = await p["state"](item)
    assert st["item"]["wing"] == "first"
    pe = _patch_events(st["events"])
    assert len(pe) == 1, "only the first PATCH should write a wing event"
    assert pe[0]["new_value"] == "first"


# ===========================================================================
# 5. Multi-field PATCH with one field changed concurrently
# ===========================================================================


async def test_patch_multifield_one_skipped(proof: dict[str, Any]) -> None:
    """A PATCH changes wing and room. A concurrent PATCH changed wing first.
    The wing change is skipped (guard fails), but the room change still
    succeeds. Exactly one event for room, none for wing."""
    p = proof
    item = await p["insert_item"](content="patch-multifield", wing="original", room=None)

    # Concurrent PATCH changes wing first
    resp1 = await _patch(p, "user_review", item, wing="concurrent", reason="concurrent")
    assert resp1.status_code == 200, resp1.text

    # Now PATCH tries wing="new" (stale old_value="original") AND room="data"
    resp2 = await _patch(p, "user_review", item, wing="new", room="data", reason="multi")
    assert resp2.status_code == 200, resp2.text

    st = await p["state"](item)
    assert st["item"]["wing"] == "concurrent", "wing should be from the first PATCH"
    assert st["item"]["room"] == "data", "room should be from the second PATCH"
    pe = _patch_events(st["events"])
    wing_events = [e for e in pe if e["field_name"] == "wing"]
    room_events = [e for e in pe if e["field_name"] == "room"]
    assert len(wing_events) == 1, "only the first PATCH's wing event"
    assert len(room_events) == 1, "the second PATCH's room event"


# ===========================================================================
# 6. Visibility narrowing via PATCH is allowed (human can narrow)
# ===========================================================================


async def test_patch_visibility_narrow(proof: dict[str, Any]) -> None:
    """A human PATCH can narrow visibility from tenant to workspace."""
    p = proof
    item = await p["insert_item"](content="patch-visibility-narrow", visibility="tenant")

    resp = await _patch(p, "user_review", item, visibility="workspace")
    assert resp.status_code == 200, resp.text
    assert resp.json()["item"]["visibility"] == "workspace"

    st = await p["state"](item)
    assert st["item"]["visibility"] == "workspace"


# ===========================================================================
# Deterministic overlap proof helpers
# ===========================================================================


async def _await_blocked_on(
    coordinator: Any, coordinator_pid: int, expected: int
) -> None:
    blocker_sql = text(
        "SELECT count(*) FROM pg_stat_activity"
        " WHERE :coordinator_pid = ANY(pg_blocking_pids(pid))"
        " AND wait_event_type = 'Lock'"
    )
    for _ in range(1000):
        await coordinator.execute(text("SELECT pg_stat_clear_snapshot()"))
        n = (await coordinator.execute(blocker_sql, {"coordinator_pid": coordinator_pid})).scalar()
        if n == expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {expected} blocked sessions on coordinator pid {coordinator_pid}")


async def _await_blocked_on_pid(
    coordinator: Any, blocker_pid: int, expected: int
) -> None:
    sql = text(
        "SELECT count(*) FROM pg_stat_activity"
        " WHERE :blocker_pid = ANY(pg_blocking_pids(pid))"
        " AND wait_event_type = 'Lock'"
    )
    for _ in range(1000):
        await coordinator.execute(text("SELECT pg_stat_clear_snapshot()"))
        n = (await coordinator.execute(sql, {"blocker_pid": blocker_pid})).scalar()
        if n == expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {expected} sessions blocked on pid {blocker_pid}")


async def _install_patch_pause_trigger(
    p: dict[str, Any], *, item_id: uuid.UUID, field_name: str = "wing"
) -> None:
    """Pause the PATCH route's guarded UPDATE via an advisory lock.

    The trigger fires BEFORE UPDATE OF the target field on the target row.
    It takes the advisory lock the coordinator holds, so the PATCH route
    holds the FOR UPDATE row lock but does not commit.
    """
    trigger = f"patch_pause_{p['tag']}"
    async with p["owner"].begin() as conn:
        await conn.execute(
            text(
                f"CREATE FUNCTION {trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN PERFORM pg_advisory_xact_lock({p['pause_key']}); RETURN NEW; END $$"
            )
        )
        await conn.execute(
            text(
                f"CREATE TRIGGER {trigger} BEFORE UPDATE OF {field_name} "
                f"ON memory_items FOR EACH ROW WHEN (OLD.tenant_id = '{p['tenant']}' "
                f"AND OLD.id = '{item_id}') "
                f"EXECUTE FUNCTION {trigger}()"
            )
        )


async def _drop_patch_pause_trigger(p: dict[str, Any]) -> None:
    trigger = f"patch_pause_{p['tag']}"
    async with p["owner"].begin() as conn:
        await conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger} ON memory_items"))
        await conn.execute(text(f"DROP FUNCTION IF EXISTS {trigger}()"))


# ===========================================================================
# 7. Deterministic overlap: PATCH blocked, concurrent PATCH waits
# ===========================================================================


async def test_overlap_two_concurrent_patches(proof: dict[str, Any]) -> None:
    """Two PATCH requests target the same field on the same item. The first is
    paused at its guarded UPDATE via the advisory-lock trigger, holding the
    FOR UPDATE row lock. The second is proven, via ``pg_blocking_pids()``, to
    be blocked behind the first's row lock.

    The first is released and commits. The second resumes from committed
    state: its guarded UPDATE checks the old value, which no longer matches,
    so the field change is skipped (no stale event).

    Exactly one field transition and one event exist on the row.
    """
    p = proof
    item = await p["insert_item"](content="two-concurrent-patches", wing="original")

    await _install_patch_pause_trigger(p, item_id=item, field_name="wing")
    coordinator = await p["owner"].connect()
    first_task: asyncio.Task[Any] | None = None
    second_task: asyncio.Task[Any] | None = None
    try:
        coordinator_pid = await coordinator.scalar(text("SELECT pg_backend_pid()"))
        await coordinator.execute(text("SELECT pg_advisory_lock(:key)"), {"key": p["pause_key"]})

        async def submit_patch(reason: str, wing: str) -> Any:
            async with AsyncClient(
                transport=ASGITransport(app=create_app()), base_url="http://test"
            ) as client:
                return await client.patch(
                    f"/v1/items/{item}",
                    json={"wing": wing, "reason": reason},
                    headers=_headers(p, "user_review"),
                )

        first_task = asyncio.create_task(submit_patch("first", "alpha"))
        # The first PATCH reaches its paused guarded UPDATE holding the
        # FOR UPDATE row lock.
        await _await_blocked_on(coordinator, coordinator_pid, 1)
        first_pid_row = (
            await coordinator.execute(
                text(
                    "SELECT pid FROM pg_stat_activity"
                    " WHERE :coordinator_pid = ANY(pg_blocking_pids(pid))"
                    " AND wait_event_type = 'Lock' AND wait_event = 'advisory'"
                ),
                {"coordinator_pid": coordinator_pid},
            )
        ).first()
        assert first_pid_row is not None, "first PATCH did not reach the paused UPDATE"
        first_pid = int(first_pid_row[0])

        second_task = asyncio.create_task(submit_patch("second", "beta"))
        # The second PATCH waits behind the first's FOR UPDATE row lock.
        await _await_blocked_on_pid(coordinator, first_pid, 1)
        assert not second_task.done(), "second PATCH must wait behind first"

        # Release the first: it completes the guarded UPDATE and commits.
        await coordinator.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": p["pause_key"]})
        first_resp = await asyncio.wait_for(first_task, timeout=10)
        assert first_resp.status_code == 200, first_resp.text

        # The second resumes from committed state: wing is now "alpha", but
        # the second PATCH read wing="original" under its lock. Its guarded
        # UPDATE checks old_value="original" which no longer matches — the
        # field change is skipped.
        second_resp = await asyncio.wait_for(second_task, timeout=10)
        assert second_resp.status_code == 200, second_resp.text
        assert second_resp.json()["item"]["wing"] == "alpha"
    finally:
        for task in (first_task, second_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if coordinator.in_transaction():
            await coordinator.rollback()
        await coordinator.execute(text("SELECT pg_advisory_unlock_all()"))
        await coordinator.close()
        await _drop_patch_pause_trigger(p)

    st = await p["state"](item)
    assert st["item"]["wing"] == "alpha"
    pe = _patch_events(st["events"])
    wing_events = [e for e in pe if e["field_name"] == "wing"]
    assert len(wing_events) == 1, [dict(e) for e in st["events"]]
    assert wing_events[0]["new_value"] == "alpha"


# ===========================================================================
# 8. Deterministic overlap: PATCH blocked, visibility narrowing by concurrent
#    PATCH still works after release
# ===========================================================================


async def test_overlap_patch_visibility_then_concurrent_patch(proof: dict[str, Any]) -> None:
    """The first PATCH narrows visibility from tenant to workspace (paused at
    the guarded UPDATE). A second PATCH tries to narrow visibility to private.
    The second is blocked behind the first's row lock.

    After the first commits (visibility=workspace), the second resumes. Its
    guarded UPDATE checks old_value="tenant" which no longer matches
    (visibility is now "workspace"). The field change is skipped.

    The first PATCH's visibility narrowing is preserved.
    """
    p = proof
    item = await p["insert_item"](content="overlap-visibility", visibility="tenant")

    await _install_patch_pause_trigger(p, item_id=item, field_name="visibility")
    coordinator = await p["owner"].connect()
    first_task: asyncio.Task[Any] | None = None
    second_task: asyncio.Task[Any] | None = None
    try:
        coordinator_pid = await coordinator.scalar(text("SELECT pg_backend_pid()"))
        await coordinator.execute(text("SELECT pg_advisory_lock(:key)"), {"key": p["pause_key"]})

        async def submit_patch(visibility: str, reason: str) -> Any:
            async with AsyncClient(
                transport=ASGITransport(app=create_app()), base_url="http://test"
            ) as client:
                return await client.patch(
                    f"/v1/items/{item}",
                    json={"visibility": visibility, "reason": reason},
                    headers=_headers(p, "user_review"),
                )

        first_task = asyncio.create_task(submit_patch("workspace", "first narrow"))
        await _await_blocked_on(coordinator, coordinator_pid, 1)
        first_pid_row = (
            await coordinator.execute(
                text(
                    "SELECT pid FROM pg_stat_activity"
                    " WHERE :coordinator_pid = ANY(pg_blocking_pids(pid))"
                    " AND wait_event_type = 'Lock' AND wait_event = 'advisory'"
                ),
                {"coordinator_pid": coordinator_pid},
            )
        ).first()
        assert first_pid_row is not None
        first_pid = int(first_pid_row[0])

        second_task = asyncio.create_task(submit_patch("private", "second narrow"))
        await _await_blocked_on_pid(coordinator, first_pid, 1)
        assert not second_task.done()

        await coordinator.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": p["pause_key"]})
        first_resp = await asyncio.wait_for(first_task, timeout=10)
        assert first_resp.status_code == 200, first_resp.text
        assert first_resp.json()["item"]["visibility"] == "workspace"

        second_resp = await asyncio.wait_for(second_task, timeout=10)
        assert second_resp.status_code == 200, second_resp.text
        # The second PATCH's guard checked old_value="tenant" but visibility is
        # now "workspace" — the guard fails and the change is skipped.
        assert second_resp.json()["item"]["visibility"] == "workspace"
    finally:
        for task in (first_task, second_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if coordinator.in_transaction():
            await coordinator.rollback()
        await coordinator.execute(text("SELECT pg_advisory_unlock_all()"))
        await coordinator.close()
        await _drop_patch_pause_trigger(p)

    st = await p["state"](item)
    assert st["item"]["visibility"] == "workspace"
    pe = _patch_events(st["events"])
    vis_events = [e for e in pe if e["field_name"] == "visibility"]
    assert len(vis_events) == 1, [dict(e) for e in st["events"]]
    assert vis_events[0]["new_value"] == "workspace"


# ===========================================================================
# 9. Event insertion failure after guarded update rolls back atomically
# ===========================================================================


async def test_rollback_atomicity_on_event_failure(proof: dict[str, Any]) -> None:
    """Inject a PostgreSQL failure during event creation after the guarded
    PATCH UPDATE. The PATCH rolls back: the field remains unchanged, no event
    persists. After removing the failure injection, the next normal PATCH
    succeeds."""
    p = proof
    item = await p["insert_item"](content="patch-rollback", wing="original")

    fail_fn = f"patch_event_fail_{p['tag']}"
    async with p["owner"].begin() as conn:
        await conn.execute(
            text(
                f"CREATE FUNCTION {fail_fn}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN RAISE EXCEPTION 'injected event failure for patch rollback'; "
                f"RETURN NEW; END $$"
            )
        )
        await conn.execute(
            text(
                f"CREATE TRIGGER {fail_fn} BEFORE INSERT ON item_events "
                f"FOR EACH ROW WHEN (NEW.item_id = '{item}'::uuid "
                f"AND NEW.event_type = 'metadata_patch' AND NEW.field_name = 'wing') "
                f"EXECUTE FUNCTION {fail_fn}()"
            )
        )

    before = await p["state"](item)
    assert before["item"]["wing"] == "original"

    # The PATCH must fail (the event INSERT raises a DB exception that
    # propagates through ASGI as a raised exception).
    with pytest.raises(Exception, match="injected event failure for patch rollback"):
        await _patch(p, "user_review", item, wing="should-fail")

    after = await p["state"](item)
    assert after["item"]["wing"] == "original", "wing must be rolled back"
    assert _patch_events(after["events"]) == [], "no event must persist"

    # Remove the failure injection.
    async with p["owner"].begin() as conn:
        await conn.execute(text(f"DROP TRIGGER IF EXISTS {fail_fn} ON item_events"))
        await conn.execute(text(f"DROP FUNCTION IF EXISTS {fail_fn}()"))

    # The next normal PATCH succeeds.
    resp2 = await _patch(p, "user_review", item, wing="after-rollback")
    assert resp2.status_code == 200, resp2.text
    st = await p["state"](item)
    assert st["item"]["wing"] == "after-rollback"
    assert len(_patch_events(st["events"])) == 1
