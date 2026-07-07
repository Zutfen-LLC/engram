"""Run the Compose-backed CI verification flow with visible sections."""

from __future__ import annotations

import asyncio
import os
import subprocess
from typing import Final

import asyncpg

DB_TABLES: Final[tuple[str, ...]] = (
    "tenants",
    "workspaces",
    "principals",
    "memory_items",
    "memory_embeddings",
    "kg_triples",
    "tenant_config",
)


def _section(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def _run(*args: str, env: dict[str, str] | None = None) -> None:
    print(f"+ {' '.join(args)}", flush=True)
    subprocess.run(args, check=True, env=env)


async def _verify_database() -> None:
    url = os.environ["ENGRAM_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
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
    finally:
        await conn.close()


def main() -> int:
    _section("Database Migration Verification")
    asyncio.run(_verify_database())

    _section("Lint")
    _run("ruff", "check", ".")

    _section("Type Check")
    _run("mypy", "engram/")

    _section("Root Service Tests")
    env = dict(os.environ)
    env["ENGRAM_FAIL_ON_DB_SKIP"] = "1"
    _run("pytest", "-q", "tests", env=env)

    _section("SDK Tests")
    _run("pytest", "-q", "-c", "sdk/engram-client/pyproject.toml", "sdk/engram-client/tests")

    _section("MCP Adapter Smoke Tests")
    _run("pytest", "-q", "adapters/mcp-server/tests")

    _section("CI Result")
    print("All Compose-backed CI checks passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
