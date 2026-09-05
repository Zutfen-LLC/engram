"""Run the Compose-backed CI verification flow with visible sections."""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import asyncpg
from ci_shards import select_root_test_shard

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


def _run_preflight() -> None:
    """Run checks that do not require PostgreSQL."""
    _section("Credential Leak Scan")
    _run("python", "scripts/scan_credential_leaks.py")

    _section("Lint")
    _run("ruff", "check", ".")

    _section("Type Check: Service")
    _run("mypy", "engram/")
    _run("mypy", "--explicit-package-bases", "evals/admission/")

    _section("Type Check: SDK")
    _run(
        "mypy",
        "--config-file",
        "sdk/engram-client/pyproject.toml",
        "sdk/engram-client/engram_client",
    )

    workspace_env = dict(os.environ)
    workspace_env["MYPYPATH"] = "sdk/engram-client"

    _section("Type Check: MCP Adapter")
    _run(
        "mypy",
        "--config-file",
        "adapters/mcp-server/pyproject.toml",
        "adapters/mcp-server/engram_mcp",
        env=workspace_env,
    )

    _section("Type Check: engram-hooks Adapter")
    _run(
        "mypy",
        "--config-file",
        "adapters/engram-hooks/pyproject.toml",
        "adapters/engram-hooks/engram_hooks",
        env=workspace_env,
    )

    _section("SDK Tests")
    _run(
        "pytest",
        "-q",
        "-c",
        "sdk/engram-client/pyproject.toml",
        *_pytest_flags("sdk"),
        "sdk/engram-client/tests",
    )

    _section("engram-hooks Tests")
    # No DB or network needed: the write-contract suite uses a hermetic fixture
    # derived from the pinned stock-Hermes revision.
    _run("pytest", "-q", *_pytest_flags("engram-hooks"), "adapters/engram-hooks/tests")


def _database_env() -> dict[str, str]:
    env = dict(os.environ)
    env["ENGRAM_FAIL_ON_DB_SKIP"] = "1"
    return env


def _run_database_verification() -> None:
    _section("Database Migration Verification")
    asyncio.run(_verify_database())


def _run_mcp_adapter_tests(env: dict[str, str]) -> None:
    _section("MCP Adapter Tests")
    # A skipped DB integration test must fail the real-DB gate.
    _run("pytest", "-q", *_pytest_flags("mcp-adapter"), "adapters/mcp-server/tests", env=env)


def _run_complete_root_suite(env: dict[str, str]) -> None:
    _section("Root Service Tests")
    _run("pytest", "-q", "--durations=25", *_pytest_flags("root"), "tests", env=env)


def _run_root_shard(env: dict[str, str], *, shard_index: int, shard_count: int) -> None:
    test_files = select_root_test_shard(
        Path("tests"), shard_index=shard_index, shard_count=shard_count
    )
    suite = f"root-shard-{shard_index + 1}-of-{shard_count}"
    total_bytes = sum(path.stat().st_size for path in test_files)

    _section(f"Root Service Tests: Shard {shard_index + 1} of {shard_count}")
    print(
        f"Selected {len(test_files)} test files ({total_bytes} source bytes):",
        flush=True,
    )
    for path in test_files:
        print(f"  {path.as_posix()}", flush=True)
    _run(
        "pytest",
        "-q",
        "--durations=25",
        *_pytest_flags(suite),
        *(path.as_posix() for path in test_files),
        env=env,
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("full", "preflight", "root-shard"),
        default=os.environ.get("ENGRAM_CI_MODE", "full"),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    if args.mode in {"full", "preflight"}:
        _run_preflight()

    if args.mode == "preflight":
        _section("CI Result")
        print("All DB-free CI preflight checks passed.", flush=True)
        return 0

    _run_database_verification()
    env = _database_env()

    if args.mode == "root-shard":
        shard_index = int(os.environ["ENGRAM_CI_SHARD_INDEX"])
        shard_count = int(os.environ["ENGRAM_CI_SHARD_COUNT"])
        if shard_index == 0:
            _run_mcp_adapter_tests(env)
        _run_root_shard(env, shard_index=shard_index, shard_count=shard_count)
    else:
        _run_mcp_adapter_tests(env)
        _run_complete_root_suite(env)

    # Hosted CI partitions the complete root suite across isolated shards. The
    # canonical trust proof remains an explicit operator/local selector via
    # ``make trust-proof`` and ``make compose-trust-proof``. Do not rerun it in
    # the hosted gate.

    _section("CI Result")
    print(f"All {args.mode} CI checks passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
