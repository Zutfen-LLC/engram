"""Real-PostgreSQL regressions for worker audit-event provenance mapping.

Blocker 1 (ENG-SCOPE-002C final correction): ``_insert_event`` must keep three
states truthfully distinct — valid execution authority, genuine legacy, and
missing/corrupt v2 — and must never relabel a v2 provenance failure as legacy.

These run against the Compose real-PostgreSQL stack (see ``make compose-ci``)
and skip without it.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

from engram.db import apply_rls_context, owner_session_factory
from engram.migrations import normalize_asyncpg_url
from engram.worker import _insert_event


def _dsn() -> str | None:
    return os.environ.get("ENGRAM_OWNER_DATABASE_URL") or os.environ.get("ENGRAM_DATABASE_URL")


async def _owner() -> Any:
    import asyncpg

    if not _dsn():
        pytest.skip("requires owner and app PostgreSQL URLs")
    try:
        connection = await asyncpg.connect(normalize_asyncpg_url(_dsn()))  # type: ignore[arg-type]
        exists = await connection.fetchval("SELECT to_regclass('candidate_ingest_executions')")
        if exists is None:
            await connection.close()
            pytest.skip("requires migration 025")
        return connection
    except Exception:
        pytest.skip("requires a live PostgreSQL with the current schema")


async def _seed_tenant(owner: Any) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create a tenant + admin principal + a memory_item; return the ids."""
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    item_id = uuid.uuid4()
    await owner.execute(
        "INSERT INTO tenants (id, name, slug) VALUES ($1, 'audit', 'audit')", tenant_id
    )
    await owner.execute(
        "INSERT INTO principals (id, tenant_id, name, type) "
        "VALUES ($1, $2, 'admin', 'admin')",
        principal_id,
        tenant_id,
    )
    await owner.execute(
        "INSERT INTO tenant_config (tenant_id, config_version, active) "
        "VALUES ($1, 'v1', TRUE)",
        tenant_id,
    )
    await owner.execute(
        "INSERT INTO memory_items "
        "(id, tenant_id, principal_id, content, content_hash, kind, visibility, "
        "review_status, memory_confidence, source_trust, importance, source_type) "
        "VALUES ($1, $2, $3, $4, $5, 'fact', 'private', 'active', .9, .9, .5, 'manual')",
        item_id,
        tenant_id,
        principal_id,
        "audit provenance target",
        f"sha256:{uuid.uuid4().hex}",
    )
    return tenant_id, principal_id, item_id


def _event_for(owner: Any, item_id: uuid.UUID) -> Any:
    return owner.fetchrow(
        "SELECT memory_context_version, api_key_id, memory_profile_id, "
        "memory_profile_revision_id FROM item_events WHERE item_id=$1 "
        "ORDER BY created_at DESC LIMIT 1",
        item_id,
    )


async def test_genuine_legacy_ingest_records_legacy_provenance() -> None:
    """A legacy-unprofiled-v0 ingest with no execution row records
    legacy-unprofiled-v0 audit provenance with no caller identity."""
    owner = await _owner()
    tenant_id = principal_id = item_id = ingest_id = None
    try:
        tenant_id, principal_id, item_id = await _seed_tenant(owner)
        ingest_id = uuid.uuid4()
        await owner.execute(
            "INSERT INTO candidate_ingests (id, tenant_id, principal_id, "
            "source_type, content_hash, memory_context_version) "
            "VALUES ($1, $2, $3, 'manual', 'sha256:legacy', 'legacy-unprofiled-v0')",
            ingest_id,
            tenant_id,
            principal_id,
        )

        async with owner_session_factory() as session:
            await apply_rls_context(
                session, tenant_id=tenant_id, principal_id=principal_id
            )
            await _insert_event(
                session,
                item_id=item_id,
                event_type="classification",
                field_name="kind",
                old_value=None,
                new_value="fact",
                actor_principal_id=principal_id,
                reason="legacy audit",
                ingest_id=ingest_id,
            )
            await session.commit()

        row = await _event_for(owner, item_id)
        assert row is not None
        assert row["memory_context_version"] == "legacy-unprofiled-v0"
        assert row["api_key_id"] is None
        assert row["memory_profile_id"] is None
        assert row["memory_profile_revision_id"] is None
    finally:
        import contextlib

        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM item_events WHERE item_id=$1", item_id)
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM candidate_ingests WHERE id=$1", ingest_id)
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM memory_items WHERE id=$1", item_id)
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM tenants WHERE id=$1", tenant_id)
        await owner.close()


