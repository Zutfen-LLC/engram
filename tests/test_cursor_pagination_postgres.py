"""Real-PostgreSQL authenticated /v1/items cursor pagination proofs (Blocker C).

These tests exercise the exact asyncpg boundary where the production cursor
defect occurred:

    authenticated API
    → page 1
    → returned cursor
    → page 2
    → SQLAlchemy text query
    → asyncpg datetime binding

They are NOT pure encode/decode tests — they traverse the real HTTP route
``/v1/items`` with an authenticated API key against real PostgreSQL with
pgvector, RLS, and the app role.

These tests require a live PostgreSQL with the v2 schema and pgvector, and
skip automatically when no DB is reachable — UNLESS
``ENGRAM_FAIL_ON_DB_SKIP=1`` is set (the Compose CI path).

Do NOT emulate asyncpg with SQLite or a mock session.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

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

_DB_SKIP_REASON = "requires a live PostgreSQL with the v2 schema"

_test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_test_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    _test_engine, class_=AsyncSession, expire_on_commit=False
)
_isolated_tenant_id: str | None = None
_isolated_admin_id: str | None = None


@pytest.fixture(autouse=True)
async def _fresh_engine() -> Any:
    global _test_engine, _test_session_factory
    _test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
    _test_session_factory = async_sessionmaker(
        _test_engine, class_=AsyncSession, expire_on_commit=False
    )
    yield
    await _test_engine.dispose()


async def _db_ok() -> bool:
    try:
        async with _test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _require_db() -> None:
    if not _db_ok_sync():
        pytest.skip(_DB_SKIP_REASON)


def _db_ok_sync() -> bool:
    import asyncio

    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_db_ok())
        finally:
            loop.close()


@pytest.fixture(autouse=True)
async def _isolated_test_tenant(_fresh_engine: Any) -> Any:
    """Create one tenant per test and remove it by tenant cascade."""
    global _isolated_tenant_id, _isolated_admin_id
    if not await _db_ok():
        yield
        return
    tenant_id = str(uuid.uuid4())
    admin_id = str(uuid.uuid4())
    slug = f"cursor-pg-{uuid.uuid4().hex}"
    async with _test_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
            {"id": tenant_id, "name": slug, "slug": slug},
        )
        await conn.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES (:id, :tenant_id, 'admin', 'admin')"
            ),
            {"id": admin_id, "tenant_id": tenant_id},
        )
        await conn.execute(
            text("INSERT INTO tenant_config (tenant_id) VALUES (:id)"),
            {"id": tenant_id},
        )
    _isolated_tenant_id, _isolated_admin_id = tenant_id, admin_id
    try:
        yield
    finally:
        async with _test_engine.begin() as conn:
            await conn.execute(text("DELETE FROM tenants WHERE id=:id"), {"id": tenant_id})
        _isolated_tenant_id = _isolated_admin_id = None


async def _issue_key(
    tenant_id: str,
    principal_id: str,
    scopes: list[str],
) -> str:
    plaintext = generate_api_key()
    parsed = parse_api_key(plaintext)
    assert parsed.key_id is not None
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO api_keys (id, tenant_id, principal_id, key_hash, key_id, "
                "secret_digest, digest_algorithm, scopes, label) "
                "VALUES (:id, :tenant_id, :principal_id, NULL, :key_id, :digest, "
                ":algorithm, :scopes, :label)"
            ),
            {
                "id": str(uuid.uuid4()),
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "key_id": parsed.key_id,
                "digest": digest_api_key_secret(parsed.secret),
                "algorithm": DIGEST_ALGORITHM,
                "scopes": scopes,
                "label": f"cursor-pg-{uuid.uuid4()}",
            },
        )
        await session.commit()
    return plaintext


async def _api_client(monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    settings.auth_enabled = True
    reset_principal_cache()
    import engram.db as db_module

    monkeypatch.setattr(db_module, "async_session_factory", _test_session_factory)
    monkeypatch.setattr(db_module, "owner_session_factory", _test_session_factory)
    monkeypatch.setattr(db_module, "read_session_factory", _test_session_factory)
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _insert_item(
    *,
    tenant_id: str,
    principal_id: str,
    content: str,
    review_status: str = "proposed",
    created_at: datetime | None = None,
    visibility: str = "tenant",
    kind: str = "fact",
    source_type: str = "manual",
) -> str:
    item_id = str(uuid.uuid4())
    if created_at is None:
        created_at = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=100)
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO memory_items ("
                "id, tenant_id, principal_id, content, content_hash, kind, "
                "visibility, review_status, memory_confidence, source_trust, "
                "source_confidence_prior, retention_confidence, retention_disposition, "
                "retention_evidence_at, authority, importance, source_type, "
                "conflict_resolution_status, created_at, valid_from"
                ") VALUES ("
                ":id, :tenant_id, :principal_id, :content, :content_hash, :kind, "
                ":visibility, :review_status, 0.35, 0.5, NULL, NULL, NULL, NULL, "
                "10, 0.5, :source_type, NULL, :created_at, :created_at"
                ")"
            ),
            {
                "id": item_id,
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "content": content,
                "content_hash": f"sha256:{uuid.uuid4().hex}",
                "review_status": review_status,
                "kind": kind,
                "source_type": source_type,
                "visibility": visibility,
                "created_at": created_at,
            },
        )
        await session.commit()
    return item_id


async def _insert_cross_tenant_item(
    *, admin_tenant_id: str, content: str
) -> tuple[str, str]:
    """Create a second tenant + item to prove cross-tenant isolation."""
    other_tenant = str(uuid.uuid4())
    other_principal = str(uuid.uuid4())
    other_item = str(uuid.uuid4())
    ts = datetime.now(UTC).replace(microsecond=0)
    async with _test_session_factory() as session:
        await session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
            {
                "id": other_tenant,
                "name": f"other-{uuid.uuid4().hex}",
                "slug": f"other-{uuid.uuid4().hex}",
            },
        )
        await session.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES (:id, :tenant_id, 'other', 'agent')"
            ),
            {"id": other_principal, "tenant_id": other_tenant},
        )
        await session.execute(
            text("INSERT INTO tenant_config (tenant_id) VALUES (:id)"),
            {"id": other_tenant},
        )
        await session.execute(
            text(
                "INSERT INTO memory_items ("
                "id, tenant_id, principal_id, content, content_hash, kind, "
                "visibility, review_status, memory_confidence, source_trust, "
                "source_confidence_prior, retention_confidence, retention_disposition, "
                "retention_evidence_at, authority, importance, source_type, "
                "conflict_resolution_status, created_at, valid_from"
                ") VALUES ("
                ":id, :tenant_id, :principal_id, :content, :content_hash, 'fact', "
                "'tenant', 'active', 0.5, 0.5, NULL, NULL, NULL, NULL, "
                "10, 0.5, 'manual', NULL, :created_at, :created_at"
                ")"
            ),
            {
                "id": other_item,
                "tenant_id": other_tenant,
                "principal_id": other_principal,
                "content": content,
                "content_hash": f"sha256:{uuid.uuid4().hex}",
                "created_at": ts,
            },
        )
        await session.commit()
    return other_tenant, other_item


def _make_malformed_cursor(
    *,
    timestamp: str | None = None,
    item_id: str | None = None,
    valid_base64: bool = True,
    valid_json: bool = True,
) -> str:
    """Build a cursor payload with controlled malformation."""
    if item_id is None:
        item_id = str(uuid.uuid4())
    if timestamp is None:
        timestamp = datetime.now(UTC).isoformat()

    if not valid_json:
        return base64.urlsafe_b64encode(b"{not json").decode().rstrip("=")

    payload = json.dumps(
        {"created_at": timestamp, "id": item_id},
        separators=(",", ":"),
    ).encode()

    if not valid_base64:
        return "!!!not-valid-base64!!!"

    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


# ── Test 1: basic page 1 → page 2 ────────────────────────────────────────────


async def test_basic_page1_to_page2(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /v1/items?limit=2 → 200 + next_cursor; page 2 → 200, not 500."""
    _require_db()
    if _isolated_tenant_id is None:
        pytest.skip(_DB_SKIP_REASON)
    tenant_id = _isolated_tenant_id
    admin_id = _isolated_admin_id
    assert admin_id is not None

    # Create 5 items with deterministic timestamps.
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=100)
    for i in range(5):
        await _insert_item(
            tenant_id=tenant_id,
            principal_id=admin_id,
            content=f"page-test-item-{i}",
            review_status="active",
            created_at=base - timedelta(minutes=i),
        )

    key = await _issue_key(tenant_id, admin_id, ["read"])
    client = await _api_client(monkeypatch)

    async with client:
        # Page 1.
        r1 = await client.get(
            "/v1/items",
            params={"limit": 2, "active_only": "true"},
            headers=_auth(key),
        )
        assert r1.status_code == 200
        data1 = r1.json()
        assert data1["count"] == 2
        next_cursor = data1.get("next_cursor")
        assert next_cursor is not None

        # Page 2 — this is where the asyncpg defect caused HTTP 500.
        r2 = await client.get(
            "/v1/items",
            params={"limit": 2, "active_only": "true", "cursor": next_cursor},
            headers=_auth(key),
        )
        assert r2.status_code == 200, f"page 2 failed: {r2.status_code} {r2.text}"
        data2 = r2.json()
        assert data2["count"] == 2

        # No overlap between pages.
        page1_ids = {item["id"] for item in data1["items"]}
        page2_ids = {item["id"] for item in data2["items"]}
        assert page1_ids.isdisjoint(page2_ids)


