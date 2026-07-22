"""Real-PostgreSQL deterministic proofs for the memory E2E audit harness.

These tests prove that Promotion Path A is deterministically reachable and
that the RLS/visibility access boundaries used by the audit harness hold,
using controlled fixture data through the supported service/repository paths.
They require a live PostgreSQL with the v2 schema (migrations/001_init.sql)
and pgvector, and skip automatically when no DB is reachable — UNLESS
``ENGRAM_FAIL_ON_DB_SKIP=1`` is set (the Compose CI path), in which case a
skip is a failure.

Coverage:

* deterministic promotion positive path — proposed item with complete
  consistent evidence, eligible kind, cooling time satisfied →
  ``would_promote=True`` via the real ``assess_promotion_candidate`` (no
  mocking of the policy evaluator);
* stable promotion blocker matrix — missing evidence, taxonomy below minimum,
  disposition not retain, evidence score below threshold, cooling period,
  kind blocked;
* reviewer-created tenant-visible item becomes active through normal governed
  review;
* agent principal can read a tenant-visible fixture when the profile permits;
* reviewer cannot read another principal's private Fixture W;
* owner diagnostic mode performs no database mutations.

These tests deliberately DO NOT mock the promotion policy evaluator. They use
the real ``load_promotion_support`` + ``assess_promotion_candidate`` against
real rows, so blocker reporting cannot drift from production policy.

The harness's live dogfood promotion result (Stage 2 in
``scripts/run_memory_e2e_audit.py``) remains calibration data and is NOT
required to match the deterministic positive fixture here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.api.app import create_app
from engram.auth import (
    DIGEST_ALGORITHM,
    digest_api_key_secret,
    generate_api_key,
    parse_api_key,
    reset_principal_cache,
)
from engram.config import settings
from engram.promotion import (
    BLOCK_CONFLICT,
    BLOCK_KIND_POLICY,
    BLOCK_NO_EVIDENCE,
    BLOCK_SCORE,
    BLOCK_TAXONOMY,
    assess_promotion_candidate,
    load_promotion_support,
)
from engram.promotion_policy import DEFAULT_EVIDENCE_THRESHOLD, EVIDENCE_TAXONOMY_MINIMUM

_DB_SKIP_REASON = "requires a live PostgreSQL with the v2 schema"

# Module-global engine, recreated per test (same pattern as test_promotion.py).
_test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_test_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    _test_engine, class_=AsyncSession, expire_on_commit=False
)
_isolated_tenant_id: str | None = None
_isolated_admin_id: str | None = None


@pytest.fixture(autouse=True)
async def _fresh_engine() -> Any:
    """Give each test a brand-new NullPool engine on its own loop."""
    global _test_engine, _test_session_factory
    _test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
    _test_session_factory = async_sessionmaker(
        _test_engine, class_=AsyncSession, expire_on_commit=False
    )
    yield
    await _test_engine.dispose()


async def _db_ok() -> bool:
    try:
        async with _test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _require_db() -> None:
    if not _db_ok_sync():
        pytest.skip(_DB_SKIP_REASON)


def _db_ok_sync() -> bool:
    # We can't await in a sync helper; rely on a best-effort connect attempt.
    import asyncio

    try:
        asyncio.get_running_loop()
        # Already in an async context — just return True and let the real
        # query fail later if the DB is down. The per-test _db_ok() guard
        # below is the authoritative check.
        return True
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_db_ok())
        finally:
            loop.close()


@pytest.fixture(autouse=True)
async def _isolated_test_tenant(_fresh_engine: Any) -> Any:
    """Create one tenant per test and remove it by tenant cascade only."""
    global _isolated_tenant_id, _isolated_admin_id
    if not await _db_ok():
        yield
        return
    tenant_id = str(uuid.uuid4())
    admin_id = str(uuid.uuid4())
    slug = f"memory-audit-{uuid.uuid4().hex}"
    async with _test_engine.connect() as conn:
        default_before = (
            await conn.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM memory_items WHERE tenant_id=t.id), "
                    "(SELECT count(*) FROM jobs WHERE tenant_id=t.id), "
                    "(SELECT count(*) FROM item_events e JOIN memory_items m ON m.id=e.item_id "
                    " WHERE m.tenant_id=t.id), "
                    "(SELECT row_to_json(c)::text FROM tenant_config c "
                    " WHERE c.tenant_id=t.id AND c.active=true) "
                    "FROM tenants t WHERE t.slug='default'"
                )
            )
        ).one()
    async with _test_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
            {"id": tenant_id, "name": slug, "slug": slug},
        )
        await conn.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES (:id, :tenant_id, 'admin', 'admin')"
            ),
            {"id": admin_id, "tenant_id": tenant_id},
        )
        await conn.execute(
            text("INSERT INTO tenant_config (tenant_id) VALUES (:id)"), {"id": tenant_id}
        )
        await conn.execute(
            text(
                "UPDATE memory_kinds SET auto_promote_from_inferred=TRUE, enabled=TRUE "
                "WHERE tenant_id=:tenant_id AND name='fact'"
            ),
            {"tenant_id": tenant_id},
        )
    _isolated_tenant_id, _isolated_admin_id = tenant_id, admin_id
    try:
        yield
    finally:
        async with _test_engine.begin() as conn:
            await conn.execute(text("DELETE FROM tenants WHERE id=:id"), {"id": tenant_id})
        async with _test_engine.connect() as conn:
            default_after = (
                await conn.execute(
                    text(
                        "SELECT "
                        "(SELECT count(*) FROM memory_items WHERE tenant_id=t.id), "
                        "(SELECT count(*) FROM jobs WHERE tenant_id=t.id), "
                        "(SELECT count(*) FROM item_events e JOIN memory_items m ON m.id=e.item_id "
                        " WHERE m.tenant_id=t.id), "
                        "(SELECT row_to_json(c)::text FROM tenant_config c "
                        " WHERE c.tenant_id=t.id AND c.active=true) "
                        "FROM tenants t WHERE t.slug='default'"
                    )
                )
            ).one()
        assert default_after == default_before
        _isolated_tenant_id = _isolated_admin_id = None


async def _default_tenant_principal() -> tuple[str, str]:
    assert _isolated_tenant_id is not None
    assert _isolated_admin_id is not None
    return _isolated_tenant_id, _isolated_admin_id


async def _second_principal(tenant_id: str, name: str = "audit-agent") -> str:
    """Create (or reuse) a second agent principal in the default tenant."""
    pid = str(uuid.uuid4())
    async with _test_session_factory() as session:
        existing = (
            await session.execute(
                text("SELECT id::text FROM principals WHERE tenant_id = :tid AND name = :name"),
                {"tid": tenant_id, "name": name},
            )
        ).scalar_one_or_none()
        if existing:
            return str(existing)
        await session.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES (:id, :tid, :name, 'agent')"
            ),
            {"id": pid, "tid": tenant_id, "name": name},
        )
        await session.commit()
    return pid


async def _issue_key(
    tenant_id: str,
    principal_id: str,
    scopes: list[str],
    *,
    profile_id: str | None = None,
) -> str:
    plaintext = generate_api_key()
    parsed = parse_api_key(plaintext)
    assert parsed.key_id is not None
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO api_keys (id, tenant_id, principal_id, key_hash, key_id, "
                "secret_digest, digest_algorithm, scopes, label, memory_profile_id) "
                "VALUES (:id, :tenant_id, :principal_id, NULL, :key_id, :digest, "
                ":algorithm, :scopes, :label, :profile_id)"
            ),
            {
                "id": str(uuid.uuid4()),
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "key_id": parsed.key_id,
                "digest": digest_api_key_secret(parsed.secret),
                "algorithm": DIGEST_ALGORITHM,
                "scopes": scopes,
                "label": f"memory-audit-{uuid.uuid4()}",
                "profile_id": profile_id,
            },
        )
        await session.commit()
    return plaintext


async def _api_client(monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    settings.auth_enabled = True
    reset_principal_cache()
    import engram.db as db_module

    monkeypatch.setattr(db_module, "async_session_factory", _test_session_factory)
    monkeypatch.setattr(db_module, "owner_session_factory", _test_session_factory)
    monkeypatch.setattr(db_module, "read_session_factory", _test_session_factory)
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _create_profile(
    tenant_id: str, creator_id: str, *, include_tenant: bool, slug: str
) -> str:
    profile_id = str(uuid.uuid4())
    revision_id = str(uuid.uuid4())
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO memory_profiles "
                "(id, tenant_id, name, slug, created_by_principal_id) "
                "VALUES (:id, :tenant_id, :name, :slug, :creator)"
            ),
            {
                "id": profile_id,
                "tenant_id": tenant_id,
                "name": slug,
                "slug": slug,
                "creator": creator_id,
            },
        )
        await session.execute(
            text(
                "INSERT INTO memory_profile_revisions "
                "(id, tenant_id, profile_id, version, include_private, include_tenant, "
                "include_public, allow_tenant_write, allow_public_write, "
                "default_write_visibility, created_by_principal_id, reason) "
                "VALUES (:id, :tenant_id, :profile_id, 1, true, :include_tenant, false, "
                "false, false, 'private', :creator, 'audit profile')"
            ),
            {
                "id": revision_id,
                "tenant_id": tenant_id,
                "profile_id": profile_id,
                "include_tenant": include_tenant,
                "creator": creator_id,
            },
        )
        await session.execute(
            text("UPDATE memory_profiles SET active_revision_id=:revision WHERE id=:id"),
            {"revision": revision_id, "id": profile_id},
        )
        await session.commit()
    return profile_id


async def _insert_ready_embedding(item_id: str, tenant_id: str) -> list[float]:
    async with _test_session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id::text, model, dimensions FROM embedding_profiles "
                    "WHERE state='active' LIMIT 1"
                )
            )
        ).one()
        vector = [1.0] + [0.0] * (int(row[2]) - 1)
        rendered = "[" + ",".join(str(value) for value in vector) + "]"
        await session.execute(
            text(
                "INSERT INTO memory_embeddings "
                "(id, memory_item_id, tenant_id, profile_id, embedding_model, "
                "embedding_dim, embedding, embedding_status) "
                "VALUES (:id, :item_id, :tenant_id, :profile_id, :model, "
                ":dimensions, CAST(:embedding AS vector), 'ready')"
            ),
            {
                "id": str(uuid.uuid4()),
                "item_id": item_id,
                "tenant_id": tenant_id,
                "profile_id": row[0],
                "model": row[1],
                "dimensions": row[2],
                "embedding": rendered,
            },
        )
        await session.commit()
    return vector


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


async def _insert_item(
    *,
    tenant_id: str,
    principal_id: str,
    content: str,
    review_status: str = "proposed",
    # Evidence-lane fixtures must not accidentally qualify via the legacy
    # confidence lane; individual legacy tests set their own value explicitly.
    memory_confidence: float = 0.35,
    created_at: datetime | None = None,
    visibility: str = "private",
    kind: str = "fact",
    source_type: str = "manual",
    source_trust: float = 0.5,
    source_confidence_prior: float | None = None,
    retention_confidence: float | None = None,
    retention_disposition: str | None = None,
    retention_evidence_at: datetime | None = None,
    authority: int = 10,
    conflict_resolution_status: str | None = None,
) -> str:
    item_id = str(uuid.uuid4())
    if created_at is None:
        created_at = _now() - timedelta(hours=100)
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO memory_items ("
                "id, tenant_id, principal_id, content, content_hash, kind, "
                "visibility, review_status, memory_confidence, source_trust, "
                "source_confidence_prior, retention_confidence, retention_disposition, "
                "retention_evidence_at, authority, importance, source_type, "
                "conflict_resolution_status, created_at, valid_from"
                ") VALUES ("
                ":id, :tenant_id, :principal_id, :content, :content_hash, :kind, "
                ":visibility, :review_status, :memory_confidence, :source_trust, "
                ":source_confidence_prior, :retention_confidence, :retention_disposition, "
                ":retention_evidence_at, :authority, 0.5, :source_type, "
                ":conflict_resolution_status, :created_at, :created_at"
                ")"
            ),
            {
                "id": item_id,
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "content": content,
                "content_hash": f"sha256:{uuid.uuid4().hex}",
                "review_status": review_status,
                "memory_confidence": memory_confidence,
                "kind": kind,
                "source_type": source_type,
                "source_trust": source_trust,
                "source_confidence_prior": source_confidence_prior,
                "retention_confidence": retention_confidence,
                "retention_disposition": retention_disposition,
                "retention_evidence_at": retention_evidence_at,
                "authority": authority,
                "visibility": visibility,
                "conflict_resolution_status": conflict_resolution_status,
                "created_at": created_at,
            },
        )
        await session.commit()
    return item_id


async def _insert_bound_evidence(
    item_id: str,
    *,
    tenant_id: str,
    principal_id: str,
    created_at: datetime,
    taxonomy_confidence: float = 0.9,
    classification_version: str = "classification-v2",
    retention_policy_version: str = "retention-v1",
) -> str:
    run_id = str(uuid.uuid4())
    async with _test_session_factory() as session:
        item = (
            (
                await session.execute(
                    text(
                        "SELECT content_hash, source_type, kind, retention_confidence, "
                        "retention_disposition FROM memory_items WHERE id = :id"
                    ),
                    {"id": item_id},
                )
            )
            .mappings()
            .one()
        )
        await session.execute(
            text(
                "INSERT INTO classification_runs ("
                "id, tenant_id, principal_id, memory_item_id, bound_at, content_hash, "
                "canonicalization_version, source_type, suggested_kind, taxonomy_confidence, "
                "retention_confidence, retention_disposition, reason, provenance, "
                "classification_version, retention_policy_version, created_at, expires_at"
                ") VALUES ("
                ":id, :tenant_id, :principal_id, :item_id, :created_at, :content_hash, "
                "'canonical-v1', :source_type, :kind, :taxonomy_confidence, "
                ":retention_confidence, :retention_disposition, 'audit evidence', "
                "'{}'::jsonb, :classification_version, :retention_policy_version, "
                ":created_at, :expires_at"
                ")"
            ),
            {
                "id": run_id,
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "item_id": item_id,
                "created_at": created_at,
                "content_hash": item["content_hash"],
                "source_type": item["source_type"],
                "kind": item["kind"],
                "taxonomy_confidence": taxonomy_confidence,
                "retention_confidence": item["retention_confidence"],
                "retention_disposition": item["retention_disposition"],
                "classification_version": classification_version,
                "retention_policy_version": retention_policy_version,
                "expires_at": created_at + timedelta(hours=1),
            },
        )
        await session.commit()
    return run_id


async def _enable_evidence_lane(tenant_id: str) -> None:
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "UPDATE tenant_config SET auto_promote_evidence_enabled = TRUE "
                "WHERE tenant_id = :tid AND active = TRUE"
            ),
            {"tid": tenant_id},
        )
        await session.commit()


async def _assess(item_id: str, *, now: datetime) -> Any:
    """Load support + assess a single item using real production policy."""
    from sqlalchemy import select

    from engram.models import MemoryItem

    async with _test_session_factory() as session:
        item = (
            await session.execute(select(MemoryItem).where(MemoryItem.id == item_id))
        ).scalar_one()
        support_map = await load_promotion_support(session, [item])
        support = support_map[item.id]
    return assess_promotion_candidate(
        item,
        support,
        confidence_threshold=0.7,
        min_age_hours=72,
        evidence_enabled=True,
        evidence_threshold=DEFAULT_EVIDENCE_THRESHOLD,
        now=now,
    )


# ── deterministic promotion positive path ────────────────────────────────────


async def test_deterministic_promotion_positive_path() -> None:
    """Path A is reachable: proposed, live, eligible kind, complete evidence,
    disposition retain, taxonomy above minimum, score above threshold, cooling
    satisfied → would_promote=True, selected_basis=retention_evidence,
    blockers empty."""
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    tenant_id, principal_id = await _default_tenant_principal()
    now = _now()
    evidence_at = now - timedelta(hours=80)

    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="deterministic audit promotion positive fixture",
        created_at=now - timedelta(hours=100),
        memory_confidence=0.35,
        source_type="session_end",
        source_trust=0.35,
        source_confidence_prior=0.35,
        retention_confidence=0.90,
        retention_disposition="retain",
        retention_evidence_at=evidence_at,
        authority=10,
    )
    await _insert_bound_evidence(
        item_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        created_at=evidence_at,
        # PostgreSQL REAL stores 0.70 just below the Python policy boundary
        # (0.699999988...), so choose an unambiguously qualifying value.
        taxonomy_confidence=EVIDENCE_TAXONOMY_MINIMUM + 0.01,
    )
    await _enable_evidence_lane(tenant_id)

    candidate = await _assess(item_id, now=now)
    assert candidate.would_promote is True
    assert candidate.selected_basis == "retention_evidence"
    assert candidate.blockers == []


# ── stable blocker matrix (negative fixtures) ─────────────────────────────────


async def test_blocker_missing_evidence() -> None:
    """No bound classification run → no_retention_evidence blocker."""
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    tenant_id, principal_id = await _default_tenant_principal()
    now = _now()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="audit blocker missing evidence",
        created_at=now - timedelta(hours=100),
        source_confidence_prior=0.35,
        retention_confidence=0.90,
        retention_disposition="retain",
        retention_evidence_at=now - timedelta(hours=80),
    )
    await _enable_evidence_lane(tenant_id)
    candidate = await _assess(item_id, now=now)
    assert candidate.would_promote is False
    assert BLOCK_NO_EVIDENCE in candidate.blockers


async def test_blocker_taxonomy_below_minimum() -> None:
    """Taxonomy confidence below the 0.70 minimum → taxonomy_confidence blocker."""
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    tenant_id, principal_id = await _default_tenant_principal()
    now = _now()
    evidence_at = now - timedelta(hours=80)
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="audit blocker taxonomy low",
        created_at=now - timedelta(hours=100),
        source_confidence_prior=0.35,
        retention_confidence=0.90,
        retention_disposition="retain",
        retention_evidence_at=evidence_at,
    )
    await _insert_bound_evidence(
        item_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        created_at=evidence_at,
        taxonomy_confidence=0.60,
    )
    await _enable_evidence_lane(tenant_id)
    candidate = await _assess(item_id, now=now)
    assert candidate.would_promote is False
    assert BLOCK_TAXONOMY in candidate.blockers


async def test_blocker_disposition_not_retain() -> None:
    """Retention disposition != retain → retention_disposition blocker."""
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    tenant_id, principal_id = await _default_tenant_principal()
    now = _now()
    evidence_at = now - timedelta(hours=80)
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="audit blocker transient disposition",
        created_at=now - timedelta(hours=100),
        source_confidence_prior=0.35,
        retention_confidence=0.90,
        retention_disposition="transient",
        retention_evidence_at=evidence_at,
    )
    await _insert_bound_evidence(
        item_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        created_at=evidence_at,
    )
    await _enable_evidence_lane(tenant_id)
    candidate = await _assess(item_id, now=now)
    assert candidate.would_promote is False
    assert "retention_disposition" in candidate.blockers


async def test_blocker_evidence_score_below_threshold() -> None:
    """Score below the 0.70 threshold → evidence_score blocker."""
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    tenant_id, principal_id = await _default_tenant_principal()
    now = _now()
    evidence_at = now - timedelta(hours=80)
    # score = 0.20*prior + 0.80*retention; pick values that stay below 0.70
    # 0.20*0.30 + 0.80*0.60 = 0.06 + 0.48 = 0.54 < 0.70
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="audit blocker score low",
        created_at=now - timedelta(hours=100),
        source_confidence_prior=0.30,
        retention_confidence=0.60,
        retention_disposition="retain",
        retention_evidence_at=evidence_at,
    )
    await _insert_bound_evidence(
        item_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        created_at=evidence_at,
    )
    await _enable_evidence_lane(tenant_id)
    candidate = await _assess(item_id, now=now)
    assert candidate.would_promote is False
    assert BLOCK_SCORE in candidate.blockers


async def test_blocker_cooling_period() -> None:
    """Evidence created too recently → cooling period not elapsed → age blocker."""
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    tenant_id, principal_id = await _default_tenant_principal()
    now = _now()
    # Evidence only 10 hours old; needs 72h cooling.
    evidence_at = now - timedelta(hours=10)
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="audit blocker cooling period",
        created_at=now - timedelta(hours=100),
        source_confidence_prior=0.35,
        retention_confidence=0.90,
        retention_disposition="retain",
        retention_evidence_at=evidence_at,
    )
    await _insert_bound_evidence(
        item_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        created_at=evidence_at,
    )
    await _enable_evidence_lane(tenant_id)
    candidate = await _assess(item_id, now=now)
    assert candidate.would_promote is False
    assert "age" in candidate.blockers


async def test_blocker_kind_disabled() -> None:
    """Kind with auto_promote_from_inferred disabled → kind_policy blocker."""
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    tenant_id, principal_id = await _default_tenant_principal()
    now = _now()
    evidence_at = now - timedelta(hours=80)
    # Disable auto-promote for the fact kind for this tenant.
    async with _test_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE memory_kinds SET auto_promote_from_inferred = FALSE "
                "WHERE name = 'fact' AND tenant_id = :tid"
            ),
            {"tid": tenant_id},
        )
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="audit blocker kind disabled",
        created_at=now - timedelta(hours=100),
        source_confidence_prior=0.35,
        retention_confidence=0.90,
        retention_disposition="retain",
        retention_evidence_at=evidence_at,
    )
    await _insert_bound_evidence(
        item_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        created_at=evidence_at,
    )
    await _enable_evidence_lane(tenant_id)
    candidate = await _assess(item_id, now=now)
    assert candidate.would_promote is False
    assert BLOCK_KIND_POLICY in candidate.blockers


async def test_blocker_conflict_unresolved() -> None:
    """An unresolved conflict on an otherwise-eligible item → conflict blocker."""
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    tenant_id, principal_id = await _default_tenant_principal()
    now = _now()
    evidence_at = now - timedelta(hours=80)
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="audit blocker conflict",
        created_at=now - timedelta(hours=100),
        source_confidence_prior=0.35,
        retention_confidence=0.90,
        retention_disposition="retain",
        retention_evidence_at=evidence_at,
        conflict_resolution_status="unresolved",
    )
    await _insert_bound_evidence(
        item_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        created_at=evidence_at,
    )
    await _enable_evidence_lane(tenant_id)
    candidate = await _assess(item_id, now=now)
    assert candidate.would_promote is False
    assert BLOCK_CONFLICT in candidate.blockers


# ── governed review transition (Fixture R model) ─────────────────────────────


async def test_whoami_reports_every_authenticated_principal_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    tenant_id, admin_id = await _default_tenant_principal()
    identities = [(admin_id, "admin")]
    for principal_type in ("agent", "user", "system"):
        identities.append(
            (await _second_principal(tenant_id, f"audit-{principal_type}"), principal_type)
        )
        async with _test_session_factory() as session:
            await session.execute(
                text("UPDATE principals SET type=:type WHERE id=:id"),
                {"type": principal_type, "id": identities[-1][0]},
            )
            await session.commit()
    client = await _api_client(monkeypatch)
    async with client:
        for principal_id, principal_type in identities:
            token = await _issue_key(tenant_id, principal_id, ["read"])
            response = await client.get("/whoami", headers=_auth(token))
            assert response.status_code == 200
            assert response.json()["principal_type"] == principal_type


async def test_reviewer_created_tenant_item_becomes_active_through_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reviewer-created tenant-visible proposed item can be activated through
    the normal governed review endpoint (no direct DB mutation)."""
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    tenant_id, reviewer_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=reviewer_id,
        content="audit controlled recall fixture via review",
        visibility="tenant",
        review_status="proposed",
    )
    agent_id = await _second_principal(tenant_id)
    reviewer_key = await _issue_key(tenant_id, reviewer_id, ["read", "write", "review"])
    agent_key = await _issue_key(tenant_id, agent_id, ["read", "write"])
    client = await _api_client(monkeypatch)
    reason = "Controlled Engram memory E2E audit fixture"
    async with client:
        identity = await client.get("/whoami", headers=_auth(reviewer_key))
        assert identity.status_code == 200
        assert identity.json()["principal_type"] == "admin"
        denied = await client.post(
            f"/v1/items/{item_id}/review",
            json={"review_status": "active", "reason": reason},
            headers=_auth(agent_key),
        )
        assert denied.status_code == 403
        hidden = await client.post(
            f"/v1/items/{uuid.uuid4()}/review",
            json={"review_status": "active", "reason": reason},
            headers=_auth(reviewer_key),
        )
        assert hidden.status_code == 404
        activated = await client.post(
            f"/v1/items/{item_id}/review",
            json={"review_status": "active", "reason": reason},
            headers=_auth(reviewer_key),
        )
        assert activated.status_code == 200, activated.text

    async with _test_session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT m.review_status, e.actor_principal_id::text, e.reason "
                    "FROM memory_items m JOIN item_events e ON e.item_id=m.id "
                    "WHERE m.id=:id AND e.old_value='proposed' AND e.new_value='active'"
                ),
                {"id": item_id},
            )
        ).one()
    assert row[0] == "active"
    assert row[1] == reviewer_id
    assert row[2] == reason


