# ruff: noqa: E501
"""Executable app-role proof for conflict-resolution authorization and atomicity.

All requests use real Bearer credentials through ASGI.  The owner connection is
used only to arrange state, install failure triggers, and inspect committed state.
Concurrency uses a test-only trigger and PostgreSQL's blocker graph to prove
that independent Bearer requests overlap while holding the production pair locks.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
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
        "user_review": (uuid.uuid4(), "user", ["review"], None),
        "admin_review": (uuid.uuid4(), "admin", ["review"], None),
        "agent_review": (uuid.uuid4(), "agent", ["review"], None),
        "agent_admin": (uuid.uuid4(), "agent", ["review", "admin"], None),
        "system_review": (uuid.uuid4(), "system", ["review"], None),
        "system_admin": (uuid.uuid4(), "system", ["review", "admin"], None),
        "user_no_review": (uuid.uuid4(), "user", ["read"], None),
        "admin_no_review": (uuid.uuid4(), "admin", ["read"], None),
        "author": (uuid.uuid4(), "agent", [], None),
        "delegate": (uuid.uuid4(), "user", [], None),
        "automation": (uuid.uuid4(), "system", [], "review_automation"),
    }
    keys: dict[str, str] = {}
    async with owner.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id,name,slug) VALUES (:id,:n,:n)"),
            {"id": tenant, "n": f"conflict-proof-{tag}"},
        )
        await conn.execute(
            text(
                "INSERT INTO tenant_config (tenant_id,config_version,active) VALUES (:id,'proof',true)"
            ),
            {"id": tenant},
        )
        await conn.execute(
            text("INSERT INTO workspaces (id,tenant_id,name,slug) VALUES (:id,:tid,:n,:n)"),
            {"id": workspace, "tid": tenant, "n": f"restricted-{tag}"},
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
                        "label": f"conflict-proof-{tag}-{name}",
                    },
                )

    async def pair(
        *,
        target_visibility: str = "tenant",
        counterpart_visibility: str = "tenant",
        target_workspace: uuid.UUID | None = None,
        counterpart_workspace: uuid.UUID | None = None,
        reciprocal: bool = True,
    ) -> tuple[uuid.UUID, uuid.UUID]:
        target, counterpart = uuid.uuid4(), uuid.uuid4()
        async with owner.begin() as conn:
            for iid, label, visibility, wid in (
                (target, "target", target_visibility, target_workspace),
                (counterpart, "counterpart", counterpart_visibility, counterpart_workspace),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO memory_items (id,tenant_id,workspace_id,principal_id,content,content_hash,kind,visibility,review_status,memory_confidence,source_trust,importance,source_type) VALUES (:id,:tid,:wid,:pid,:content,:hash,'fact',:visibility,'active',.81,.73,.64,'manual')"
                    ),
                    {
                        "id": iid,
                        "tid": tenant,
                        "wid": wid,
                        "pid": actors["author"][0],
                        "content": f"{tag}:{label}:{iid}",
                        "hash": f"sha256:{iid.hex}",
                        "visibility": visibility,
                    },
                )
            await conn.execute(
                text(
                    "UPDATE memory_items SET conflicts_with_item_id=:cp, conflict_type='contradiction', conflict_resolution_status='unresolved' WHERE id=:id"
                ),
                {"id": target, "cp": counterpart},
            )
            if reciprocal:
                await conn.execute(
                    text(
                        "UPDATE memory_items SET conflicts_with_item_id=:cp, conflict_type='contradiction', conflict_resolution_status='unresolved' WHERE id=:id"
                    ),
                    {"id": counterpart, "cp": target},
                )
        return target, counterpart

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
                            "SELECT * FROM item_events WHERE item_id=:id AND event_type='conflict_resolution' ORDER BY created_at,id"
                        ),
                        {"id": item_id},
                    )
                )
                .mappings()
                .all()
            )
        return {"item": dict(row), "events": [dict(event) for event in events]}

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
        "tenant": tenant,
        "workspace": workspace,
        "actors": {k: v[0] for k, v in actors.items()},
        "keys": keys,
        "pair": pair,
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
            await conn.execute(text(f"DROP TRIGGER IF EXISTS conflict_pause_{tag} ON memory_items"))
            await conn.execute(text(f"DROP FUNCTION IF EXISTS conflict_pause_{tag}()"))
            await conn.execute(
                text("DROP TRIGGER IF EXISTS conflict_proof_fail_event ON item_events")
            )
            await conn.execute(text("DROP FUNCTION IF EXISTS conflict_proof_fail_event()"))
            await conn.execute(
                text("DROP TRIGGER IF EXISTS conflict_proof_fail_update ON memory_items")
            )
            await conn.execute(text("DROP FUNCTION IF EXISTS conflict_proof_fail_update()"))
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


async def _resolve(
    p: dict[str, Any], actor: str, item_id: uuid.UUID, resolution: str = "accepted", **body: Any
) -> Any:
    return await p["client"].post(
        f"/v1/items/{item_id}/resolve-conflict",
        json={"resolution": resolution, **body},
        headers=_headers(p, actor),
    )


async def _overlapping_resolutions(
    p: dict[str, Any], requests: list[tuple[str, uuid.UUID, str, str]]
) -> list[Any]:
    """Pause the lock winner and prove every competitor is in its lock wait chain."""
    trigger = f"conflict_pause_{p['tag']}"
    async with p["owner"].begin() as conn:
        await conn.execute(
            text(
                f"CREATE FUNCTION {trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN PERFORM pg_advisory_xact_lock({p['pause_key']}); RETURN NEW; END $$"
            )
        )
        await conn.execute(
            text(
                f"CREATE TRIGGER {trigger} BEFORE UPDATE OF conflict_resolution_status "
                f"ON memory_items FOR EACH ROW WHEN (OLD.tenant_id = '{p['tenant']}' "
                "AND OLD.conflict_resolution_status = 'unresolved') "
                f"EXECUTE FUNCTION {trigger}()"
            )
        )

    coordinator = await p["owner"].connect()
    tasks: list[asyncio.Task[Any]] = []
    try:
        await coordinator.execute(text("SELECT pg_advisory_lock(:key)"), {"key": p["pause_key"]})

        async def submit(actor: str, item_id: uuid.UUID, resolution: str, reason: str) -> Any:
            # A distinct client gives every request its own ASGI and DB-session path.
            async with AsyncClient(
                transport=ASGITransport(app=create_app()), base_url="http://test"
            ) as client:
                return await client.post(
                    f"/v1/items/{item_id}/resolve-conflict",
                    json={"resolution": resolution, "reason": reason},
                    headers=_headers(p, actor),
                )

        tasks = [asyncio.create_task(submit(*request)) for request in requests]
        blocker_sql = text(
            "SELECT count(*) AS waiters, "
            "count(*) FILTER (WHERE wait_event = 'advisory') AS advisory_waiters "
            "FROM pg_stat_activity WHERE state = 'active' AND wait_event_type = 'Lock'"
        )
        for _ in range(1000):
            await coordinator.execute(text("SELECT pg_stat_clear_snapshot()"))
            overlap = (await coordinator.execute(blocker_sql)).one()
            if overlap == (len(requests), 1):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError(f"requests did not reach expected lock waits: {overlap}")
        assert all(not task.done() for task in tasks)
        await coordinator.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": p["pause_key"]})
        return await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
    finally:
        if tasks:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if coordinator.in_transaction():
            await coordinator.rollback()
        await coordinator.execute(text("SELECT pg_advisory_unlock_all()"))
        await coordinator.close()
        async with p["owner"].begin() as conn:
            await conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger} ON memory_items"))
            await conn.execute(text(f"DROP FUNCTION IF EXISTS {trigger}()"))


def _changed_columns(before: dict[str, Any], after: dict[str, Any]) -> set[str]:
    return {key for key in before if before[key] != after[key]}


async def test_app_role_force_rls_and_principal_type_scope_matrix(proof: dict[str, Any]) -> None:
    async with proof["app"].connect() as conn:
        role = (
            await conn.execute(
                text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
            )
        ).one()
        forced = dict(
            (
                await conn.execute(
                    text(
                        "SELECT relname,relforcerowsecurity FROM pg_class WHERE relname IN ('memory_items','item_events')"
                    )
                )
            ).all()
        )
    assert role == (False, False)
    assert forced == {"memory_items": True, "item_events": True}
    expected = {
        "user_review": 200,
        "admin_review": 200,
        "agent_review": 403,
        "agent_admin": 403,
        "system_review": 403,
        "system_admin": 403,
        "user_no_review": 403,
        "admin_no_review": 403,
    }
    for actor, status in expected.items():
        target, counterpart = await proof["pair"]()
        before_target, before_counterpart = (
            await proof["state"](target),
            await proof["state"](counterpart),
        )
        response = await _resolve(proof, actor, target)
        assert response.status_code == status, (actor, response.text)
        after_target, after_counterpart = (
            await proof["state"](target),
            await proof["state"](counterpart),
        )
        if status == 200:
            assert response.json()["resolved_by"] == str(proof["actors"][actor])
            assert after_target["events"][0]["actor_principal_id"] == proof["actors"][actor]
        else:
            assert after_target == before_target
            assert after_counterpart == before_counterpart


@pytest.mark.parametrize("edge", ["private_target", "private_counterpart", "workspace_counterpart"])
async def test_eligibility_precedes_actor_class_and_denials_are_inert(
    proof: dict[str, Any], edge: str
) -> None:
    kwargs: dict[str, Any] = {}
    if edge == "private_target":
        kwargs["target_visibility"] = "private"
    elif edge == "private_counterpart":
        kwargs["counterpart_visibility"] = "private"
    else:
        kwargs.update(counterpart_visibility="workspace", counterpart_workspace=proof["workspace"])
    target, counterpart = await proof["pair"](**kwargs)
    before = (await proof["state"](target), await proof["state"](counterpart))
    denied = await _resolve(proof, "agent_review", target)
    assert denied.status_code == 404
    assert (await proof["state"](target), await proof["state"](counterpart)) == before
    eligible_target, eligible_counterpart = await proof["pair"]()
    before = (await proof["state"](eligible_target), await proof["state"](eligible_counterpart))
    denied = await _resolve(proof, "agent_review", eligible_target)
    assert denied.status_code == 403
    assert (
        await proof["state"](eligible_target),
        await proof["state"](eligible_counterpart),
    ) == before


@pytest.mark.parametrize(
    ("actor", "resolution"),
    [("user_review", "accepted"), ("admin_review", "rejected"), ("user_review", "merged")],
)
async def test_success_attribution_event_and_metadata_only(
    proof: dict[str, Any], actor: str, resolution: str
) -> None:
    target, counterpart = await proof["pair"]()
    before_target, before_counterpart = (
        await proof["state"](target),
        await proof["state"](counterpart),
    )
    response = await _resolve(proof, actor, target, resolution, reason=f"choose {resolution}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "resolved"
    assert body["counterpart_id"] == str(counterpart)
    assert body["conflict_resolution_status"] == resolution
    assert body["resolver_attribution_status"] == "recorded"
    assert body["resolved_by"] == str(proof["actors"][actor])
    after_target, after_counterpart = (
        await proof["state"](target),
        await proof["state"](counterpart),
    )
    assert after_target["item"]["conflict_resolved_by"] == proof["actors"][actor]
    assert after_target["item"]["conflict_resolved_at"] is not None
    assert len(after_target["events"]) == 1
    event = after_target["events"][0]
    assert event["actor_principal_id"] == proof["actors"][actor]
    assert body["event"]["id"] == str(event["id"])
    changed = {
        key
        for key in before_target["item"]
        if before_target["item"][key] != after_target["item"][key]
    }
    assert changed == {"conflict_resolution_status", "conflict_resolved_by", "conflict_resolved_at"}
    assert after_counterpart == before_counterpart
    if resolution == "merged":
        async with proof["owner"].connect() as conn:
            count = await conn.scalar(
                text("SELECT count(*) FROM memory_items WHERE tenant_id=:tid"),
                {"tid": proof["tenant"]},
            )
        assert count == 2


async def test_delegation_records_represented_principal_separately_and_internal_fails_closed(
    proof: dict[str, Any],
) -> None:
    target, _ = await proof["pair"]()
    response = await _resolve(
        proof, "admin_review", target, on_behalf_of_principal_id=str(proof["actors"]["delegate"])
    )
    assert response.status_code == 200
    state = await proof["state"](target)
    assert state["item"]["conflict_resolved_by"] == proof["actors"]["admin_review"]
    assert state["events"][0]["actor_principal_id"] == proof["actors"]["admin_review"]
    assert json.loads(state["events"][0]["reason"])["on_behalf_of_principal_id"] == str(
        proof["actors"]["delegate"]
    )
    failed_target, failed_counterpart = await proof["pair"]()
    before = (await proof["state"](failed_target), await proof["state"](failed_counterpart))
    denied = await _resolve(
        proof,
        "admin_review",
        failed_target,
        on_behalf_of_principal_id=str(proof["actors"]["automation"]),
    )
    assert denied.status_code == 404
    assert (await proof["state"](failed_target), await proof["state"](failed_counterpart)) == before


async def test_retries_preserve_original_attribution_reason_and_legacy_unknown(
    proof: dict[str, Any],
) -> None:
    target, _ = await proof["pair"]()
    first = await _resolve(
        proof,
        "admin_review",
        target,
        reason="original",
        on_behalf_of_principal_id=str(proof["actors"]["delegate"]),
    )
    assert first.status_code == 200
    original = await proof["state"](target)
    retry = await _resolve(proof, "user_review", target, reason="replacement")
    assert retry.status_code == 200
    assert retry.json()["status"] == "unchanged"
    assert await proof["state"](target) == original
    conflict = await _resolve(proof, "user_review", target, "rejected")
    assert conflict.status_code == 409
    assert await proof["state"](target) == original

    legacy, _ = await proof["pair"]()
    async with proof["owner"].begin() as conn:
        await conn.execute(
            text(
                "UPDATE memory_items SET conflict_resolution_status='merged', conflict_resolved_at=now(), conflict_resolved_by=NULL WHERE id=:id"
            ),
            {"id": legacy},
        )
    before = await proof["state"](legacy)
    response = await _resolve(proof, "user_review", legacy, "merged")
    assert response.status_code == 200
    assert response.json()["status"] == "unchanged"
    assert response.json()["resolved_by"] is None
    assert response.json()["resolver_attribution_status"] == "legacy_unknown"
    assert await proof["state"](legacy) == before


async def test_concurrent_same_resolution_is_canonical_and_idempotent(
    proof: dict[str, Any],
) -> None:
    target, _ = await proof["pair"]()
    responses = await _overlapping_resolutions(
        proof,
        [
            ("user_review", target, "accepted", "first contender"),
            ("admin_review", target, "accepted", "second contender"),
        ],
    )
    assert [response.status_code for response in responses] == [200, 200]
    bodies = [response.json() for response in responses]
    assert sorted(body["status"] for body in bodies) == ["resolved", "unchanged"]
    assert {body["conflict_resolution_status"] for body in bodies} == {"accepted"}
    winning = next(body for body in bodies if body["status"] == "resolved")
    unchanged = next(body for body in bodies if body["status"] == "unchanged")
    assert unchanged["resolved_by"] == winning["resolved_by"]
    assert unchanged["resolved_at"] == winning["resolved_at"]
    state = await proof["state"](target)
    assert state["item"]["conflict_resolved_by"] == uuid.UUID(winning["resolved_by"])
    assert state["item"]["conflict_resolved_at"] == datetime.fromisoformat(
        winning["resolved_at"].replace("Z", "+00:00")
    )
    assert len(state["events"]) == 1


async def test_concurrent_different_resolutions_preserve_winner(
    proof: dict[str, Any],
) -> None:
    target, _ = await proof["pair"]()
    responses = await _overlapping_resolutions(
        proof,
        [
            ("user_review", target, "accepted", "accept reason"),
            ("admin_review", target, "rejected", "reject reason"),
        ],
    )
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner_index = next(i for i, response in enumerate(responses) if response.status_code == 200)
    winner = responses[winner_index].json()
    winning_actor = ("user_review", "admin_review")[winner_index]
    winning_reason = ("accept reason", "reject reason")[winner_index]
    state = await proof["state"](target)
    assert state["item"]["conflict_resolution_status"] == winner["conflict_resolution_status"]
    assert state["item"]["conflict_resolved_by"] == proof["actors"][winning_actor]
    assert state["item"]["conflict_resolved_at"] == datetime.fromisoformat(
        winner["resolved_at"].replace("Z", "+00:00")
    )
    assert len(state["events"]) == 1
    event = state["events"][0]
    assert event["actor_principal_id"] == proof["actors"][winning_actor]
    assert event["new_value"] == winner["conflict_resolution_status"]
    assert event["reason"] == winning_reason


async def test_concurrent_three_way_resolution_has_one_side_effect(
    proof: dict[str, Any],
) -> None:
    target, counterpart = await proof["pair"]()
    before_target = await proof["state"](target)
    before_counterpart = await proof["state"](counterpart)
    requests = [
        ("user_review", target, "accepted", "accepted contender"),
        ("admin_review", target, "rejected", "rejected contender"),
        ("user_review", target, "merged", "merged contender"),
    ]
    responses = await _overlapping_resolutions(proof, requests)
    assert sorted(response.status_code for response in responses) == [200, 409, 409]
    winner_index = next(i for i, response in enumerate(responses) if response.status_code == 200)
    winner = responses[winner_index].json()
    after_target = await proof["state"](target)
    after_counterpart = await proof["state"](counterpart)
    assert (
        after_target["item"]["conflict_resolution_status"] == winner["conflict_resolution_status"]
    )
    assert (
        after_target["item"]["conflict_resolved_by"] == proof["actors"][requests[winner_index][0]]
    )
    assert len(after_target["events"]) == 1
    assert after_target["events"][0]["reason"] == requests[winner_index][3]
    assert _changed_columns(before_target["item"], after_target["item"]) == {
        "conflict_resolution_status",
        "conflict_resolved_by",
        "conflict_resolved_at",
    }
    assert after_counterpart == before_counterpart
    async with proof["owner"].connect() as conn:
        count = await conn.scalar(
            text("SELECT count(*) FROM memory_items WHERE tenant_id=:tid"),
            {"tid": proof["tenant"]},
        )
    assert count == 2


@pytest.mark.parametrize("iteration", range(3))
async def test_reciprocal_pair_canonical_lock_order_has_no_deadlock(
    proof: dict[str, Any], iteration: int
) -> None:
    first, second = await proof["pair"]()
    before_first = await proof["state"](first)
    before_second = await proof["state"](second)
    requests = [
        ("user_review", first, "accepted", f"first-{iteration}"),
        ("admin_review", second, "rejected", f"second-{iteration}"),
    ]
    if iteration % 2:
        requests.reverse()
    responses = await _overlapping_resolutions(proof, requests)
    assert [response.status_code for response in responses] == [200, 200]
    for request, response in zip(requests, responses, strict=True):
        actor, target, resolution, reason = request
        body = response.json()
        state = await proof["state"](target)
        assert body["status"] == "resolved"
        assert body["conflict_resolution_status"] == resolution
        assert state["item"]["conflict_resolution_status"] == resolution
        assert state["item"]["conflict_resolved_by"] == proof["actors"][actor]
        assert len(state["events"]) == 1
        assert state["events"][0]["reason"] == reason
    after_first = await proof["state"](first)
    after_second = await proof["state"](second)
    for before, after in (
        (before_first["item"], after_first["item"]),
        (before_second["item"], after_second["item"]),
    ):
        assert _changed_columns(before, after) == {
            "conflict_resolution_status",
            "conflict_resolved_by",
            "conflict_resolved_at",
        }


@pytest.mark.parametrize("failure", ["event", "update"])
async def test_postgres_failure_rolls_back_then_request_recovers(
    proof: dict[str, Any], failure: str
) -> None:
    target, _ = await proof["pair"]()
    async with proof["owner"].begin() as conn:
        if failure == "event":
            await conn.execute(
                text(
                    "CREATE FUNCTION conflict_proof_fail_event() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'forced conflict event failure'; END $$"
                )
            )
            await conn.execute(
                text(
                    f"CREATE TRIGGER conflict_proof_fail_event BEFORE INSERT ON item_events FOR EACH ROW WHEN (NEW.event_type='conflict_resolution' AND NEW.item_id='{target}') EXECUTE FUNCTION conflict_proof_fail_event()"
                )
            )
        else:
            await conn.execute(
                text(
                    "CREATE FUNCTION conflict_proof_fail_update() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'forced conflict update failure'; END $$"
                )
            )
            await conn.execute(
                text(
                    f"CREATE TRIGGER conflict_proof_fail_update BEFORE UPDATE ON memory_items FOR EACH ROW WHEN (OLD.id='{target}' AND OLD.conflict_resolution_status='unresolved') EXECUTE FUNCTION conflict_proof_fail_update()"
                )
            )
    with pytest.raises(Exception, match=f"forced conflict {failure} failure"):  # noqa: B017
        await _resolve(proof, "user_review", target)
    rolled_back = await proof["state"](target)
    assert rolled_back["item"]["conflict_resolution_status"] == "unresolved"
    assert rolled_back["item"]["conflict_resolved_at"] is None
    assert rolled_back["item"]["conflict_resolved_by"] is None
    assert rolled_back["events"] == []
    async with proof["owner"].begin() as conn:
        await conn.execute(
            text(
                f"DROP TRIGGER conflict_proof_fail_{failure} ON {'item_events' if failure == 'event' else 'memory_items'}"
            )
        )
        await conn.execute(text(f"DROP FUNCTION conflict_proof_fail_{failure}()"))
    recovered = await _resolve(proof, "user_review", target)
    assert recovered.status_code == 200
    state = await proof["state"](target)
    assert len(state["events"]) == 1
    assert state["item"]["conflict_resolved_by"] == proof["actors"]["user_review"]
    assert state["events"][0]["actor_principal_id"] == proof["actors"]["user_review"]
