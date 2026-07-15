# ruff: noqa: E501
"""Postgres-level RLS isolation tests for the non-owner application role.

ENG-AUD-002: prove that Row Level Security is a real defense-in-depth backstop
when the service connects as the non-owner role (``engram_app``), not just when
it connects as the table owner (which bypassed RLS before this slice).

These tests connect with three identities:

* OWNER (``ENGRAM_DATABASE_URL``) — sets up data across tenants (it bypasses
  RLS, so it can write tenant A and tenant B rows directly).
* APP (``ENGRAM_APP_DATABASE_URL``) — the non-owner runtime role. All isolation
  assertions run as this role.

Coverage:
  - A cross-tenant direct SELECT as the app role (with tenant-A context set)
    cannot see tenant-B rows, across every representative RLS table.
  - A SELECT as the app role with NO tenant context set leaks zero rows.
  - The app role has no BYPASSRLS and owns no tenant-scoped table.
  - Every tenant-scoped table has ``relforcerowsecurity = true`` (so owner
    bypass can no longer be relied on).

Requires a live PostgreSQL with the v2 schema + migration 003 applied AND the
``engram_app`` role created. Skips automatically otherwise.
"""

from __future__ import annotations

import contextlib
import os
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

# asyncpg is imported lazily so the module imports without a live DB.

# Tables that carry a tenant_isolation_* policy and must have FORCE RLS.
# ``tenants`` (root parent, no policy) and ``schema_migrations`` (meta) are
# intentionally excluded.
RLS_TABLES: tuple[str, ...] = (
    "memory_items",
    "memory_embeddings",
    "item_events",
    "recall_logs",
    "api_keys",
    "workspace_members",
    "kg_triples",
    "tunnels",
    "classification_rules",
    "classification_runs",
    "tenant_config",
    "deletion_events",
    "feedback_events",
    "workspaces",
    "principals",
    "jobs",
    "memory_kinds",
    "memory_edges",
    "usage_events",
)

# Representative high-risk tables exercised with real rows below. The full set
# is covered by the schema-level FORCE-RLS assertion.
REPRESENTATIVE_RLS_TABLES: tuple[str, ...] = (
    "memory_items",
    "memory_embeddings",
    "item_events",
    "recall_logs",
    "api_keys",
    "workspace_members",
    "usage_events",
)


def _owner_dsn() -> str | None:
    return os.environ.get("ENGRAM_DATABASE_URL") or os.environ.get("ENGRAM_OWNER_DATABASE_URL")


def _app_dsn() -> str | None:
    return os.environ.get("ENGRAM_APP_DATABASE_URL")


async def _connect(url: str):
    import asyncpg

    from engram.migrations import normalize_asyncpg_url

    return await asyncpg.connect(normalize_asyncpg_url(url))


async def _owner_ok() -> bool:
    url = _owner_dsn()
    if not url:
        return False
    try:
        conn = await _connect(url)
        await conn.close()
        return True
    except Exception:
        return False


async def _app_ok() -> bool:
    """True iff the app role exists and is connectable."""
    url = _app_dsn()
    if not url:
        return False
    try:
        conn = await _connect(url)
        await conn.close()
        return True
    except Exception:
        return False


def _skip_if_no_rls_stack():
    if not _owner_dsn():
        pytest.skip("requires ENGRAM_DATABASE_URL (owner) for setup")
    if not _app_dsn():
        pytest.skip("requires ENGRAM_APP_DATABASE_URL (non-owner app role)")


# ---------------------------------------------------------------------------
# Fixtures: create two fresh tenants + per-table data, owned/cleaned by owner.
# ---------------------------------------------------------------------------


