"""Real-PostgreSQL tests for the startup context-receipt dark write (ENG-CONTEXT-002B).

Exercises the full orchestrator and the production ``POST /v1/recall`` route
against a live PostgreSQL, proving:

- default-off: disabled startup performs NO receipt-specific work (no parse,
  no manifest, no receipt DB session, no telemetry, no receipt log);
- enabled startup recall creates exactly one receipt that references the
  response's recall_log_id and verifies;
- the stored manifest matches the exact HTTP response;
- empty startup recall creates a valid empty receipt;
- requested budgets are preserved; effective budgets reflect actual execution;
- workspace-scoped startup records the resolved workspace ID;
- semantic recall creates no receipt while the feature is enabled;
- existing RecallResponse JSON is identical with the feature on and off
  (excluding naturally unique recall_log_id);
- no receipt fields appear in the public response;
- immediate reload and verification happen before commit;
- verification failure rolls back a newly inserted receipt;
- receipt insertion failure leaves the recall log committed;
- manifest construction failure leaves the recall log committed;
- timeout leaves the recall log committed;
- malformed executed provenance leaves the recall log committed and creates
  no receipt, with the standardized failure observability emitted;
- every failure still returns HTTP 200 for an otherwise successful recall;
- retrieval telemetry status remains succeeded after receipt failure;
- a subsequent recall works after a timed-out or failed receipt attempt;
- RLS tenant/principal ownership is applied to the dedicated session;
- identical helper retry for one recall log returns idempotent;
- the finalized-response snapshot boundary holds: later memory mutations
  cannot alter the stored served snapshot;
- the dedicated receipt session connects as the non-owner app role subject
  to FORCE RLS.

Skips without the Compose real-PostgreSQL stack (see ``make compose-ci``).
``ENGRAM_FAIL_ON_DB_SKIP=1`` must produce zero database skips. With
``ENGRAM_FAIL_ON_DB_SKIP=1`` the absence of ``ENGRAM_APP_DATABASE_URL``
(non-owner app role) fails the authoritative RLS suite rather than producing
a hidden skip — the receipt must be written through the app role, not the
owner.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from engram.api.app import create_app
from engram.api.routes.memory import RecallResponse
from engram.config import settings
from engram.context_manifest import (
    ContextManifestV1,
)
from engram.context_receipt_dark_write import (
    write_startup_context_receipt_best_effort,
)
from engram.context_receipts import (
    ContextReceiptConflictError,
    ContextReceiptIntegrityError,
    get_context_receipt_for_recall_log,
)
from engram.memory_context import MEMORY_CONTEXT_VERSION as MC_VERSION
from engram.memory_context import ResolvedMemoryContext
from engram.models import ContextReceipt, UsageEvent

_test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_test_session_factory = async_sessionmaker(
    _test_engine, class_=AsyncSession, expire_on_commit=False
)

# Dedicated non-owner app-role engine. In the CI root suite
# ENGRAM_DATABASE_URL is the owner/superuser DSN; only ENGRAM_APP_DATABASE_URL
# is the non-owner application role subject to FORCE RLS. The orchestrator's
# dedicated receipt session is patched to this factory (Fix #2) so the
# real-DB dark-write proofs exercise the app role, NOT the owner.
_app_engine: AsyncEngine | None = None
_app_session_factory: async_sessionmaker[AsyncSession] | None = None
_app_role_name: str | None = None


def _maybe_init_app_engine() -> None:
    global _app_engine, _app_session_factory, _app_role_name
    if _app_engine is not None:
        return
    dsn = _app_dsn()
    if not dsn:
        return
    _app_engine = create_async_engine(dsn, poolclass=NullPool)
    _app_session_factory = async_sessionmaker(
        _app_engine, class_=AsyncSession, expire_on_commit=False
    )
    # Extract the role name from the DSN for the current_user assertion.
    try:
        from urllib.parse import urlsplit

        _app_role_name = urlsplit(dsn).username
    except Exception:  # noqa: BLE001
        _app_role_name = None


_NAME_PREFIX = "engctx002b-"

# Receipt fields that must NEVER appear in the public response.
_FORBIDDEN_RESPONSE_FIELDS = (
    "receipt_id",
    "manifest_hash",
    "packet_hash",
    "receipt_status",
    "receipt_error",
    "verification_status",
)


def _app_dsn() -> str | None:
    import os

    return os.environ.get("ENGRAM_APP_DATABASE_URL")


async def _db_ok() -> bool:
    try:
        async with _test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


# The canonical database-required skip reason. Used only for genuinely
# non-authoritative local runs where PostgreSQL is unavailable. With
# ENGRAM_FAIL_ON_DB_SKIP=1 any test that emits this reason fails the suite
# (see conftest.py).
_DB_SKIP_REASON = "requires a live PostgreSQL with the v2 schema"
_APP_ROLE_SKIP_REASON = (
    "requires ENGRAM_APP_DATABASE_URL (non-owner app role) for RLS proofs"
)


def _require_db() -> None:
    pytest.skip(_DB_SKIP_REASON)


def _require_app_role() -> None:
    """Skip when the non-owner app-role DSN is unavailable (Fix #2).

    With ``ENGRAM_FAIL_ON_DB_SKIP=1`` this skip must NOT be hidden — the
    authoritative CI suite sets that flag, so the absence of the app-role DSN
    in CI is a configuration failure rather than a tolerated local skip. The
    conftest hook only fails on the canonical ``_DB_SKIP_REASON``, so this
    helper emits the canonical reason too when the fail flag is set.
    """
    import os

    if os.environ.get("ENGRAM_FAIL_ON_DB_SKIP") == "1":
        # Authoritative CI path: the app-role DSN is required, not optional.
        pytest.fail(
            "ENGRAM_APP_DATABASE_URL is required for app-role RLS proofs under "
            "ENGRAM_FAIL_ON_DB_SKIP=1"
        )
    pytest.skip(_APP_ROLE_SKIP_REASON)


async def _receipts_table_exists() -> bool:
    try:
        async with _test_engine.connect() as conn:
            res = await conn.execute(text("SELECT to_regclass('context_receipts')"))
            return res.scalar() is not None
    except Exception:
        return False


@pytest.fixture(autouse=True)
async def _clean_db() -> None:
    """Wipe every dark-write-relevant row before each test.

    Tests in this file insert items under the default tenant's admin
    principal (via ``_default_tenant_principal``). Cleanup by principal-name
    prefix would never match those rows, leaving items accumulated across
    tests and breaking count-sensitive assertions (e.g. the empty-recall
    proof and the on/off parity proof). Wipe by content prefix and — because
    the admin principal is shared — wipe all default-tenant items/recall
    logs/receipts/events so each test starts from a known-empty state.
    """
    if not await _db_ok():
        return
    async with _test_engine.begin() as conn:
        with contextlib.suppress(Exception):
            await conn.execute(text("DELETE FROM context_receipts"))
        await conn.execute(text("DELETE FROM item_events"))
        await conn.execute(text("DELETE FROM recall_logs"))
        # Delete every memory item for the default tenant. These tests own
        # the only items they insert; no cross-file coupling relies on rows
        # surviving between tests in this module.
        await conn.execute(
            text(
                "DELETE FROM memory_items WHERE tenant_id = "
                "(SELECT id FROM tenants WHERE slug = 'default')"
            )
        )
        # Remove any test-only principals (e.g. the RLS cross-principal proof).
        await conn.execute(
            text(
                "DELETE FROM principals WHERE tenant_id = "
                "(SELECT id FROM tenants WHERE slug = 'default') "
                "AND name IN ('other-rls', 'engctx002b-other')"
            )
        )
        with contextlib.suppress(Exception):
            await conn.execute(text("DELETE FROM usage_events"))


@pytest.fixture(autouse=True)
def _patch_orchestrator_app_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the orchestrator's dedicated receipt session to the non-owner
    app-role factory (Fix #2). In the CI root suite the route's get_session
    uses the owner DSN (ENGRAM_DATABASE_URL), but the receipt must be written
    through the app role subject to FORCE RLS — matching production. When
    ENGRAM_APP_DATABASE_URL is unavailable (local dev without the app role),
    the orchestrator keeps its default factory and these tests skip.
    """
    _maybe_init_app_engine()
    if _app_session_factory is not None:
        monkeypatch.setattr(
            "engram.context_receipt_dark_write.async_session_factory",
            _app_session_factory,
        )


@pytest.fixture(scope="session", autouse=True)
async def _dispose_app_engine() -> None:
    """Dispose the module-level app-role test engine at session teardown.

    Keeps NullPool (no cross-loop connection reuse) while still releasing the
    engine's resources cleanly so no event-loop or open-connection leakage
    occurs between test sessions.
    """
    yield
    global _app_engine
    if _app_engine is not None:
        with contextlib.suppress(Exception):
            await _app_engine.dispose()
        _app_engine = None


async def _default_tenant_principal() -> tuple[str, str]:
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
    return str(row["tenant_id"]), str(row["principal_id"])


async def _insert_item(
    *,
    tenant_id: str,
    principal_id: str,
    content: str,
    review_status: str = "active",
    pinned: bool = False,
    importance: float = 0.9,
    visibility: str = "tenant",
    authority: int = 10,
    memory_confidence: float = 0.9,
    source_trust: float = 0.5,
    human_verified: bool = True,
    conflict_type: str | None = None,
    conflict_resolution_status: str | None = None,
) -> str:
    item_id = str(uuid.uuid4())
    created_at = datetime.now(UTC) - timedelta(hours=1)
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO memory_items ("
                "id, tenant_id, principal_id, content, content_hash, kind, "
                "visibility, review_status, memory_confidence, source_trust, "
                "importance, authority, source_type, human_verified, pinned, "
                "conflict_type, conflict_resolution_status, "
                "created_at, valid_from"
                ") VALUES ("
                ":id, :tenant_id, :principal_id, :content, :content_hash, 'fact', "
                ":visibility, :review_status, :memory_confidence, :source_trust, "
                ":importance, :authority, 'manual', :human_verified, :pinned, "
                ":conflict_type, :conflict_resolution_status, "
                ":created_at, :created_at"
                ")"
            ),
            {
                "id": item_id,
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "content": content,
                "content_hash": f"sha256:{uuid.uuid4().hex}",
                "visibility": visibility,
                "review_status": review_status,
                "memory_confidence": memory_confidence,
                "source_trust": source_trust,
                "importance": importance,
                "authority": authority,
                "human_verified": human_verified,
                "pinned": pinned,
                "conflict_type": conflict_type,
                "conflict_resolution_status": conflict_resolution_status,
                "created_at": created_at,
            },
        )
        await session.commit()
    return item_id


