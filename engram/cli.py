"""Engram CLI entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engram import __version__


def main() -> None:
    parser = argparse.ArgumentParser(prog="engram", description="Engram memory service")
    parser.add_argument("--version", action="version", version=f"engram {__version__}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="Start the Engram API server")

    init_parser = sub.add_parser(
        "init-db",
        help="Apply pending database migrations (idempotent). Tracks applied "
        "migrations in a schema_migrations table. Use --baseline to record an "
        "already-bootstrapped database (e.g. one created by Docker's first-boot "
        "initdb) without re-running its migrations.",
    )
    init_parser.add_argument(
        "--database-url",
        default=None,
        help="Database URL to migrate. Defaults to ENGRAM_DATABASE_URL. "
        "Accepts postgresql+asyncpg:// or postgresql:// schemes.",
    )
    init_parser.add_argument(
        "--baseline",
        nargs="?",
        const="all",
        default=None,
        metavar="UPTO",
        help="Record migration files as applied WITHOUT executing them. Use once "
        "on a database bootstrapped via Docker initdb.d or a manual 'psql -f', "
        "so future migrations apply cleanly. With no value, baselines ALL current "
        "files (assumes the DB already reflects every one of them). To avoid "
        "masking a migration that shipped after the bootstrap, pass an explicit "
        "cutoff filename, e.g. --baseline 002_backfill_indexes.sql (records that "
        "file and everything before it).",
    )
    init_parser.add_argument(
        "--migrations-dir",
        default=None,
        help="Directory of *.sql migration files (default: bundled migrations/).",
    )

    key_parser = sub.add_parser(
        "generate-key",
        help="Generate a new API key (eng_<key_id>_<secret>) and its digest "
        "for manual insertion into api_keys. Prefer `bootstrap-key` or the "
        "admin API for normal key creation.",
    )
    key_parser.add_argument(
        "--label", default=None, help="Optional label for the key"
    )

    bootstrap_parser = sub.add_parser(
        "bootstrap-key",
        help="Create the FIRST API key for the seeded default/admin principal. "
        "Solves the chicken-and-egg first-key problem without hand-written SQL. "
        "Prints the plaintext key exactly once; only a hash is stored.",
    )
    bootstrap_parser.add_argument(
        "--label",
        default="bootstrap",
        help="Label for the bootstrap key (default: 'bootstrap').",
    )
    bootstrap_parser.add_argument(
        "--scopes",
        default="read,write,admin,export",
        help="Comma-separated scopes for the bootstrap key "
        "(default: read,write,admin,export).",
    )
    bootstrap_parser.add_argument(
        "--database-url",
        default=None,
        help="Database URL. Defaults to ENGRAM_DATABASE_URL.",
    )
    bootstrap_parser.add_argument(
        "--force",
        action="store_true",
        help="Allow creating an additional key even when a non-revoked key "
        "already exists for the seeded admin principal. Without --force the "
        "command refuses (idempotent guard against accidental duplicate keys).",
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

    worker_parser = sub.add_parser(
        "worker",
        help="Run the background job worker. Polls the jobs table and processes "
        "embedding.generate / conflict.check / classification.refine / "
        "promotion.path_a / retention.sweep jobs off the request path. The "
        "service still works without a worker; semantic recall, LLM "
        "classification refinement, and semantic conflict detection lag until "
        "jobs are processed.",
    )
    worker_parser.add_argument(
        "--once",
        action="store_true",
        help="Process at most one job, then exit (exit 0 even if no job was "
        "available). Without --once the worker polls indefinitely.",
    )
    worker_parser.add_argument(
        "--poll-interval",
        type=float,
        default=None,
        help="Seconds between claim attempts (default: ENGRAM_JOB_POLL_INTERVAL_SECONDS).",
    )
    worker_parser.add_argument(
        "--job-type",
        action="append",
        default=None,
        help="Only claim jobs of this type. Repeatable (e.g. "
        "--job-type embedding.generate --job-type classification.refine). "
        "Default: every job type.",
    )
    worker_parser.add_argument(
        "--max-jobs",
        type=int,
        default=None,
        help="Stop after processing this many jobs, then exit. Default: run forever.",
    )
    worker_parser.add_argument(
        "--tenant",
        default=None,
        help="Informational only — a single worker claims jobs across all tenants. "
        "Reserved for future tenant-sharded workers.",
    )
    worker_parser.add_argument(
        "--worker-id",
        default=None,
        help="Identifier recorded on claimed jobs (default: <hostname>:<pid>).",
    )

    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn

        uvicorn.run("engram.api.app:app", host="0.0.0.0", port=8000, reload=False)
    elif args.command == "init-db":
        from engram.config import settings

        # Migrations run DDL (CREATE ROLE/GRANT/ALTER TABLE FORCE RLS), which the
        # non-owner app role cannot do. Prefer the owner URL; fall back to the
        # runtime URL for single-role dev/test where they are the same.
        db_url = args.database_url or settings.owner_database_url or settings.database_url
        migrations_dir = Path(args.migrations_dir) if args.migrations_dir else None
        raise SystemExit(
            asyncio.run(
                _run_init_db(
                    db_url,
                    baseline=args.baseline,
                    migrations_dir=migrations_dir,
                )
            )
        )
    elif args.command == "generate-key":
        from engram.auth import (
            DIGEST_ALGORITHM,
            digest_api_key_secret,
            generate_api_key,
            parse_api_key,
        )

        plaintext = generate_api_key()
        parsed = parse_api_key(plaintext)
        assert parsed.key_id is not None  # new-format keys always carry a key_id
        print(f"key:              {plaintext}")
        print(f"key_id:           {parsed.key_id}")
        print(f"secret_digest:    {digest_api_key_secret(parsed.secret)}")
        print(f"digest_algorithm: {DIGEST_ALGORITHM}")
        if args.label:
            print(f"label:            {args.label}")
        print(
            "Insert key_id/secret_digest/digest_algorithm into the api_keys "
            "table. The plaintext key is shown only once.",
            file=sys.stderr,
        )
    elif args.command == "bootstrap-key":
        from engram.config import settings

        # bootstrap-key resolves the seed principal and inserts an api_keys row
        # WITHOUT RLS context (the very first key, before auth exists). It must
        # bypass RLS, so it connects as the owner.
        db_url = args.database_url or settings.owner_database_url or settings.database_url
        raise SystemExit(
            asyncio.run(
                _run_bootstrap_key(
                    db_url, label=args.label, scopes=args.scopes, force=args.force
                )
            )
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
    elif args.command == "worker":
        raise SystemExit(
            asyncio.run(
                _run_worker(
                    once=args.once,
                    poll_interval=args.poll_interval,
                    job_types=args.job_type,
                    max_jobs=args.max_jobs,
                    worker_id=args.worker_id,
                )
            )
        )
    else:
        parser.print_help()


# --- init-db ---------------------------------------------------------------


def select_baseline_targets(all_names: list[str], baseline: str) -> list[str]:
    """Return the migration filenames a ``--baseline`` run should record.

    ``baseline="all"`` returns every name. An explicit cutoff filename returns
    that file and everything before it (in sorted order), so a migration that
    shipped after the external bootstrap is NOT recorded as applied. Raises
    ``ValueError`` if the cutoff is not found in ``all_names``.
    """
    if baseline == "all":
        return list(all_names)
    if baseline not in all_names:
        raise ValueError(
            f"--baseline cutoff {baseline!r} not found in migrations "
            f"({', '.join(all_names)})"
        )
    index = all_names.index(baseline)
    return list(all_names[: index + 1])


async def _run_init_db(
    database_url: str,
    *,
    baseline: str | None = None,
    migrations_dir: Path | None = None,
) -> int:
    """Apply pending migrations against ``database_url``.

    Idempotent: applied migrations are recorded in a ``schema_migrations`` table
    and skipped on subsequent runs.

    ``baseline`` records migration files as applied WITHOUT executing them — for
    databases bootstrapped out-of-band (Docker's first-boot ``initdb.d`` or a
    manual ``psql -f``). It accepts either ``"all"`` (record every current file,
    with a warning that this assumes the DB already reflects all of them) or a
    specific cutoff filename (record that file and everything before it). The
    cutoff avoids masking a migration that shipped after the external bootstrap.

    Connects as the configured DB role (the table owner), which bypasses RLS so
    DDL and seed inserts apply. Returns 0 on success, non-zero on error.
    """
    import asyncpg

    from engram.migrations import (
        SCHEMA_MIGRATIONS_DDL,
        discover_migrations,
        migration_filename,
        normalize_asyncpg_url,
    )

    directory = migrations_dir if migrations_dir is not None else None
    dsn = normalize_asyncpg_url(database_url)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(SCHEMA_MIGRATIONS_DDL)
        applied = {
            row["filename"]
            for row in await conn.fetch("SELECT filename FROM schema_migrations")
        }
        files = discover_migrations(directory) if directory is not None else discover_migrations()
        names = [migration_filename(f) for f in files]

        if baseline is not None:
            # Resolve which files to baseline (see select_baseline_targets).
            try:
                to_baseline_names = select_baseline_targets(names, baseline)
            except ValueError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 2

            untracked_names = [n for n in to_baseline_names if n not in applied]
            if not untracked_names:
                print(f"All {len(to_baseline_names)} requested migration(s) already tracked.")
                return 0

            if baseline == "all":
                print(
                    "WARNING: --baseline with no cutoff records ALL current "
                    "migration files as applied WITHOUT running them.",
                    file=sys.stderr,
                )
                print(
                    "This assumes the database already reflects every file below. "
                    "If any shipped AFTER your database was bootstrapped, do NOT "
                    "baseline it — apply it with 'engram init-db' instead. To "
                    "baseline up to a specific file, pass --baseline <filename>.",
                    file=sys.stderr,
                )
            async with conn.transaction():
                for n in untracked_names:
                    await conn.execute(
                        "INSERT INTO schema_migrations (filename) VALUES ($1) "
                        "ON CONFLICT (filename) DO NOTHING",
                        n,
                    )
            for n in untracked_names:
                print(f"baselined: {n}  (recorded as applied, NOT executed)")
            print(
                f"Baselined {len(untracked_names)} migration(s). Future runs will "
                "apply only newer migrations.",
            )
            return 0

        pending = [f for f in files if migration_filename(f) not in applied]

        # Guard: schema already present but nothing tracked -> was bootstrapped
        # externally. Refuse to blindly re-run CREATE TABLE (would error) and
        # point the operator at --baseline.
        if not applied:
            core_exists = await conn.fetchval(
                "SELECT to_regclass('public.memory_items') IS NOT NULL"
            )
            if core_exists:
                print(
                    "ERROR: the 'memory_items' table already exists but no "
                    "migrations are tracked.",
                    file=sys.stderr,
                )
                print(
                    "This database was likely bootstrapped via Docker's "
                    "docker-entrypoint-initdb.d (first boot on an empty volume) "
                    "or a manual 'psql -f migrations/...'.",
                    file=sys.stderr,
                )
                print(
                    "Run 'engram init-db --baseline' once to record the current "
                    "migrations as applied, then re-run 'engram init-db' to apply "
                    "any newer migrations.",
                    file=sys.stderr,
                )
                return 1

        if not pending:
            print(f"Database is up to date ({len(applied)} migration(s) applied).")
            return 0

        for f in pending:
            sql = f.read_text(encoding="utf-8")
            fname = migration_filename(f)
            print(f"applying: {fname}")
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (filename) VALUES ($1) "
                    "ON CONFLICT (filename) DO NOTHING",
                    fname,
                )
        print(f"Applied {len(pending)} migration(s). Database is up to date.")
        return 0
    finally:
        await conn.close()


# --- bootstrap-key ---------------------------------------------------------


@dataclass(frozen=True)
class BootstrapKeyMaterial:
    """Pure key material produced for a bootstrap key (no DB state)."""

    plaintext: str
    key_id: str
    secret_digest: str
    digest_algorithm: str
    scopes: tuple[str, ...]
    label: str | None


def parse_scopes(raw: str) -> list[str]:
    """Parse a comma-separated scope string into a validated, de-duplicated list.

    Raises ``ValueError`` if any scope is unknown or the list is empty. The set
    of valid scopes mirrors :data:`engram.auth.VALID_SCOPES`.
    """
    from engram.auth import VALID_SCOPES

    scopes = [s.strip() for s in raw.split(",") if s.strip()]
    if not scopes:
        raise ValueError("at least one scope is required")
    invalid = [s for s in scopes if s not in VALID_SCOPES]
    if invalid:
        raise ValueError(f"unknown scope(s): {', '.join(invalid)}")
    # de-duplicate while preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    for s in scopes:
        if s not in seen:
            seen.add(s)
            ordered.append(s)
    return ordered


def make_bootstrap_key(label: str | None, scopes: list[str]) -> BootstrapKeyMaterial:
    """Generate a new-format key + digest for a bootstrap key (pure, no DB)."""
    from engram.auth import (
        DIGEST_ALGORITHM,
        digest_api_key_secret,
        generate_api_key,
        parse_api_key,
    )

    plaintext = generate_api_key()
    parsed = parse_api_key(plaintext)
    assert parsed.key_id is not None  # new-format keys always carry a key_id
    return BootstrapKeyMaterial(
        plaintext=plaintext,
        key_id=parsed.key_id,
        secret_digest=digest_api_key_secret(parsed.secret),
        digest_algorithm=DIGEST_ALGORITHM,
        scopes=tuple(scopes),
        label=label,
    )


async def _run_bootstrap_key(
    database_url: str, *, label: str, scopes: str, force: bool = False
) -> int:
    """Create the first API key for the seeded default/admin principal.

    Connects as the table-owning DB role (bypasses RLS) to insert the key for
    the seeded admin principal. Prints the plaintext key exactly once. Returns
    0 on success, non-zero if the seed principal is missing.

    Idempotency guard: refuses to create a key when a non-revoked key already
    exists for the seed principal unless ``force=True``. This prevents accidental
    duplicate admin keys from re-runs (the command is meant to create the FIRST
    key) while still allowing an explicit override.
    """
    import asyncpg

    from engram.db import _DEFAULT_PRINCIPAL_NAME, _DEFAULT_TENANT_SLUG
    from engram.migrations import normalize_asyncpg_url

    try:
        scope_list = parse_scopes(scopes)
    except ValueError as exc:
        print(f"ERROR: invalid --scopes: {exc}", file=sys.stderr)
        return 2

    material = make_bootstrap_key(label, scope_list)
    dsn = normalize_asyncpg_url(database_url)
    conn = await asyncpg.connect(dsn)
    try:
        row = await conn.fetchrow(
            "SELECT CAST(t.id AS TEXT) AS tenant_id, "
            "       CAST(p.id AS TEXT) AS principal_id "
            "FROM tenants t "
            "JOIN principals p "
            "  ON p.tenant_id = t.id AND p.name = $1 "
            "WHERE t.slug = $2",
            _DEFAULT_PRINCIPAL_NAME,
            _DEFAULT_TENANT_SLUG,
        )
        if row is None:
            print(
                "ERROR: the seeded default/admin principal was not found.",
                file=sys.stderr,
            )
            print(
                "Apply the schema first with 'engram init-db' (or let Docker's "
                "first-boot initdb.d run on an empty volume).",
                file=sys.stderr,
            )
            return 1

        existing = await conn.fetchval(
            "SELECT COUNT(*) FROM api_keys "
            "WHERE principal_id = $1::uuid AND revoked_at IS NULL",
            row["principal_id"],
        )
        if existing and not force:
            print(
                f"ERROR: {existing} non-revoked API key(s) already exist for the "
                f"seeded {_DEFAULT_PRINCIPAL_NAME!r} principal.",
                file=sys.stderr,
            )
            print(
                "bootstrap-key is meant to create the FIRST key. To create an "
                "additional key anyway, re-run with --force. To manage further "
                "keys, use the admin API (POST /v1/admin/api-keys).",
                file=sys.stderr,
            )
            return 1

        await conn.execute(
            "INSERT INTO api_keys "
            "  (tenant_id, principal_id, key_hash, key_id, secret_digest, "
            "   digest_algorithm, scopes, label, created_at) "
            "VALUES ($1::uuid, $2::uuid, NULL, $3, $4, $5, $6, $7, now())",
            row["tenant_id"],
            row["principal_id"],
            material.key_id,
            material.secret_digest,
            material.digest_algorithm,
            list(material.scopes),
            material.label,
        )
    finally:
        await conn.close()

    # Print the plaintext key exactly once with a loud warning.
    print("========================================================")
    print("  BOOTSTRAP API KEY — shown only once. Save it now.")
    print("========================================================")
    print(f"key:          {material.plaintext}")
    print(f"label:        {material.label}")
    print(f"scopes:       {', '.join(material.scopes)}")
    print(f"key_id:       {material.key_id}")
    print(f"tenant_id:    {row['tenant_id']}")
    print(f"principal_id: {row['principal_id']}")
    print()
    print(
        "Store this key securely. Only a deterministic digest of the secret is "
        "persisted (no plaintext, no bcrypt hash). To revoke or rotate, see "
        "docs/deployment.md (Auth > Rotate or revoke a key).",
        file=sys.stderr,
    )
    return 0


async def _run_promotion(
    tenant_id: str | None,
    limit: int | None,
    session_factory: Any | None = None,
) -> int:
    """Run Path A auto-promotion and print a per-tenant summary.

    Returns 0 on success. Connecting as the table-owning role (default ``engram``)
    bypasses RLS so every tenant is scanned; the service still filters by an
    explicit ``tenant_id`` so results are correct under RLS too.

    ``session_factory`` defaults to the app's ``engram.db.owner_session_factory``;
    tests pass their own NullPool factory so the CLI shares the test event loop's
    engine (avoiding asyncpg cross-loop connection issues).
    """
    from sqlalchemy import select

    from engram.db import owner_session_factory as _default_factory
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

    ``session_factory`` defaults to the app's ``engram.db.owner_session_factory``;
    tests pass their own NullPool factory so the CLI shares the test event
    loop's engine (avoiding asyncpg cross-loop connection issues).
    """
    from sqlalchemy import select

    from engram.db import owner_session_factory as _default_factory
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


