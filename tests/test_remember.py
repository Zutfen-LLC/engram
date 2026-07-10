"""Tests for POST /v1/remember — the canonical write path.

These tests require a live PostgreSQL with the v2 schema (migrations/001_init.sql).
They skip automatically when no DB is reachable, matching the pattern in
test_health.py. Run locally with ``docker compose up``.

We use a NullPool engine to avoid asyncpg connection pool issues across
pytest-asyncio's per-function event loops, and override the get_session
dependency so the app uses our test engine.
"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.api.app import create_app
from engram.api.routes import memory as memory_routes
from engram.classification import ClassificationResult
from engram.config import settings
from engram.db import get_session

# Engine with NullPool — no cross-loop connection pooling issues.
_test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_test_session_factory = async_sessionmaker(
    _test_engine, class_=AsyncSession, expire_on_commit=False
)


async def _db_ok() -> bool:
    try:
        async with _test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _get_test_session() -> AsyncSession:
    """Replacement for get_session that uses the NullPool test engine.

    Applies RLS context via the SAME helper get_session uses (apply_rls_context)
    so this override tracks production behavior — including the commit that lets
    context survive a mid-request rollback (the dedup path exercised below).
    """
    from sqlalchemy import text as sa_text

    from engram.db import _DEFAULT_PRINCIPAL_NAME, _DEFAULT_TENANT_SLUG, apply_rls_context

    async with _test_session_factory() as session:
        row = (
            await session.execute(
                sa_text(
                    "SELECT t.id::text AS tenant_id, p.id::text AS principal_id "
                    "FROM tenants t "
                    "JOIN principals p ON p.tenant_id = t.id AND p.name = :principal "
                    "WHERE t.slug = :slug"
                ),
                {"slug": _DEFAULT_TENANT_SLUG, "principal": _DEFAULT_PRINCIPAL_NAME},
            )
        ).mappings().one()
        await apply_rls_context(
            session, tenant_id=row["tenant_id"], principal_id=row["principal_id"]
        )
        yield session


@pytest.fixture
def app():
    app = create_app()
    app.dependency_overrides[get_session] = _get_test_session
    return app


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
async def _clean_db():
    """Delete all memory_items and embeddings before each test for isolation."""
    if not await _db_ok():
        return
    async with _test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM memory_embeddings"))
        await conn.execute(text("DELETE FROM memory_items"))


# ---- Trust model: source_type → source_trust, review_status ----


async def test_manual_user_source_active_high_trust(client):
    """source_type='manual' from user/admin → review_status='active', source_trust=0.9."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    # kind is pinned so this isolates _resolve_trust_defaults; without it the
    # classifier would run and blend confidence away from the pure default.
    response = await client.post(
        "/v1/remember",
        json={"content": "The sky is blue today", "source_type": "manual", "kind": "fact"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "created"
    assert body["review_status"] == "active"
    # The seed admin principal has type='admin' which maps to manual_user trust.
    assert body["memory_confidence"] == pytest.approx(0.9)


async def test_extraction_source_proposed_low_trust(client):
    """source_type='extraction' → review_status='proposed', source_trust=0.5."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    response = await client.post(
        "/v1/remember",
        json={
            "content": "Extracted fact from conversation log",
            "source_type": "extraction",
            "kind": "fact",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "created"
    assert body["review_status"] == "proposed"
    assert body["memory_confidence"] == pytest.approx(0.5)


async def test_sync_turn_source_proposed_low_trust(client):
    """source_type='sync_turn' → review_status='proposed', source_trust=0.4."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    response = await client.post(
        "/v1/remember",
        json={
            "content": "Turn summary from sync",
            "source_type": "sync_turn",
            "kind": "fact",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "created"
    assert body["review_status"] == "proposed"
    assert body["memory_confidence"] == pytest.approx(0.4)


async def test_import_source_active_medium_trust(client):
    """source_type='import' → review_status='active', source_trust=0.8."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    response = await client.post(
        "/v1/remember",
        json={
            "content": "Imported memory from external store",
            "source_type": "import",
            "kind": "fact",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "created"
    assert body["review_status"] == "active"
    assert body["memory_confidence"] == pytest.approx(0.8)


async def test_migration_source_active_medium_trust(client):
    """source_type='migration' → review_status='active', source_trust=0.8."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    response = await client.post(
        "/v1/remember",
        json={
            "content": "Migrated memory from legacy system",
            "source_type": "migration",
            "kind": "fact",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "created"
    assert body["review_status"] == "active"
    assert body["memory_confidence"] == pytest.approx(0.8)


async def test_pre_compress_source_proposed_lowest_trust(client):
    """source_type='pre_compress' → review_status='proposed', source_trust=0.3."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    response = await client.post(
        "/v1/remember",
        json={
            "content": "Pre-compression extracted memory",
            "source_type": "pre_compress",
            "kind": "fact",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "created"
    assert body["review_status"] == "proposed"
    assert body["memory_confidence"] == pytest.approx(0.3)


# ---- Dedup ----


async def test_dedup_same_content_returns_deduped(client):
    """Same content written twice → second write returns status='deduped'."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    payload = {"content": "Unique dedup test content 12345", "source_type": "manual"}
    first = await client.post("/v1/remember", json=payload)
    assert first.status_code == 201
    assert first.json()["status"] == "created"

    second = await client.post("/v1/remember", json=payload)
    assert second.status_code == 201
    body = second.json()
    assert body["status"] == "deduped"
    assert body["deduped_existing_id"] is not None
    assert body["deduped_existing_id"] == first.json()["id"]


async def test_dedup_respects_canonicalization(client):
    """Whitespace differences canonicalize to the same hash → dedup."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    first = await client.post(
        "/v1/remember",
        json={"content": "Canonicalization   test   content", "source_type": "manual"},
    )
    assert first.status_code == 201
    assert first.json()["status"] == "created"

    second = await client.post(
        "/v1/remember",
        json={"content": "canonicalization test content", "source_type": "manual"},
    )
    assert second.status_code == 201
    assert second.json()["status"] == "deduped"


# ---- Supersession ----


async def test_supersession_preference_same_subject(client):
    """Writing a preference with same subject supersedes the old one."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    first = await client.post(
        "/v1/remember",
        json={
            "content": "User prefers dark mode",
            "kind": "preference",
            "subject_type": "user",
            "subject_id": "user-1",
            "source_type": "manual",
        },
    )
    assert first.status_code == 201
    assert first.json()["status"] == "created"
    first_id = first.json()["id"]

    second = await client.post(
        "/v1/remember",
        json={
            "content": "User now prefers light mode",
            "kind": "preference",
            "subject_type": "user",
            "subject_id": "user-1",
            "source_type": "manual",
        },
    )
    assert second.status_code == 201
    body = second.json()
    assert body["status"] == "superseded"
    assert body["superseded_id"] == first_id


async def test_supersession_invariant_same_subject(client):
    """Writing an invariant with same subject supersedes the old one."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    first = await client.post(
        "/v1/remember",
        json={
            "content": "System must use TLS 1.3",
            "kind": "invariant",
            "subject_type": "system",
            "subject_id": "tls-config",
            "source_type": "manual",
        },
    )
    assert first.status_code == 201
    assert first.json()["status"] == "created"
    first_id = first.json()["id"]

    second = await client.post(
        "/v1/remember",
        json={
            "content": "System must use TLS 1.3 minimum",
            "kind": "invariant",
            "subject_type": "system",
            "subject_id": "tls-config",
            "source_type": "manual",
        },
    )
    assert second.status_code == 201
    body = second.json()
    assert body["status"] == "superseded"
    assert body["superseded_id"] == first_id


async def test_no_supersession_for_fact_kind(client):
    """Fact kind does NOT trigger supersession — both items coexist."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    first = await client.post(
        "/v1/remember",
        json={
            "content": "Fact about subject A version 1",
            "kind": "fact",
            "subject_type": "domain_entity",
            "subject_id": "entity-1",
            "source_type": "manual",
        },
    )
    assert first.status_code == 201
    assert first.json()["status"] == "created"

    second = await client.post(
        "/v1/remember",
        json={
            "content": "Fact about subject A version 2",
            "kind": "fact",
            "subject_type": "domain_entity",
            "subject_id": "entity-1",
            "source_type": "manual",
        },
    )
    assert second.status_code == 201
    assert second.json()["status"] == "created"


# ---- Secret rejection ----


async def test_secret_aws_key_blocked(client):
    """AWS access key pattern triggers 422."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    response = await client.post(
        "/v1/remember",
        json={"content": "Use this key: AKIAIOSFODNN7EXAMPLE1234", "source_type": "manual"},
    )
    assert response.status_code == 422


async def test_secret_github_token_blocked(client):
    """GitHub token pattern triggers 422."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    response = await client.post(
        "/v1/remember",
        json={"content": "ghp_abcdefghijklmnopqrstuvwxyz0123456789AB", "source_type": "manual"},
    )
    assert response.status_code == 422


async def test_secret_private_key_blocked(client):
    """Private key pattern triggers 422."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    response = await client.post(
        "/v1/remember",
        json={
            "content": "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA...",
            "source_type": "manual",
        },
    )
    assert response.status_code == 422


async def test_normal_content_not_blocked(client):
    """Normal content without secrets is accepted."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    response = await client.post(
        "/v1/remember",
        json={"content": "The meeting is at 3pm in conference room B", "source_type": "manual"},
    )
    assert response.status_code == 201


