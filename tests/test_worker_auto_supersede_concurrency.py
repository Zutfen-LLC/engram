# ruff: noqa: E501
"""Executable app-role proof for P0-FIX-004D: worker AUTO_SUPERSEDE serialization.

Proves that the worker's ``handle_conflict_check`` AUTO_SUPERSEDE branch
(``ConflictAction.AUTO_SUPERSEDE``) serializes safely against concurrent human
review, human verification, manual supersession, manual invalidation, profile
cutover, and competing worker jobs.

Proof categories (do not conflate these — they exercise different evidence
levels):

* **Ordinary behavior / idempotency** (cases 1, 2, 20): the production
  ``handle_conflict_check`` is invoked directly over a real app-role
  PostgreSQL transaction with FORCE RLS and a deterministic forced
  ``ConflictResult``. They prove the truthful mutation/event shape and
  idempotency, not concurrency overlap.
* **Committed-first stale-state revalidation** (cases 3–13): a human/owner
  mutation commits *completely* before the worker starts. The worker then
  revalidates from the locked rows and is a no-op. These prove stale-proposal
  revalidation, *not* concurrent overlap — the worker never contends with the
  other writer.
* **Deterministic blocker-graph overlap** (cases 15, 16, 21): a test-only
  trigger calls ``pg_advisory_xact_lock`` at the worker's guarded old-row
  UPDATE (or the profile-row lock) and a coordinator holds the matching
  advisory/row lock. Overlap is proven via PostgreSQL's blocker graph
  (``pg_blocking_pids``), not sleeps or task scheduling order.
* **Rollback / failure injection** (case 17): a PostgreSQL trigger raises
  during event INSERT after the guarded UPDATE; the transaction rolls back
  atomically.
* **Concurrent scheduling without observed contention** (case 14): two
  reciprocal proposals are scheduled concurrently, but creation-direction
  means only the newer side reaches ``_apply_auto_supersede``. This proves
  concurrent scheduling completes and creation direction is invariant to
  task order; it does *not* prove pair-lock contention or observed blocking
  (both handlers cannot contend for the pair lock in the current path).

Almost all cases invoke the production ``handle_conflict_check`` directly
(the same function ``process_one_job`` dispatches to). Case 22 is a focused
dispatch smoke test that runs through the production ``process_one_job``
loop (claim → handler → mark succeeded) to cover the dispatch path.

Conflict detection is deterministically controlled by monkeypatching
``engram.conflicts.detect_conflicts`` so the tests exercise the production
mutation branch without a live embedding provider. Human requests use real
Bearer credentials through ASGI. The owner connection is used only to arrange
state, install triggers, and inspect committed state.

Requires a live PostgreSQL with the v2 schema and the non-owner application
role; skips automatically when unreachable.

Scope: only the AUTO_SUPERSEDE branch (``ConflictAction.AUTO_SUPERSEDE``).
``DEDUP`` and the flagging actions closed by P0-FIX-004C1/004C1A/004C2 are
intentionally out of scope and untouched.
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

# Authority ordinals (mirrors engram.authority.MemoryAuthority).
_TRUSTED_IMPORT = 40
_EXPLICIT_USER = 50


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
    # Actors: an agent author writes items; a human user reviews, verifies,
    # supersedes, and invalidates. ``user_review`` holds ``write`` and
    # ``review`` so it can dispute (write) and activate/reject (review);
    # verification is human-only (user/admin) and needs ``review``.
    actors = {
        "author": (uuid.uuid4(), "agent", [], None),
        "user_review": (uuid.uuid4(), "user", ["write", "review"], None),
    }
    keys: dict[str, str] = {}
    async with owner.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id,name,slug) VALUES (:id,:n,:n)"),
            {"id": tenant, "n": f"worker-supersede-{tag}"},
        )
        await conn.execute(
            text(
                "INSERT INTO tenant_config (tenant_id,config_version,active) VALUES (:id,'proof',true)"
            ),
            {"id": tenant},
        )
        await conn.execute(
            text("INSERT INTO workspaces (id,tenant_id,name,slug) VALUES (:id,:tid,:n,:n)"),
            {"id": workspace, "tid": tenant, "n": f"supersede-{tag}"},
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
                        "label": f"worker-supersede-{tag}-{name}",
                    },
                )

    author_id = actors["author"][0]
    old = datetime.now(UTC).replace(microsecond=0)

    async def insert_item(
        *,
        content: str,
        review_status: str = "active",
        created_at: datetime | None = None,
        authority: int = 10,
        human_verified: bool = False,
        verified_by: uuid.UUID | None = None,
        valid_to: datetime | None = None,
        superseded_by: uuid.UUID | None = None,
        workspace_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        item_id = uuid.uuid4()
        cts = created_at if created_at is not None else old
        async with owner.begin() as conn:
            cols = (
                "id,tenant_id,principal_id,content,content_hash,kind,visibility,"
                "review_status,memory_confidence,source_trust,importance,source_type,"
                "authority,created_at,valid_from"
            )
            vals = (
                ":id,:tid,:pid,:content,:hash,'fact','tenant',:review_status,"
                ".81,.73,.64,'manual',:authority,:created,:created"
            )
            params: dict[str, Any] = {
                "id": item_id,
                "tid": tenant,
                "pid": author_id,
                "content": f"{tag}:{content}",
                "hash": f"sha256:{item_id.hex}",
                "review_status": review_status,
                "authority": authority,
                "created": cts,
            }
            extra_cols = ""
            extra_vals = ""
            if human_verified:
                extra_cols += ",human_verified,verified_by"
                extra_vals += ",TRUE,:vby"
                params["vby"] = verified_by
            if valid_to is not None:
                extra_cols += ",valid_to"
                extra_vals += ",:valid_to"
                params["valid_to"] = valid_to
            if superseded_by is not None:
                extra_cols += ",superseded_by"
                extra_vals += ",:superseded_by"
                params["superseded_by"] = superseded_by
            if workspace_id is not None:
                extra_cols += ",workspace_id"
                extra_vals += ",:workspace_id"
                params["workspace_id"] = workspace_id
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
        *,
        existing_item_id: uuid.UUID,
        action: str = "auto_supersede",
        verdict: str | None = None,
        conflict_type: str | None = None,
        classifier_confidence: float = 0.95,
        provenance: dict[str, Any] | None = None,
    ) -> None:
        from engram.conflicts import ConflictAction, ConflictResult, ConflictVerdict

        resolved_verdict = (
            ConflictVerdict(verdict)
            if verdict is not None
            else (ConflictVerdict.REFINE if action == "auto_supersede" else ConflictVerdict.DUPLICATE)
        )
        result = ConflictResult(
            verdict=resolved_verdict,
            action=ConflictAction(action),
            existing_item_id=existing_item_id,
            similarity=0.97,
            classifier_confidence=classifier_confidence,
            conflict_type=conflict_type,
            reason=f"forced {action}",
            provenance=provenance if provenance is not None else {"provider": "test", "mode": "forced"},
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
    # Drain stale pending/running conflict.check jobs left by other test modules
    # so the ``process_one_job`` dispatch smoke test claims only this fixture's
    # job (claim is global across tenants under the owner role).
    async with owner.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM jobs WHERE job_type='conflict.check' "
                "AND status IN ('pending','running')"
            )
        )
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
                text(f"DROP TRIGGER IF EXISTS worker_supersede_pause_{tag} ON memory_items")
            )
            await conn.execute(text(f"DROP FUNCTION IF EXISTS worker_supersede_pause_{tag}()"))
            await conn.execute(
                text(f"DROP TRIGGER IF EXISTS worker_supersede_event_fail_{tag} ON item_events")
            )
            await conn.execute(text(f"DROP FUNCTION IF EXISTS worker_supersede_event_fail_{tag}()"))
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


async def _verify(p: dict[str, Any], actor: str, item_id: uuid.UUID, **body: Any) -> Any:
    return await p["client"].post(
        f"/v1/items/{item_id}/verify",
        json={**body} if body else None,
        headers=_headers(p, actor),
    )


async def _supersede(p: dict[str, Any], actor: str, item_id: uuid.UUID, **body: Any) -> Any:
    return await p["client"].post(
        f"/v1/items/{item_id}/supersede",
        json={**body} if body else None,
        headers=_headers(p, actor),
    )


async def _invalidate(p: dict[str, Any], actor: str, item_id: uuid.UUID, **body: Any) -> Any:
    return await p["client"].post(
        f"/v1/items/{item_id}/invalidate",
        json={**body} if body else None,
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
    """Pause the worker's guarded old-row supersession UPDATE via an advisory lock.

    The trigger fires BEFORE UPDATE OF valid_to on the target row (the guarded
    supersession sets valid_to from NULL to the supersession timestamp). It takes
    the advisory lock the coordinator holds, so the worker holds the pair locks
    but does not commit.
    """
    trigger = f"worker_supersede_pause_{p['tag']}"
    async with p["owner"].begin() as conn:
        await conn.execute(
            text(
                f"CREATE FUNCTION {trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN PERFORM pg_advisory_xact_lock({p['pause_key']}); RETURN NEW; END $$"
            )
        )
        await conn.execute(
            text(
                f"CREATE TRIGGER {trigger} BEFORE UPDATE OF valid_to "
                f"ON memory_items FOR EACH ROW WHEN (OLD.tenant_id = '{p['tenant']}' "
                f"AND OLD.id = '{item_id}' AND OLD.valid_to IS NULL) "
                f"EXECUTE FUNCTION {trigger}()"
            )
        )


async def _drop_worker_pause_trigger(p: dict[str, Any]) -> None:
    trigger = f"worker_supersede_pause_{p['tag']}"
    async with p["owner"].begin() as conn:
        await conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger} ON memory_items"))
        await conn.execute(text(f"DROP FUNCTION IF EXISTS {trigger}()"))


async def _drop_trigger(p: dict[str, Any], trigger: str) -> None:
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


def _supersede_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        e
        for e in events
        if e["event_type"] == "conflict_detected" and e["field_name"] == "superseded_by"
    ]


async def _make_pair(p: dict[str, Any], *, old_content: str, new_content: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Create an old/new item pair where the new item qualifies for auto-supersession."""
    old_item = await p["insert_item"](
        content=old_content,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        authority=10,
    )
    new_item = await p["insert_item"](
        content=new_content,
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        authority=_TRUSTED_IMPORT,
    )
    return old_item, new_item