# ── visibility / access boundaries ───────────────────────────────────────────


async def test_profile_bound_allow_and_deny_direct_read_and_semantic_recall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bound profile revisions govern real API reads and semantic recall."""
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    tenant_id, reviewer_id = await _default_tenant_principal()
    agent_id = await _second_principal(tenant_id)
    marker = f"AUDIT-RECALL-{uuid.uuid4()}"
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=reviewer_id,
        content=f"The controlled Engram recall marker is {marker}.",
        visibility="tenant",
        review_status="active",
    )
    vector = await _insert_ready_embedding(item_id, tenant_id)
    allowed_profile = await _create_profile(
        tenant_id, reviewer_id, include_tenant=True, slug=f"allowed-{uuid.uuid4().hex}"
    )
    denied_profile = await _create_profile(
        tenant_id, reviewer_id, include_tenant=False, slug=f"denied-{uuid.uuid4().hex}"
    )
    allowed_key = await _issue_key(
        tenant_id, agent_id, ["read"], profile_id=allowed_profile
    )
    denied_key = await _issue_key(tenant_id, agent_id, ["read"], profile_id=denied_profile)

    async def fixed_embedding(*args: Any, **kwargs: Any) -> list[float]:
        return vector

    import engram.recall as recall_module

    monkeypatch.setattr(recall_module, "generate_embedding", fixed_embedding)
    client = await _api_client(monkeypatch)
    async with client:
        allowed_read = await client.get(f"/v1/items/{item_id}", headers=_auth(allowed_key))
        denied_read = await client.get(f"/v1/items/{item_id}", headers=_auth(denied_key))
        assert allowed_read.status_code == 200
        assert denied_read.status_code == 404
        allowed_recall = await client.post(
            "/v1/recall",
            json={"mode": "semantic", "query": marker},
            headers=_auth(allowed_key),
        )
        denied_recall = await client.post(
            "/v1/recall",
            json={"mode": "semantic", "query": marker},
            headers=_auth(denied_key),
        )
        assert allowed_recall.status_code == 200, allowed_recall.text
        assert denied_recall.status_code == 200, denied_recall.text
        assert item_id in {str(item["id"]) for item in allowed_recall.json()["items"]}
        assert item_id not in {str(item["id"]) for item in denied_recall.json()["items"]}


async def test_reviewer_cannot_read_private_fixture_w() -> None:
    """Fixture W (private to the agent principal) is NOT eligible to the
    reviewer — even though the reviewer has review scope. This is the core
    governance invariant the audit's negative control relies on."""
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    tenant_id, reviewer_id = await _default_tenant_principal()
    agent_id = await _second_principal(tenant_id)
    marker = f"AUDIT-WRITE-{uuid.uuid4()}"
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=agent_id,
        content=f"the Engram write-audit marker is {marker}",
        visibility="private",
        review_status="proposed",
    )
    from sqlalchemy import select

    from engram.memory_access import eligibility_expression
    from engram.models import MemoryItem

    async with _test_session_factory() as session:
        visible = (
            await session.execute(
                select(MemoryItem).where(
                    MemoryItem.id == uuid.UUID(item_id),
                    MemoryItem.tenant_id == tenant_id,
                    eligibility_expression(reviewer_id),
                )
            )
        ).scalar_one_or_none()
    assert visible is None, "reviewer must NOT be able to read the agent's private Fixture W"


