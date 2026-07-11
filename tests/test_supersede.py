# ruff: noqa: E501
"""Postgres-backed tests for item supersession (ENG-AUD-006 / findings F6 + F7).

These tests run against a live PostgreSQL with the v2 schema
(migrations/001_init.sql + 003 FORCE RLS) so the partial unique index
``idx_memitems_dedup`` (``WHERE valid_to IS NULL AND review_status !=
'rejected'``, ``NULLS NOT DISTINCT``) and RLS policies are actually enforced.
The prior SQLite-only coverage in tests/test_items.py cannot catch the F6
ordering bug because the hand-rolled SQLite schema has no such index.

Most tests run as the default tenant (owner role in CI; the unique index and
CHECK constraints apply to all roles). The cross-tenant isolation test uses
the non-owner app role (``ENGRAM_APP_DATABASE_URL``) so RLS is actually
enforced — mirroring tests/test_rls_isolation.py.

Run locally with::

    docker compose up -d
    pytest tests/test_supersede.py tests/test_remember.py

These tests skip automatically when no DB is reachable; under CI
(``ENGRAM_FAIL_ON_DB_SKIP=1``) the skip reason matches tests/conftest.py so a
skip fails the run.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.api.app import create_app
from engram.api.routes import memory as memory_routes
from engram.config import settings
from engram.conflicts import authority_allows_supersession
from engram.db import _DEFAULT_PRINCIPAL_NAME, _DEFAULT_TENANT_SLUG, get_session
from engram.models import MemoryItem

_test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
# Engine bound to settings.database_url (owner role in CI; same pattern as
# test_conflicts.py / test_remember.py). The unique index and CHECK constraints
# apply regardless of role, which is what the F6/ordering/rollback/dedup cases
# exercise.
_test_session_factory = async_sessionmaker(
    _test_engine, class_=AsyncSession, expire_on_commit=False
)

_DB_SKIP_REASON = "requires a live PostgreSQL with the v2 schema"


async def _db_ok() -> bool:
    try:
        async with _test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _require_db():
    """Skip helper for use inside a test body (after an await _db_ok() check)."""
    pytest.skip(f"{_DB_SKIP_REASON} (run docker compose up)")


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


@pytest.fixture(autouse=True)
def _reset_embedding_provider():
    original_provider = settings.embedding_provider
    original_conflict = settings.conflict_check_on_write
    yield
    settings.embedding_provider = original_provider
    settings.conflict_check_on_write = original_conflict


async def _default_tenant_principal() -> tuple[str, str]:
    async with _test_session_factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT t.id::text AS tenant_id, p.id::text AS principal_id "
                        "FROM tenants t "
                        "JOIN principals p ON p.tenant_id = t.id AND p.name = :principal "
                        "WHERE t.slug = :slug"
                    ),
                    {"slug": _DEFAULT_TENANT_SLUG, "principal": _DEFAULT_PRINCIPAL_NAME},
                )
            )
            .mappings()
            .one()
        )
    return str(row["tenant_id"]), str(row["principal_id"])


def _make_client(tenant_id: str, principal_id: str) -> AsyncClient:
    """Build a test client whose get_session override sets an explicit RLS context.

    Uses the same set_config approach as test_item_read_eligibility so a
    non-default tenant/principal context can be injected (for cross-tenant
    tests) without depending on the auth path.
    """

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


async def _insert_item(
    *,
    tenant_id: str,
    principal_id: str,
    content: str,
    content_hash: str | None = None,
    kind: str = "fact",
    review_status: str = "active",
    source_trust: float = 0.7,
    valid_to: datetime | None = None,
    superseded_by: str | None = None,
    workspace_id: str | None = None,
) -> str:
    """Insert a memory_items row with explicit control over supersede-relevant fields.

    UUID strings are passed as ``uuid.UUID`` so asyncpg binds them natively; the
    earlier failures here (string datetime / cast syntax) are why this helper
    keeps types explicit rather than relying on text-level casts.
    """
    item_id = uuid4()
    async with _test_session_factory() as session:
        try:
            await session.execute(
                text(
                    "INSERT INTO memory_items ("
                    "id, tenant_id, workspace_id, principal_id, content, content_hash, kind, "
                    "visibility, review_status, memory_confidence, source_trust, importance, "
                    "source_type, valid_to, superseded_by"
                    ") VALUES ("
                    ":id, :tenant_id, :workspace_id, :principal_id, :content, :content_hash, "
                    ":kind, 'workspace', :review_status, 0.8, :source_trust, 0.5, "
                    "'manual', :valid_to, :superseded_by"
                    ")"
                ),
                {
                    "id": item_id,
                    "tenant_id": UUID(tenant_id),
                    "workspace_id": UUID(workspace_id) if workspace_id else None,
                    "principal_id": UUID(principal_id),
                    "content": content,
                    "content_hash": content_hash or f"sha256:{uuid4().hex}",
                    "kind": kind,
                    "review_status": review_status,
                    "source_trust": source_trust,
                    "valid_to": valid_to,
                    "superseded_by": UUID(superseded_by) if superseded_by else None,
                },
            )
            await session.commit()
        except Exception:
            # Roll back so a constraint violation (e.g. an intentional dedup
            # collision) doesn't leave the connection in an aborted state.
            await session.rollback()
            raise
    return str(item_id)


async def _fetch_item(item_id: str) -> dict[str, object]:
    async with _test_session_factory() as session:
        return (
            (
                await session.execute(
                    text(
                        "SELECT id::text, review_status, valid_to, superseded_by, "
                        "content_hash, source_trust FROM memory_items WHERE id = :id"
                    ),
                    {"id": item_id},
                )
            )
            .mappings()
            .one()
        )


async def _events_for(item_id: str) -> list[dict[str, object]]:
    async with _test_session_factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT event_type, field_name, new_value, old_value "
                        "FROM item_events WHERE item_id = :id ORDER BY created_at ASC, id ASC"
                    ),
                    {"id": item_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


# ===========================================================================
# 1. Explicit supersede succeeds without unique-index violation (F6)
# ===========================================================================


async def test_explicit_supersede_succeeds_without_unique_violation():
    """F6: supersede must not insert the replacement before expiring the
    original — on real Postgres the dedup partial index rejects that. This
    test passes only because the handler now expires-first."""
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    client = _make_client(tenant_id, principal_id)

    chash = f"sha256:{uuid4().hex}"
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="supersede me",
        content_hash=chash,
    )

    async with client:
        resp = await client.post(
            f"/v1/items/{item_id}/supersede",
            json={"reason": "outdated"},
        )
    assert resp.status_code == 200, resp.text


# ===========================================================================
# 2. Original is expired and points to replacement
# ===========================================================================


async def test_original_expired_and_points_to_replacement():
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    client = _make_client(tenant_id, principal_id)

    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="original to expire",
    )

    async with client:
        resp = await client.post(f"/v1/items/{item_id}/supersede", json={})
    assert resp.status_code == 200, resp.text
    new_id = resp.json()["new_item"]["id"]

    old = await _fetch_item(item_id)
    assert old["valid_to"] is not None
    assert str(old["superseded_by"]) == new_id


# ===========================================================================
# 3. Replacement is active and provenance-links to original
# ===========================================================================


async def test_replacement_active_with_provenance_link():
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    client = _make_client(tenant_id, principal_id)

    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="provenance target",
    )

    async with client:
        resp = await client.post(f"/v1/items/{item_id}/supersede", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    new_id = body["new_item"]["id"]

    new = await _fetch_item(new_id)
    assert new["valid_to"] is None
    assert new["superseded_by"] is None
    assert new["review_status"] == "active"

    # Replacement provenance event points back to the original.
    repl_events = await _events_for(new_id)
    assert any(
        e["event_type"] == "supersede"
        and e["field_name"] == "replaces"
        and str(e["new_value"]) == item_id
        for e in repl_events
    ), repl_events


# ===========================================================================
# 4. Transaction rollback leaves original active if replacement insert fails
# ===========================================================================


async def test_rollback_leaves_original_active_on_failed_insert(monkeypatch):
    """Atomicity: if the replacement insert fails after the original is expired,
    the expiration must roll back — the original stays active.

    The failure is simulated by wrapping the module-level ``insert`` so it
    raises an ``IntegrityError`` (unique violation, SQLSTATE 23505) on the
    MemoryItem insert. This routes through the app's real
    ``integrity_error_handler`` → 409, exercising the genuine rollback path
    rather than an artificial 500. ``monkeypatch`` auto-restores ``insert``
    on test exit so sibling tests are unaffected.
    """
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    client = _make_client(tenant_id, principal_id)

    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="rollback guard",
    )

    real_insert = memory_routes.insert

    def failing_insert(target, *args, **kwargs):
        # Only the MemoryItem replacement insert should fail; other inserts
        # (none in this handler besides the replacement) pass through.
        if target is MemoryItem:
            # Build a recognizable unique-violation IntegrityError so the app's
            # error handler maps it to 409 and the transaction rolls back.
            raise IntegrityError(
                "simulated replacement insert failure",
                params=None,
                orig=type(
                    "FakeUniqueViolation",
                    (),
                    {"sqlstate": "23505", "constraint_name": "idx_memitems_dedup"},
                )(),
            )
        return real_insert(target, *args, **kwargs)

    monkeypatch.setattr(memory_routes, "insert", failing_insert)

    async with client:
        resp = await client.post(f"/v1/items/{item_id}/supersede", json={})

    assert resp.status_code == 409, resp.text

    # The original must remain active — the expiration rolled back with the
    # failed replacement insert (single transaction).
    old = await _fetch_item(item_id)
    assert old["valid_to"] is None
    assert old["superseded_by"] is None


# ===========================================================================
# 5. Dedup constraints still work
# ===========================================================================


async def test_dedup_still_enforced_and_expired_original_no_longer_blocks():
    """The dedup partial index tracks the *active* row. Before supersession a
    duplicate content_hash is rejected; after supersession the original leaves
    the index (expired) and the replacement becomes the active dedup target —
    proving the index keys correctly on ``valid_to``/``review_status``, not on
    row identity."""
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    client = _make_client(tenant_id, principal_id)

    chash = f"sha256:{uuid4().hex}"
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="dedup window content",
        content_hash=chash,
    )

    # Same content_hash while original is active → rejected by the unique index.
    with pytest.raises(IntegrityError):
        await _insert_item(
            tenant_id=tenant_id,
            principal_id=principal_id,
            content="dedup window content",
            content_hash=chash,
        )

    # Supersede the original (expires it, removing it from the partial index;
    # the replacement is now the single active row for this content_hash).
    async with client:
        resp = await client.post(f"/v1/items/{item_id}/supersede", json={})
    assert resp.status_code == 200, resp.text
    replacement_id = resp.json()["new_item"]["id"]

    # A third active row with the same content_hash is STILL rejected — now by
    # the replacement, which is the active dedup target. This confirms the index
    # did not widen (every active row is still protected) and that supersession
    # did not leave two active rows.
    with pytest.raises(IntegrityError):
        await _insert_item(
            tenant_id=tenant_id,
            principal_id=principal_id,
            content="dedup window content",
            content_hash=chash,
        )

    # Exactly one active row remains for this content_hash (the replacement).
    async with _test_session_factory() as session:
        active_id = (
            await session.execute(
                text(
                    "SELECT id::text FROM memory_items "
                    "WHERE content_hash = :chash AND valid_to IS NULL "
                    "AND review_status != 'rejected'"
                ),
                {"chash": chash},
            )
        ).scalar_one()
    assert active_id == replacement_id


# ===========================================================================
# 6. Authority hierarchy preserved
# ===========================================================================


def test_authority_allows_supersession_helper():
    """The centralized authority comparison: higher/equal may supersede, lower may not."""
    # Higher authority may supersede lower.
    assert authority_allows_supersession(new_authority=50, old_authority=10) is True
    # Equal authority may supersede.
    assert authority_allows_supersession(new_authority=30, old_authority=30) is True
    # Lower authority may not supersede.
    assert authority_allows_supersession(new_authority=10, old_authority=50) is False


async def test_endpoint_equal_authority_supersede_allowed():
    """The explicit supersede endpoint clones source_trust, so the replacement
    is equal-authority and the gate allows it."""
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    client = _make_client(tenant_id, principal_id)

    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="equal authority",
        source_trust=0.9,
    )

    async with client:
        resp = await client.post(f"/v1/items/{item_id}/supersede", json={})
    assert resp.status_code == 200, resp.text
    new = await _fetch_item(resp.json()["new_item"]["id"])
    # source_trust is a REAL (float32) column; 0.9 rounds to 0.900000...
    assert float(new["source_trust"]) == pytest.approx(0.9)


# ===========================================================================
# 7. Cross-tenant / ineligible supersede cannot disclose or mutate
# ===========================================================================


def _app_dsn() -> str | None:
    """The non-owner app role DSN (RLS-enforced). Only set in CI / docker compose."""
    return os.environ.get("ENGRAM_APP_DATABASE_URL")


async def _seed_second_tenant() -> tuple[str, str]:
    """Create a second tenant with an admin principal + tenant_config."""
    tenant_id = str(uuid4())
    principal_id = str(uuid4())
    async with _test_session_factory() as session:
        await session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
            {"id": tenant_id, "name": "Other", "slug": f"other-{tenant_id[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES (:id, :tid, 'admin', 'admin')"
            ),
            {"id": principal_id, "tid": tenant_id},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_config (tenant_id, config_version, active) "
                "VALUES (:tid, 'v1', TRUE)"
            ),
            {"tid": tenant_id},
        )
        await session.commit()
    return tenant_id, principal_id


async def test_cross_tenant_supersede_returns_404_and_does_not_mutate():
    """A caller scoped to tenant B cannot supersede an item in tenant A. The
    supersede endpoint fetches via ``_fetch_item`` (no app-level predicate),
    relying on RLS for isolation — so this test connects as the non-owner app
    role (``ENGRAM_APP_DATABASE_URL``) where RLS is actually enforced.

    Skips when the app role DSN is unavailable (e.g. local single-role dev);
    the broader RLS posture is covered by tests/test_rls_isolation.py.
    """
    if not await _db_ok():
        _require_db()
    app_dsn = _app_dsn()
    if not app_dsn:
        pytest.skip("requires ENGRAM_APP_DATABASE_URL (non-owner app role; run docker compose up)")

    tenant_a, principal_a = await _default_tenant_principal()
    tenant_b, principal_b = await _seed_second_tenant()

    item_id = await _insert_item(
        tenant_id=tenant_a,
        principal_id=principal_a,
        content="tenant A private item",
    )

    # Build an app-role engine + client scoped to tenant B.
    app_engine = create_async_engine(app_dsn, poolclass=NullPool)
    app_factory = async_sessionmaker(app_engine, class_=AsyncSession, expire_on_commit=False)

    async def _override_get_session_b() -> AsyncIterator[AsyncSession]:
        async with app_factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_b}
            )
            await session.execute(
                text("SELECT set_config('app.principal_id', :pid, true)"), {"pid": principal_b}
            )
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = _override_get_session_b
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client_b:
            resp = await client_b.post(f"/v1/items/{item_id}/supersede", json={})
        assert resp.status_code == 404, resp.text
    finally:
        await app_engine.dispose()

    # Original in tenant A is unchanged.
    old = await _fetch_item(item_id)
    assert old["valid_to"] is None
    assert old["superseded_by"] is None


# ===========================================================================
# 8. Remember-time singleton supersession under real Postgres
# ===========================================================================


async def test_singleton_preference_supersession_works():
    """The /v1/remember singleton-supersession path must be safe under real
    Postgres constraints (it is: supersession is keyed on subject family, not
    content hash, so there is no dedup window)."""
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    client = _make_client(tenant_id, principal_id)

    settings.embedding_provider = "none"

    async with client:
        first = await client.post(
            "/v1/remember",
            json={
                "content": "prefer dark mode",
                "kind": "preference",
                "subject_type": "user",
                "subject_id": "u1",
                "source_type": "manual",
            },
        )
        assert first.status_code == 201, first.text
        first_id = first.json()["id"]

        second = await client.post(
            "/v1/remember",
            json={
                "content": "prefer light mode",
                "kind": "preference",
                "subject_type": "user",
                "subject_id": "u1",
                "source_type": "manual",
            },
        )
        assert second.status_code == 201, second.text
        body = second.json()
        assert body["status"] == "superseded"
        assert body["superseded_id"] == first_id

    old = await _fetch_item(first_id)
    assert old["valid_to"] is not None
    assert str(old["superseded_by"]) == body["id"]


async def test_rejected_singleton_does_not_block_new_active_write():
    """A rejected or expired singleton must not block a new active singleton
    write — the dedup index excludes rejected rows, and expired rows leave the
    partial index."""
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()

    chash = "sha2:rejected-singleton"
    # A rejected singleton with this content_hash.
    await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="rejected pref",
        content_hash=chash,
        kind="preference",
        review_status="rejected",
    )

    # A new active item with the same content_hash must succeed — rejected rows
    # are excluded from idx_memitems_dedup.
    new_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="rejected pref",
        content_hash=chash,
        kind="preference",
        review_status="active",
    )
    new = await _fetch_item(new_id)
    assert new["valid_to"] is None
    assert new["review_status"] == "active"


# ===========================================================================
# 9. Eligibility: superseding an already-dead item is rejected (409)
# ===========================================================================


async def test_supersede_already_expired_returns_409():
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    client = _make_client(tenant_id, principal_id)

    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="already expired",
        valid_to=datetime(2026, 1, 1, tzinfo=UTC),
    )

    async with client:
        resp = await client.post(f"/v1/items/{item_id}/supersede", json={})
    assert resp.status_code == 409, resp.text


async def test_supersede_rejected_item_returns_409():
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    client = _make_client(tenant_id, principal_id)

    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="rejected item",
        review_status="rejected",
    )

    async with client:
        resp = await client.post(f"/v1/items/{item_id}/supersede", json={})
    assert resp.status_code == 409, resp.text