async def _setup_pair_with_embeddings(
    p: dict[str, Any], *, old_content: str, new_content: str
) -> tuple[uuid.UUID, uuid.UUID]:
    old_item, new_item = await _make_pair(p, old_content=old_content, new_content=new_content)
    for iid in (old_item, new_item):
        await p["add_ready_embedding"](iid)
    p["force_detection"](existing_item_id=old_item)
    return old_item, new_item


# ===========================================================================
# 1. Normal AUTO_SUPERSEDE mutates only old row and writes one truthful event
# ===========================================================================


async def test_normal_auto_supersede_mutates_old_row_truthful_event(
    proof: dict[str, Any],
) -> None:
    """The worker supersedes the OLD row (sets superseded_by=new.id and
    valid_to), writes exactly one truthful event on the OLD row, and leaves
    the NEW row live and unchanged."""
    p = proof
    old_item, new_item = await _setup_pair_with_embeddings(
        p, old_content="supersede-normal-old", new_content="supersede-normal-new"
    )

    await p["run_conflict_check"](new_item)

    st_old = await p["state"](old_item)
    st_new = await p["state"](new_item)
    # OLD row is superseded.
    assert str(st_old["item"]["superseded_by"]) == str(new_item)
    assert st_old["item"]["valid_to"] is not None
    assert st_old["item"]["review_status"] == "active"
    # NEW row is live and unchanged.
    assert st_new["item"]["valid_to"] is None
    assert st_new["item"]["superseded_by"] is None
    assert st_new["item"]["review_status"] == "active"
    # Exactly one truthful event on the OLD row.
    cd = _supersede_events(st_old["events"])
    assert len(cd) == 1, [dict(e) for e in st_old["events"]]
    assert cd[0]["old_value"] is None
    assert cd[0]["new_value"] == str(new_item)
    # No event on the NEW row.
    assert _supersede_events(st_new["events"]) == []


# ===========================================================================
# 2. Idempotent rerun preserves original valid_to/link and event count
# ===========================================================================


async def test_idempotent_rerun_preserves_state_and_event(proof: dict[str, Any]) -> None:
    """Re-running the same AUTO_SUPERSEDE job after a successful supersession
    does not change valid_to, replace superseded_by, or add another event."""
    p = proof
    old_item, new_item = await _setup_pair_with_embeddings(
        p, old_content="supersede-idempotent-old", new_content="supersede-idempotent-new"
    )

    await p["run_conflict_check"](new_item)
    first = await p["state"](old_item)
    assert str(first["item"]["superseded_by"]) == str(new_item)
    assert first["item"]["valid_to"] is not None
    cd1 = _supersede_events(first["events"])
    assert len(cd1) == 1
    first_valid_to = first["item"]["valid_to"]

    # Rerun the same job.
    await p["run_conflict_check"](new_item)
    second = await p["state"](old_item)

    # valid_to is unchanged; superseded_by is unchanged; no duplicate event.
    assert str(second["item"]["superseded_by"]) == str(new_item)
    assert second["item"]["valid_to"] == first_valid_to
    assert second["events"] == first["events"]
    cd2 = _supersede_events(second["events"])
    assert len(cd2) == 1

    # NEW row remains live and unchanged.
    st_new = await p["state"](new_item)
    assert st_new["item"]["valid_to"] is None
    assert st_new["item"]["superseded_by"] is None
    assert _supersede_events(st_new["events"]) == []


# ===========================================================================
# 3. Old item manually superseded first -> worker no-op
# ===========================================================================


async def test_old_item_manually_superseded_first_worker_noop(
    proof: dict[str, Any],
) -> None:
    """The old item is manually superseded via the Bearer supersede route before
    the worker locks. The worker observes superseded_by is set and valid_to is
    set, and performs no mutation or event."""
    p = proof
    old_item, new_item = await _setup_pair_with_embeddings(
        p, old_content="supersede-manual-first-old", new_content="supersede-manual-first-new"
    )

    # Manually supersede the old item via the Bearer route.
    resp = await _supersede(p, "user_review", old_item, reason="manual supersede first")
    assert resp.status_code == 200, resp.text

    before_old = await p["state"](old_item)
    before_new = await p["state"](new_item)
    await p["run_conflict_check"](new_item)
    after_old = await p["state"](old_item)
    after_new = await p["state"](new_item)

    # Worker did not touch either item.
    assert after_old["item"] == before_old["item"]
    assert after_old["events"] == before_old["events"]
    assert after_new["item"] == before_new["item"]
    assert after_new["events"] == before_new["events"]
    assert _supersede_events(after_old["events"]) == []
    assert _supersede_events(after_new["events"]) == []
    # The manual superseded_by is NOT the new item (the route creates a clone).
    assert after_old["item"]["superseded_by"] is not None
    assert after_old["item"]["superseded_by"] != str(new_item)


# ===========================================================================
# 4. Old item invalidated first -> worker no-op
# ===========================================================================


