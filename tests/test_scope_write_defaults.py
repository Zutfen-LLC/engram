# ruff: noqa: E501
"""Real-Postgres, real-API-key coverage for ENG-SCOPE-001 write-scope
invariants: safe defaults, workspace authorization, admin bypass, and the
classify -> remember membership-revocation window.

Every request here authenticates with a genuine ``eng_...`` bearer token
(never a GUC-context override), because admin-scope evaluation
(``Principal.has_scope("admin")``) lives in the real auth resolver — a
session-override bypass would make every caller admin and hide exactly the
authorization gap this slice closes. Mirrors the corpus-fixture pattern in
tests/test_review_kg_integrity.py.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
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


def _owner_url() -> str | None:
    import os

    return os.getenv("ENGRAM_OWNER_DATABASE_URL") or os.getenv("ENGRAM_DATABASE_URL")


def _app_url() -> str | None:
    import os

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
    ids: dict[str, Any] = {"tag": tag, "owner": owner}
    ids["ta"], ids["tb"] = uuid.uuid4(), uuid.uuid4()
    ids["alpha"], ids["beta"] = uuid.uuid4(), uuid.uuid4()
    principals = {
        "member": (uuid.uuid4(), "user", ["read", "write"]),
        "outsider": (uuid.uuid4(), "user", ["read", "write"]),
        "admin": (uuid.uuid4(), "admin", ["read", "write", "admin"]),
        "other_tenant_admin": (uuid.uuid4(), "admin", ["read", "write", "admin"]),
    }
    ids["principals"] = {name: value[0] for name, value in principals.items()}
    ids["keys"] = {}

    async with owner.begin() as conn:
        for tid, label in ((ids["ta"], "a"), (ids["tb"], "b")):
            await conn.execute(
                text("INSERT INTO tenants (id,name,slug) VALUES (:id,:n,:s)"),
                {"id": tid, "n": f"scope-wd-{label}-{tag}", "s": f"scope-wd-{label}-{tag}"},
            )
            await conn.execute(
                text("INSERT INTO tenant_config (tenant_id,config_version,active) VALUES (:id,'v1',true)"),
                {"id": tid},
            )
        await conn.execute(
            text("INSERT INTO workspaces (id,tenant_id,name,slug) VALUES (:id,:tid,:s,:s)"),
            {"id": ids["alpha"], "tid": ids["ta"], "s": f"alpha-{tag}"},
        )
        await conn.execute(
            text("INSERT INTO workspaces (id,tenant_id,name,slug) VALUES (:id,:tid,:s,:s)"),
            {"id": ids["beta"], "tid": ids["tb"], "s": f"beta-{tag}"},
        )
        for name, (pid, ptype, _scopes) in principals.items():
            tid = ids["tb"] if name == "other_tenant_admin" else ids["ta"]
            await conn.execute(
                text("INSERT INTO principals (id,tenant_id,name,type) VALUES (:id,:tid,:n,:t)"),
                {"id": pid, "tid": tid, "n": f"{name}-{tag}", "t": ptype},
            )
        await conn.execute(
            text("INSERT INTO workspace_members (id,workspace_id,principal_id) VALUES (:id,:wid,:pid)"),
            {"id": uuid.uuid4(), "wid": ids["alpha"], "pid": ids["principals"]["member"]},
        )
        for name, (pid, _ptype, scopes) in principals.items():
            token = generate_api_key()
            parsed = parse_api_key(token)
            assert parsed.key_id
            ids["keys"][name] = token
            tid = ids["tb"] if name == "other_tenant_admin" else ids["ta"]
            await conn.execute(
                text(
                    "INSERT INTO api_keys (id,tenant_id,principal_id,key_id,secret_digest,"
                    "digest_algorithm,scopes,label) VALUES (:id,:tid,:pid,:kid,:digest,:algorithm,:scopes,:label)"
                ),
                {
                    "id": uuid.uuid4(),
                    "tid": tid,
                    "pid": pid,
                    "kid": parsed.key_id,
                    "digest": digest_api_key_secret(parsed.secret),
                    "algorithm": DIGEST_ALGORITHM,
                    "scopes": scopes,
                    "label": f"scope-wd-{tag}-{name}",
                },
            )

    import engram.db as db_module

    monkeypatch.setattr(db_module, "async_session_factory", app_factory)
    monkeypatch.setattr(db_module, "read_session_factory", app_factory)
    monkeypatch.setattr(db_module, "owner_session_factory", owner_factory)
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "embedding_provider", "none")
    reset_principal_cache()
    ids["client"] = AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")
    try:
        yield ids
    finally:
        await ids["client"].aclose()
        async with owner.begin() as conn:
            await conn.execute(text("DELETE FROM api_keys WHERE label LIKE :p"), {"p": f"scope-wd-{tag}-%"})
            await conn.execute(
                text(
                    "DELETE FROM item_events WHERE actor_principal_id IN "
                    "(SELECT id FROM principals WHERE tenant_id IN (:a,:b))"
                ),
                {"a": ids["ta"], "b": ids["tb"]},
            )
            await conn.execute(text("DELETE FROM tenants WHERE id IN (:a,:b)"), {"a": ids["ta"], "b": ids["tb"]})
        await owner.dispose()
        await app_engine.dispose()


def _h(c: dict[str, Any], actor: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {c['keys'][actor]}"}


async def _stored(c: dict[str, Any], item_id: str) -> dict[str, Any]:
    async with c["owner"].connect() as conn:
        row = (
            await conn.execute(
                text("SELECT visibility, workspace_id FROM memory_items WHERE id = :id"), {"id": item_id}
            )
        ).mappings().one()
    return dict(row)


async def _counts(c: dict[str, Any]) -> tuple[int, int, int]:
    async with c["owner"].connect() as conn:
        items = await conn.scalar(
            text("SELECT count(*) FROM memory_items WHERE tenant_id = :tid"), {"tid": c["ta"]}
        )
        ingests = await conn.scalar(
            text("SELECT count(*) FROM candidate_ingests WHERE tenant_id = :tid"), {"tid": c["ta"]}
        )
        receipts = await conn.scalar(
            text("SELECT count(*) FROM classification_runs WHERE tenant_id = :tid"), {"tid": c["ta"]}
        )
    return items, ingests, receipts


# ---- Safe defaults ----


async def test_omitted_visibility_and_workspace_is_private(corpus):
    c = corpus
    resp = await c["client"].post(
        "/v1/remember", json={"content": "bare fact"}, headers=_h(c, "member")
    )
    assert resp.status_code == 201, resp.text
    stored = await _stored(c, resp.json()["id"])
    assert stored["visibility"] == "private"
    assert stored["workspace_id"] is None


async def test_json_null_visibility_and_omitted_workspace_is_private(corpus):
    c = corpus
    resp = await c["client"].post(
        "/v1/remember",
        json={"content": "explicit json null visibility", "visibility": None},
        headers=_h(c, "member"),
    )
    assert resp.status_code == 201, resp.text
    stored = await _stored(c, resp.json()["id"])
    assert stored["visibility"] == "private"


async def test_omitted_visibility_with_authorized_workspace_is_workspace(corpus):
    c = corpus
    resp = await c["client"].post(
        "/v1/remember",
        json={"content": "workspace default fact", "workspace": f"alpha-{c['tag']}"},
        headers=_h(c, "member"),
    )
    assert resp.status_code == 201, resp.text
    stored = await _stored(c, resp.json()["id"])
    assert stored["visibility"] == "workspace"
    assert str(stored["workspace_id"]) == str(c["alpha"])


@pytest.mark.parametrize("visibility", ["private", "tenant", "public"])
async def test_explicit_non_workspace_visibility_needs_no_workspace(corpus, visibility):
    c = corpus
    resp = await c["client"].post(
        "/v1/remember",
        json={"content": f"explicit {visibility} fact", "visibility": visibility},
        headers=_h(c, "member"),
    )
    assert resp.status_code == 201, resp.text
    stored = await _stored(c, resp.json()["id"])
    assert stored["visibility"] == visibility


async def test_invalid_visibility_is_422(corpus):
    c = corpus
    resp = await c["client"].post(
        "/v1/remember",
        json={"content": "bad visibility", "visibility": "shared-with-everyone"},
        headers=_h(c, "member"),
    )
    assert resp.status_code == 422


async def test_explicit_workspace_visibility_without_workspace_is_422_and_creates_nothing(corpus):
    c = corpus
    before = await _counts(c)
    resp = await c["client"].post(
        "/v1/remember",
        json={"content": "workspace visibility no workspace", "visibility": "workspace"},
        headers=_h(c, "member"),
    )
    assert resp.status_code == 422
    after = await _counts(c)
    assert after == before


# ---- Workspace authorization ----


async def test_member_can_classify_and_write_into_workspace(corpus):
    c = corpus
    slug = f"alpha-{c['tag']}"
    classified = await c["client"].post(
        "/v1/classify", json={"content": "member workspace write", "workspace": slug}, headers=_h(c, "member")
    )
    assert classified.status_code == 200, classified.text
    resp = await c["client"].post(
        "/v1/remember",
        json={
            "content": "member workspace write",
            "workspace": slug,
            "classification_run_id": classified.json()["classification_run_id"],
        },
        headers=_h(c, "member"),
    )
    assert resp.status_code == 201, resp.text
    stored = await _stored(c, resp.json()["id"])
    assert stored["visibility"] == "workspace"


async def test_non_member_gets_non_disclosing_404_on_classify_and_remember(corpus):
    c = corpus
    slug = f"alpha-{c['tag']}"
    classify_resp = await c["client"].post(
        "/v1/classify", json={"content": "outsider attempt", "workspace": slug}, headers=_h(c, "outsider")
    )
    assert classify_resp.status_code == 404
    remember_resp = await c["client"].post(
        "/v1/remember",
        json={"content": "outsider attempt", "workspace": slug},
        headers=_h(c, "outsider"),
    )
    assert remember_resp.status_code == 404
    assert remember_resp.json()["detail"] == classify_resp.json()["detail"]


async def test_unknown_workspace_gets_same_404_as_non_member(corpus):
    c = corpus
    unknown_resp = await c["client"].post(
        "/v1/remember",
        json={"content": "unknown workspace", "workspace": f"does-not-exist-{c['tag']}"},
        headers=_h(c, "outsider"),
    )
    nonmember_resp = await c["client"].post(
        "/v1/remember",
        json={"content": "nonmember workspace", "workspace": f"alpha-{c['tag']}"},
        headers=_h(c, "outsider"),
    )
    assert unknown_resp.status_code == nonmember_resp.status_code == 404
    assert unknown_resp.json()["detail"] == nonmember_resp.json()["detail"]


async def test_admin_scope_writes_into_same_tenant_workspace_without_membership(corpus):
    c = corpus
    slug = f"alpha-{c['tag']}"
    resp = await c["client"].post(
        "/v1/remember",
        json={"content": "admin bypass write", "workspace": slug},
        headers=_h(c, "admin"),
    )
    assert resp.status_code == 201, resp.text
    stored = await _stored(c, resp.json()["id"])
    assert stored["visibility"] == "workspace"
    assert str(stored["workspace_id"]) == str(c["alpha"])


async def test_admin_scope_cannot_cross_tenant(corpus):
    c = corpus
    beta_slug = f"beta-{c['tag']}"
    classify_resp = await c["client"].post(
        "/v1/classify", json={"content": "cross tenant admin", "workspace": beta_slug}, headers=_h(c, "admin")
    )
    assert classify_resp.status_code == 404
    remember_resp = await c["client"].post(
        "/v1/remember",
        json={"content": "cross tenant admin", "workspace": beta_slug},
        headers=_h(c, "admin"),
    )
    assert remember_resp.status_code == 404


async def test_direct_private_write_available_without_workspace_membership(corpus):
    c = corpus
    resp = await c["client"].post(
        "/v1/remember",
        json={"content": "outsider private write", "visibility": "private"},
        headers=_h(c, "outsider"),
    )
    assert resp.status_code == 201, resp.text
    stored = await _stored(c, resp.json()["id"])
    assert stored["visibility"] == "private"


# ---- classify -> remember membership-revocation window ----


async def test_membership_revoked_between_classify_and_remember_blocks_consumption(corpus):
    c = corpus
    slug = f"alpha-{c['tag']}"
    classified = await c["client"].post(
        "/v1/classify", json={"content": "revoked membership", "workspace": slug}, headers=_h(c, "member")
    )
    assert classified.status_code == 200, classified.text

    async with c["owner"].begin() as conn:
        await conn.execute(
            text("DELETE FROM workspace_members WHERE workspace_id = :wid AND principal_id = :pid"),
            {"wid": c["alpha"], "pid": c["principals"]["member"]},
        )

    resp = await c["client"].post(
        "/v1/remember",
        json={
            "content": "revoked membership",
            "workspace": slug,
            "classification_run_id": classified.json()["classification_run_id"],
        },
        headers=_h(c, "member"),
    )
    assert resp.status_code == 404

    async with c["owner"].connect() as conn:
        run = (
            await conn.execute(
                text("SELECT memory_item_id, bound_at FROM classification_runs WHERE id = :id"),
                {"id": classified.json()["classification_run_id"]},
            )
        ).one()
    assert tuple(run) == (None, None)