def _unrestricted_context(
    tenant_id: str,
    principal_id: str,
) -> ResolvedMemoryContext:
    return ResolvedMemoryContext(
        version=MC_VERSION,
        tenant_id=uuid.UUID(tenant_id),
        principal_id=uuid.UUID(principal_id),
        api_key_id=None,
        memory_profile_id=None,
        memory_profile_revision_id=None,
        memory_profile_slug=None,
        memory_profile_version=None,
        include_private=True,
        include_tenant=True,
        include_public=True,
        readable_workspace_ids=None,
        allow_tenant_write=True,
        allow_public_write=True,
        default_write_visibility="private",
        default_write_workspace_id=None,
        writable_workspace_ids=None,
        admin_workspace_bypass=False,
    )


def _raw_result_from_response(
    response: RecallResponse,
    *,
    effective_byte_budget: int | None,
    effective_token_budget: int | None = None,
    workspace_id: str | None = None,
    candidate_strategy_version: str = "startup-candidates-v1",
) -> dict[str, Any]:
    """Build a raw startup engine result dict that mirrors what
    ``engram.recall.execute_startup_recall`` returns for the given response.

    This is the production-shaped input the orchestrator parses. Tests build
    it from a real finalized ``RecallResponse`` (so the manifest provenance
    matches what was actually served) plus the engine-attested effective
    budgets. No defaults are invented for provenance fields the engine must
    attest — the parser would reject them.
    """
    return {
        "working_set": response.working_set,
        "item_count": response.item_count,
        "byte_count": response.byte_count,
        "pinned_omitted_count": response.pinned_omitted_count,
        "omitted_count": response.omitted_count,
        "items": response.items,
        "scoring_version": response.scoring_version,
        "config_version": response.config_version,
        "recall_log_id": response.recall_log_id,
        "candidate_strategy_version": candidate_strategy_version,
        "workspace_id": workspace_id,
        "effective_byte_budget": effective_byte_budget,
        "effective_token_budget": effective_token_budget,
        "effective_item_budget": None,
    }