@pytest.fixture
async def two_tenant_data():
    """Yield (owner_conn, tenant_a, principal_a, tenant_b, principal_b).

    Creates two brand-new tenants each with an admin principal, workspace, and
    representative rows in every RLS-protected table (as the owner, bypassing
    RLS). Tears everything down afterwards. Skips if the RLS stack is absent.
    """
    import asyncpg

    _skip_if_no_rls_stack()
    if not await _owner_ok() or not await _app_ok():
        pytest.skip("requires a live PostgreSQL with the app role (run docker compose up)")

    owner = await _connect(_owner_dsn())  # type: ignore[arg-type]
    a_id = str(uuid4())
    b_id = str(uuid4())
    p_a = str(uuid4())
    p_b = str(uuid4())
    ws_a = str(uuid4())
    ws_b = str(uuid4())
    item_a = str(uuid4())
    item_b = str(uuid4())
    event_a = str(uuid4())
    event_b = str(uuid4())
    member_a = str(uuid4())
    member_b = str(uuid4())
    try:
        async with owner.transaction():
            for tid, pid, label in ((a_id, p_a, "TenantA"), (b_id, p_b, "TenantB")):
                await owner.execute(
                    "INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $3)",
                    tid,
                    label,
                    f"{label.lower()}-{tid[:8]}",
                )
                await owner.execute(
                    "INSERT INTO principals (id, tenant_id, name, type) "
                    "VALUES ($1, $2, 'admin', 'admin')",
                    pid,
                    tid,
                )
                await owner.execute(
                    "INSERT INTO tenant_config (tenant_id, config_version, active) "
                    "VALUES ($1, 'v1', TRUE)",
                    tid,
                )

            # Workspaces + membership in EACH tenant (so workspace_members is
            # testable both ways).
            await owner.execute(
                "INSERT INTO workspaces (id, tenant_id, name, slug) VALUES ($1, $2, 'ws-a', 'ws-a')",
                ws_a,
                a_id,
            )
            await owner.execute(
                "INSERT INTO workspaces (id, tenant_id, name, slug) VALUES ($1, $2, 'ws-b', 'ws-b')",
                ws_b,
                b_id,
            )
            await owner.execute(
                "INSERT INTO workspace_members (id, workspace_id, principal_id, role) "
                "VALUES ($1, $2, $3, 'owner')",
                member_a,
                ws_a,
                p_a,
            )
            await owner.execute(
                "INSERT INTO workspace_members (id, workspace_id, principal_id, role) "
                "VALUES ($1, $2, $3, 'owner')",
                member_b,
                ws_b,
                p_b,
            )

            # memory_items in each tenant.
            await owner.execute(
                "INSERT INTO memory_items (id, tenant_id, principal_id, content, content_hash, "
                "kind, review_status) VALUES ($1, $2, $3, 'tenant A secret', 'ha', 'fact', 'active')",
                item_a,
                a_id,
                p_a,
            )
            await owner.execute(
                "INSERT INTO memory_items (id, tenant_id, principal_id, content, content_hash, "
                "kind, review_status) VALUES ($1, $2, $3, 'tenant B secret', 'hb', 'fact', 'active')",
                item_b,
                b_id,
                p_b,
            )

            # memory_embeddings (denormalized tenant_id; composite FK).
            await owner.execute(
                "INSERT INTO memory_embeddings (id, memory_item_id, tenant_id, embedding_model, "
                "embedding_dim, embedding_status) VALUES ($1, $2, $3, 'm', 1, 'complete')",
                str(uuid4()),
                item_a,
                a_id,
            )
            await owner.execute(
                "INSERT INTO memory_embeddings (id, memory_item_id, tenant_id, embedding_model, "
                "embedding_dim, embedding_status) VALUES ($1, $2, $3, 'm', 1, 'complete')",
                str(uuid4()),
                item_b,
                b_id,
            )

            # item_events (policy: EXISTS via memory_items.tenant_id).
            await owner.execute(
                "INSERT INTO item_events (id, item_id, event_type) VALUES ($1, $2, 'observed')",
                event_a,
                item_a,
            )
            await owner.execute(
                "INSERT INTO item_events (id, item_id, event_type) VALUES ($1, $2, 'observed')",
                event_b,
                item_b,
            )

            # recall_logs in each tenant.
            await owner.execute(
                "INSERT INTO recall_logs (id, tenant_id, principal_id, mode, query) "
                "VALUES ($1, $2, $3, 'startup', 'a')",
                str(uuid4()),
                a_id,
                p_a,
            )
            await owner.execute(
                "INSERT INTO recall_logs (id, tenant_id, principal_id, mode, query) "
                "VALUES ($1, $2, $3, 'startup', 'b')",
                str(uuid4()),
                b_id,
                p_b,
            )

            # api_keys in each tenant.
            await owner.execute(
                "INSERT INTO api_keys (id, tenant_id, principal_id, key_hash, label) "
                "VALUES ($1, $2, $3, 'hash-a', 'a')",
                str(uuid4()),
                a_id,
                p_a,
            )
            await owner.execute(
                "INSERT INTO api_keys (id, tenant_id, principal_id, key_hash, label) "
                "VALUES ($1, $2, $3, 'hash-b', 'b')",
                str(uuid4()),
                b_id,
                p_b,
            )

            # usage_events in each tenant.
            await owner.execute(
                "INSERT INTO usage_events (id, tenant_id, principal_id, event_type, "
                "operation, status) VALUES ($1, $2, $3, 'candidate.observed', "
                "'process_memory_candidate', 'accepted_for_processing')",
                str(uuid4()),
                a_id,
                p_a,
            )
            await owner.execute(
                "INSERT INTO usage_events (id, tenant_id, principal_id, event_type, "
                "operation, status) VALUES ($1, $2, $3, 'candidate.observed', "
                "'process_memory_candidate', 'accepted_for_processing')",
                str(uuid4()),
                b_id,
                p_b,
            )

        yield {
            "owner": owner,
            "tenant_a": a_id,
            "principal_a": p_a,
            "tenant_b": b_id,
            "principal_b": p_b,
            "item_a": item_a,
            "item_b": item_b,
            "event_b": event_b,
            "member_b": member_b,
        }
    finally:
        # Teardown: delete memory_items first (their principal_id FK has no
        # cascade), which cascades to memory_embeddings + item_events; then the
        # tenants cascade the rest (principals, workspaces/members, api_keys,
        # recall_logs, tenant_config). Failures are suppressed — each test uses
        # fresh UUIDs so any orphan rows never collide with another test.
        with contextlib.suppress(asyncpg.PostgresError):
            await owner.execute(
                "DELETE FROM memory_items WHERE tenant_id = ANY($1::uuid[])", [a_id, b_id]
            )
        with contextlib.suppress(asyncpg.PostgresError):
            await owner.execute("DELETE FROM tenants WHERE id = ANY($1::uuid[])", [a_id, b_id])
        await owner.close()


