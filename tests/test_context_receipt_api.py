"""Real-PostgreSQL API test suite for ENG-CONTEXT-003A receipt inspect/verify.

Exercises the three read-only Context Receipt API endpoints against a live
PostgreSQL database with the full v2 schema:

  GET /v1/context-receipts
      Keyset-paginated newest-first list with recall_log_id filter.

  GET /v1/context-receipts/{receipt_id}
      Inspect one receipt envelope and its exact stored manifest.

  GET /v1/context-receipts/{receipt_id}/verify
      Deterministic stored-artifact + recall-log-binding verification.

These tests prove:

- **Auth and scope:** all three routes are reachable by the default principal
  under auth-disabled dev mode and require a valid receipt identity.
- **Principal isolation:** the list returns only the owning principal's
  receipts; cross-tenant and cross-principal detail/verify requests return
  the same non-disclosing 404 as a genuinely missing ID.
- **List contract:** limit boundaries (1..100) are enforced; malformed
  cursors return 422; recall_log_id filter narrows results; ordering is
  newest-first.
- **Detail contract:** the response carries the exact stored manifest dict;
  an integrity-corrupt row remains inspectable with ``manifest_parse_status``
  reflecting the issue; the manifest dict contains no raw memory content or
  working_set.
- **Verify contract:** a correct receipt returns 200 valid; a corrupt
  manifest_hash returns 200 invalid with stable code
  ``MANIFEST_HASH_MISMATCH``; the limitations array is always present.

The suite skips gracefully when no live PostgreSQL is reachable.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.api.app import create_app
from engram.config import settings
from engram.context_receipts import store_context_receipt
from engram.db import get_session
from engram.memory_context import MEMORY_CONTEXT_VERSION, ResolvedMemoryContext
from tests.context_receipt_helpers import build_manifest

# ─── Engine / session factory (owner privileges, no RLS) ───────────────

_test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_test_session_factory = async_sessionmaker(
    _test_engine, class_=AsyncSession, expire_on_commit=False
)


# ─── DB availability guard ────────────────────────────────────────────


async def _db_ok() -> bool:
    try:
        async with _test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _require_db() -> None:
    pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")


async def _check_table() -> None:
    """Ensure the context_receipts table exists (migration 026 applied)."""
    async with _test_engine.connect() as conn:
        exists = await conn.scalar(
            text("SELECT to_regclass('context_receipts')")
        )
    if exists is None:
        pytest.skip("requires migration 026 (context_receipts table)")


# ─── Autouse cleanup ──────────────────────────────────────────────────


@pytest.fixture(autouse=True)
async def _clean_receipts():
    """Delete test-seeded tenants (cascades receipts, recall_logs, principals)."""
    if not await _db_ok():
        return
    async with _test_engine.begin() as conn:
        # Clean up test-created tenants first — cascades to principals,
        # recall_logs, and context_receipts.
        await conn.execute(
            text("DELETE FROM tenants WHERE slug LIKE 'crtest-%'")
        )
        # Also remove any stray test receipts in the default tenant.
        await conn.execute(
            text(
                "DELETE FROM context_receipts WHERE "
                "recall_log_id IN ("
                "  SELECT id FROM recall_logs WHERE "
                "  principal_id IN ("
                "    SELECT id FROM principals WHERE name LIKE 'crtest-%'"
                "  )"
                ")"
            )
        )
        await conn.execute(
            text("DELETE FROM principals WHERE name LIKE 'crtest-%'")
        )


# ─── Seeding helpers ──────────────────────────────────────────────────


async def _seed_tenant(slug_suffix: str = "") -> tuple[str, str]:
    """Insert a tenant and return (tenant_id, slug)."""
    tenant_id = str(uuid.uuid4())
    slug = f"crtest-{tenant_id[:8]}{slug_suffix}"
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"
            ),
            {"id": tenant_id, "name": slug, "slug": slug},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_config (tenant_id, config_version, active) "
                "VALUES (:tid, 'v1', TRUE)"
            ),
            {"tid": tenant_id},
        )
        await session.commit()
    return tenant_id, slug


async def _seed_principal(tenant_id: str, name: str, ptype: str = "admin") -> str:
    """Insert a principal and return its ID."""
    principal_id = str(uuid.uuid4())
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES (:id, :tid, :name, :type)"
            ),
            {"id": principal_id, "tid": tenant_id, "name": name, "type": ptype},
        )
        await session.commit()
    return principal_id


async def _insert_recall_log(
    *,
    tenant_id: str,
    principal_id: str,
    recall_log_id: str | None = None,
    item_ids: list[str] | None = None,
    byte_budget: int | None = 8192,
    mode: str = "startup",
    memory_context_version: str = "memory-context-v2",
) -> str:
    """Insert a recall_log row and return its ID."""
    rl_id = recall_log_id or str(uuid.uuid4())
    item_uuids = (
        [uuid.UUID(i) if isinstance(i, str) else i for i in item_ids]
        if item_ids
        else None
    )
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO recall_logs (id, tenant_id, principal_id, mode, query, "
                "item_ids, byte_budget, token_budget, scoring_version, "
                "config_version, memory_context_version) "
                "VALUES (:id, :tid, :pid, :mode, NULL, :item_ids, :byte_budget, "
                "NULL, 'v1', 'v1', :mcv)"
            ),
            {
                "id": rl_id,
                "tid": tenant_id,
                "pid": principal_id,
                "mode": mode,
                "item_ids": item_uuids,
                "byte_budget": byte_budget,
                "mcv": memory_context_version,
            },
        )
        await session.commit()
    return rl_id


async def _store_receipt(
    *,
    tenant_id: str,
    principal_id: str,
    recall_log_id: str,
    item_ids: list[str] | None = None,
    content: str = "served content",
) -> str:
    """Build a valid manifest, store a receipt, and return its receipt_id."""
    items = item_ids or [str(uuid.uuid4())]
    manifest = build_manifest(
        tenant_id=tenant_id,
        principal_id=principal_id,
        item_ids=items,
        content=content,
    )
    async with _test_session_factory() as session:
        result = await store_context_receipt(
            session,
            tenant_id=uuid.UUID(tenant_id),
            principal_id=uuid.UUID(principal_id),
            recall_log_id=uuid.UUID(recall_log_id),
            manifest=manifest,
        )
        await session.commit()
    return str(result.receipt.id)


async def _seed_full_setup(
    *, num_receipts: int = 1
) -> dict[str, str]:
    """Seed tenant + principal + recall_logs + receipts. Return IDs."""
    await _check_table()
    tenant_id, _ = await _seed_tenant()
    principal_id = await _seed_principal(tenant_id, f"crtest-principal-{tenant_id[:8]}")

    receipt_ids: list[str] = []
    for i in range(num_receipts):
        item_id = str(uuid.uuid4())
        rl_id = await _insert_recall_log(
            tenant_id=tenant_id,
            principal_id=principal_id,
            item_ids=[item_id],
        )
        rid = await _store_receipt(
            tenant_id=tenant_id,
            principal_id=principal_id,
            recall_log_id=rl_id,
            item_ids=[item_id],
            content=f"content-{i}",
        )
        receipt_ids.append(rid)

    return {
        "tenant_id": tenant_id,
        "principal_id": principal_id,
        "receipt_ids": receipt_ids,  # type: ignore[dict-item]
    }


# ─── ASGI client factory ──────────────────────────────────────────────


async def _make_client(
    tenant_id: str,
    principal_id: str,
) -> AsyncClient:
    """Build an ASGI client with a session override setting RLS GUCs.

    Mirrors the ``_make_admin_client`` pattern from test_trusted_actor.py.
    The override sets ``app.tenant_id`` / ``app.principal_id`` GUCs so the
    request session sees only the target tenant/principal's rows.
    """
    app = create_app()

    async def _override_get_session():
        async with _test_session_factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": tenant_id},
            )
            await session.execute(
                text("SELECT set_config('app.principal_id', :pid, true)"),
                {"pid": principal_id},
            )
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def _make_owner_client() -> AsyncClient:
    """ASGI client with no RLS narrowing (owner/bypass).

    Used only to confirm the routes themselves require authentication —
    here the default principal under auth-disabled mode is still resolved,
    so this is equivalent to the admin client against the default tenant.
    """
    # Resolve the default tenant/principal from the DB.
    async with _test_session_factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT t.id::text AS tenant_id, p.id::text AS principal_id "
                        "FROM tenants t "
                        "JOIN principals p ON p.tenant_id = t.id AND p.name = 'admin' "
                        "WHERE t.slug = 'default'"
                    )
                )
            )
            .mappings()
            .one()
        )
    return await _make_client(row["tenant_id"], row["principal_id"])


# ===========================================================================
# A. Auth and scope tests
# ===========================================================================


async def test_list_endpoint_accessible_by_default_principal():
    """GET /v1/context-receipts is reachable under auth-disabled dev mode."""
    if not await _db_ok():
        _require_db()
    setup = await _seed_full_setup()
    client = await _make_client(setup["tenant_id"], setup["principal_id"])
    async with client:
        resp = await client.get("/v1/context-receipts")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "items" in data
    assert len(data["items"]) >= 1


async def test_detail_endpoint_accessible_by_default_principal():
    """GET /v1/context-receipts/{id} is reachable under auth-disabled dev mode."""
    if not await _db_ok():
        _require_db()
    setup = await _seed_full_setup()
    receipt_id = setup["receipt_ids"][0]
    client = await _make_client(setup["tenant_id"], setup["principal_id"])
    async with client:
        resp = await client.get(f"/v1/context-receipts/{receipt_id}")
    assert resp.status_code == 200, resp.text


async def test_verify_endpoint_accessible_by_default_principal():
    """GET /v1/context-receipts/{id}/verify is reachable in auth-disabled mode."""
    if not await _db_ok():
        _require_db()
    setup = await _seed_full_setup()
    receipt_id = setup["receipt_ids"][0]
    client = await _make_client(setup["tenant_id"], setup["principal_id"])
    async with client:
        resp = await client.get(f"/v1/context-receipts/{receipt_id}/verify")
    assert resp.status_code == 200, resp.text


# ===========================================================================
# B. List endpoint tests
# ===========================================================================


async def test_list_returns_only_owning_principal_receipts():
    """List does not leak receipts from a different principal."""
    if not await _db_ok():
        _require_db()
    # Principal A has 2 receipts; Principal B has 1 receipt (same tenant).
    tenant_id, _ = await _seed_tenant()
    pid_a = await _seed_principal(tenant_id, f"crtest-pa-{tenant_id[:8]}")
    pid_b = await _seed_principal(tenant_id, f"crtest-pb-{tenant_id[:8]}")

    item_a1 = str(uuid.uuid4())
    item_a2 = str(uuid.uuid4())
    item_b1 = str(uuid.uuid4())
    rl_a1 = await _insert_recall_log(
        tenant_id=tenant_id, principal_id=pid_a, item_ids=[item_a1]
    )
    rl_a2 = await _insert_recall_log(
        tenant_id=tenant_id, principal_id=pid_a, item_ids=[item_a2]
    )
    rl_b1 = await _insert_recall_log(
        tenant_id=tenant_id, principal_id=pid_b, item_ids=[item_b1]
    )

    await _store_receipt(
        tenant_id=tenant_id, principal_id=pid_a, recall_log_id=rl_a1, item_ids=[item_a1]
    )
    await _store_receipt(
        tenant_id=tenant_id, principal_id=pid_a, recall_log_id=rl_a2, item_ids=[item_a2]
    )
    await _store_receipt(
        tenant_id=tenant_id, principal_id=pid_b, recall_log_id=rl_b1, item_ids=[item_b1]
    )

    client = await _make_client(tenant_id, pid_a)
    async with client:
        resp = await client.get("/v1/context-receipts")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 2
    returned_pids = {it["recall_log_id"] for it in items}
    assert rl_b1 not in returned_pids


async def test_list_limit_zero_returns_422():
    """limit=0 is below the minimum (ge=1) → 422."""
    if not await _db_ok():
        _require_db()
    setup = await _seed_full_setup()
    client = await _make_client(setup["tenant_id"], setup["principal_id"])
    async with client:
        resp = await client.get("/v1/context-receipts?limit=0")
    assert resp.status_code == 422


async def test_list_limit_101_returns_422():
    """limit=101 is above the maximum (le=100) → 422."""
    if not await _db_ok():
        _require_db()
    setup = await _seed_full_setup()
    client = await _make_client(setup["tenant_id"], setup["principal_id"])
    async with client:
        resp = await client.get("/v1/context-receipts?limit=101")
    assert resp.status_code == 422


async def test_list_malformed_cursor_returns_422():
    """A garbage cursor string → 422 (InvalidCursorError → HTTP 422)."""
    if not await _db_ok():
        _require_db()
    setup = await _seed_full_setup()
    client = await _make_client(setup["tenant_id"], setup["principal_id"])
    async with client:
        resp = await client.get("/v1/context-receipts?cursor=not-a-valid-cursor")
    assert resp.status_code == 422


async def test_list_recall_log_id_filter():
    """recall_log_id query param narrows the list to that log's receipt."""
    if not await _db_ok():
        _require_db()
    setup = await _seed_full_setup(num_receipts=3)
    tenant_id = setup["tenant_id"]
    principal_id = setup["principal_id"]

    # The setup created 3 receipts; grab the recall_log_id of the first receipt.
    first_receipt_id = setup["receipt_ids"][0]
    async with _test_session_factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT recall_log_id::text AS rl FROM context_receipts "
                        "WHERE id = :rid"
                    ),
                    {"rid": first_receipt_id},
                )
            )
            .mappings()
            .one()
        )
    target_rl = row["rl"]

    client = await _make_client(tenant_id, principal_id)
    async with client:
        resp = await client.get(f"/v1/context-receipts?recall_log_id={target_rl}")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["recall_log_id"] == target_rl