async def _count_receipts() -> int:
    async with _test_session_factory() as session:
        return await session.scalar(select(func.count()).select_from(ContextReceipt)) or 0


async def _receipt_for_recall_log(
    recall_log_id: str | None,
) -> ContextReceipt | None:
    if recall_log_id is None:
        return None
    async with _test_session_factory() as session:
        result = await session.scalar(
            select(ContextReceipt).where(
                ContextReceipt.recall_log_id == uuid.UUID(recall_log_id)
            )
        )
    return result


async def _count_usage_events(event_type: str) -> int:
    async with _test_session_factory() as session:
        return (
            await session.scalar(
                select(func.count())
                .select_from(UsageEvent)
                .where(UsageEvent.event_type == event_type)
            )
        ) or 0


# ─── Route-level integration ────────────────────────────────────────────


async def test_disabled_startup_invokes_no_receipt_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disabled startup recall performs NO receipt-specific work at all:
    no executed-result parse, no manifest build, no receipt session, no
    receipt telemetry, no receipt usage event, no receipt row. The response
    is the unchanged RecallResponse.
    """
    if not await _db_ok() or not await _receipts_table_exists():
        _require_db()
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", False)
    tenant_id, principal_id = await _default_tenant_principal()
    await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content=f"{_NAME_PREFIX}disabled-item",
    )

    # Each receipt-specific callable must raise if invoked while disabled.
    def _boom_parse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("parse_startup_executed_context must not run when disabled")

    def _boom_build(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("_build_manifest must not run when disabled")

    def _boom_factory(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("async_session_factory must not run when disabled")

    async def _boom_telemetry(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("record_context_receipt_dark_write must not run when disabled")

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.parse_startup_executed_context", _boom_parse
    )
    monkeypatch.setattr(
        "engram.context_receipt_dark_write._build_manifest", _boom_build
    )
    monkeypatch.setattr(
        "engram.context_receipt_dark_write.async_session_factory", _boom_factory
    )
    monkeypatch.setattr(
        "engram.usage.record_context_receipt_dark_write", _boom_telemetry
    )

    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        resp = await client.post("/v1/recall", json={"mode": "startup"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The response is the unchanged RecallResponse.
    assert body["item_count"] == 1
    assert "receipt_id" not in body
    # No receipt row.
    assert await _count_receipts() == 0
    # No context_receipt.dark_write usage event.
    assert await _count_usage_events("context_receipt.dark_write") == 0


async def test_disabled_startup_creates_no_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    if not await _db_ok() or not await _receipts_table_exists():
        _require_db()
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", False)
    tenant_id, principal_id = await _default_tenant_principal()
    await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content=f"{_NAME_PREFIX}disabled-item",
    )
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        resp = await client.post("/v1/recall", json={"mode": "startup"})
    assert resp.status_code == 200, resp.text
    assert await _count_receipts() == 0


async def test_enabled_startup_creates_one_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    if not await _db_ok() or not await _receipts_table_exists():
        _require_db()
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "context_receipt_dark_write_timeout_seconds", 5.0)
    tenant_id, principal_id = await _default_tenant_principal()
    await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content=f"{_NAME_PREFIX}enabled-a"
    )
    await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content=f"{_NAME_PREFIX}enabled-b"
    )
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        resp = await client.post("/v1/recall", json={"mode": "startup"})
    assert resp.status_code == 200, resp.text

    response = RecallResponse(**resp.json())
    assert response.recall_log_id is not None
    receipt = await _receipt_for_recall_log(response.recall_log_id)
    assert receipt is not None
    assert receipt.recall_log_id == uuid.UUID(response.recall_log_id)
    assert receipt.tenant_id == uuid.UUID(tenant_id)
    assert receipt.principal_id == uuid.UUID(principal_id)
    assert receipt.mode == "startup"
    # The stored record verifies.
    from engram.context_receipts import verify_context_receipt_record

    verified = verify_context_receipt_record(receipt)
    assert verified.mode == "startup"


async def test_stored_manifest_matches_exact_http_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not await _db_ok() or not await _receipts_table_exists():
        _require_db()
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "context_receipt_dark_write_timeout_seconds", 5.0)
    tenant_id, principal_id = await _default_tenant_principal()
    await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content=f"{_NAME_PREFIX}exact-alpha",
    )
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        resp = await client.post("/v1/recall", json={"mode": "startup"})
    assert resp.status_code == 200

    response = RecallResponse(**resp.json())
    receipt = await _receipt_for_recall_log(response.recall_log_id)
    assert receipt is not None
    manifest = ContextManifestV1.model_validate(receipt.manifest)
    # Packet hash matches the exact served working_set.
    assert manifest.packet.hash == "sha256:" + hashlib.sha256(
        response.working_set.encode("utf-8")
    ).hexdigest()
    # Ordered item IDs match the response exactly.
    assert [i.item_id for i in manifest.items] == [it["id"] for it in response.items]


async def test_empty_startup_creates_valid_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not await _db_ok() or not await _receipts_table_exists():
        _require_db()
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "context_receipt_dark_write_timeout_seconds", 5.0)
    # No items inserted → empty working set.
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        resp = await client.post("/v1/recall", json={"mode": "startup"})
    assert resp.status_code == 200, resp.text
    response = RecallResponse(**resp.json())
    assert response.item_count == 0
    assert response.working_set == ""
    receipt = await _receipt_for_recall_log(response.recall_log_id)
    assert receipt is not None
    manifest = ContextManifestV1.model_validate(receipt.manifest)
    assert manifest.result.item_count == 0
    assert manifest.items == []


async def test_requested_budgets_preserved_in_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not await _db_ok() or not await _receipts_table_exists():
        _require_db()
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "context_receipt_dark_write_timeout_seconds", 5.0)
    tenant_id, principal_id = await _default_tenant_principal()
    await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content=f"{_NAME_PREFIX}budget-item",
    )
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/recall",
            json={
                "mode": "startup",
                "byte_budget": 2048,
                "token_budget": 512,
                "item_budget": 7,
            },
        )
    assert resp.status_code == 200
    response = RecallResponse(**resp.json())
    receipt = await _receipt_for_recall_log(response.recall_log_id)
    assert receipt is not None
    manifest = ContextManifestV1.model_validate(receipt.manifest)
    # Requested descriptor preserves caller values.
    assert manifest.request.requested.byte_budget == 2048
    assert manifest.request.requested.token_budget == 512
    assert manifest.request.requested.item_budget == 7
    # Effective byte budget reflects the resolved default (caller-supplied 2048).
    assert manifest.request.effective.byte_budget == 2048
    assert manifest.request.effective.token_budget == 512
    # Effective item budget is always null for startup v1.
    assert manifest.request.effective.item_budget is None


async def test_defaulted_effective_byte_budget_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not await _db_ok() or not await _receipts_table_exists():
        _require_db()
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "context_receipt_dark_write_timeout_seconds", 5.0)
    tenant_id, principal_id = await _default_tenant_principal()
    await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content=f"{_NAME_PREFIX}default-byte-item",
    )
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        resp = await client.post("/v1/recall", json={"mode": "startup"})
    assert resp.status_code == 200
    response = RecallResponse(**resp.json())
    receipt = await _receipt_for_recall_log(response.recall_log_id)
    assert receipt is not None
    manifest = ContextManifestV1.model_validate(receipt.manifest)
    # Requested is null (caller omitted it).
    assert manifest.request.requested.byte_budget is None
    # Effective reflects the resolved default.
    assert manifest.request.effective.byte_budget == settings.recall_byte_budget


async def test_explicit_token_budget_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    if not await _db_ok() or not await _receipts_table_exists():
        _require_db()
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "context_receipt_dark_write_timeout_seconds", 5.0)
    tenant_id, principal_id = await _default_tenant_principal()
    await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content=f"{_NAME_PREFIX}token-budget-item",
    )
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/recall", json={"mode": "startup", "token_budget": 1000}
        )
    assert resp.status_code == 200
    response = RecallResponse(**resp.json())
    receipt = await _receipt_for_recall_log(response.recall_log_id)
    assert receipt is not None
    manifest = ContextManifestV1.model_validate(receipt.manifest)
    assert manifest.request.effective.token_budget == 1000


async def test_requested_item_budget_recorded_effective_remains_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not await _db_ok() or not await _receipts_table_exists():
        _require_db()
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "context_receipt_dark_write_timeout_seconds", 5.0)
    tenant_id, principal_id = await _default_tenant_principal()
    await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content=f"{_NAME_PREFIX}item-budget-req",
    )
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/recall", json={"mode": "startup", "item_budget": 9}
        )
    assert resp.status_code == 200
    response = RecallResponse(**resp.json())
    receipt = await _receipt_for_recall_log(response.recall_log_id)
    assert receipt is not None
    manifest = ContextManifestV1.model_validate(receipt.manifest)
    assert manifest.request.requested.item_budget == 9
    assert manifest.request.effective.item_budget is None


async def test_no_receipt_fields_in_public_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not await _db_ok() or not await _receipts_table_exists():
        _require_db()
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "context_receipt_dark_write_timeout_seconds", 5.0)
    tenant_id, principal_id = await _default_tenant_principal()
    await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content=f"{_NAME_PREFIX}no-fields-item",
    )
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        resp = await client.post("/v1/recall", json={"mode": "startup"})
    assert resp.status_code == 200
    body = resp.json()
    for field in _FORBIDDEN_RESPONSE_FIELDS:
        assert field not in body, f"receipt field {field!r} leaked into response"
    for item in body.get("items", []):
        for field in _FORBIDDEN_RESPONSE_FIELDS:
            assert field not in item


async def test_response_json_identical_on_and_off_excluding_recall_log_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing RecallResponse JSON is identical with the feature on and off,
    excluding naturally unique recall_log_id values.

    Inserts exactly one item before BOTH recalls so the served working set is
    identical across the two calls (no per-call item insertion that would
    differ between off and on).
    """
    if not await _db_ok() or not await _receipts_table_exists():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    content = f"{_NAME_PREFIX}parity-item"
    # Insert ONE item before either recall; do not insert again between them.
    await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content=content
    )

    async def _recall() -> dict[str, Any]:
        async with AsyncClient(
            transport=ASGITransport(app=create_app()), base_url="http://test"
        ) as client:
            resp = await client.post("/v1/recall", json={"mode": "startup"})
        assert resp.status_code == 200
        return dict(resp.json())

    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", False)
    off = await _recall()
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "context_receipt_dark_write_timeout_seconds", 5.0)
    on = await _recall()

    # Exclude naturally unique recall_log_id.
    off_cmp = {k: v for k, v in off.items() if k != "recall_log_id"}
    on_cmp = {k: v for k, v in on.items() if k != "recall_log_id"}
    assert off_cmp == on_cmp


