"""Engram CLI entry point."""

from __future__ import annotations

import argparse
import sys

from engram import __version__


def main() -> None:
    parser = argparse.ArgumentParser(prog="engram", description="Engram memory service")
    parser.add_argument("--version", action="version", version=f"engram {__version__}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="Start the Engram API server")
    sub.add_parser("init-db", help="Run database migrations")

    key_parser = sub.add_parser(
        "generate-key", help="Generate a new API key and its bcrypt hash"
    )
    key_parser.add_argument(
        "--label", default=None, help="Optional label for the key"
    )

    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn

        uvicorn.run("engram.api.app:app", host="0.0.0.0", port=8000, reload=False)
    elif args.command == "init-db":
        print("Run migrations: psql -f migrations/001_init.sql")
    elif args.command == "generate-key":
        from engram.auth import generate_api_key, hash_api_key

        plaintext = generate_api_key()
        key_hash = hash_api_key(plaintext)
        print(f"key:      {plaintext}")
        print(f"key_hash: {key_hash}")
        if args.label:
            print(f"label:    {args.label}")
        print(
            "Store the key_hash in the api_keys table. The plaintext key is "
            "shown only once.",
            file=sys.stderr,
        )
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