async def _run_worker(
    *,
    once: bool = False,
    poll_interval: float | None = None,
    job_types: list[str] | None = None,
    max_jobs: int | None = None,
    worker_id: str | None = None,
    session_factory: Any | None = None,
    app_session_factory: Any | None = None,
) -> int:
    """Run the background job worker.

    Returns 0 on normal completion (``--once`` always returns 0, even when no
    job was available); nonzero only on fatal setup errors. Ordinary job
    failures retry/dead-letter without stopping the loop.

    Claim/lock bookkeeping uses the owner session factory (cross-tenant queue
    coordination via ``FOR UPDATE SKIP LOCKED``); payload processing uses the
    app-role session factory scoped per-tenant (see engram/worker.py).

    ``session_factory`` / ``app_session_factory`` default to the app's
    ``owner_session_factory`` / ``async_session_factory``; tests inject their
    own NullPool factories so the CLI shares the test event loop's engine
    (avoiding asyncpg cross-loop connection issues).
    """
    import os
    import socket

    from engram.db import async_session_factory as _default_app_factory
    from engram.db import owner_session_factory as _default_owner_factory
    from engram.worker import run_worker

    owner_factory = session_factory if session_factory is not None else _default_owner_factory
    app_factory = app_session_factory if app_session_factory is not None else _default_app_factory
    wid = worker_id or f"{socket.gethostname()}:{os.getpid()}"

    return await run_worker(
        worker_id=wid,
        session_factory=owner_factory,
        app_session_factory=app_factory,
        once=once,
        poll_interval=poll_interval,
        job_types=job_types,
        max_jobs=max_jobs,
    )


if __name__ == "__main__":
    main()
