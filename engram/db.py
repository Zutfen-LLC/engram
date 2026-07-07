"""Database connection management."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields a DB session with RLS context set.

    RLS policies reference ``current_setting('app.tenant_id', true)``; without a
    value set they filter out every row. When ``auth_enabled`` is False (Phase
    1A) we set the seed default tenant + admin principal so policies resolve.
    Once auth is enabled this will set the authenticated principal's tenant.

    The seed UUIDs are generated at migration time via ``uuid_generate_v4()``,
    so they must be looked up rather than hard-coded.
    """
    async with async_session_factory() as session:
        if not settings.auth_enabled:
            row = (
                await session.execute(
                    text(
                        "SELECT t.id::text AS tenant_id, "
                        "p.id::text AS principal_id "
                        "FROM tenants t "
                        "JOIN principals p "
                        "  ON p.tenant_id = t.id AND p.name = :principal "
                        "WHERE t.slug = :slug"
                    ),
                    {"slug": _DEFAULT_TENANT_SLUG, "principal": _DEFAULT_PRINCIPAL_NAME},
                )
            ).mappings().one()
            # set_config(name, value, is_local=true) == SET LOCAL, but parameterized.
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": row["tenant_id"]},
            )
            await session.execute(
                text("SELECT set_config('app.principal_id', :pid, true)"),
                {"pid": row["principal_id"]},
            )
        yield session
