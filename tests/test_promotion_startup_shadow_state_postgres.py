"""Real-PostgreSQL RLS proof for B5's non-authoritative shadow state."""

from __future__ import annotations

import os
import uuid
from typing import Any

import asyncpg
import pytest


def _owner_dsn() -> str | None:
    return os.environ.get("ENGRAM_DATABASE_URL") or os.environ.get("ENGRAM_OWNER_DATABASE_URL")


def _app_dsn() -> str | None:
    return os.environ.get("ENGRAM_APP_DATABASE_URL")


async def _connect(url: str) -> Any:
    import asyncpg

    from engram.migrations import normalize_asyncpg_url

    return await asyncpg.connect(normalize_asyncpg_url(url))


async def _owner_with_shadow_state() -> Any:
    if not _owner_dsn() or not _app_dsn():
        pytest.skip("requires Compose owner and non-owner app database URLs")
    owner = await _connect(_owner_dsn())
    if await owner.fetchval("SELECT to_regclass('promotion_startup_shadow_state')") is None:
        await owner.close()
        pytest.skip("requires migration 035")
    return owner


async def _seed_tenant(owner: Any, tenant_id: uuid.UUID, label: str) -> None:
    await owner.execute(
        "INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $3)",
        tenant_id,
        label,
        f"{label.lower()}-{tenant_id.hex[:8]}",
    )


async def test_shadow_state_is_forced_rls_and_app_scoped() -> None:
    owner = await _owner_with_shadow_state()
    app = await _connect(_app_dsn())  # type: ignore[arg-type]
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    try:
        await _seed_tenant(owner, tenant_a, "ShadowA")
        await _seed_tenant(owner, tenant_b, "ShadowB")
        await owner.execute(
            "INSERT INTO promotion_startup_shadow_state (tenant_id) VALUES ($1), ($2)",
            tenant_a,
            tenant_b,
        )
        row = await owner.fetchrow(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = 'promotion_startup_shadow_state'"
        )
        assert row is not None
        assert row["relrowsecurity"] is True
        assert row["relforcerowsecurity"] is True

        await app.execute("SELECT set_config('app.tenant_id', $1, false)", str(tenant_a))
        assert await app.fetchval("SELECT count(*) FROM promotion_startup_shadow_state") == 1
        assert await app.execute(
            "UPDATE promotion_startup_shadow_state SET rotation = 1 WHERE tenant_id = $1",
            tenant_a,
        ) == "UPDATE 1"
        assert await app.execute(
            "UPDATE promotion_startup_shadow_state SET rotation = 1 WHERE tenant_id = $1",
            tenant_b,
        ) == "UPDATE 0"
        with pytest.raises(asyncpg.PostgresError):
            await app.execute("DELETE FROM promotion_startup_shadow_state")
    finally:
        await app.close()
        await owner.execute("DELETE FROM tenants WHERE id = ANY($1::uuid[])", [tenant_a, tenant_b])
        await owner.close()
