"""Tests for conflict detection (T09 / ENG-AUD-008).

These tests require a live PostgreSQL with the v2 schema (migrations/001_init.sql).
They skip automatically when no DB is reachable, matching the pattern in
test_remember.py / test_search.py.

As of ENG-AUD-008, conflict detection runs OFF the write path: ``/v1/remember``
enqueues an ``embedding.generate`` job, which (when ready) enqueues a
``conflict.check`` job that runs ``detect_conflicts`` and applies conservative
state transitions. These tests POST items, then drain the worker job queue to
drive the async conflict detection, then assert on the resulting memory state.

Embeddings are generated through a fake provider (orthogonal unit vectors),
and the conflict classifier is monkeypatched at the module level so the real
detect_conflicts() runs end-to-end with a controlled verdict.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import engram.embeddings as embeddings_mod
from engram.api.app import create_app
from engram.api.routes import memory as memory_routes
from engram.config import settings
from engram.conflicts import ConflictAction, ConflictVerdict
from engram.db import get_session

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
    async with _test_session_factory() as session:
        from sqlalchemy import text as sa_text

        from engram.db import _DEFAULT_PRINCIPAL_NAME, _DEFAULT_TENANT_SLUG

        row = (
            (
                await session.execute(
                    sa_text(
                        "SELECT t.id::text AS tenant_id, p.id::text AS principal_id "
                        "FROM tenants t "
                        "JOIN principals p ON p.tenant_id = t.id AND p.name = :principal "
                        "WHERE t.slug = :slug"
                    ),
                    {"slug": _DEFAULT_TENANT_SLUG, "principal": _DEFAULT_PRINCIPAL_NAME},
                )
            )
            .mappings()
            .one()
        )
        from engram.db import apply_rls_context

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
    if not await _db_ok():
        return
    async with _test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM jobs"))
        await conn.execute(text("DELETE FROM memory_embeddings"))
        await conn.execute(text("DELETE FROM memory_items"))


# ---- Helpers ----

_VECTOR_A = [1.0] + [0.0] * 1535
_VECTOR_B = [1.0, 0.0] + [0.0] * 1534  # near-identical to A → similarity > 0.85


def _enable_embeddings(monkeypatch):
    """Turn on the fake embedding provider and stub the generator.

    Patches BOTH ``memory_routes.generate_embedding`` (used by search/recall
    query embedding) and ``engram.embeddings.generate_embedding`` (used by the
    worker's embedding.generate handler). The worker imports it lazily at
    handler-call time, so patching the module attribute is sufficient.
    """
    settings.embedding_provider = "openai"

    async def fake_embedding(text_value: str, *_args: object, **_kwargs: object):
        # Items we want to look similar share vector A.
        if "dup" in text_value or "same" in text_value or "refine" in text_value:
            return _VECTOR_A
        if "contradict" in text_value or "resolve" in text_value or "review conflict" in text_value:
            return _VECTOR_A
        if "authority" in text_value:
            return _VECTOR_A
        return _VECTOR_B

    monkeypatch.setattr(memory_routes, "generate_embedding", fake_embedding)
    monkeypatch.setattr(embeddings_mod, "generate_embedding", fake_embedding)


async def _drain_jobs(max_iterations: int = 10) -> None:
    """Process queued jobs until the queue is empty.

    Drives the async write path: embedding.generate → conflict.check. Uses the
    worker's process_one_job against the per-test session factories so the same
    NullPool engine (current event loop) is used. Conflict check on write is
    honored automatically by the embedding handler.
    """
    from engram.worker import process_one_job

    for _ in range(max_iterations):
        processed = await process_one_job(
            worker_id="test",
            session_factory=_test_session_factory,
            app_session_factory=_test_session_factory,
        )
        if not processed:
            return


def _stub_verdict(monkeypatch, verdict: ConflictVerdict, confidence: float = 0.9):
    """Force the conflict classifier to return ``verdict`` with ``confidence``.

    Monkeypatches engram.conflicts._classify_relationship so the real
    detect_conflicts() similarity search runs, but the verdict is controlled.
    """

    async def fake_classify(old_content, new_content, similarity, **_kwargs):
        return verdict, confidence, f"forced verdict: {verdict.value}", {"provider": "test"}

    monkeypatch.setattr("engram.conflicts._classify_relationship", fake_classify)


async def _fetch_item_fields(item_id: str) -> dict[str, object]:
    async with _test_session_factory() as session:
        return (
            await session.execute(
                text(
                    "SELECT conflicts_with_item_id, conflict_type, "
                    "conflict_resolution_status, review_status, superseded_by, valid_to "
                    "FROM memory_items WHERE id = :id"
                ),
                {"id": item_id},
            )
        ).mappings().one()


# ---- DB-backed integration tests ----


async def test_conflict_check_skipped_when_no_embeddings(client, monkeypatch):
    """With embedding_provider='none', conflict detection does not run."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "none"

    called = False
    import engram.conflicts as conflicts_mod

    async def spy(*args, **kwargs):
        nonlocal called
        called = True
        return ConflictVerdict.REFINE, 0.9, "spy", {}

    monkeypatch.setattr(conflicts_mod, "_classify_relationship", spy)

    response = await client.post(
        "/v1/remember", json={"content": "no embeddings conflict test", "kind": "fact"}
    )
    assert response.status_code == 201
    assert response.json()["status"] == "created"
    assert not called, "classifier must not run when embeddings are disabled"


async def test_duplicate_auto_dedups(client, monkeypatch):
    """duplicate verdict → the new item is rejected+invalidated (eventual, async).

    As of ENG-AUD-008, conflict detection runs off the write path: the second
    /v1/remember returns 'created' (a placeholder + jobs), then the worker
    drains embedding.generate → conflict.check, which rejects the duplicate.
    """
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    _enable_embeddings(monkeypatch)
    _stub_verdict(monkeypatch, ConflictVerdict.DUPLICATE, confidence=0.95)

    first = await client.post(
        "/v1/remember",
        json={"content": "dup original content", "kind": "fact", "source_type": "manual"},
    )
    assert first.status_code == 201
    assert first.json()["status"] == "created"
    first_id = first.json()["id"]

    second = await client.post(
        "/v1/remember",
        json={"content": "dup reworded content", "kind": "fact", "source_type": "manual"},
    )
    assert second.status_code == 201
    # The write path returns 'created' immediately; the dedup is eventual.
    assert second.json()["status"] == "created"
    new_id = second.json()["id"]

    await _drain_jobs()

    # After the conflict.check job runs, the duplicate (new) item is rejected
    # and invalidated; the original survives.
    new = await _fetch_item_fields(new_id)
    assert new["review_status"] == "rejected"
    assert new["valid_to"] is not None
    original = await _fetch_item_fields(first_id)
    assert original["review_status"] != "rejected"


async def test_refine_auto_supersede_high_authority(client, monkeypatch):
    """refine + high source_trust + high confidence → auto-supersede (eventual)."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    _enable_embeddings(monkeypatch)
    # manual_user source_trust = 0.9 (high); confidence 0.9 (>= 0.8)
    _stub_verdict(monkeypatch, ConflictVerdict.REFINE, confidence=0.9)

    first = await client.post(
        "/v1/remember",
        json={"content": "refine original content", "kind": "fact", "source_type": "manual"},
    )
    assert first.status_code == 201
    first_id = first.json()["id"]

    second = await client.post(
        "/v1/remember",
        json={"content": "refine improved content", "kind": "fact", "source_type": "manual"},
    )
    assert second.status_code == 201
    new_id = second.json()["id"]
    # The supersession is eventual (applied by the conflict.check job).
    assert second.json()["status"] == "created"

    await _drain_jobs()

    # Old item must be marked superseded by the new item.
    old = await _fetch_item_fields(first_id)
    assert old["valid_to"] is not None
    assert str(old["superseded_by"]) == new_id


async def test_refine_proposed_supersession_medium_confidence(client, monkeypatch):
    """refine + medium confidence (< 0.8) → proposed supersession, flagged for review."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    _enable_embeddings(monkeypatch)
    # manual_user source_trust = 0.9 (high) but confidence 0.6 (< 0.8) → proposed
    _stub_verdict(monkeypatch, ConflictVerdict.REFINE, confidence=0.6)

    first = await client.post(
        "/v1/remember",
        json={"content": "medium refine original", "kind": "fact", "source_type": "manual"},
    )
    assert first.status_code == 201
    assert first.json()["status"] == "created"
    first_id = first.json()["id"]

    second = await client.post(
        "/v1/remember",
        json={"content": "medium refine update", "kind": "fact", "source_type": "manual"},
    )
    assert second.status_code == 201
    body = second.json()
    # Proposed supersession: new item created (not deduped, not superseded old)
    assert body["status"] == "created"
    new_id = body["id"]

    await _drain_jobs()

    row = await _fetch_item_fields(new_id)
    assert str(row["conflicts_with_item_id"]) == first_id
    assert row["conflict_type"] == "stale"
    assert row["conflict_resolution_status"] == "unresolved"
    assert row["review_status"] == "proposed"

    # Old item must NOT be superseded.
    old = await _fetch_item_fields(first_id)
    assert old["superseded_by"] is None
    assert old["valid_to"] is None


async def test_refine_lower_authority_never_supersedes(client, monkeypatch):
    """refine where new item has lower authority than old → scope_overlap flag."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    _enable_embeddings(monkeypatch)
    _stub_verdict(monkeypatch, ConflictVerdict.REFINE, confidence=0.95)

    # Old item: high authority (manual_user, source_trust=0.9, review_status=active)
    first = await client.post(
        "/v1/remember",
        json={"content": "authority old item", "kind": "fact", "source_type": "manual"},
    )
    assert first.status_code == 201
    first_id = first.json()["id"]

    # New item: low authority (extraction, source_trust=0.5) → lower than old
    second = await client.post(
        "/v1/remember",
        json={"content": "authority new item", "kind": "fact", "source_type": "extraction"},
    )
    assert second.status_code == 201
    body = second.json()
    assert body["status"] == "created"
    new_id = body["id"]

    await _drain_jobs()

    row = await _fetch_item_fields(new_id)
    assert str(row["conflicts_with_item_id"]) == first_id
    assert row["conflict_type"] == "scope_overlap"
    assert row["review_status"] == "proposed"

    old = await _fetch_item_fields(first_id)
    assert old["superseded_by"] is None
    assert old["valid_to"] is None


async def test_contradict_flags_conflict(client, monkeypatch):
    """contradict verdict → conflict flagged, review_status='proposed'."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    _enable_embeddings(monkeypatch)
    _stub_verdict(monkeypatch, ConflictVerdict.CONTRADICT, confidence=0.9)

    first = await client.post(
        "/v1/remember",
        json={"content": "contradict original claim", "kind": "fact", "source_type": "manual"},
    )
    assert first.status_code == 201
    first_id = first.json()["id"]

    second = await client.post(
        "/v1/remember",
        json={"content": "contradict opposing claim", "kind": "fact", "source_type": "manual"},
    )
    assert second.status_code == 201
    body = second.json()
    assert body["status"] == "created"
    new_id = body["id"]

    await _drain_jobs()

    row = await _fetch_item_fields(new_id)
    assert str(row["conflicts_with_item_id"]) == first_id
    assert row["conflict_type"] == "contradiction"
    assert row["conflict_resolution_status"] == "unresolved"
    assert row["review_status"] == "proposed"


async def test_no_conflict_below_similarity_threshold(client, monkeypatch):
    """Dissimilar embeddings do not trigger conflict detection."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "openai"

    async def fake_embedding(text_value: str, *_args: object, **_kwargs: object):
        # Every item gets a distinct orthogonal vector → near-zero similarity.
        idx = abs(hash(text_value)) % 1536
        vec = [0.0] * 1536
        vec[idx] = 1.0
        return vec

    monkeypatch.setattr(memory_routes, "generate_embedding", fake_embedding)
    monkeypatch.setattr(embeddings_mod, "generate_embedding", fake_embedding)
    # If the threshold were crossed this would force a dedup; it should not fire.
    _stub_verdict(monkeypatch, ConflictVerdict.DUPLICATE, confidence=0.99)

    first = await client.post(
        "/v1/remember",
        json={"content": "dissimilar alpha", "kind": "fact", "source_type": "manual"},
    )
    second = await client.post(
        "/v1/remember",
        json={"content": "dissimilar beta", "kind": "fact", "source_type": "manual"},
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["status"] == "created"
    second_id = second.json()["id"]

    await _drain_jobs()
    # No conflict applied: the second item remains active (manual default).
    row = await _fetch_item_fields(second_id)
    assert row["conflicts_with_item_id"] is None


async def test_review_conflicts_lists_unresolved(client, monkeypatch):
    """GET /v1/review/conflicts lists items with unresolved conflicts."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    _enable_embeddings(monkeypatch)
    _stub_verdict(monkeypatch, ConflictVerdict.CONTRADICT, confidence=0.9)

    first = await client.post(
        "/v1/remember",
        json={"content": "review conflict original", "kind": "fact", "source_type": "manual"},
    )
    second = await client.post(
        "/v1/remember",
        json={"content": "review conflict opposing", "kind": "fact", "source_type": "manual"},
    )
    assert first.status_code == 201
    assert second.status_code == 201
    new_id = second.json()["id"]

    await _drain_jobs()

    response = await client.get("/v1/review/conflicts")
    assert response.status_code == 200
    body = response.json()
    ids = [item["id"] for item in body["items"]]
    assert new_id in ids
    assert all(item["conflict_resolution_status"] == "unresolved" for item in body["items"])


async def test_resolve_conflict_accepts_resolution(client, monkeypatch):
    """POST /v1/items/{id}/resolve-conflict sets resolution status + writes event."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    _enable_embeddings(monkeypatch)
    _stub_verdict(monkeypatch, ConflictVerdict.CONTRADICT, confidence=0.9)

    first = await client.post(
        "/v1/remember",
        json={"content": "resolve original claim", "kind": "fact", "source_type": "manual"},
    )
    second = await client.post(
        "/v1/remember",
        json={"content": "resolve opposing claim", "kind": "fact", "source_type": "manual"},
    )
    assert first.status_code == 201
    assert second.status_code == 201
    conflict_id = second.json()["id"]

    await _drain_jobs()

    response = await client.post(
        f"/v1/items/{conflict_id}/resolve-conflict",
        json={"resolution": "accepted", "reason": "newer info is correct"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["conflict_resolution_status"] == "accepted"

    row = await _fetch_item_fields(conflict_id)
    assert row["conflict_resolution_status"] == "accepted"

    # An item_event must have been written.
    async with _test_session_factory() as session:
        event_count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM item_events "
                    "WHERE item_id = :id AND event_type = 'conflict_resolution'"
                ),
                {"id": conflict_id},
            )
        ).scalar_one()
    assert event_count == 1


async def test_resolve_conflict_rejects_item_without_conflict(client, monkeypatch):
    """Resolving an item without a conflict returns 422."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    settings.embedding_provider = "none"
    created = await client.post(
        "/v1/remember",
        json={"content": "item without conflict", "kind": "fact", "source_type": "manual"},
    )
    assert created.status_code == 201
    item_id = created.json()["id"]

    response = await client.post(
        f"/v1/items/{item_id}/resolve-conflict",
        json={"resolution": "accepted"},
    )
    assert response.status_code == 422


# ---- Unit tests for the pure decision function (no DB required) ----


def _resolve_action(*args, **kwargs):
    from engram.conflicts import _resolve_action as impl

    return impl(*args, **kwargs)


def test_resolve_action_duplicate():
    action, conflict_type = _resolve_action(
        verdict=ConflictVerdict.DUPLICATE,
        new_authority=10,
        old_authority=50,
        classifier_confidence=0.9,
    )
    assert action is ConflictAction.DEDUP
    assert conflict_type == "duplicate"


def test_resolve_action_refine_auto_supersede():
    action, conflict_type = _resolve_action(
        verdict=ConflictVerdict.REFINE,
        new_authority=50,
        old_authority=50,
        classifier_confidence=0.85,
    )
    assert action is ConflictAction.AUTO_SUPERSEDE
    assert conflict_type is None


def test_resolve_action_refine_lower_authority():
    action, conflict_type = _resolve_action(
        verdict=ConflictVerdict.REFINE,
        new_authority=10,
        old_authority=50,
        classifier_confidence=0.95,
    )
    assert action is ConflictAction.FLAG_SCOPE_OVERLAP
    assert conflict_type == "scope_overlap"


def test_resolve_action_refine_medium_confidence():
    action, conflict_type = _resolve_action(
        verdict=ConflictVerdict.REFINE,
        new_authority=50,
        old_authority=50,
        classifier_confidence=0.6,
    )
    assert action is ConflictAction.PROPOSED_SUPERSEDE
    assert conflict_type == "stale"


def test_resolve_action_contradict():
    action, conflict_type = _resolve_action(
        verdict=ConflictVerdict.CONTRADICT,
        new_authority=50,
        old_authority=50,
        classifier_confidence=0.9,
    )
    assert action is ConflictAction.FLAG_CONTRADICTION
    assert conflict_type == "contradiction"


def test_trusted_agent_does_not_auto_supersede() -> None:
    action, conflict_type = _resolve_action(
        verdict=ConflictVerdict.REFINE,
        new_authority=30,
        old_authority=10,
        classifier_confidence=1.0,
    )
    assert action is ConflictAction.PROPOSED_SUPERSEDE
    assert conflict_type == "stale"


def test_trusted_import_auto_supersedes_independent_of_source_trust() -> None:
    action, conflict_type = _resolve_action(
        verdict=ConflictVerdict.REFINE,
        new_authority=40,
        old_authority=30,
        classifier_confidence=0.8,
    )
    assert action is ConflictAction.AUTO_SUPERSEDE
    assert conflict_type is None