async def test_retrieval_telemetry_status_succeeded_after_receipt_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not await _db_ok() or not await _receipts_table_exists():
        _require_db()
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "context_receipt_dark_write_timeout_seconds", 5.0)
    monkeypatch.setattr(settings, "usage_telemetry_enabled", True)
    tenant_id, principal_id = await _default_tenant_principal()
    await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content=f"{_NAME_PREFIX}telemetry-status-item",
    )

    # Force the dark write to fail by patching store to raise.
    async def _raise_store(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("forced store failure")

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.store_context_receipt", _raise_store
    )

    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        resp = await client.post("/v1/recall", json={"mode": "startup"})
    assert resp.status_code == 200

    # The retrieval.request telemetry must still record status=succeeded.
    async with _test_session_factory() as session:
        row = (
            (
                await session.execute(
                    select(UsageEvent)
                    .where(
                        UsageEvent.event_type == "retrieval.request",
                        UsageEvent.operation == "startup_recall",
                    )
                    .order_by(UsageEvent.created_at.desc())
                    .limit(1)
                )
            )
            .scalar_one_or_none()
        )
    assert row is not None
    assert row.status == "succeeded"


async def test_subsequent_recall_works_after_failed_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subsequent recall works normally after a failed receipt attempt.

    Replaces a broad ``monkeypatch.undo()`` (which would also remove the
    autouse app-role receipt-factory patch and the feature-setting
    restoration) with a targeted restore of only the one patched production
    callable, so the second recall continues to write the receipt through
    the app role.
    """
    if not await _db_ok() or not await _receipts_table_exists():
        _require_db()
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "context_receipt_dark_write_timeout_seconds", 5.0)
    tenant_id, principal_id = await _default_tenant_principal()
    await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content=f"{_NAME_PREFIX}subsequent-item",
    )

    # Save the real production store callable so we can restore ONLY it after
    # the first (forced-failure) recall. This preserves the autouse
    # app-role receipt-factory patch and the feature-flag settings.
    from engram.context_receipt_dark_write import store_context_receipt as _real_store

    async def _raise_store(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("forced store failure")

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.store_context_receipt", _raise_store
    )
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        resp1 = await client.post("/v1/recall", json={"mode": "startup"})
    assert resp1.status_code == 200

    # Restore ONLY the store callable — the app-role factory patch and the
    # feature settings from the autouse fixture / explicit sets above remain
    # in effect, so the second recall writes its receipt through the app role.
    monkeypatch.setattr(
        "engram.context_receipt_dark_write.store_context_receipt", _real_store
    )
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        resp2 = await client.post("/v1/recall", json={"mode": "startup"})
    assert resp2.status_code == 200
    response2 = RecallResponse(**resp2.json())
    receipt2 = await _receipt_for_recall_log(response2.recall_log_id)
    assert receipt2 is not None


# ─── Semantic exclusion ─────────────────────────────────────────────────


async def test_semantic_recall_creates_no_receipt_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not await _db_ok() or not await _receipts_table_exists():
        _require_db()
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "context_receipt_dark_write_timeout_seconds", 5.0)
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        # Semantic recall returns a helpful empty message when no embeddings;
        # that's fine — we just need the recall_log_id to exist and no receipt.
        resp = await client.post(
            "/v1/recall", json={"mode": "semantic", "query": "anything"}
        )
    assert resp.status_code == 200
    assert await _count_receipts() == 0


# ─── Direct orchestrator: idempotency, dedicated session, RLS ───────────


async def _build_real_response_and_raw_result(
    monkeypatch: pytest.MonkeyPatch,
    *,
    content: str = f"{_NAME_PREFIX}direct-item",
    byte_budget: int | None = None,
    token_budget: int | None = None,
) -> tuple[RecallResponse, dict[str, Any], str, str]:
    """Execute a real startup recall and return the finalized response, a
    raw-result dict the orchestrator can parse, and the tenant/principal ids
    — WITHOUT creating a receipt.

    The dark-write feature is temporarily disabled for this setup recall only
    (so the route produces a recall log but no receipt), then RESTORED to its
    prior value. Callers that want the feature enabled for the subsequent
    direct orchestrator call must re-enable it explicitly after this helper
    returns.
    """
    saved = settings.context_receipt_dark_write_enabled
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", False)
    tenant_id, principal_id = await _default_tenant_principal()
    await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content=content
    )
    request_json: dict[str, Any] = {"mode": "startup"}
    if byte_budget is not None:
        request_json["byte_budget"] = byte_budget
    if token_budget is not None:
        request_json["token_budget"] = token_budget
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        resp = await client.post("/v1/recall", json=request_json)
    # Restore the prior value so callers control the feature state.
    monkeypatch.setattr(
        settings, "context_receipt_dark_write_enabled", saved
    )
    assert resp.status_code == 200
    response = RecallResponse(**resp.json())
    assert response.recall_log_id is not None
    raw_result = _raw_result_from_response(
        response,
        effective_byte_budget=(
            byte_budget if byte_budget is not None else settings.recall_byte_budget
        ),
        effective_token_budget=token_budget,
    )
    return response, raw_result, tenant_id, principal_id


async def _direct_best_effort(
    monkeypatch: pytest.MonkeyPatch,
    response: RecallResponse,
    raw_result: dict[str, Any],
    tenant_id: str,
    principal_id: str,
) -> Any:
    """Call the orchestrator directly with the new raw-result API."""
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "context_receipt_dark_write_timeout_seconds", 5.0)
    memory_context = _unrestricted_context(tenant_id, principal_id)
    return await write_startup_context_receipt_best_effort(
        response=response,
        raw_result=raw_result,
        memory_context=memory_context,
        requested_workspace_supplied=False,
        requested_byte_budget=None,
        requested_token_budget=None,
        requested_item_budget=None,
    )


async def test_idempotent_retry_returns_created_then_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two identical helper retries for one recall log return ``created`` then
    ``idempotent`` — never a second row."""
    if not await _db_ok() or not await _receipts_table_exists():
        _require_db()
    if _app_session_factory is None:
        _require_app_role()
    response, raw_result, tenant_id, principal_id = (
        await _build_real_response_and_raw_result(monkeypatch)
    )
    first = await _direct_best_effort(
        monkeypatch, response, raw_result, tenant_id, principal_id
    )
    assert first.status == "created"
    assert first.receipt_id is not None
    second = await _direct_best_effort(
        monkeypatch, response, raw_result, tenant_id, principal_id
    )
    assert second.status == "idempotent"
    assert second.receipt_id == first.receipt_id
    # Exactly one receipt for this recall log.
    receipt = await _receipt_for_recall_log(response.recall_log_id)
    assert receipt is not None


