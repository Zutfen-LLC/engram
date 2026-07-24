"""Command-line entry point for the Engram Control Plane.

Commands: ``serve``, ``migrate``, ``check-migrations``, ``export-openapi``.

``export-openapi`` is database-free and secret-free (alteration 6): it imports
the app and serializes its OpenAPI schema without touching the database.
``migrate`` / ``check-migrations`` operate through Alembic and ALWAYS run as the
owner/migration role (never the runtime role). ``serve`` runs production-required
configuration validation at startup.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from engram_control_plane import __version__

if TYPE_CHECKING:
    from alembic.config import Config


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="engram-control-plane",
        description="Engram Control Plane service",
    )
    parser.add_argument(
        "--version", action="version", version=f"engram-control-plane {__version__}"
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="Start the Control Plane API server")

    migrate_parser = sub.add_parser(
        "migrate",
        help="Apply pending Alembic migrations (owner/migration role only).",
    )
    migrate_parser.add_argument(
        "--database-url",
        default=None,
        help="Owner/migration database URL. Defaults to "
        "ENGRAM_CONTROL_OWNER_DATABASE_URL.",
    )

    check_parser = sub.add_parser(
        "check-migrations",
        help="Exit 0 if at head, 1 if behind, 2 on error.",
    )
    check_parser.add_argument(
        "--database-url",
        default=None,
        help="Owner/migration database URL. Defaults to "
        "ENGRAM_CONTROL_OWNER_DATABASE_URL.",
    )

    openapi_parser = sub.add_parser(
        "export-openapi",
        help="Write the deterministic Control Plane OpenAPI JSON to stdout or a file.",
    )
    openapi_parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output file (default: stdout).",
    )

    args = parser.parse_args()
    if args.command == "serve":
        _serve()
    elif args.command == "migrate":
        raise SystemExit(_migrate(args.database_url))
    elif args.command == "check-migrations":
        raise SystemExit(_check_migrations(args.database_url))
    elif args.command == "export-openapi":
        raise SystemExit(_export_openapi(args.output))
    else:
        parser.print_help()


def _serve() -> None:
    import uvicorn

    # Production-required validation at command execution, not import.
    from engram_control_plane.config import settings, validate_for_runtime

    validate_for_runtime(settings)

    uvicorn.run(
        "engram_control_plane.api.app:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


def _alembic_config(database_url: str | None) -> Config:
    """Build an Alembic Config pointing at the owner/migration URL."""
    from alembic.config import Config

    from engram_control_plane.config import settings

    url = database_url or settings.owner_database_url
    if not url:
        raise SystemExit(
            "ERROR: no owner database URL. Set ENGRAM_CONTROL_OWNER_DATABASE_URL "
            "or pass --database-url."
        )

    cfg = Config()
    cfg.set_main_option("script_location", _alembic_script_location().as_posix())
    # Keep the async asyncpg dialect; env.py runs migrations through an async
    # engine, so no separate sync (psycopg) driver is introduced.
    cfg.set_main_option("sqlalchemy.url", _to_async_url(url))
    return cfg


def _alembic_script_location() -> Path:
    return Path(__file__).resolve().parent / "migrations"


def _to_async_url(url: str) -> str:
    """Normalize to the asyncpg dialect URL for Alembic's async env."""
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


def _migrate(database_url: str | None) -> int:
    """Apply pending migrations as the owner/migration role."""
    from alembic import command

    cfg = _alembic_config(database_url)
    command.upgrade(cfg, "head")
    print("Control Plane migrations applied to head.")
    return 0


def _check_migrations(database_url: str | None) -> int:
    """Exit 0 if at head, 1 if behind, 2 on error."""
    cfg = _alembic_config(database_url)
    async_url = cfg.get_main_option("sqlalchemy.url")
    assert async_url is not None

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def _current_revision() -> str | None:
        engine = create_async_engine(async_url)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(text("SELECT version_num FROM alembic_version"))
                return result.scalar()
        finally:
            await engine.dispose()

    try:
        current = asyncio.run(_current_revision())
        from alembic.script import ScriptDirectory

        script = ScriptDirectory.from_config(cfg)
        head = script.get_current_head()
    except Exception as exc:  # noqa: BLE001
        # Log the type only; the message may contain connection details.
        print(f"ERROR: migration check failed: {type(exc).__name__}", file=sys.stderr)
        return 2

    if current == head:
        print(f"at head ({current})")
        return 0
    print(f"behind: current={current!r} head={head!r}", file=sys.stderr)
    return 1


def _export_openapi(output: str | None) -> int:
    """Write the deterministic OpenAPI JSON. Database-free and secret-free."""
    from engram_control_plane.api.app import create_app
    from engram_control_plane.api.openapi import canonical_openapi_json

    text = canonical_openapi_json(create_app())
    if output:
        Path(output).write_text(text, encoding="utf-8")
        print(f"wrote {output}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    main()
