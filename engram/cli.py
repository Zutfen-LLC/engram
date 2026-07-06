"""Engram CLI entry point."""

from __future__ import annotations

import argparse

from engram import __version__


def main() -> None:
    parser = argparse.ArgumentParser(prog="engram", description="Engram memory service")
    parser.add_argument("--version", action="version", version=f"engram {__version__}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="Start the Engram API server")
    sub.add_parser("init-db", help="Run database migrations")

    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn

        uvicorn.run("engram.api.app:app", host="0.0.0.0", port=8000, reload=False)
    elif args.command == "init-db":
        print("Run migrations: psql -f migrations/001_init.sql")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
