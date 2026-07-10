"""Embedding profile registry, lifecycle, coverage, and index management."""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, func, select, text, true, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from engram.models import EmbeddingProfile, MemoryEmbedding, MemoryItem

LEGACY_PROVIDER = "openai"
LEGACY_MODEL = "text-embedding-3-small"
LEGACY_DIMENSIONS = 1536
MAX_WRITABLE_PROFILES = 3
MAX_INDEX_DIMENSIONS = 2000  # pgvector HNSW vector operator-class limit


def make_profile_key(provider: str, model: str, dimensions: int) -> str:
    return f"{provider}:{model}:{dimensions}"


async def get_profile(session: AsyncSession, profile_key: str) -> EmbeddingProfile:
    profile = (
        await session.execute(
            select(EmbeddingProfile).where(EmbeddingProfile.profile_key == profile_key)
        )
    ).scalar_one_or_none()
    if profile is None:
        raise LookupError(f"embedding profile {profile_key!r} not found")
    return profile


async def get_profile_by_id(session: AsyncSession, profile_id: uuid.UUID | str) -> EmbeddingProfile:
    profile = await session.get(EmbeddingProfile, uuid.UUID(str(profile_id)))
    if profile is None:
        raise LookupError(f"embedding profile {profile_id!s} not found")
    return profile


async def get_active_profile(session: AsyncSession) -> EmbeddingProfile:
    profile = (
        await session.execute(select(EmbeddingProfile).where(EmbeddingProfile.state == "active"))
    ).scalar_one_or_none()
    if profile is None:
        raise RuntimeError("no active embedding profile is configured")
    return profile


async def get_writable_profiles(session: AsyncSession) -> list[EmbeddingProfile]:
    profiles = list(
        (
            await session.execute(
                select(EmbeddingProfile)
                .where(EmbeddingProfile.state.in_(("active", "candidate")))
                .order_by(
                    case((EmbeddingProfile.state == "active", 0), else_=1),
                    EmbeddingProfile.created_at,
                )
            )
        ).scalars()
    )
    if len(profiles) > MAX_WRITABLE_PROFILES:
        raise RuntimeError(
            f"{len(profiles)} writable embedding profiles exceeds maximum {MAX_WRITABLE_PROFILES}"
        )
    return profiles


def validate_profile(profile: EmbeddingProfile) -> None:
    if profile.dimensions <= 0:
        raise ValueError("embedding profile dimensions must be positive")
    if profile.distance_metric != "cosine":
        raise ValueError("only cosine embedding profiles are supported")
    if profile.provider != "openai":
        raise ValueError(f"unsupported embedding provider: {profile.provider!r}")


@dataclass(frozen=True)
class ProfileCoverage:
    total_eligible: int
    ready: int
    pending: int
    failed: int
    missing: int

    @property
    def percentage(self) -> float:
        if self.total_eligible == 0:
            return 100.0
        return 100.0 * self.ready / self.total_eligible


@dataclass(frozen=True)
class ProfileBackfillResult:
    eligible: int
    already_ready: int
    pending: int
    failed: int
    enqueued: int
    skipped_expired_rejected: int


async def calculate_coverage(
    session: AsyncSession, profile: EmbeddingProfile, *, tenant_id: str | None = None
) -> ProfileCoverage:
    eligible = [MemoryItem.valid_to.is_(None), MemoryItem.review_status != "rejected"]
    if tenant_id is not None:
        eligible.append(MemoryItem.tenant_id == tenant_id)
    total = int(
        (
            await session.execute(select(func.count()).select_from(MemoryItem).where(*eligible))
        ).scalar_one()
    )

    counts_stmt = (
        select(MemoryEmbedding.embedding_status, func.count())
        .join(MemoryItem, MemoryItem.id == MemoryEmbedding.memory_item_id)
        .where(MemoryEmbedding.profile_id == profile.id, *eligible)
        .group_by(MemoryEmbedding.embedding_status)
    )
    counts = {
        str(status): int(count) for status, count in (await session.execute(counts_stmt)).all()
    }
    ready = counts.get("ready", 0)
    pending = counts.get("pending", 0)
    failed = counts.get("failed", 0)
    return ProfileCoverage(total, ready, pending, failed, max(0, total - sum(counts.values())))