# ---- Response shape and optional fields ----


async def test_response_has_all_fields(client):
    """Response contains id, status, review_status, memory_confidence."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    response = await client.post(
        "/v1/remember",
        json={"content": "Response shape verification content", "source_type": "manual"},
    )
    assert response.status_code == 201
    body = response.json()
    assert "id" in body
    assert "status" in body
    assert "review_status" in body
    assert "memory_confidence" in body


async def test_subject_fields_stored(client):
    """subject_type, subject_id, subject_name are accepted and stored."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    response = await client.post(
        "/v1/remember",
        json={
            "content": "Subject field test content",
            "source_type": "manual",
            "subject_type": "project",
            "subject_id": "proj-42",
            "subject_name": "Project Alpha",
        },
    )
    assert response.status_code == 201
    assert response.json()["status"] == "created"


async def test_external_fields_stored(client):
    """external_id and external_source are accepted for imports."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    response = await client.post(
        "/v1/remember",
        json={
            "content": "External import test content",
            "source_type": "import",
            "external_id": "ext-001",
            "external_source": "legacy-memstore",
        },
    )
    assert response.status_code == 201
    assert response.json()["status"] == "created"


async def test_sensitivity_field_accepted(client):
    """sensitivity field is accepted with valid values."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    response = await client.post(
        "/v1/remember",
        json={
            "content": "Sensitive content test",
            "source_type": "manual",
            "sensitivity": "sensitive",
        },
    )
    assert response.status_code == 201
    assert response.json()["status"] == "created"


