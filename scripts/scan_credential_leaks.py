"""Fail CI if credential-shaped plaintext appears in tracked files or PostgreSQL."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from pathlib import Path

import asyncpg

from engram.migrations import normalize_asyncpg_url

PATTERNS = (
    re.compile(rb"engd_[0-9A-Za-z]{22}\*[A-Za-z0-9_-]{43}"),
    re.compile(rb"engsvc_[0-9A-Za-z]{22}_[A-Za-z0-9_-]{43}"),
    re.compile(rb"eng_[0-9A-Za-z]{22}_[A-Za-z0-9_-]{43}"),
)
POSTGRES_PATTERN = (
    r"(engd_[0-9A-Za-z]{22}\*[A-Za-z0-9_-]{43}|"
    r"engsvc_[0-9A-Za-z]{22}_[A-Za-z0-9_-]{43}|"
    r"eng_[0-9A-Za-z]{22}_[A-Za-z0-9_-]{43})"
)


def scan_tracked_files(repository: Path) -> int:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    leaked_files = 0
    for raw_name in result.stdout.split(b"\0"):
        if not raw_name:
            continue
        path = repository / os.fsdecode(raw_name)
        try:
            payload = path.read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            continue
        if any(pattern.search(payload) for pattern in PATTERNS):
            leaked_files += 1
    return leaked_files


async def scan_database(url: str) -> int:
    conn = await asyncpg.connect(normalize_asyncpg_url(url))
    try:
        columns = await conn.fetch(
            "SELECT quote_ident(table_name) AS table_name,"
            "quote_ident(column_name) AS column_name "
            "FROM information_schema.columns "
            "WHERE table_schema='public' "
            "AND data_type IN ('character','character varying','text','json','jsonb') "
            "ORDER BY table_name,column_name"
        )
        matches = 0
        for column in columns:
            query = (
                f"SELECT count(*) FROM {column['table_name']} "
                f"WHERE CAST({column['column_name']} AS text) ~ $1"
            )
            matches += int(await conn.fetchval(query, POSTGRES_PATTERN))
        return matches
    finally:
        await conn.close()


def main() -> int:
    repository = Path(__file__).resolve().parents[1]
    tracked_matches = scan_tracked_files(repository)
    database_url = os.environ.get("ENGRAM_OWNER_DATABASE_URL")
    database_matches = (
        asyncio.run(scan_database(database_url)) if database_url is not None else 0
    )
    if tracked_matches or database_matches:
        print(
            "Credential leak scan failed: "
            f"tracked_files={tracked_matches} database_fields={database_matches}"
        )
        return 1
    print("Credential leak scan passed: tracked_files=0 database_fields=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
