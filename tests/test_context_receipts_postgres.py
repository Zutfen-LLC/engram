"""Real-PostgreSQL migration, RLS, privilege, and relational-integrity proof
for ``context_receipts`` (migration 026, ENG-CONTEXT-002A).

These tests connect as the non-owner ``engram_app`` role to prove, against real
PostgreSQL, that the durable context-receipt substrate is:

- tenant+principal isolated (FORCE RLS, both GUCs required);
- least-privilege (SELECT/INSERT only; UPDATE/DELETE denied);
- relationally tenant-safe (composite FK to ``recall_logs``, ON DELETE RESTRICT);
- protocol-checked (schema/mode/hash/JSON-shape/agreement CHECKs reject invalid
  envelopes at the database level);
- one-to-one with recall logs (unique ``recall_log_id``);
- idempotent to re-apply (every object guarded / IF NOT EXISTS).

They skip without the Compose real-PostgreSQL stack (see ``make compose-ci``).
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from datetime import UTC
from typing import Any

import pytest

# asyncpg is imported lazily so the module imports without a live DB.

_DB_SKIP_REASON = "requires a live PostgreSQL with the v2 schema"


def _owner_dsn() -> str | None:
    return os.environ.get("ENGRAM_DATABASE_URL") or os.environ.get(
        "ENGRAM_OWNER_DATABASE_URL"
    )


def _app_dsn() -> str | None:
    return os.environ.get("ENGRAM_APP_DATABASE_URL")


async def _connect(url: str) -> Any:
    import asyncpg

    from engram.migrations import normalize_asyncpg_url

    return await asyncpg.connect(normalize_asyncpg_url(url))


def _skip_if_no_stack() -> None:
    if not _owner_dsn():
        pytest.skip("requires ENGRAM_DATABASE_URL (owner) for setup")
    if not _app_dsn():
        pytest.skip("requires ENGRAM_APP_DATABASE_URL (non-owner app role)")


async def _owner_with_026() -> Any:
    _skip_if_no_stack()
    owner = await _connect(_owner_dsn())  # type: ignore[arg-type]
    if await owner.fetchval("SELECT to_regclass('context_receipts')") is None:
        await owner.close()
        pytest.skip("requires migration 026")
    return owner


def _denied(exc: BaseException) -> bool:
    """True for a PostgreSQL privilege/RLS rejection."""
    import asyncpg

    if isinstance(exc, asyncpg.PostgresError):
        sqlstate = getattr(exc, "sqlstate", None) or ""
        return sqlstate in {"42501", "23000", "23514"}
    return False


def _check_violation(exc: BaseException) -> bool:
    import asyncpg

    if isinstance(exc, asyncpg.PostgresError):
        sqlstate = getattr(exc, "sqlstate", None) or ""
        return sqlstate == "23514"
    return False


def _fk_violation(exc: BaseException) -> bool:
    import asyncpg

    if isinstance(exc, asyncpg.PostgresError):
        sqlstate = getattr(exc, "sqlstate", None) or ""
        return sqlstate == "23503"
    return False


def _unique_violation(exc: BaseException) -> bool:
    import asyncpg

    if isinstance(exc, asyncpg.PostgresError):
        sqlstate = getattr(exc, "sqlstate", None) or ""
        return sqlstate == "23505"
    return False


# ─── Helpers: build a valid manifest JSONB + recall log ─────────────────


async def _seed_tenant_principal(
    owner: Any, *, tenant_id: uuid.UUID, principal_id: uuid.UUID, label: str
) -> None:
    await owner.execute(
        "INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $3)",
        tenant_id,
        label,
        f"{label.lower()}-{tenant_id.hex[:8]}",
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


async def _insert_recall_log(
    owner: Any,
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    recall_log_id: uuid.UUID,
    item_ids: list[uuid.UUID] | None,
    byte_budget: int | None = None,
    token_budget: int | None = None,
    scoring_version: str = "v1",
    config_version: str = "v1",
    mode: str = "startup",
    memory_context_version: str = "memory-context-v2",
) -> None:
    await owner.execute(
        "INSERT INTO recall_logs (id, tenant_id, principal_id, mode, query, "
        "item_ids, byte_budget, token_budget, scoring_version, config_version, "
        "memory_context_version) "
        "VALUES ($1, $2, $3, $4, NULL, $5, $6, $7, $8, $9, $10)",
        recall_log_id,
        tenant_id,
        principal_id,
        mode,
        item_ids,
        byte_budget,
        token_budget,
        scoring_version,
        config_version,
        memory_context_version,
    )


def _valid_manifest_jsonb(
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    item_ids: list[uuid.UUID],
    byte_budget: int | None = 8192,
    packet_hash: str | None = None,
    manifest_hash: str | None = None,
) -> dict[str, Any]:
    """Build a minimal valid manifest dict satisfying all migration CHECKs.

    The database cannot recompute RFC 8785, so this only needs to satisfy the
    CHECK constraints (object shape, agreement, hash format, ownership). The
    repository's exact manifest-hash verification is exercised separately.
    """
    import hashlib

    if packet_hash is None:
        packet_hash = "sha256:" + hashlib.sha256(b"packet").hexdigest()
    items = [
        {
            "ordinal": i,
            "item_id": str(iid),
            "kind": "fact",
            "served_content_hash": "sha256:" + hashlib.sha256(b"c").hexdigest(),
            "review_status": "active",
            "authority": 10,
            "visibility": "tenant",
            "workspace_id": None,
            "score": 0.5,
            "reasons": [],
            "warnings": [],
            "pinned": False,
            "importance": 0.9,
            "source_trust": 0.5,
            "memory_confidence": 0.5,
            "human_verified": True,
            "conflict_type": None,
            "conflict_resolution_status": None,
        }
        for i, iid in enumerate(item_ids)
    ]
    manifest: dict[str, Any] = {
        "schema": "engram.context-manifest",
        "schema_version": "1.0",
        "canonicalization": "rfc8785",
        "mode": "startup",
        "subject": {
            "tenant_id": str(tenant_id),
            "principal_id": str(principal_id),
            "workspace_id": None,
            "memory_context_version": "memory-context-v2",
            "memory_profile_id": None,
            "memory_profile_revision_id": None,
            "memory_profile_version": None,
        },
        "request": {
            "requested": {
                "workspace_supplied": False,
                "byte_budget": None,
                "token_budget": None,
                "item_budget": None,
            },
            "effective": {
                "workspace_id": None,
                "byte_budget": byte_budget,
                "token_budget": None,
                "item_budget": None,
            },
            "query_digest": None,
            "request_digest": "sha256:" + hashlib.sha256(b"req").hexdigest(),
        },
        "versions": {
            "scoring_version": "v1",
            "config_version": "v1",
            "candidate_strategy_version": "startup-candidates-v1",
            "manifest_contract_version": "context-manifest-v1",
            "packet_render_version": "working-set-v1",
        },
        "result": {
            "item_count": len(item_ids),
            "served_content_byte_count": len(item_ids),
            "rendered_packet_byte_count": 6,
            "pinned_omitted_count": 0,
            "omitted_count": 0,
            "message": None,
        },
        "packet": {
            "media_type": "text/plain; charset=utf-8",
            "render_version": "working-set-v1",
            "hash": packet_hash,
        },
        "items": items,
    }
    if manifest_hash is not None:
        # Smuggling a manifest_hash field is rejected by a CHECK; only used by
        # the negative test that asserts that rejection.
        manifest["manifest_hash"] = manifest_hash
    return manifest


async def _owner_insert_valid_receipt(
    owner: Any,
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    recall_log_id: uuid.UUID,
    receipt_id: uuid.UUID,
    item_ids: list[uuid.UUID],
    byte_budget: int | None = 8192,
    retention_expires_at: Any = None,
) -> dict[str, Any]:
    manifest = _valid_manifest_jsonb(
        tenant_id=tenant_id, principal_id=principal_id, item_ids=item_ids,
        byte_budget=byte_budget,
    )
    packet_hash = manifest["packet"]["hash"]
    import hashlib

    manifest_hash = "sha256:" + hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode("utf-8")
    ).hexdigest()
    await owner.execute(
        "INSERT INTO context_receipts (id, tenant_id, principal_id, recall_log_id, "
        "manifest_schema, manifest_schema_version, canonicalization, mode, "
        "manifest, manifest_hash, packet_hash, retention_expires_at) "
        "VALUES ($1, $2, $3, $4, 'engram.context-manifest', '1.0', 'rfc8785', "
        "'startup', $5::jsonb, $6, $7, $8)",
        receipt_id,
        tenant_id,
        principal_id,
        recall_log_id,
        json.dumps(manifest),
        manifest_hash,
        packet_hash,
        retention_expires_at,
    )
    return manifest


# ─── Tests ─────────────────────────────────────────────────────────────


async def test_rls_enabled_and_forced() -> None:
    owner = await _owner_with_026()
    try:
        row = await owner.fetchrow(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = 'context_receipts'"
        )
        assert row is not None
        assert row["relrowsecurity"] is True
        assert row["relforcerowsecurity"] is True
    finally:
        await owner.close()


async def test_app_role_owns_no_table_and_has_no_bypassrls() -> None:
    owner = await _owner_with_026()
    try:
        role = await owner.fetchrow(
            "SELECT rolbypassrls, rolsuper FROM pg_roles WHERE rolname = 'engram_app'"
        )
        assert role is not None
        assert role["rolbypassrls"] is False
        assert role["rolsuper"] is False
        owner_role = await owner.fetchval(
            "SELECT tableowner FROM pg_tables WHERE tablename = 'context_receipts'"
        )
        assert owner_role != "engram_app"
    finally:
        await owner.close()


async def test_app_role_select_insert_only_no_update_delete() -> None:
    owner = await _owner_with_026()
    app = await _connect(_app_dsn())  # type: ignore[arg-type]
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    recall_log_id = uuid.uuid4()
    receipt_id = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    try:
        await _seed_tenant_principal(
            owner, tenant_id=tenant_id, principal_id=principal_id, label="priv"
        )
        await _insert_recall_log(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=recall_log_id,
            item_ids=item_ids,
        )
        await _owner_insert_valid_receipt(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=recall_log_id,
            receipt_id=receipt_id,
            item_ids=item_ids,
        )

        await app.execute("SELECT set_config('app.tenant_id', $1, false)", str(tenant_id))
        await app.execute(
            "SELECT set_config('app.principal_id', $1, false)", str(principal_id)
        )

        # SELECT sees the row.
        seen = await app.fetchval(
            "SELECT count(*) FROM context_receipts WHERE id = $1", receipt_id
        )
        assert seen == 1

        # INSERT succeeds (a second receipt for a different recall log).
        rl2 = uuid.uuid4()
        await owner.execute(
            "INSERT INTO recall_logs (id, tenant_id, principal_id, mode, item_ids, "
            "memory_context_version) VALUES ($1, $2, $3, 'startup', $4, "
            "'memory-context-v2')",
            rl2, tenant_id, principal_id, [uuid.uuid4()],
        )
        manifest2 = _valid_manifest_jsonb(
            tenant_id=tenant_id, principal_id=principal_id,
            item_ids=[uuid.uuid4()],
        )
        import hashlib

        mh2 = "sha256:" + hashlib.sha256(
            json.dumps(manifest2, sort_keys=True).encode("utf-8")
        ).hexdigest()
        await app.execute(
            "INSERT INTO context_receipts (tenant_id, principal_id, recall_log_id, "
            "manifest_schema, manifest_schema_version, canonicalization, mode, "
            "manifest, manifest_hash, packet_hash) "
            "VALUES ($1, $2, $3, 'engram.context-manifest', '1.0', 'rfc8785', "
            "'startup', $4::jsonb, $5, $6)",
            tenant_id, principal_id, rl2,
            json.dumps(manifest2), mh2, manifest2["packet"]["hash"],
        )

        # UPDATE must be denied.
        import asyncpg

        with pytest.raises(asyncpg.PostgresError) as exc_info:
            await app.execute(
                "UPDATE context_receipts SET retention_expires_at = now() "
                "WHERE id = $1", receipt_id
            )
        assert _denied(exc_info.value)

        # DELETE must be denied.
        with pytest.raises(asyncpg.PostgresError) as exc_info:
            await app.execute(
                "DELETE FROM context_receipts WHERE id = $1", receipt_id
            )
        assert _denied(exc_info.value)
    finally:
        with contextlib.suppress(Exception):
            await owner.execute(
                "DELETE FROM context_receipts WHERE tenant_id = $1", tenant_id
            )
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await app.close()
        await owner.close()


async def test_rls_requires_both_tenant_and_principal() -> None:
    owner = await _owner_with_026()
    app = await _connect(_app_dsn())  # type: ignore[arg-type]
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    other_principal_id = uuid.uuid4()
    recall_log_id = uuid.uuid4()
    receipt_id = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    try:
        await _seed_tenant_principal(
            owner, tenant_id=tenant_id, principal_id=principal_id, label="both"
        )
        await owner.execute(
            "INSERT INTO principals (id, tenant_id, name, type) "
            "VALUES ($1, $2, 'other', 'agent')",
            other_principal_id, tenant_id,
        )
        await _insert_recall_log(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=recall_log_id,
            item_ids=item_ids,
        )
        await _owner_insert_valid_receipt(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=recall_log_id,
            receipt_id=receipt_id,
            item_ids=item_ids,
        )

        # Unset tenant/principal -> zero rows.
        assert await app.fetchval("SELECT count(*) FROM context_receipts") == 0

        # Set tenant only -> zero rows (principal GUC still required).
        await app.execute("SELECT set_config('app.tenant_id', $1, false)", str(tenant_id))
        assert await app.fetchval("SELECT count(*) FROM context_receipts") == 0

        # Set principal only -> zero rows. Reset tenant by opening a fresh
        # connection (SET LOCAL is transaction-scoped; a new txn clears it).
        await app.close()
        app = await _connect(_app_dsn())  # type: ignore[arg-type]
        await app.execute(
            "SELECT set_config('app.principal_id', $1, false)", str(principal_id)
        )
        assert await app.fetchval("SELECT count(*) FROM context_receipts") == 0

        # Set both (owner) -> see the row.
        await app.execute("SELECT set_config('app.tenant_id', $1, false)", str(tenant_id))
        await app.execute(
            "SELECT set_config('app.principal_id', $1, false)", str(principal_id)
        )
        assert (
            await app.fetchval(
                "SELECT count(*) FROM context_receipts WHERE id = $1", receipt_id
            )
            == 1
        )

        # Same tenant, different principal -> zero rows.
        await app.execute(
            "SELECT set_config('app.principal_id', $1, false)", str(other_principal_id)
        )
        assert (
            await app.fetchval(
                "SELECT count(*) FROM context_receipts WHERE id = $1", receipt_id
            )
            == 0
        )
    finally:
        with contextlib.suppress(Exception):
            await owner.execute(
                "DELETE FROM context_receipts WHERE tenant_id = $1", tenant_id
            )
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await app.close()
        await owner.close()


async def test_other_tenant_cannot_read_or_insert() -> None:
    owner = await _owner_with_026()
    app = await _connect(_app_dsn())  # type: ignore[arg-type]
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    principal_a = uuid.uuid4()
    principal_b = uuid.uuid4()
    recall_log_a = uuid.uuid4()
    receipt_id = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    try:
        await _seed_tenant_principal(
            owner, tenant_id=tenant_a, principal_id=principal_a, label="ota"
        )
        await _seed_tenant_principal(
            owner, tenant_id=tenant_b, principal_id=principal_b, label="otb"
        )
        await _insert_recall_log(
            owner,
            tenant_id=tenant_a,
            principal_id=principal_a,
            recall_log_id=recall_log_a,
            item_ids=item_ids,
        )
        await _owner_insert_valid_receipt(
            owner,
            tenant_id=tenant_a,
            principal_id=principal_a,
            recall_log_id=recall_log_a,
            receipt_id=receipt_id,
            item_ids=item_ids,
        )
        # Scoped to tenant B: tenant A's receipt is invisible.
        await app.execute("SELECT set_config('app.tenant_id', $1, false)", str(tenant_b))
        await app.execute(
            "SELECT set_config('app.principal_id', $1, false)", str(principal_b)
        )
        assert (
            await app.fetchval(
                "SELECT count(*) FROM context_receipts WHERE id = $1", receipt_id
            )
            == 0
        )

        # Cross-tenant INSERT (tenant B context, tenant A row) rejected by WITH
        # CHECK before any row lands.
        import asyncpg

        with pytest.raises(asyncpg.PostgresError) as exc_info:
            manifest = _valid_manifest_jsonb(
                tenant_id=tenant_a, principal_id=principal_a, item_ids=item_ids
            )
            import hashlib

            mh = "sha256:" + hashlib.sha256(
                json.dumps(manifest, sort_keys=True).encode("utf-8")
            ).hexdigest()
            await app.execute(
                "INSERT INTO context_receipts (tenant_id, principal_id, "
                "recall_log_id, manifest_schema, manifest_schema_version, "
                "canonicalization, mode, manifest, manifest_hash, packet_hash) "
                "VALUES ($1, $2, $3, 'engram.context-manifest', '1.0', 'rfc8785', "
                "'startup', $4::jsonb, $5, $6)",
                tenant_a, principal_a, recall_log_a,
                json.dumps(manifest), mh, manifest["packet"]["hash"],
            )
        assert _denied(exc_info.value)
    finally:
        with contextlib.suppress(Exception):
            await owner.execute(
                "DELETE FROM context_receipts WHERE tenant_id = ANY($1::uuid[])",
                [tenant_a, tenant_b],
            )
        with contextlib.suppress(Exception):
            await owner.execute(
                "DELETE FROM tenants WHERE id = ANY($1::uuid[])", [tenant_a, tenant_b]
            )
        await app.close()
        await owner.close()


async def test_unset_context_cannot_insert() -> None:
    owner = await _owner_with_026()
    app = await _connect(_app_dsn())  # type: ignore[arg-type]
    try:
        # No GUCs set at all -> WITH CHECK rejects any insert.
        import asyncpg

        with pytest.raises(asyncpg.PostgresError) as exc_info:
            manifest = _valid_manifest_jsonb(
                tenant_id=uuid.uuid4(), principal_id=uuid.uuid4(), item_ids=[]
            )
            import hashlib

            mh = "sha256:" + hashlib.sha256(
                json.dumps(manifest, sort_keys=True).encode("utf-8")
            ).hexdigest()
            await app.execute(
                "INSERT INTO context_receipts (tenant_id, principal_id, "
                "recall_log_id, manifest_schema, manifest_schema_version, "
                "canonicalization, mode, manifest, manifest_hash, packet_hash) "
                "VALUES ($1, $2, $3, 'engram.context-manifest', '1.0', 'rfc8785', "
                "'startup', $4::jsonb, $5, $6)",
                uuid.uuid4(), uuid.uuid4(), uuid.uuid4(),
                json.dumps(manifest), mh, manifest["packet"]["hash"],
            )
        assert _denied(exc_info.value)
    finally:
        await app.close()
        await owner.close()


async def test_required_constraints_exist() -> None:
    owner = await _owner_with_026()
    try:
        constraint_names = {
            row["conname"]
            for row in await owner.fetch(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'context_receipts'::regclass"
            )
        }
        for name in (
            "fk_context_receipts_recall_log",
            "fk_context_receipts_tenant",
            "fk_context_receipts_principal",
            "chk_context_receipts_schema",
            "chk_context_receipts_schema_version",
            "chk_context_receipts_canonicalization",
            "chk_context_receipts_mode",
            "chk_context_receipts_manifest_hash",
            "chk_context_receipts_packet_hash",
            "chk_context_receipts_manifest_is_object",
            "chk_context_receipts_manifest_sections",
            "chk_context_receipts_schema_agreement",
            "chk_context_receipts_subject_tenant",
            "chk_context_receipts_subject_principal",
            "chk_context_receipts_packet_agreement",
            "chk_context_receipts_no_manifest_hash_field",
            "chk_context_receipts_retention",
        ):
            assert name in constraint_names, f"missing constraint: {name}"

        # The recall-log unique identity exists on recall_logs.
        rl_unique = await owner.fetchval(
            "SELECT count(*) FROM pg_constraint "
            "WHERE conrelid = 'recall_logs'::regclass "
            "AND conname = 'uq_recall_logs_tenant_principal_id'"
        )
        assert rl_unique == 1
    finally:
        await owner.close()


async def test_required_indexes_exist() -> None:
    owner = await _owner_with_026()
    try:
        index_names = {
            row["indexname"]
            for row in await owner.fetch(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'context_receipts'"
            )
        }
        for name in (
            "idx_context_receipts_recall_log",
            "idx_context_receipts_principal_timeline",
            "idx_context_receipts_tenant_manifest_hash",
            "idx_context_receipts_retention_sweep",
        ):
            assert name in index_names, f"missing index: {name}"

        # The recall-log index is unique.
        unique = await owner.fetchval(
            "SELECT indisunique FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indrelid "
            "JOIN pg_class ci ON ci.oid = i.indexrelid "
            "WHERE c.relname = 'context_receipts' AND ci.relname = "
            "'idx_context_receipts_recall_log'"
        )
        assert unique is True
    finally:
        await owner.close()


async def test_valid_row_satisfies_all_checks() -> None:
    owner = await _owner_with_026()
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    recall_log_id = uuid.uuid4()
    receipt_id = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    try:
        await _seed_tenant_principal(
            owner, tenant_id=tenant_id, principal_id=principal_id, label="valid"
        )
        await _insert_recall_log(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=recall_log_id,
            item_ids=item_ids,
        )
        await _owner_insert_valid_receipt(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=recall_log_id,
            receipt_id=receipt_id,
            item_ids=item_ids,
        )
        row = await owner.fetchrow(
            "SELECT id, tenant_id, principal_id, recall_log_id, manifest_schema, "
            "manifest_schema_version, canonicalization, mode, manifest_hash, "
            "packet_hash FROM context_receipts WHERE id = $1",
            receipt_id,
        )
        assert row is not None
        assert row["manifest_schema"] == "engram.context-manifest"
        assert row["mode"] == "startup"
    finally:
        with contextlib.suppress(Exception):
            await owner.execute(
                "DELETE FROM context_receipts WHERE tenant_id = $1", tenant_id
            )
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await owner.close()


async def test_invalid_manifest_hash_rejected() -> None:
    owner = await _owner_with_026()
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    recall_log_id = uuid.uuid4()
    receipt_id = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    try:
        await _seed_tenant_principal(
            owner, tenant_id=tenant_id, principal_id=principal_id, label="badhash"
        )
        await _insert_recall_log(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=recall_log_id,
            item_ids=item_ids,
        )
        manifest = _valid_manifest_jsonb(
            tenant_id=tenant_id, principal_id=principal_id, item_ids=item_ids
        )
        import asyncpg

        with pytest.raises(asyncpg.PostgresError) as exc_info:
            await owner.execute(
                "INSERT INTO context_receipts (id, tenant_id, principal_id, "
                "recall_log_id, manifest_schema, manifest_schema_version, "
                "canonicalization, mode, manifest, manifest_hash, packet_hash) "
                "VALUES ($1, $2, $3, $4, 'engram.context-manifest', '1.0', "
                "'rfc8785', 'startup', $5::jsonb, 'not-a-sha256', $6)",
                receipt_id, tenant_id, principal_id, recall_log_id,
                json.dumps(manifest), manifest["packet"]["hash"],
            )
        assert _check_violation(exc_info.value)
    finally:
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await owner.close()


async def test_envelope_manifest_tenant_mismatch_rejected() -> None:
    owner = await _owner_with_026()
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    recall_log_id = uuid.uuid4()
    receipt_id = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    try:
        await _seed_tenant_principal(
            owner, tenant_id=tenant_id, principal_id=principal_id, label="tnmismatch"
        )
        await _insert_recall_log(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=recall_log_id,
            item_ids=item_ids,
        )
        # Manifest subject describes a different tenant than the envelope.
        manifest = _valid_manifest_jsonb(
            tenant_id=uuid.uuid4(), principal_id=principal_id, item_ids=item_ids
        )
        import asyncpg

        with pytest.raises(asyncpg.PostgresError) as exc_info:
            await owner.execute(
                "INSERT INTO context_receipts (id, tenant_id, principal_id, "
                "recall_log_id, manifest_schema, manifest_schema_version, "
                "canonicalization, mode, manifest, manifest_hash, packet_hash) "
                "VALUES ($1, $2, $3, $4, 'engram.context-manifest', '1.0', "
                "'rfc8785', 'startup', $5::jsonb, $6, $7)",
                receipt_id, tenant_id, principal_id, recall_log_id,
                json.dumps(manifest), "sha256:" + "a" * 64,
                manifest["packet"]["hash"],
            )
        assert _check_violation(exc_info.value)
    finally:
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await owner.close()


async def test_envelope_manifest_principal_mismatch_rejected() -> None:
    owner = await _owner_with_026()
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    recall_log_id = uuid.uuid4()
    receipt_id = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    try:
        await _seed_tenant_principal(
            owner, tenant_id=tenant_id, principal_id=principal_id, label="pmismatch"
        )
        await _insert_recall_log(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=recall_log_id,
            item_ids=item_ids,
        )
        manifest = _valid_manifest_jsonb(
            tenant_id=tenant_id, principal_id=uuid.uuid4(), item_ids=item_ids
        )
        import asyncpg

        with pytest.raises(asyncpg.PostgresError) as exc_info:
            await owner.execute(
                "INSERT INTO context_receipts (id, tenant_id, principal_id, "
                "recall_log_id, manifest_schema, manifest_schema_version, "
                "canonicalization, mode, manifest, manifest_hash, packet_hash) "
                "VALUES ($1, $2, $3, $4, 'engram.context-manifest', '1.0', "
                "'rfc8785', 'startup', $5::jsonb, $6, $7)",
                receipt_id, tenant_id, principal_id, recall_log_id,
                json.dumps(manifest), "sha256:" + "a" * 64,
                manifest["packet"]["hash"],
            )
        assert _check_violation(exc_info.value)
    finally:
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await owner.close()


async def test_envelope_manifest_packet_hash_mismatch_rejected() -> None:
    owner = await _owner_with_026()
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    recall_log_id = uuid.uuid4()
    receipt_id = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    try:
        await _seed_tenant_principal(
            owner, tenant_id=tenant_id, principal_id=principal_id, label="pktmismatch"
        )
        await _insert_recall_log(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=recall_log_id,
            item_ids=item_ids,
        )
        manifest = _valid_manifest_jsonb(
            tenant_id=tenant_id, principal_id=principal_id, item_ids=item_ids
        )
        import asyncpg

        with pytest.raises(asyncpg.PostgresError) as exc_info:
            await owner.execute(
                "INSERT INTO context_receipts (id, tenant_id, principal_id, "
                "recall_log_id, manifest_schema, manifest_schema_version, "
                "canonicalization, mode, manifest, manifest_hash, packet_hash) "
                "VALUES ($1, $2, $3, $4, 'engram.context-manifest', '1.0', "
                "'rfc8785', 'startup', $5::jsonb, $6, $7)",
                receipt_id, tenant_id, principal_id, recall_log_id,
                json.dumps(manifest), "sha256:" + "a" * 64,
                "sha256:" + "b" * 64,  # different from manifest.packet.hash
            )
        assert _check_violation(exc_info.value)
    finally:
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await owner.close()


async def test_second_receipt_for_one_recall_log_rejected() -> None:
    owner = await _owner_with_026()
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    recall_log_id = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    try:
        await _seed_tenant_principal(
            owner, tenant_id=tenant_id, principal_id=principal_id, label="second"
        )
        await _insert_recall_log(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=recall_log_id,
            item_ids=item_ids,
        )
        await _owner_insert_valid_receipt(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=recall_log_id,
            receipt_id=uuid.uuid4(),
            item_ids=item_ids,
        )
        import asyncpg

        with pytest.raises(asyncpg.PostgresError) as exc_info:
            await _owner_insert_valid_receipt(
                owner,
                tenant_id=tenant_id,
                principal_id=principal_id,
                recall_log_id=recall_log_id,
                receipt_id=uuid.uuid4(),
                item_ids=item_ids,
            )
        assert _unique_violation(exc_info.value)
    finally:
        with contextlib.suppress(Exception):
            await owner.execute(
                "DELETE FROM context_receipts WHERE tenant_id = $1", tenant_id
            )
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await owner.close()


async def test_cross_tenant_recall_log_attachment_rejected() -> None:
    owner = await _owner_with_026()
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    principal_a = uuid.uuid4()
    principal_b = uuid.uuid4()
    recall_log_a = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    try:
        await _seed_tenant_principal(
            owner, tenant_id=tenant_a, principal_id=principal_a, label="cta"
        )
        await _seed_tenant_principal(
            owner, tenant_id=tenant_b, principal_id=principal_b, label="ctb"
        )
        await _insert_recall_log(
            owner,
            tenant_id=tenant_a,
            principal_id=principal_a,
            recall_log_id=recall_log_a,
            item_ids=item_ids,
        )
        # Try to attach a receipt owned by tenant B to tenant A's recall log.
        manifest = _valid_manifest_jsonb(
            tenant_id=tenant_b, principal_id=principal_b, item_ids=item_ids
        )
        import asyncpg

        with pytest.raises(asyncpg.PostgresError) as exc_info:
            await owner.execute(
                "INSERT INTO context_receipts (id, tenant_id, principal_id, "
                "recall_log_id, manifest_schema, manifest_schema_version, "
                "canonicalization, mode, manifest, manifest_hash, packet_hash) "
                "VALUES ($1, $2, $3, $4, 'engram.context-manifest', '1.0', "
                "'rfc8785', 'startup', $5::jsonb, $6, $7)",
                uuid.uuid4(), tenant_b, principal_b, recall_log_a,
                json.dumps(manifest), "sha256:" + "a" * 64,
                manifest["packet"]["hash"],
            )
        assert _fk_violation(exc_info.value)
    finally:
        with contextlib.suppress(Exception):
            await owner.execute(
                "DELETE FROM tenants WHERE id = ANY($1::uuid[])",
                [tenant_a, tenant_b],
            )
        await owner.close()


async def test_cross_principal_recall_log_attachment_rejected() -> None:
    owner = await _owner_with_026()
    tenant_id = uuid.uuid4()
    principal_a = uuid.uuid4()
    principal_b = uuid.uuid4()
    recall_log_a = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    try:
        await _seed_tenant_principal(
            owner, tenant_id=tenant_id, principal_id=principal_a, label="cpa"
        )
        await owner.execute(
            "INSERT INTO principals (id, tenant_id, name, type) "
            "VALUES ($1, $2, 'b', 'agent')",
            principal_b, tenant_id,
        )
        await _insert_recall_log(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_a,
            recall_log_id=recall_log_a,
            item_ids=item_ids,
        )
        manifest = _valid_manifest_jsonb(
            tenant_id=tenant_id, principal_id=principal_b, item_ids=item_ids
        )
        import asyncpg

        with pytest.raises(asyncpg.PostgresError) as exc_info:
            await owner.execute(
                "INSERT INTO context_receipts (id, tenant_id, principal_id, "
                "recall_log_id, manifest_schema, manifest_schema_version, "
                "canonicalization, mode, manifest, manifest_hash, packet_hash) "
                "VALUES ($1, $2, $3, $4, 'engram.context-manifest', '1.0', "
                "'rfc8785', 'startup', $5::jsonb, $6, $7)",
                uuid.uuid4(), tenant_id, principal_b, recall_log_a,
                json.dumps(manifest), "sha256:" + "a" * 64,
                manifest["packet"]["hash"],
            )
        assert _fk_violation(exc_info.value)
    finally:
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await owner.close()


async def test_recall_log_deletion_restricted_while_receipt_remains() -> None:
    owner = await _owner_with_026()
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    recall_log_id = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    try:
        await _seed_tenant_principal(
            owner, tenant_id=tenant_id, principal_id=principal_id, label="restrict"
        )
        await _insert_recall_log(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=recall_log_id,
            item_ids=item_ids,
        )
        await _owner_insert_valid_receipt(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=recall_log_id,
            receipt_id=uuid.uuid4(),
            item_ids=item_ids,
        )
        import asyncpg

        # Deferred constraint fires at commit.
        with pytest.raises(asyncpg.PostgresError) as exc_info:
            async with owner.transaction():
                await owner.execute(
                    "DELETE FROM recall_logs WHERE id = $1", recall_log_id
                )
        assert _fk_violation(exc_info.value)

        # After removing the receipt, the recall log can be deleted (owner).
        await owner.execute(
            "DELETE FROM context_receipts WHERE recall_log_id = $1", recall_log_id
        )
        await owner.execute("DELETE FROM recall_logs WHERE id = $1", recall_log_id)
    finally:
        with contextlib.suppress(Exception):
            await owner.execute(
                "DELETE FROM context_receipts WHERE tenant_id = $1", tenant_id
            )
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await owner.close()


async def test_invalid_retention_expiry_rejected() -> None:
    owner = await _owner_with_026()
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    recall_log_id = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    try:
        await _seed_tenant_principal(
            owner, tenant_id=tenant_id, principal_id=principal_id, label="ret"
        )
        await _insert_recall_log(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=recall_log_id,
            item_ids=item_ids,
        )
        manifest = _valid_manifest_jsonb(
            tenant_id=tenant_id, principal_id=principal_id, item_ids=item_ids
        )
        from datetime import datetime, timedelta

        import asyncpg

        past = datetime.now(UTC) - timedelta(days=1)
        with pytest.raises(asyncpg.PostgresError) as exc_info:
            await owner.execute(
                "INSERT INTO context_receipts (id, tenant_id, principal_id, "
                "recall_log_id, manifest_schema, manifest_schema_version, "
                "canonicalization, mode, manifest, manifest_hash, packet_hash, "
                "retention_expires_at) "
                "VALUES ($1, $2, $3, $4, 'engram.context-manifest', '1.0', "
                "'rfc8785', 'startup', $5::jsonb, $6, $7, $8)",
                uuid.uuid4(), tenant_id, principal_id, recall_log_id,
                json.dumps(manifest), "sha256:" + "a" * 64,
                manifest["packet"]["hash"], past,
            )
        assert _check_violation(exc_info.value)
    finally:
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await owner.close()


async def test_manifest_hash_field_inside_manifest_rejected() -> None:
    owner = await _owner_with_026()
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    recall_log_id = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    try:
        await _seed_tenant_principal(
            owner, tenant_id=tenant_id, principal_id=principal_id, label="mhfield"
        )
        await _insert_recall_log(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=recall_log_id,
            item_ids=item_ids,
        )
        manifest = _valid_manifest_jsonb(
            tenant_id=tenant_id,
            principal_id=principal_id,
            item_ids=item_ids,
            manifest_hash="sha256:" + "a" * 64,
        )
        import asyncpg

        with pytest.raises(asyncpg.PostgresError) as exc_info:
            await owner.execute(
                "INSERT INTO context_receipts (id, tenant_id, principal_id, "
                "recall_log_id, manifest_schema, manifest_schema_version, "
                "canonicalization, mode, manifest, manifest_hash, packet_hash) "
                "VALUES ($1, $2, $3, $4, 'engram.context-manifest', '1.0', "
                "'rfc8785', 'startup', $5::jsonb, $6, $7)",
                uuid.uuid4(), tenant_id, principal_id, recall_log_id,
                json.dumps(manifest), "sha256:" + "a" * 64,
                manifest["packet"]["hash"],
            )
        assert _check_violation(exc_info.value)
    finally:
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await owner.close()


# ─── Absent JSON key rejection (IS TRUE CHECK semantics) ───────────────
# PostgreSQL passes a CHECK when its expression is TRUE OR NULL. JSON
# accessors return NULL for absent keys, so the migration wraps each nullable
# JSON comparison in IS TRUE. These tests prove an envelope with absent
# contract fields is rejected at the database level.


async def _assert_manifest_insert_rejected(
    owner: Any,
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    recall_log_id: uuid.UUID,
    manifest_jsonb: str,
    packet_hash: str = "sha256:" + "c" * 64,
) -> None:
    import asyncpg

    with pytest.raises(asyncpg.PostgresError) as exc_info:
        await owner.execute(
            "INSERT INTO context_receipts (tenant_id, principal_id, "
            "recall_log_id, manifest_schema, manifest_schema_version, "
            "canonicalization, mode, manifest, manifest_hash, packet_hash) "
            "VALUES ($1, $2, $3, 'engram.context-manifest', '1.0', "
            "'rfc8785', 'startup', $4::jsonb, $5, $6)",
            tenant_id, principal_id, recall_log_id,
            manifest_jsonb, "sha256:" + "a" * 64, packet_hash,
        )
    assert _check_violation(exc_info.value), (
        f"expected CHECK violation, got: {exc_info.value!r}"
    )


async def test_empty_jsonb_manifest_rejected() -> None:
    owner = await _owner_with_026()
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    recall_log_id = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    try:
        await _seed_tenant_principal(
            owner, tenant_id=tenant_id, principal_id=principal_id, label="empty"
        )
        await _insert_recall_log(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=recall_log_id,
            item_ids=item_ids,
        )
        await _assert_manifest_insert_rejected(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=recall_log_id,
            manifest_jsonb="{}",
        )
    finally:
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await owner.close()


async def test_missing_required_top_level_section_rejected() -> None:
    owner = await _owner_with_026()
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    recall_log_id = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    try:
        await _seed_tenant_principal(
            owner, tenant_id=tenant_id, principal_id=principal_id, label="misssec"
        )
        await _insert_recall_log(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=recall_log_id,
            item_ids=item_ids,
        )
        # Drop each required section in turn and prove rejection.
        for section in ("subject", "request", "versions", "result", "packet", "items"):
            manifest = _valid_manifest_jsonb(
                tenant_id=tenant_id, principal_id=principal_id, item_ids=item_ids
            )
            manifest.pop(section, None)
            await _assert_manifest_insert_rejected(
                owner,
                tenant_id=tenant_id,
                principal_id=principal_id,
                recall_log_id=recall_log_id,
                manifest_jsonb=json.dumps(manifest),
            )
    finally:
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await owner.close()


async def test_missing_envelope_protocol_marker_rejected() -> None:
    owner = await _owner_with_026()
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    recall_log_id = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    try:
        await _seed_tenant_principal(
            owner, tenant_id=tenant_id, principal_id=principal_id, label="missmarker"
        )
        await _insert_recall_log(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=recall_log_id,
            item_ids=item_ids,
        )
        for marker in ("schema", "schema_version", "canonicalization", "mode"):
            manifest = _valid_manifest_jsonb(
                tenant_id=tenant_id, principal_id=principal_id, item_ids=item_ids
            )
            manifest.pop(marker, None)
            await _assert_manifest_insert_rejected(
                owner,
                tenant_id=tenant_id,
                principal_id=principal_id,
                recall_log_id=recall_log_id,
                manifest_jsonb=json.dumps(manifest),
            )
    finally:
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await owner.close()


async def test_missing_subject_tenant_or_principal_rejected() -> None:
    owner = await _owner_with_026()
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    recall_log_id = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    try:
        await _seed_tenant_principal(
            owner, tenant_id=tenant_id, principal_id=principal_id, label="misssub"
        )
        await _insert_recall_log(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=recall_log_id,
            item_ids=item_ids,
        )
        for field in ("tenant_id", "principal_id"):
            manifest = _valid_manifest_jsonb(
                tenant_id=tenant_id, principal_id=principal_id, item_ids=item_ids
            )
            manifest["subject"].pop(field, None)
            await _assert_manifest_insert_rejected(
                owner,
                tenant_id=tenant_id,
                principal_id=principal_id,
                recall_log_id=recall_log_id,
                manifest_jsonb=json.dumps(manifest),
            )
    finally:
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await owner.close()


async def test_missing_packet_hash_rejected() -> None:
    owner = await _owner_with_026()
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    recall_log_id = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    try:
        await _seed_tenant_principal(
            owner, tenant_id=tenant_id, principal_id=principal_id, label="misspkt"
        )
        await _insert_recall_log(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=recall_log_id,
            item_ids=item_ids,
        )
        manifest = _valid_manifest_jsonb(
            tenant_id=tenant_id, principal_id=principal_id, item_ids=item_ids
        )
        manifest["packet"].pop("hash", None)
        await _assert_manifest_insert_rejected(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=recall_log_id,
            manifest_jsonb=json.dumps(manifest),
        )
    finally:
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await owner.close()


async def test_migration_is_reapplicable() -> None:
    """Re-running migration 026 must not duplicate objects or fail."""
    owner = await _owner_with_026()
    try:
        # Apply the migration a second time directly; it must succeed and leave
        # the object counts unchanged.
        from pathlib import Path

        sql = Path("migrations/026_context_receipts.sql").read_text()
        before_constraints = await owner.fetchval(
            "SELECT count(*) FROM pg_constraint "
            "WHERE conrelid = 'context_receipts'::regclass"
        )
        before_policies = await owner.fetchval(
            "SELECT count(*) FROM pg_policies WHERE tablename = 'context_receipts'"
        )
        await owner.execute(sql)
        after_constraints = await owner.fetchval(
            "SELECT count(*) FROM pg_constraint "
            "WHERE conrelid = 'context_receipts'::regclass"
        )
        after_policies = await owner.fetchval(
            "SELECT count(*) FROM pg_policies WHERE tablename = 'context_receipts'"
        )
        assert after_constraints == before_constraints
        assert after_policies == before_policies == 1
    finally:
        await owner.close()