"""Database connection management."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from engram.auth import Principal, get_current_principal
from engram.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=10,
    max_overflow=20,
)

async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Seed principal name / tenant slug used as the default RLS context when auth is
# disabled (Phase 1A). These match the rows inserted by migrations/001_init.sql.
_DEFAULT_TENANT_SLUG = "default"
_DEFAULT_PRINCIPAL_NAME = "admin"


async def get_session(
    principal: Principal = Depends(get_current_principal),  # noqa: B008
) -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields a DB session with RLS context set.

    RLS policies reference ``current_setting('app.tenant_id', true)``; without a
    value set they filter out every row. The resolved ``principal`` (from
    :func:`engram.auth.get_current_principal`) supplies the tenant/principal to
    set via ``SET LOCAL``. When auth is disabled the principal is the seed
    default tenant/admin, preserving Phase 1A behavior.

    The seed UUIDs are generated at migration time via ``uuid_generate_v4()``,
    so the principal ids are resolved at request time, not hard-coded.
    """
    async with async_session_factory() as session:
        # set_config(name, value, is_local=true) == SET LOCAL, but parameterized.
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": principal.tenant_id},
        )
        await session.execute(
            text("SELECT set_config('app.principal_id', :pid, true)"),
            {"pid": principal.principal_id},
        )
        yield session