async def test_agent_can_read_own_private_fixture_w() -> None:
    """The author agent principal CAN read its own private Fixture W."""
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    tenant_id, _reviewer_id = await _default_tenant_principal()
    agent_id = await _second_principal(tenant_id)
    marker = f"AUDIT-WRITE-{uuid.uuid4()}"
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=agent_id,
        content=f"the Engram write-audit marker is {marker}",
        visibility="private",
        review_status="proposed",
    )
    from sqlalchemy import select

    from engram.memory_access import eligibility_expression
    from engram.models import MemoryItem

    async with _test_session_factory() as session:
        visible = (
            await session.execute(
                select(MemoryItem).where(
                    MemoryItem.id == uuid.UUID(item_id),
                    MemoryItem.tenant_id == tenant_id,
                    eligibility_expression(agent_id),
                )
            )
        ).scalar_one_or_none()
    assert visible is not None


# ── owner diagnostic mode performs no mutations ──────────────────────────────


async def test_owner_diagnostic_mode_performs_no_mutations() -> None:
    """The owner-diagnostics path the harness documents (read-only queries
    against the owner DSN) must not insert, update, or delete anything.

    We prove this by running a representative read-only diagnostic query
    inside a read-only transaction and asserting the item table row count
    is unchanged before and after."""
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    tenant_id, principal_id = await _default_tenant_principal()
    item_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=principal_id,
        content="owner diagnostic read-only probe",
        review_status="proposed",
    )
    tables = (
        "memory_items",
        "classification_runs",
        "item_events",
        "jobs",
        "feedback_events",
        "usage_events",
    )

    async def snapshot() -> dict[str, list[tuple[Any, ...]]]:
        async with _test_session_factory() as session:
            return {
                table: list(
                    (
                        await session.execute(
                            text(f"SELECT * FROM {table} WHERE tenant_id=:tenant_id ORDER BY id"),
                            {"tenant_id": tenant_id},
                        )
                    ).tuples()
                )
                for table in tables
            }

    before = await snapshot()
    from scripts.run_memory_e2e_audit import _owner_promotion_diagnostic

    owner_url = settings.owner_database_url or settings.database_url
    diagnostic = await _owner_promotion_diagnostic(owner_url, item_id)
    assert diagnostic["read_only"] is True
    assert await snapshot() == before

    async with _test_session_factory() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        with pytest.raises(DBAPIError):
            await session.execute(
                text("UPDATE memory_items SET importance=0.1 WHERE id=:id"), {"id": item_id}
            )
        await session.rollback()


