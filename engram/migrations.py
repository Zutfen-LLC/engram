"""Migration application utilities for the ``engram init-db`` command.

This is a deliberately small, pragmatic migration runner — not a full framework.
It tracks applied migrations in a ``schema_migrations`` table so that:

* ``engram init-db`` is idempotent (re-runs skip already-applied files), and
* a database bootstrapped out-of-band (e.g. by Docker's
  ``docker-entrypoint-initdb.d`` on a fresh volume, or by a manual ``psql -f``)
  can be "baselined" so subsequent migrations apply cleanly without re-running
  the files that created the schema.

Only the migration *SQL* is Postgres-specific; the discovery, sorting, URL
normalization, and baseline logic here are pure and unit-tested.
"""

from __future__ import annotations

from pathlib import Path

# Where bundled migration files live (repo-root ``migrations/``). Resolved
# relative to this module so the CLI works regardless of the current directory
# (e.g. inside the Docker container where the package is installed).
MIGRATIONS_DIR: Path = Path(__file__).resolve().parent.parent / "migrations"

# Table used to record which migration files have been applied. Created lazily
# by ``init-db``; its presence is what makes the command idempotent.
SCHEMA_MIGRATIONS_DDL = """\
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename     TEXT PRIMARY KEY,
    applied_at   TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[Path]:
    """Return migration ``.sql`` files in ``directory``, sorted by name.

    Sorting by filename (e.g. ``001_init.sql``, ``002_backfill_indexes.sql``)
    is the application order. Missing directory is an error — the bundled
    migrations must ship with the package.
    """
    if not directory.is_dir():
        raise FileNotFoundError(f"migrations directory not found: {directory}")
    files = sorted(directory.glob("*.sql"), key=lambda p: p.name)
    return files


def normalize_asyncpg_url(url: str) -> str:
    """Convert an SQLAlchemy asyncpg URL to a bare libpq/asyncpg URL.

    ``ENGRAM_DATABASE_URL`` uses the ``postgresql+asyncpg://`` scheme (the
    SQLAlchemy async dialect). The raw-SQL migration runner connects with
    asyncpg directly, which wants ``postgresql://``. Any explicit ``postgresql://``
    URL is returned unchanged.
    """
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql://" + url[len("postgresql+asyncpg://"):]
    return url


def migration_filename(path: Path) -> str:
    """The recorded migration key — the basename, e.g. ``001_init.sql``."""
    return path.name
