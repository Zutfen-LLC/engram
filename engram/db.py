"""Database connection management."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator
from uuid import UUID

from fastapi import Depends
from sqlalchemy import Connection, event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, SessionTransaction

from engram.auth import Principal, get_current_principal
from engram.config import settings

# Runtime engine — connects as the non-owner application role (engram_app in the
# default deployment). RLS policies apply to it, so tenant isolation is enforced
# at the database level. The service serves all requests through this engine.
engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=10,
    max_overflow=20,
)

async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Owner/admin engine — connects as the table-owning role (a superuser in the
# default Compose image, which bypasses RLS). Used by operations that must see
# across tenants: principal/key resolution, cross-tenant CLI scans
# (promote-proposed / backfill-embeddings), and is the role migrations run as.
# Sized to match the runtime engine because principal/key resolution runs on
# every request through this pool. When ``owner_database_url`` is unset this is
# the same engine as the runtime one (single-role dev/test).
owner_engine = create_async_engine(
    settings.owner_database_url or settings.database_url,
    echo=False,
    pool_size=10,
    max_overflow=10,
)

owner_session_factory = async_sessionmaker(
    owner_engine, class_=AsyncSession, expire_on_commit=False
)

# Read engine — connects as the same non-owner application role, but may point
# at a read replica via ``ENGRAM_READ_DATABASE_URL`` (ENG-AUD-011 / F18). RLS
# applies identically to this connection since it uses the same app role.
# When unset this falls back to the primary ``database_url`` (same fallback
# idiom as ``owner_database_url``), so "read session" always means "the
# read-safe path", not "a replica specifically" — read-replica support is
# opt-in and only as complete as this fallback. Only read-only recall paths
# (currently: startup recall candidate selection) use this engine; promotion,
# item-event writes, job-queue writes, and telemetry always use the primary
# engine.
read_engine = create_async_engine(
    settings.read_database_url or settings.database_url,
    echo=False,
    pool_size=10,
    max_overflow=20,
)

read_session_factory = async_sessionmaker(read_engine, class_=AsyncSession, expire_on_commit=False)

# Seed principal name / tenant slug used as the default RLS context when auth is
# disabled (Phase 1A). These match the rows inserted by migrations/001_init.sql.
_DEFAULT_TENANT_SLUG = "default"
_DEFAULT_PRINCIPAL_NAME = "admin"

# SET LOCAL (is_local=true): the GUC is scoped to the *current transaction*. It
# is re-applied at the start of every transaction by the ``after_begin`` listener
# in :func:`apply_rls_context`, so it survives the route's mid-request
# commit/rollback. Transaction-scoping matters because SQLAlchemy's AsyncSession
# checks out a fresh connection whenever a transaction ends (commit/rollback) — a
# session-level GUC set once would be dropped on the next transaction. It also
# means a pooled connection never carries a stale tenant context (the GUC is
# discarded automatically at transaction end).
_APPLY_RLS_SQL = text(
    "SELECT set_config('app.tenant_id', :tid, true), "
    "set_config('app.principal_id', :pid, true)"
)


async def apply_rls_context(
    session: AsyncSession, *, tenant_id: str | UUID, principal_id: str | UUID
) -> None:
    """Set the request's RLS tenant/principal context for the whole session.

    Sets ``SET LOCAL`` for the current transaction AND registers an
    ``after_begin`` listener that re-applies it at the start of every subsequent
    transaction in ``session``. This is required because SQLAlchemy's AsyncSession
    checks out a fresh connection whenever a transaction ends (commit/rollback),
    so a GUC set once would be lost on the next transaction. Re-applying per
    transaction keeps the context alive for the entire request — including the
    ``/v1/remember`` dedup path's mid-request rollback — while ``SET LOCAL``
    scoping means pooled connections never carry a stale tenant context.
    """
    tid, pid = str(tenant_id), str(principal_id)

    def _apply_rls(
        _session: Session, _transaction: SessionTransaction, connection: Connection
    ) -> None:
        # Fetch + close the result so asyncpg doesn't keep the operation pending
        # (which would surface as "another operation is in progress" on the next
        # statement).
        connection.execute(_APPLY_RLS_SQL, {"tid": tid, "pid": pid}).close()

    # Re-apply at the start of every transaction (instance-scoped listener; the
    # session is short-lived per request, so the listener is collected with it).
    event.listen(session.sync_session, "after_begin", _apply_rls)
    # Apply to the current/next transaction immediately so a transaction already
    # begun before this call (e.g. a test fixture's seed query) is covered too.
    await session.execute(_APPLY_RLS_SQL, {"tid": tid, "pid": pid})


async def clear_rls_context(session: AsyncSession) -> None:
    """Best-effort cleanup before the session returns to the pool.

    With ``SET LOCAL`` scoping the tenant GUC is already dropped at transaction
    end, so pooled connections never carry stale context. This only rolls back
    any pending/failed transaction to leave the session clean.
    """
    await session.rollback()


async def get_session(
    principal: Principal = Depends(get_current_principal),  # noqa: B008
) -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields a DB session with RLS context set.

    RLS policies reference ``current_setting('app.tenant_id', true)``; without a
    value set they filter out every row. The resolved ``principal`` (from
    :func:`engram.auth.get_current_principal`) supplies the tenant/principal to
    set. When auth is disabled the principal is the seed default tenant/admin,
    preserving Phase 1A behavior.

    Context is applied session-scoped and committed (:func:`apply_rls_context`)
    so it survives the request's ``commit()``/``rollback()`` — required once RLS
    is enforced for the app role (ENG-AUD-002), since transaction-local context
    is lost after a mid-request rollback (e.g. the dedup re-query path). The
    context is cleared on exit (:func:`clear_rls_context`) so a pooled connection
    is never handed to the next request with a stale tenant context.

    The seed UUIDs are generated at migration time via ``uuid_generate_v4()``,
    so the principal ids are resolved at request time, not hard-coded.
    """
    async with async_session_factory() as session:
        await apply_rls_context(
            session, tenant_id=principal.tenant_id, principal_id=principal.principal_id
        )
        try:
            yield session
        finally:
            # Best-effort cleanup: must not mask the request's own exception.
            with contextlib.suppress(Exception):
                await clear_rls_context(session)
