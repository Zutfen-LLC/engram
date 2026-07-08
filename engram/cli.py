"""Engram CLI entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

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

    promote_parser = sub.add_parser(
        "promote-proposed",
        help="Run auto-promotion Path A (age + confidence + no conflict) for "
        "proposed memories across all tenants, or a single tenant with --tenant.",
    )
    promote_parser.add_argument(
        "--tenant",
        default=None,
        help="Restrict promotion to a single tenant id. Default: every tenant.",
    )
    promote_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap candidates scanned per tenant (safety valve for very large queues).",
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
    elif args.command == "promote-proposed":
        raise SystemExit(asyncio.run(_run_promotion(args.tenant, args.limit)))
    else:
        parser.print_help()


async def _run_promotion(
    tenant_id: str | None,
    limit: int | None,
    session_factory: Any | None = None,
) -> int:
    """Run Path A auto-promotion and print a per-tenant summary.

    Returns 0 on success. Connecting as the table-owning role (default ``engram``)
    bypasses RLS so every tenant is scanned; the service still filters by an
    explicit ``tenant_id`` so results are correct under RLS too.

    ``session_factory`` defaults to the app's ``engram.db.async_session_factory``;
    tests pass their own NullPool factory so the CLI shares the test event loop's
    engine (avoiding asyncpg cross-loop connection issues).
    """
    from sqlalchemy import select

    from engram.db import async_session_factory as _default_factory
    from engram.models import Tenant
    from engram.promotion import auto_promote_proposed_memories, summarize

    factory = session_factory if session_factory is not None else _default_factory

    async with factory() as session:
        if tenant_id is not None:
            tenant_ids: list[str] = [tenant_id]
        else:
            tenant_rows = await session.execute(select(Tenant.id))
            tenant_ids = [str(tid) for tid in tenant_rows.scalars().all()]

        if not tenant_ids:
            print("No tenants to process.")
            return 0

        total_promoted = 0
        total_scanned = 0
        for tid in tenant_ids:
            result = await auto_promote_proposed_memories(session, tid, limit=limit)
            print(summarize(result))
            total_promoted += result.promoted
            total_scanned += result.scanned

        print(f"\nTotal: scanned={total_scanned} promoted={total_promoted}")
        return 0


if __name__ == "__main__":
    main()