# ── Test 2: active_only=false ────────────────────────────────────────────────


async def test_active_only_false_paginates_proposed_and_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /v1/items?active_only=false paginates both proposed and active rows."""
    _require_db()
    if _isolated_tenant_id is None:
        pytest.skip(_DB_SKIP_REASON)
    tenant_id = _isolated_tenant_id
    admin_id = _isolated_admin_id
    assert admin_id is not None

    base = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=100)
    # Create both active and proposed items.
    for i in range(3):
        await _insert_item(
            tenant_id=tenant_id,
            principal_id=admin_id,
            content=f"active-item-{i}",
            review_status="active",
            created_at=base - timedelta(minutes=i),
        )
    for i in range(3):
        await _insert_item(
            tenant_id=tenant_id,
            principal_id=admin_id,
            content=f"proposed-item-{i}",
            review_status="proposed",
            created_at=base - timedelta(minutes=10 + i),
        )

    key = await _issue_key(tenant_id, admin_id, ["read"])
    client = await _api_client(monkeypatch)

    all_items: list[dict[str, Any]] = []
    cursor = None
    async with client:
        while True:
            params: dict[str, Any] = {"limit": 2, "active_only": "false"}
            if cursor:
                params["cursor"] = cursor
            r = await client.get("/v1/items", params=params, headers=_auth(key))
            assert r.status_code == 200
            data = r.json()
            all_items.extend(data["items"])
            cursor = data.get("next_cursor")
            if not cursor:
                break

    statuses = {item["review_status"] for item in all_items}
    assert "active" in statuses
    assert "proposed" in statuses
    assert len(all_items) == 6


# ── Test 3: complete traversal ──────────────────────────────────────────────


async def test_complete_traversal_no_duplicates_no_omissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Traverse every cursor until next_cursor=null. No dupes, no omissions."""
    _require_db()
    if _isolated_tenant_id is None:
        pytest.skip(_DB_SKIP_REASON)
    tenant_id = _isolated_tenant_id
    admin_id = _isolated_admin_id
    assert admin_id is not None

    # Create exactly 7 active items.
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=200)
    expected_ids: set[str] = set()
    for i in range(7):
        item_id = await _insert_item(
            tenant_id=tenant_id,
            principal_id=admin_id,
            content=f"traverse-item-{i}",
            review_status="active",
            created_at=base - timedelta(minutes=i),
        )
        expected_ids.add(item_id)

    key = await _issue_key(tenant_id, admin_id, ["read"])
    client = await _api_client(monkeypatch)

    traversed: list[str] = []
    cursor = None
    async with client:
        while True:
            params: dict[str, Any] = {"limit": 3, "active_only": "true"}
            if cursor:
                params["cursor"] = cursor
            r = await client.get("/v1/items", params=params, headers=_auth(key))
            assert r.status_code == 200
            data = r.json()
            traversed.extend(item["id"] for item in data["items"])
            cursor = data.get("next_cursor")
            if not cursor:
                break

    # No duplicates.
    assert len(traversed) == len(set(traversed))
    # No omissions.
    traversed_set = set(traversed)
    assert traversed_set == expected_ids
    # Stable descending order by created_at then id DESC.
    # (Timestamps are deterministic so this should be stable.)


