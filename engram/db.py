"""Database connection management."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator
from uuid import UUID

from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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

# Seed principal name / tenant slug used as the default RLS context when auth is
# disabled (Phase 1A). These match the rows inserted by migrations/001_init.sql.
_DEFAULT_TENANT_SLUG = "default"
_DEFAULT_PRINCIPAL_NAME = "admin"

# Coalesce both GUC writes into a single round-trip.
_APPLY_RLS_SQL = text(
    "SELECT set_config('app.tenant_id', :tid, false), "
    "set_config('app.principal_id', :pid, false)"
)
_CLEAR_RLS_SQL = text(
    "SELECT set_config('app.tenant_id', '', false), "
    "set_config('app.principal_id', '', false)"
)


async def apply_rls_context(
    session: AsyncSession, *, tenant_id: str | UUID, principal_id: str | UUID
) -> None:
    """Set the request's RLS tenant/principal context on ``session``.

    Uses session-scoped GUCs (``set_config(..., is_local=false)``) and **commits**
    them in their own short transaction. The commit is essential: PostgreSQL
    reverts a session-level ``SET`` when the transaction it was issued in is
    rolled back. Without it, a mid-request rollback (e.g. the dedup re-query
    after an ``IntegrityError``) would drop the context and the next statement
    would see no rows under enforced RLS. Once committed, the GUCs persist across
    every subsequent transaction in the request.
    """
    await session.execute(
        _APPLY_RLS_SQL, {"tid": str(tenant_id), "pid": str(principal_id)}
    )
    await session.commit()


async def clear_rls_context(session: AsyncSession) -> None:
    """Clear the RLS context before the connection returns to the pool.

    Drops any pending/failed transaction, then resets both GUCs and **commits**
    so the reset persists (same session-level-SET-reverts-on-rollback rule as
    :func:`apply_rls_context`). Belt-and-suspenders: every consumer of the
    runtime engine applies context before querying, so a stale value is already
    overwritten before use; this keeps the pooled connection clean regardless.
    """
    await session.rollback()
    await session.execute(_CLEAR_RLS_SQL)
    await session.commit()


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