async def test_sensitivity_restricted_accepted(client):
    """sensitivity='restricted' is the correct product vocabulary and succeeds."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    response = await client.post(
        "/v1/remember",
        json={
            "content": "Restricted content test",
            "source_type": "manual",
            "sensitivity": "restricted",
        },
    )
    assert response.status_code == 201
    assert response.json()["status"] == "created"


async def test_sensitivity_confidential_rejected_with_422(client):
    """sensitivity='confidential' is not a valid value — Pydantic must reject it
    with a 422 before the request ever reaches the database CHECK constraint."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    response = await client.post(
        "/v1/remember",
        json={
            "content": "Confidential content test",
            "source_type": "manual",
            "sensitivity": "confidential",
        },
    )
    assert response.status_code == 422


# ---- Classification → trust/visibility wiring (ENG-AUD-005) ----


async def _stored_item(item_id: str) -> dict[str, object]:
    """Read the stored memory_items row to verify visibility/confidence on disk."""
    async with _test_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT visibility, memory_confidence FROM memory_items WHERE id = :id"
            ),
            {"id": item_id},
        )
        return dict(result.mappings().one())


async def _latest_classification_event() -> dict[str, object]:
    async with _test_engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT new_value
                FROM item_events
                WHERE event_type = 'classification'
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        )
        row = result.mappings().one()
        return json.loads(row["new_value"])


def _patch_classifier(monkeypatch, **result_kwargs):
    """Make /v1/remember classify() return a deterministic ClassificationResult.

    kwargs override the defaults, including ``confidence`` and
    ``suggested_visibility``.
    """
    defaults: dict[str, object] = {
        "suggested_kind": "observation",
        "confidence": 0.5,
        "reason": "test classifier",
        "rules_matched": [],
        "provenance": {"provider": "openai", "mode": "llm"},
    }
    defaults.update(result_kwargs)

    async def fake_classifier(content: str, tenant_id, session, context=None):
        return ClassificationResult(**defaults)  # type: ignore[arg-type]

    monkeypatch.setattr(memory_routes, "classify_rules_only", fake_classifier)


