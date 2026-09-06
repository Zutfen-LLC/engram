"""Real-PostgreSQL up/down safety proof for the recall-profile migrations
(040 recall_logs audit column, 041 tenant shadow-policy column — issue #160).

Both migrations are additive and must stay reapplicable (the runner may
replay a partially applied deployment), and their downgrades must leave the
schema usable rather than half-dropped. The test always restores the upgraded
state before finishing.

They skip without a reachable database (see ``make compose-ci``).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

_MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
_M040 = _MIGRATIONS / "040_recall_profiles.sql"
_M040_DOWN = _MIGRATIONS / "downgrades" / "040_recall_profiles.sql"
_M041 = _MIGRATIONS / "041_recall_profile_shadow_policy.sql"
_M041_DOWN = _MIGRATIONS / "downgrades" / "041_recall_profile_shadow_policy.sql"


def _owner_dsn() -> str | None:
    return os.environ.get("ENGRAM_DATABASE_URL") or os.environ.get("ENGRAM_OWNER_DATABASE_URL")


async def _connect(url: str) -> Any:
    import asyncpg

    from engram.migrations import normalize_asyncpg_url

    return await asyncpg.connect(normalize_asyncpg_url(url))


async def _column(conn: Any, table: str, column: str) -> bool:
    return (
        await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name = $1 AND column_name = $2)",
            table,
            column,
        )
        is True
    )


@pytest.fixture
async def owner():
    dsn = _owner_dsn()
    if not dsn:
        pytest.skip("requires ENGRAM_DATABASE_URL (owner) for setup")
    conn = await _connect(dsn)
    if not await _column(conn, "recall_logs", "recall_profile"):
        await conn.close()
        pytest.skip("requires migration 040")
    yield conn
    # Always leave the schema upgraded, whatever a test did to it.
    await conn.execute(_M040.read_text())
    await conn.execute(_M041.read_text())
    await conn.close()


async def test_040_is_reapplicable_and_its_downgrade_is_safe(owner) -> None:
    # Replaying the migration over an upgraded schema is a no-op.
    await owner.execute(_M040.read_text())
    assert await _column(owner, "recall_logs", "recall_profile")

    # Downgrade drops the audit column cleanly (and the constraint first).
    await owner.execute(_M040_DOWN.read_text())
    assert not await _column(owner, "recall_logs", "recall_profile")

    # Re-applying restores it with the documented default + check constraint.
    await owner.execute(_M040.read_text())
    assert await _column(owner, "recall_logs", "recall_profile")
    default = await owner.fetchval(
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_name = 'recall_logs' AND column_name = 'recall_profile'"
    )
    assert default is not None and "'legacy'" in str(default)
    constraint = await owner.fetchval(
        "SELECT conname FROM pg_constraint "
        "WHERE conrelid = 'recall_logs'::regclass AND conname = 'recall_logs_recall_profile_check'"
    )
    assert constraint == "recall_logs_recall_profile_check"


async def test_041_is_reapplicable_and_its_downgrade_fails_closed(owner) -> None:
    # Replaying is a no-op; the column exists with the fail-closed default.
    await owner.execute(_M041.read_text())
    assert await _column(owner, "tenant_config", "recall_profile_shadow_enabled")
    default = await owner.fetchval(
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_name = 'tenant_config' AND column_name = 'recall_profile_shadow_enabled'"
    )
    assert default is not None and "false" in str(default).lower()

    # Downgrade removes only the tenant allow decision; nothing served
    # depends on the column (the surface fails closed without it).
    await owner.execute(_M041_DOWN.read_text())
    assert not await _column(owner, "tenant_config", "recall_profile_shadow_enabled")

    # Re-applying restores the fail-closed policy column.
    await owner.execute(_M041.read_text())
    assert await _column(owner, "tenant_config", "recall_profile_shadow_enabled")