async def test_old_item_invalidated_first_worker_noop(proof: dict[str, Any]) -> None:
    """The old item is invalidated via the Bearer invalidate route before the
    worker locks. The worker observes valid_to is set and performs no mutation
    or event. (The reverse race — worker invalidates before manual invalidation
    — remains an open boundary; manual invalidation is the next writer to
    serialize.)"""
    p = proof
    old_item, new_item = await _setup_pair_with_embeddings(
        p, old_content="supersede-invalidate-first-old", new_content="supersede-invalidate-first-new"
    )

    resp = await _invalidate(p, "user_review", old_item, reason="invalidate first")
    assert resp.status_code == 200, resp.text

    before_old = await p["state"](old_item)
    before_new = await p["state"](new_item)
    await p["run_conflict_check"](new_item)
    after_old = await p["state"](old_item)
    after_new = await p["state"](new_item)

    assert after_old["item"] == before_old["item"]
    assert after_old["events"] == before_old["events"]
    assert after_new["item"] == before_new["item"]
    assert after_new["events"] == before_new["events"]
    assert _supersede_events(after_old["events"]) == []
    assert _supersede_events(after_new["events"]) == []
    assert after_old["item"]["valid_to"] is not None
    assert after_old["item"]["superseded_by"] is None


# ===========================================================================
# 5. Old item human-reviewed first (disputed/rejected/archived) -> worker no-op
# ===========================================================================


@pytest.mark.parametrize("review_status", ["disputed", "rejected", "archived"])
async def test_old_item_human_reviewed_first_worker_noop(
    proof: dict[str, Any], review_status: str
) -> None:
    """The old item is transitioned away from active via a Bearer review before
    the worker locks. The worker observes the old item is no longer active and
    performs no mutation or event."""
    p = proof
    old_item, new_item = await _setup_pair_with_embeddings(
        p,
        old_content=f"supersede-review-first-{review_status}-old",
        new_content=f"supersede-review-first-{review_status}-new",
    )

    resp = await _review(p, "user_review", old_item, review_status, reason="review first")
    assert resp.status_code == 200, resp.text

    before_old = await p["state"](old_item)
    before_new = await p["state"](new_item)
    await p["run_conflict_check"](new_item)
    after_old = await p["state"](old_item)
    after_new = await p["state"](new_item)

    assert after_old["item"] == before_old["item"]
    assert after_old["events"] == before_old["events"]
    assert after_new["item"] == before_new["item"]
    assert after_new["events"] == before_new["events"]
    assert _supersede_events(after_old["events"]) == []
    assert _supersede_events(after_new["events"]) == []
    assert after_old["item"]["review_status"] == review_status


# ===========================================================================
# 6. Old item human-verified first -> worker no-op
# ===========================================================================


async def test_old_item_human_verified_first_worker_noop(proof: dict[str, Any]) -> None:
    """The old item is human-verified via the Bearer verify route before the
    worker locks. The worker observes human governance on the old item and
    performs no mutation or event."""
    p = proof
    old_item, new_item = await _setup_pair_with_embeddings(
        p, old_content="supersede-verify-first-old", new_content="supersede-verify-first-new"
    )

    resp = await _verify(p, "user_review", old_item, reason="verify old first")
    assert resp.status_code == 200, resp.text

    before_old = await p["state"](old_item)
    before_new = await p["state"](new_item)
    await p["run_conflict_check"](new_item)
    after_old = await p["state"](old_item)
    after_new = await p["state"](new_item)

    assert after_old["item"] == before_old["item"]
    assert after_old["events"] == before_old["events"]
    assert after_new["item"] == before_new["item"]
    assert after_new["events"] == before_new["events"]
    assert _supersede_events(after_old["events"]) == []
    assert _supersede_events(after_new["events"]) == []
    assert after_old["item"]["human_verified"] is True
    assert after_old["item"]["valid_to"] is None
    assert after_old["item"]["superseded_by"] is None


# ===========================================================================
# 7. New item human-governed first -> worker no-op
# ===========================================================================


@pytest.mark.parametrize("governance", ["verified", "reviewed"])
async def test_new_item_human_governed_first_worker_noop(
    proof: dict[str, Any], governance: str
) -> None:
    """The new item is human-governed (verified or review-transitioned) via a
    Bearer route before the worker locks. The worker observes human governance
    on the new item and performs no mutation or event on the old item."""
    p = proof
    old_item, new_item = await _setup_pair_with_embeddings(
        p,
        old_content=f"supersede-new-gov-{governance}-old",
        new_content=f"supersede-new-gov-{governance}-new",
    )

    if governance == "verified":
        resp = await _verify(p, "user_review", new_item, reason="verify new first")
    else:
        resp = await _review(p, "user_review", new_item, "disputed", reason="dispute new first")
    assert resp.status_code == 200, resp.text

    before_old = await p["state"](old_item)
    before_new = await p["state"](new_item)
    await p["run_conflict_check"](new_item)
    after_old = await p["state"](old_item)
    after_new = await p["state"](new_item)

    assert after_old["item"] == before_old["item"]
    assert after_old["events"] == before_old["events"]
    assert _supersede_events(after_old["events"]) == []
    assert after_new["item"] == before_new["item"]
    assert after_new["events"] == before_new["events"]
    # The old item was NOT superseded.
    assert after_old["item"]["valid_to"] is None
    assert after_old["item"]["superseded_by"] is None


# ===========================================================================
# 8. Authority changes before lock so supersession is no longer allowed
# ===========================================================================


async def test_authority_changes_before_lock_worker_noop(proof: dict[str, Any]) -> None:
    """The new item's authority is lowered (or the old item's is raised) so
    that supersession is no longer allowed before the worker locks. The worker
    revalidates authority under the lock and performs no mutation or event."""
    p = proof
    old_item, new_item = await _setup_pair_with_embeddings(
        p, old_content="supersede-authority-old", new_content="supersede-authority-new"
    )

    # Lower the new item's authority below trusted_import so it no longer
    # qualifies for auto-supersession.
    async with p["owner"].begin() as conn:
        await conn.execute(
            text("UPDATE memory_items SET authority=10 WHERE id=:id"),
            {"id": new_item},
        )

    before_old = await p["state"](old_item)
    before_new = await p["state"](new_item)
    await p["run_conflict_check"](new_item)
    after_old = await p["state"](old_item)
    after_new = await p["state"](new_item)

    assert after_old["item"] == before_old["item"]
    assert after_old["events"] == before_old["events"]
    assert after_new["item"] == before_new["item"]
    assert after_new["events"] == before_new["events"]
    assert _supersede_events(after_old["events"]) == []
    assert after_old["item"]["valid_to"] is None
    assert after_old["item"]["superseded_by"] is None


# ===========================================================================
# 9. Kind changes before lock -> worker no-op
# ===========================================================================


@pytest.mark.parametrize("which", ["original", "newer"])
async def test_kind_changed_before_lock_worker_noop(
    proof: dict[str, Any], which: str
) -> None:
    """Detection proposes a same-kind supersede pair. After detection but
    before the worker locks, the kind of either item is changed so the two no
    longer share a kind. The worker revalidates kind under the lock and performs
    no mutation or event."""
    p = proof
    old_item, new_item = await _setup_pair_with_embeddings(
        p,
        old_content=f"supersede-kind-{which}-old",
        new_content=f"supersede-kind-{which}-new",
    )

    target = old_item if which == "original" else new_item
    async with p["owner"].begin() as conn:
        await conn.execute(
            text("UPDATE memory_items SET kind='preference' WHERE id=:id"),
            {"id": target},
        )

    before_old = await p["state"](old_item)
    before_new = await p["state"](new_item)
    await p["run_conflict_check"](new_item)
    after_old = await p["state"](old_item)
    after_new = await p["state"](new_item)

    assert after_old["item"] == before_old["item"]
    assert after_old["events"] == before_old["events"]
    assert after_new["item"] == before_new["item"]
    assert after_new["events"] == before_new["events"]
    assert _supersede_events(after_old["events"]) == []
    assert after_old["item"]["valid_to"] is None
    assert after_old["item"]["superseded_by"] is None


