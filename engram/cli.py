"""Engram CLI entry point."""
# ruff: noqa: E501

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
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
    key_parser.add_argument("--label", default=None, help="Optional label for the key")

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

    service_client_parser = sub.add_parser(
        "service-client", help="Manage service provisioning clients and credentials (owner DB only)."
    )
    service_client_sub = service_client_parser.add_subparsers(dest="service_client_command", required=True)
    service_client_create = service_client_sub.add_parser("create", help="Create a client and one credential.")
    service_client_create.add_argument("--slug", required=True)
    service_client_create.add_argument("--display-name", required=True)
    service_client_create.add_argument("--permission", action="append", default=None)
    service_client_create.add_argument("--json", action="store_true")
    service_client_rotate = service_client_sub.add_parser("rotate-key", help="Create an overlapping credential.")
    service_client_rotate.add_argument("client")
    service_client_rotate.add_argument("--label", default=None)
    service_client_rotate.add_argument("--expires-at", default=None)
    service_client_rotate.add_argument("--json", action="store_true")
    service_client_revoke = service_client_sub.add_parser("revoke-key", help="Revoke a credential by UUID or key id.")
    service_client_revoke.add_argument("credential")
    service_client_disable = service_client_sub.add_parser("disable", help="Disable a client immediately.")
    service_client_disable.add_argument("client")
    service_client_enable = service_client_sub.add_parser("enable", help="Enable a client without restoring revoked keys.")
    service_client_enable.add_argument("client")
    service_client_permissions = service_client_sub.add_parser(
        "set-permissions", help="Replace a service client's complete permission set."
    )
    service_client_permissions.add_argument("client")
    service_client_permissions.add_argument(
        "--permission", action="append", required=True
    )
    service_client_permissions.add_argument("--json", action="store_true")
    delegation_grant_parser = sub.add_parser(
        "delegation-grant",
        help="Manage owner-controlled service delegation grants (owner DB only).",
    )
    delegation_grant_sub = delegation_grant_parser.add_subparsers(
        dest="delegation_grant_command", required=True
    )
    delegation_grant_create = delegation_grant_sub.add_parser("create")
    delegation_grant_create.add_argument("--issuer", required=True)
    delegation_grant_create.add_argument("--binding-owner", required=True)
    delegation_grant_create.add_argument(
        "--authority-class", choices=["read", "review"], default="read"
    )
    delegation_grant_create.add_argument("--max-ttl-seconds", required=True, type=int)
    delegation_grant_create.add_argument(
        "--reason",
        choices=[
            "operator_action",
            "security_incident",
            "policy_changed",
            "credential_rotation",
            "client_disabled",
        ],
        default="operator_action",
    )
    delegation_grant_create.add_argument("--json", action="store_true")
    delegation_grant_revoke = delegation_grant_sub.add_parser("revoke")
    delegation_grant_revoke.add_argument("--issuer", required=True)
    delegation_grant_revoke.add_argument("--binding-owner", required=True)
    delegation_grant_revoke.add_argument(
        "--authority-class", choices=["read", "review"], default="read"
    )
    delegation_grant_revoke.add_argument(
        "--reason",
        choices=[
            "operator_action",
            "security_incident",
            "policy_changed",
            "credential_rotation",
            "client_disabled",
        ],
        required=True,
    )
    delegation_grant_revoke.add_argument("--json", action="store_true")
    delegation_grant_list = delegation_grant_sub.add_parser("list")
    delegation_grant_list.add_argument("--issuer", default=None)
    delegation_grant_list.add_argument(
        "--authority-class", choices=["read", "review"], default=None
    )
    delegation_grant_list.add_argument("--json", action="store_true")
    portal_enrollment_parser = sub.add_parser(
        "portal-enrollment",
        help="Manage fixed Portal installation enrollment authority (owner DB only).",
    )
    portal_enrollment_sub = portal_enrollment_parser.add_subparsers(
        dest="portal_enrollment_command", required=True
    )
    portal_enrollment_terminate = portal_enrollment_sub.add_parser(
        "terminate", help="Irreversibly terminate one enrolled installation."
    )
    portal_enrollment_terminate.add_argument("--installation", required=True)
    portal_enrollment_terminate.add_argument(
        "--reason",
        choices=["operator_action", "security_incident", "client_disabled"],
        required=True,
    )
    portal_enrollment_terminate.add_argument("--json", action="store_true")
    bootstrap_parser.add_argument(
        "--scopes",
        default="read,write,admin,export",
        help="Comma-separated scopes for the bootstrap key: read, write, review, "
        "export, admin (default: read,write,admin,export). `admin` is a "
        "super-scope and already satisfies `review`.",
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
    bootstrap_parser.add_argument(
        "--memory-profile",
        default=None,
        help="Optional enabled profile slug or UUID to bind immutably to the bootstrap key.",
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
    promote_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate both promotion lanes without writing state or audit events.",
    )

    reconcile_parser = sub.add_parser(
        "reconcile-promotion",
        help="Request bounded promotion reconciliation (issue #155 backstop) for "
        "one tenant (--tenant) or one bounded all-tenant continuation page: "
        "enqueues the tenant-scoped "
        "promotion.reconcile chain that repairs missing/dead targeted "
        "evaluation work. The all-tenant form advances one durable bounded "
        "tenant page; repeat with the printed --request-id until complete. "
        "Never a synchronous scan or bulk promotion.",
    )
    reconcile_parser.add_argument(
        "--tenant",
        default=None,
        help="Restrict the request to one tenant. Without it, process one bounded "
        "restart-safe tenant page.",
    )
    reconcile_parser.add_argument(
        "--reason",
        choices=["operator_request", "provider_recovery"],
        default="operator_request",
        help="operator_request: promotion policy/config changed; reconcile this "
        "tenant (the honest path for tenant_config changes made by direct SQL, "
        "which the service cannot observe). provider_recovery: additionally "
        "re-enqueue the async classification contract for live proposals whose "
        "evidence never bound — request only after the provider is available "
        "again; no provider call happens inline.",
    )
    reconcile_parser.add_argument(
        "--request-id",
        default=None,
        help="Stable identity for a durable tenant request. Replays report active or "
        "completed; a failed identity requires a fresh --request-id.",
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
    backfill_parser.add_argument(
        "--profile",
        default=None,
        help="Target profile key. Uses queue-backed profile backfill (recommended).",
    )
    backfill_parser.add_argument(
        "--force",
        action="store_true",
        help="Re-enqueue already-ready rows for --profile.",
    )

    profiles_parser = sub.add_parser(
        "embedding-profiles", help="Manage deployment-global embedding profiles."
    )
    profiles_sub = profiles_parser.add_subparsers(dest="profiles_command", required=True)
    profiles_sub.add_parser("list", help="List profiles and coverage.")
    create_profile = profiles_sub.add_parser("create", help="Create a candidate profile.")
    create_profile.add_argument("--key", required=True)
    create_profile.add_argument("--provider", required=True)
    create_profile.add_argument("--model", required=True)
    create_profile.add_argument("--dimensions", required=True, type=int)
    ensure_profile = profiles_sub.add_parser("ensure-index", help="Ensure its HNSW index.")
    ensure_profile.add_argument("profile_key")
    activate_profile_parser = profiles_sub.add_parser("activate", help="Activate a profile.")
    activate_profile_parser.add_argument("profile_key")
    activate_profile_parser.add_argument("--force", action="store_true")
    activate_profile_parser.add_argument("--threshold", type=float, default=None)
    retire_profile_parser = profiles_sub.add_parser("retire", help="Retire a candidate profile.")
    retire_profile_parser.add_argument("profile_key")

    worker_parser = sub.add_parser(
        "worker",
        help="Run the background job worker. Polls the jobs table and processes "
        "embedding.generate / conflict.check / classification.refine / "
        "promotion.path_a / promotion.evaluate / retention.sweep jobs off the "
        "request path. The service still works without a worker; semantic "
        "recall, LLM classification refinement, and semantic conflict "
        "detection lag until jobs are processed.",
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
        help="Seconds to wait before another claim when the queue is idle or the worker loop "
        "errors (default: ENGRAM_JOB_POLL_INTERVAL_SECONDS).",
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

    # --- setup-embeddings ---------------------------------------------------
    setup_parser = sub.add_parser(
        "setup-embeddings",
        help="Validate the embedding provider configuration by generating a "
        "test embedding. Exits 0 on success, 1 on failure with a diagnostic "
        "message. Run this after configuring ENGRAM_EMBEDDING_PROVIDER, "
        "ENGRAM_OPENAI_API_KEY, and ENGRAM_OPENAI_BASE_URL.",
    )
    setup_parser.add_argument(
        "--text",
        default="The quick brown fox jumps over the lazy dog.",
        help="Text to embed for the validation test (default: a pangram).",
    )

    # --- usage-report ---------------------------------------------------------
    usage_report_parser = sub.add_parser(
        "usage-report",
        help="Dogfood usage/metering report (ENG-METER-001): candidate funnel, "
        "provider economics, retrieval, worker, and storage stats derived from "
        "the append-only usage_events ledger. Observability only — never an "
        "invoice or authoritative billable usage.",
    )
    usage_report_parser.add_argument(
        "--tenant",
        default=None,
        help="Restrict the report to a single tenant id. Default: all tenants.",
    )
    usage_report_parser.add_argument(
        "--since",
        default=None,
        help="ISO-8601 window start. Default: 7 days ago.",
    )
    usage_report_parser.add_argument(
        "--until",
        default=None,
        help="ISO-8601 window end. Default: now.",
    )
    usage_report_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit stable machine-readable JSON instead of a human-readable report.",
    )

    # --- doctor -----------------------------------------------------------------
    doctor_parser = sub.add_parser(
        "doctor",
        help="Read-only automatic-memory-loop doctor (ENG-LOOP-001A): composes "
        "health, identity, usage-report, review, recall, and Context Receipt "
        "evidence into one bounded report answering whether Engram and its "
        "automatic memory loop are healthy, degraded, unhealthy, or "
        "unobservable. Never mutates memory, configuration, or queues. "
        "Reads the API key only from ENGRAM_API_KEY.",
    )
    doctor_parser.add_argument(
        "--base-url",
        default=None,
        help="Engram API URL. Default: ENGRAM_BASE_URL, else a loopback URL on "
        "the configured service port.",
    )
    doctor_parser.add_argument(
        "--tenant",
        default=None,
        help="Tenant UUID for database-level evidence. Default: the tenant "
        "returned by /whoami. When identity cannot be resolved, deployment-wide "
        "non-content counts may be reported and tenant-specific checks are "
        "marked unknown.",
    )
    doctor_parser.add_argument(
        "--since",
        default=None,
        help="Timezone-aware ISO-8601 window start. Default: 24 hours before --until.",
    )
    doctor_parser.add_argument(
        "--until",
        default=None,
        help="Timezone-aware ISO-8601 window end. Default: now (UTC).",
    )
    doctor_parser.add_argument(
        "--database-url",
        default=None,
        help="Operator database URL. Default: ENGRAM_OWNER_DATABASE_URL, then "
        "ENGRAM_DATABASE_URL. Accepts postgresql+asyncpg:// or postgresql:// "
        "schemes. Never echoed, serialized, or logged.",
    )
    doctor_parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=None,
        help="Finite positive HTTP/diagnostic timeout in seconds (default: 10).",
    )
    doctor_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit only the stable engram.doctor JSON report. Human-readable "
        "output remains the default.",
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
                    db_url, label=args.label, scopes=args.scopes, force=args.force,
                    memory_profile=args.memory_profile,
                )
            )
        )
    elif args.command == "service-client":
        from engram.config import settings

        if not settings.owner_database_url:
            print(
                "ERROR: ENGRAM_OWNER_DATABASE_URL is required for service-client management.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        raise SystemExit(asyncio.run(_run_service_client(args, settings.owner_database_url)))
    elif args.command == "delegation-grant":
        from engram.config import settings

        if not settings.owner_database_url:
            print(
                "ERROR: ENGRAM_OWNER_DATABASE_URL is required for delegation-grant management.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        raise SystemExit(asyncio.run(_run_delegation_grant(args, settings.owner_database_url)))
    elif args.command == "portal-enrollment":
        from engram.config import settings

        if not settings.owner_database_url:
            print(
                "ERROR: ENGRAM_OWNER_DATABASE_URL is required for Portal enrollment management.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        raise SystemExit(
            asyncio.run(_run_portal_enrollment(args, settings.owner_database_url))
        )
    elif args.command == "promote-proposed":
        raise SystemExit(asyncio.run(_run_promotion(args.tenant, args.limit, dry_run=args.dry_run)))
    elif args.command == "reconcile-promotion":
        raise SystemExit(
            asyncio.run(
                _run_reconciliation_request(
                    args.tenant, reason=args.reason, request_id=args.request_id
                )
            )
        )
    elif args.command == "backfill-embeddings":
        from engram.embeddings import MAX_PROVIDER_BATCH_SIZE

        if args.batch_size < 1:
            parser.error("--batch-size must be a positive integer")
        if args.batch_size > MAX_PROVIDER_BATCH_SIZE:
            parser.error(
                f"--batch-size must be <= {MAX_PROVIDER_BATCH_SIZE} "
                "(provider per-request input limit)"
            )
        if args.profile is not None:
            raise SystemExit(
                asyncio.run(
                    _run_profile_backfill(
                        args.profile, tenant_id=args.tenant, limit=args.limit, force=args.force
                    )
                )
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
    elif args.command == "embedding-profiles":
        raise SystemExit(asyncio.run(_run_embedding_profiles(args)))
    elif args.command == "worker":
        _configure_worker_logging()
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
    elif args.command == "setup-embeddings":
        raise SystemExit(asyncio.run(_run_setup_embeddings(args.text)))
    elif args.command == "usage-report":
        raise SystemExit(
            asyncio.run(
                _run_usage_report(
                    tenant=args.tenant,
                    since=args.since,
                    until=args.until,
                    as_json=args.json,
                )
            )
        )
    elif args.command == "doctor":
        from engram.config import settings
        from engram.doctor import (
            DEFAULT_TIMEOUT_SECONDS,
            parse_iso8601,
            resolve_base_url,
            resolve_database_url,
            validate_timeout_seconds,
        )

        timeout_seconds = DEFAULT_TIMEOUT_SECONDS
        if args.timeout_seconds is not None:
            try:
                timeout_seconds = validate_timeout_seconds(args.timeout_seconds)
            except ValueError as exc:
                parser.error(str(exc))

        since_dt = None
        if args.since is not None:
            try:
                since_dt = parse_iso8601(args.since, param_name="--since")
            except ValueError as exc:
                parser.error(str(exc))

        until_dt = None
        if args.until is not None:
            try:
                until_dt = parse_iso8601(args.until, param_name="--until")
            except ValueError as exc:
                parser.error(str(exc))

        if args.tenant is not None:
            try:
                uuid.UUID(args.tenant)
            except ValueError:
                parser.error(f"--tenant must be a valid UUID (got {args.tenant!r})")

        base_url = resolve_base_url(args.base_url, settings_obj=settings)
        doctor_database_url = resolve_database_url(args.database_url)

        raise SystemExit(
            asyncio.run(
                _run_doctor(
                    base_url=base_url,
                    tenant=args.tenant,
                    since=since_dt,
                    until=until_dt,
                    timeout_seconds=timeout_seconds,
                    database_url=doctor_database_url,
                    as_json=args.json,
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
            f"--baseline cutoff {baseline!r} not found in migrations ({', '.join(all_names)})"
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
            row["filename"] for row in await conn.fetch("SELECT filename FROM schema_migrations")
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
                    "ERROR: the 'memory_items' table already exists but no migrations are tracked.",
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
    """Parse a comma-separated scope string into a validated, canonical list.

    Raises ``ValueError`` if any scope is unknown or the list is empty (unlike
    the JSON admin API, an explicitly empty scope list isn't meaningful for a
    comma-separated CLI flag). Delegates validation, de-duplication, and
    canonical ordering to :func:`engram.auth.canonicalize_scopes` — the same
    function the admin API's key-issuance endpoint uses (V2-BL-004), so both
    paths reject unknown scopes and order valid ones identically.
    """
    from engram.auth import canonicalize_scopes

    scopes = [s.strip() for s in raw.split(",") if s.strip()]
    if not scopes:
        raise ValueError("at least one scope is required")
    return canonicalize_scopes(scopes)


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
    database_url: str, *, label: str, scopes: str, force: bool = False,
    memory_profile: str | None = None,
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
    transaction = conn.transaction()
    await transaction.start()
    try:
        row = await conn.fetchrow(
            "SELECT CAST(t.id AS TEXT) AS tenant_id, "
            "       CAST(p.id AS TEXT) AS principal_id, "
            "       p.internal_key AS internal_key "
            "FROM tenants t "
            "JOIN principals p "
            "  ON p.tenant_id = t.id AND p.name = $1 "
            "WHERE t.slug = $2 FOR UPDATE OF p",
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
            await transaction.rollback()
            return 1

        # Fail-closed: the seed admin principal must be an ordinary principal
        # (internal_key NULL). A future seed change that makes it internal would
        # make it non-credentialable — refuse rather than silently issuing a
        # key that cannot authenticate.
        if row["internal_key"] is not None:
            print(
                "ERROR: the seeded default/admin principal is an internal "
                "principal and cannot receive API keys.",
                file=sys.stderr,
            )
            await transaction.rollback()
            return 1

        existing = await conn.fetchval(
            "SELECT COUNT(*) FROM api_keys WHERE principal_id = $1::uuid AND revoked_at IS NULL",
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
            await transaction.rollback()
            return 1

        profile = None
        if memory_profile is not None:
            profile = await conn.fetchrow(
                "SELECT p.id::text AS id, p.slug, p.active_revision_id::text AS revision_id, r.version "
                "FROM memory_profiles p JOIN memory_profile_revisions r "
                "ON r.id = p.active_revision_id AND r.profile_id = p.id AND r.tenant_id = p.tenant_id "
                "WHERE p.tenant_id = $1::uuid AND p.disabled_at IS NULL "
                "AND (p.slug = $2 OR p.id::text = $2) FOR UPDATE OF p",
                row["tenant_id"], memory_profile,
            )
            if profile is None:
                print("ERROR: memory profile was not found, enabled, and valid for the default tenant.", file=sys.stderr)
                await transaction.rollback()
                return 2

        inserted_key_id = await conn.fetchval(
            "INSERT INTO api_keys "
            "  (tenant_id, principal_id, key_hash, key_id, secret_digest, "
            "   digest_algorithm, scopes, label, memory_profile_id, created_at) "
            "VALUES ($1::uuid, $2::uuid, NULL, $3, $4, $5, $6, $7, $8::uuid, now()) RETURNING id::text",
            row["tenant_id"],
            row["principal_id"],
            material.key_id,
            material.secret_digest,
            material.digest_algorithm,
            list(material.scopes),
            material.label,
            profile["id"] if profile else None,
        )
        if profile is not None:
            await conn.execute(
                "INSERT INTO memory_profile_events "
                "(tenant_id, profile_id, revision_id, actor_principal_id, event_type, reason, details) "
                "VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, 'profile_bound_at_key_issuance', "
                "'Bootstrap API key issuance', "
                "jsonb_build_object('api_key_id', $5::text, 'label', $6::text))",
                row["tenant_id"], profile["id"], profile["revision_id"], row["principal_id"],
                inserted_key_id, material.label,
            )
        await transaction.commit()
    except Exception:
        await transaction.rollback()
        print(
            "ERROR: bootstrap API key issuance failed; no key was created.",
            file=sys.stderr,
        )
        return 1
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
    if profile is not None:
        print(f"memory_profile: {profile['slug']} (revision {profile['version']})")
    print()
    print(
        "Store this key securely. Only a deterministic digest of the secret is "
        "persisted (no plaintext, no bcrypt hash). To revoke or rotate, see "
        "docs/deployment.md (Auth > Rotate or revoke a key).",
        file=sys.stderr,
    )
    return 0


async def _run_portal_enrollment(args: argparse.Namespace, database_url: str) -> int:
    """Terminate fixed Portal authority through the owner-only SQL function."""
    import json

    import asyncpg

    from engram.migrations import normalize_asyncpg_url

    try:
        installation = uuid.UUID(args.installation)
    except ValueError:
        print("ERROR: invalid Portal installation reference", file=sys.stderr)
        return 1

    conn: asyncpg.Connection | None = None
    try:
        conn = await asyncpg.connect(normalize_asyncpg_url(database_url))
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM terminate_portal_installation_enrollment($1,$2,$3)",
                installation,
                args.reason,
                "operator-cli",
            )
        if row is None or row["error_code"] is not None:
            code = "TERMINATION_FAILED" if row is None else row["error_code"]
            print(f"ERROR: Portal enrollment termination failed ({code})", file=sys.stderr)
            return 1
        payload = {
            "status": row["enrollment_status"],
            "credential_generation": row["credential_generation"],
            "terminated": row["terminated"],
        }
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        else:
            print("terminated" if row["terminated"] else "already terminated")
        return 0
    except asyncpg.PostgresError:
        print("ERROR: Portal enrollment termination failed", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            await conn.close()


async def _run_service_client(args: argparse.Namespace, database_url: str) -> int:
    """Owner-only service-client management; stdout is the only secret sink."""
    import json

    import asyncpg

    from engram.migrations import normalize_asyncpg_url
    from engram.service_auth import (
        canonicalize_service_permissions,
        digest_service_secret,
        generate_service_credential,
        parse_service_credential,
        validate_service_client_display_name,
        validate_service_client_slug,
        validate_service_credential_expiry,
        validate_service_credential_label,
    )

    async def event(conn: asyncpg.Connection, client_id: str, event_type: str) -> None:
        await conn.execute(
            "INSERT INTO service_provisioning_events "
            "(service_client_id, event_type, outcome, request_id, details) "
            "VALUES ($1::uuid, $2, 'success', 'operator-cli', '{}'::jsonb)",
            client_id,
            event_type,
        )

    # Validate operator input before opening an owner connection. This makes
    # malformed create commands fail closed without touching the database.
    try:
        if args.service_client_command == "create":
            args.slug = validate_service_client_slug(args.slug)
            args.display_name = validate_service_client_display_name(args.display_name)
            args.permission = canonicalize_service_permissions(
                args.permission or ["tenant.provision", "principal.provision"]
            )
        elif args.service_client_command == "rotate-key":
            args.label = validate_service_credential_label(args.label)
            args.expires_at = validate_service_credential_expiry(args.expires_at)
        elif args.service_client_command == "set-permissions":
            args.permission = canonicalize_service_permissions(args.permission)
    except ValueError:
        print("ERROR: invalid service client input", file=sys.stderr)
        return 1

    conn: asyncpg.Connection | None = None
    try:
        conn = await asyncpg.connect(normalize_asyncpg_url(database_url))
        command = args.service_client_command
        async with conn.transaction():
            if command == "create":
                permissions = args.permission
                credential = generate_service_credential()
                parsed = parse_service_credential(credential)
                row = await conn.fetchrow(
                    "INSERT INTO service_clients (slug, display_name, permissions) VALUES ($1, $2, $3) "
                    "RETURNING id::text AS id, slug, display_name",
                    args.slug,
                    args.display_name,
                    permissions,
                )
                await conn.execute(
                    "INSERT INTO service_client_credentials "
                    "(service_client_id, key_id, secret_digest, digest_algorithm) "
                    "VALUES ($1::uuid, $2, $3, 'sha256')",
                    row["id"],
                    parsed.key_id,
                    digest_service_secret(parsed.secret),
                )
                await event(conn, row["id"], "service_client.created")
                await event(conn, row["id"], "service_credential.created")
                payload = {
                    "id": row["id"], "slug": row["slug"], "display_name": row["display_name"],
                    "permissions": permissions, "credential": credential,
                }
                if args.json:
                    print(json.dumps(payload, sort_keys=True))
                else:
                    print(f"service_client_id: {row['id']}\ncredential: {credential}")
                return 0
            portal_enrollment_available = await conn.fetchval(
                "SELECT to_regclass('portal_installation_enrollment_clients') IS NOT NULL"
            )
            enrolled = False
            if portal_enrollment_available:
                enrolled = await conn.fetchval(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM portal_installation_enrollment_clients enrolled "
                    "JOIN service_client_credentials credential "
                    "ON credential.service_client_id=enrolled.service_client_id "
                    "WHERE (enrolled.service_client_id::text=$1 "
                    "OR EXISTS (SELECT 1 FROM service_clients client "
                    "WHERE client.id=enrolled.service_client_id AND client.slug=$1) "
                    "OR credential.id::text=$1 OR credential.key_id=$1))",
                    args.credential if command == "revoke-key" else args.client,
                )
            if enrolled:
                print(
                    "ERROR: enrolled Portal authority is immutable. Use "
                    "`engram portal-enrollment terminate`.",
                    file=sys.stderr,
                )
                return 1
            if command == "rotate-key":
                row = await conn.fetchrow(
                    "SELECT id::text AS id FROM service_clients WHERE id::text = $1 OR slug = $1 FOR UPDATE",
                    args.client,
                )
                if row is None:
                    print("ERROR: service client not found", file=sys.stderr)
                    return 1
                credential = generate_service_credential()
                parsed = parse_service_credential(credential)
                credential_id = await conn.fetchval(
                    "INSERT INTO service_client_credentials "
                    "(service_client_id, key_id, secret_digest, digest_algorithm, label, expires_at) "
                    "VALUES ($1::uuid, $2, $3, 'sha256', $4, $5::timestamptz) RETURNING id::text",
                    row["id"], parsed.key_id, digest_service_secret(parsed.secret), args.label, args.expires_at,
                )
                await event(conn, row["id"], "service_credential.created")
                payload = {"credential_id": credential_id, "credential": credential}
                print(json.dumps(payload, sort_keys=True) if args.json else f"credential: {credential}")
                return 0
            if command == "revoke-key":
                row = await conn.fetchrow(
                    "UPDATE service_client_credentials SET status = 'revoked', revoked_at = COALESCE(revoked_at, now()) "
                    "WHERE (id::text = $1 OR key_id = $1) AND status = 'active' "
                    "RETURNING service_client_id::text AS service_client_id",
                    args.credential,
                )
                if row is not None:
                    await event(conn, row["service_client_id"], "service_credential.revoked")
                    print("revoked")
                    return 0
                known = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM service_client_credentials "
                    "WHERE id::text = $1 OR key_id = $1)",
                    args.credential,
                )
                if known:
                    print("already revoked")
                    return 0
                print("ERROR: service credential not found", file=sys.stderr)
                return 1
            if command == "set-permissions":
                row = await conn.fetchrow(
                    "SELECT id::text AS id, permissions FROM service_clients "
                    "WHERE id::text = $1 OR slug = $1 FOR UPDATE",
                    args.client,
                )
                if row is None:
                    print("ERROR: service client not found", file=sys.stderr)
                    return 1
                old_permissions = list(row["permissions"])
                changed = old_permissions != args.permission
                if changed:
                    await conn.execute(
                        "UPDATE service_clients SET permissions=$2, updated_at=now() "
                        "WHERE id=$1::uuid",
                        row["id"],
                        args.permission,
                    )
                    await conn.execute(
                        "INSERT INTO service_provisioning_events "
                        "(service_client_id,event_type,outcome,request_id,details) "
                        "VALUES ($1::uuid,'service_client.permissions_changed','success',"
                        "'operator-cli',jsonb_build_object("
                        "'old_permissions',$2::text[],'new_permissions',$3::text[]))",
                        row["id"],
                        old_permissions,
                        args.permission,
                    )
                payload = {
                    "id": row["id"],
                    "permissions": args.permission,
                    "changed": changed,
                }
                if args.json:
                    print(json.dumps(payload, sort_keys=True))
                else:
                    print("updated" if changed else "unchanged")
                return 0
            target_status = {"disable": "disabled", "enable": "active"}[command]
            row = await conn.fetchrow(
                "UPDATE service_clients SET status = $2, disabled_at = "
                "CASE WHEN $2 = 'disabled' THEN COALESCE(disabled_at, now()) ELSE NULL END, updated_at = now() "
                "WHERE (id::text = $1 OR slug = $1) AND status <> $2 RETURNING id::text AS id",
                args.client,
                target_status,
            )
            if row is not None:
                event_type = {
                    "disable": "service_client.disabled",
                    "enable": "service_client.enabled",
                }[command]
                await event(conn, row["id"], event_type)
                print("disabled" if command == "disable" else "enabled")
                return 0
            known = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM service_clients WHERE id::text = $1 OR slug = $1)",
                args.client,
            )
            if known:
                print(f"already {command}d")
                return 0
            print("ERROR: service client not found", file=sys.stderr)
            return 1
    except (ValueError, asyncpg.PostgresError):
        print("ERROR: service client operation failed", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            await conn.close()


async def _run_delegation_grant(args: argparse.Namespace, database_url: str) -> int:
    """Owner-only grant management; never creates or prints credentials."""
    import json

    import asyncpg

    from engram.migrations import normalize_asyncpg_url
    from engram.service_auth import validate_service_client_slug

    try:
        issuer_slug = (
            validate_service_client_slug(args.issuer)
            if args.issuer is not None
            else None
        )
        owner_slug = (
            validate_service_client_slug(args.binding_owner)
            if hasattr(args, "binding_owner")
            else None
        )
        authority_filter = getattr(args, "authority_class", None)
        authority_class = authority_filter or "read"
        maximum_ttl = 60 if authority_class == "review" else 300
        if (
            args.delegation_grant_command == "create"
            and not 30 <= args.max_ttl_seconds <= maximum_ttl
        ):
            raise ValueError("invalid TTL")
        if issuer_slug is not None and owner_slug is not None and issuer_slug == owner_slug:
            raise ValueError("issuer and binding owner must differ")
    except ValueError:
        print("ERROR: invalid delegation grant input", file=sys.stderr)
        return 1

    conn: asyncpg.Connection | None = None
    try:
        conn = await asyncpg.connect(normalize_asyncpg_url(database_url))
        command = args.delegation_grant_command
        if command == "list":
            rows = await conn.fetch(
                "SELECT grant_row.id::text AS id,issuer.slug AS issuer_slug,"
                "owner_client.slug AS binding_owner_slug,"
                "grant_row.authority_class,grant_row.status,"
                "grant_row.max_ttl_seconds,grant_row.created_at,grant_row.updated_at,"
                "grant_row.revoked_at FROM service_delegation_grants grant_row "
                "JOIN service_clients issuer "
                "ON issuer.id=grant_row.issuer_service_client_id "
                "JOIN service_clients owner_client "
                "ON owner_client.id=grant_row.binding_owner_service_client_id "
                "WHERE ($1::text IS NULL OR issuer.slug=$1) "
                "AND ($2::text IS NULL OR grant_row.authority_class=$2) "
                "ORDER BY grant_row.created_at,grant_row.id",
                issuer_slug,
                authority_filter,
            )
            list_payload = [dict(row) for row in rows]
            if args.json:
                print(json.dumps(list_payload, default=str, sort_keys=True))
            else:
                for row in list_payload:
                    print(
                        f"{row['id']} issuer={row['issuer_slug']} "
                        f"binding_owner={row['binding_owner_slug']} "
                        f"authority_class={row['authority_class']} "
                        f"status={row['status']} max_ttl_seconds={row['max_ttl_seconds']}"
                    )
            return 0

        async with conn.transaction():
            clients = await conn.fetch(
                "SELECT id::text AS id,slug,status,permissions FROM service_clients "
                "WHERE slug=ANY($1::text[]) ORDER BY id FOR UPDATE",
                [issuer_slug, owner_slug],
            )
            by_slug = {row["slug"]: row for row in clients}
            if issuer_slug not in by_slug or owner_slug not in by_slug:
                print("ERROR: service client not found", file=sys.stderr)
                return 1
            issuer = by_slug[issuer_slug]
            owner = by_slug[owner_slug]
            portal_enrollment_available = await conn.fetchval(
                "SELECT to_regclass('portal_installation_enrollment_clients') IS NOT NULL"
            )
            enrolled = False
            if portal_enrollment_available:
                enrolled = await conn.fetchval(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM portal_installation_enrollment_clients "
                    "WHERE service_client_id=ANY($1::uuid[]))",
                    [uuid.UUID(issuer["id"]), uuid.UUID(owner["id"])],
                )
            if enrolled:
                print(
                    "ERROR: enrolled Portal grants are immutable. Use "
                    "`engram portal-enrollment terminate`.",
                    file=sys.stderr,
                )
                return 1
            required_permission = (
                "delegation.review.issue"
                if authority_class == "review"
                else "delegation.issue"
            )
            if (
                issuer["status"] != "active"
                or owner["status"] != "active"
                or required_permission not in issuer["permissions"]
            ):
                print("ERROR: delegation grant authority is invalid", file=sys.stderr)
                return 1

            active = await conn.fetchrow(
                "SELECT id::text AS id,max_ttl_seconds,status,created_at,updated_at,"
                "revoked_at FROM service_delegation_grants "
                "WHERE issuer_service_client_id=$1::uuid "
                "AND binding_owner_service_client_id=$2::uuid "
                "AND authority_class=$3 AND status='active' "
                "FOR UPDATE",
                issuer["id"],
                owner["id"],
                authority_class,
            )
            if command == "create":
                if active is not None and active["max_ttl_seconds"] != args.max_ttl_seconds:
                    print(
                        "ERROR: active grant TTL differs; revoke it before creating a new grant",
                        file=sys.stderr,
                    )
                    return 1
                created = active is None
                row = active
                if row is None:
                    row = await conn.fetchrow(
                        "INSERT INTO service_delegation_grants "
                        "(issuer_service_client_id,binding_owner_service_client_id,"
                        "authority_class,max_ttl_seconds) "
                        "VALUES ($1::uuid,$2::uuid,$3,$4) "
                        "RETURNING id::text AS id,max_ttl_seconds,status,created_at,"
                        "updated_at,revoked_at",
                        issuer["id"],
                        owner["id"],
                        authority_class,
                        args.max_ttl_seconds,
                    )
                    await conn.execute(
                        "INSERT INTO service_delegation_events "
                        "(event_type,outcome,issuer_service_client_id,"
                        "binding_owner_service_client_id,grant_id,authority_class,"
                        "request_id,"
                        "reason_code,details) VALUES "
                        "('delegation_grant.created','success',$1::uuid,$2::uuid,"
                        "$3::uuid,$4,'operator-cli',$5,"
                        "jsonb_build_object("
                        "'ttl_seconds',$6::integer,'disposition','created'))",
                        issuer["id"],
                        owner["id"],
                        row["id"],
                        authority_class,
                        args.reason,
                        args.max_ttl_seconds,
                    )
                result_payload: dict[str, Any] = {
                    "id": row["id"],
                    "issuer_slug": issuer_slug,
                    "binding_owner_slug": owner_slug,
                    "authority_class": authority_class,
                    "status": row["status"],
                    "max_ttl_seconds": row["max_ttl_seconds"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "revoked_at": row["revoked_at"],
                    "created": created,
                }
            else:
                if active is None:
                    row = await conn.fetchrow(
                        "SELECT id::text AS id,max_ttl_seconds,status,created_at,"
                        "updated_at,revoked_at FROM service_delegation_grants "
                        "WHERE issuer_service_client_id=$1::uuid "
                        "AND binding_owner_service_client_id=$2::uuid "
                        "AND authority_class=$3 "
                        "ORDER BY created_at DESC LIMIT 1",
                        issuer["id"],
                        owner["id"],
                        authority_class,
                    )
                    if row is None:
                        print("ERROR: delegation grant not found", file=sys.stderr)
                        return 1
                    revoked = False
                else:
                    row = await conn.fetchrow(
                        "UPDATE service_delegation_grants SET status='revoked',"
                        "revoked_at=now(),updated_at=now(),revocation_reason=$2 "
                        "WHERE id=$1::uuid RETURNING id::text AS id,max_ttl_seconds,"
                        "status,created_at,updated_at,revoked_at",
                        active["id"],
                        args.reason,
                    )
                    revoked = True
                    await conn.execute(
                        "INSERT INTO service_delegation_events "
                        "(event_type,outcome,issuer_service_client_id,"
                        "binding_owner_service_client_id,grant_id,authority_class,"
                        "request_id,"
                        "reason_code,details) VALUES "
                        "('delegation_grant.revoked','success',$1::uuid,$2::uuid,"
                        "$3::uuid,$4,'operator-cli',$5,"
                        "jsonb_build_object('disposition','revoked'))",
                        issuer["id"],
                        owner["id"],
                        row["id"],
                        authority_class,
                        args.reason,
                    )
                result_payload = {
                    "id": row["id"],
                    "issuer_slug": issuer_slug,
                    "binding_owner_slug": owner_slug,
                    "authority_class": authority_class,
                    "status": row["status"],
                    "max_ttl_seconds": row["max_ttl_seconds"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "revoked_at": row["revoked_at"],
                    "revoked": revoked,
                }
            if args.json:
                print(json.dumps(result_payload, default=str, sort_keys=True))
            else:
                if result_payload.get("created"):
                    print("created")
                elif result_payload.get("revoked"):
                    print("revoked")
                else:
                    print("unchanged")
            return 0
    except (ValueError, asyncpg.PostgresError):
        print("ERROR: delegation grant operation failed", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            await conn.close()


async def _run_promotion(
    tenant_id: str | None,
    limit: int | None,
    dry_run: bool = False,
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
            result = await auto_promote_proposed_memories(
                session, tid, limit=limit, source="cli", dry_run=dry_run
            )
            print(summarize(result))
            if dry_run:
                blockers = Counter(
                    blocker
                    for candidate in result.candidates
                    for blocker in set(candidate.blockers)
                )
                blocker_text = " ".join(
                    f"{name}={count}" for name, count in sorted(blockers.items())
                )
                print(f"  blockers: {blocker_text or 'none'}")
                detail_limit = 20
                for candidate in result.candidates[:detail_limit]:
                    if candidate.would_promote:
                        eligible_at = (
                            candidate.eligible_at.isoformat() if candidate.eligible_at else None
                        )
                        print(
                            f"  would-promote item_id={candidate.item_id} "
                            f"basis={candidate.selected_basis} "
                            f"eligible_at={eligible_at}"
                        )
                    else:
                        print(
                            f"  blocked item_id={candidate.item_id} kind={candidate.kind} "
                            f"blockers={','.join(candidate.blockers) or 'none'}"
                        )
                omitted = len(result.candidates) - detail_limit
                if omitted > 0:
                    print(f"  ... {omitted} candidate detail rows omitted")
            total_promoted += result.would_promote if dry_run else result.promoted
            total_scanned += result.scanned

        action = "would_promote" if dry_run else "promoted"
        print(f"\nTotal: scanned={total_scanned} {action}={total_promoted}")
        return 0


async def _run_reconciliation_request(
    tenant_id: str | None,
    *,
    reason: str = "operator_request",
    request_id: str | None = None,
    session_factory: Any | None = None,
) -> int:
    """Request bounded promotion reconciliation and print a content-free summary.

    With ``--tenant``, enqueues exactly one ``promotion.reconcile`` chain.
    Without it, advances one durable bounded tenant page and prints the stable
    continuation id (never a synchronous global scan or item fan-out).
    Connecting as the table-owning role (default) bypasses RLS for the
    content-free tenant enumeration and queue insert; every item-level
    discovery/evaluation runs later in the worker under the normal app-role
    tenant context. Returns 0 for accepted or completed idempotent replays, 1
    when a durable request identity previously failed (the operator must use a
    fresh ``--request-id``), and 3 when the reconciliation rollout flag is off
    (the request was a documented no-op).
    """
    import uuid as _uuid

    from engram.config import settings
    from engram.db import owner_session_factory as _default_factory
    from engram.promotion_reconciliation import (
        request_global_reconciliation_window,
        request_reconciliation_chain_result,
    )

    factory = session_factory if session_factory is not None else _default_factory

    async with factory() as session:
        if not settings.promotion_reconciliation_enabled:
            print(
                "promotion reconciliation is disabled "
                "(ENGRAM_PROMOTION_RECONCILIATION_ENABLED=false): "
                "status=not_enqueued; no work requested."
            )
            return 3

        trigger_id = request_id if request_id else f"request:{_uuid.uuid4()}"
        if tenant_id is None:
            window = await request_global_reconciliation_window(
                session,
                reason=reason,
                trigger_id=trigger_id,
            )
            print(
                f"reason={reason} request_id={trigger_id} inspected={window.inspected} "
                f"enqueued={window.enqueued} complete={window.completed}"
            )
            if not window.completed:
                print(
                    "Continuation required: rerun with "
                    f"--reason {reason} --request-id {trigger_id}"
                )
            return 0
        result = await request_reconciliation_chain_result(
            session,
            tenant_id=tenant_id,
            reason=reason,
            trigger_id=trigger_id,
        )
        await session.commit()
        if result.status == "failed":
            print(
                f"tenant={tenant_id} reason={reason} request_id={trigger_id} "
                "status=failed"
            )
            print("This reconciliation request previously failed. Retry with a fresh --request-id.")
            return 1
        if result.status in {"completed", "not_enqueued"}:
            print(
                f"tenant={tenant_id} reason={reason} request_id={trigger_id} "
                f"status={result.status}"
            )
            return 0
        print(
            f"tenant={tenant_id} reason={reason} request_id={trigger_id} "
            f"status={result.status} job_id={result.job_id}"
        )
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


async def _run_profile_backfill(
    profile_key: str,
    *,
    tenant_id: str | None = None,
    limit: int | None = None,
    force: bool = False,
    session_factory: Any | None = None,
) -> int:
    """Enqueue profile-specific backfill work without provider calls."""
    from engram.db import owner_session_factory as default_factory
    from engram.embedding_profiles import enqueue_profile_backfill, get_profile

    factory = session_factory or default_factory
    async with factory() as session:
        profile = await get_profile(session, profile_key)
        result = await enqueue_profile_backfill(
            session, profile, tenant_id=tenant_id, limit=limit, force=force
        )
        print(
            f"profile={profile.profile_key} eligible={result.eligible} "
            f"already_ready={result.already_ready} pending={result.pending} "
            f"failed={result.failed} enqueued={result.enqueued} "
            f"skipped_expired_rejected={result.skipped_expired_rejected}"
        )
    return 0


async def _run_embedding_profiles(
    args: argparse.Namespace,
    *,
    session_factory: Any | None = None,
    owner_engine: Any | None = None,
) -> int:
    from sqlalchemy import func, select

    from engram.config import settings
    from engram.db import owner_engine as default_engine
    from engram.db import owner_session_factory as default_factory
    from engram.embedding_profiles import (
        MAX_WRITABLE_PROFILES,
        activate_profile,
        calculate_coverage,
        ensure_profile_index,
        get_profile,
        retire_profile,
        validate_profile,
    )
    from engram.models import EmbeddingProfile

    factory = session_factory or default_factory
    engine = owner_engine or default_engine
    async with factory() as session:
        command = args.profiles_command
        if command == "list":
            profiles = list(
                (
                    await session.execute(
                        select(EmbeddingProfile).order_by(EmbeddingProfile.created_at)
                    )
                ).scalars()
            )
            for profile in profiles:
                coverage = await calculate_coverage(session, profile)
                print(
                    f"{profile.profile_key} provider={profile.provider} model={profile.model} "
                    f"dimensions={profile.dimensions} state={profile.state} "
                    f"index={profile.index_status}:{profile.index_name or '-'} "
                    f"coverage={coverage.percentage:.2f}% "
                    f"ready={coverage.ready}/{coverage.total_eligible} "
                    f"pending={coverage.pending} failed={coverage.failed} "
                    f"missing={coverage.missing}"
                )
            return 0
        if command == "create":
            writable = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(EmbeddingProfile)
                        .where(EmbeddingProfile.state.in_(("active", "candidate")))
                    )
                ).scalar_one()
            )
            if writable >= MAX_WRITABLE_PROFILES:
                raise ValueError(f"maximum writable profile count is {MAX_WRITABLE_PROFILES}")
            profile = EmbeddingProfile(
                profile_key=args.key,
                provider=args.provider,
                model=args.model,
                dimensions=args.dimensions,
                distance_metric="cosine",
                state="candidate",
                index_status="missing",
                profile_metadata={},
            )
            validate_profile(profile)
            session.add(profile)
            await session.commit()
            print(f"created candidate profile {profile.profile_key} ({profile.id})")
            return 0
        profile = await get_profile(session, args.profile_key)
        if command == "ensure-index":
            await session.commit()
            name = await ensure_profile_index(engine, profile.id)
            print(f"profile={profile.profile_key} index=ready:{name}")
            return 0
        if command == "activate":
            threshold = (
                args.threshold
                if args.threshold is not None
                else settings.embedding_activation_coverage_threshold
            )
            if args.force:
                print(
                    "WARNING: forcing embedding profile activation below coverage threshold",
                    file=sys.stderr,
                )
            coverage = await activate_profile(
                session, profile, threshold=threshold, force=args.force
            )
            print(f"activated {profile.profile_key}; coverage={coverage.percentage:.2f}%")
            return 0
        if command == "retire":
            await retire_profile(session, profile)
            print(f"retired {profile.profile_key}; vectors and index retained")
            return 0
    return 1


def _configure_worker_logging() -> None:
    """Configure logging for the ``engram worker`` CLI path.

    Unlike the API server (which relies on Uvicorn's logging setup), the CLI
    entry point does not initialize Python logging. Without this call the
    worker's INFO-level startup, job-completion, retry, and failure messages
    are invisible in container logs.

    Only the ``engram`` logger is configured — library consumers and other
    loggers are not affected.
    """
    import logging

    from engram.config import settings

    level_name = (settings.log_level or "info").lower()
    level = getattr(logging, level_name.upper(), logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger = logging.getLogger("engram")
    logger.setLevel(level)
    # Avoid duplicate handlers if called more than once (e.g. in tests).
    if not logger.handlers:
        logger.addHandler(handler)
    logger.propagate = False


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


async def _run_setup_embeddings(test_text: str) -> int:
    """Validate the embedding provider configuration.

    Checks that:
    1. The provider is not 'none' (embeddings enabled).
    2. An API key is configured.
    3. A base URL is configured (the most common misconfiguration — without
       it, the OpenAI SDK defaults to api.openai.com).
    4. The provider accepts the test text and returns a vector of the
       expected dimension.
    """
    from engram.config import settings

    print("Engram embedding configuration check")
    print("=" * 50)

    # 1. Provider
    provider = settings.embedding_provider
    print(f"  provider: {provider}")
    if provider == "none":
        print("\n  FAIL: ENGRAM_EMBEDDING_PROVIDER is 'none'.")
        print("  Set it to 'openai' to enable embeddings.")
        print("  Example .env:")
        print("    ENGRAM_EMBEDDING_PROVIDER=openai")
        return 1

    # 2. API key
    api_key = settings.openai_api_key
    if not api_key:
        print("\n  FAIL: No API key configured.")
        print("  Set ENGRAM_OPENAI_API_KEY in your .env.")
        return 1
    key_preview = api_key[:8] + "..." + api_key[-4:] if len(api_key) > 12 else "***"
    print(f"  api_key:  {key_preview}")

    # 3. Base URL
    base_url = settings.openai_base_url
    if not base_url:
        print("\n  WARNING: No base URL configured.")
        print("  Without ENGRAM_OPENAI_BASE_URL, the OpenAI SDK defaults to")
        print("  https://api.openai.com. This will fail with 401 if you are")
        print("  using OpenRouter, DeepInfra, or another OpenAI-compatible provider.")
        print("  Set ENGRAM_OPENAI_BASE_URL in your .env.")
        print("  Example:")
        print("    ENGRAM_OPENAI_BASE_URL=https://openrouter.ai/api/v1")
        print("    ENGRAM_OPENAI_BASE_URL=https://api.deepinfra.com/v1/openai")
    else:
        print(f"  base_url: {base_url}")

    # 4. Dimension
    print(f"  dimensions: {settings.embedding_dim}")

    # 5. Test embedding generation
    print(f'\n  Generating test embedding for: "{test_text[:60]}..."')
    # Best-effort tenant resolution for the embedding_setup usage-telemetry
    # event only — this diagnostic ping is deliberately excluded from normal
    # product-usage totals in the dogfood report (operation=embedding_setup).
    # Never blocks the diagnostic: a lookup failure just means no telemetry.
    setup_tenant_id = None
    try:
        from sqlalchemy import select as _select

        from engram.db import owner_session_factory
        from engram.models import Tenant

        async with owner_session_factory() as _session:
            setup_tenant_id = await _session.scalar(_select(Tenant.id).limit(1))
    except Exception:  # noqa: BLE001 - diagnostic tenant lookup is best-effort
        setup_tenant_id = None
    try:
        from engram.embeddings import generate_embedding

        vec = await generate_embedding(
            test_text,
            tenant_id=setup_tenant_id,
            operation="embedding_setup",
            usage_class="diagnostic",
        )
    except Exception as exc:
        print("\n  FAIL: Embedding generation raised an error:")
        print(f"    {type(exc).__name__}: {exc}")
        if "401" in str(exc) or "AuthenticationError" in type(exc).__name__:
            print("\n  This is an authentication error. Check that:")
            print("  - The API key is valid for the provider")
            print("  - The base_url points to the correct provider endpoint")
            print("  - You are not sending an OpenRouter key to OpenAI (or vice versa)")
        elif "connection" in str(exc).lower() or "timeout" in str(exc).lower():
            print("\n  This is a connection error. Check that:")
            print("  - The base_url is reachable from this host")
            print("  - The model name is correct for the provider")
        return 1

    if vec is None:
        print("\n  FAIL: generate_embedding() returned None.")
        print("  This happens when the provider is 'none'. Check your config.")
        return 1

    if len(vec) != settings.embedding_dim:
        print("\n  FAIL: Dimension mismatch.")
        print(f"  Expected {settings.embedding_dim}, got {len(vec)}.")
        print("  Update ENGRAM_EMBEDDING_DIM or use a different model.")
        return 1

    print(f"\n  SUCCESS: Generated {len(vec)}-dimensional embedding.")
    print(f"  First 5 values: {vec[:5]}")
    print("\n  Embedding configuration is valid.")
    return 0


# --- usage-report ------------------------------------------------------------


def _json_default(value: Any) -> Any:
    """json.dumps ``default=`` for datetime/Decimal values from raw SQL rows."""
    import datetime as _dt
    import decimal

    if isinstance(value, _dt.datetime):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    return str(value)


def _print_human_usage_report(report: dict[str, Any]) -> None:
    cov = report["coverage"]
    funnel = report["candidate_funnel"]
    retrieval = report["retrieval"]
    conflict = report["conflict_economics"]
    storage = report["storage"]

    print("Engram dogfood usage report")
    print("=" * 60)
    print(f"tenant:  {report['tenant_id'] or '(all tenants)'}")
    print(f"schema:  {report['report_schema_version']}")
    print(f"window:  {report['since']}  to  {report['until']}")
    print()
    print("-- Coverage & data quality --")
    print(f"  telemetry_enabled:            {cov['telemetry_enabled']}")
    print(f"  first_event_at:               {cov['first_event_at']}")
    print(f"  last_event_at:                {cov['last_event_at']}")
    print(f"  actual calls w/ tokens:        {cov['pct_provider_calls_with_tokens']}%")
    print(f"  actual calls w/ cost:          {cov['pct_provider_calls_with_cost']}%")
    print(
        "  candidate events w/ ingest:   "
        f"{cov['pct_candidate_events_with_ingest_id']}% "
        f"({cov['legacy_candidate_event_count']} legacy)"
    )
    print(
        f"  active principals:            {cov['active_principals']} "
        f"({cov['active_principals_with_lifecycle_summary']} with lifecycle summaries)"
    )
    for w in cov["warnings"]:
        print(f"  WARNING: {w}")
    print()
    print("-- Candidate funnel --")
    for key in (
        "lifecycle_extracted", "lifecycle_guard_rejected", "lifecycle_classified",
        "lifecycle_parked", "candidate_observations", "candidate_cohort_size",
        "candidate_ingests", "candidate_ingests_with_outcomes",
        "candidate_ingests_unresolved", "legacy_correlation_candidates",
        "ingest_identity_coverage_pct",
        "logical_candidates", "unresolved_candidates", "remember_attempts",
        "created", "deduped", "superseded", "failed", "new_memory_writes",
        "failed_attempts", "successful_attempts", "retry_successes_in_window",
        "attempts_per_cohort_candidate_avg",
        "flat_candidate_units", "kib_candidate_units",
    ):
        print(f"  {key:32s} {funnel[key]}")
    print(
        f"  candidate_bytes p50/p90/p99:   "
        f"{funnel['candidate_bytes_p50']}/{funnel['candidate_bytes_p90']}/"
        f"{funnel['candidate_bytes_p99']}"
    )
    print()
    print("-- Breakdown by source type --")
    for row in report["by_source_type"]:
        print(f"  {row['source_type']:16s} observed={row['candidate_observations']:<8} "
              f"bytes={row['candidate_bytes']:<10} kib_units={row['kib_candidate_units']}")
    print()
    print("-- Provider economics (usage class/operation/host/model) --")
    print(
        "  all operations/calls:          "
        f"{report['all_provider_operations']}/{report['all_actual_provider_calls']}"
    )
    print(f"  non-attempted failures:        {report['all_non_attempted_failures']}")
    print(f"  disabled operations:           {report['all_disabled_operations']}")
    print(
        "  product operations/calls:      "
        f"{report['product_provider_operations']}/"
        f"{report['product_actual_provider_calls']}"
    )
    print(
        "  maintenance operations/calls:  "
        f"{report['maintenance_provider_operations']}/"
        f"{report['maintenance_actual_provider_calls']}"
    )
    print(
        "  diagnostic operations/calls:   "
        f"{report['diagnostic_provider_operations']}/"
        f"{report['diagnostic_actual_provider_calls']}"
    )
    for row in report["provider_economics"]:
        disabled = row.get("disabled_n") or 0
        print(
            f"  {row['usage_class']:18s} {row['operation']:24s} "
            f"{row['provider_host'] or '-':22s} {row['model'] or '-':20s} "
            f"operations={row['calls']:<6} calls={row.get('actual_calls', 0):<6} "
            f"disabled={disabled:<4} ok={row['successes']:<6} fail={row['failures']:<4} "
            f"fallback={row.get('application_fallbacks', 0):<4} "
            f"tokens={row['total_tokens']:<8} "
            f"cost=${row['reported_cost_usd'] or 0:.4f} "
            f"cost_cov={row['reported_cost_coverage_pct']}%"
        )
    print()
    print("-- Conflict economics --")
    print(f"  conflict operations:           {conflict['conflict_classifications']}")
    print(f"  actual conflict LLM calls:     {conflict['conflict_actual_calls']}")
    print(
        "  per 1000 candidate obs:        "
        f"{conflict['conflict_calls_per_1000_candidate_observations']}"
    )
    print(f"  verdict distribution:         {conflict['verdict_distribution']}")
    print(f"  failures:                      {conflict['failed_calls']}")
    print(f"  application fallbacks:         {conflict['application_fallback_count']}")
    print()
    print("-- Retrieval --")
    for row in retrieval["by_mode"]:
        print(f"  {row['operation']:18s} requests={row['requests']:<6} "
              f"items={row['item_total']:<8} bytes={row['byte_total']}")
    print(f"  query-embedding operations:    {retrieval['query_embedding_calls']}")
    print(f"  actual query-embedding calls:  {retrieval['query_embedding_actual_calls']}")
    print(f"  query-embedding tokens:        {retrieval['query_embedding_tokens']}")
    print(
        "  semantic_queries/new_write:    "
        f"{retrieval['semantic_queries_per_new_memory_write']}"
    )
    print(f"  retrievals/active_principal:   {retrieval['retrievals_per_active_principal']}")
    print()
    print("-- Worker/queue --")
    for row in report["worker"]["by_job_type_status"]:
        print(f"  {row['job_type']:24s} {row['status']:12s} {row['n']}")
    print(f"  oldest_pending_age_seconds:    {report['worker']['oldest_pending_age_seconds']}")
    print()
    print("-- Storage --")
    for key in (
        "memory_items_total", "memory_items_live", "memory_items_active",
        "memory_items_proposed", "memory_items_disputed", "memory_items_rejected",
        "memory_items_archived", "embeddings_ready", "embeddings_pending",
        "embeddings_failed", "embedding_profiles_total", "embedding_profiles_writable",
        "database_bytes", "bytes_per_retained_memory", "bytes_per_ready_embedding",
    ):
        print(f"  {key:28s} {storage[key]}")


async def _run_usage_report(
    *,
    tenant: str | None,
    since: str | None,
    until: str | None,
    as_json: bool,
) -> int:
    """Build and print the dogfood usage report (ENG-METER-001 / ENG-METER-002).

    Uses the owner database URL for cross-tenant reporting (bypasses RLS,
    matching ``_run_promotion``/``_run_backfill``); every query still filters
    explicitly by ``--tenant`` when given, so results are correct under RLS too.
    """
    import json as _json_module
    from datetime import UTC, datetime

    from engram.db import owner_session_factory
    from engram.usage_report import build_report

    since_dt = datetime.fromisoformat(since).astimezone(UTC) if since else None
    until_dt = datetime.fromisoformat(until).astimezone(UTC) if until else None

    async with owner_session_factory() as session:
        report = await build_report(session, tenant_id=tenant, since=since_dt, until=until_dt)

    if as_json:
        print(_json_module.dumps(report, default=_json_default, indent=2, sort_keys=True))
    else:
        _print_human_usage_report(report)
    return 0


# --- doctor ------------------------------------------------------------------


async def _run_doctor(
    *,
    base_url: str,
    tenant: str | None,
    since: datetime | None,
    until: datetime | None,
    timeout_seconds: float,
    database_url: str | None,
    as_json: bool,
) -> int:
    """Build and print the read-only automatic-memory-loop doctor report.

    Reads the API key only from ``ENGRAM_API_KEY`` — there is no ``--api-key``
    flag, so the secret never appears in shell history or the process list.
    Never mutates memory, configuration, or queues (ENG-LOOP-001A).
    """
    import os

    from engram.doctor import render_human, run_doctor

    api_key = os.environ.get("ENGRAM_API_KEY")
    try:
        report = await run_doctor(
            base_url=base_url,
            api_key=api_key,
            tenant=tenant,
            since=since,
            until=until,
            timeout_seconds=timeout_seconds,
            database_url=database_url,
        )
    except Exception as exc:  # noqa: BLE001 - the report itself failing to build is exit 2
        print(
            f"ERROR: doctor report could not be constructed safely ({type(exc).__name__}).",
            file=sys.stderr,
        )
        return 2

    if as_json:
        print(report.model_dump_json(indent=2, by_alias=True))
    else:
        print(render_human(report))
    return report.exit_code


if __name__ == "__main__":
    main()