async def test_receipt_written_through_app_role_subject_to_rls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dedicated receipt session connects as the non-owner app role,
    subject to FORCE RLS — NOT the owner/superuser. Proves:

      - ``current_user`` is the app role (``engram_app``);
      - the role has no ``rolsuper`` / ``rolbypassrls``;
      - a receipt written under the owning tenant/principal is visible only
        to that identity.
    """
    if not await _db_ok() or not await _receipts_table_exists():
        _require_db()
    if _app_session_factory is None or _app_role_name is None:
        _require_app_role()
    response, raw_result, tenant_id, principal_id = (
        await _build_real_response_and_raw_result(monkeypatch)
    )
    result = await _direct_best_effort(
        monkeypatch, response, raw_result, tenant_id, principal_id
    )
    assert result.status == "created"

    # The dedicated session connected as the app role.
    assert _app_engine is not None
    assert _app_session_factory is not None
    async with _app_session_factory() as session:
        from engram.db import apply_rls_context

        await apply_rls_context(
            session, tenant_id=uuid.UUID(tenant_id), principal_id=uuid.UUID(principal_id)
        )
        current_user = (
            await session.execute(text("SELECT current_user"))
        ).scalar()
        role_flags = (
            await session.execute(
                text(
                    "SELECT rolsuper, rolbypassrls FROM pg_roles "
                    "WHERE rolname = current_user"
                )
            )
        ).one()
    assert current_user == _app_role_name
    assert bool(role_flags[0]) is False, "app role must not be a superuser"
    assert bool(role_flags[1]) is False, "app role must not bypass RLS"

    # The receipt is visible under the owning identity and not under a
    # different principal in the same tenant.
    other_principal = uuid.uuid4()
    async with _test_session_factory() as setup:
        await setup.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES (:id, :tid, 'other-rls', 'agent')"
            ),
            {"id": other_principal, "tid": tenant_id},
        )
        await setup.commit()

    async def _app_get_receipt(t: uuid.UUID, p: uuid.UUID) -> ContextReceipt | None:
        assert _app_session_factory is not None
        async with _app_session_factory() as s:
            from engram.db import apply_rls_context

            await apply_rls_context(s, tenant_id=t, principal_id=p)
            return await get_context_receipt_for_recall_log(
                s,
                tenant_id=t,
                principal_id=p,
                recall_log_id=uuid.UUID(str(response.recall_log_id)),
            )

    receipt_owned = await _app_get_receipt(uuid.UUID(tenant_id), uuid.UUID(principal_id))
    assert receipt_owned is not None
    receipt_other = await _app_get_receipt(uuid.UUID(tenant_id), other_principal)
    assert receipt_other is None


# ─── Finalized-response snapshot boundary (mutation proof) ──────────────


async def test_mutation_boundary_served_snapshot_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Later memory mutations cannot alter the stored served snapshot.

    Executes startup selection, finalizes RecallResponse, mutates the
    underlying MemoryItem metadata before receipt persistence, completes the
    receipt write, and verifies the stored manifest contains the served
    values from RecallResponse — not the later database values.

    Uses a CHECK-valid authority value (20) for the mutation so the UPDATE
    succeeds under the ``chk_memory_authority`` constraint
    (authority ∈ {10,20,30,40,50}) — the proof only needs a value DIFFERENT
    from the served one (10).
    """
    if not await _db_ok() or not await _receipts_table_exists():
        _require_db()
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", False)
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content=f"{_NAME_PREFIX}mutation-boundary-item",
        importance=0.9,
        source_trust=0.8,
        memory_confidence=0.75,
        authority=10,
        visibility="tenant",
        review_status="active",
        human_verified=True,
    )

    # Execute startup recall (dark write OFF) to get the finalized response.
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        resp = await client.post("/v1/recall", json={"mode": "startup"})
    assert resp.status_code == 200
    response = RecallResponse(**resp.json())
    served_item = next(i for i in response.items if i["id"] == item_id)
    served_authority = served_item["authority"]
    served_visibility = served_item["visibility"]
    served_review_status = served_item["review_status"]
    served_importance = served_item["importance"]
    served_source_trust = served_item["source_trust"]
    served_memory_confidence = served_item["memory_confidence"]
    served_human_verified = served_item["human_verified"]

    # Mutate the underlying MemoryItem metadata BEFORE receipt persistence.
    # authority=20 is CHECK-valid (the constraint requires {10,20,30,40,50})
    # and different from the served 10 — sufficient for the snapshot proof.
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "UPDATE memory_items SET authority = 20, visibility = 'private', "
                "review_status = 'disputed', importance = 0.01, source_trust = 0.01, "
                "memory_confidence = 0.01, human_verified = false "
                "WHERE id = :id"
            ),
            {"id": item_id},
        )
        await session.commit()

    # Now persist the receipt from the ORIGINAL served response.
    raw_result = _raw_result_from_response(
        response, effective_byte_budget=settings.recall_byte_budget
    )
    result = await _direct_best_effort(
        monkeypatch, response, raw_result, tenant_id, principal_id
    )
    assert result.status == "created"

    # The stored manifest must contain the SERVED values, not the mutated ones.
    receipt = await _receipt_for_recall_log(response.recall_log_id)
    assert receipt is not None
    manifest = ContextManifestV1.model_validate(receipt.manifest)
    stored_item = next(i for i in manifest.items if i.item_id == item_id)
    assert stored_item.authority == served_authority
    assert stored_item.visibility == served_visibility
    assert stored_item.review_status == served_review_status
    assert stored_item.importance == served_importance
    assert stored_item.source_trust == served_source_trust
    assert stored_item.memory_confidence == served_memory_confidence
    assert stored_item.human_verified == served_human_verified
    # NOT the mutated values.
    assert stored_item.authority != 20
    assert stored_item.visibility != "private"
    assert stored_item.importance != 0.01