# ---------------------------------------------------------------------------
# 1. Cross-tenant SELECT is blocked (representative tables).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", REPRESENTATIVE_RLS_TABLES)
async def test_cross_tenant_select_blocked_as_app_role(two_tenant_data, table: str):
    """As the app role scoped to tenant A, tenant B rows are invisible."""
    data = two_tenant_data
    app = await _connect(_app_dsn())  # type: ignore[arg-type]
    try:
        await app.execute("SELECT set_config('app.tenant_id', $1, false)", data["tenant_a"])
        await app.execute("SELECT set_config('app.principal_id', $1, false)", data["principal_a"])

        # Each table's tenant-B row must be invisible when scoped to tenant A.
        if table == "memory_items":
            assert await app.fetchval(
                "SELECT count(*) FROM memory_items WHERE id = $1", data["item_b"]
            ) == 0
            assert await app.fetchval("SELECT count(*) FROM memory_items") >= 1
        elif table == "memory_embeddings":
            assert await app.fetchval(
                "SELECT count(*) FROM memory_embeddings WHERE tenant_id::text = $1",
                data["tenant_b"],
            ) == 0
            assert await app.fetchval("SELECT count(*) FROM memory_embeddings") >= 1
        elif table == "item_events":
            # Transitive policy via memory_items; tenant B's event must not appear.
            assert await app.fetchval(
                "SELECT count(*) FROM item_events WHERE id = $1", data["event_b"]
            ) == 0
            assert await app.fetchval("SELECT count(*) FROM item_events") >= 1
        elif table == "recall_logs":
            assert await app.fetchval(
                "SELECT count(*) FROM recall_logs WHERE tenant_id::text = $1", data["tenant_b"]
            ) == 0
            assert await app.fetchval("SELECT count(*) FROM recall_logs") >= 1
        elif table == "api_keys":
            assert await app.fetchval(
                "SELECT count(*) FROM api_keys WHERE tenant_id::text = $1", data["tenant_b"]
            ) == 0
            assert await app.fetchval("SELECT count(*) FROM api_keys") >= 1
        elif table == "workspace_members":
            # Transitive policy via workspaces; tenant B's membership must not appear.
            assert await app.fetchval(
                "SELECT count(*) FROM workspace_members WHERE id = $1", data["member_b"]
            ) == 0
            assert await app.fetchval("SELECT count(*) FROM workspace_members") >= 1
        elif table == "usage_events":
            assert await app.fetchval(
                "SELECT count(*) FROM usage_events WHERE tenant_id::text = $1", data["tenant_b"]
            ) == 0
            assert await app.fetchval("SELECT count(*) FROM usage_events") >= 1
    finally:
        await app.close()


