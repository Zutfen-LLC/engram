"""Database access for the Engram Control Plane.

SQLAlchemy asyncio + asyncpg (ENG-PORTAL-001A, alteration 4). Migration
*execution* is owner-only (see ``cli.py`` / Alembic); the runtime dependency
here connects as the non-owner ``engram_control_app`` role and performs only
the reads needed for readiness — including the Alembic ``alembic_version``
table.

Engines are created lazily from the resolved settings. Importing this module
never opens a connection and never requires ``ENGRAM_CONTROL_DATABASE_URL`` to
be set, so app import and ``export-openapi`` remain database-free (alteration
6).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from engram_control_plane.config import settings


def _normalize_async_url(url: str) -> str:
    """Accept postgresql:// or postgresql+asyncpg://; return an asyncpg URL.

    asyncpg's SQLAlchemy dialect wants the ``postgresql+asyncpg://`` scheme.
    A bare ``postgresql://`` (e.g. from an owner/migration DSN) is upgraded so
    SQLAlchemy routes it through the asyncpg driver.
    """
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Return the runtime async engine, creating it once on first use.

    Raises ``RuntimeError`` if no database URL is configured. Callers that may
    run without a database (health, meta, openapi) do not call this.
    """
    global _engine
    if _engine is None:
        if not settings.database_url:
            raise RuntimeError(
                "ENGRAM_CONTROL_DATABASE_URL is not configured; the Control "
                "Plane runtime requires a dedicated database."
            )
        _engine = create_async_engine(
            _normalize_async_url(settings.database_url),
            echo=False,
            pool_size=10,
            max_overflow=20,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the async session factory, creating it once on first use."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(), class_=AsyncSession, expire_on_commit=False
        )
    return _session_factory


def reset_engine_for_tests() -> None:
    """Clear cached engine/factory so tests can inject settings changes."""
    global _engine, _session_factory
    if _engine is not None:
        # Best-effort disposal; tests typically patch settings before calling.
        _engine = None
    _session_factory = None


async def check_readiness(session: AsyncSession) -> dict[str, Any]:
    """Perform readiness checks against the dedicated Control Plane database.

    Returns a dict with ``database`` and ``migration`` status keys. A ready
    result has ``database="connected"`` and ``migration="at_head"``. This reads
    the Alembic ``alembic_version`` table (the runtime role is granted SELECT
    on it) and compares the single stored revision against the head revision
    reported by the configured Alembic environment.
    """
    # 1) Connectivity: a trivial round-trip.
    await session.execute(text("SELECT 1"))

    # 2) Migration head agreement: the runtime role may SELECT alembic_version.
    version_result = await session.execute(
        text("SELECT version_num FROM alembic_version")
    )
    current = version_result.scalar()

    head = _alembic_head_revision()
    migration = "at_head" if (current is not None and current == head) else "behind"
    return {"database": "connected", "migration": migration, "current": current, "head": head}


def _alembic_head_revision() -> str | None:
    """Return the head revision id of the bundled Alembic history, or None.

    Imported lazily and defensively so readiness never hard-fails on an Alembic
    import error inside the request path (it surfaces as ``behind`` instead).
    """
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config()  # populated programmatically; no ini file required at runtime
        cfg.set_main_option(
            "script_location", _alembic_script_location().as_posix()
        )
        script = ScriptDirectory.from_config(cfg)
        return script.get_current_head()
    except Exception:
        return None


def _alembic_script_location() -> Path:
    """Resolve the bundled Alembic ``versions`` directory."""
    from pathlib import Path

    return Path(__file__).resolve().parent / "migrations"


async def dispose_engine() -> None:
    """Dispose the runtime engine (used on app shutdown)."""
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
