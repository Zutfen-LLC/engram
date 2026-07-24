"""Alembic environment for the Engram Control Plane.

Migration *execution* is owner-only by contract: the ``engram-control-plane
migrate`` / ``check-migrations`` commands connect via the owner/migration URL
(``ENGRAM_CONTROL_OWNER_DATABASE_URL``), never the runtime role. The runtime
role (``engram_control_app``) is granted only the SELECT needed for readiness
(including ``alembic_version``) — it cannot create schema or run migrations
(ENG-PORTAL-001A, alteration 5).

Uses asyncpg (the Control Plane's only database driver) with an async engine,
so no separate psycopg dependency is introduced.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
