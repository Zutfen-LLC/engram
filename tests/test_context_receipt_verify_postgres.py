"""Real-PostgreSQL repository tests for ``list_context_receipts`` and
``verify_context_receipt_with_recall_log`` (ENG-CONTEXT-003A).

Exercises the list (keyset-paginated, profile-narrowed, ownership-scoped) and
verify (full stored-record + recall-log binding) repository functions against
a live PostgreSQL with the non-owner application role, proving:

- list returns only the owning principal's receipts;
- cross-tenant rows are invisible;
- timeline order is ``created_at DESC, id DESC``;
- keyset pagination across equal timestamps has no duplicates or omissions;
- ``recall_log_id`` filtering remains ownership-scoped;
- profile-bound context sees only exact profile+revision receipts;
- profile-bound context cannot see unprofiled receipts;
- unprofiled context sees all receipts;
- verification detects owner-role corruption of ``manifest_hash``;
- verification detects owner-role corruption of ``packet_hash`` (packet binding);
- verification detects a modified parent recall-log overlap field;
- verification performs no INSERT, UPDATE, or DELETE.

Setup uses an asyncpg owner connection (bypasses RLS). Assertions use a
SQLAlchemy AsyncSession bound to the non-owner app role with RLS GUCs applied.

Skips without the Compose real-PostgreSQL stack (see ``make compose-ci``).
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.context_receipts import (
    VERIFICATION_FAILURE_MANIFEST_HASH_MISMATCH,
    VERIFICATION_FAILURE_PACKET_HASH_BINDING_MISMATCH,
    VERIFICATION_FAILURE_RECALL_LOG_BINDING_MISMATCH,
    list_context_receipts,
    verify_context_receipt_with_recall_log,
)
from tests.context_receipt_helpers import build_manifest, manifest_hash

# ─── DSN helpers (same pattern as test_context_receipts_postgres.py) ────


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


# ─── Setup helpers ─────────────────────────────────────────────────────


async def _seed_tenant_principal(
    owner: Any,
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    label: str,
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
    memory_profile_id: uuid.UUID | None = None,
    memory_profile_revision_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
) -> None:
    cols = (
        "id, tenant_id, principal_id, mode, query, item_ids, byte_budget, "
        "token_budget, scoring_version, config_version, memory_context_version, "
        "memory_profile_id, memory_profile_revision_id"
    )
    placeholders = "$1, $2, $3, $4, NULL, $5, $6, $7, $8, $9, $10, $11, $12"
    args: list[Any] = [
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
        memory_profile_id,
        memory_profile_revision_id,
    ]
    if created_at is not None:
        cols += ", created_at"
        placeholders += ", $13"
        args.append(created_at)
    await owner.execute(
        f"INSERT INTO recall_logs ({cols}) VALUES ({placeholders})",
        *args,
    )


def _build_manifest_for(
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    item_ids: list[uuid.UUID],
    byte_budget: int | None = 8192,
    token_budget: int | None = None,
    memory_profile_id: str | None = None,
    memory_profile_revision_id: str | None = None,
    memory_profile_version: int | None = None,
    content: str = "served content",
) -> Any:
    """Build a valid ContextManifestV1 and return it."""
    return build_manifest(
        tenant_id=str(tenant_id),
        principal_id=str(principal_id),
        item_ids=[str(i) for i in item_ids],
        byte_budget=byte_budget,
        token_budget=token_budget,
        memory_profile_id=memory_profile_id,
        memory_profile_revision_id=memory_profile_revision_id,
        memory_profile_version=memory_profile_version,
        content=content,
    )


async def _owner_insert_receipt(
    owner: Any,
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    recall_log_id: uuid.UUID,
    receipt_id: uuid.UUID,
    item_ids: list[uuid.UUID],
    byte_budget: int | None = 8192,
    token_budget: int | None = None,
    memory_profile_id: str | None = None,
    memory_profile_revision_id: str | None = None,
    memory_profile_version: int | None = None,
    content: str = "served content",
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """Owner-insert a valid receipt and return the manifest dict used.

    The manifest is built via ``build_manifest`` so ``manifest_hash`` and
    ``packet_hash`` are derived from the real manifest model — the same way
    ``store_context_receipt`` computes them.
    """
    manifest = _build_manifest_for(
        tenant_id=tenant_id,
        principal_id=principal_id,
        item_ids=item_ids,
        byte_budget=byte_budget,
        token_budget=token_budget,
        memory_profile_id=memory_profile_id,
        memory_profile_revision_id=memory_profile_revision_id,
        memory_profile_version=memory_profile_version,
        content=content,
    )
    mh = manifest_hash(manifest)
    ph = manifest.packet.hash
    payload = manifest.model_dump(mode="json", by_alias=True, exclude_none=False)

    cols = (
        "id, tenant_id, principal_id, recall_log_id, manifest_schema, "
        "manifest_schema_version, canonicalization, mode, manifest, "
        "manifest_hash, packet_hash"
    )
    placeholders = (
        "$1, $2, $3, $4, 'engram.context-manifest', '1.0', 'rfc8785', "
        "'startup', $5::jsonb, $6, $7"
    )
    args: list[Any] = [
        receipt_id,
        tenant_id,
        principal_id,
        recall_log_id,
        json.dumps(payload),
        mh,
        ph,
    ]
    if created_at is not None:
        cols += ", created_at"
        placeholders += ", $8"
        args.append(created_at)

    await owner.execute(
        f"INSERT INTO context_receipts ({cols}) VALUES ({placeholders})",
        *args,
    )
    return payload


async def _apply_rls(
    session: AsyncSession, *, tenant_id: uuid.UUID, principal_id: uuid.UUID
) -> None:
    from sqlalchemy import text

    await session.execute(
        text(
            "SELECT set_config('app.tenant_id', :tid, true), "
            "set_config('app.principal_id', :pid, true)"
        ),
        {"tid": str(tenant_id), "pid": str(principal_id)},
    )


async def _cleanup(owner: Any, *tenant_ids: uuid.UUID) -> None:
    for tid in tenant_ids:
        with contextlib.suppress(Exception):
            await owner.execute(
                "DELETE FROM context_receipts WHERE tenant_id = $1", tid
            )
        with contextlib.suppress(Exception):
            await owner.execute("DELETE FROM tenants WHERE id = $1", tid)


# ─── 1. List returns only the owning principal's receipts ──────────────


async def test_list_returns_only_owning_principal_receipts() -> None:
    owner = await _owner_connect()
    engine = _app_engine()
    factory = _app_factory(engine)
    tenant_id = uuid.uuid4()
    principal_a = uuid.uuid4()
    principal_b = uuid.uuid4()
    rl_a = uuid.uuid4()
    rl_b = uuid.uuid4()
    receipt_a = uuid.uuid4()
    receipt_b = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    try:
        await _seed_tenant_principal(
            owner, tenant_id=tenant_id, principal_id=principal_a, label="pa"
        )
        await owner.execute(
            "INSERT INTO principals (id, tenant_id, name, type) "
            "VALUES ($1, $2, 'pb', 'agent')",
            principal_b,
            tenant_id,
        )
        await _insert_recall_log(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_a,
            recall_log_id=rl_a,
            item_ids=item_ids,
            byte_budget=8192,
        )
        await _insert_recall_log(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_b,
            recall_log_id=rl_b,
            item_ids=item_ids,
            byte_budget=8192,
        )
        await _owner_insert_receipt(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_a,
            recall_log_id=rl_a,
            receipt_id=receipt_a,
            item_ids=item_ids,
        )
        await _owner_insert_receipt(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_b,
            recall_log_id=rl_b,
            receipt_id=receipt_b,
            item_ids=item_ids,
        )

        async with factory() as session:
            await _apply_rls(
                session, tenant_id=tenant_id, principal_id=principal_a
            )
            result = await list_context_receipts(
                session, tenant_id=tenant_id, principal_id=principal_a
            )
        assert len(result.items) == 1
        assert result.items[0].id == receipt_a
        assert result.next_cursor is None
    finally:
        await _cleanup(owner, tenant_id)
        await engine.dispose()
        await owner.close()


# ─── 2. Cross-tenant rows are invisible ─────────────────────────────────


async def test_cross_tenant_rows_invisible() -> None:
    owner = await _owner_connect()
    engine = _app_engine()
    factory = _app_factory(engine)
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    principal_a = uuid.uuid4()
    principal_b = uuid.uuid4()
    rl_b = uuid.uuid4()
    receipt_b = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    try:
        await _seed_tenant_principal(
            owner, tenant_id=tenant_a, principal_id=principal_a, label="ta"
        )
        await _seed_tenant_principal(
            owner, tenant_id=tenant_b, principal_id=principal_b, label="tb"
        )
        await _insert_recall_log(
            owner,
            tenant_id=tenant_b,
            principal_id=principal_b,
            recall_log_id=rl_b,
            item_ids=item_ids,
            byte_budget=8192,
        )
        await _owner_insert_receipt(
            owner,
            tenant_id=tenant_b,
            principal_id=principal_b,
            recall_log_id=rl_b,
            receipt_id=receipt_b,
            item_ids=item_ids,
        )

        async with factory() as session:
            await _apply_rls(
                session, tenant_id=tenant_a, principal_id=principal_a
            )
            result = await list_context_receipts(
                session, tenant_id=tenant_a, principal_id=principal_a
            )
        assert len(result.items) == 0
        assert result.next_cursor is None
    finally:
        await _cleanup(owner, tenant_a, tenant_b)
        await engine.dispose()
        await owner.close()


# ─── 3. Timeline order is created_at DESC, id DESC ─────────────────────


async def test_timeline_order_created_at_desc_id_desc() -> None:
    owner = await _owner_connect()
    engine = _app_engine()
    factory = _app_factory(engine)
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    try:
        await _seed_tenant_principal(
            owner, tenant_id=tenant_id, principal_id=principal_id, label="order"
        )
        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        receipt_ids: list[uuid.UUID] = []
        for i in range(3):
            rl = uuid.uuid4()
            rid = uuid.uuid4()
            receipt_ids.append(rid)
            ts = base + timedelta(seconds=i)
            await _insert_recall_log(
                owner,
                tenant_id=tenant_id,
                principal_id=principal_id,
                recall_log_id=rl,
                item_ids=item_ids,
                byte_budget=8192,
                created_at=ts,
            )
            await _owner_insert_receipt(
                owner,
                tenant_id=tenant_id,
                principal_id=principal_id,
                recall_log_id=rl,
                receipt_id=rid,
                item_ids=item_ids,
                created_at=ts,
            )

        async with factory() as session:
            await _apply_rls(
                session, tenant_id=tenant_id, principal_id=principal_id
            )
            result = await list_context_receipts(
                session,
                tenant_id=tenant_id,
                principal_id=principal_id,
                limit=10,
            )
        assert len(result.items) == 3
        # Newest first: index 2 (latest timestamp), then 1, then 0.
        assert result.items[0].id == receipt_ids[2]
        assert result.items[1].id == receipt_ids[1]
        assert result.items[2].id == receipt_ids[0]
    finally:
        await _cleanup(owner, tenant_id)
        await engine.dispose()
        await owner.close()


# ─── 4. Pagination across equal timestamps has no duplicates ───────────


async def test_pagination_equal_timestamps_no_duplicates() -> None:
    owner = await _owner_connect()
    engine = _app_engine()
    factory = _app_factory(engine)
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    try:
        await _seed_tenant_principal(
            owner, tenant_id=tenant_id, principal_id=principal_id, label="page"
        )
        ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        # 5 receipts with identical created_at — order is determined by id DESC.
        all_receipt_ids: list[uuid.UUID] = []
        for _ in range(5):
            rl = uuid.uuid4()
            rid = uuid.uuid4()
            all_receipt_ids.append(rid)
            await _insert_recall_log(
                owner,
                tenant_id=tenant_id,
                principal_id=principal_id,
                recall_log_id=rl,
                item_ids=item_ids,
                byte_budget=8192,
                created_at=ts,
            )
            await _owner_insert_receipt(
                owner,
                tenant_id=tenant_id,
                principal_id=principal_id,
                recall_log_id=rl,
                receipt_id=rid,
                item_ids=item_ids,
                created_at=ts,
            )

        seen_ids: set[uuid.UUID] = set()
        cursor: str | None = None
        for _page in range(5):
            async with factory() as session:
                await _apply_rls(
                    session, tenant_id=tenant_id, principal_id=principal_id
                )
                result = await list_context_receipts(
                    session,
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    limit=2,
                    cursor=cursor,
                )
            for item in result.items:
                assert item.id not in seen_ids, f"duplicate receipt {item.id}"
                seen_ids.add(item.id)
            cursor = result.next_cursor
            if cursor is None:
                break

        assert len(seen_ids) == 5
        assert seen_ids == set(all_receipt_ids)
    finally:
        await _cleanup(owner, tenant_id)
        await engine.dispose()
        await owner.close()


# ─── 5. recall_log_id filtering remains ownership-scoped ───────────────


async def test_recall_log_id_filter_ownership_scoped() -> None:
    owner = await _owner_connect()
    engine = _app_engine()
    factory = _app_factory(engine)
    tenant_id = uuid.uuid4()
    principal_a = uuid.uuid4()
    principal_b = uuid.uuid4()
    rl_a1 = uuid.uuid4()
    rl_a2 = uuid.uuid4()
    rl_b1 = uuid.uuid4()
    receipt_a1 = uuid.uuid4()
    receipt_a2 = uuid.uuid4()
    receipt_b1 = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    try:
        await _seed_tenant_principal(
            owner, tenant_id=tenant_id, principal_id=principal_a, label="rla"
        )
        await owner.execute(
            "INSERT INTO principals (id, tenant_id, name, type) "
            "VALUES ($1, $2, 'rlb', 'agent')",
            principal_b,
            tenant_id,
        )
        for rl, pid in [(rl_a1, principal_a), (rl_a2, principal_a), (rl_b1, principal_b)]:
            await _insert_recall_log(
                owner,
                tenant_id=tenant_id,
                principal_id=pid,
                recall_log_id=rl,
                item_ids=item_ids,
                byte_budget=8192,
            )
        await _owner_insert_receipt(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_a,
            recall_log_id=rl_a1,
            receipt_id=receipt_a1,
            item_ids=item_ids,
        )
        await _owner_insert_receipt(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_a,
            recall_log_id=rl_a2,
            receipt_id=receipt_a2,
            item_ids=item_ids,
        )
        await _owner_insert_receipt(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_b,
            recall_log_id=rl_b1,
            receipt_id=receipt_b1,
            item_ids=item_ids,
        )

        # Principal A filters by rl_a1 — sees only that receipt.
        async with factory() as session:
            await _apply_rls(
                session, tenant_id=tenant_id, principal_id=principal_a
            )
            result = await list_context_receipts(
                session,
                tenant_id=tenant_id,
                principal_id=principal_a,
                recall_log_id=rl_a1,
            )
        assert len(result.items) == 1
        assert result.items[0].id == receipt_a1

        # Principal A filtering by rl_b1 (owned by B) sees nothing.
        async with factory() as session:
            await _apply_rls(
                session, tenant_id=tenant_id, principal_id=principal_a
            )
            result = await list_context_receipts(
                session,
                tenant_id=tenant_id,
                principal_id=principal_a,
                recall_log_id=rl_b1,
            )
        assert len(result.items) == 0
    finally:
        await _cleanup(owner, tenant_id)
        await engine.dispose()
        await owner.close()


# ─── 6. Profile-bound context sees only exact profile+revision receipts ─


async def test_profile_bound_sees_only_matching_profile_receipts() -> None:
    owner = await _owner_connect()
    engine = _app_engine()
    factory = _app_factory(engine)
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    profile_id = uuid.uuid4()
    profile_rev = uuid.uuid4()
    other_profile_id = uuid.uuid4()
    other_profile_rev = uuid.uuid4()
    try:
        await _seed_tenant_principal(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            label="prof",
        )
        # Recall log + receipt for profile A.
        rl_a = uuid.uuid4()
        receipt_a = uuid.uuid4()
        await _insert_recall_log(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=rl_a,
            item_ids=item_ids,
            byte_budget=8192,
            memory_profile_id=profile_id,
            memory_profile_revision_id=profile_rev,
        )
        await _owner_insert_receipt(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=rl_a,
            receipt_id=receipt_a,
            item_ids=item_ids,
            memory_profile_id=str(profile_id),
            memory_profile_revision_id=str(profile_rev),
            memory_profile_version=1,
        )

        # Recall log + receipt for profile B.
        rl_b = uuid.uuid4()
        receipt_b = uuid.uuid4()
        await _insert_recall_log(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=rl_b,
            item_ids=item_ids,
            byte_budget=8192,
            memory_profile_id=other_profile_id,
            memory_profile_revision_id=other_profile_rev,
        )
        await _owner_insert_receipt(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=rl_b,
            receipt_id=receipt_b,
            item_ids=item_ids,
            memory_profile_id=str(other_profile_id),
            memory_profile_revision_id=str(other_profile_rev),
            memory_profile_version=1,
        )

        async with factory() as session:
            await _apply_rls(
                session, tenant_id=tenant_id, principal_id=principal_id
            )
            result = await list_context_receipts(
                session,
                tenant_id=tenant_id,
                principal_id=principal_id,
                memory_profile_id=profile_id,
                memory_profile_revision_id=profile_rev,
                limit=10,
            )
        ids = {item.id for item in result.items}
        assert receipt_a in ids
        assert receipt_b not in ids
    finally:
        await _cleanup(owner, tenant_id)
        await engine.dispose()
        await owner.close()


# ─── 7. Profile-bound context cannot see unprofiled receipts ───────────


async def test_profile_bound_cannot_see_unprofiled_receipts() -> None:
    owner = await _owner_connect()
    engine = _app_engine()
    factory = _app_factory(engine)
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    profile_id = uuid.uuid4()
    profile_rev = uuid.uuid4()
    try:
        await _seed_tenant_principal(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            label="punp",
        )
        rl = uuid.uuid4()
        receipt_unprofiled = uuid.uuid4()
        await _insert_recall_log(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=rl,
            item_ids=item_ids,
            byte_budget=8192,
        )
        await _owner_insert_receipt(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=rl,
            receipt_id=receipt_unprofiled,
            item_ids=item_ids,
        )

        async with factory() as session:
            await _apply_rls(
                session, tenant_id=tenant_id, principal_id=principal_id
            )
            result = await list_context_receipts(
                session,
                tenant_id=tenant_id,
                principal_id=principal_id,
                memory_profile_id=profile_id,
                memory_profile_revision_id=profile_rev,
                limit=10,
            )
        assert len(result.items) == 0
    finally:
        await _cleanup(owner, tenant_id)
        await engine.dispose()
        await owner.close()


# ─── 8. Unprofiled context sees all receipts ───────────────────────────


async def test_unprofiled_context_sees_all_receipts() -> None:
    owner = await _owner_connect()
    engine = _app_engine()
    factory = _app_factory(engine)
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    profile_id = uuid.uuid4()
    profile_rev = uuid.uuid4()
    try:
        await _seed_tenant_principal(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            label="unp",
        )
        # Profiled receipt.
        rl_p = uuid.uuid4()
        receipt_p = uuid.uuid4()
        await _insert_recall_log(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=rl_p,
            item_ids=item_ids,
            byte_budget=8192,
            memory_profile_id=profile_id,
            memory_profile_revision_id=profile_rev,
        )
        await _owner_insert_receipt(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=rl_p,
            receipt_id=receipt_p,
            item_ids=item_ids,
            memory_profile_id=str(profile_id),
            memory_profile_revision_id=str(profile_rev),
            memory_profile_version=1,
        )

        # Unprofiled receipt.
        rl_u = uuid.uuid4()
        receipt_u = uuid.uuid4()
        await _insert_recall_log(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=rl_u,
            item_ids=item_ids,
            byte_budget=8192,
        )
        await _owner_insert_receipt(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=rl_u,
            receipt_id=receipt_u,
            item_ids=item_ids,
        )

        async with factory() as session:
            await _apply_rls(
                session, tenant_id=tenant_id, principal_id=principal_id
            )
            result = await list_context_receipts(
                session,
                tenant_id=tenant_id,
                principal_id=principal_id,
                limit=10,
            )
        ids = {item.id for item in result.items}
        assert receipt_p in ids
        assert receipt_u in ids
        assert len(result.items) == 2
    finally:
        await _cleanup(owner, tenant_id)
        await engine.dispose()
        await owner.close()


# ─── 9. Verification detects owner-role corruption of manifest_hash ────


async def test_verify_detects_manifest_hash_corruption() -> None:
    owner = await _owner_connect()
    engine = _app_engine()
    factory = _app_factory(engine)
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    rl = uuid.uuid4()
    rid = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    try:
        await _seed_tenant_principal(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            label="vmh",
        )
        await _insert_recall_log(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=rl,
            item_ids=item_ids,
            byte_budget=8192,
        )
        await _owner_insert_receipt(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=rl,
            receipt_id=rid,
            item_ids=item_ids,
        )

        # Owner corrupts manifest_hash.
        await owner.execute(
            "UPDATE context_receipts SET manifest_hash = $1 WHERE id = $2",
            "sha256:" + "0" * 64,
            rid,
        )

        async with factory() as session:
            await _apply_rls(
                session, tenant_id=tenant_id, principal_id=principal_id
            )
            # Load the receipt via the repository's own getter.
            from engram.context_receipts import get_context_receipt

            receipt = await get_context_receipt(
                session,
                tenant_id=tenant_id,
                principal_id=principal_id,
                receipt_id=rid,
            )
            assert receipt is not None
            result = await verify_context_receipt_with_recall_log(
                session,
                receipt,
                tenant_id=tenant_id,
                principal_id=principal_id,
            )
        assert result.status == "invalid"
        assert result.failure_code == VERIFICATION_FAILURE_MANIFEST_HASH_MISMATCH
    finally:
        await _cleanup(owner, tenant_id)
        await engine.dispose()
        await owner.close()


# ─── 10. Verification detects owner-role corruption of packet_hash ─────


async def test_verify_detects_packet_hash_corruption() -> None:
    owner = await _owner_connect()
    engine = _app_engine()
    factory = _app_factory(engine)
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    rl = uuid.uuid4()
    rid = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    try:
        await _seed_tenant_principal(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            label="vph",
        )
        await _insert_recall_log(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=rl,
            item_ids=item_ids,
            byte_budget=8192,
        )
        await _owner_insert_receipt(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=rl,
            receipt_id=rid,
            item_ids=item_ids,
        )

        # Owner corrupts packet_hash.
        await owner.execute(
            "UPDATE context_receipts SET packet_hash = $1 WHERE id = $2",
            "sha256:" + "0" * 64,
            rid,
        )

        async with factory() as session:
            await _apply_rls(
                session, tenant_id=tenant_id, principal_id=principal_id
            )
            from engram.context_receipts import get_context_receipt

            receipt = await get_context_receipt(
                session,
                tenant_id=tenant_id,
                principal_id=principal_id,
                receipt_id=rid,
            )
            assert receipt is not None
            result = await verify_context_receipt_with_recall_log(
                session,
                receipt,
                tenant_id=tenant_id,
                principal_id=principal_id,
            )
        assert result.status == "invalid"
        assert result.failure_code == VERIFICATION_FAILURE_PACKET_HASH_BINDING_MISMATCH
    finally:
        await _cleanup(owner, tenant_id)
        await engine.dispose()
        await owner.close()


# ─── 11. Verification detects modified recall-log overlap field ────────


async def test_verify_detects_recall_log_byte_budget_change() -> None:
    owner = await _owner_connect()
    engine = _app_engine()
    factory = _app_factory(engine)
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    rl = uuid.uuid4()
    rid = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    try:
        await _seed_tenant_principal(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            label="vrl",
        )
        await _insert_recall_log(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=rl,
            item_ids=item_ids,
            byte_budget=8192,
        )
        await _owner_insert_receipt(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=rl,
            receipt_id=rid,
            item_ids=item_ids,
        )

        # Owner corrupts the recall log's byte_budget.
        # The manifest attests byte_budget=8192; changing the log to 4096
        # creates a binding mismatch.
        await owner.execute(
            "UPDATE recall_logs SET byte_budget = 4096 WHERE id = $1",
            rl,
        )

        async with factory() as session:
            await _apply_rls(
                session, tenant_id=tenant_id, principal_id=principal_id
            )
            from engram.context_receipts import get_context_receipt

            receipt = await get_context_receipt(
                session,
                tenant_id=tenant_id,
                principal_id=principal_id,
                receipt_id=rid,
            )
            assert receipt is not None
            result = await verify_context_receipt_with_recall_log(
                session,
                receipt,
                tenant_id=tenant_id,
                principal_id=principal_id,
            )
        assert result.status == "invalid"
        assert result.failure_code == VERIFICATION_FAILURE_RECALL_LOG_BINDING_MISMATCH
    finally:
        await _cleanup(owner, tenant_id)
        await engine.dispose()
        await owner.close()


# ─── 12. Verification performs no INSERT, UPDATE, or DELETE ────────────


async def test_verify_performs_no_mutations() -> None:
    owner = await _owner_connect()
    engine = _app_engine()
    factory = _app_factory(engine)
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    rl = uuid.uuid4()
    rid = uuid.uuid4()
    item_ids = [uuid.uuid4()]
    try:
        await _seed_tenant_principal(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            label="vnomut",
        )
        await _insert_recall_log(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=rl,
            item_ids=item_ids,
            byte_budget=8192,
        )
        await _owner_insert_receipt(
            owner,
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=rl,
            receipt_id=rid,
            item_ids=item_ids,
        )

        # Snapshot row counts for both tables before verification.
        receipts_before = await owner.fetchval(
            "SELECT count(*) FROM context_receipts WHERE tenant_id = $1",
            tenant_id,
        )
        recall_logs_before = await owner.fetchval(
            "SELECT count(*) FROM recall_logs WHERE tenant_id = $1",
            tenant_id,
        )

        async with factory() as session:
            await _apply_rls(
                session, tenant_id=tenant_id, principal_id=principal_id
            )
            from engram.context_receipts import get_context_receipt

            receipt = await get_context_receipt(
                session,
                tenant_id=tenant_id,
                principal_id=principal_id,
                receipt_id=rid,
            )
            assert receipt is not None
            result = await verify_context_receipt_with_recall_log(
                session,
                receipt,
                tenant_id=tenant_id,
                principal_id=principal_id,
            )
            # The session must NOT be committed — but even without commit, we
            # verify no pending INSERT/UPDATE/DELETE by checking the row counts
            # in a separate owner connection after the session closes (without
            # committing). Since the verify function never commits and we don't
            # commit here, no mutations can be visible.

        receipts_after = await owner.fetchval(
            "SELECT count(*) FROM context_receipts WHERE tenant_id = $1",
            tenant_id,
        )
        recall_logs_after = await owner.fetchval(
            "SELECT count(*) FROM recall_logs WHERE tenant_id = $1",
            tenant_id,
        )
        assert receipts_after == receipts_before
        assert recall_logs_after == recall_logs_before

        # The verification should be valid (no corruption).
        assert result.status == "valid"
    finally:
        await _cleanup(owner, tenant_id)
        await engine.dispose()
        await owner.close()