async def test_missing_v2_ingest_records_neutral_internal_provenance() -> None:
    """A memory-context-v2 ingest with NO execution row must NOT be relabeled
    legacy. The audit fallback is neutral internal-system-v1 with no caller
    identity."""
    owner = await _owner()
    tenant_id = principal_id = item_id = ingest_id = None
    try:
        tenant_id, principal_id, item_id = await _seed_tenant(owner)
        ingest_id = uuid.uuid4()
        # v2 origin (profiled classify) but no durable execution row.
        await owner.execute(
            "INSERT INTO candidate_ingests (id, tenant_id, principal_id, "
            "source_type, content_hash, memory_context_version) "
            "VALUES ($1, $2, $3, 'manual', 'sha256:v2-missing', 'memory-context-v2')",
            ingest_id,
            tenant_id,
            principal_id,
        )

        async with owner_session_factory() as session:
            await apply_rls_context(
                session, tenant_id=tenant_id, principal_id=principal_id
            )
            await _insert_event(
                session,
                item_id=item_id,
                event_type="classification",
                field_name="kind",
                old_value=None,
                new_value="fact",
                actor_principal_id=principal_id,
                reason="missing v2 audit",
                ingest_id=ingest_id,
            )
            await session.commit()

        row = await _event_for(owner, item_id)
        assert row is not None
        assert row["memory_context_version"] == "internal-system-v1"
        assert row["api_key_id"] is None
        assert row["memory_profile_id"] is None
        assert row["memory_profile_revision_id"] is None
    finally:
        import contextlib

        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM item_events WHERE item_id=$1", item_id)
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM candidate_ingests WHERE id=$1", ingest_id)
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM memory_items WHERE id=$1", item_id)
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM tenants WHERE id=$1", tenant_id)
        await owner.close()


async def test_corrupt_v2_ingest_records_neutral_internal_provenance() -> None:
    """A v2 ingest whose execution row carries an unsupported/corrupt context
    version must also fall back to neutral internal provenance (not legacy, no
    fabricated identity)."""
    owner = await _owner()
    tenant_id = principal_id = item_id = ingest_id = None
    try:
        tenant_id, principal_id, item_id = await _seed_tenant(owner)
        ingest_id = uuid.uuid4()
        await owner.execute(
            "INSERT INTO candidate_ingests (id, tenant_id, principal_id, "
            "source_type, content_hash, memory_context_version) "
            "VALUES ($1, $2, $3, 'manual', 'sha256:v2-corrupt', 'memory-context-v2')",
            ingest_id,
            tenant_id,
            principal_id,
        )
        # Execution row present but with an unsupported context version -> raises
        # "unsupported candidate memory context" inside memory_context_from_ingest.
        await owner.execute(
            "INSERT INTO candidate_ingest_executions "
            "(ingest_id, tenant_id, memory_context_version) "
            "VALUES ($1, $2, 'unsupported-v99')",
            ingest_id,
            tenant_id,
        )

        async with owner_session_factory() as session:
            await apply_rls_context(
                session, tenant_id=tenant_id, principal_id=principal_id
            )
            await _insert_event(
                session,
                item_id=item_id,
                event_type="classification",
                field_name="kind",
                old_value=None,
                new_value="fact",
                actor_principal_id=principal_id,
                reason="corrupt v2 audit",
                ingest_id=ingest_id,
            )
            await session.commit()

        row = await _event_for(owner, item_id)
        assert row is not None
        assert row["memory_context_version"] == "internal-system-v1"
        assert row["api_key_id"] is None
        assert row["memory_profile_id"] is None
        assert row["memory_profile_revision_id"] is None
    finally:
        import contextlib

        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM item_events WHERE item_id=$1", item_id)
        with contextlib.suppress(Exception):
            await owner.execute(
                "DELETE FROM candidate_ingest_executions WHERE ingest_id=$1", ingest_id
            )
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM candidate_ingests WHERE id=$1", ingest_id)
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM memory_items WHERE id=$1", item_id)
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM tenants WHERE id=$1", tenant_id)
        await owner.close()