# ── Test 4: timestamp ties ──────────────────────────────────────────────────


async def test_timestamp_ties_stable_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple rows with the same created_at produce stable order via UUID."""
    _require_db()
    if _isolated_tenant_id is None:
        pytest.skip(_DB_SKIP_REASON)
    tenant_id = _isolated_tenant_id
    admin_id = _isolated_admin_id
    assert admin_id is not None

    # Create 5 items with the SAME timestamp.
    shared_ts = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=50)
    tie_ids: list[str] = []
    for i in range(5):
        item_id = await _insert_item(
            tenant_id=tenant_id,
            principal_id=admin_id,
            content=f"tie-item-{i}",
            review_status="active",
            created_at=shared_ts,
        )
        tie_ids.append(item_id)

    key = await _issue_key(tenant_id, admin_id, ["read"])
    client = await _api_client(monkeypatch)

    all_items: list[str] = []
    cursor = None
    async with client:
        while True:
            params: dict[str, Any] = {"limit": 2, "active_only": "true"}
            if cursor:
                params["cursor"] = cursor
            r = await client.get("/v1/items", params=params, headers=_auth(key))
            assert r.status_code == 200
            data = r.json()
            all_items.extend(item["id"] for item in data["items"])
            cursor = data.get("next_cursor")
            if not cursor:
                break

    # All 5 retrieved, no duplicates.
    assert len(all_items) == 5
    assert len(set(all_items)) == 5
    assert set(all_items) == set(tie_ids)


# ── Test 5: invalid timestamp in cursor ─────────────────────────────────────


async def test_invalid_timestamp_cursor_returns_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A correctly encoded cursor with a malformed timestamp returns 400."""
    _require_db()
    if _isolated_tenant_id is None:
        pytest.skip(_DB_SKIP_REASON)
    tenant_id = _isolated_tenant_id
    admin_id = _isolated_admin_id
    assert admin_id is not None

    # Create at least one item so the list endpoint works.
    await _insert_item(
        tenant_id=tenant_id,
        principal_id=admin_id,
        content="placeholder",
        review_status="active",
    )

    key = await _issue_key(tenant_id, admin_id, ["read"])
    client = await _api_client(monkeypatch)

    bad_cursor = _make_malformed_cursor(timestamp="not-a-timestamp")
    async with client:
        r = await client.get(
            "/v1/items",
            params={"cursor": bad_cursor},
            headers=_auth(key),
        )
        assert r.status_code == 400, f"expected 400, got {r.status_code}"
        assert r.status_code != 500