async def enqueue_profile_backfill(
    session: AsyncSession,
    profile: EmbeddingProfile,
    *,
    tenant_id: str | None = None,
    limit: int | None = None,
    force: bool = False,
) -> ProfileBackfillResult:
    """Create placeholders and durable jobs for a profile; never calls a provider."""
    from engram.embeddings import STATUS_FAILED, STATUS_PENDING, STATUS_READY
    from engram.jobs import enqueue_job

    eligible_filter = [MemoryItem.valid_to.is_(None), MemoryItem.review_status != "rejected"]
    tenant_filter: list[Any] = []
    if tenant_id is not None:
        tenant_filter.append(MemoryItem.tenant_id == tenant_id)
    eligible = int(
        (
            await session.execute(
                select(func.count()).select_from(MemoryItem).where(*eligible_filter, *tenant_filter)
            )
        ).scalar_one()
    )
    skipped = int(
        (
            await session.execute(
                select(func.count())
                .select_from(MemoryItem)
                .where(
                    ~(MemoryItem.valid_to.is_(None) & (MemoryItem.review_status != "rejected")),
                    *tenant_filter,
                )
            )
        ).scalar_one()
    )

    counts = {
        str(status): int(count)
        for status, count in (
            await session.execute(
                select(MemoryEmbedding.embedding_status, func.count())
                .join(MemoryItem, MemoryItem.id == MemoryEmbedding.memory_item_id)
                .where(MemoryEmbedding.profile_id == profile.id, *tenant_filter)
                .group_by(MemoryEmbedding.embedding_status)
            )
        ).all()
    }

    existing = select(MemoryEmbedding.memory_item_id).where(
        MemoryEmbedding.profile_id == profile.id,
        MemoryEmbedding.memory_item_id == MemoryItem.id,
    )
    work_filter: ColumnElement[bool]
    if force:
        work_filter = true()
    else:
        work_filter = (
            ~existing.exists()
            | select(MemoryEmbedding.id)
            .where(
                MemoryEmbedding.profile_id == profile.id,
                MemoryEmbedding.memory_item_id == MemoryItem.id,
                MemoryEmbedding.embedding_status == STATUS_FAILED,
            )
            .exists()
        )
    stmt = (
        select(MemoryItem)
        .where(*eligible_filter, *tenant_filter, work_filter)
        .order_by(MemoryItem.created_at, MemoryItem.id)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    items = list((await session.execute(stmt)).scalars())
    enqueued = 0
    for item in items:
        emb = (
            await session.execute(
                select(MemoryEmbedding).where(
                    MemoryEmbedding.memory_item_id == item.id,
                    MemoryEmbedding.profile_id == profile.id,
                )
            )
        ).scalar_one_or_none()
        if emb is None:
            emb = MemoryEmbedding(
                memory_item_id=item.id,
                tenant_id=item.tenant_id,
                profile_id=profile.id,
                embedding_model=profile.model,
                embedding_dim=profile.dimensions,
                embedding_status=STATUS_PENDING,
                embedding=None,
            )
            session.add(emb)
            await session.flush()
        else:
            emb.embedding = None
            emb.embedding_status = STATUS_PENDING
            emb.embedding_model = profile.model
            emb.embedding_dim = profile.dimensions
            await session.flush()
        await enqueue_job(
            session,
            tenant_id=item.tenant_id,
            job_type="embedding.generate",
            payload={
                "memory_item_id": str(item.id),
                "profile_id": str(profile.id),
                "profile_key": profile.profile_key,
            },
            dedupe_key=f"embedding.generate:{item.id}:{profile.id}",
        )
        enqueued += 1
    return ProfileBackfillResult(
        eligible=eligible,
        already_ready=counts.get(STATUS_READY, 0),
        pending=counts.get(STATUS_PENDING, 0),
        failed=counts.get(STATUS_FAILED, 0),
        enqueued=enqueued,
        skipped_expired_rejected=skipped,
    )


def profile_index_name(profile: EmbeddingProfile) -> str:
    digest = hashlib.sha256(str(profile.id).encode()).hexdigest()[:16]
    return f"idx_emb_profile_{digest}"


def profile_index_sql(profile: EmbeddingProfile, *, concurrently: bool = True) -> str:
    validate_profile(profile)
    if profile.dimensions > MAX_INDEX_DIMENSIONS:
        raise ValueError(
            f"dimension {profile.dimensions} is not indexable with pgvector's vector "
            f"HNSW operator class (maximum {MAX_INDEX_DIMENSIONS}); consider a supported "
            "dimension or an explicit halfvec migration"
        )
    name = profile_index_name(profile)
    if re.fullmatch(r"idx_emb_profile_[0-9a-f]{16}", name) is None:
        raise ValueError("unsafe generated embedding index name")
    dimensions = int(profile.dimensions)
    profile_id = str(profile.id)
    concurrent = " CONCURRENTLY" if concurrently else ""
    return (
        f"CREATE INDEX{concurrent} IF NOT EXISTS {name} ON memory_embeddings "
        f"USING hnsw ((embedding::vector({dimensions})) vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64) "
        f"WHERE profile_id = '{profile_id}'::uuid AND embedding_dim = {dimensions} "
        "AND embedding_status = 'ready'"
    )


async def ensure_profile_index(engine: AsyncEngine, profile_id: uuid.UUID) -> str:
    """Create a profile index in autocommit mode and persist lifecycle state."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        profile = await get_profile_by_id(session, profile_id)
        sql = profile_index_sql(profile)
        name = profile_index_name(profile)
        profile.index_status = "creating"
        profile.index_name = name
        await session.commit()
    try:
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text(sql))
    except Exception:
        async with factory() as session:
            await session.execute(
                update(EmbeddingProfile)
                .where(EmbeddingProfile.id == profile_id)
                .values(index_status="failed")
            )
            await session.commit()
        raise
    async with factory() as session:
        await session.execute(
            update(EmbeddingProfile)
            .where(EmbeddingProfile.id == profile_id)
            .values(index_status="ready", index_name=name)
        )
        await session.commit()
    return name


async def activate_profile(
    session: AsyncSession,
    profile: EmbeddingProfile,
    *,
    threshold: float = 95.0,
    force: bool = False,
) -> ProfileCoverage:
    if profile.state not in {"candidate", "retired"}:
        raise ValueError("only candidate or retired profiles may be activated")
    if profile.index_status != "ready" or not profile.index_name:
        raise ValueError("profile index is not ready")
    coverage = await calculate_coverage(session, profile)
    inconsistent = int(
        (
            await session.execute(
                select(func.count())
                .select_from(MemoryEmbedding)
                .where(
                    MemoryEmbedding.profile_id == profile.id,
                    (MemoryEmbedding.embedding_model != profile.model)
                    | (MemoryEmbedding.embedding_dim != profile.dimensions),
                )
            )
        ).scalar_one()
    )
    if inconsistent:
        raise ValueError(f"profile has {inconsistent} inconsistent embedding row(s)")
    if coverage.percentage < threshold and not force:
        raise ValueError(
            f"profile coverage {coverage.percentage:.2f}% is below required {threshold:.2f}%"
        )
    now = datetime.now(UTC)
    await session.execute(
        update(EmbeddingProfile)
        .where(EmbeddingProfile.state == "active")
        .values(state="retired", retired_at=now)
    )
    profile.state = "active"
    profile.activated_at = now
    profile.retired_at = None
    await session.commit()
    return coverage


async def retire_profile(session: AsyncSession, profile: EmbeddingProfile) -> None:
    if profile.state == "active":
        raise ValueError("cannot retire the active profile without activating a replacement")
    profile.state = "retired"
    profile.retired_at = datetime.now(UTC)
    await session.commit()


__all__ = [
    "LEGACY_DIMENSIONS",
    "LEGACY_MODEL",
    "LEGACY_PROVIDER",
    "MAX_WRITABLE_PROFILES",
    "ProfileBackfillResult",
    "ProfileCoverage",
    "activate_profile",
    "calculate_coverage",
    "enqueue_profile_backfill",
    "ensure_profile_index",
    "get_active_profile",
    "get_profile",
    "get_profile_by_id",
    "get_writable_profiles",
    "make_profile_key",
    "profile_index_name",
    "profile_index_sql",
    "retire_profile",
    "validate_profile",
]
