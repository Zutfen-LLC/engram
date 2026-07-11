# ruff: noqa: E501
"""Real-Postgres coverage for V2-BL-004 scope enforcement (ticket sections C-G).

Exercises the *real* API-key resolver end to end — a plaintext `eng_...`
bearer token authenticated over HTTP, never a `get_session` override — since
scope enforcement lives in `get_current_principal` + `ScopeGuard`, which a
GUC-override bypass (as used by test_trusted_actor.py's `_make_admin_client`)
would skip entirely. SQLite-only tests are insufficient proof here (the ticket
is explicit about this) because `api_keys.scopes` is a real `TEXT[]` column
and RLS-dependent route handlers only run against Postgres.

Requires a live PostgreSQL with the v2 schema; skips automatically when no DB
is reachable (mirrors tests/test_trusted_actor.py).

Sections:
  C. Representative per-scope HTTP matrix (+ F: admin super-scope).
  D. Conditional review-transition matrix.
  E. Eligibility-vs-scope ordering (404 before scope-403).
  G. Existing-key / historical-scope compatibility.
  Plus: auth-disabled regression (dev-mode default principal still works).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.api.app import create_app
from engram.auth import (
    DIGEST_ALGORITHM,
    digest_api_key_secret,
    generate_api_key,
    parse_api_key,
    reset_principal_cache,
)
from engram.config import settings

_test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_test_session_factory = async_sessionmaker(
    _test_engine, class_=AsyncSession, expire_on_commit=False
)

_NAME_PREFIX = "v2bl4-enf-"


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
    reset_principal_cache()
    async with _test_engine.begin() as conn:
        # recall_logs/kg_triples/memory_items.principal_id have no ON DELETE
        # CASCADE, so they must be cleared before principals — otherwise a
        # principal created by a test in this module leaves a dangling
        # FK-blocked row that fails the next test's cleanup. memory_items is
        # deleted by principal ownership (a subquery against principals),
        # not by a content-prefix filter: e.g. POST /v1/diary stores its
        # `entry` field verbatim as memory_items.content, and
        # test_write_scope_matrix posts entry="x" — a content-prefix filter
        # misses that row entirely and leaves it dangling.
        await conn.execute(text("DELETE FROM item_events"))
        await conn.execute(text("DELETE FROM recall_logs"))
        await conn.execute(text("DELETE FROM kg_triples"))
        await conn.execute(
            text(
                "DELETE FROM memory_items WHERE principal_id IN ("
                "SELECT id FROM principals WHERE tenant_id = (SELECT id FROM tenants WHERE slug = 'default') "
                f"AND name LIKE '{_NAME_PREFIX}%')"
            )
        )
        await conn.execute(
            text("DELETE FROM api_keys WHERE label LIKE :prefix"),
            {"prefix": f"{_NAME_PREFIX}%"},
        )
        await conn.execute(
            text(
                "DELETE FROM principals WHERE tenant_id = (SELECT id FROM tenants WHERE slug = 'default') "
                f"AND name LIKE '{_NAME_PREFIX}%'"
            )
        )


# ===========================================================================
# Seeding helpers
# ===========================================================================


async def _default_tenant_principal() -> tuple[str, str]:
    async with _test_session_factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT t.id::text AS tenant_id, p.id::text AS principal_id "
                        "FROM tenants t "
                        "JOIN principals p ON p.tenant_id = t.id AND p.name = 'admin' "
                        "WHERE t.slug = 'default'"
                    )
                )
            )
            .mappings()
            .one()
        )
    return str(row["tenant_id"]), str(row["principal_id"])


async def _seed_principal(tenant_id: str, name: str, ptype: str) -> str:
    principal_id = str(uuid.uuid4())
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


def _pg_array(scopes: list[str]) -> str:
    # Safe here: scopes are always drawn from this test module's own fixed
    # vocabulary, never external/user input.
    return "{" + ",".join(scopes) + "}"


async def _issue_key(
    *, tenant_id: str, principal_id: str, scopes: list[str], label: str
) -> str:
    """Insert a real new-format API key row and return its plaintext."""
    plaintext = generate_api_key()
    parsed = parse_api_key(plaintext)
    assert parsed.key_id is not None
    digest = digest_api_key_secret(parsed.secret)
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO api_keys (id, tenant_id, principal_id, key_hash, key_id, "
                "secret_digest, digest_algorithm, scopes, label, created_at, revoked_at) "
                f"VALUES (:id, :tid, :pid, NULL, :kid, :sd, :da, '{_pg_array(scopes)}', :lbl, now(), NULL)"
            ),
            {
                "id": str(uuid.uuid4()),
                "tid": tenant_id,
                "pid": principal_id,
                "kid": parsed.key_id,
                "sd": digest,
                "da": DIGEST_ALGORITHM,
                "lbl": label,
            },
        )
        await session.commit()
    return plaintext


async def _insert_item(
    *,
    tenant_id: str,
    principal_id: str,
    content: str,
    review_status: str = "active",
    visibility: str = "workspace",
    human_verified: bool = False,
) -> str:
    item_id = str(uuid.uuid4())
    created_at = datetime.now(UTC) - timedelta(hours=1)
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO memory_items ("
                "id, tenant_id, principal_id, content, content_hash, kind, "
                "visibility, review_status, memory_confidence, source_trust, "
                "importance, source_type, human_verified, created_at, valid_from"
                ") VALUES ("
                ":id, :tenant_id, :principal_id, :content, :content_hash, 'fact', "
                ":visibility, :review_status, 0.9, 0.5, "
                "0.5, 'manual', :human_verified, :created_at, :created_at"
                ")"
            ),
            {
                "id": item_id,
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "content": content,
                "content_hash": f"sha256:{uuid.uuid4().hex}",
                "visibility": visibility,
                "review_status": review_status,
                "human_verified": human_verified,
                "created_at": created_at,
            },
        )
        await session.commit()
    return item_id


async def _mark_conflict(item_id: str, counterpart_id: str) -> None:
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "UPDATE memory_items SET conflicts_with_item_id = :cp, "
                "conflict_type = 'contradiction', conflict_resolution_status = 'unresolved' "
                "WHERE id = :id"
            ),
            {"id": item_id, "cp": counterpart_id},
        )
        await session.commit()


async def _write_diary_row(tenant_id: str, principal_id: str, content: str) -> None:
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO memory_items ("
                "id, tenant_id, principal_id, content, content_hash, kind, "
                "visibility, review_status, memory_confidence, source_trust, "
                "importance, source_type, created_at, valid_from"
                ") VALUES ("
                ":id, :tenant_id, :principal_id, :content, :content_hash, 'diary_entry', "
                "'private', 'active', 0.7, 0.7, 0.4, 'manual', now(), now()"
                ")"
            ),
            {
                "id": str(uuid.uuid4()),
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "content": content,
                "content_hash": f"sha256:{uuid.uuid4().hex}",
            },
        )
        await session.commit()


async def _make_client(monkeypatch: pytest.MonkeyPatch, *, auth_enabled: bool = True) -> AsyncClient:
    """A real, unmodified app — auth flows entirely through the bearer token.

    Only the DB session *factories* are monkeypatched (to point at this
    module's test engine, avoiding asyncpg cross-event-loop errors); unlike
    test_trusted_actor.py's `_make_admin_client`, `get_session` itself is
    NOT overridden, so `get_current_principal` and `ScopeGuard` run for real.
    """
    settings.auth_enabled = auth_enabled
    app = create_app()
    import engram.db as db_module

    monkeypatch.setattr(db_module, "async_session_factory", _test_session_factory)
    monkeypatch.setattr(db_module, "owner_session_factory", _test_session_factory)
    monkeypatch.setattr(db_module, "read_session_factory", _test_session_factory)
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ===========================================================================
# C / F. Representative per-scope HTTP matrix (admin super-scope folded in)
# ===========================================================================


async def test_read_scope_matrix(monkeypatch: pytest.MonkeyPatch):
    if not await _db_ok():
        _require_db()
    tenant_id, _ = await _default_tenant_principal()
    pid = await _seed_principal(tenant_id, f"{_NAME_PREFIX}read-agent", "agent")
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=pid, content=f"{_NAME_PREFIX}read-item"
    )
    await _write_diary_row(tenant_id, pid, f"{_NAME_PREFIX}diary")
    diary_name = f"{_NAME_PREFIX}read-agent"
    token = await _issue_key(
        tenant_id=tenant_id, principal_id=pid, scopes=["read"], label=f"{_NAME_PREFIX}read"
    )
    client = await _make_client(monkeypatch)
    async with client:
        h = _auth(token)
        # May call:
        assert (await client.post("/v1/recall", json={}, headers=h)).status_code == 200
        assert (
            await client.post("/v1/search", json={"query": "x"}, headers=h)
        ).status_code == 200
        assert (await client.get("/v1/items", headers=h)).status_code == 200
        assert (await client.get(f"/v1/items/{item_id}", headers=h)).status_code == 200
        assert (await client.get("/v1/taxonomy", headers=h)).status_code == 200
        assert (
            await client.get("/v1/kg/query", params={"entity": "x"}, headers=h)
        ).status_code == 200
        assert (await client.get(f"/v1/diary/{diary_name}", headers=h)).status_code == 200
        assert (
            await client.post("/v1/classify", json={"content": "hello world"}, headers=h)
        ).status_code == 200
        # May not call:
        assert (
            await client.post("/v1/remember", json={"content": "x"}, headers=h)
        ).status_code == 403
        assert (
            await client.post(
                "/v1/feedback", json={"item_id": item_id, "feedback": "useful"}, headers=h
            )
        ).status_code == 403
        assert (await client.patch(f"/v1/items/{item_id}", json={}, headers=h)).status_code == 403
        assert (
            await client.post(
                "/v1/kg", json={"subject": "a", "predicate": "b", "object": "c"}, headers=h
            )
        ).status_code == 403
        assert (
            await client.post(
                "/v1/diary", json={"entry": "x", "principal": diary_name}, headers=h
            )
        ).status_code == 403
        assert (await client.get("/v1/review/conflicts", headers=h)).status_code == 403
        assert (
            await client.post(f"/v1/items/{item_id}/verify", json={}, headers=h)
        ).status_code == 403
        assert (await client.get("/v1/export/cca", headers=h)).status_code == 403
        assert (
            await client.get("/v1/admin/principals", params={"tenant_id": tenant_id}, headers=h)
        ).status_code == 403


async def test_write_scope_matrix(monkeypatch: pytest.MonkeyPatch):
    if not await _db_ok():
        _require_db()
    tenant_id, _ = await _default_tenant_principal()
    pid = await _seed_principal(tenant_id, f"{_NAME_PREFIX}write-agent", "agent")
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=pid, content=f"{_NAME_PREFIX}write-item"
    )
    item_id_2 = await _insert_item(
        tenant_id=tenant_id, principal_id=pid, content=f"{_NAME_PREFIX}write-item-2"
    )
    item_id_3 = await _insert_item(
        tenant_id=tenant_id, principal_id=pid, content=f"{_NAME_PREFIX}write-item-3"
    )
    token = await _issue_key(
        tenant_id=tenant_id, principal_id=pid, scopes=["write"], label=f"{_NAME_PREFIX}write"
    )
    client = await _make_client(monkeypatch)
    async with client:
        h = _auth(token)
        # May call:
        assert (
            await client.post(
                "/v1/remember", json={"content": f"{_NAME_PREFIX}new"}, headers=h
            )
        ).status_code == 201
        assert (
            await client.post(
                "/v1/feedback", json={"item_id": item_id, "feedback": "useful"}, headers=h
            )
        ).status_code == 201
        assert (await client.patch(f"/v1/items/{item_id}", json={}, headers=h)).status_code == 200
        assert (
            await client.post(f"/v1/items/{item_id_2}/supersede", json={}, headers=h)
        ).status_code == 200
        assert (
            await client.post(f"/v1/items/{item_id_3}/invalidate", json={}, headers=h)
        ).status_code == 200
        assert (
            await client.post(
                "/v1/kg", json={"subject": "a", "predicate": "b", "object": "c"}, headers=h
            )
        ).status_code == 201
        assert (
            await client.post(
                "/v1/kg/invalidate",
                json={"subject": "a", "predicate": "b", "object": "c"},
                headers=h,
            )
        ).status_code == 200
        assert (
            await client.post(
                "/v1/diary",
                json={"entry": "x", "principal": f"{_NAME_PREFIX}write-agent"},
                headers=h,
            )
        ).status_code == 201
        assert (
            await client.post(
                "/v1/tunnels",
                json={"source_wing": "a", "target_wing": "b"},
                headers=h,
            )
        ).status_code == 201
        # May not call:
        assert (await client.get("/v1/items", headers=h)).status_code == 403
        assert (await client.get("/v1/export/cca", headers=h)).status_code == 403
        assert (await client.get("/v1/review/conflicts", headers=h)).status_code == 403
        assert (
            await client.post(f"/v1/items/{item_id}/verify", json={}, headers=h)
        ).status_code == 403
        assert (
            await client.post(
                f"/v1/items/{item_id}/resolve-conflict",
                json={"resolution": "accepted"},
                headers=h,
            )
        ).status_code == 403
        assert (
            await client.post("/v1/items/bulk-archive", json={"item_ids": []}, headers=h)
        ).status_code == 403
        assert (
            await client.get("/v1/admin/principals", params={"tenant_id": tenant_id}, headers=h)
        ).status_code == 403


async def test_review_scope_matrix(monkeypatch: pytest.MonkeyPatch):
    if not await _db_ok():
        _require_db()
    tenant_id, _ = await _default_tenant_principal()
    # The review-scoped credential belongs to a `user`-type principal so the
    # principal-type policy (evaluate_transition/can_human_verify), which is
    # orthogonal to scope, also permits the privileged actions below.
    uid = await _seed_principal(tenant_id, f"{_NAME_PREFIX}review-user", "user")
    author_id = await _seed_principal(tenant_id, f"{_NAME_PREFIX}review-author", "agent")
    proposed_item = await _insert_item(
        tenant_id=tenant_id,
        principal_id=author_id,
        content=f"{_NAME_PREFIX}review-proposed",
        review_status="proposed",
    )
    verify_item = await _insert_item(
        tenant_id=tenant_id, principal_id=author_id, content=f"{_NAME_PREFIX}review-verify"
    )
    conflict_a = await _insert_item(
        tenant_id=tenant_id, principal_id=author_id, content=f"{_NAME_PREFIX}conflict-a"
    )
    conflict_b = await _insert_item(
        tenant_id=tenant_id, principal_id=author_id, content=f"{_NAME_PREFIX}conflict-b"
    )
    await _mark_conflict(conflict_a, conflict_b)
    archive_item = await _insert_item(
        tenant_id=tenant_id,
        principal_id=author_id,
        content=f"{_NAME_PREFIX}bulk-archive",
        review_status="proposed",
    )
    token = await _issue_key(
        tenant_id=tenant_id, principal_id=uid, scopes=["review"], label=f"{_NAME_PREFIX}review"
    )
    client = await _make_client(monkeypatch)
    async with client:
        h = _auth(token)
        # May call:
        assert (await client.get("/v1/review/conflicts", headers=h)).status_code == 200
        assert (await client.get("/v1/review/stats", headers=h)).status_code == 200
        assert (
            await client.post(
                f"/v1/items/{proposed_item}/review",
                json={"review_status": "active"},
                headers=h,
            )
        ).status_code == 200
        assert (
            await client.post(f"/v1/items/{verify_item}/verify", json={}, headers=h)
        ).status_code == 200
        assert (
            await client.post(
                f"/v1/items/{conflict_a}/resolve-conflict",
                json={"resolution": "accepted"},
                headers=h,
            )
        ).status_code == 200
        assert (
            await client.post(
                "/v1/items/bulk-archive", json={"item_ids": [archive_item]}, headers=h
            )
        ).status_code == 200
        # May not call:
        assert (await client.get("/v1/items", headers=h)).status_code == 403
        assert (await client.post("/v1/recall", json={}, headers=h)).status_code == 403
        assert (
            await client.post("/v1/remember", json={"content": "x"}, headers=h)
        ).status_code == 403
        assert (await client.get("/v1/export/cca", headers=h)).status_code == 403
        assert (
            await client.get("/v1/admin/principals", params={"tenant_id": tenant_id}, headers=h)
        ).status_code == 403


async def test_export_scope_matrix(monkeypatch: pytest.MonkeyPatch):
    if not await _db_ok():
        _require_db()
    tenant_id, _ = await _default_tenant_principal()
    pid = await _seed_principal(tenant_id, f"{_NAME_PREFIX}export-agent", "agent")
    token = await _issue_key(
        tenant_id=tenant_id, principal_id=pid, scopes=["export"], label=f"{_NAME_PREFIX}export"
    )
    client = await _make_client(monkeypatch)
    async with client:
        h = _auth(token)
        assert (await client.get("/v1/export/cca", headers=h)).status_code == 200
        assert (await client.get("/v1/items", headers=h)).status_code == 403
        assert (await client.post("/v1/recall", json={}, headers=h)).status_code == 403
        assert (
            await client.post("/v1/remember", json={"content": "x"}, headers=h)
        ).status_code == 403
        assert (await client.get("/v1/review/conflicts", headers=h)).status_code == 403
        assert (
            await client.get("/v1/admin/principals", params={"tenant_id": tenant_id}, headers=h)
        ).status_code == 403


async def test_admin_scope_satisfies_every_category(monkeypatch: pytest.MonkeyPatch):
    if not await _db_ok():
        _require_db()
    tenant_id, _ = await _default_tenant_principal()
    pid = await _seed_principal(tenant_id, f"{_NAME_PREFIX}admin-agent", "agent")
    item_id = await _insert_item(
        tenant_id=tenant_id, principal_id=pid, content=f"{_NAME_PREFIX}admin-item"
    )
    token = await _issue_key(
        tenant_id=tenant_id, principal_id=pid, scopes=["admin"], label=f"{_NAME_PREFIX}admin"
    )
    client = await _make_client(monkeypatch)
    async with client:
        h = _auth(token)
        assert (await client.get("/v1/items", headers=h)).status_code == 200
        assert (
            await client.post("/v1/remember", json={"content": f"{_NAME_PREFIX}x"}, headers=h)
        ).status_code == 201
        assert (await client.get("/v1/review/conflicts", headers=h)).status_code == 200
        assert (await client.get("/v1/export/cca", headers=h)).status_code == 200
        assert (
            await client.get("/v1/admin/principals", params={"tenant_id": tenant_id}, headers=h)
        ).status_code == 200
        # Item eligibility / principal-type rules still apply even to an
        # admin-scoped credential — admin scope doesn't imply admin principal
        # type for review-transition purposes; verify this specific item is
        # still reachable and correctly gated.
        assert (await client.get(f"/v1/items/{item_id}", headers=h)).status_code == 200


async def test_empty_scope_key_gets_403_everywhere_except_health(monkeypatch: pytest.MonkeyPatch):
    if not await _db_ok():
        _require_db()
    tenant_id, _ = await _default_tenant_principal()
    pid = await _seed_principal(tenant_id, f"{_NAME_PREFIX}empty-agent", "agent")
    token = await _issue_key(
        tenant_id=tenant_id, principal_id=pid, scopes=[], label=f"{_NAME_PREFIX}empty"
    )
    client = await _make_client(monkeypatch)
    async with client:
        h = _auth(token)
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/ready")).status_code in (200, 503)  # DB-dependent, not scope
        assert (await client.get("/v1/items", headers=h)).status_code == 403
        assert (await client.post("/v1/recall", json={}, headers=h)).status_code == 403
        assert (
            await client.post("/v1/remember", json={"content": "x"}, headers=h)
        ).status_code == 403
        assert (await client.get("/v1/export/cca", headers=h)).status_code == 403
        assert (await client.get("/v1/review/conflicts", headers=h)).status_code == 403
        assert (
            await client.get("/v1/admin/principals", params={"tenant_id": tenant_id}, headers=h)
        ).status_code == 403


# ===========================================================================
# D. Conditional review-transition matrix
# ===========================================================================


async def test_write_scoped_agent_review_transitions(monkeypatch: pytest.MonkeyPatch):
    if not await _db_ok():
        _require_db()
    tenant_id, _ = await _default_tenant_principal()
    agent_id = await _seed_principal(tenant_id, f"{_NAME_PREFIX}wsa-agent", "agent")
    other_agent_id = await _seed_principal(tenant_id, f"{_NAME_PREFIX}wsa-other", "agent")
    token = await _issue_key(
        tenant_id=tenant_id, principal_id=agent_id, scopes=["write"], label=f"{_NAME_PREFIX}wsa"
    )
    client = await _make_client(monkeypatch)
    async with client:
        h = _auth(token)

        proposed = await _insert_item(
            tenant_id=tenant_id,
            principal_id=agent_id,
            content=f"{_NAME_PREFIX}wsa-proposed",
            review_status="proposed",
        )
        resp = await client.post(
            f"/v1/items/{proposed}/review", json={"review_status": "disputed"}, headers=h
        )
        assert resp.status_code == 200, resp.text

        active = await _insert_item(
            tenant_id=tenant_id,
            principal_id=agent_id,
            content=f"{_NAME_PREFIX}wsa-active",
            review_status="active",
        )
        resp = await client.post(
            f"/v1/items/{active}/review", json={"review_status": "disputed"}, headers=h
        )
        assert resp.status_code == 200, resp.text

        own_proposed = await _insert_item(
            tenant_id=tenant_id,
            principal_id=agent_id,
            content=f"{_NAME_PREFIX}wsa-own-proposed",
            review_status="proposed",
        )
        resp = await client.post(
            f"/v1/items/{own_proposed}/review", json={"review_status": "archived"}, headers=h
        )
        assert resp.status_code == 200, resp.text

        cannot_activate = await _insert_item(
            tenant_id=tenant_id,
            principal_id=agent_id,
            content=f"{_NAME_PREFIX}wsa-cannot-activate",
            review_status="proposed",
        )
        resp = await client.post(
            f"/v1/items/{cannot_activate}/review", json={"review_status": "active"}, headers=h
        )
        assert resp.status_code == 403, resp.text

        disputed_item = await _insert_item(
            tenant_id=tenant_id,
            principal_id=agent_id,
            content=f"{_NAME_PREFIX}wsa-disputed",
            review_status="disputed",
        )
        resp = await client.post(
            f"/v1/items/{disputed_item}/review", json={"review_status": "active"}, headers=h
        )
        assert resp.status_code == 403, resp.text  # reactivate

        rejected_item = await _insert_item(
            tenant_id=tenant_id,
            principal_id=agent_id,
            content=f"{_NAME_PREFIX}wsa-rejected-src",
            review_status="active",
        )
        resp = await client.post(
            f"/v1/items/{rejected_item}/review", json={"review_status": "rejected"}, headers=h
        )
        assert resp.status_code == 403, resp.text  # reject

        others_proposal = await _insert_item(
            tenant_id=tenant_id,
            principal_id=other_agent_id,
            content=f"{_NAME_PREFIX}wsa-others-proposal",
            review_status="proposed",
        )
        resp = await client.post(
            f"/v1/items/{others_proposal}/review", json={"review_status": "archived"}, headers=h
        )
        assert resp.status_code == 403, resp.text  # archive someone else's proposal


async def test_write_scoped_human_cannot_bypass_scope_via_principal_type(
    monkeypatch: pytest.MonkeyPatch,
):
    """Mandatory: principal type alone must not bypass the scope boundary."""
    if not await _db_ok():
        _require_db()
    tenant_id, _ = await _default_tenant_principal()
    user_id = await _seed_principal(tenant_id, f"{_NAME_PREFIX}wsh-user", "user")
    token = await _issue_key(
        tenant_id=tenant_id, principal_id=user_id, scopes=["write"], label=f"{_NAME_PREFIX}wsh"
    )
    client = await _make_client(monkeypatch)
    async with client:
        h = _auth(token)

        disputable = await _insert_item(
            tenant_id=tenant_id,
            principal_id=user_id,
            content=f"{_NAME_PREFIX}wsh-disputable",
            review_status="proposed",
        )
        resp = await client.post(
            f"/v1/items/{disputable}/review", json={"review_status": "disputed"}, headers=h
        )
        assert resp.status_code == 200, resp.text

        for target_status in ("active", "rejected"):
            proposed = await _insert_item(
                tenant_id=tenant_id,
                principal_id=user_id,
                content=f"{_NAME_PREFIX}wsh-{target_status}",
                review_status="proposed",
            )
            resp = await client.post(
                f"/v1/items/{proposed}/review",
                json={"review_status": target_status},
                headers=h,
            )
            # The principal-type matrix would allow a `user` to do this; the
            # scope boundary must still deny it since `review` is missing.
            assert resp.status_code == 403, resp.text

        archived_item = await _insert_item(
            tenant_id=tenant_id,
            principal_id=user_id,
            content=f"{_NAME_PREFIX}wsh-archive-privileged",
            review_status="active",
        )
        resp = await client.post(
            f"/v1/items/{archived_item}/review", json={"review_status": "archived"}, headers=h
        )
        assert resp.status_code == 403, resp.text


async def test_review_scoped_human_privileged_transitions(monkeypatch: pytest.MonkeyPatch):
    if not await _db_ok():
        _require_db()
    tenant_id, _ = await _default_tenant_principal()
    user_id = await _seed_principal(tenant_id, f"{_NAME_PREFIX}rsh-user", "user")
    token = await _issue_key(
        tenant_id=tenant_id, principal_id=user_id, scopes=["review"], label=f"{_NAME_PREFIX}rsh"
    )
    client = await _make_client(monkeypatch)
    async with client:
        h = _auth(token)

        proposed = await _insert_item(
            tenant_id=tenant_id,
            principal_id=user_id,
            content=f"{_NAME_PREFIX}rsh-activate",
            review_status="proposed",
        )
        assert (
            await client.post(
                f"/v1/items/{proposed}/review", json={"review_status": "active"}, headers=h
            )
        ).status_code == 200

        disputed = await _insert_item(
            tenant_id=tenant_id,
            principal_id=user_id,
            content=f"{_NAME_PREFIX}rsh-reactivate",
            review_status="disputed",
        )
        assert (
            await client.post(
                f"/v1/items/{disputed}/review", json={"review_status": "active"}, headers=h
            )
        ).status_code == 200

        active = await _insert_item(
            tenant_id=tenant_id,
            principal_id=user_id,
            content=f"{_NAME_PREFIX}rsh-reject",
            review_status="active",
        )
        assert (
            await client.post(
                f"/v1/items/{active}/review", json={"review_status": "rejected"}, headers=h
            )
        ).status_code == 200

        to_archive = await _insert_item(
            tenant_id=tenant_id,
            principal_id=user_id,
            content=f"{_NAME_PREFIX}rsh-archive",
            review_status="active",
        )
        assert (
            await client.post(
                f"/v1/items/{to_archive}/review", json={"review_status": "archived"}, headers=h
            )
        ).status_code == 200

        # Disputing is classified as a `write`-level action (see
        # review_policy._WRITE_PERMITTED_TRANSITIONS), not a `review`-level
        # one — a review-only credential lacks `write` and is denied here,
        # even though every review-gated transition above succeeded.
        to_dispute = await _insert_item(
            tenant_id=tenant_id,
            principal_id=user_id,
            content=f"{_NAME_PREFIX}rsh-dispute",
            review_status="proposed",
        )
        assert (
            await client.post(
                f"/v1/items/{to_dispute}/review", json={"review_status": "disputed"}, headers=h
            )
        ).status_code == 403


async def test_review_scoped_agent_still_denied_by_principal_type(
    monkeypatch: pytest.MonkeyPatch,
):
    if not await _db_ok():
        _require_db()
    tenant_id, _ = await _default_tenant_principal()
    agent_id = await _seed_principal(tenant_id, f"{_NAME_PREFIX}rsa-agent", "agent")
    token = await _issue_key(
        tenant_id=tenant_id, principal_id=agent_id, scopes=["review"], label=f"{_NAME_PREFIX}rsa"
    )
    client = await _make_client(monkeypatch)
    async with client:
        h = _auth(token)

        proposed = await _insert_item(
            tenant_id=tenant_id,
            principal_id=agent_id,
            content=f"{_NAME_PREFIX}rsa-activate",
            review_status="proposed",
        )
        # Scope admission succeeds (no 403 for lacking scope) but the
        # agent principal-type policy still forbids activation.
        resp = await client.post(
            f"/v1/items/{proposed}/review", json={"review_status": "active"}, headers=h
        )
        assert resp.status_code == 403, resp.text

        # Disputing needs `write` scope (see review_policy), which this
        # review-only credential lacks — denied on the scope gate, before
        # principal-type policy is ever consulted.
        to_dispute = await _insert_item(
            tenant_id=tenant_id,
            principal_id=agent_id,
            content=f"{_NAME_PREFIX}rsa-dispute",
            review_status="proposed",
        )
        resp = await client.post(
            f"/v1/items/{to_dispute}/review", json={"review_status": "disputed"}, headers=h
        )
        assert resp.status_code == 403, resp.text


async def test_write_and_review_user_can_do_both(monkeypatch: pytest.MonkeyPatch):
    if not await _db_ok():
        _require_db()
    tenant_id, _ = await _default_tenant_principal()
    user_id = await _seed_principal(tenant_id, f"{_NAME_PREFIX}wru-user", "user")
    token = await _issue_key(
        tenant_id=tenant_id,
        principal_id=user_id,
        scopes=["write", "review"],
        label=f"{_NAME_PREFIX}wru",
    )
    client = await _make_client(monkeypatch)
    async with client:
        h = _auth(token)

        disputable = await _insert_item(
            tenant_id=tenant_id,
            principal_id=user_id,
            content=f"{_NAME_PREFIX}wru-dispute",
            review_status="proposed",
        )
        assert (
            await client.post(
                f"/v1/items/{disputable}/review", json={"review_status": "disputed"}, headers=h
            )
        ).status_code == 200

        activatable = await _insert_item(
            tenant_id=tenant_id,
            principal_id=user_id,
            content=f"{_NAME_PREFIX}wru-activate",
            review_status="proposed",
        )
        assert (
            await client.post(
                f"/v1/items/{activatable}/review", json={"review_status": "active"}, headers=h
            )
        ).status_code == 200


async def test_admin_scoped_caller_all_scope_gates_pass(monkeypatch: pytest.MonkeyPatch):
    if not await _db_ok():
        _require_db()
    tenant_id, _ = await _default_tenant_principal()
    user_id = await _seed_principal(tenant_id, f"{_NAME_PREFIX}adm-user", "user")
    token = await _issue_key(
        tenant_id=tenant_id, principal_id=user_id, scopes=["admin"], label=f"{_NAME_PREFIX}adm"
    )
    client = await _make_client(monkeypatch)
    async with client:
        h = _auth(token)
        proposed = await _insert_item(
            tenant_id=tenant_id,
            principal_id=user_id,
            content=f"{_NAME_PREFIX}adm-activate",
            review_status="proposed",
        )
        assert (
            await client.post(
                f"/v1/items/{proposed}/review", json={"review_status": "active"}, headers=h
            )
        ).status_code == 200


async def test_noop_review_transition_write_and_review_ok_read_only_403(
    monkeypatch: pytest.MonkeyPatch,
):
    if not await _db_ok():
        _require_db()
    tenant_id, _ = await _default_tenant_principal()
    agent_id = await _seed_principal(tenant_id, f"{_NAME_PREFIX}noop-agent", "agent")
    write_token = await _issue_key(
        tenant_id=tenant_id, principal_id=agent_id, scopes=["write"], label=f"{_NAME_PREFIX}noopw"
    )
    review_token = await _issue_key(
        tenant_id=tenant_id,
        principal_id=agent_id,
        scopes=["review"],
        label=f"{_NAME_PREFIX}noopr",
    )
    read_token = await _issue_key(
        tenant_id=tenant_id, principal_id=agent_id, scopes=["read"], label=f"{_NAME_PREFIX}noopread"
    )
    client = await _make_client(monkeypatch)
    async with client:
        item_w = await _insert_item(
            tenant_id=tenant_id,
            principal_id=agent_id,
            content=f"{_NAME_PREFIX}noop-w",
            review_status="active",
        )
        resp = await client.post(
            f"/v1/items/{item_w}/review", json={"review_status": "active"}, headers=_auth(write_token)
        )
        assert resp.status_code == 200, resp.text

        item_r = await _insert_item(
            tenant_id=tenant_id,
            principal_id=agent_id,
            content=f"{_NAME_PREFIX}noop-r",
            review_status="active",
        )
        resp = await client.post(
            f"/v1/items/{item_r}/review",
            json={"review_status": "active"},
            headers=_auth(review_token),
        )
        assert resp.status_code == 200, resp.text

        item_read = await _insert_item(
            tenant_id=tenant_id,
            principal_id=agent_id,
            content=f"{_NAME_PREFIX}noop-read",
            review_status="active",
        )
        resp = await client.post(
            f"/v1/items/{item_read}/review",
            json={"review_status": "active"},
            headers=_auth(read_token),
        )
        assert resp.status_code == 403, resp.text  # route-level: no write/review/admin at all


# ===========================================================================
# E. Eligibility-vs-scope ordering
# ===========================================================================


async def test_review_endpoint_404_before_403_for_inaccessible_item(
    monkeypatch: pytest.MonkeyPatch,
):
    if not await _db_ok():
        _require_db()
    tenant_id, _ = await _default_tenant_principal()
    agent_id = await _seed_principal(tenant_id, f"{_NAME_PREFIX}ord-agent", "agent")
    token = await _issue_key(
        tenant_id=tenant_id, principal_id=agent_id, scopes=["write"], label=f"{_NAME_PREFIX}ord"
    )
    client = await _make_client(monkeypatch)
    async with client:
        h = _auth(token)
        missing_id = str(uuid.uuid4())
        resp = await client.post(
            f"/v1/items/{missing_id}/review", json={"review_status": "active"}, headers=h
        )
        assert resp.status_code == 404, resp.text

        accessible = await _insert_item(
            tenant_id=tenant_id,
            principal_id=agent_id,
            content=f"{_NAME_PREFIX}ord-accessible",
            review_status="proposed",
        )
        resp = await client.post(
            f"/v1/items/{accessible}/review", json={"review_status": "active"}, headers=h
        )
        assert resp.status_code == 403, resp.text  # write scope, but activate needs review


async def test_ordinary_route_missing_scope_403_before_404(monkeypatch: pytest.MonkeyPatch):
    if not await _db_ok():
        _require_db()
    tenant_id, _ = await _default_tenant_principal()
    pid = await _seed_principal(tenant_id, f"{_NAME_PREFIX}ord2-agent", "agent")
    token = await _issue_key(
        tenant_id=tenant_id, principal_id=pid, scopes=["write"], label=f"{_NAME_PREFIX}ord2"
    )
    client = await _make_client(monkeypatch)
    async with client:
        h = _auth(token)
        missing_id = str(uuid.uuid4())
        # write-scoped key hitting a read-gated route for a nonexistent item:
        # must be 403 (missing scope), not 404 — the handler body (which
        # would resolve eligibility) never runs.
        resp = await client.get(f"/v1/items/{missing_id}", headers=h)
        assert resp.status_code == 403, resp.text


# ===========================================================================
# G. Existing-key / historical-scope compatibility
# ===========================================================================


async def test_historical_unknown_scope_string_is_inert_not_authoritative(
    monkeypatch: pytest.MonkeyPatch,
):
    if not await _db_ok():
        _require_db()
    tenant_id, _ = await _default_tenant_principal()
    pid = await _seed_principal(tenant_id, f"{_NAME_PREFIX}legacy-agent", "agent")
    # Historical row: one valid scope alongside one that predates validation.
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO api_keys (id, tenant_id, principal_id, key_hash, key_id, "
                "secret_digest, digest_algorithm, scopes, label, created_at, revoked_at) "
                "VALUES (:id, :tid, :pid, NULL, :kid, :sd, :da, '{read,unknown_legacy_scope}', :lbl, now(), NULL)"
            ),
            {
                "id": str(uuid.uuid4()),
                "tid": tenant_id,
                "pid": pid,
                "kid": (parsed := parse_api_key(plaintext := generate_api_key())).key_id,
                "sd": digest_api_key_secret(parsed.secret),
                "da": DIGEST_ALGORITHM,
                "lbl": f"{_NAME_PREFIX}legacy-mixed",
            },
        )
        await session.commit()
    client = await _make_client(monkeypatch)
    async with client:
        h = _auth(plaintext)
        # The valid `read` scope still works...
        assert (await client.get("/v1/items", headers=h)).status_code == 200
        # ...but the unknown scope confers no authority.
        assert (
            await client.post("/v1/remember", json={"content": "x"}, headers=h)
        ).status_code == 403


async def test_unknown_only_scope_row_authenticates_with_no_authority(
    monkeypatch: pytest.MonkeyPatch,
):
    if not await _db_ok():
        _require_db()
    tenant_id, _ = await _default_tenant_principal()
    pid = await _seed_principal(tenant_id, f"{_NAME_PREFIX}legacy-only-agent", "agent")
    async with _test_session_factory() as session:
        parsed = parse_api_key(plaintext := generate_api_key())
        await session.execute(
            text(
                "INSERT INTO api_keys (id, tenant_id, principal_id, key_hash, key_id, "
                "secret_digest, digest_algorithm, scopes, label, created_at, revoked_at) "
                "VALUES (:id, :tid, :pid, NULL, :kid, :sd, :da, '{unknown_legacy_scope}', :lbl, now(), NULL)"
            ),
            {
                "id": str(uuid.uuid4()),
                "tid": tenant_id,
                "pid": pid,
                "kid": parsed.key_id,
                "sd": digest_api_key_secret(parsed.secret),
                "da": DIGEST_ALGORITHM,
                "lbl": f"{_NAME_PREFIX}legacy-only",
            },
        )
        await session.commit()
    client = await _make_client(monkeypatch)
    async with client:
        h = _auth(plaintext)
        # Authenticates fine (no crash)...
        assert (await client.get("/health")).status_code == 200
        # ...but has no protected-route authority whatsoever.
        assert (await client.get("/v1/items", headers=h)).status_code == 403
        assert (
            await client.post("/v1/remember", json={"content": "x"}, headers=h)
        ).status_code == 403


# ===========================================================================
# Auth-disabled regression — the super-scope must not require special-casing
# ===========================================================================


async def test_auth_disabled_default_principal_satisfies_review_and_admin(
    monkeypatch: pytest.MonkeyPatch,
):
    if not await _db_ok():
        _require_db()
    client = await _make_client(monkeypatch, auth_enabled=False)
    async with client:
        # No Authorization header at all — dev-mode default principal.
        assert (await client.get("/v1/review/conflicts")).status_code == 200
        tenant_id, _ = await _default_tenant_principal()
        assert (
            await client.get("/v1/admin/principals", params={"tenant_id": tenant_id})
        ).status_code == 200