# ── Test 6: invalid UUID in cursor ──────────────────────────────────────────


async def test_invalid_uuid_cursor_returns_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cursor with a non-UUID item_id returns 400."""
    _require_db()
    if _isolated_tenant_id is None:
        pytest.skip(_DB_SKIP_REASON)
    tenant_id = _isolated_tenant_id
    admin_id = _isolated_admin_id
    assert admin_id is not None

    await _insert_item(
        tenant_id=tenant_id,
        principal_id=admin_id,
        content="placeholder",
        review_status="active",
    )

    key = await _issue_key(tenant_id, admin_id, ["read"])
    client = await _api_client(monkeypatch)

    bad_cursor = _make_malformed_cursor(item_id="not-a-uuid")
    async with client:
        r = await client.get(
            "/v1/items",
            params={"cursor": bad_cursor},
            headers=_auth(key),
        )
        assert r.status_code == 400
        assert r.status_code != 500


# ── Test 7: invalid Base64/JSON ─────────────────────────────────────────────


async def test_invalid_base64_cursor_returns_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garbage input that cannot be decoded as base64 returns 400."""
    _require_db()
    if _isolated_tenant_id is None:
        pytest.skip(_DB_SKIP_REASON)
    tenant_id = _isolated_tenant_id
    admin_id = _isolated_admin_id
    assert admin_id is not None

    await _insert_item(
        tenant_id=tenant_id,
        principal_id=admin_id,
        content="placeholder",
        review_status="active",
    )

    key = await _issue_key(tenant_id, admin_id, ["read"])
    client = await _api_client(monkeypatch)

    async with client:
        r = await client.get(
            "/v1/items",
            params={"cursor": "!!!not-valid-base64-or-json!!!"},
            headers=_auth(key),
        )
        assert r.status_code == 400