# ── audit cleanup changes only exact recorded fixture ids ────────────────────


async def test_cleanup_changes_only_exact_recorded_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The harness cleanup archives ONLY exact recorded item ids, never a
    marker-wide fuzzy search. A decoy item with a similar marker is untouched."""
    if not await _db_ok():
        pytest.skip(_DB_SKIP_REASON)
    tenant_id, reviewer_id = await _default_tenant_principal()
    marker = f"AUDIT-RECALL-{uuid.uuid4()}"
    target_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=reviewer_id,
        content=f"The controlled Engram recall marker is {marker}.",
        visibility="tenant",
        review_status="active",
    )
    # Decoy: same marker fragment, different item, must NOT be archived.
    decoy_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=reviewer_id,
        content=f"unrelated but mentions {marker}",
        visibility="tenant",
        review_status="active",
    )
    epistemic_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=reviewer_id,
        content=f"epistemic {uuid.uuid4()}",
        visibility="tenant",
        review_status="active",
    )
    agent_id = await _second_principal(tenant_id)
    write_id = await _insert_item(
        tenant_id=tenant_id,
        principal_id=agent_id,
        content=f"private write {uuid.uuid4()}",
        visibility="private",
        review_status="active",
    )
    reviewer_key = await _issue_key(tenant_id, reviewer_id, ["read", "write", "review"])
    agent_key = await _issue_key(tenant_id, agent_id, ["read", "write"])
    settings.auth_enabled = True
    reset_principal_cache()
    import engram.db as db_module

    monkeypatch.setattr(db_module, "async_session_factory", _test_session_factory)
    monkeypatch.setattr(db_module, "owner_session_factory", _test_session_factory)
    monkeypatch.setattr(db_module, "read_session_factory", _test_session_factory)
    app = create_app()
    import scripts.run_memory_e2e_audit as audit_cli
    from engram.memory_audit import RunState

    def asgi_client(api: Any) -> AsyncClient:
        return AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"authorization": f"Bearer {api._key}", "accept": "application/json"},
        )
    monkeypatch.setattr(audit_cli.EngramAPI, "_client", asgi_client)
    cfg = audit_cli.AuditConfig()
    cfg.base_url, cfg.reviewer_key, cfg.agent_key = "http://test", reviewer_key, agent_key
    state = RunState(
        run_id=str(uuid.uuid4()), started_at=datetime.now(UTC), target_host="test"
    )
    state.fixture("recall").item_id = target_id
    state.fixture("epistemic").item_id = epistemic_id
    state.fixture("write").item_id = write_id
    await audit_cli.cmd_cleanup(state, cfg)

    async with _test_session_factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT id::text, review_status FROM memory_items "
                        "WHERE id IN (:a, :b, :e, :w)"
                    ),
                    {"a": target_id, "b": decoy_id, "e": epistemic_id, "w": write_id},
                )
            )
            .mappings()
            .all()
        )
    by_id = {r["id"]: r["review_status"] for r in rows}
    assert by_id[target_id] == "archived"
    assert by_id[epistemic_id] == "archived"
    assert by_id[decoy_id] == "active", "decoy must not be archived by fuzzy cleanup"
    assert by_id[write_id] == "active"
    assert write_id in state.stage("cleanup").evidence["skipped_ids"]