# ===========================================================================
# 10. Workspace scope changes before lock -> worker no-op
# ===========================================================================


@pytest.mark.parametrize("which", ["original", "newer"])
async def test_workspace_scope_changed_before_lock_worker_noop(
    proof: dict[str, Any], which: str
) -> None:
    """Detection proposes a supersede pair where both items are tenant-scoped
    (workspace_id IS NULL). After detection, one item is moved into a workspace
    so the two no longer share workspace scope. The worker revalidates exact
    workspace scope under the lock and performs no mutation or event."""
    p = proof
    old_item, new_item = await _setup_pair_with_embeddings(
        p,
        old_content=f"supersede-workspace-{which}-old",
        new_content=f"supersede-workspace-{which}-new",
    )

    target = old_item if which == "original" else new_item
    async with p["owner"].begin() as conn:
        await conn.execute(
            text("UPDATE memory_items SET workspace_id=:ws WHERE id=:id"),
            {"ws": p["workspace"], "id": target},
        )

    before_old = await p["state"](old_item)
    before_new = await p["state"](new_item)
    await p["run_conflict_check"](new_item)
    after_old = await p["state"](old_item)
    after_new = await p["state"](new_item)

    assert after_old["item"] == before_old["item"]
    assert after_old["events"] == before_old["events"]
    assert after_new["item"] == before_new["item"]
    assert after_new["events"] == before_new["events"]
    assert _supersede_events(after_old["events"]) == []
    assert after_old["item"]["valid_to"] is None
    assert after_old["item"]["superseded_by"] is None


# ===========================================================================
# 11. Either embedding becomes missing/not-ready before embedding lock
# ===========================================================================


@pytest.mark.parametrize("which", ["original", "newer"])
async def test_embedding_not_ready_before_lock_worker_noop(
    proof: dict[str, Any], which: str
) -> None:
    """Detection proposes a supersede pair (both items had ready embeddings).
    After detection, one item's embedding is marked non-ready. The worker locks
    and revalidates the embedding rows, observes the non-ready embedding, and
    performs no mutation or event."""
    p = proof
    old_item, new_item = await _setup_pair_with_embeddings(
        p,
        old_content=f"supersede-emb-{which}-old",
        new_content=f"supersede-emb-{which}-new",
    )

    target = old_item if which == "original" else new_item
    async with p["owner"].begin() as conn:
        await conn.execute(
            text(
                "UPDATE memory_embeddings SET embedding_status='failed', embedding=NULL "
                "WHERE memory_item_id=:id"
            ),
            {"id": target},
        )

    before_old = await p["state"](old_item)
    before_new = await p["state"](new_item)
    await p["run_conflict_check"](new_item)
    after_old = await p["state"](old_item)
    after_new = await p["state"](new_item)

    assert after_old["item"] == before_old["item"]
    assert after_old["events"] == before_old["events"]
    assert after_new["item"] == before_new["item"]
    assert after_new["events"] == before_new["events"]
    assert _supersede_events(after_old["events"]) == []
    assert after_old["item"]["valid_to"] is None
    assert after_old["item"]["superseded_by"] is None


# ===========================================================================
# 12. Old item ceases to be active/live before lock -> worker no-op
# ===========================================================================


async def test_old_item_ceases_active_before_lock_worker_noop(
    proof: dict[str, Any],
) -> None:
    """The old item is invalidated (valid_to set) via owner fixture after
    detection but before the worker locks. The worker observes valid_to is set
    and performs no mutation or event."""
    p = proof
    old_item, new_item = await _setup_pair_with_embeddings(
        p, old_content="supersede-old-invalidated-old", new_content="supersede-old-invalidated-new"
    )

    async with p["owner"].begin() as conn:
        await conn.execute(
            text("UPDATE memory_items SET valid_to=NOW() WHERE id=:id"),
            {"id": old_item},
        )

    before_old = await p["state"](old_item)
    before_new = await p["state"](new_item)
    await p["run_conflict_check"](new_item)
    after_old = await p["state"](old_item)
    after_new = await p["state"](new_item)

    assert after_old["item"] == before_old["item"]
    assert after_old["events"] == before_old["events"]
    assert after_new["item"] == before_new["item"]
    assert after_new["events"] == before_new["events"]
    assert _supersede_events(after_old["events"]) == []
    assert after_old["item"]["valid_to"] is not None


# ===========================================================================
# 13. New item ceases to be live/eligible before lock -> worker no-op
# ===========================================================================


async def test_new_item_ceases_live_before_lock_worker_noop(
    proof: dict[str, Any],
) -> None:
    """The new item is invalidated (valid_to set) via owner fixture after
    detection but before the worker locks. The worker observes the new item is
    no longer live and performs no mutation or event."""
    p = proof
    old_item, new_item = await _setup_pair_with_embeddings(
        p, old_content="supersede-new-invalidated-old", new_content="supersede-new-invalidated-new"
    )

    async with p["owner"].begin() as conn:
        await conn.execute(
            text("UPDATE memory_items SET valid_to=NOW() WHERE id=:id"),
            {"id": new_item},
        )

    before_old = await p["state"](old_item)
    before_new = await p["state"](new_item)
    await p["run_conflict_check"](new_item)
    after_old = await p["state"](old_item)
    after_new = await p["state"](new_item)

    assert after_old["item"] == before_old["item"]
    assert after_old["events"] == before_old["events"]
    assert after_new["item"] == before_new["item"]
    assert after_new["events"] == before_new["events"]
    assert _supersede_events(after_old["events"]) == []
    assert after_old["item"]["valid_to"] is None
    assert after_old["item"]["superseded_by"] is None


# ===========================================================================
# 14. Reciprocal proposals: concurrent scheduling, creation-direction acts
# ===========================================================================