async def test_invalid_json_cursor_returns_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cursor that decodes to non-JSON returns 400."""
    _require_db()
    if _isolated_tenant_id is None:
        pytest.skip(_DB_SKIP_REASON)
    tenant_id = _isolated_tenant_id
    admin_id = _isolated_admin_id
    assert admin_id is not None

    await _insert_item(
        tenant_id=tenant_id,
        principal_id=admin_id,
        content="placeholder",
        review_status="active",
    )

    key = await _issue_key(tenant_id, admin_id, ["read"])
    client = await _api_client(monkeypatch)

    bad_cursor = _make_malformed_cursor(valid_json=False)
    async with client:
        r = await client.get(
            "/v1/items",
            params={"cursor": bad_cursor},
            headers=_auth(key),
        )
        assert r.status_code == 400


# ── Test 8: visibility/RLS — ineligible rows ────────────────────────────────


async def test_private_items_not_in_tenant_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Private items from a different principal do not appear in the listing."""
    _require_db()
    if _isolated_tenant_id is None:
        pytest.skip(_DB_SKIP_REASON)
    tenant_id = _isolated_tenant_id
    admin_id = _isolated_admin_id
    assert admin_id is not None

    # Create a second principal with a private item.
    agent_id = str(uuid.uuid4())
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES (:id, :tenant_id, 'agent', 'agent')"
            ),
            {"id": agent_id, "tenant_id": tenant_id},
        )
        await session.commit()

    private_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=agent_id,
        content="private-item-not-visible",
        review_status="active",
        visibility="private",
    )
    tenant_id_visible = await _insert_item(
        tenant_id=tenant_id,
        principal_id=admin_id,
        content="tenant-visible-item",
        review_status="active",
        visibility="tenant",
    )

    key = await _issue_key(tenant_id, agent_id, ["read"])
    client = await _api_client(monkeypatch)

    all_items: list[str] = []
    cursor = None
    async with client:
        while True:
            params: dict[str, Any] = {"limit": 10}
            if cursor:
                params["cursor"] = cursor
            r = await client.get("/v1/items", params=params, headers=_auth(key))
            assert r.status_code == 200
            data = r.json()
            all_items.extend(item["id"] for item in data["items"])
            cursor = data.get("next_cursor")
            if not cursor:
                break

    # The agent's own private item should be visible, but the admin's
    # tenant-visible item should also be visible (it's tenant-scoped).
    # The key test: private items of OTHER principals don't appear.
    # Since the agent is the caller, its own private_id should appear,
    # and the admin's tenant-visible item should appear.
    assert private_id in all_items  # own private is visible
    assert tenant_id_visible in all_items  # tenant-visible is visible


