"""Run the Compose-backed CI verification flow with visible sections."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Final

import asyncpg

# JUnit XML lands here so the workflow can copy it out of the stopped container
# and upload it as an artifact. Failures are then readable as structured test
# results instead of by scrolling a Compose log interleaved with Postgres
# output. Overridable so a local `make compose-ci` can redirect it.
RESULTS_DIR: Final[Path] = Path(os.environ.get("ENGRAM_CI_RESULTS_DIR", "/app/test-results"))

# Per-test wall-clock ceiling. The slowest test in the suite runs ~2.5s, so 60s
# is ~24x headroom and only trips on a genuine hang. The concurrency suites
# (worker dedup/auto-supersede/flagging, promotion review/feedback, manual
# invalidation) are the realistic deadlock sources; before this a hang consumed
# the full 30-minute job timeout and never named the offending test.
#
# The `signal` method is deliberate: `thread` hard-exits via os._exit(), which
# would abort pytest before it writes the JUnit XML above. `signal` raises
# inside the test, so the run reports the culprit, writes results, and carries
# on to the remaining tests.
TEST_TIMEOUT_SECONDS: Final[int] = 60

DB_TABLES: Final[tuple[str, ...]] = (
    "tenants",
    "workspaces",
    "principals",
    "memory_items",
    "memory_embeddings",
    "kg_triples",
    "tenant_config",
    "classification_runs",
    "usage_events",
    "context_receipts",
)

# Tables that must have FORCE ROW LEVEL SECURITY (ENG-AUD-002).
RLS_FORCED_TABLES: Final[tuple[str, ...]] = (
    "memory_items",
    "memory_embeddings",
    "item_events",
    "recall_logs",
    "api_keys",
    "workspace_members",
    "jobs",
    "classification_runs",
    "usage_events",
    "context_receipts",
)


def _section(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def _run(*args: str, env: dict[str, str] | None = None) -> None:
    print(f"+ {' '.join(args)}", flush=True)
    subprocess.run(args, check=True, env=env)


def _pytest_flags(suite: str) -> tuple[str, ...]:
    """Return the reporting/timeout flags shared by every pytest invocation."""
    return (
        f"--timeout={TEST_TIMEOUT_SECONDS}",
        "--timeout-method=signal",
        f"--junitxml={RESULTS_DIR / f'{suite}.xml'}",
    )


async def _verify_database() -> None:
    from engram.migrations import normalize_asyncpg_url

    url = normalize_asyncpg_url(os.environ["ENGRAM_DATABASE_URL"])
    conn = await asyncpg.connect(url)
    try:
        version = await conn.fetchval(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        )
        if version is None:
            raise RuntimeError("pgvector extension is not installed")

        missing = []
        for table in DB_TABLES:
            exists = await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f"public.{table}")
            if not exists:
                missing.append(table)
        if missing:
            raise RuntimeError(f"migration incomplete; missing tables: {', '.join(missing)}")

        tenant_count = await conn.fetchval("SELECT COUNT(*) FROM tenants")
        if tenant_count == 0:
            raise RuntimeError("migration did not seed tenants")

        print(f"pgvector version: {version}", flush=True)
        print(f"seed tenants: {tenant_count}", flush=True)

        # ENG-AUD-002: the non-owner application role must exist with no
        # BYPASSRLS, and tenant-scoped tables must FORCE RLS.
        app_role = await conn.fetchrow(
            "SELECT rolname, rolbypassrls, rolsuper FROM pg_roles WHERE rolname = 'engram_app'"
        )
        if app_role is None:
            raise RuntimeError("engram_app role was not created (migration 003 missing?)")
        if app_role["rolbypassrls"]:
            raise RuntimeError("engram_app must not have BYPASSRLS")
        if app_role["rolsuper"]:
            raise RuntimeError("engram_app must not be a superuser")

        not_forced = []
        for table in RLS_FORCED_TABLES:
            forced = await conn.fetchval(
                """
                SELECT c.relforcerowsecurity
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname = $1
                """,
                table,
            )
            if not forced:
                not_forced.append(table)
        if not_forced:
            raise RuntimeError(
                f"FORCE ROW LEVEL SECURITY missing on: {', '.join(not_forced)}"
            )
        print("engram_app role: present, NOBYPASSRLS, non-superuser", flush=True)
        print(f"FORCE RLS verified on {len(RLS_FORCED_TABLES)} representative table(s)", flush=True)
    finally:
        await conn.close()


def main() -> int:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    _section("Database Migration Verification")
    asyncio.run(_verify_database())

    _section("Lint")
    _run("ruff", "check", ".")

    _section("Type Check")
    _run("mypy", "engram/")

    _section("Root Service Tests")
    env = dict(os.environ)
    env["ENGRAM_FAIL_ON_DB_SKIP"] = "1"
    _run("pytest", "-q", "--durations=25", *_pytest_flags("root"), "tests", env=env)

    # Hosted CI runs the complete root suite once. The canonical trust proof
    # remains an explicit operator/local selector via ``make trust-proof`` and
    # ``make compose-trust-proof`` and must not be rerun inside the hosted gate.

    _section("SDK Tests")
    _run(
        "pytest",
        "-q",
        "-c",
        "sdk/engram-client/pyproject.toml",
        *_pytest_flags("sdk"),
        "sdk/engram-client/tests",
    )

    _section("MCP Adapter Tests")
    # ENGRAM_FAIL_ON_DB_SKIP=1 makes a DB-backed integration skip fail the run,
    # so API/SDK drift that breaks the MCP round trips fails CI instead of
    # silently skipping. The DB is available in the CI Compose stack.
    _run("pytest", "-q", *_pytest_flags("mcp-adapter"), "adapters/mcp-server/tests", env=env)

    _section("engram-hooks Tests")
    # No DB or network needed: the write-contract suite uses a hermetic fixture
    # derived from the pinned stock-Hermes revision.
    _run("pytest", "-q", *_pytest_flags("engram-hooks"), "adapters/engram-hooks/tests")

    _section("Credential Leak Scan")
    _run("python", "scripts/scan_credential_leaks.py")

    _section("CI Result")
    print("All Compose-backed CI checks passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