async def test_blend_weak_source_low_classifier_lowers_stored_confidence(client, monkeypatch):
    """sync_turn + low classifier confidence → stored memory_confidence is blended down."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    _patch_classifier(monkeypatch, confidence=0.2)
    response = await client.post(
        "/v1/remember",
        json={"content": "Turn summary from sync", "source_type": "sync_turn"},
    )
    assert response.status_code == 201
    # 0.5*0.4 + 0.5*0.2 = 0.30, authority cap = 0.4
    assert response.json()["memory_confidence"] == pytest.approx(0.30)


async def test_blend_weak_source_high_classifier_capped_by_authority(client, monkeypatch):
    """sync_turn + high classifier confidence cannot self-promote past source authority."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    _patch_classifier(monkeypatch, confidence=0.9)
    response = await client.post(
        "/v1/remember",
        json={"content": "Turn summary from sync", "source_type": "sync_turn"},
    )
    assert response.status_code == 201
    # blended = 0.65 but cap = max(0.4, 0.4) = 0.4
    assert response.json()["memory_confidence"] == pytest.approx(0.40)


async def test_blend_manual_source_modest_drop_on_uncertain_classification(client, monkeypatch):
    """manual_user + low classifier confidence → modest drop, not aggressive."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    _patch_classifier(monkeypatch, confidence=0.2)
    response = await client.post(
        "/v1/remember",
        json={"content": "A manually recorded fact", "source_type": "manual"},
    )
    assert response.status_code == 201
    # 0.85*0.9 + 0.15*0.2 = 0.795
    assert response.json()["memory_confidence"] == pytest.approx(0.795)


async def test_explicit_kind_preserves_default_confidence_and_visibility(client, monkeypatch):
    """Explicit-kind writes skip classification → confidence/visibility untouched."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")

    async def should_not_run(*args, **kwargs):
        raise AssertionError("classify() must not be called when kind is explicit")

    monkeypatch.setattr(memory_routes, "classify_rules_only", should_not_run)
    response = await client.post(
        "/v1/remember",
        json={
            "content": "Explicit kind keeps defaults",
            "kind": "fact",
            "source_type": "manual",
            "visibility": "tenant",
        },
    )
    assert response.status_code == 201
    body = response.json()
    # manual_user default, no blend
    assert body["memory_confidence"] == pytest.approx(0.9)
    stored = await _stored_item(body["id"])
    assert stored["visibility"] == "tenant"


async def test_visibility_narrowed_downward_on_remember(client, monkeypatch):
    """requested=tenant, suggested=private → stored=private."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    _patch_classifier(monkeypatch, suggested_visibility="private")
    response = await client.post(
        "/v1/remember",
        json={
            "content": "Narrow me down",
            "source_type": "manual",
            "visibility": "tenant",
        },
    )
    assert response.status_code == 201
    stored = await _stored_item(response.json()["id"])
    assert stored["visibility"] == "private"


async def test_visibility_not_widened_on_remember(client, monkeypatch):
    """requested=private, suggested=tenant → stored=private (never widens)."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    _patch_classifier(monkeypatch, suggested_visibility="tenant")
    response = await client.post(
        "/v1/remember",
        json={
            "content": "Do not widen me",
            "source_type": "manual",
            "visibility": "private",
        },
    )
    assert response.status_code == 201
    stored = await _stored_item(response.json()["id"])
    assert stored["visibility"] == "private"


async def test_visibility_preserved_when_classifier_has_no_suggestion(client, monkeypatch):
    """requested=workspace, suggested=None → stored=workspace."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    _patch_classifier(monkeypatch, suggested_visibility=None)
    response = await client.post(
        "/v1/remember",
        json={
            "content": "No visibility opinion",
            "source_type": "manual",
            "visibility": "workspace",
        },
    )
    assert response.status_code == 201
    stored = await _stored_item(response.json()["id"])
    assert stored["visibility"] == "workspace"


async def test_classification_event_records_trust_and_visibility_audit(client, monkeypatch):
    """The classification event JSON records requested/suggested/final visibility
    and default/final memory_confidence plus the applied policy flags."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    _patch_classifier(monkeypatch, confidence=0.2, suggested_visibility="private")
    response = await client.post(
        "/v1/remember",
        json={
            "content": "Audit my trust decisions",
            "source_type": "sync_turn",
            "visibility": "tenant",
        },
    )
    assert response.status_code == 201
    payload = await _latest_classification_event()
    assert payload["source"] == "auto_classified"
    assert payload["source_type"] == "sync_turn"
    assert payload["requested_visibility"] == "tenant"
    assert payload["suggested_visibility"] == "private"
    assert payload["final_visibility"] == "private"
    assert payload["visibility_narrowed"] is True
    assert payload["default_memory_confidence"] == pytest.approx(0.4)
    assert payload["final_memory_confidence"] == pytest.approx(0.30)
    assert payload["memory_confidence_blended"] is True
    # The classifier dump carries its own confidence/visibility for traceability.
    assert payload["classification"]["confidence"] == pytest.approx(0.2)
    assert payload["classification"]["suggested_visibility"] == "private"
