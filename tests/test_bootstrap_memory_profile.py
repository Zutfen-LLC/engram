"""Real-database bootstrap-key transaction tests for profile binding."""

from __future__ import annotations

import os
import uuid

import pytest

from engram.cli import _run_bootstrap_key
from engram.migrations import normalize_asyncpg_url


def _owner_dsn() -> str | None:
    return os.environ.get("ENGRAM_OWNER_DATABASE_URL") or os.environ.get("ENGRAM_DATABASE_URL")


async def _owner():
    import asyncpg

    if not _owner_dsn():
        pytest.skip("requires owner and app PostgreSQL URLs")
    try:
        return await asyncpg.connect(normalize_asyncpg_url(_owner_dsn()))  # type: ignore[arg-type]
    except Exception:
        pytest.skip("requires a live PostgreSQL with the v2 schema")


async def _profile(owner, slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    default = await owner.fetchrow(
        "SELECT t.id AS tenant_id, p.id AS principal_id "
        "FROM tenants t JOIN principals p ON p.tenant_id = t.id "
        "WHERE t.slug = 'default' AND p.name = 'admin'"
    )
    profile_id, revision_id = uuid.uuid4(), uuid.uuid4()
    async with owner.transaction():
        await owner.execute(
            "INSERT INTO memory_profiles "
            "(id, tenant_id, name, slug, created_by_principal_id) "
            "VALUES ($1, $2, $3, $3, $4)",
            profile_id,
            default["tenant_id"],
            slug,
            default["principal_id"],
        )
        await owner.execute(
            "INSERT INTO memory_profile_revisions "
            "(id, tenant_id, profile_id, version, created_by_principal_id, reason) "
            "VALUES ($1, $2, $3, 1, $4, 'bootstrap proof')",
            revision_id,
            default["tenant_id"],
            profile_id,
            default["principal_id"],
        )
        await owner.execute(
            "UPDATE memory_profiles SET active_revision_id = $1 WHERE id = $2",
            revision_id,
            profile_id,
        )
    return profile_id, revision_id


async def test_unprofiled_bootstrap_and_existing_key_guard(capsys) -> None:
    owner = await _owner()
    label = f"bootstrap-unprofiled-{uuid.uuid4()}"
    try:
        result = await _run_bootstrap_key(
            _owner_dsn(),
            label=label,
            scopes="read,write",
            force=True,  # type: ignore[arg-type]
        )
        captured = capsys.readouterr()
        assert result == 0
        assert "BOOTSTRAP API KEY" in captured.out
        assert "memory_profile:" not in captured.out
        assert await owner.fetchval("SELECT count(*) FROM api_keys WHERE label = $1", label) == 1

        guarded_label = f"bootstrap-guarded-{uuid.uuid4()}"
        guarded = await _run_bootstrap_key(
            _owner_dsn(),  # type: ignore[arg-type]
            label=guarded_label,
            scopes="read",
            force=False,
        )
        captured = capsys.readouterr()
        assert guarded == 1
        assert "BOOTSTRAP API KEY" not in captured.out
        assert (
            await owner.fetchval("SELECT count(*) FROM api_keys WHERE label = $1", guarded_label)
            == 0
        )
    finally:
        await owner.execute("DELETE FROM api_keys WHERE label = $1", label)
        await owner.close()


async def test_profile_selectors_disabled_and_unknown_are_all_or_nothing(capsys) -> None:
    owner = await _owner()
    slug = f"bootstrap-profile-{uuid.uuid4().hex[:10]}"
    profile_id, revision_id = await _profile(owner, slug)
    labels: list[str] = []
    try:
        for selector in (slug, str(profile_id)):
            label = f"bootstrap-profiled-{uuid.uuid4()}"
            labels.append(label)
            result = await _run_bootstrap_key(
                _owner_dsn(),  # type: ignore[arg-type]
                label=label,
                scopes="read,write",
                force=True,
                memory_profile=selector,
            )
            captured = capsys.readouterr()
            assert result == 0
            assert f"memory_profile: {slug} (revision 1)" in captured.out
            key_id = await owner.fetchval("SELECT id FROM api_keys WHERE label = $1", label)
            assert key_id is not None
            assert (
                await owner.fetchval(
                    "SELECT count(*) FROM memory_profile_events "
                    "WHERE profile_id = $1 AND revision_id = $2 "
                    "AND event_type = 'profile_bound_at_key_issuance' "
                    "AND details->>'api_key_id' = $3",
                    profile_id,
                    revision_id,
                    str(key_id),
                )
                == 1
            )

        for selector in ("missing-profile", slug):
            if selector == slug:
                await owner.execute(
                    "UPDATE memory_profiles SET disabled_at = now() WHERE id = $1", profile_id
                )
            label = f"bootstrap-rejected-{uuid.uuid4()}"
            labels.append(label)
            result = await _run_bootstrap_key(
                _owner_dsn(),  # type: ignore[arg-type]
                label=label,
                scopes="read",
                force=True,
                memory_profile=selector,
            )
            captured = capsys.readouterr()
            assert result == 2
            assert "BOOTSTRAP API KEY" not in captured.out
            assert (
                await owner.fetchval("SELECT count(*) FROM api_keys WHERE label = $1", label) == 0
            )
    finally:
        await owner.execute("DELETE FROM api_keys WHERE label = ANY($1::text[])", labels)
        await owner.execute("DELETE FROM memory_profiles WHERE id = $1", profile_id)
        await owner.close()


async def test_event_failure_rolls_key_back_and_prints_no_plaintext(monkeypatch, capsys) -> None:
    import asyncpg

    owner = await _owner()
    slug = f"bootstrap-rollback-{uuid.uuid4().hex[:10]}"
    profile_id, _ = await _profile(owner, slug)
    label = f"bootstrap-rollback-{uuid.uuid4()}"
    real_connect = asyncpg.connect

    class FailingEventConnection:
        def __init__(self, connection) -> None:
            self.connection = connection

        def transaction(self):
            return self.connection.transaction()

        async def fetchrow(self, *args, **kwargs):
            return await self.connection.fetchrow(*args, **kwargs)

        async def fetchval(self, *args, **kwargs):
            return await self.connection.fetchval(*args, **kwargs)

        async def execute(self, query, *args, **kwargs):
            if "INSERT INTO memory_profile_events" in query:
                raise RuntimeError("forced event failure")
            return await self.connection.execute(query, *args, **kwargs)

        async def close(self) -> None:
            await self.connection.close()

    async def failing_connect(*args, **kwargs):
        return FailingEventConnection(await real_connect(*args, **kwargs))

    monkeypatch.setattr(asyncpg, "connect", failing_connect)
    try:
        result = await _run_bootstrap_key(
            _owner_dsn(),  # type: ignore[arg-type]
            label=label,
            scopes="read",
            force=True,
            memory_profile=slug,
        )
        captured = capsys.readouterr()
        assert result == 1
        assert "BOOTSTRAP API KEY" not in captured.out
        assert "eng_" not in captured.out
        assert await owner.fetchval("SELECT count(*) FROM api_keys WHERE label = $1", label) == 0
        assert (
            await owner.fetchval(
                "SELECT count(*) FROM memory_profile_events "
                "WHERE profile_id = $1 AND details->>'label' = $2",
                profile_id,
                label,
            )
            == 0
        )
    finally:
        await owner.execute("DELETE FROM memory_profiles WHERE id = $1", profile_id)
        await owner.close()