async def test_valid_execution_context_records_exact_caller_provenance() -> None:
    """A v2 ingest with a valid execution row records the exact caller API-key,
    profile, revision, and memory-context-v2 in the audit event."""
    owner = await _owner()
    tenant_id = principal_id = item_id = ingest_id = None
    api_key_id = profile_id = revision_id = None
    try:
        tenant_id, principal_id, item_id = await _seed_tenant(owner)
        api_key_id = uuid.uuid4()
        profile_id = uuid.uuid4()
        revision_id = uuid.uuid4()
        ingest_id = uuid.uuid4()
        await owner.execute(
            "INSERT INTO api_keys (id, tenant_id, principal_id, scopes, key_hash) "
            "VALUES ($1, $2, $3, ARRAY['admin']::text[], 'valid-hash')",
            api_key_id,
            tenant_id,
            principal_id,
        )
        await owner.execute(
            "INSERT INTO memory_profiles (id, tenant_id, name, slug) "
            "VALUES ($1, $2, 'p', 'p')",
            profile_id,
            tenant_id,
        )
        await owner.execute(
            "INSERT INTO memory_profile_revisions (id, profile_id, tenant_id, version, "
            "include_private, include_tenant, include_public, allow_tenant_write, "
            "allow_public_write, default_write_visibility, reason) "
            "VALUES ($1, $2, $3, 1, TRUE, FALSE, FALSE, FALSE, FALSE, 'private', 'test')",
            revision_id,
            profile_id,
            tenant_id,
        )
        await owner.execute(
            "INSERT INTO candidate_ingests (id, tenant_id, principal_id, "
            "source_type, content_hash, memory_context_version, api_key_id, "
            "memory_profile_id, memory_profile_revision_id) "
            "VALUES ($1, $2, $3, 'manual', 'sha256:v2-valid', 'memory-context-v2', "
            "$4, $5, $6)",
            ingest_id,
            tenant_id,
            principal_id,
            api_key_id,
            profile_id,
            revision_id,
        )
        await owner.execute(
            "INSERT INTO candidate_ingest_executions "
            "(ingest_id, tenant_id, api_key_id, memory_profile_id, "
            "memory_profile_revision_id, memory_context_version) "
            "VALUES ($1, $2, $3, $4, $5, 'memory-context-v2')",
            ingest_id,
            tenant_id,
            api_key_id,
            profile_id,
            revision_id,
        )
        # memory_context_from_ingest looks up the api_key scopes; seed admin so
        # the admin_workspace_bypass path resolves without widening.
        await owner.execute(
            "UPDATE api_keys SET scopes=ARRAY['admin']::text[] WHERE id=$1", api_key_id
        )

        async with owner_session_factory() as session:
            await apply_rls_context(
                session, tenant_id=tenant_id, principal_id=principal_id
            )
            await _insert_event(
                session,
                item_id=item_id,
                event_type="classification",
                field_name="kind",
                old_value=None,
                new_value="fact",
                actor_principal_id=principal_id,
                reason="valid v2 audit",
                ingest_id=ingest_id,
            )
            await session.commit()

        row = await _event_for(owner, item_id)
        assert row is not None
        assert row["memory_context_version"] == "memory-context-v2"
        assert row["api_key_id"] == api_key_id
        assert row["memory_profile_id"] == profile_id
        assert row["memory_profile_revision_id"] == revision_id
    finally:
        import contextlib

        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM item_events WHERE item_id=$1", item_id)
        with contextlib.suppress(Exception):
            await owner.execute(
                "DELETE FROM candidate_ingest_executions WHERE ingest_id=$1", ingest_id
            )
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM candidate_ingests WHERE id=$1", ingest_id)
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM memory_profile_revisions WHERE id=$1", revision_id)
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM memory_profiles WHERE id=$1", profile_id)
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM api_keys WHERE id=$1", api_key_id)
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM memory_items WHERE id=$1", item_id)
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM tenants WHERE id=$1", tenant_id)
        await owner.close()