# ---------------------------------------------------------------------------
# 2. Missing tenant context leaks nothing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", REPRESENTATIVE_RLS_TABLES)
async def test_missing_tenant_context_leaks_nothing(two_tenant_data, table: str):
    """As the app role with NO tenant context set, no protected rows are visible.

    This also doubles as the "owner-bypass detection" guard: if these tests were
    accidentally pointed at the owner role, the owner would bypass RLS and see
    rows here (count > 0), failing the assertion.
    """
    app = await _connect(_app_dsn())  # type: ignore[arg-type]
    try:
        # Deliberately do NOT set app.tenant_id / app.principal_id.
        count = await app.fetchval(f"SELECT count(*) FROM {table}")
        assert count == 0, (
            f"{table}: app role with no tenant context saw {count} row(s) — RLS context leak"
        )
    finally:
        await app.close()


async def test_missing_then_set_context_isolates(two_tenant_data):
    """Setting then relying on context works on a single app-role connection."""
    data = two_tenant_data
    app = await _connect(_app_dsn())  # type: ignore[arg-type]
    try:
        # No context -> nothing.
        assert await app.fetchval("SELECT count(*) FROM memory_items") == 0
        # Set tenant A -> see only tenant A.
        await app.execute("SELECT set_config('app.tenant_id', $1, false)", data["tenant_a"])
        await app.execute("SELECT set_config('app.principal_id', $1, false)", data["principal_a"])
        assert await app.fetchval("SELECT count(*) FROM memory_items") >= 1
        assert await app.fetchval(
            "SELECT count(*) FROM memory_items WHERE tenant_id::text <> $1", data["tenant_a"]
        ) == 0
    finally:
        await app.close()