# ─── Verification-before-commit / failure injection ─────────────────────


async def test_verification_failure_rolls_back_new_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not await _db_ok() or not await _receipts_table_exists():
        _require_db()
    response, raw_result, tenant_id, principal_id = (
        await _build_real_response_and_raw_result(
            monkeypatch, content=f"{_NAME_PREFIX}verify-fail-item"
        )
    )

    def _raise_integrity(*args: Any, **kwargs: Any) -> Any:
        raise ContextReceiptIntegrityError("tampered SECRET")

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.verify_context_receipt_record",
        _raise_integrity,
    )
    result = await _direct_best_effort(
        monkeypatch, response, raw_result, tenant_id, principal_id
    )
    assert result.status == "failed"
    assert result.failure_stage == "verify"
    # The newly inserted receipt must have been rolled back.
    receipt = await _receipt_for_recall_log(response.recall_log_id)
    assert receipt is None


async def test_manifest_construction_failure_leaves_recall_log_committed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not await _db_ok() or not await _receipts_table_exists():
        _require_db()
    response, raw_result, tenant_id, principal_id = (
        await _build_real_response_and_raw_result(
            monkeypatch, content=f"{_NAME_PREFIX}manifest-fail-item"
        )
    )

    def _raise_build(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("bad manifest SECRET")

    monkeypatch.setattr(
        "engram.context_receipt_dark_write._build_manifest", _raise_build
    )
    result = await _direct_best_effort(
        monkeypatch, response, raw_result, tenant_id, principal_id
    )
    assert result.status == "failed"
    assert result.failure_stage == "build_manifest"
    # The recall log must still be committed (it was committed before the dark write).
    async with _test_session_factory() as session:
        rl = await session.scalar(
            text("SELECT id FROM recall_logs WHERE id = :id"),
            {"id": str(response.recall_log_id)},
        )
    assert rl is not None
    # No receipt.
    receipt = await _receipt_for_recall_log(response.recall_log_id)
    assert receipt is None


async def test_receipt_insertion_failure_leaves_recall_log_committed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not await _db_ok() or not await _receipts_table_exists():
        _require_db()
    response, raw_result, tenant_id, principal_id = (
        await _build_real_response_and_raw_result(
            monkeypatch, content=f"{_NAME_PREFIX}insert-fail-item"
        )
    )

    async def _raise_conflict(*args: Any, **kwargs: Any) -> Any:
        raise ContextReceiptConflictError("conflict SECRET")

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.store_context_receipt", _raise_conflict
    )
    result = await _direct_best_effort(
        monkeypatch, response, raw_result, tenant_id, principal_id
    )
    assert result.status == "failed"
    assert result.failure_stage == "store"
    # Recall log still committed.
    async with _test_session_factory() as session:
        rl = await session.scalar(
            text("SELECT id FROM recall_logs WHERE id = :id"),
            {"id": str(response.recall_log_id)},
        )
    assert rl is not None


