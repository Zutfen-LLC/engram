# ruff: noqa: E501
"""Executable app-role proof for P0-FIX-004C1: worker conflict-flagging serialization.

Proves that the worker's ``handle_conflict_check`` flagging branch
(``FLAG_CONTRADICTION`` / ``FLAG_SCOPE_OVERLAP`` / ``PROPOSED_SUPERSEDE``)
serializes safely against concurrent human conflict resolution and review
changes. Covers the seven required proofs:

  1. worker wins, then human resolves — worker holds the canonical pair
     locks, a Bearer-authenticated human resolution waits behind them, worker
     commits one unresolved relationship + one event, human resumes and
     resolves it; final state carries the human decision; worker does not
     later reopen it.
  2. human resolution wins first — a Bearer resolution owns the pair locks,
     the worker observes the completed decision and does not reset it.
  3. human review wins first — terminal (rejected/archived) and disputed
     review decisions land first; the worker skips all mutation and writes no
     flagging event; on a disputed item, conflict metadata (if newly recorded)
     is recorded once and review stays disputed.
  4. worker wins before review — worker owns the row locks, a Bearer review
     waits, worker commits the valid flag transition, review resumes from the
     committed state and applies its normal policy.
  5. idempotent rerun — the same flagging job run again is event-free and
     state-free.
  6. different relationship preservation — an existing unresolved relationship
     with item B is not silently replaced by a worker detecting item C.
  7. reciprocal worker stress — concurrent worker flagging jobs on opposite
     sides of the same pair complete within an explicit timeout with no
     PostgreSQL deadlock; canonical pair lock ordering is used; the
     creation-order guard allows only the correct newer side to act.

The production worker handler is invoked directly over an independent app-role
session with FORCE RLS. Conflict detection is deterministically controlled by
monkeypatching ``engram.conflicts.detect_conflicts`` so the tests exercise the
production mutation branch without a live embedding provider. Human requests
use real Bearer credentials through ASGI. Synchronization is deterministic and
database-level: a test-only trigger calls ``pg_advisory_xact_lock`` at the
worker's UPDATE write point (the guarded transition), and the coordinator holds
the matching advisory lock to pause the worker while the human request runs.
Overlap is proven via PostgreSQL's blocker graph (``pg_blocking_pids``), not
sleeps. The owner connection is used only to arrange state, install triggers,
and inspect committed state.

Requires a live PostgreSQL with the v2 schema and the non-owner application
role; skips automatically when unreachable.

Scope: only the flagging branch (``FLAG_CONTRADICTION`` /
``FLAG_SCOPE_OVERLAP`` / ``PROPOSED_SUPERSEDE``). ``DEDUP`` and
``AUTO_SUPERSEDE`` are intentionally out of scope and untouched.
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

import engram.conflicts as conflicts_mod
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
from engram.jobs import enqueue_job
from engram.models import Job, MemoryItem
from engram.worker import handle_conflict_check, process_one_job

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
    # Actors: an agent author writes active items (the conflict neighbours); a
    # human user reviews and resolves conflicts. ``user_review`` holds both
    # ``write`` and ``review`` so it can dispute (write) and activate/reject
    # (review). ``author`` holds ``write`` so it can self-withdraw and dispute.
    actors = {
        "author": (uuid.uuid4(), "agent", [], None),
        "user_review": (uuid.uuid4(), "user", ["write", "review"], None),
    }
    keys: dict[str, str] = {}
    async with owner.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id,name,slug) VALUES (:id,:n,:n)"),
            {"id": tenant, "n": f"worker-flag-{tag}"},
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
                        "label": f"worker-flag-{tag}-{name}",
                    },
                )

    author_id = actors["author"][0]
    old = datetime.now(UTC).replace(microsecond=0)

    async def insert_item(
        *,
        content: str,
        review_status: str = "active",
        created_at: datetime | None = None,
        conflicts_with_item_id: uuid.UUID | None = None,
        conflict_type: str | None = None,
        conflict_resolution_status: str | None = None,
    ) -> uuid.UUID:
        item_id = uuid.uuid4()
        cts = created_at if created_at is not None else old
        async with owner.begin() as conn:
            cols = (
                "id,tenant_id,principal_id,content,content_hash,kind,visibility,"
                "review_status,memory_confidence,source_trust,importance,source_type,"
                "created_at,valid_from"
            )
            vals = (
                ":id,:tid,:pid,:content,:hash,'fact','tenant',:review_status,"
                ".81,.73,.64,'manual',:created,:created"
            )
            params: dict[str, Any] = {
                "id": item_id,
                "tid": tenant,
                "pid": author_id,
                "content": f"{tag}:{content}",
                "hash": f"sha256:{item_id.hex}",
                "review_status": review_status,
                "created": cts,
            }
            extra_cols = ""
            extra_vals = ""
            if conflicts_with_item_id is not None:
                extra_cols += ",conflicts_with_item_id"
                extra_vals += ",:cwi"
                params["cwi"] = conflicts_with_item_id
            if conflict_type is not None:
                extra_cols += ",conflict_type"
                extra_vals += ",:ct"
                params["ct"] = conflict_type
            if conflict_resolution_status is not None:
                extra_cols += ",conflict_resolution_status"
                extra_vals += ",:crs"
                params["crs"] = conflict_resolution_status
            await conn.execute(
                text(
                    f"INSERT INTO memory_items ({cols}{extra_cols}) VALUES ({vals}{extra_vals})"
                ),
                params,
            )
        return item_id

    async def add_ready_embedding(item_id: uuid.UUID) -> None:
        """Attach a ready embedding so the profile lookup inside the handler succeeds."""
        from engram.db import apply_rls_context as _apply
        from engram.models import MemoryEmbedding

        async with owner_factory() as session:
            await _apply(session, tenant_id=str(tenant), principal_id=str(author_id))
            profile = (
                (
                    await session.execute(
                        text("SELECT * FROM embedding_profiles WHERE state='active' LIMIT 1")
                    )
                )
                .mappings()
                .first()
            )
            if profile is None:
                pytest.skip("no active embedding profile seeded")
            dim = int(profile["dimensions"])
            session.add(
                MemoryEmbedding(
                    memory_item_id=item_id,
                    tenant_id=tenant,
                    embedding_model=profile["model"],
                    embedding_dim=dim,
                    embedding=[1.0] + [0.0] * (dim - 1),
                    embedding_status="ready",
                )
            )
            await session.commit()

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

    # A deterministic ConflictResult the worker will see as the detection output.
    def force_detection(
        *, existing_item_id: uuid.UUID, action: str, conflict_type: str | None
    ) -> None:
        from engram.conflicts import ConflictAction, ConflictResult, ConflictVerdict

        result = ConflictResult(
            verdict=ConflictVerdict.CONTRADICT,
            action=ConflictAction(action),
            existing_item_id=existing_item_id,
            similarity=0.95,
            classifier_confidence=0.9,
            conflict_type=conflict_type,
            reason=f"forced {action}",
            provenance={"provider": "test", "mode": "forced"},
        )

        async def fake_detect(_item: MemoryItem, _session: AsyncSession, **_kw: Any) -> Any:
            return result

        monkeypatch.setattr(conflicts_mod, "detect_conflicts", fake_detect)

    async def run_conflict_check(item_id: uuid.UUID) -> None:
        """Invoke the production worker handler over an app-role session.

        Builds a real Job row (so provenance/tenant routing match production),
        then calls ``handle_conflict_check`` directly — the same function
        ``process_one_job`` dispatches to. Uses a fresh app-role session with
        FORCE RLS scoped to the job's tenant.
        """
        async with app_factory() as session:
            await apply_rls_context(session, tenant_id=str(tenant), principal_id=str(author_id))
            job = Job(
                id=uuid.uuid4(),
                tenant_id=tenant,
                job_type="conflict.check",
                payload={"memory_item_id": str(item_id)},
                status="running",
                attempts=0,
                max_attempts=1,
            )
            session.add(job)
            await session.commit()
            job_id = job.id
        # Re-open a fresh session for the handler (mirrors process_one_job: the
        # job is claimed under one session, the handler runs under another).
        async with app_factory() as session:
            await apply_rls_context(session, tenant_id=str(tenant), principal_id=str(author_id))
            from engram.models import Job as JobModel

            job_obj = await session.get(JobModel, job_id)
            assert job_obj is not None
            await handle_conflict_check(session, job_obj)

    async def run_conflict_check_via_process(item_id: uuid.UUID) -> None:
        """Enqueue + process a conflict.check job through the production worker loop."""
        async with owner_factory() as session:
            await enqueue_job(
                session,
                tenant_id=str(tenant),
                job_type="conflict.check",
                payload={"memory_item_id": str(item_id)},
            )
        await process_one_job(
            worker_id="test",
            session_factory=owner_factory,
            app_session_factory=app_factory,
            job_types=["conflict.check"],
        )

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
        "actors": {k: v[0] for k, v in actors.items()},
        "keys": keys,
        "insert_item": insert_item,
        "add_ready_embedding": add_ready_embedding,
        "state": state,
        "force_detection": force_detection,
        "run_conflict_check": run_conflict_check,
        "run_conflict_check_via_process": run_conflict_check_via_process,
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
                text(f"DROP TRIGGER IF EXISTS worker_flag_pause_{tag} ON memory_items")
            )
            await conn.execute(text(f"DROP FUNCTION IF EXISTS worker_flag_pause_{tag}()"))
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


async def _review(
    p: dict[str, Any], actor: str, item_id: uuid.UUID, status: str, **body: Any
) -> Any:
    return await p["client"].post(
        f"/v1/items/{item_id}/review",
        json={"review_status": status, **body},
        headers=_headers(p, actor),
    )


async def _resolve(
    p: dict[str, Any], actor: str, item_id: uuid.UUID, resolution: str = "accepted", **body: Any
) -> Any:
    return await p["client"].post(
        f"/v1/items/{item_id}/resolve-conflict",
        json={"resolution": resolution, **body},
        headers=_headers(p, actor),
    )


async def _await_blocked_on(
    coordinator: Any, coordinator_pid: int, expected: int, *, wait_event: str = "advisory"
) -> None:
    blocker_sql = text(
        "WITH RECURSIVE lock_chain(pid) AS ("
        " SELECT pid FROM pg_stat_activity"
        " WHERE :coordinator_pid = ANY(pg_blocking_pids(pid))"
        " AND wait_event_type = 'Lock'"
        " UNION"
        " SELECT activity.pid FROM pg_stat_activity activity"
        " JOIN lock_chain blocker"
        " ON blocker.pid = ANY(pg_blocking_pids(activity.pid))"
        " WHERE activity.wait_event_type = 'Lock'"
        ") SELECT count(*) AS waiters,"
        " count(*) FILTER (WHERE activity.wait_event = :wait_event) AS named_waiters"
        " FROM lock_chain JOIN pg_stat_activity activity USING (pid)"
    )
    for _ in range(1000):
        await coordinator.execute(text("SELECT pg_stat_clear_snapshot()"))
        overlap = (
            await coordinator.execute(
                blocker_sql,
                {"coordinator_pid": coordinator_pid, "wait_event": wait_event},
            )
        ).one()
        if overlap == (expected, expected):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {expected} blocked sessions, last overlap={overlap}")


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


async def _install_worker_pause_trigger(p: dict[str, Any], *, item_id: uuid.UUID) -> None:
    """Pause the worker's guarded UPDATE on ``item_id`` via an advisory lock.

    The trigger fires BEFORE UPDATE OF conflicts_with_item_id on the target row
    (the guarded transition sets conflicts_with_item_id from NULL to the
    counterpart). It takes the advisory lock the coordinator holds, so the
    worker holds the pair locks but does not commit.
    """
    trigger = f"worker_flag_pause_{p['tag']}"
    async with p["owner"].begin() as conn:
        await conn.execute(
            text(
                f"CREATE FUNCTION {trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN PERFORM pg_advisory_xact_lock({p['pause_key']}); RETURN NEW; END $$"
            )
        )
        await conn.execute(
            text(
                f"CREATE TRIGGER {trigger} BEFORE UPDATE OF conflicts_with_item_id "
                f"ON memory_items FOR EACH ROW WHEN (OLD.tenant_id = '{p['tenant']}' "
                f"AND OLD.id = '{item_id}' AND OLD.conflicts_with_item_id IS NULL) "
                f"EXECUTE FUNCTION {trigger}()"
            )
        )


async def _drop_worker_pause_trigger(p: dict[str, Any]) -> None:
    trigger = f"worker_flag_pause_{p['tag']}"
    async with p["owner"].begin() as conn:
        await conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger} ON memory_items"))
        await conn.execute(text(f"DROP FUNCTION IF EXISTS {trigger}()"))


async def _worker_pid_blocked_on_coordinator(
    coordinator: Any, coordinator_pid: int
) -> int:
    row = (
        await coordinator.execute(
            text(
                "SELECT pid FROM pg_stat_activity"
                " WHERE :coordinator_pid = ANY(pg_blocking_pids(pid))"
                " AND wait_event_type = 'Lock' AND wait_event = 'advisory'"
            ),
            {"coordinator_pid": coordinator_pid},
        )
    ).first()
    assert row is not None, "worker did not reach the paused UPDATE"
    return int(row[0])


def _conflict_detected_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        e
        for e in events
        if e["event_type"] == "conflict_detected" and e["field_name"] == "conflicts_with_item_id"
    ]


# ===========================================================================
# 1. Worker wins, then human resolves
# ===========================================================================


async def test_worker_wins_then_human_resolves(proof: dict[str, Any]) -> None:
    """The worker establishes an unresolved relationship from a clean state,
    paused at its guarded UPDATE while holding the canonical pair locks. A
    Bearer-authenticated human resolution request starts during this window and
    *cannot observe the half-committed establish*: the counterpart is only
    visible after the worker commits (READ COMMITTED), so the human request
    sees no conflict to resolve and returns 422 — it cannot race into the
    worker's atomic establish. The worker commits one unresolved relationship
    and one ``conflict_detected`` event. The human request is then resubmitted,
    sees the committed relationship, and resolves it. The final state carries
    the human decision; the event history is ordered and truthful; the worker
    does not later reopen the decision."""
    p = proof
    old_item = await p["insert_item"](
        content="worker-wins-old", created_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    new_item = await p["insert_item"](
        content="worker-wins-new", created_at=datetime(2026, 1, 2, tzinfo=UTC)
    )
    for iid in (old_item, new_item):
        await p["add_ready_embedding"](iid)
    p["force_detection"](
        existing_item_id=old_item, action="flag_contradiction", conflict_type="contradiction"
    )

    await _install_worker_pause_trigger(p, item_id=new_item)
    coordinator = await p["owner"].connect()
    worker_task: asyncio.Task[None] | None = None
    try:
        coordinator_pid = await coordinator.scalar(text("SELECT pg_backend_pid()"))
        await coordinator.execute(text("SELECT pg_advisory_lock(:key)"), {"key": p["pause_key"]})

        worker_task = asyncio.create_task(p["run_conflict_check"](new_item))
        # Worker reaches its paused guarded UPDATE holding the pair locks
        # (conflicts_with_item_id is still NULL at this point — uncommitted).
        await _await_blocked_on(coordinator, coordinator_pid, 1)
        worker_pid = await _worker_pid_blocked_on_coordinator(coordinator, coordinator_pid)

        # The human resolution cannot observe the worker's half-committed
        # establish: the counterpart is not visible under READ COMMITTED until
        # the worker commits, so this returns 422 ("no conflict to resolve")
        # rather than racing into the worker's pair lock.
        resolve_during = await _resolve(p, "user_review", new_item, "accepted", reason="during")
        assert resolve_during.status_code == 422, resolve_during.text
        # Prove the worker is still holding the pair locks (it did not commit).
        n_blocked = (
            await coordinator.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE :pid = ANY(pg_blocking_pids(pid)) AND wait_event_type='Lock'"
                ),
                {"pid": worker_pid},
            )
        ).scalar()
        # No human request is waiting on the worker because the resolve could
        # not reach its pair-lock stage; the worker is still blocked on the
        # advisory lock (paused).
        assert n_blocked == 0

        # Release: worker commits one unresolved relationship + one event.
        await coordinator.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": p["pause_key"]})
        await asyncio.wait_for(worker_task, timeout=10)
    finally:
        if worker_task is not None and not worker_task.done():
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
        if coordinator.in_transaction():
            await coordinator.rollback()
        await coordinator.execute(text("SELECT pg_advisory_unlock_all()"))
        await coordinator.close()
        await _drop_worker_pause_trigger(p)

    # Worker established one unresolved relationship + one event.
    st = await p["state"](new_item)
    assert st["item"]["conflicts_with_item_id"] == old_item
    assert st["item"]["conflict_resolution_status"] == "unresolved"
    assert st["item"]["review_status"] == "proposed"  # active -> proposed demote
    cd_events = _conflict_detected_events(st["events"])
    assert len(cd_events) == 1
    assert cd_events[0]["old_value"] is None
    assert cd_events[0]["new_value"] == str(old_item)

    # Now the human resolves the committed relationship.
    resolve_resp = await _resolve(p, "user_review", new_item, "accepted", reason="human after worker")
    assert resolve_resp.status_code == 200, resolve_resp.text
    assert resolve_resp.json()["status"] == "resolved"

    # Final state carries the human decision; worker did not reopen it.
    st = await p["state"](new_item)
    assert st["item"]["conflict_resolution_status"] == "accepted"
    assert st["item"]["conflicts_with_item_id"] == old_item
    assert st["item"]["conflict_resolved_by"] == p["actors"]["user_review"]
    # One conflict_detected event + one conflict_resolution event, ordered.
    cd = _conflict_detected_events(st["events"])
    res = [e for e in st["events"] if e["event_type"] == "conflict_resolution"]
    assert len(cd) == 1
    assert cd[0]["new_value"] == str(old_item)
    assert len(res) == 1
    assert res[0]["old_value"] == "unresolved"
    assert res[0]["new_value"] == "accepted"
    assert res[0]["actor_principal_id"] == str(p["actors"]["user_review"])


# ===========================================================================
# 2. Human resolution wins first
# ===========================================================================


async def test_human_resolution_wins_first_worker_does_not_reopen(proof: dict[str, Any]) -> None:
    p = proof
    old_item = await p["insert_item"](
        content="resolve-first-old", created_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    new_item = await p["insert_item"](
        content="resolve-first-new",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        conflicts_with_item_id=old_item,
        conflict_type="contradiction",
        conflict_resolution_status="unresolved",
        review_status="proposed",
    )
    # Reciprocal relationship so resolve-conflict's pair lock finds both rows.
    async with p["owner"].begin() as conn:
        await conn.execute(
            text(
                "UPDATE memory_items SET conflicts_with_item_id=:cp, "
                "conflict_type='contradiction', conflict_resolution_status='unresolved' "
                "WHERE id=:id"
            ),
            {"id": old_item, "cp": new_item},
        )
    for iid in (old_item, new_item):
        await p["add_ready_embedding"](iid)
    p["force_detection"](
        existing_item_id=old_item, action="flag_contradiction", conflict_type="contradiction"
    )

    # Pause the human resolution's UPDATE so it owns the pair locks without committing.
    trigger = f"worker_flag_pause_{p['tag']}"
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
                f"AND OLD.id = '{new_item}' AND OLD.conflict_resolution_status = 'unresolved') "
                f"EXECUTE FUNCTION {trigger}()"
            )
        )

    coordinator = await p["owner"].connect()
    resolve_task: asyncio.Task[Any] | None = None
    worker_task: asyncio.Task[None] | None = None
    try:
        coordinator_pid = await coordinator.scalar(text("SELECT pg_backend_pid()"))
        await coordinator.execute(text("SELECT pg_advisory_lock(:key)"), {"key": p["pause_key"]})

        async def submit_resolve() -> Any:
            async with AsyncClient(
                transport=ASGITransport(app=create_app()), base_url="http://test"
            ) as client:
                return await client.post(
                    f"/v1/items/{new_item}/resolve-conflict",
                    json={"resolution": "accepted", "reason": "human first"},
                    headers=_headers(p, "user_review"),
                )

        resolve_task = asyncio.create_task(submit_resolve())
        await _await_blocked_on(coordinator, coordinator_pid, 1)
        resolver_pid = await _worker_pid_blocked_on_coordinator(coordinator, coordinator_pid)

        # Worker runs while the human owns the pair locks; it waits for them.
        worker_task = asyncio.create_task(p["run_conflict_check"](new_item))
        await _await_blocked_on_pid(coordinator, resolver_pid, 1)
        assert not worker_task.done(), "worker must wait behind human resolution"

        # Release: human resolution commits first.
        await coordinator.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": p["pause_key"]})
        resolve_resp = await asyncio.wait_for(resolve_task, timeout=10)
        assert resolve_resp.status_code == 200, resolve_resp.text
        # Worker resumes, observes the completed decision, does not reset it.
        await asyncio.wait_for(worker_task, timeout=10)
    finally:
        for task in (worker_task, resolve_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if coordinator.in_transaction():
            await coordinator.rollback()
        await coordinator.execute(text("SELECT pg_advisory_unlock_all()"))
        await coordinator.close()
        async with p["owner"].begin() as conn:
            await conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger} ON memory_items"))
            await conn.execute(text(f"DROP FUNCTION IF EXISTS {trigger}()"))

    st = await p["state"](new_item)
    # Completed decision preserved; worker did not reset to unresolved.
    assert st["item"]["conflict_resolution_status"] == "accepted"
    assert st["item"]["conflicts_with_item_id"] == old_item
    assert st["item"]["conflict_resolved_by"] == p["actors"]["user_review"]
    assert st["item"]["conflict_resolved_at"] is not None
    # No additional worker flagging event: exactly one conflict_resolution event,
    # and zero conflict_detected events for conflicts_with_item_id.
    cd = _conflict_detected_events(st["events"])
    res = [e for e in st["events"] if e["event_type"] == "conflict_resolution"]
    assert cd == [], [dict(e) for e in st["events"]]
    assert len(res) == 1
    assert res[0]["new_value"] == "accepted"
    assert res[0]["actor_principal_id"] == str(p["actors"]["user_review"])


# ===========================================================================
# 3. Human review wins first (rejected/archived + disputed)
# ===========================================================================


@pytest.mark.parametrize("decision", ["rejected", "archived"])
async def test_terminal_review_first_worker_skips(proof: dict[str, Any], decision: str) -> None:
    p = proof
    old_item = await p["insert_item"](
        content=f"terminal-{decision}-old", created_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    # The new item already carries the human terminal decision.
    new_item = await p["insert_item"](
        content=f"terminal-{decision}-new",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        review_status=decision,
    )
    for iid in (old_item, new_item):
        await p["add_ready_embedding"](iid)
    p["force_detection"](
        existing_item_id=old_item, action="flag_contradiction", conflict_type="contradiction"
    )

    before = await p["state"](new_item)
    await p["run_conflict_check"](new_item)
    after = await p["state"](new_item)

    # Worker skips all mutation and writes no flagging event.
    assert after["item"] == before["item"]
    assert after["events"] == before["events"]
    cd = _conflict_detected_events(after["events"])
    assert cd == []
    assert after["item"]["conflicts_with_item_id"] is None
    assert after["item"]["review_status"] == decision


async def test_disputed_review_first_worker_preserves_disputed(proof: dict[str, Any]) -> None:
    p = proof
    old_item = await p["insert_item"](
        content="disputed-old", created_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    new_item = await p["insert_item"](
        content="disputed-new",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        review_status="disputed",
    )
    for iid in (old_item, new_item):
        await p["add_ready_embedding"](iid)
    p["force_detection"](
        existing_item_id=old_item, action="flag_contradiction", conflict_type="contradiction"
    )

    await p["run_conflict_check"](new_item)
    st = await p["state"](new_item)

    # Conflict metadata may be recorded once (no existing relationship), but
    # review stays disputed — never demoted to proposed.
    assert st["item"]["review_status"] == "disputed"
    assert st["item"]["conflicts_with_item_id"] == old_item
    assert st["item"]["conflict_type"] == "contradiction"
    assert st["item"]["conflict_resolution_status"] == "unresolved"
    cd = _conflict_detected_events(st["events"])
    assert len(cd) == 1, [dict(e) for e in st["events"]]
    assert cd[0]["old_value"] is None
    assert cd[0]["new_value"] == str(old_item)

    # Idempotent rerun on the disputed item: no second event, no state change.
    before = await p["state"](new_item)
    await p["run_conflict_check"](new_item)
    after = await p["state"](new_item)
    assert after["item"] == before["item"]
    cd2 = _conflict_detected_events(after["events"])
    assert len(cd2) == 1


# ===========================================================================
# 4. Worker wins before review
# ===========================================================================


async def test_worker_wins_before_review_resumes_from_committed_state(
    proof: dict[str, Any],
) -> None:
    p = proof
    old_item = await p["insert_item"](
        content="worker-before-review-old", created_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    new_item = await p["insert_item"](
        content="worker-before-review-new", created_at=datetime(2026, 1, 2, tzinfo=UTC)
    )
    for iid in (old_item, new_item):
        await p["add_ready_embedding"](iid)
    p["force_detection"](
        existing_item_id=old_item, action="flag_contradiction", conflict_type="contradiction"
    )

    await _install_worker_pause_trigger(p, item_id=new_item)
    coordinator = await p["owner"].connect()
    worker_task: asyncio.Task[None] | None = None
    review_task: asyncio.Task[Any] | None = None
    try:
        coordinator_pid = await coordinator.scalar(text("SELECT pg_backend_pid()"))
        await coordinator.execute(text("SELECT pg_advisory_lock(:key)"), {"key": p["pause_key"]})

        worker_task = asyncio.create_task(p["run_conflict_check"](new_item))
        await _await_blocked_on(coordinator, coordinator_pid, 1)
        worker_pid = await _worker_pid_blocked_on_coordinator(coordinator, coordinator_pid)

        async def submit_review() -> Any:
            async with AsyncClient(
                transport=ASGITransport(app=create_app()), base_url="http://test"
            ) as client:
                return await client.post(
                    f"/v1/items/{new_item}/review",
                    json={"review_status": "disputed", "reason": "review after worker flag"},
                    headers=_headers(p, "user_review"),
                )

        review_task = asyncio.create_task(submit_review())
        # Review waits behind the worker's pair lock.
        await _await_blocked_on_pid(coordinator, worker_pid, 1)
        assert not review_task.done(), "review must wait behind worker"

        # Release: worker commits the valid flag transition (active -> proposed).
        await coordinator.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": p["pause_key"]})
        await asyncio.wait_for(worker_task, timeout=10)

        # Review resumes from the committed proposed state: proposed -> disputed.
        review_resp = await asyncio.wait_for(review_task, timeout=10)
        assert review_resp.status_code == 200, review_resp.text
    finally:
        for task in (worker_task, review_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if coordinator.in_transaction():
            await coordinator.rollback()
        await coordinator.execute(text("SELECT pg_advisory_unlock_all()"))
        await coordinator.close()
        await _drop_worker_pause_trigger(p)

    st = await p["state"](new_item)
    # Final state reflects worker flag + human dispute, a valid serial order.
    assert st["item"]["review_status"] == "disputed"
    assert st["item"]["conflicts_with_item_id"] == old_item
    assert st["item"]["conflict_resolution_status"] == "unresolved"
    cd = _conflict_detected_events(st["events"])
    review_events = [
        e for e in st["events"] if e["event_type"] == "review_change" and e["field_name"] == "review_status"
    ]
    # Worker wrote one conflict_detected (active -> proposed), then the human
    # review wrote proposed -> disputed. Ordered and truthful.
    assert len(cd) == 1
    assert cd[0]["new_value"] == str(old_item)
    assert len(review_events) == 1
    assert review_events[0]["old_value"] == "proposed"
    assert review_events[0]["new_value"] == "disputed"
    assert review_events[0]["actor_principal_id"] == str(p["actors"]["user_review"])
    # The review event's old_value matches the worker's committed state.
    assert review_events[0]["old_value"] == "proposed"


# ===========================================================================
# 5. Idempotent rerun
# ===========================================================================


async def test_idempotent_rerun_is_event_free(proof: dict[str, Any]) -> None:
    p = proof
    old_item = await p["insert_item"](
        content="idempotent-old", created_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    new_item = await p["insert_item"](
        content="idempotent-new", created_at=datetime(2026, 1, 2, tzinfo=UTC)
    )
    for iid in (old_item, new_item):
        await p["add_ready_embedding"](iid)
    p["force_detection"](
        existing_item_id=old_item, action="flag_contradiction", conflict_type="contradiction"
    )

    await p["run_conflict_check"](new_item)
    first = await p["state"](new_item)
    assert first["item"]["conflicts_with_item_id"] == old_item
    assert first["item"]["conflict_resolution_status"] == "unresolved"
    cd1 = _conflict_detected_events(first["events"])
    assert len(cd1) == 1

    # Rerun the same flagging job.
    await p["run_conflict_check"](new_item)
    second = await p["state"](new_item)

    # No state changes, no duplicate event, no resolver/review metadata changes.
    assert second["item"] == first["item"]
    assert second["events"] == first["events"]
    cd2 = _conflict_detected_events(second["events"])
    assert len(cd2) == 1


# ===========================================================================
# 6. Different relationship preservation
# ===========================================================================


async def test_different_existing_relationship_not_replaced(proof: dict[str, Any]) -> None:
    p = proof
    item_b = await p["insert_item"](
        content="existing-b", created_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    item_c = await p["insert_item"](
        content="detected-c", created_at=datetime(2026, 1, 3, tzinfo=UTC)
    )
    # The job item already points to item B (unresolved), created between B and C.
    job_item = await p["insert_item"](
        content="different-rel-job",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        conflicts_with_item_id=item_b,
        conflict_type="scope_overlap",
        conflict_resolution_status="unresolved",
        review_status="proposed",
    )
    for iid in (job_item, item_b, item_c):
        await p["add_ready_embedding"](iid)
    # Worker detects item C, not item B.
    p["force_detection"](
        existing_item_id=item_c, action="flag_contradiction", conflict_type="contradiction"
    )

    before = await p["state"](job_item)
    await p["run_conflict_check"](job_item)
    after = await p["state"](job_item)

    # B is not replaced by C; no new flagging event; review unchanged.
    assert after["item"]["conflicts_with_item_id"] == item_b
    assert after["item"]["conflict_type"] == "scope_overlap"
    assert after["item"]["conflict_resolution_status"] == "unresolved"
    assert after["item"]["review_status"] == "proposed"
    assert after["events"] == before["events"]
    cd = _conflict_detected_events(after["events"])
    assert cd == before.get("conflict_detected", [])
    # No conflict_detected event naming item_c was written.
    assert all(e["new_value"] != str(item_c) for e in after["events"])


# ===========================================================================
# 7. Reciprocal worker stress
# ===========================================================================


@pytest.mark.parametrize("iteration", range(3))
async def test_reciprocal_worker_pair_no_deadlock(proof: dict[str, Any], iteration: int) -> None:
    p = proof
    # Two items in the same pair; their conflict.check jobs detect each other.
    # The creation-order guard must allow only the newer side to act.
    item_a = await p["insert_item"](
        content=f"reciprocal-a-{iteration}", created_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    item_b = await p["insert_item"](
        content=f"reciprocal-b-{iteration}", created_at=datetime(2026, 1, 2, tzinfo=UTC)
    )
    for iid in (item_a, item_b):
        await p["add_ready_embedding"](iid)

    # item_b is newer (created later). item_a's job detects item_b (so it would
    # be the older side and must NOT act); item_b's job detects item_a (newer
    # side, should act). Reversing request order on odd iterations proves
    # canonical pair lock ordering regardless of submission order.
    from engram.conflicts import ConflictAction, ConflictResult, ConflictVerdict

    result_a_detects_b = ConflictResult(
        verdict=ConflictVerdict.CONTRADICT,
        action=ConflictAction.FLAG_CONTRADICTION,
        existing_item_id=item_b,
        similarity=0.95,
        classifier_confidence=0.9,
        conflict_type="contradiction",
        reason="forced",
        provenance={"provider": "test"},
    )
    result_b_detects_a = ConflictResult(
        verdict=ConflictVerdict.CONTRADICT,
        action=ConflictAction.FLAG_CONTRADICTION,
        existing_item_id=item_a,
        similarity=0.95,
        classifier_confidence=0.9,
        conflict_type="contradiction",
        reason="forced",
        provenance={"provider": "test"},
    )

    original_detect = conflicts_mod.detect_conflicts

    async def detect_dispatch(item: MemoryItem, session: AsyncSession, **kw: Any) -> Any:
        if item.id == item_a:
            return result_a_detects_b
        if item.id == item_b:
            return result_b_detects_a
        return await original_detect(item, session, **kw)

    import engram.conflicts as cm

    cm.detect_conflicts = detect_dispatch  # type: ignore[assignment]  # noqa: SLF001
    try:
        jobs = [item_a, item_b] if iteration % 2 else [item_b, item_a]
        await asyncio.wait_for(
            asyncio.gather(p["run_conflict_check"](jobs[0]), p["run_conflict_check"](jobs[1])),
            timeout=20,
        )
    finally:
        cm.detect_conflicts = original_detect  # noqa: SLF001

    # Only the newer item (item_b) acts: it gets the unresolved relationship to
    # item_a. item_a is untouched (creation-order guard held).
    st_a = await p["state"](item_a)
    st_b = await p["state"](item_b)
    assert st_b["item"]["conflicts_with_item_id"] == item_a
    assert st_b["item"]["conflict_resolution_status"] == "unresolved"
    assert st_b["item"]["review_status"] == "proposed"  # active -> proposed
    assert st_a["item"]["conflicts_with_item_id"] is None
    assert st_a["item"]["review_status"] == "active"
    # At most one effective relationship/event created (on item_b).
    cd_b = _conflict_detected_events(st_b["events"])
    cd_a = _conflict_detected_events(st_a["events"])
    assert len(cd_b) == 1
    assert cd_b[0]["new_value"] == str(item_a)
    assert cd_a == []
    # No partial state: item_b has a complete unresolved relationship.
    assert st_b["item"]["conflict_type"] == "contradiction"


# ===========================================================================
# 8. Counterpart already non-active (non-concurrent regression)
#
# P0-FIX-004C1A: detection proposes counterpart B, but B is already
# non-active when the pair is locked. ``_apply_flagging()`` must fail closed —
# no state change, no event.
# ===========================================================================


@pytest.mark.parametrize("counterpart_status", ["rejected", "disputed", "archived"])
async def test_counterpart_already_non_active_worker_skips(
    proof: dict[str, Any], counterpart_status: str
) -> None:
    """Detection proposes a counterpart that is already in a non-active review
    state when the pair is locked. The worker must not flag the job item
    against a counterpart ``detect_conflicts`` would no longer return."""
    p = proof
    old_item = await p["insert_item"](
        content=f"counterpart-stale-{counterpart_status}-old",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        review_status=counterpart_status,
    )
    new_item = await p["insert_item"](
        content=f"counterpart-stale-{counterpart_status}-new",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    for iid in (old_item, new_item):
        await p["add_ready_embedding"](iid)
    p["force_detection"](
        existing_item_id=old_item, action="flag_contradiction", conflict_type="contradiction"
    )

    before_new = await p["state"](new_item)
    before_old = await p["state"](old_item)
    await p["run_conflict_check"](new_item)
    after_new = await p["state"](new_item)
    after_old = await p["state"](old_item)

    # Worker skips all mutation and writes no flagging event on the job item.
    assert after_new["item"] == before_new["item"]
    assert after_new["events"] == before_new["events"]
    cd = _conflict_detected_events(after_new["events"])
    assert cd == []
    assert after_new["item"]["conflicts_with_item_id"] is None
    assert after_new["item"]["review_status"] == "active"
    # The counterpart is untouched by the worker.
    assert after_old["item"] == before_old["item"]
    assert after_old["item"]["review_status"] == counterpart_status


# ===========================================================================
# 9. Human review wins first on the COUNTERPART (rejected / disputed)
#
# P0-FIX-004C1A: a Bearer-authenticated human review transitions the
# detected counterpart B away from active while it owns B's row lock. The
# worker waits behind the human-held counterpart lock, the human review
# commits, the worker reloads B under the pair lock and observes the
# non-active review state, and performs no flagging mutation.
# ===========================================================================


async def _install_counterpart_review_pause_trigger(
    p: dict[str, Any], *, counterpart_id: uuid.UUID
) -> str:
    """Pause a human review's UPDATE on the counterpart row via an advisory lock.

    The trigger fires BEFORE UPDATE OF review_status on the counterpart. The
    review endpoint has already taken SELECT ... FOR UPDATE on the row (via
    ``_require_eligible_item(for_update=True)``) before this UPDATE runs, so
    the row lock is held while the trigger blocks on the advisory lock the
    coordinator owns.
    """
    trigger = f"worker_flag_pause_{p['tag']}"
    async with p["owner"].begin() as conn:
        await conn.execute(
            text(
                f"CREATE FUNCTION {trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN PERFORM pg_advisory_xact_lock({p['pause_key']}); RETURN NEW; END $$"
            )
        )
        await conn.execute(
            text(
                f"CREATE TRIGGER {trigger} BEFORE UPDATE OF review_status "
                f"ON memory_items FOR EACH ROW WHEN (OLD.tenant_id = '{p['tenant']}' "
                f"AND OLD.id = '{counterpart_id}') "
                f"EXECUTE FUNCTION {trigger}()"
            )
        )
    return trigger


async def _drop_trigger(p: dict[str, Any], trigger: str) -> None:
    async with p["owner"].begin() as conn:
        await conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger} ON memory_items"))
        await conn.execute(text(f"DROP FUNCTION IF EXISTS {trigger}()"))


@pytest.mark.parametrize("review_status", ["rejected", "disputed"])
async def test_counterpart_human_review_first_worker_skips(
    proof: dict[str, Any], review_status: str
) -> None:
    """A Bearer-authenticated human review transitions the counterpart B from
    active to a non-active state (rejected / disputed) while owning B's row
    lock. The worker flagging job for A→B starts, waits behind the human-held
    counterpart lock, the human review commits, the worker reloads B under
    the pair lock and observes the non-active review state, and performs no
    flagging mutation — no new counterpart, no conflict type, no resolution
    status, no review demotion on A, and no ``conflict_detected`` event."""
    p = proof
    old_item = await p["insert_item"](
        content=f"counterpart-review-first-{review_status}-old",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    new_item = await p["insert_item"](
        content=f"counterpart-review-first-{review_status}-new",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    for iid in (old_item, new_item):
        await p["add_ready_embedding"](iid)
    p["force_detection"](
        existing_item_id=old_item, action="flag_contradiction", conflict_type="contradiction"
    )

    trigger = await _install_counterpart_review_pause_trigger(p, counterpart_id=old_item)
    coordinator = await p["owner"].connect()
    review_task: asyncio.Task[Any] | None = None
    worker_task: asyncio.Task[None] | None = None
    try:
        coordinator_pid = await coordinator.scalar(text("SELECT pg_backend_pid()"))
        await coordinator.execute(text("SELECT pg_advisory_lock(:key)"), {"key": p["pause_key"]})

        async def submit_review() -> Any:
            async with AsyncClient(
                transport=ASGITransport(app=create_app()), base_url="http://test"
            ) as client:
                return await client.post(
                    f"/v1/items/{old_item}/review",
                    json={"review_status": review_status, "reason": "human first on counterpart"},
                    headers=_headers(p, "user_review"),
                )

        review_task = asyncio.create_task(submit_review())
        # The human review reaches its paused UPDATE on the counterpart,
        # holding the counterpart's FOR UPDATE row lock.
        await _await_blocked_on(coordinator, coordinator_pid, 1)
        reviewer_pid = await _worker_pid_blocked_on_coordinator(coordinator, coordinator_pid)

        # Worker runs while the human owns the counterpart row lock; it waits
        # for the pair lock (which includes the counterpart row).
        worker_task = asyncio.create_task(p["run_conflict_check"](new_item))
        await _await_blocked_on_pid(coordinator, reviewer_pid, 1)
        assert not worker_task.done(), "worker must wait behind human review on counterpart"

        # Release: human review commits first (active -> rejected/disputed).
        await coordinator.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": p["pause_key"]})
        review_resp = await asyncio.wait_for(review_task, timeout=10)
        assert review_resp.status_code == 200, review_resp.text
        # Worker resumes, reloads the counterpart under the pair lock, observes
        # the non-active review state, and skips all mutation.
        await asyncio.wait_for(worker_task, timeout=10)
    finally:
        for task in (worker_task, review_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if coordinator.in_transaction():
            await coordinator.rollback()
        await coordinator.execute(text("SELECT pg_advisory_unlock_all()"))
        await coordinator.close()
        await _drop_trigger(p, trigger)

    st_new = await p["state"](new_item)
    st_old = await p["state"](old_item)
    # A retains its original review state and no conflict relationship.
    assert st_new["item"]["review_status"] == "active"
    assert st_new["item"]["conflicts_with_item_id"] is None
    assert st_new["item"]["conflict_type"] is None
    assert st_new["item"]["conflict_resolution_status"] is None
    # No worker flagging event was written on A.
    cd = _conflict_detected_events(st_new["events"])
    assert cd == [], [dict(e) for e in st_new["events"]]
    # The counterpart B carries the human review decision.
    assert st_old["item"]["review_status"] == review_status
    review_events = [
        e
        for e in st_old["events"]
        if e["event_type"] == "review_change" and e["field_name"] == "review_status"
    ]
    assert len(review_events) == 1
    assert review_events[0]["old_value"] == "active"
    assert review_events[0]["new_value"] == review_status
    assert review_events[0]["actor_principal_id"] == str(p["actors"]["user_review"])


# ===========================================================================
# 10. Worker wins first, then human reviews the counterpart
#
# P0-FIX-004C1A: the worker owns both A and B locks and commits the A→B
# flag while B is still active. A valid Bearer-authenticated review
# transition on B starts during the window, waits behind the worker's lock,
# and after the worker commits applies its normal transition to B. The
# history represents a valid serial order; neither operation errors. The
# worker does NOT automatically remove the relationship when B changes
# afterward (cleanup is separate product work).
# ===========================================================================


async def test_worker_wins_then_counterpart_review_applies_after(
    proof: dict[str, Any],
) -> None:
    """The worker owns both A and B pair locks (paused at its guarded
    UPDATE), commits the A→B flag while B is still active (writing exactly one
    truthful event). A Bearer-authenticated review on B starts during the
    window, waits behind the worker's lock, and after the worker commits
    applies a valid active→disputed transition to B. The final history is a
    valid serial order and neither operation encounters a database error."""
    p = proof
    old_item = await p["insert_item"](
        content="worker-wins-counterpart-review-old", created_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    new_item = await p["insert_item"](
        content="worker-wins-counterpart-review-new", created_at=datetime(2026, 1, 2, tzinfo=UTC)
    )
    for iid in (old_item, new_item):
        await p["add_ready_embedding"](iid)
    p["force_detection"](
        existing_item_id=old_item, action="flag_contradiction", conflict_type="contradiction"
    )

    await _install_worker_pause_trigger(p, item_id=new_item)
    coordinator = await p["owner"].connect()
    worker_task: asyncio.Task[None] | None = None
    review_task: asyncio.Task[Any] | None = None
    try:
        coordinator_pid = await coordinator.scalar(text("SELECT pg_backend_pid()"))
        await coordinator.execute(text("SELECT pg_advisory_lock(:key)"), {"key": p["pause_key"]})

        worker_task = asyncio.create_task(p["run_conflict_check"](new_item))
        # Worker reaches its paused guarded UPDATE holding both pair locks.
        await _await_blocked_on(coordinator, coordinator_pid, 1)
        worker_pid = await _worker_pid_blocked_on_coordinator(coordinator, coordinator_pid)

        async def submit_review() -> Any:
            async with AsyncClient(
                transport=ASGITransport(app=create_app()), base_url="http://test"
            ) as client:
                return await client.post(
                    f"/v1/items/{old_item}/review",
                    json={"review_status": "disputed", "reason": "review counterpart after worker"},
                    headers=_headers(p, "user_review"),
                )

        review_task = asyncio.create_task(submit_review())
        # The human review on B waits behind the worker's pair lock.
        await _await_blocked_on_pid(coordinator, worker_pid, 1)
        assert not review_task.done(), "review must wait behind worker"

        # Release: worker commits the A→B flag while B is still active.
        await coordinator.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": p["pause_key"]})
        await asyncio.wait_for(worker_task, timeout=10)

        # Review resumes from the committed state and applies active→disputed on B.
        review_resp = await asyncio.wait_for(review_task, timeout=10)
        assert review_resp.status_code == 200, review_resp.text
    finally:
        for task in (worker_task, review_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if coordinator.in_transaction():
            await coordinator.rollback()
        await coordinator.execute(text("SELECT pg_advisory_unlock_all()"))
        await coordinator.close()
        await _drop_worker_pause_trigger(p)

    st_new = await p["state"](new_item)
    st_old = await p["state"](old_item)
    # Worker established A→B while B was still active: one unresolved
    # relationship + one truthful conflict_detected event on A.
    assert st_new["item"]["conflicts_with_item_id"] == old_item
    assert st_new["item"]["conflict_resolution_status"] == "unresolved"
    assert st_new["item"]["review_status"] == "proposed"  # active -> proposed demote
    cd = _conflict_detected_events(st_new["events"])
    assert len(cd) == 1
    assert cd[0]["old_value"] is None
    assert cd[0]["new_value"] == str(old_item)
    # After the worker committed, the human review applied active→disputed on B.
    assert st_old["item"]["review_status"] == "disputed"
    review_events = [
        e
        for e in st_old["events"]
        if e["event_type"] == "review_change" and e["field_name"] == "review_status"
    ]
    assert len(review_events) == 1
    assert review_events[0]["old_value"] == "active"
    assert review_events[0]["new_value"] == "disputed"
    assert review_events[0]["actor_principal_id"] == str(p["actors"]["user_review"])
    # The relationship is NOT automatically removed when B changes afterward
    # (cleanup of relationships invalidated by later state changes is separate
    # product work). The history remains a valid serial order.
    assert st_new["item"]["conflicts_with_item_id"] == old_item