async def test_rls_context_survives_mid_request_rollback(two_tenant_data):
    """F5: context set via get_session's helper survives a mid-request rollback.

    Replicates the dedup/conflict path shape that motivated the F5 fix: the
    request applies RLS context (``engram.db.apply_rls_context``), then later
    rolls back (the ``IntegrityError``-recovery ``session.rollback()`` before the
    dedup re-query), then re-queries. The re-query must STILL see the tenant's
    rows.

    A session-level GUC issued inside a transaction that is then rolled back is
    reverted by PostgreSQL — so ``apply_rls_context`` commits the GUCs in their
    own short transaction. If that commit were removed, the ``rollback()`` below
    would drop the context and the re-query would see zero rows, failing this
    test. This is why the test uses the real helper on a SQLAlchemy session
    (matching production) rather than hand-rolled asyncpg ``set_config``.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from engram.db import apply_rls_context

    data = two_tenant_data
    url = _app_dsn()
    assert url is not None
    app_engine = create_async_engine(url, poolclass=NullPool)
    factory = async_sessionmaker(app_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as session:
            await apply_rls_context(
                session,
                tenant_id=data["tenant_a"],
                principal_id=data["principal_a"],
            )
            before = (await session.execute(text("SELECT count(*) FROM memory_items"))).scalar()
            assert before is not None and before >= 1

            # Simulate the mid-request rollback (dedup IntegrityError recovery).
            await session.rollback()

            # The re-query must still see tenant A's rows — context survived.
            after = (await session.execute(text("SELECT count(*) FROM memory_items"))).scalar()
            assert after == before
    finally:
        await app_engine.dispose()


# ---------------------------------------------------------------------------
# 3. App role posture: no BYPASSRLS, no table ownership.
# ---------------------------------------------------------------------------


async def test_app_role_has_no_bypassrls():
    _skip_if_no_rls_stack()
    if not await _app_ok():
        pytest.skip("requires the non-owner app role")
    owner = await _connect(_owner_dsn())  # type: ignore[arg-type]
    try:
        row = await owner.fetchrow(
            "SELECT rolbypassrls, rolsuper, rolcreaterole FROM pg_roles WHERE rolname = 'engram_app'"
        )
        assert row is not None, "engram_app role was not created by the migration"
        assert row["rolbypassrls"] is False
        assert row["rolsuper"] is False
        assert row["rolcreaterole"] is False
    finally:
        await owner.close()


async def test_app_role_owns_no_tenant_scoped_table():
    _skip_if_no_rls_stack()
    if not await _app_ok():
        pytest.skip("requires the non-owner app role")
    owner = await _connect(_owner_dsn())  # type: ignore[arg-type]
    try:
        # No tenant-scoped table's owner should be engram_app.
        owned = await owner.fetch(
            """
            SELECT c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_roles r ON r.oid = c.relowner
            WHERE n.nspname = 'public'
              AND c.relkind IN ('r', 'p')
              AND r.rolname = 'engram_app'
            """
        )
        assert owned == [], f"engram_app unexpectedly owns table(s): {[row['relname'] for row in owned]}"
    finally:
        await owner.close()


# ---------------------------------------------------------------------------
# 4. Every tenant-scoped table has FORCE ROW LEVEL SECURITY.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", RLS_TABLES)
async def test_tenant_tables_have_force_rls(table: str):
    _skip_if_no_rls_stack()
    if not await _owner_ok():
        pytest.skip("requires a live PostgreSQL")
    owner = await _connect(_owner_dsn())  # type: ignore[arg-type]
    try:
        row = await owner.fetchrow(
            """
            SELECT c.relrowsecurity, c.relforcerowsecurity
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname = $1
            """,
            table,
        )
        assert row is not None, f"{table} not found in public schema"
        assert row["relrowsecurity"] is True, f"{table}: RLS is not ENABLEd"
        assert row["relforcerowsecurity"] is True, (
            f"{table}: RLS is not FORCEd (table owner would bypass it)"
        )
    finally:
        await owner.close()


# ---------------------------------------------------------------------------
# 5. End-to-end: app role cannot INSERT a foreign-tenant row (WITH CHECK).
# ---------------------------------------------------------------------------


async def test_app_role_cannot_insert_cross_tenant_memory(two_tenant_data):
    """RLS WITH CHECK blocks an app-role INSERT of a tenant-B row while scoped to A."""
    data = two_tenant_data
    app = await _connect(_app_dsn())  # type: ignore[arg-type]
    import asyncpg

    try:
        await app.execute("SELECT set_config('app.tenant_id', $1, false)", data["tenant_a"])
        await app.execute("SELECT set_config('app.principal_id', $1, false)", data["principal_a"])
        with pytest.raises(asyncpg.PostgresError):
            await app.execute(
                "INSERT INTO memory_items (id, tenant_id, principal_id, content, content_hash, "
                "kind, review_status) VALUES ($1, $2, $3, 'sneaky', 'hx', 'fact', 'active')",
                str(uuid4()),
                data["tenant_b"],  # foreign tenant
                data["principal_b"],
            )
    finally:
        await app.close()
