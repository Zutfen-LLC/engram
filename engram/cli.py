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

    backfill_parser = sub.add_parser(
        "backfill-embeddings",
        help="Populate pending/missing memory_embeddings for the configured "
        "embedding model across all tenants, or a single tenant with --tenant.",
    )
    backfill_parser.add_argument(
        "--tenant",
        default=None,
        help="Restrict backfill to a single tenant id. Default: every tenant.",
    )
    backfill_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap total candidates processed per tenant. The budget is shared "
        "across pending and missing-row populations, pending first "
        "(safety valve for very large backlogs).",
    )
    backfill_parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Items embedded per provider call/transaction (default: 100). A "
        "failed call only fails its own batch. Capped at the provider's "
        "per-request input limit (2048).",
    )
    backfill_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report pending/missing work without writing. Still scans when the "
        "embedding provider is 'none'.",
    )
    backfill_parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Abort on the first embedding failure instead of marking the row "
        "failed and continuing.",
    )
    backfill_parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Re-attempt rows previously marked 'failed'. By default failed rows "
        "are skipped (counted as skipped_failed) to avoid an endless failure loop.",
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
    elif args.command == "backfill-embeddings":
        from engram.embeddings import MAX_PROVIDER_BATCH_SIZE

        if args.batch_size < 1:
            parser.error("--batch-size must be a positive integer")
        if args.batch_size > MAX_PROVIDER_BATCH_SIZE:
            parser.error(
                f"--batch-size must be <= {MAX_PROVIDER_BATCH_SIZE} "
                "(provider per-request input limit)"
            )
        raise SystemExit(
            asyncio.run(
                _run_backfill(
                    args.tenant,
                    limit=args.limit,
                    batch_size=args.batch_size,
                    dry_run=args.dry_run,
                    fail_fast=args.fail_fast,
                    retry_failed=args.retry_failed,
                )
            )
        )
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


async def _run_backfill(
    tenant_id: str | None,
    *,
    limit: int | None = None,
    batch_size: int = 100,
    dry_run: bool = False,
    fail_fast: bool = False,
    retry_failed: bool = False,
    session_factory: Any | None = None,
) -> int:
    """Run embedding backfill and print a per-tenant summary.

    Returns 0 on success. Returns :data:`engram.embeddings.EXIT_PROVIDER_DISABLED`
    (2) when a real (non-dry-run) backfill is a no-op because the provider is
    ``none`` — ``--dry-run`` always returns 0 since it intentionally scans
    without writing regardless of provider state.

    Connecting as the table-owning role (default ``engram``) bypasses RLS so
    every tenant is scanned; the service still filters by an explicit
    ``tenant_id`` so results are correct under RLS too.

    ``session_factory`` defaults to the app's ``engram.db.async_session_factory``;
    tests pass their own NullPool factory so the CLI shares the test event
    loop's engine (avoiding asyncpg cross-loop connection issues).
    """
    from sqlalchemy import select

    from engram.db import async_session_factory as _default_factory
    from engram.embeddings import EXIT_PROVIDER_DISABLED, backfill_embeddings, summarize_backfill
    from engram.models import Tenant

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

        total_scanned = 0
        total_created = 0
        total_populated = 0
        total_failed = 0
        provider_disabled = False
        for tid in tenant_ids:
            result = await backfill_embeddings(
                session,
                tid,
                limit=limit,
                batch_size=batch_size,
                dry_run=dry_run,
                fail_fast=fail_fast,
                retry_failed=retry_failed,
            )
            print(summarize_backfill(result))
            total_scanned += result.scanned
            total_created += result.created
            total_populated += result.populated
            total_failed += result.failed
            if not result.provider_enabled and not dry_run:
                provider_disabled = True

        if dry_run:
            print(
                f"\nTotal: scanned={total_scanned} "
                f"would_create/populate across tenants (dry-run, no writes)."
            )
        else:
            print(
                f"\nTotal: scanned={total_scanned} created={total_created} "
                f"populated={total_populated} failed={total_failed}"
            )
        # A real run that wrote nothing because the provider is disabled is a
        # configuration error the operator should notice. Dry-run is always 0.
        if provider_disabled:
            return EXIT_PROVIDER_DISABLED
        return 0


if __name__ == "__main__":
    main()