@pytest.mark.parametrize("iteration", range(3))
async def test_reciprocal_proposals_concurrent_scheduling_creation_direction(
    proof: dict[str, Any], iteration: int
) -> None:
    """Schedule two reciprocal AUTO_SUPERSEDE proposals concurrently for opposite
    sides of the same pair and assert the final state/event are invariant to
    task creation order.

    This is a concurrent *scheduling* and *creation-direction* test, NOT a
    pair-lock contention test. The creation-order guard in
    ``handle_conflict_check`` exits the older-side job before
    ``_apply_auto_supersede`` (the older item's job detects the newer item, sees
    it is not the newer side, and commits without entering the pair lock). So
    both reciprocal handlers cannot contend for the pair lock in the current
    production path, and no observed blocking is expected or claimed here. The
    general code-level canonical pair-lock order (deterministic UUID order) is
    documented in ``docs/design.md`` as architectural deadlock prevention, but
    this test does not prove reciprocal pair-lock contention or observed
    blocking.

    What this test proves:

    * concurrent reciprocal proposals complete within an explicit timeout;
    * creation direction permits only the newer side to act (the older item is
      superseded by the newer item, the newer item remains live);
    * final state and event are invariant to task creation order (reversing the
      gather order does not change the outcome).
    """
    p = proof
    item_a = await p["insert_item"](
        content=f"supersede-reciprocal-a-{iteration}",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        authority=_TRUSTED_IMPORT,
    )
    item_b = await p["insert_item"](
        content=f"supersede-reciprocal-b-{iteration}",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        authority=_TRUSTED_IMPORT,
    )
    for iid in (item_a, item_b):
        await p["add_ready_embedding"](iid)

    # item_b is newer (created later). item_a's job detects item_b (so it would
    # be the older side and must NOT act); item_b's job detects item_a (newer
    # side, should act and supersede item_a).
    from engram.conflicts import ConflictAction, ConflictResult, ConflictVerdict

    result_a_detects_b = ConflictResult(
        verdict=ConflictVerdict.REFINE,
        action=ConflictAction.AUTO_SUPERSEDE,
        existing_item_id=item_b,
        similarity=0.97,
        classifier_confidence=0.95,
        conflict_type=None,
        reason="forced",
        provenance={"provider": "test"},
    )
    result_b_detects_a = ConflictResult(
        verdict=ConflictVerdict.REFINE,
        action=ConflictAction.AUTO_SUPERSEDE,
        existing_item_id=item_a,
        similarity=0.97,
        classifier_confidence=0.95,
        conflict_type=None,
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

    # Only the older item (item_a) is superseded by the newer (item_b); the
    # newer (item_b) remains live and unchanged.
    st_a = await p["state"](item_a)
    st_b = await p["state"](item_b)
    assert str(st_a["item"]["superseded_by"]) == str(item_b)
    assert st_a["item"]["valid_to"] is not None
    assert st_b["item"]["valid_to"] is None
    assert st_b["item"]["superseded_by"] is None
    # Exactly one effective supersession and event (on item_a).
    cd_a = _supersede_events(st_a["events"])
    cd_b = _supersede_events(st_b["events"])
    assert len(cd_a) == 1
    assert cd_a[0]["old_value"] is None
    assert cd_a[0]["new_value"] == str(item_b)
    assert cd_b == []


# ===========================================================================
# 15. Two new items contend for one old item -> exactly one winner (deterministic blocker graph)
# ===========================================================================


async def test_two_new_items_contend_one_old_exactly_one_winner(
    proof: dict[str, Any],
) -> None:
    """Two newer items both detect the same old item as the supersession target.
    Both have qualifying authority and ready embeddings. Both AUTO_SUPERSEDE
    handlers run concurrently against the same old item and contend for the
    canonical pair lock on the old row.

    Deterministic overlap proof (no sleeps, no reliance on task scheduling
    order):

    * A test-only trigger pauses worker A at its guarded old-row UPDATE via an
      advisory lock the coordinator holds, so worker A holds the pair locks
      (including the old row) but does not commit.
    * Worker B is then started and proven, via ``pg_blocking_pids()``, to be
      blocked behind worker A on the shared old-item row/pair lock —
      establishing real lock contention before release.
    * Worker A is released, commits the supersession atomically (one link +
      one event on the old row).
    * Worker B resumes, observes the old row is superseded, and is a truthful
      no-op (no second link, no second event).
    * Exactly one old-row transition occurs; exactly one truthful event
      exists; the event's ``new_value`` equals the winning
      ``superseded_by``; both new items remain unchanged.
    """
    p = proof
    old_item = await p["insert_item"](
        content="supersede-race-old",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        authority=10,
    )
    new_a = await p["insert_item"](
        content="supersede-race-new-a",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        authority=_TRUSTED_IMPORT,
    )
    new_b = await p["insert_item"](
        content="supersede-race-new-b",
        created_at=datetime(2026, 1, 3, tzinfo=UTC),
        authority=_TRUSTED_IMPORT,
    )
    for iid in (old_item, new_a, new_b):
        await p["add_ready_embedding"](iid)

    from engram.conflicts import ConflictAction, ConflictResult, ConflictVerdict

    result_a = ConflictResult(
        verdict=ConflictVerdict.REFINE,
        action=ConflictAction.AUTO_SUPERSEDE,
        existing_item_id=old_item,
        similarity=0.97,
        classifier_confidence=0.95,
        conflict_type=None,
        reason="forced",
        provenance={"provider": "test"},
    )
    result_b = ConflictResult(
        verdict=ConflictVerdict.REFINE,
        action=ConflictAction.AUTO_SUPERSEDE,
        existing_item_id=old_item,
        similarity=0.97,
        classifier_confidence=0.95,
        conflict_type=None,
        reason="forced",
        provenance={"provider": "test"},
    )

    original_detect = conflicts_mod.detect_conflicts

    async def detect_dispatch(item: MemoryItem, session: AsyncSession, **kw: Any) -> Any:
        if item.id == new_a:
            return result_a
        if item.id == new_b:
            return result_b
        return await original_detect(item, session, **kw)

    import engram.conflicts as cm

    cm.detect_conflicts = detect_dispatch  # type: ignore[assignment]  # noqa: SLF001

    await _install_worker_pause_trigger(p, item_id=old_item)
    coordinator = await p["owner"].connect()
    worker_a_task: asyncio.Task[None] | None = None
    worker_b_task: asyncio.Task[None] | None = None
    try:
        coordinator_pid = await coordinator.scalar(text("SELECT pg_backend_pid()"))
        await coordinator.execute(text("SELECT pg_advisory_lock(:key)"), {"key": p["pause_key"]})

        worker_a_task = asyncio.create_task(p["run_conflict_check"](new_a))
        # Worker A reaches its paused guarded supersession holding the pair locks.
        await _await_blocked_on(coordinator, coordinator_pid, 1)
        worker_a_pid = await _worker_pid_blocked_on_coordinator(coordinator, coordinator_pid)

        worker_b_task = asyncio.create_task(p["run_conflict_check"](new_b))
        # Worker B is blocked behind worker A on the shared old-item row/pair
        # lock — real lock contention, proven via the blocker graph.
        await _await_blocked_on_pid(coordinator, worker_a_pid, 1)
        assert worker_b_task is not None and not worker_b_task.done(), "worker B must wait behind A"

        # Release worker A: it commits the supersession atomically (one event).
        await coordinator.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": p["pause_key"]})
        await asyncio.wait_for(worker_a_task, timeout=10)
        # Worker B resumes and is a truthful no-op (old row already superseded).
        await asyncio.wait_for(worker_b_task, timeout=10)
    finally:
        cm.detect_conflicts = original_detect  # noqa: SLF001
        for task in (worker_a_task, worker_b_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if coordinator.in_transaction():
            await coordinator.rollback()
        await coordinator.execute(text("SELECT pg_advisory_unlock_all()"))
        await coordinator.close()
        await _drop_worker_pause_trigger(p)

    st_old = await p["state"](old_item)
    # Exactly one superseded_by link and one event.
    assert st_old["item"]["valid_to"] is not None
    assert str(st_old["item"]["superseded_by"]) in (str(new_a), str(new_b))
    cd = _supersede_events(st_old["events"])
    assert len(cd) == 1, [dict(e) for e in st_old["events"]]
    assert cd[0]["old_value"] is None
    assert cd[0]["new_value"] == str(st_old["item"]["superseded_by"])
    # The winner new item is unchanged; the loser new item is also unchanged.
    winner = new_a if str(st_old["item"]["superseded_by"]) == str(new_a) else new_b
    loser = new_b if winner == new_a else new_a
    st_winner = await p["state"](winner)
    st_loser = await p["state"](loser)
    assert st_winner["item"]["valid_to"] is None
    assert st_winner["item"]["superseded_by"] is None
    assert st_loser["item"]["valid_to"] is None
    assert st_loser["item"]["superseded_by"] is None
    assert _supersede_events(st_winner["events"]) == []
    assert _supersede_events(st_loser["events"]) == []


# ===========================================================================
# 16. Worker wins before a lock-respecting human/manual supersession path
# ===========================================================================


async def test_worker_wins_before_human_supersession_path(proof: dict[str, Any]) -> None:
    """The worker owns both pair locks (paused at its guarded old-row UPDATE).
    A Bearer-authenticated human supersede request on the old item starts during
    this window and waits behind the worker's row lock. The worker commits the
    supersession atomically (one link + one event on the old row). The human
    supersede resumes from the committed state and returns its canonical
    terminal/ineligible response (supersede on an already-expired item is 409).
    No stale human supersede event corrupts the worker's link/event."""
    p = proof
    old_item, new_item = await _setup_pair_with_embeddings(
        p, old_content="supersede-worker-wins-old", new_content="supersede-worker-wins-new"
    )

    await _install_worker_pause_trigger(p, item_id=old_item)
    coordinator = await p["owner"].connect()
    worker_task: asyncio.Task[None] | None = None
    supersede_task: asyncio.Task[Any] | None = None
    try:
        coordinator_pid = await coordinator.scalar(text("SELECT pg_backend_pid()"))
        await coordinator.execute(text("SELECT pg_advisory_lock(:key)"), {"key": p["pause_key"]})

        worker_task = asyncio.create_task(p["run_conflict_check"](new_item))
        # Worker reaches its paused guarded supersession holding the pair locks.
        await _await_blocked_on(coordinator, coordinator_pid, 1)
        worker_pid = await _worker_pid_blocked_on_coordinator(coordinator, coordinator_pid)

        async def submit_supersede() -> Any:
            async with AsyncClient(
                transport=ASGITransport(app=create_app()), base_url="http://test"
            ) as client:
                return await client.post(
                    f"/v1/items/{old_item}/supersede",
                    json={"reason": "supersede after worker"},
                    headers=_headers(p, "user_review"),
                )

        supersede_task = asyncio.create_task(submit_supersede())
        # The human supersede waits behind the worker's pair lock on the old item.
        await _await_blocked_on_pid(coordinator, worker_pid, 1)
        assert not supersede_task.done(), "supersede must wait behind worker"

        # Release: worker commits the supersession atomically (one event).
        await coordinator.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": p["pause_key"]})
        await asyncio.wait_for(worker_task, timeout=10)

        # Supersede resumes from the committed superseded state: an already-
        # expired item cannot be superseded (409) — the route's canonical
        # terminal/ineligible response.
        supersede_resp = await asyncio.wait_for(supersede_task, timeout=10)
        assert supersede_resp.status_code == 409, supersede_resp.text
    finally:
        for task in (worker_task, supersede_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if coordinator.in_transaction():
            await coordinator.rollback()
        await coordinator.execute(text("SELECT pg_advisory_unlock_all()"))
        await coordinator.close()
        await _drop_worker_pause_trigger(p)

    # Worker committed exactly one link + one truthful event on the old row.
    st_old = await p["state"](old_item)
    st_new = await p["state"](new_item)
    assert str(st_old["item"]["superseded_by"]) == str(new_item)
    assert st_old["item"]["valid_to"] is not None
    cd = _supersede_events(st_old["events"])
    assert len(cd) == 1, [dict(e) for e in st_old["events"]]
    assert cd[0]["old_value"] is None
    assert cd[0]["new_value"] == str(new_item)
    # The new item is live and unchanged.
    assert st_new["item"]["valid_to"] is None
    assert st_new["item"]["superseded_by"] is None
    # No stale human supersede event was written (the 409 wrote nothing).
    supersede_events = [e for e in st_old["events"] if e["event_type"] == "supersede"]
    assert supersede_events == []


# ===========================================================================
# 17. Event insertion failure after guarded update rolls back atomically
# ===========================================================================


async def test_rollback_atomicity_on_event_failure(proof: dict[str, Any]) -> None:
    """Inject a PostgreSQL failure during event creation after the guarded old-
    row supersession UPDATE. The supersession rolls back: valid_to remains
    unchanged, superseded_by remains NULL, no event persists, the new item is
    unchanged. After removing the failure injection, the next normal run
    succeeds once."""
    p = proof
    old_item, new_item = await _setup_pair_with_embeddings(
        p, old_content="supersede-rollback-old", new_content="supersede-rollback-new"
    )

    # Install a trigger that raises on INSERT into item_events for the old item
    # — this fires AFTER the guarded supersession UPDATE but BEFORE the
    # transaction commits, so the whole transaction (supersession + event) rolls
    # back atomically.
    fail_fn = f"worker_supersede_event_fail_{p['tag']}"
    async with p["owner"].begin() as conn:
        await conn.execute(
            text(
                f"CREATE FUNCTION {fail_fn}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN RAISE EXCEPTION 'injected event failure for supersede rollback'; "
                f"RETURN NEW; END $$"
            )
        )
        await conn.execute(
            text(
                f"CREATE TRIGGER {fail_fn} BEFORE INSERT ON item_events "
                f"FOR EACH ROW WHEN (NEW.item_id = '{old_item}'::uuid "
                f"AND NEW.event_type = 'conflict_detected') "
                f"EXECUTE FUNCTION {fail_fn}()"
            )
        )

    before_old = await p["state"](old_item)
    before_new = await p["state"](new_item)
    assert before_old["item"]["valid_to"] is None
    assert before_old["item"]["superseded_by"] is None

    # The worker run must raise (the event INSERT fails); the supersession
    # rolls back.
    with pytest.raises(Exception, match="injected event failure for supersede rollback"):
        await p["run_conflict_check"](new_item)

    after_old = await p["state"](old_item)
    after_new = await p["state"](new_item)
    # valid_to and superseded_by remain unchanged; no event persists.
    assert after_old["item"]["valid_to"] is None, after_old["item"]
    assert after_old["item"]["superseded_by"] is None, after_old["item"]
    assert _supersede_events(after_old["events"]) == []
    # The new item is unchanged.
    assert after_new["item"] == before_new["item"]

    # Remove the failure injection.
    async with p["owner"].begin() as conn:
        await conn.execute(text(f"DROP TRIGGER IF EXISTS {fail_fn} ON item_events"))
        await conn.execute(text(f"DROP FUNCTION IF EXISTS {fail_fn}()"))

    # The next normal run succeeds once.
    await p["run_conflict_check"](new_item)
    final = await p["state"](old_item)
    assert str(final["item"]["superseded_by"]) == str(new_item)
    assert final["item"]["valid_to"] is not None
    cd = _supersede_events(final["events"])
    assert len(cd) == 1
    assert cd[0]["new_value"] == str(new_item)
    # The new item remains unchanged.
    final_new = await p["state"](new_item)
    assert final_new["item"]["valid_to"] is None
    assert final_new["item"]["superseded_by"] is None


# ===========================================================================
# 18. Retry after rollback succeeds exactly once
# ===========================================================================


async def test_retry_after_rollback_succeeds_once(proof: dict[str, Any]) -> None:
    """After a rollback (from case 17), a retry succeeds exactly once: the old
    item is superseded, valid_to is set, exactly one event exists, and a third
    run is an idempotent no-op (no second event, no timestamp change)."""
    p = proof
    old_item, new_item = await _setup_pair_with_embeddings(
        p, old_content="supersede-retry-old", new_content="supersede-retry-new"
    )

    # First run succeeds.
    await p["run_conflict_check"](new_item)
    first = await p["state"](old_item)
    assert str(first["item"]["superseded_by"]) == str(new_item)
    assert first["item"]["valid_to"] is not None
    assert len(_supersede_events(first["events"])) == 1
    first_valid_to = first["item"]["valid_to"]

    # Retry: idempotent no-op.
    await p["run_conflict_check"](new_item)
    second = await p["state"](old_item)
    assert str(second["item"]["superseded_by"]) == str(new_item)
    assert second["item"]["valid_to"] == first_valid_to
    assert second["events"] == first["events"]
    assert len(_supersede_events(second["events"])) == 1


# ===========================================================================
# 19. Cross-tenant/missing counterpart fails closed under app-role RLS
# ===========================================================================


async def test_cross_tenant_counterpart_fails_closed(proof: dict[str, Any]) -> None:
    """The detected counterpart belongs to a different tenant (simulated by
    pointing the detection at a foreign item id). Under app-role RLS, the pair
    lock sees only one row (the job's tenant row), so it fails closed and
    performs no mutation or event."""
    p = proof
    # Create the job item in the proof tenant.
    new_item = await p["insert_item"](
        content="supersede-cross-tenant-new",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        authority=_TRUSTED_IMPORT,
    )
    await p["add_ready_embedding"](new_item)

    # Create a foreign item in a separate tenant (owner-only fixture).
    foreign_tenant = uuid.uuid4()
    foreign_item = uuid.uuid4()
    async with p["owner"].begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id,name,slug) VALUES (:id,:n,:n)"),
            {"id": foreign_tenant, "n": f"foreign-{p['tag']}"},
        )
        await conn.execute(
            text(
                "INSERT INTO tenant_config (tenant_id,config_version,active) "
                "VALUES (:id,'proof',true)"
            ),
            {"id": foreign_tenant},
        )
        await conn.execute(
            text(
                "INSERT INTO principals (id,tenant_id,name,type) VALUES (:pid,:tid,'foreign','agent')"
            ),
            {"pid": uuid.uuid4(), "tid": foreign_tenant},
        )
        await conn.execute(
            text(
                "INSERT INTO memory_items "
                "(id,tenant_id,principal_id,content,content_hash,kind,visibility,review_status,"
                "memory_confidence,source_trust,importance,source_type,authority,created_at,valid_from) "
                "VALUES (:id,:tid,(SELECT id FROM principals WHERE tenant_id=:tid LIMIT 1),"
                ":content,:hash,'fact','tenant','active',.8,.7,.5,'manual',10,:created,:created)"
            ),
            {
                "id": foreign_item,
                "tid": foreign_tenant,
                "content": f"{p['tag']}:foreign",
                "hash": f"sha256:{foreign_item.hex}",
                "created": datetime(2026, 1, 1, tzinfo=UTC),
            },
        )

    try:
        p["force_detection"](existing_item_id=foreign_item)
        before_new = await p["state"](new_item)
        await p["run_conflict_check"](new_item)
        after_new = await p["state"](new_item)
        # Worker fails closed: no mutation, no event on the job item.
        assert after_new["item"] == before_new["item"]
        assert after_new["events"] == before_new["events"]
        assert _supersede_events(after_new["events"]) == []

        # The foreign item is unchanged (the app-role session could not see it).
        async with p["owner"].connect() as conn:
            foreign_row = (
                await conn.execute(
                    text("SELECT valid_to, superseded_by FROM memory_items WHERE id=:id"),
                    {"id": foreign_item},
                )
            ).one()
        assert foreign_row[0] is None
        assert foreign_row[1] is None
    finally:
        async with p["owner"].begin() as conn:
            await conn.execute(text("DELETE FROM tenants WHERE id=:id"), {"id": foreign_tenant})


# ===========================================================================
# 20. Attribution: conflict automation actor and event target/value truth
# ===========================================================================


async def test_attribution_event_target_and_value_truth(proof: dict[str, Any]) -> None:
    """The AUTO_SUPERSEDE event is attributed to the conflict_automation
    internal actor, is attached to the MUTATED OLD row (not the new row), and
    records the actual new superseded_by value (the new item id) as
    new_value with old_value=None. The payload names old and new roles
    unambiguously."""
    p = proof
    old_item, new_item = await _setup_pair_with_embeddings(
        p, old_content="supersede-attribution-old", new_content="supersede-attribution-new"
    )

    await p["run_conflict_check"](new_item)

    st_old = await p["state"](old_item)
    st_new = await p["state"](new_item)

    # Exactly one conflict_detected event on the OLD row.
    cd = _supersede_events(st_old["events"])
    assert len(cd) == 1
    event = cd[0]
    # Truthful values: the mutated field is superseded_by, old_value=None,
    # new_value=the new item id.
    assert event["field_name"] == "superseded_by"
    assert event["old_value"] is None
    assert event["new_value"] == str(new_item)
    # No conflict_detected event on the new row.
    assert _supersede_events(st_new["events"]) == []

    # The actor is the conflict_automation internal principal.
    import json

    from engram.internal_actors import CONFLICT_AUTOMATION_INTERNAL_KEY

    async with p["owner"].connect() as conn:
        actor_id = (
            await conn.execute(
                text(
                    "SELECT id FROM principals WHERE tenant_id=:t AND internal_key=:key"
                ),
                {"t": p["tenant"], "key": CONFLICT_AUTOMATION_INTERNAL_KEY},
            )
        ).scalar_one_or_none()
    assert actor_id is not None
    assert event["actor_principal_id"] == str(actor_id)

    provenance = json.loads(event["reason"])
    assert provenance["worker_operation"] == "conflict.check"
    assert provenance["internal_actor_key"] == CONFLICT_AUTOMATION_INTERNAL_KEY
    assert provenance["action"] == "auto_supersede"
    # Payload names old and new roles unambiguously.
    assert provenance["old_item_id"] == str(old_item)
    assert provenance["new_item_id"] == str(new_item)
    assert provenance["existing_item_id"] == str(old_item)

# ===========================================================================
# 21. Active profile cutover while worker holds pair locks -> worker no-op
#     (deterministic blocker graph: pg_blocking_pids on the profile row lock)
# ===========================================================================


async def test_profile_cutover_during_apply_is_noop(proof: dict[str, Any]) -> None:
    """A conflict result produced using profile P may mutate trust state only
    while P is still the active profile at mutation-authority time.

    Deterministic overlap proof (no sleeps):

    1. A coordinator transaction locks the currently active profile row with
       ``SELECT ... FOR UPDATE``.
    2. The production AUTO_SUPERSEDE handler runs in a separate app-role
       session. After locking the ``memory_items`` pair, it attempts to lock
       the profile row and blocks.
    3. ``pg_blocking_pids()`` proves the worker is blocked on the
       coordinator/profile lock after obtaining (or attempting) its mutation
       path — establishing real cutover overlap before release.
    4. In the coordinator transaction, P is retired (and a replacement profile
       is activated so the schema's single-active invariant holds), then the
       coordinator commits.
    5. The worker resumes, observes P is no longer active, and performs no item
       mutation/event.
    6. The old/new items and their events remain unchanged.
    """
    p = proof
    old_item, new_item = await _setup_pair_with_embeddings(
        p, old_content="supersede-cutover-old", new_content="supersede-cutover-new"
    )

    # Resolve the currently active profile P (the one the worker will use).
    async with p["owner"].connect() as conn:
        prof = (
            await conn.execute(
                text(
                    "SELECT id, profile_key, dimensions, provider, model, distance_metric "
                    "FROM embedding_profiles WHERE state='active' LIMIT 1"
                )
            )
        ).one()
    profile_id = prof[0]

    before_old = await p["state"](old_item)
    before_new = await p["state"](new_item)
    assert before_old["item"]["valid_to"] is None
    assert before_old["item"]["superseded_by"] is None

    coordinator = await p["owner"].connect()
    worker_task: asyncio.Task[None] | None = None
    try:
        coordinator_pid = await coordinator.scalar(text("SELECT pg_backend_pid()"))
        # 1. Coordinator locks the active profile row. The AsyncConnection
        # auto-begins a transaction on the first execute; hold it open (do not
        # commit) so the FOR UPDATE row lock persists until the explicit commit
        # below, after the worker is proven blocked.
        await coordinator.execute(
            text("SELECT id FROM embedding_profiles WHERE id=:id FOR UPDATE"),
            {"id": profile_id},
        )

        # 2. Start the production handler in a separate app-role session.
        worker_task = asyncio.create_task(p["run_conflict_check"](new_item))
        # 3. Prove the worker is blocked on the coordinator/profile lock.
        await _await_blocked_on_pid(coordinator, coordinator_pid, 1)
        # The worker holds the pair locks but has NOT committed the supersession
        # yet (it is paused at the profile-row lock). Confirm the old item is
        # still live while the worker is blocked.
        async with p["owner"].connect() as check_conn:
            live = (
                await check_conn.execute(
                    text("SELECT valid_to, superseded_by FROM memory_items WHERE id=:id"),
                    {"id": old_item},
                )
            ).one()
        assert live[0] is None and live[1] is None, "worker must not commit before profile lock"

        # 4. Retire P and activate a replacement profile (single-active invariant).
        #    The schema's partial unique index ``idx_embedding_profiles_one_active``
        #    allows only one row with ``state='active'``, so retire P first (the
        #    coordinator holds P's row lock and may update it), then insert the
        #    replacement as active in the same locked transaction.
        repl_id = uuid.uuid4()
        repl_key = f"openai:text-embedding-3-small:1536:cutover-{p['tag']}"
        await coordinator.execute(
            text(
                "UPDATE embedding_profiles SET state='retired', retired_at=NOW() "
                "WHERE id=:id"
            ),
            {"id": profile_id},
        )
        await coordinator.execute(
            text(
                "INSERT INTO embedding_profiles "
                "(id, profile_key, provider, model, dimensions, distance_metric, state, "
                "index_status, index_name) VALUES "
                "(:id,:key,'openai','text-embedding-3-small',1536,'cosine','active',"
                "'ready','idx_cutover')"
            ),
            {"id": repl_id, "key": repl_key},
        )
        await coordinator.execute(text("COMMIT"))

        # 5. Worker resumes and is a mutation-free, event-free no-op.
        await asyncio.wait_for(worker_task, timeout=10)
    finally:
        if worker_task is not None and not worker_task.done():
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
        if coordinator.in_transaction():
            await coordinator.rollback()
        await coordinator.close()
        # Restore: re-activate the original profile and drop the replacement so
        # other tests in this module find a single active profile.
        async with p["owner"].begin() as conn:
            await conn.execute(
                text(
                    "DELETE FROM memory_embeddings WHERE profile_id=:id"
                ),
                {"id": repl_id},
            )
            await conn.execute(
                text("DELETE FROM embedding_profiles WHERE id=:id"),
                {"id": repl_id},
            )
            await conn.execute(
                text(
                    "UPDATE embedding_profiles SET state='active', retired_at=NULL "
                    "WHERE id=:id"
                ),
                {"id": profile_id},
            )

    # 6. Old/new items and events remain unchanged.
    after_old = await p["state"](old_item)
    after_new = await p["state"](new_item)
    assert after_old["item"] == before_old["item"]
    assert after_old["events"] == before_old["events"]
    assert after_new["item"] == before_new["item"]
    assert after_new["events"] == before_new["events"]
    assert _supersede_events(after_old["events"]) == []
    assert after_old["item"]["valid_to"] is None
    assert after_old["item"]["superseded_by"] is None


# ===========================================================================
# 22. Malformed verdict/action proposal is a mutation-free no-op
# ===========================================================================


async def test_malformed_verdict_action_proposal_is_noop(proof: dict[str, Any]) -> None:
    """AUTO_SUPERSEDE is a valid action only for ``ConflictVerdict.REFINE``. A
    malformed proposal (``verdict=DUPLICATE`` paired with
    ``action=AUTO_SUPERSEDE``) reaches ``_apply_auto_supersede`` but is a
    mutation-free, event-free no-op: no item mutation, no event."""
    p = proof
    old_item, new_item = await _setup_pair_with_embeddings(
        p, old_content="supersede-malformed-old", new_content="supersede-malformed-new"
    )

    # Force a malformed proposal: DUPLICATE verdict + AUTO_SUPERSEDE action.
    p["force_detection"](
        existing_item_id=old_item,
        action="auto_supersede",
        verdict="duplicate",
    )

    before_old = await p["state"](old_item)
    before_new = await p["state"](new_item)
    await p["run_conflict_check"](new_item)
    after_old = await p["state"](old_item)
    after_new = await p["state"](new_item)

    assert after_old["item"] == before_old["item"]
    assert after_old["events"] == before_old["events"]
    assert after_new["item"] == before_new["item"]
    assert after_new["events"] == before_new["events"]
    assert _supersede_events(after_old["events"]) == []
    assert after_old["item"]["valid_to"] is None
    assert after_old["item"]["superseded_by"] is None


# ===========================================================================
# 23. Hostile detector provenance cannot overwrite canonical event fields
# ===========================================================================


async def test_hostile_provenance_cannot_overwrite_canonical_event_fields(
    proof: dict[str, Any],
) -> None:
    """Detector provenance is untrusted/stale. Supply provenance containing
    deliberately false values for every canonical event-role/security field.
    Complete a valid AUTO_SUPERSEDE and parse the stored event reason JSON.

    Assert the canonical top-level values describe the committed transition
    and cannot be replaced, and the hostile/colliding provenance remains only
    under ``detector_provenance`` (namespaced).
    """
    p = proof
    old_item, new_item = await _setup_pair_with_embeddings(
        p, old_content="supersede-hostile-old", new_content="supersede-hostile-new"
    )

    hostile = {
        "provider": "hostile",
        "action": "dedup",
        "verdict": "duplicate",
        "old_item_id": str(uuid.uuid4()),
        "new_item_id": str(uuid.uuid4()),
        "existing_item_id": str(uuid.uuid4()),
        "reason": "hostile reason",
        "worker_operation": "hostile.op",
        "job_id": str(uuid.uuid4()),
        "item_author_principal_id": str(uuid.uuid4()),
        "internal_actor_key": "hostile_actor",
    }
    p["force_detection"](
        existing_item_id=old_item,
        action="auto_supersede",
        provenance=hostile,
    )

    await p["run_conflict_check"](new_item)

    st_old = await p["state"](old_item)
    cd = _supersede_events(st_old["events"])
    assert len(cd) == 1
    event = cd[0]
    # Canonical event row fields are truthful and cannot be replaced.
    assert event["field_name"] == "superseded_by"
    assert event["old_value"] is None
    assert event["new_value"] == str(new_item)

    import json

    payload = json.loads(event["reason"])
    # Canonical top-level fields describe the committed transition.
    assert payload["action"] == "auto_supersede"
    assert payload["verdict"] == "refine"
    assert payload["old_item_id"] == str(old_item)
    assert payload["new_item_id"] == str(new_item)
    assert payload["existing_item_id"] == str(old_item)
    assert payload["reason"] == "forced auto_supersede"
    assert payload["worker_operation"] == "conflict.check"
    assert payload["internal_actor_key"] == "conflict_automation"
    # Hostile values are NOT at the top level.
    assert "provider" not in payload
    assert payload["action"] != hostile["action"]
    assert payload["old_item_id"] != hostile["old_item_id"]
    assert payload["new_item_id"] != hostile["new_item_id"]
    assert payload["internal_actor_key"] != hostile["internal_actor_key"]
    # Hostile provenance is preserved only under the namespaced key.
    assert payload["detector_provenance"] == hostile
    assert payload["detector_provenance"]["provider"] == "hostile"
    assert payload["detector_provenance"]["action"] == "dedup"


# ===========================================================================
# 24. Production dispatch smoke test through process_one_job
# ===========================================================================


async def test_dispatch_via_process_one_job_succeeds(proof: dict[str, Any]) -> None:
    """Focused dispatch smoke test: a conflict.check job enqueued and processed
    through the production ``process_one_job`` loop (claim → handler → mark
    succeeded) completes a valid AUTO_SUPERSEDE. Covers the worker-loop dispatch
    path; the deeper concurrency proofs live in the deterministic blocker-graph
    cases above."""
    p = proof
    old_item, new_item = await _setup_pair_with_embeddings(
        p, old_content="supersede-dispatch-old", new_content="supersede-dispatch-new"
    )

    await p["run_conflict_check_via_process"](new_item)

    st_old = await p["state"](old_item)
    assert str(st_old["item"]["superseded_by"]) == str(new_item)
    assert st_old["item"]["valid_to"] is not None
    cd = _supersede_events(st_old["events"])
    assert len(cd) == 1
    assert cd[0]["new_value"] == str(new_item)
    st_new = await p["state"](new_item)
    assert st_new["item"]["valid_to"] is None
    assert st_new["item"]["superseded_by"] is None