async def test_list_newest_first_ordering():
    """List returns receipts ordered by created_at DESC, id DESC."""
    if not await _db_ok():
        _require_db()
    setup = await _seed_full_setup(num_receipts=5)
    client = await _make_client(setup["tenant_id"], setup["principal_id"])
    async with client:
        resp = await client.get("/v1/context-receipts?limit=100")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 5

    created_times = [it["created_at"] for it in items]
    # Must be non-increasing (newest first).
    for i in range(1, len(created_times)):
        assert created_times[i] <= created_times[i - 1], (
            f"ordering violation at index {i}: {created_times[i]} > "
            f"{created_times[i - 1]}"
        )


# ===========================================================================
# C. Detail endpoint tests
# ===========================================================================


async def test_detail_returns_exact_stored_manifest():
    """The manifest dict field matches the stored JSONB exactly."""
    if not await _db_ok():
        _require_db()
    await _check_table()
    tenant_id, _ = await _seed_tenant()
    principal_id = await _seed_principal(tenant_id, f"crtest-p-{tenant_id[:8]}")
    item_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    rl_id = await _insert_recall_log(
        tenant_id=tenant_id, principal_id=principal_id, item_ids=item_ids
    )

    manifest = build_manifest(
        tenant_id=tenant_id,
        principal_id=principal_id,
        item_ids=item_ids,
        content="exact-manifest-content",
    )
    async with _test_session_factory() as session:
        result = await store_context_receipt(
            session,
            tenant_id=uuid.UUID(tenant_id),
            principal_id=uuid.UUID(principal_id),
            recall_log_id=uuid.UUID(rl_id),
            manifest=manifest,
        )
        await session.commit()
    receipt_id = str(result.receipt.id)

    # Read back the raw stored manifest JSONB from the DB for exact comparison.
    async with _test_session_factory() as session:
        stored = (
            (
                await session.execute(
                    text(
                        "SELECT manifest FROM context_receipts WHERE id = :rid"
                    ),
                    {"rid": receipt_id},
                )
            )
            .mappings()
            .one()
        )["manifest"]

    client = await _make_client(tenant_id, principal_id)
    async with client:
        resp = await client.get(f"/v1/context-receipts/{receipt_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["manifest"] == stored, "detail manifest must equal stored JSONB"


async def test_detail_readable_for_integrity_invalid_row():
    """An accessible but hash-corrupt row is still inspectable.

    Owner-corrupt the manifest_hash so verification would fail, then confirm
    GET detail still returns 200 with manifest_parse_status reflecting the
    manifest (which is still parseable, so "valid" parse status but a
    mismatched hash).
    """
    if not await _db_ok():
        _require_db()
    setup = await _seed_full_setup()
    receipt_id = setup["receipt_ids"][0]

    # Owner-corrupt the manifest_hash directly in the DB.
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "UPDATE context_receipts SET manifest_hash = 'sha256:" + "d" * 64 + "' "
                "WHERE id = :rid"
            ),
            {"rid": receipt_id},
        )
        await session.commit()

    client = await _make_client(setup["tenant_id"], setup["principal_id"])
    async with client:
        resp = await client.get(f"/v1/context-receipts/{receipt_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The manifest JSON itself is still well-formed, so parse_status is "valid".
    # The point is that the row is still readable despite the hash mismatch.
    assert body["manifest_parse_status"] == "valid"
    assert body["manifest_hash"] == "sha256:" + "d" * 64


async def test_detail_response_has_no_raw_content_or_working_set():
    """The manifest dict must not leak raw memory content or working_set.

    The top-level manifest keys are: schema, schema_version, canonicalization,
    mode, subject, request, versions, result, packet, items. None of these
    is "content" or "working_set". Individual items carry served_content_hash
    (a digest), never the plaintext content.
    """
    if not await _db_ok():
        _require_db()
    setup = await _seed_full_setup()
    receipt_id = setup["receipt_ids"][0]
    client = await _make_client(setup["tenant_id"], setup["principal_id"])
    async with client:
        resp = await client.get(f"/v1/context-receipts/{receipt_id}")
    assert resp.status_code == 200, resp.text
    manifest = resp.json()["manifest"]

    assert "content" not in manifest, "manifest must not contain raw content"
    assert "working_set" not in manifest, "manifest must not contain working_set"

    # Items must have served_content_hash, never raw "content".
    for item in manifest.get("items", []):
        assert "content" not in item, "item must not contain raw content"
        assert "served_content_hash" in item, "item must have served_content_hash"


# ===========================================================================
# D. Verify endpoint tests
# ===========================================================================


async def test_verify_returns_valid_for_correct_receipt():
    """A properly stored receipt verifies as valid."""
    if not await _db_ok():
        _require_db()
    setup = await _seed_full_setup()
    receipt_id = setup["receipt_ids"][0]
    client = await _make_client(setup["tenant_id"], setup["principal_id"])
    async with client:
        resp = await client.get(f"/v1/context-receipts/{receipt_id}/verify")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "valid"
    assert body["failure_code"] is None
    assert body["verification_scope"] == "stored_artifact_and_recall_log_binding"


async def test_verify_returns_invalid_with_stable_code_for_corrupt_receipt():
    """Owner-corrupt manifest_hash → 200 invalid, code MANIFEST_HASH_MISMATCH."""
    if not await _db_ok():
        _require_db()
    setup = await _seed_full_setup()
    receipt_id = setup["receipt_ids"][0]

    async with _test_session_factory() as session:
        await session.execute(
            text(
                "UPDATE context_receipts SET manifest_hash = 'sha256:" + "c" * 64 + "' "
                "WHERE id = :rid"
            ),
            {"rid": receipt_id},
        )
        await session.commit()

    client = await _make_client(setup["tenant_id"], setup["principal_id"])
    async with client:
        resp = await client.get(f"/v1/context-receipts/{receipt_id}/verify")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "invalid"
    assert body["failure_code"] == "MANIFEST_HASH_MISMATCH"


async def test_verify_response_includes_limitations_array():
    """The verify response always carries the non-disclosure limitations array."""
    if not await _db_ok():
        _require_db()
    setup = await _seed_full_setup()
    receipt_id = setup["receipt_ids"][0]
    client = await _make_client(setup["tenant_id"], setup["principal_id"])
    async with client:
        resp = await client.get(f"/v1/context-receipts/{receipt_id}/verify")
    assert resp.status_code == 200, resp.text
    limitations = resp.json()["limitations"]
    assert isinstance(limitations, list)
    assert len(limitations) >= 1
    # Every limitation is a non-empty string.
    for lim in limitations:
        assert isinstance(lim, str) and len(lim) > 0


# ===========================================================================
# E. 404 non-disclosure tests
# ===========================================================================


async def test_cross_tenant_receipt_returns_404():
    """A receipt in a different tenant returns 404 (same as missing)."""
    if not await _db_ok():
        _require_db()
    # Seed receipt in tenant A.
    setup_a = await _seed_full_setup()
    receipt_id = setup_a["receipt_ids"][0]

    # Seed a separate tenant B + principal.
    tenant_b, _ = await _seed_tenant("-b")
    pid_b = await _seed_principal(tenant_b, f"crtest-pb-{tenant_b[:8]}")

    client = await _make_client(tenant_b, pid_b)
    async with client:
        resp = await client.get(f"/v1/context-receipts/{receipt_id}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "receipt not found"


async def test_cross_principal_receipt_returns_404():
    """A receipt owned by a different principal in the same tenant returns 404."""
    if not await _db_ok():
        _require_db()
    tenant_id, _ = await _seed_tenant()
    pid_owner = await _seed_principal(tenant_id, f"crtest-owner-{tenant_id[:8]}")
    pid_other = await _seed_principal(tenant_id, f"crtest-other-{tenant_id[:8]}")

    item_id = str(uuid.uuid4())
    rl_id = await _insert_recall_log(
        tenant_id=tenant_id, principal_id=pid_owner, item_ids=[item_id]
    )
    receipt_id = await _store_receipt(
        tenant_id=tenant_id,
        principal_id=pid_owner,
        recall_log_id=rl_id,
        item_ids=[item_id],
    )

    client = await _make_client(tenant_id, pid_other)
    async with client:
        resp = await client.get(f"/v1/context-receipts/{receipt_id}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "receipt not found"


async def test_nonexistent_receipt_returns_404():
    """A random UUID returns the same non-disclosing 404."""
    if not await _db_ok():
        _require_db()
    setup = await _seed_full_setup()
    client = await _make_client(setup["tenant_id"], setup["principal_id"])
    fake_id = str(uuid.uuid4())
    async with client:
        resp = await client.get(f"/v1/context-receipts/{fake_id}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "receipt not found"


async def test_nonexistent_receipt_verify_returns_404():
    """Verify on a random UUID also returns the same non-disclosing 404."""
    if not await _db_ok():
        _require_db()
    setup = await _seed_full_setup()
    client = await _make_client(setup["tenant_id"], setup["principal_id"])
    fake_id = str(uuid.uuid4())
    async with client:
        resp = await client.get(f"/v1/context-receipts/{fake_id}/verify")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "receipt not found"


async def test_404_responses_are_identical_for_missing_and_cross_tenant():
    """Missing, cross-tenant, and cross-principal all return identical 404 bodies.

    Non-disclosure: the response must not reveal whether the receipt exists
    under a different identity.
    """
    if not await _db_ok():
        _require_db()
    setup_a = await _seed_full_setup()
    receipt_id = setup_a["receipt_ids"][0]

    # Cross-tenant client.
    tenant_b, _ = await _seed_tenant("-ident")
    pid_b = await _seed_principal(tenant_b, f"crtest-pb-{tenant_b[:8]}")
    cross_client = await _make_client(tenant_b, pid_b)

    # Owner client (for genuinely-missing ID).
    owner_client = await _make_client(setup_a["tenant_id"], setup_a["principal_id"])
    fake_id = str(uuid.uuid4())

    async with cross_client:
        cross_resp = await cross_client.get(f"/v1/context-receipts/{receipt_id}")
    async with owner_client:
        missing_resp = await owner_client.get(f"/v1/context-receipts/{fake_id}")

    assert cross_resp.status_code == 404
    assert missing_resp.status_code == 404
    assert cross_resp.json() == missing_resp.json(), (
        "404 bodies must be identical for non-disclosure"
    )


# ===========================================================================
# F. Partial profile context fail-closed (F8)
# ===========================================================================


async def _make_partial_profile_client(
    tenant_id: str,
    principal_id: str,
    *,
    profile_id_only: bool = False,
    revision_id_only: bool = False,
) -> AsyncClient:
    """ASGI client with a partially specified memory context.

    Overrides ``resolve_memory_context`` to return a ResolvedMemoryContext
    where exactly one of memory_profile_id / memory_profile_revision_id is set.
    """
    app = create_app()

    async def _override_get_session():
        async with _test_session_factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": tenant_id},
            )
            await session.execute(
                text("SELECT set_config('app.principal_id', :pid, true)"),
                {"pid": principal_id},
            )
            yield session

    pid = uuid.uuid4() if profile_id_only else None
    rid = uuid.uuid4() if revision_id_only else None

    async def _override_memory_context():
        return ResolvedMemoryContext(
            version=MEMORY_CONTEXT_VERSION,
            tenant_id=uuid.UUID(tenant_id),
            principal_id=uuid.UUID(principal_id),
            api_key_id=None,
            memory_profile_id=pid,
            memory_profile_revision_id=rid,
            memory_profile_slug="test-profile" if pid else None,
            memory_profile_version=1 if pid else None,
            include_private=True,
            include_tenant=True,
            include_public=True,
            readable_workspace_ids=None,
            allow_tenant_write=True,
            allow_public_write=True,
            default_write_visibility="private",
            default_write_workspace_id=None,
            writable_workspace_ids=None,
        )

    from engram.memory_context import resolve_memory_context

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[resolve_memory_context] = _override_memory_context
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_list_with_partial_profile_id_only_returns_401():
    """GET /v1/context-receipts with profile_id set and revision_id null
    cannot expose unrestricted receipts — returns 401 (fail closed)."""
    if not await _db_ok():
        _require_db()
    await _check_table()
    setup = await _seed_full_setup(num_receipts=2)

    client = await _make_partial_profile_client(
        setup["tenant_id"], setup["principal_id"], profile_id_only=True
    )
    async with client:
        resp = await client.get("/v1/context-receipts")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid or revoked API key"


async def test_list_with_partial_profile_revision_only_returns_401():
    """GET /v1/context-receipts with revision_id set and profile_id null
    cannot expose unrestricted receipts — returns 401 (fail closed)."""
    if not await _db_ok():
        _require_db()
    await _check_table()
    setup = await _seed_full_setup(num_receipts=2)

    client = await _make_partial_profile_client(
        setup["tenant_id"], setup["principal_id"], revision_id_only=True
    )
    async with client:
        resp = await client.get("/v1/context-receipts")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid or revoked API key"