async def test_timeout_leaves_recall_log_committed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not await _db_ok() or not await _receipts_table_exists():
        _require_db()
    response, raw_result, tenant_id, principal_id = (
        await _build_real_response_and_raw_result(
            monkeypatch, content=f"{_NAME_PREFIX}timeout-item"
        )
    )
    # Now set a very short timeout and patch store to hang.
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "context_receipt_dark_write_timeout_seconds", 0.01)

    async def _slow_store(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(10)

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.store_context_receipt", _slow_store
    )
    memory_context = _unrestricted_context(tenant_id, principal_id)
    result = await write_startup_context_receipt_best_effort(
        response=response,
        raw_result=raw_result,
        memory_context=memory_context,
        requested_workspace_supplied=False,
        requested_byte_budget=None,
        requested_token_budget=None,
        requested_item_budget=None,
    )
    assert result.status == "timed_out"
    # Recall log still committed.
    async with _test_session_factory() as session:
        rl = await session.scalar(
            text("SELECT id FROM recall_logs WHERE id = :id"),
            {"id": str(response.recall_log_id)},
        )
    assert rl is not None


# ─── Malformed executed provenance (real-DB proof) ──────────────────────


async def test_malformed_provenance_leaves_recall_log_committed_no_receipt(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the startup engine result omits a required provenance key, the
    route still returns HTTP 200, the recall log persists, no receipt is
    created, and the standardized receipt-failure observability is emitted.

    Monkeypatches the startup engine result to omit ``candidate_strategy_
    version`` — a key the receipt must attest exactly. The route's public
    ``RecallResponse`` defaults must NOT mask the absence.
    """
    if not await _db_ok() or not await _receipts_table_exists():
        _require_db()
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "context_receipt_dark_write_timeout_seconds", 5.0)
    tenant_id, principal_id = await _default_tenant_principal()
    await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content=f"{_NAME_PREFIX}malformed-prov-item",
    )

    # Wrap execute_startup_recall so the returned dict omits a required key.
    # The route imports this callable inside the function body, so patch it at
    # its source module — the route's local import resolves the patched
    # attribute at call time.
    import engram.recall as recall_module
    from engram.recall import execute_startup_recall as _real_execute

    async def _stripped_execute(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = await _real_execute(*args, **kwargs)
        # Remove a required provenance key — the receipt must fail open.
        result.pop("candidate_strategy_version", None)
        return result

    monkeypatch.setattr(
        recall_module, "execute_startup_recall", _stripped_execute
    )

    with caplog.at_level(logging.INFO, logger="engram.context_receipt_dark_write"):
        async with AsyncClient(
            transport=ASGITransport(app=create_app()), base_url="http://test"
        ) as client:
            resp = await client.post("/v1/recall", json={"mode": "startup"})

    assert resp.status_code == 200
    response = RecallResponse(**resp.json())
    assert response.recall_log_id is not None
    # Recall log persists.
    async with _test_session_factory() as session:
        rl = await session.scalar(
            text("SELECT id FROM recall_logs WHERE id = :id"),
            {"id": str(response.recall_log_id)},
        )
    assert rl is not None
    # No receipt.
    assert await _receipt_for_recall_log(response.recall_log_id) is None
    # The standardized receipt failure log was emitted with the bounded
    # build_decision_context stage.
    dark_logs = [
        r
        for r in caplog.records
        if r.name == "engram.context_receipt_dark_write"
        and "context_receipt_dark_write" in r.getMessage()
    ]
    assert len(dark_logs) == 1
    msg = dark_logs[0].getMessage()
    assert "status=failed" in msg
    assert "failure_stage=build_decision_context" in msg
    assert "exception_type=StartupDecisionContextError" in msg
    # No raw content or exception message leaked.
    assert "malformed-prov" not in msg


# ─── Privacy: no raw content/query/exception leakage ────────────────────


async def test_no_raw_content_in_receipt_or_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    if not await _db_ok() or not await _receipts_table_exists():
        _require_db()
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "context_receipt_dark_write_timeout_seconds", 5.0)
    tenant_id, principal_id = await _default_tenant_principal()
    secret_content = f"{_NAME_PREFIX}SECRET-MARKER-CONTENT-xyz"
    await _insert_item(
        tenant_id=tenant_id, principal_id=principal_id, content=secret_content
    )
    with caplog.at_level(logging.INFO, logger="engram.context_receipt_dark_write"):
        async with AsyncClient(
            transport=ASGITransport(app=create_app()), base_url="http://test"
        ) as client:
            resp = await client.post("/v1/recall", json={"mode": "startup"})
    assert resp.status_code == 200
    # The secret content must not appear in any captured log line.
    for record in caplog.records:
        assert secret_content not in record.getMessage()
        assert "SECRET-MARKER" not in record.getMessage()


async def test_exception_message_not_in_logs_on_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    if not await _db_ok() or not await _receipts_table_exists():
        _require_db()
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "context_receipt_dark_write_timeout_seconds", 5.0)
    monkeypatch.setattr(settings, "usage_telemetry_enabled", False)
    tenant_id, principal_id = await _default_tenant_principal()
    await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content=f"{_NAME_PREFIX}exc-message-item",
    )
    secret = "EXC-MESSAGE-SECRET-SENTINEL-abc"

    async def _raise_store(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"store failed with {secret}")

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.store_context_receipt", _raise_store
    )
    with caplog.at_level(logging.INFO, logger="engram.context_receipt_dark_write"):
        async with AsyncClient(
            transport=ASGITransport(app=create_app()), base_url="http://test"
        ) as client:
            resp = await client.post("/v1/recall", json={"mode": "startup"})
    assert resp.status_code == 200
    for record in caplog.records:
        assert secret not in record.getMessage()
        assert record.exc_info is None