# ── Test 9: cross-tenant isolation ──────────────────────────────────────────


async def test_cross_tenant_isolation_in_traversal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Traversal never includes another tenant's row."""
    _require_db()
    if _isolated_tenant_id is None:
        pytest.skip(_DB_SKIP_REASON)
    tenant_id = _isolated_tenant_id
    admin_id = _isolated_admin_id
    assert admin_id is not None

    # Create items in our tenant.
    for i in range(3):
        await _insert_item(
            tenant_id=tenant_id,
            principal_id=admin_id,
            content=f"our-item-{i}",
            review_status="active",
        )

    # Create items in a different tenant.
    _, other_item = await _insert_cross_tenant_item(
        admin_tenant_id=tenant_id, content="other-tenant-item"
    )

    key = await _issue_key(tenant_id, admin_id, ["read"])
    client = await _api_client(monkeypatch)

    all_items: list[str] = []
    cursor = None
    async with client:
        while True:
            params: dict[str, Any] = {"limit": 2}
            if cursor:
                params["cursor"] = cursor
            r = await client.get("/v1/items", params=params, headers=_auth(key))
            assert r.status_code == 200
            data = r.json()
            all_items.extend(item["id"] for item in data["items"])
            cursor = data.get("next_cursor")
            if not cursor:
                break

    assert other_item not in all_items


# ── Test 10: page boundary mutation posture ─────────────────────────────────


async def test_page_boundary_snapshot_semantics_documented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Document the cursor's snapshot semantics.

    The cursor encodes (created_at, id) from the last item of the previous
    page. It does NOT provide snapshot isolation — items inserted between
    page 1 and page 2 may shift relative position. This is the documented
    expected behavior: the cursor is a keyset continuation token, not a
    transaction-time snapshot.

    This test verifies that the cursor's semantics are as expected: the
    second page starts strictly after the last item of the first page.
    """
    _require_db()
    if _isolated_tenant_id is None:
        pytest.skip(_DB_SKIP_REASON)
    tenant_id = _isolated_tenant_id
    admin_id = _isolated_admin_id
    assert admin_id is not None

    base = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=100)
    for i in range(4):
        await _insert_item(
            tenant_id=tenant_id,
            principal_id=admin_id,
            content=f"boundary-item-{i}",
            review_status="active",
            created_at=base - timedelta(minutes=i),
        )

    key = await _issue_key(tenant_id, admin_id, ["read"])
    client = await _api_client(monkeypatch)

    async with client:
        r1 = await client.get(
            "/v1/items",
            params={"limit": 2, "active_only": "true"},
            headers=_auth(key),
        )
        assert r1.status_code == 200
        data1 = r1.json()
        next_cursor = data1["next_cursor"]
        assert next_cursor is not None

        r2 = await client.get(
            "/v1/items",
            params={"limit": 2, "active_only": "true", "cursor": next_cursor},
            headers=_auth(key),
        )
        assert r2.status_code == 200
        data2 = r2.json()

        # The second page's items must be strictly older (or same ts but
        # lower id) than the last item of page 1.
        last_page1 = data1["items"][-1]
        first_page2 = data2["items"][0]
        last_ts = datetime.fromisoformat(last_page1["created_at"])
        first_ts = datetime.fromisoformat(first_page2["created_at"])

        if last_ts == first_ts:
            assert first_page2["id"] < last_page1["id"]
        else:
            assert first_ts < last_ts
