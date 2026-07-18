"""Route-parity tests for the Context Manifest (ENG-CONTEXT-001).

Exercises the *real* startup recall route (``POST /v1/recall``) against a live
PostgreSQL, captures the finalized ``RecallResponse``, builds a manifest from
it, and proves the manifest describes the response actually served:

- ordered IDs, scores, reasons, warnings, counts, and packet bytes match the
  HTTP response exactly;
- the manifest contains no raw memory content;
- the builder has no database dependency (pure function of the response).

Skips with the canonical DB-skip reason when no PostgreSQL is reachable, so
``make compose-ci`` / ``make compose-trust-proof`` with
``ENGRAM_FAIL_ON_DB_SKIP=1`` is the authoritative run.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.api.app import create_app
from engram.api.routes.memory import RecallResponse
from engram.config import settings
from engram.context_manifest import (
    MANIFEST_CONTRACT_VERSION,
    MEMORY_CONTEXT_VERSION,
    PACKET_RENDER_VERSION,
    ContextManifestEffectiveV1,
    ContextManifestRequestedV1,
    ContextManifestRequestInputV1,
    ContextManifestSubjectV1,
    ContextManifestVersionsV1,
    build_startup_context_manifest_v1,
    compute_manifest_hash,
)

_test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_test_session_factory = async_sessionmaker(
    _test_engine, class_=AsyncSession, expire_on_commit=False
)

_NAME_PREFIX = "engctx001-route-"


async def _db_ok() -> bool:
    try:
        async with _test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _require_db() -> None:
    pytest.skip("requires a live PostgreSQL with the v2 schema")


@pytest.fixture(autouse=True)
async def _clean_db() -> None:
    if not await _db_ok():
        return
    async with _test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM item_events"))
        await conn.execute(text("DELETE FROM recall_logs"))
        await conn.execute(
            text(
                "DELETE FROM memory_items WHERE principal_id IN ("
                "SELECT id FROM principals WHERE tenant_id = "
                "(SELECT id FROM tenants WHERE slug = 'default') "
                f"AND name LIKE '{_NAME_PREFIX}%')"
            )
        )
        await conn.execute(
            text(
                "DELETE FROM principals WHERE tenant_id = "
                "(SELECT id FROM tenants WHERE slug = 'default') "
                f"AND name LIKE '{_NAME_PREFIX}%'"
            )
        )


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
                "created_at, valid_from"
                ") VALUES ("
                ":id, :tenant_id, :principal_id, :content, :content_hash, 'fact', "
                "'tenant', :review_status, 0.9, 0.5, "
                ":importance, 10, 'manual', true, :pinned, "
                ":created_at, :created_at"
                ")"
            ),
            {
                "id": item_id,
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "content": content,
                "content_hash": f"sha256:{uuid.uuid4().hex}",
                "review_status": review_status,
                "importance": importance,
                "pinned": pinned,
                "created_at": created_at,
            },
        )
        await session.commit()
    return item_id


def _manifest_inputs(
    *, tenant_id: str, principal_id: str, workspace_supplied: bool = False
) -> tuple[
    ContextManifestSubjectV1,
    ContextManifestRequestInputV1,
    ContextManifestVersionsV1,
]:
    subject = ContextManifestSubjectV1(
        tenant_id=tenant_id,
        principal_id=principal_id,
        workspace_id=None,
        memory_context_version=MEMORY_CONTEXT_VERSION,
        memory_profile_id=None,
        memory_profile_revision_id=None,
        memory_profile_version=None,
    )
    request_ctx = ContextManifestRequestInputV1(
        requested=ContextManifestRequestedV1(
            workspace_supplied=workspace_supplied,
            byte_budget=None,
            token_budget=None,
            item_budget=None,
        ),
        effective=ContextManifestEffectiveV1(
            workspace_id=None,
            byte_budget=settings.recall_byte_budget,
            token_budget=None,
            item_budget=None,
        ),
        query_digest=None,
    )
    versions = ContextManifestVersionsV1(
        scoring_version="v1",
        config_version="v1",
        candidate_strategy_version="startup-candidates-v1",
        manifest_contract_version=MANIFEST_CONTRACT_VERSION,
        packet_render_version=PACKET_RENDER_VERSION,
    )
    return subject, request_ctx, versions


# ─── tests ─────────────────────────────────────────────────────────────


async def test_route_parity_manifest_matches_served_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content=f"{_NAME_PREFIX}route-alpha",
        importance=0.9,
    )
    await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content=f"{_NAME_PREFIX}route-beta",
        importance=0.5,
    )

    # auth is disabled in CI/dev: the default admin principal resolves without
    # a bearer token.
    monkeypatch.setattr(settings, "auth_enabled", False)
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        resp = await client.post("/v1/recall", json={"mode": "startup"})
    assert resp.status_code == 200, resp.text
    response = RecallResponse(**resp.json())

    subject, request_ctx, versions = _manifest_inputs(
        tenant_id=tenant_id, principal_id=principal_id
    )
    manifest = build_startup_context_manifest_v1(
        response=response,
        subject_context=subject,
        request_context=request_ctx,
        decision_versions=versions,
    )

    # Packet bytes / packet hash match the HTTP response exactly.
    assert manifest.packet.hash == "sha256:" + hashlib.sha256(
        response.working_set.encode("utf-8")
    ).hexdigest()
    assert manifest.packet.hash == "sha256:" + hashlib.sha256(
        resp.json()["working_set"].encode("utf-8")
    ).hexdigest()
    assert manifest.result.rendered_packet_byte_count == len(
        response.working_set.encode("utf-8")
    )

    # Ordered IDs / scores / reasons / warnings match the response exactly.
    assert [i.item_id for i in manifest.items] == [it["id"] for it in response.items]
    for m_item, r_item in zip(manifest.items, response.items, strict=True):
        assert m_item.kind == r_item["kind"]
        assert m_item.review_status == r_item["review_status"]
        assert m_item.reasons == list(r_item["reasons"])
        assert m_item.warnings == list(r_item["warnings"])
        assert m_item.score == r_item["score"]
        assert m_item.importance == r_item["importance"]
        assert m_item.source_trust == r_item["source_trust"]
        assert m_item.memory_confidence == r_item["memory_confidence"]
        assert m_item.human_verified == r_item["human_verified"]
        # Additive served-decision fields are present on the served response.
        assert m_item.authority == r_item["authority"]
        assert m_item.visibility == r_item["visibility"]
        assert m_item.workspace_id == r_item["workspace_id"]
        assert m_item.conflict_type == r_item["conflict_type"]
        assert m_item.conflict_resolution_status == r_item["conflict_resolution_status"]

    # Counts match.
    assert manifest.result.item_count == response.item_count
    assert manifest.result.pinned_omitted_count == response.pinned_omitted_count
    assert manifest.result.omitted_count == response.omitted_count

    # No raw content in the manifest.
    dumped = manifest.model_dump(mode="json", exclude_none=False, by_alias=True)

    def walk(o: object):
        if isinstance(o, dict):
            for v in o.values():
                yield from walk(v)
        elif isinstance(o, list):
            for v in o:
                yield from walk(v)
        else:
            yield o

    values = [str(v) for v in walk(dumped)]
    for frag in (
        f"{_NAME_PREFIX}route-alpha",
        f"{_NAME_PREFIX}route-beta",
        "route-alpha",
        "route-beta",
    ):
        assert not any(frag in v for v in values), f"raw content leaked: {frag!r}"


async def test_route_parity_manifest_is_deterministic_across_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content=f"{_NAME_PREFIX}det-item",
    )

    monkeypatch.setattr(settings, "auth_enabled", False)
    hashes: list[str] = []
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        for _ in range(2):
            resp = await client.post("/v1/recall", json={"mode": "startup"})
            assert resp.status_code == 200, resp.text
            response = RecallResponse(**resp.json())
            subject, request_ctx, versions = _manifest_inputs(
                tenant_id=tenant_id, principal_id=principal_id
            )
            manifest = build_startup_context_manifest_v1(
                response=response,
                subject_context=subject,
                request_context=request_ctx,
                decision_versions=versions,
            )
            hashes.append(compute_manifest_hash(manifest))
    assert len(set(hashes)) == 1, "manifest hash must be stable across identical recalls"


async def test_builder_has_no_database_dependency_from_route_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The manifest built from the route response survives disposing the DB
    engine — proving the builder holds no live database/session reference and
    the manifest is a pure snapshot of the finalized response."""
    if not await _db_ok():
        _require_db()
    tenant_id, principal_id = await _default_tenant_principal()
    await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content=f"{_NAME_PREFIX}nodb-item",
    )
    monkeypatch.setattr(settings, "auth_enabled", False)
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        resp = await client.post("/v1/recall", json={"mode": "startup"})
    assert resp.status_code == 200, resp.text
    response = RecallResponse(**resp.json())

    subject, request_ctx, versions = _manifest_inputs(
        tenant_id=tenant_id, principal_id=principal_id
    )
    manifest = build_startup_context_manifest_v1(
        response=response,
        subject_context=subject,
        request_context=request_ctx,
        decision_versions=versions,
    )
    before = compute_manifest_hash(manifest)

    # Dispose the engine / clear the response — the manifest is unaffected.
    await _test_engine.dispose()
    response.items.clear()
    response.working_set = ""

    assert compute_manifest_hash(manifest) == before
