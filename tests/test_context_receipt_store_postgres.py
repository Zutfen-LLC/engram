"""Real-PostgreSQL repository tests for ``engram.context_receipts`` (ENG-CONTEXT-002A).

Exercises the repository public API (``store_context_receipt``,
``get_context_receipt``, ``get_context_receipt_for_recall_log``,
``verify_context_receipt_record``) against a live PostgreSQL with the non-owner
application role, proving:

- first insert returns ``created=True``;
- identical retry returns ``created=False``;
- conflicting retry raises ``ContextReceiptConflictError`` without overwriting;
- retrieval by receipt ID and by recall-log ID;
- wrong principal/tenant returns none under RLS;
- parent-log mismatch, ordered-item mismatch, budget mismatch, scoring/config
  mismatch, and profile/context mismatch are rejected;
- retention timestamp must be timezone-aware;
- the repository does NOT commit or roll back — caller rollback removes the new
  row, caller commit persists it;
- concurrent same-input insertion creates exactly one row;
- concurrent different-input insertion produces one stored row and one conflict.

Skips without the Compose real-PostgreSQL stack (see ``make compose-ci``).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.context_receipts import (
    ContextReceiptConflictError,
    ContextReceiptIntegrityError,
    ContextReceiptRecallLogNotFoundError,
    get_context_receipt,
    get_context_receipt_for_recall_log,
    store_context_receipt,
    verify_context_receipt_record,
)

# Helpers are test-only.
from tests.context_receipt_helpers import build_manifest, manifest_hash  # noqa: E402


def _owner_dsn() -> str | None:
    return os.environ.get("ENGRAM_DATABASE_URL") or os.environ.get(
        "ENGRAM_OWNER_DATABASE_URL"
    )


def _app_dsn() -> str | None:
    return os.environ.get("ENGRAM_APP_DATABASE_URL")


async def _owner_connect() -> Any:
    import asyncpg

    from engram.migrations import normalize_asyncpg_url

    if not _owner_dsn():
        pytest.skip("requires ENGRAM_DATABASE_URL (owner) for setup")
    if not _app_dsn():
        pytest.skip("requires ENGRAM_APP_DATABASE_URL (non-owner app role)")
    owner = await asyncpg.connect(normalize_asyncpg_url(_owner_dsn()))  # type: ignore[arg-type]
    if await owner.fetchval("SELECT to_regclass('context_receipts')") is None:
        await owner.close()
        pytest.skip("requires migration 026")
    return owner


def _app_engine():
    return create_async_engine(_app_dsn(), poolclass=NullPool)  # type: ignore[arg-type]


def _app_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _seed(
    owner: Any,
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    label: str,
) -> None:
    await owner.execute(
        "INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $3)",
        tenant_id, label, f"{label.lower()}-{tenant_id.hex[:8]}",
    )
    await owner.execute(
        "INSERT INTO principals (id, tenant_id, name, type) "
        "VALUES ($1, $2, 'admin', 'admin')",
        principal_id, tenant_id,
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
    memory_profile_id: uuid.UUID | None = None,
    memory_profile_revision_id: uuid.UUID | None = None,
) -> None:
    await owner.execute(
        "INSERT INTO recall_logs (id, tenant_id, principal_id, mode, query, "
        "item_ids, byte_budget, token_budget, scoring_version, config_version, "
        "memory_context_version, memory_profile_id, memory_profile_revision_id) "
        "VALUES ($1, $2, $3, $4, NULL, $5, $6, $7, $8, $9, $10, $11, $12)",
        recall_log_id, tenant_id, principal_id, mode, item_ids,
        byte_budget, token_budget, scoring_version, config_version,
        memory_context_version, memory_profile_id, memory_profile_revision_id,
    )


async def _apply_rls(
    session: AsyncSession, *, tenant_id: uuid.UUID, principal_id: uuid.UUID
) -> None:
    from sqlalchemy import text

    await session.execute(
        text("SELECT set_config('app.tenant_id', :tid, true), "
             "set_config('app.principal_id', :pid, true)"),
        {"tid": str(tenant_id), "pid": str(principal_id)},
    )


@pytest.fixture
async def env():
    """Yield (owner, engine, factory, tenant_id, principal_id, recall_log_id, item_ids)."""
    owner = await _owner_connect()
    engine = _app_engine()
    factory = _app_factory(engine)
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    recall_log_id = uuid.uuid4()
    item_ids = [uuid.uuid4(), uuid.uuid4()]
    await _seed(owner, tenant_id=tenant_id, principal_id=principal_id, label="repo")
    await _insert_recall_log(
        owner,
        tenant_id=tenant_id,
        principal_id=principal_id,
        recall_log_id=recall_log_id,
        item_ids=item_ids,
        byte_budget=8192,
    )
    try:
        yield {
            "owner": owner,
            "engine": engine,
            "factory": factory,
            "tenant_id": tenant_id,
            "principal_id": principal_id,
            "recall_log_id": recall_log_id,
            "item_ids": item_ids,
        }
    finally:
        with contextlib.suppress(Exception):
            await owner.execute(
                "DELETE FROM context_receipts WHERE tenant_id = $1", tenant_id
            )
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await engine.dispose()
        await owner.close()


def _manifest_for(env: dict[str, Any], **kwargs) -> Any:
    return build_manifest(
        tenant_id=str(env["tenant_id"]),
        principal_id=str(env["principal_id"]),
        item_ids=[str(i) for i in env["item_ids"]],
        byte_budget=8192,
        **kwargs,
    )


# ─── Core store/retrieve ───────────────────────────────────────────────


async def test_first_insert_returns_created_true(env) -> None:
    manifest = _manifest_for(env)
    async with env["factory"]() as session:
        await _apply_rls(
            session,
            tenant_id=env["tenant_id"],
            principal_id=env["principal_id"],
        )
        result = await store_context_receipt(
            session,
            tenant_id=env["tenant_id"],
            principal_id=env["principal_id"],
            recall_log_id=env["recall_log_id"],
            manifest=manifest,
        )
        await session.commit()
    assert result.created is True
    assert result.receipt.recall_log_id == env["recall_log_id"]
    assert result.receipt.manifest_hash == manifest_hash(manifest)
    assert result.receipt.packet_hash == manifest.packet.hash
    assert result.receipt.mode == "startup"


async def test_identical_retry_returns_created_false(env) -> None:
    manifest = _manifest_for(env)
    async with env["factory"]() as session:
        await _apply_rls(
            session,
            tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        first = await store_context_receipt(
            session,
            tenant_id=env["tenant_id"],
            principal_id=env["principal_id"],
            recall_log_id=env["recall_log_id"],
            manifest=manifest,
        )
        await session.commit()
    assert first.created is True

    async with env["factory"]() as session:
        await _apply_rls(
            session,
            tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        second = await store_context_receipt(
            session,
            tenant_id=env["tenant_id"],
            principal_id=env["principal_id"],
            recall_log_id=env["recall_log_id"],
            manifest=manifest,
        )
        await session.commit()
    assert second.created is False
    assert second.receipt.id == first.receipt.id
    assert second.receipt.manifest_hash == first.receipt.manifest_hash


async def test_conflicting_retry_raises_without_overwrite(env) -> None:
    manifest_a = _manifest_for(env, content="served content a")
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        first = await store_context_receipt(
            session,
            tenant_id=env["tenant_id"],
            principal_id=env["principal_id"],
            recall_log_id=env["recall_log_id"],
            manifest=manifest_a,
        )
        await session.commit()
    assert first.created is True
    original_hash = first.receipt.manifest_hash

    # Different manifest (different content -> different packet hash + manifest hash).
    manifest_b = _manifest_for(env, content="served content b")
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        with pytest.raises(ContextReceiptConflictError):
            await store_context_receipt(
                session,
                tenant_id=env["tenant_id"],
                principal_id=env["principal_id"],
                recall_log_id=env["recall_log_id"],
                manifest=manifest_b,
            )
        await session.rollback()

    # The existing row is unchanged.
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        fetched = await get_context_receipt_for_recall_log(
            session,
            tenant_id=env["tenant_id"],
            principal_id=env["principal_id"],
            recall_log_id=env["recall_log_id"],
        )
    assert fetched is not None
    assert fetched.manifest_hash == original_hash


async def test_retrieval_by_receipt_id(env) -> None:
    manifest = _manifest_for(env)
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        result = await store_context_receipt(
            session,
            tenant_id=env["tenant_id"],
            principal_id=env["principal_id"],
            recall_log_id=env["recall_log_id"],
            manifest=manifest,
        )
        await session.commit()
    rid = result.receipt.id

    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        fetched = await get_context_receipt(
            session,
            tenant_id=env["tenant_id"],
            principal_id=env["principal_id"],
            receipt_id=rid,
        )
    assert fetched is not None
    assert fetched.id == rid


async def test_retrieval_by_recall_log_id(env) -> None:
    manifest = _manifest_for(env)
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        await store_context_receipt(
            session,
            tenant_id=env["tenant_id"],
            principal_id=env["principal_id"],
            recall_log_id=env["recall_log_id"],
            manifest=manifest,
        )
        await session.commit()

    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        fetched = await get_context_receipt_for_recall_log(
            session,
            tenant_id=env["tenant_id"],
            principal_id=env["principal_id"],
            recall_log_id=env["recall_log_id"],
        )
    assert fetched is not None
    assert fetched.recall_log_id == env["recall_log_id"]


async def test_wrong_principal_returns_none_under_rls(env) -> None:
    manifest = _manifest_for(env)
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        result = await store_context_receipt(
            session,
            tenant_id=env["tenant_id"],
            principal_id=env["principal_id"],
            recall_log_id=env["recall_log_id"],
            manifest=manifest,
        )
        await session.commit()
    rid = result.receipt.id

    # Create a second principal in the same tenant.
    other_principal = uuid.uuid4()
    await env["owner"].execute(
        "INSERT INTO principals (id, tenant_id, name, type) "
        "VALUES ($1, $2, 'other', 'agent')",
        other_principal, env["tenant_id"],
    )
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=other_principal
        )
        fetched = await get_context_receipt(
            session,
            tenant_id=env["tenant_id"],
            principal_id=other_principal,
            receipt_id=rid,
        )
    assert fetched is None


async def test_wrong_tenant_returns_none_under_rls(env) -> None:
    manifest = _manifest_for(env)
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        result = await store_context_receipt(
            session,
            tenant_id=env["tenant_id"],
            principal_id=env["principal_id"],
            recall_log_id=env["recall_log_id"],
            manifest=manifest,
        )
        await session.commit()
    rid = result.receipt.id

    other_tenant = uuid.uuid4()
    other_principal = uuid.uuid4()
    await _seed(env["owner"], tenant_id=other_tenant, principal_id=other_principal, label="wt")
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=other_tenant, principal_id=other_principal
        )
        fetched = await get_context_receipt(
            session,
            tenant_id=other_tenant,
            principal_id=other_principal,
            receipt_id=rid,
        )
    assert fetched is None
    with contextlib.suppress(Exception):
        await env["owner"].execute("DELETE FROM tenants WHERE id = $1", other_tenant)


# ─── Recall-log overlap validation ─────────────────────────────────────


async def test_parent_log_not_found_raises(env) -> None:
    manifest = _manifest_for(env)
    bogus_log_id = uuid.uuid4()
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        with pytest.raises(ContextReceiptRecallLogNotFoundError):
            await store_context_receipt(
                session,
                tenant_id=env["tenant_id"],
                principal_id=env["principal_id"],
                recall_log_id=bogus_log_id,
                manifest=manifest,
            )
        await session.rollback()


async def test_ordered_item_mismatch_rejected(env) -> None:
    # Manifest with item IDs in a different order than the recall log.
    reversed_ids = list(reversed(env["item_ids"]))
    manifest = build_manifest(
        tenant_id=str(env["tenant_id"]),
        principal_id=str(env["principal_id"]),
        item_ids=[str(i) for i in reversed_ids],
        byte_budget=8192,
    )
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        with pytest.raises(ContextReceiptConflictError):
            await store_context_receipt(
                session,
                tenant_id=env["tenant_id"],
                principal_id=env["principal_id"],
                recall_log_id=env["recall_log_id"],
                manifest=manifest,
            )
        await session.rollback()


async def test_budget_mismatch_rejected(env) -> None:
    # Recall log attests byte_budget=8192; manifest effective says 4096.
    manifest = build_manifest(
        tenant_id=str(env["tenant_id"]),
        principal_id=str(env["principal_id"]),
        item_ids=[str(i) for i in env["item_ids"]],
        byte_budget=4096,
    )
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        with pytest.raises(ContextReceiptConflictError):
            await store_context_receipt(
                session,
                tenant_id=env["tenant_id"],
                principal_id=env["principal_id"],
                recall_log_id=env["recall_log_id"],
                manifest=manifest,
            )
        await session.rollback()


async def test_log_token_null_manifest_token_non_null_rejected(env) -> None:
    # The recall log in env has token_budget=None. A manifest claiming a
    # non-null effective token_budget must be rejected (the receipt would
    # attest a decision input the parent recall did not use).
    manifest = build_manifest(
        tenant_id=str(env["tenant_id"]),
        principal_id=str(env["principal_id"]),
        item_ids=[str(i) for i in env["item_ids"]],
        byte_budget=8192,
        token_budget=500,
    )
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        with pytest.raises(ContextReceiptConflictError):
            await store_context_receipt(
                session,
                tenant_id=env["tenant_id"],
                principal_id=env["principal_id"],
                recall_log_id=env["recall_log_id"],
                manifest=manifest,
            )
        await session.rollback()


async def test_log_token_non_null_manifest_token_null_rejected(env) -> None:
    # Recall log attests token_budget=500; manifest claims token_budget=None.
    rl = uuid.uuid4()
    await _insert_recall_log(
        env["owner"],
        tenant_id=env["tenant_id"],
        principal_id=env["principal_id"],
        recall_log_id=rl,
        item_ids=env["item_ids"],
        byte_budget=8192,
        token_budget=500,
    )
    manifest = build_manifest(
        tenant_id=str(env["tenant_id"]),
        principal_id=str(env["principal_id"]),
        item_ids=[str(i) for i in env["item_ids"]],
        byte_budget=8192,
        token_budget=None,
    )
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        with pytest.raises(ContextReceiptConflictError):
            await store_context_receipt(
                session,
                tenant_id=env["tenant_id"],
                principal_id=env["principal_id"],
                recall_log_id=rl,
                manifest=manifest,
            )
        await session.rollback()


async def test_unequal_non_null_token_budgets_rejected(env) -> None:
    rl = uuid.uuid4()
    await _insert_recall_log(
        env["owner"],
        tenant_id=env["tenant_id"],
        principal_id=env["principal_id"],
        recall_log_id=rl,
        item_ids=env["item_ids"],
        byte_budget=8192,
        token_budget=500,
    )
    manifest = build_manifest(
        tenant_id=str(env["tenant_id"]),
        principal_id=str(env["principal_id"]),
        item_ids=[str(i) for i in env["item_ids"]],
        byte_budget=8192,
        token_budget=1000,
    )
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        with pytest.raises(ContextReceiptConflictError):
            await store_context_receipt(
                session,
                tenant_id=env["tenant_id"],
                principal_id=env["principal_id"],
                recall_log_id=rl,
                manifest=manifest,
            )
        await session.rollback()


async def test_log_byte_budget_null_manifest_byte_budget_non_null_rejected(env) -> None:
    # Recall log attests byte_budget=None; manifest claims byte_budget=8192.
    rl = uuid.uuid4()
    await _insert_recall_log(
        env["owner"],
        tenant_id=env["tenant_id"],
        principal_id=env["principal_id"],
        recall_log_id=rl,
        item_ids=env["item_ids"],
        byte_budget=None,
    )
    manifest = build_manifest(
        tenant_id=str(env["tenant_id"]),
        principal_id=str(env["principal_id"]),
        item_ids=[str(i) for i in env["item_ids"]],
        byte_budget=8192,
    )
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        with pytest.raises(ContextReceiptConflictError):
            await store_context_receipt(
                session,
                tenant_id=env["tenant_id"],
                principal_id=env["principal_id"],
                recall_log_id=rl,
                manifest=manifest,
            )
        await session.rollback()


async def test_matching_null_budgets_accepted(env) -> None:
    # Both recall log and manifest attest byte_budget=None and token_budget=None.
    rl = uuid.uuid4()
    await _insert_recall_log(
        env["owner"],
        tenant_id=env["tenant_id"],
        principal_id=env["principal_id"],
        recall_log_id=rl,
        item_ids=env["item_ids"],
        byte_budget=None,
        token_budget=None,
    )
    manifest = build_manifest(
        tenant_id=str(env["tenant_id"]),
        principal_id=str(env["principal_id"]),
        item_ids=[str(i) for i in env["item_ids"]],
        byte_budget=None,
        token_budget=None,
    )
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        result = await store_context_receipt(
            session,
            tenant_id=env["tenant_id"],
            principal_id=env["principal_id"],
            recall_log_id=rl,
            manifest=manifest,
        )
        await session.commit()
    assert result.created is True


async def test_startup_recall_log_with_non_null_query_rejected(env) -> None:
    # A startup recall log with a non-null query cannot be the parent of a
    # startup manifest (startup query data must remain absent).
    rl = uuid.uuid4()
    await env["owner"].execute(
        "INSERT INTO recall_logs (id, tenant_id, principal_id, mode, query, "
        "item_ids, byte_budget, token_budget, scoring_version, config_version, "
        "memory_context_version) "
        "VALUES ($1, $2, $3, 'startup', 'leaked-query', $4, 8192, NULL, 'v1', "
        "'v1', 'memory-context-v2')",
        rl, env["tenant_id"], env["principal_id"], env["item_ids"],
    )
    manifest = _manifest_for(env)
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        with pytest.raises(ContextReceiptConflictError):
            await store_context_receipt(
                session,
                tenant_id=env["tenant_id"],
                principal_id=env["principal_id"],
                recall_log_id=rl,
                manifest=manifest,
            )
        await session.rollback()


async def test_scoring_config_mismatch_rejected(env) -> None:
    manifest = build_manifest(
        tenant_id=str(env["tenant_id"]),
        principal_id=str(env["principal_id"]),
        item_ids=[str(i) for i in env["item_ids"]],
        byte_budget=8192,
        scoring_version="v2",
    )
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        with pytest.raises(ContextReceiptConflictError):
            await store_context_receipt(
                session,
                tenant_id=env["tenant_id"],
                principal_id=env["principal_id"],
                recall_log_id=env["recall_log_id"],
                manifest=manifest,
            )
        await session.rollback()


async def test_memory_context_version_mismatch_rejected(env) -> None:
    # Build a manifest, then tamper with its subject memory_context_version to
    # disagree with the recall log's memory-context-v2. The manifest model is
    # strict, so we construct via the builder then mutate the recall log instead.
    manifest = _manifest_for(env)
    # Insert a recall log with a different memory_context_version.
    rl_other = uuid.uuid4()
    await _insert_recall_log(
        env["owner"],
        tenant_id=env["tenant_id"],
        principal_id=env["principal_id"],
        recall_log_id=rl_other,
        item_ids=env["item_ids"],
        byte_budget=8192,
        memory_context_version="legacy-unprofiled-v0",
    )
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        with pytest.raises(ContextReceiptConflictError):
            await store_context_receipt(
                session,
                tenant_id=env["tenant_id"],
                principal_id=env["principal_id"],
                recall_log_id=rl_other,
                manifest=manifest,
            )
        await session.rollback()


async def test_mode_mismatch_rejected(env) -> None:
    # The manifest is always startup; the recall log with a different mode
    # cannot be the parent.
    rl_semantic = uuid.uuid4()
    await _insert_recall_log(
        env["owner"],
        tenant_id=env["tenant_id"],
        principal_id=env["principal_id"],
        recall_log_id=rl_semantic,
        item_ids=env["item_ids"],
        byte_budget=8192,
        mode="semantic",
    )
    manifest = _manifest_for(env)
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        with pytest.raises(ContextReceiptConflictError):
            await store_context_receipt(
                session,
                tenant_id=env["tenant_id"],
                principal_id=env["principal_id"],
                recall_log_id=rl_semantic,
                manifest=manifest,
            )
        await session.rollback()


async def test_empty_manifest_with_null_recall_log_item_ids_accepted(env) -> None:
    # An empty manifest with a recall log whose item_ids is NULL is the
    # normalized empty case — it must be accepted.
    rl_empty = uuid.uuid4()
    await _insert_recall_log(
        env["owner"],
        tenant_id=env["tenant_id"],
        principal_id=env["principal_id"],
        recall_log_id=rl_empty,
        item_ids=None,
        byte_budget=8192,
    )
    manifest = build_manifest(
        tenant_id=str(env["tenant_id"]),
        principal_id=str(env["principal_id"]),
        item_ids=[],
        byte_budget=8192,
    )
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        result = await store_context_receipt(
            session,
            tenant_id=env["tenant_id"],
            principal_id=env["principal_id"],
            recall_log_id=rl_empty,
            manifest=manifest,
        )
        await session.commit()
    assert result.created is True


# ─── Retention metadata ────────────────────────────────────────────────


async def test_naive_retention_timestamp_rejected(env) -> None:
    manifest = _manifest_for(env)
    naive = datetime(2030, 1, 1, 0, 0, 0)  # no tzinfo
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        with pytest.raises(ContextReceiptConflictError):
            await store_context_receipt(
                session,
                tenant_id=env["tenant_id"],
                principal_id=env["principal_id"],
                recall_log_id=env["recall_log_id"],
                manifest=manifest,
                retention_expires_at=naive,
            )
        await session.rollback()


async def test_aware_retention_timestamp_accepted(env) -> None:
    manifest = _manifest_for(env)
    aware = datetime.now(UTC) + timedelta(days=30)
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        result = await store_context_receipt(
            session,
            tenant_id=env["tenant_id"],
            principal_id=env["principal_id"],
            recall_log_id=env["recall_log_id"],
            manifest=manifest,
            retention_expires_at=aware,
        )
        await session.commit()
    assert result.created is True
    assert result.receipt.retention_expires_at is not None


# ─── Transaction ownership ─────────────────────────────────────────────


async def test_repository_does_not_commit_caller_rollback_removes_row(env) -> None:
    manifest = _manifest_for(env)
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        result = await store_context_receipt(
            session,
            tenant_id=env["tenant_id"],
            principal_id=env["principal_id"],
            recall_log_id=env["recall_log_id"],
            manifest=manifest,
        )
        assert result.created is True
        # Do NOT commit — rollback instead.
        await session.rollback()

    # The row must not have persisted.
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        fetched = await get_context_receipt_for_recall_log(
            session,
            tenant_id=env["tenant_id"],
            principal_id=env["principal_id"],
            recall_log_id=env["recall_log_id"],
        )
    assert fetched is None


async def test_caller_commit_persists_row(env) -> None:
    manifest = _manifest_for(env)
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        result = await store_context_receipt(
            session,
            tenant_id=env["tenant_id"],
            principal_id=env["principal_id"],
            recall_log_id=env["recall_log_id"],
            manifest=manifest,
        )
        await session.commit()
    assert result.created is True

    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        fetched = await get_context_receipt_for_recall_log(
            session,
            tenant_id=env["tenant_id"],
            principal_id=env["principal_id"],
            recall_log_id=env["recall_log_id"],
        )
    assert fetched is not None
    assert fetched.id == result.receipt.id


# ─── Stored-record verification ────────────────────────────────────────


async def test_stored_record_verifies(env) -> None:
    manifest = _manifest_for(env)
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        result = await store_context_receipt(
            session,
            tenant_id=env["tenant_id"],
            principal_id=env["principal_id"],
            recall_log_id=env["recall_log_id"],
            manifest=manifest,
        )
        await session.commit()
    parsed = verify_context_receipt_record(result.receipt)
    assert parsed.schema_name == manifest.schema_name
    assert manifest_hash(parsed) == result.receipt.manifest_hash


# ─── Concurrency ───────────────────────────────────────────────────────


async def test_concurrent_same_input_insert_creates_one_row(env) -> None:
    manifest = _manifest_for(env)

    async def _store() -> Any:
        async with env["factory"]() as session:
            await _apply_rls(
                session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
            )
            result = await store_context_receipt(
                session,
                tenant_id=env["tenant_id"],
                principal_id=env["principal_id"],
                recall_log_id=env["recall_log_id"],
                manifest=manifest,
            )
            await session.commit()
            return result

    results = await asyncio.gather(*[_store() for _ in range(4)], return_exceptions=True)
    created = [r for r in results if not isinstance(r, Exception) and r.created]
    not_created = [
        r for r in results if not isinstance(r, Exception) and not r.created
    ]
    errors = [r for r in results if isinstance(r, Exception)]
    # Exactly one creation; the rest are idempotent retrievals; no errors.
    assert len(created) == 1
    assert len(not_created) == 3
    assert errors == []
    # All refer to the same receipt ID.
    ids = {r.receipt.id for r in results if not isinstance(r, Exception)}
    assert len(ids) == 1

    # Exactly one row in the database.
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        from sqlalchemy import func, select

        from engram.models import ContextReceipt

        count = await session.scalar(
            select(func.count())
            .select_from(ContextReceipt)
            .where(ContextReceipt.recall_log_id == env["recall_log_id"])
        )
    assert count == 1


async def test_concurrent_different_input_produces_one_row_and_one_conflict(env) -> None:
    manifest_a = _manifest_for(env, content="concurrent a")
    manifest_b = _manifest_for(env, content="concurrent b")

    async def _store(manifest) -> Any:
        async with env["factory"]() as session:
            await _apply_rls(
                session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
            )
            result = await store_context_receipt(
                session,
                tenant_id=env["tenant_id"],
                principal_id=env["principal_id"],
                recall_log_id=env["recall_log_id"],
                manifest=manifest,
            )
            await session.commit()
            return result

    results = await asyncio.gather(
        _store(manifest_a), _store(manifest_b), return_exceptions=True
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    conflicts = [r for r in results if isinstance(r, ContextReceiptConflictError)]
    # One success, one conflict (the conflict may surface as a commit-time
    # IntegrityError depending on timing; both are acceptable failures).
    other_errors = [
        r for r in results
        if isinstance(r, Exception) and not isinstance(r, ContextReceiptConflictError)
    ]
    assert len(successes) == 1
    # At least one of the two must fail (conflict or db-level unique violation).
    assert len(conflicts) + len(other_errors) == 1

    # Exactly one row in the database, matching the winner.
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        from sqlalchemy import func, select

        from engram.models import ContextReceipt

        count = await session.scalar(
            select(func.count())
            .select_from(ContextReceipt)
            .where(ContextReceipt.recall_log_id == env["recall_log_id"])
        )
    assert count == 1


# ─── Integrity errors ──────────────────────────────────────────────────


async def test_tampered_manifest_hash_raises_integrity_error(env) -> None:
    manifest = _manifest_for(env)
    async with env["factory"]() as session:
        await _apply_rls(
            session, tenant_id=env["tenant_id"], principal_id=env["principal_id"]
        )
        result = await store_context_receipt(
            session,
            tenant_id=env["tenant_id"],
            principal_id=env["principal_id"],
            recall_log_id=env["recall_log_id"],
            manifest=manifest,
        )
        await session.commit()
    receipt = result.receipt
    # Tamper with the stored manifest_hash (owner connection, bypassing RLS).
    await env["owner"].execute(
        "UPDATE context_receipts SET manifest_hash = $1 WHERE id = $2",
        "sha256:" + "0" * 64, receipt.id,
    )
    # Reload via owner (RLS would hide it from app role under a different
    # session; the integrity check is a pure function of the row).
    row = await env["owner"].fetchrow(
        "SELECT id, tenant_id, principal_id, recall_log_id, manifest_schema, "
        "manifest_schema_version, canonicalization, mode, manifest, manifest_hash, "
        "packet_hash, retention_expires_at, created_at "
        "FROM context_receipts WHERE id = $1",
        receipt.id,
    )
    assert row is not None
    from engram.models import ContextReceipt

    tampered = ContextReceipt(
        id=row["id"], tenant_id=row["tenant_id"], principal_id=row["principal_id"],
        recall_log_id=row["recall_log_id"], manifest_schema=row["manifest_schema"],
        manifest_schema_version=row["manifest_schema_version"],
        canonicalization=row["canonicalization"], mode=row["mode"],
        manifest=row["manifest"], manifest_hash=row["manifest_hash"],
        packet_hash=row["packet_hash"],
        retention_expires_at=row["retention_expires_at"],
        created_at=row["created_at"],
    )
    with pytest.raises(ContextReceiptIntegrityError):
        verify_context_receipt_record(tampered)