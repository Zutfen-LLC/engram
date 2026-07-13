"""Embedding management for memory_items."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import ColumnElement, and_, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from engram.config import settings
from engram.embedding_profiles import LEGACY_MODEL
from engram.models import EmbeddingProfile, MemoryEmbedding, MemoryItem

log = logging.getLogger(__name__)

# Single source of truth for the embedding model name. The write path, the
# semantic-search read path, and conflict detection all key off this so an
# embedding written by one path is queryable by the others.
EMBEDDING_MODEL = LEGACY_MODEL

# Back-compat alias for existing ``from engram.embeddings import _EMBEDDING_MODEL``
# imports (memory routes, semantic.py, conflicts.py). New code should use
# :data:`EMBEDDING_MODEL`.
_EMBEDDING_MODEL = EMBEDDING_MODEL

# embedding_status vocabulary used by the live write path. The migration
# comment lists ``complete | failed | stale`` (and the column DEFAULT is
# ``'complete'``), but there is no CHECK constraint and every row written by
# the application uses the values below — ``pending`` while the vector is
# being generated, ``ready`` once populated, ``failed`` on error. Backfill
# follows the live vocabulary; search/recall filter on ``embedding IS NOT
# NULL`` so the status string does not gate retrieval.
STATUS_PENDING = "pending"
STATUS_READY = "ready"
STATUS_FAILED = "failed"


async def generate_embeddings(
    texts: list[str], profile: EmbeddingProfile | None = None
) -> list[list[float] | None]:
    """Generate embedding vectors for a batch of ``texts`` in one provider call.

    Returns one vector per input text, in input order. When the provider is
    ``none`` every entry is ``None``. Provider call errors propagate to the
    caller; the backfill batches so a single failed call only fails its batch.
    """
    provider = profile.provider if profile is not None else settings.embedding_provider
    model = profile.model if profile is not None else EMBEDDING_MODEL
    dimensions = profile.dimensions if profile is not None else settings.embedding_dim
    if provider == "none" or settings.embedding_provider == "none":
        return [None] * len(texts)
    if provider != "openai":
        raise ValueError(f"unsupported embedding provider: {provider!r}")

    try:
        from openai import AsyncOpenAI
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError(
            "openai package is required when ENGRAM_EMBEDDING_PROVIDER=openai"
        ) from exc

    client_kwargs: dict[str, Any] = {"api_key": settings.openai_api_key}
    if settings.openai_base_url:
        client_kwargs["base_url"] = settings.openai_base_url
    client = AsyncOpenAI(**client_kwargs)
    response = await client.embeddings.create(
        model=model,
        input=texts,
        dimensions=dimensions,
        # Explicitly request float format: the SDK defaults to "base64" which
        # some OpenAI-compatible providers (e.g. OpenRouter) do not support,
        # causing a silent "No embedding data received" error.
        encoding_format="float",
    )
    # The API returns exactly one embedding per input, in input order.
    vectors: list[list[float] | None] = [
        [float(value) for value in item.embedding] for item in response.data
    ]
    for vector in vectors:
        if vector is None:
            continue
        if len(vector) != dimensions:
            raise ValueError(
                f"embedding provider returned dimension {len(vector)}; expected {dimensions} "
                f"for profile {profile.profile_key if profile else model}"
            )
    return vectors


async def generate_embedding(
    text: str, profile: EmbeddingProfile | None = None
) -> list[float] | None:
    """Generate an embedding vector for ``text`` or return ``None`` when disabled."""
    return (await generate_embeddings([text], profile))[0]


async def create_embedding_placeholder(
    session: AsyncSession,
    memory_item_id: uuid.UUID,
    tenant_id: uuid.UUID,
    profile: EmbeddingProfile | None = None,
) -> MemoryEmbedding:
    """Insert a pending memory_embeddings row to be updated once the vector is ready."""
    if profile is None:
        from engram.embedding_profiles import get_active_profile

        profile = await get_active_profile(session)
    placeholder = MemoryEmbedding(
        memory_item_id=memory_item_id,
        tenant_id=tenant_id,
        profile_id=profile.id,
        embedding_model=profile.model,
        embedding_dim=profile.dimensions,
        embedding=None,
        embedding_status=STATUS_PENDING,
    )
    session.add(placeholder)
    return placeholder


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------

# Default chunk size for batched embedding generation. The provider call is
# the latency-dominant step; batching lets a failed row abort only its batch
# and bounds the size of one transaction.
DEFAULT_BACKFILL_BATCH_SIZE = 100

# Upper bound on ``batch_size``. The OpenAI embeddings endpoint accepts at most
# 2048 input strings per request, so a larger batch fails every call. The CLI
# validates against this so an oversized ``--batch-size`` fails loudly with a
# clear message rather than as a per-row provider error.
MAX_PROVIDER_BATCH_SIZE = 2048

# Guidance returned to the caller when a real (non-dry-run) backfill is run
# with the provider disabled, so they get an actionable next step rather than
# a per-row failure cascade.
_PROVIDER_DISABLED_MESSAGE = (
    "embedding provider is 'none' — set ENGRAM_EMBEDDING_PROVIDER and the "
    "provider's API key before running backfill; new /remember calls create "
    "pending rows, this command populates them"
)

# Exit code the CLI returns when a real backfill is a no-op because the
# provider is disabled. Signals "you asked for writes, none happened."
EXIT_PROVIDER_DISABLED = 2


@dataclass
class BackfillResult:
    """Summary of one backfill invocation for a single tenant.

    The "would_*" counters describe *planned* work (populated in both dry-run
    and real runs, never mutated by the run). The "created"/"populated"
    counters describe *performed* work (always 0 in a dry-run). Keeping them
    separate means a dry-run report never looks like a partial write.

    Attributes:
        tenant_id: tenant the run covered (empty when no RLS context).
        model: configured embedding model the run targeted.
        provider_enabled: whether ``settings.embedding_provider != "none"``.
        dry_run: whether this run was a dry-run (no writes).
        message: human-readable status/guidance for the caller (may be None).
        scanned: rows needing work (pending rows + items missing a row for
            the configured model, plus any failed rows included via
            ``retry_failed``).
        would_create: missing-row items that would gain an embedding row.
        would_populate: existing pending/failed rows that would be embedded.
        created: missing-row embedding rows actually inserted (0 in dry-run).
        populated: existing rows actually embedded (0 in dry-run).
        skipped_ready: rows with a populated vector, regardless of status
            string (a populated vector is effectively ready; left untouched).
        skipped_failed: ``failed`` rows skipped because ``retry_failed`` is
            False (left untouched).
        failed: rows that errored or returned no vector this run.
        failed_items: item_ids whose embedding failed this run.
        batch_size: batch size used.
    """

    tenant_id: str
    model: str
    provider_enabled: bool
    dry_run: bool
    message: str | None = None
    scanned: int = 0
    would_create: int = 0
    would_populate: int = 0
    created: int = 0
    populated: int = 0
    skipped_ready: int = 0
    skipped_failed: int = 0
    failed: int = 0
    failed_items: list[str] = field(default_factory=list)
    batch_size: int = DEFAULT_BACKFILL_BATCH_SIZE


def summarize_backfill(result: BackfillResult) -> str:
    """Human-readable single-tenant summary for CLI output / endpoint response."""
    prefix = (
        f"tenant={result.tenant_id} "
        f"model={result.model} "
        f"provider={'enabled' if result.provider_enabled else 'disabled'} "
        f"dry_run={'true' if result.dry_run else 'false'} "
        f"batch_size={result.batch_size}"
    )
    if result.dry_run:
        detail = (
            f"scanned={result.scanned} "
            f"would_create={result.would_create} "
            f"would_populate={result.would_populate} "
            f"skipped_ready={result.skipped_ready} "
            f"skipped_failed={result.skipped_failed}"
        )
    else:
        detail = (
            f"scanned={result.scanned} "
            f"created={result.created} "
            f"populated={result.populated} "
            f"skipped_ready={result.skipped_ready} "
            f"skipped_failed={result.skipped_failed} "
            f"failed={result.failed}"
        )
    line = f"{prefix} {detail}"
    if result.message:
        line += f" msg={result.message!r}"
    return line


async def backfill_embeddings(
    session: AsyncSession,
    tenant_id: str | None = None,
    *,
    dry_run: bool = False,
    batch_size: int = DEFAULT_BACKFILL_BATCH_SIZE,
    limit: int | None = None,
    fail_fast: bool = False,
    retry_failed: bool = False,
) -> BackfillResult:
    """Backfill pending/missing embeddings for a tenant.

    Covers two populations of work (both required by BL-006):

    1. Existing ``memory_embeddings`` rows for the configured model that are
       not ``ready`` (or whose ``embedding`` is NULL). By default only
       ``pending`` rows are processed; ``failed`` rows are skipped unless
       ``retry_failed=True`` (so a broken provider/config doesn't create an
       endless failure loop).
    2. ``memory_items`` (``valid_to IS NULL``) with no embedding row for the
       configured model — a row is created then embedded.

    The function is idempotent: any row with a populated vector is counted as
    ``skipped_ready`` and never touched, so a repeat run reports nothing left
    to do.

    ``limit`` is a single shared budget across both populations, applied
    pending-first; at most ``limit`` candidates are processed in total (``None``
    means no cap). ``batch_size`` is the number of items embedded in one
    provider call and one transaction: embedding happens in real batches (a
    single call to the provider covers the whole batch), each batch is flushed
    and committed as it completes, and a failed provider call only fails its
    own batch (use ``batch_size=1`` for per-item isolation). Candidates are
    streamed one batch at a time (keyset pagination) rather than loaded up
    front, so memory and per-batch transaction size both stay bounded by
    ``batch_size`` even when ``limit`` is unset. Summary counts come from cheap
    ``count(*)`` queries, so a dry-run never materializes the candidate rows.

    Content embedded here already passed /remember's ``has_secrets()`` check on
    the way in, so backfill does not re-run secret detection.

    Concurrency: pending rows are fetched ``FOR UPDATE SKIP LOCKED`` per batch
    and missing-row inserts tolerate the unique constraint, so overlapping runs
    divide work instead of double-embedding or crashing. (The function commits
    as it goes, so callers — CLI / future admin endpoint — don't manage
    transaction boundaries.)

    When ``settings.embedding_provider == 'none'``:

    - ``dry_run=True``: still scans and reports candidates (no writes).
    - ``dry_run=False``: writes nothing and returns a result with guidance in
      ``message`` for the caller to act on.

    Tenant safety: new embedding rows read ``tenant_id`` from the parent
    memory_item, satisfying the composite FK ``(memory_item_id, tenant_id) →
    memory_items(id, tenant_id)``. Every query filters ``tenant_id``
    explicitly, so results are correct under RLS too.
    """
    provider_enabled = settings.embedding_provider != "none"

    if tenant_id is None:
        # Resolve tenant from RLS session context the same way the promotion
        # service does; treat missing context as empty rather than raising.
        tid = (
            await session.execute(text("SELECT current_setting('app.tenant_id', true)::text"))
        ).scalar_one_or_none()
        if not tid:
            return BackfillResult(
                tenant_id="",
                model=EMBEDDING_MODEL,
                provider_enabled=provider_enabled,
                dry_run=dry_run,
                message="no app.tenant_id context set on the session",
            )
        tenant_id = tid

    # Cheap count(*) queries drive the summary (and dry-run reporting); the
    # actual rows are streamed one batch at a time below. ``limit`` is a shared
    # budget across both populations, applied pending-first.
    would_populate, would_create = await _count_work(
        session, tenant_id, limit=limit, retry_failed=retry_failed
    )
    skipped_ready, skipped_failed = await _count_skipped(
        session, tenant_id, retry_failed=retry_failed
    )

    result = BackfillResult(
        tenant_id=tenant_id,
        model=EMBEDDING_MODEL,
        provider_enabled=provider_enabled,
        dry_run=dry_run,
        skipped_ready=skipped_ready,
        skipped_failed=skipped_failed,
        batch_size=batch_size,
        would_populate=would_populate,
        would_create=would_create,
    )
    result.scanned = result.would_populate + result.would_create

    if dry_run:
        # Dry-run reports planned work for both provider states without writing
        # any rows (the commit only ends the read transaction).
        result.message = (
            "dry-run: no rows written" if provider_enabled else _PROVIDER_DISABLED_MESSAGE
        )
        await session.commit()
        return result

    if not provider_enabled:
        # Real run with provider disabled: writes nothing, actionable guidance.
        result.message = _PROVIDER_DISABLED_MESSAGE
        await session.commit()
        return result

    # ---- Real run with provider enabled: streamed, batched embedding ------------
    eff_batch = max(1, batch_size)
    await _stream_pending(
        session,
        tenant_id,
        batch_size=eff_batch,
        max_rows=would_populate,
        retry_failed=retry_failed,
        result=result,
        fail_fast=fail_fast,
    )
    await _stream_missing(
        session,
        tenant_id,
        batch_size=eff_batch,
        max_rows=would_create,
        result=result,
        fail_fast=fail_fast,
    )
    await session.commit()
    return result


def _pending_work_filter(tenant_id: str, pending_statuses: list[str]) -> ColumnElement[bool]:
    """WHERE clause selecting existing embedding rows that still need a vector.

    Targets the configured model and is one of: a pending (optionally failed)
    status, a ``ready`` row missing its vector (anomaly), or any other status
    (e.g. the migration-default ``'complete'``) missing its vector.
    """
    return and_(
        MemoryEmbedding.tenant_id == tenant_id,
        MemoryEmbedding.embedding_model == EMBEDDING_MODEL,
        or_(
            MemoryEmbedding.embedding_status.in_(pending_statuses),
            and_(
                MemoryEmbedding.embedding_status == STATUS_READY,
                MemoryEmbedding.embedding.is_(None),
            ),
            and_(
                MemoryEmbedding.embedding_status.not_in(
                    [STATUS_READY, STATUS_FAILED, STATUS_PENDING]
                ),
                MemoryEmbedding.embedding.is_(None),
            ),
        ),
    )


def _missing_work_filter(tenant_id: str) -> ColumnElement[bool]:
    """WHERE clause selecting live memory_items with no embedding row for the model."""
    has_embedding_subq = select(MemoryEmbedding.memory_item_id).where(
        MemoryEmbedding.tenant_id == tenant_id,
        MemoryEmbedding.embedding_model == EMBEDDING_MODEL,
    )
    return and_(
        MemoryItem.tenant_id == tenant_id,
        MemoryItem.valid_to.is_(None),
        MemoryItem.id.not_in(has_embedding_subq),
    )


async def _count_work(
    session: AsyncSession,
    tenant_id: str,
    *,
    limit: int | None,
    retry_failed: bool,
) -> tuple[int, int]:
    """Return ``(would_populate, would_create)`` via cheap ``count(*)`` queries.

    ``limit`` is a shared budget across both populations, applied pending-first:
    the missing population only gets whatever budget the pending population
    doesn't consume. Counts (not row loads) so the summary / dry-run stays cheap.
    """
    pending_statuses = [STATUS_PENDING]
    if retry_failed:
        pending_statuses.append(STATUS_FAILED)

    pending_count = int(
        (
            await session.execute(
                select(func.count(MemoryEmbedding.id)).where(
                    _pending_work_filter(tenant_id, pending_statuses)
                )
            )
        ).scalar_one()
    )
    pending_budget = pending_count if limit is None else min(pending_count, limit)

    remaining: int | None = None
    if limit is not None:
        remaining = max(0, limit - pending_budget)
        if remaining == 0:
            return pending_budget, 0

    missing_count = int(
        (
            await session.execute(
                select(func.count(MemoryItem.id)).where(_missing_work_filter(tenant_id))
            )
        ).scalar_one()
    )
    missing_budget = missing_count if remaining is None else min(missing_count, remaining)
    return pending_budget, missing_budget


async def _stream_pending(
    session: AsyncSession,
    tenant_id: str,
    *,
    batch_size: int,
    max_rows: int,
    retry_failed: bool,
    result: BackfillResult,
    fail_fast: bool,
) -> None:
    """Keyset-paginate pending rows, embedding ``batch_size`` at a time.

    Rows are fetched ``FOR UPDATE SKIP LOCKED`` so a concurrent backfill skips
    rows this run is processing. Each batch is its own transaction (commit per
    batch). Keyset (``embedded_at, id``) ordering guarantees single-pass
    progress even when a row fails and stays in the candidate set
    (``retry_failed``), unlike a re-query-until-empty loop.
    """
    if max_rows == 0:
        return
    pending_statuses = [STATUS_PENDING]
    if retry_failed:
        pending_statuses.append(STATUS_FAILED)

    processed = 0
    last_key: tuple[Any, uuid.UUID] | None = None  # (embedded_at, id)
    while processed < max_rows:
        take = min(batch_size, max_rows - processed)
        stmt = (
            select(MemoryEmbedding)
            .where(_pending_work_filter(tenant_id, pending_statuses))
            # Oldest rows first; embedded_at is set at insert and never updated
            # on backfill, so this is effectively "row created" ordering.
            .order_by(MemoryEmbedding.embedded_at.asc(), MemoryEmbedding.id.asc())
            .limit(take)
            .with_for_update(skip_locked=True)
        )
        if last_key is not None:
            last_ts, last_id = last_key
            stmt = stmt.where(
                or_(
                    MemoryEmbedding.embedded_at > last_ts,
                    and_(
                        MemoryEmbedding.embedded_at == last_ts,
                        MemoryEmbedding.id > last_id,
                    ),
                )
            )
        rows = list((await session.execute(stmt)).scalars().all())
        if not rows:
            break
        # Capture the keyset cursor from fetched (pre-mutation) rows: embedded_at
        # and id are never changed by embedding, and reading them before commit
        # avoids any expire_on_commit lazy-load.
        last_key = (rows[-1].embedded_at, rows[-1].id)
        await _embed_batch(session, rows, result, fail_fast=fail_fast, kind="populate")
        await session.commit()
        # Release processed rows (each now carries its ~1536-dim vector) from the
        # session identity map so memory stays bounded by batch_size, not by the
        # total number of rows embedded this run. The keyset cursor was already
        # captured above, so the rows aren't referenced again.
        session.expunge_all()
        processed += len(rows)


async def _stream_missing(
    session: AsyncSession,
    tenant_id: str,
    *,
    batch_size: int,
    max_rows: int,
    result: BackfillResult,
    fail_fast: bool,
) -> None:
    """Keyset-paginate items missing a row, creating+embedding ``batch_size`` at a time.

    Per batch: build pending rows via :func:`create_embedding_placeholder`,
    flush (INSERT), embed, commit. A flush that hits the unique constraint
    (a concurrent run already created the same row) rolls the batch back and
    skips it — those items are embedded by the other run / a future run.
    """
    if max_rows == 0:
        return
    processed = 0
    last_key: tuple[Any, uuid.UUID] | None = None  # (created_at, id)
    while processed < max_rows:
        take = min(batch_size, max_rows - processed)
        stmt = (
            select(MemoryItem.id, MemoryItem.tenant_id, MemoryItem.created_at)
            .where(_missing_work_filter(tenant_id))
            .order_by(MemoryItem.created_at.asc(), MemoryItem.id.asc())
            .limit(take)
        )
        if last_key is not None:
            last_ts, last_id = last_key
            stmt = stmt.where(
                or_(
                    MemoryItem.created_at > last_ts,
                    and_(MemoryItem.created_at == last_ts, MemoryItem.id > last_id),
                )
            )
        page = (await session.execute(stmt)).all()
        if not page:
            break
        last_key = (page[-1].created_at, page[-1].id)

        new_rows = []
        for row in page:
            new_rows.append(await create_embedding_placeholder(session, row.id, row.tenant_id))
        try:
            await session.flush()  # INSERT the batch; populates ids
        except IntegrityError:
            # A concurrent run created some of these rows already. Roll back and
            # skip the batch; the cursor already advanced past these items.
            await session.rollback()
            processed += len(page)
            continue
        await _embed_batch(session, new_rows, result, fail_fast=fail_fast, kind="create")
        await session.commit()
        # Drop the now-embedded rows from the identity map to bound memory
        # (see _stream_pending). Rollback above already detaches conflicting rows.
        session.expunge_all()
        processed += len(page)


async def _count_skipped(
    session: AsyncSession, tenant_id: str, *, retry_failed: bool
) -> tuple[int, int]:
    """Return ``(ready_count, failed_skipped_count)`` for the summary.

    ``ready_count`` counts any row with a populated vector regardless of status
    string: retrieval gates on ``embedding IS NOT NULL``, so a row carrying
    the migration-default ``'complete'`` plus a vector is effectively ready and
    is accounted for here rather than left invisible. Counts use ``count(*)``
    so very large backlogs don't stream every id into Python.
    """
    ready_stmt = select(func.count(MemoryEmbedding.id)).where(
        MemoryEmbedding.tenant_id == tenant_id,
        MemoryEmbedding.embedding_model == EMBEDDING_MODEL,
        MemoryEmbedding.embedding.is_not(None),
    )
    ready_count = int((await session.execute(ready_stmt)).scalar_one())

    if retry_failed:
        return ready_count, 0
    failed_stmt = select(func.count(MemoryEmbedding.id)).where(
        MemoryEmbedding.tenant_id == tenant_id,
        MemoryEmbedding.embedding_model == EMBEDDING_MODEL,
        MemoryEmbedding.embedding_status == STATUS_FAILED,
    )
    failed_count = int((await session.execute(failed_stmt)).scalar_one())
    return ready_count, failed_count


def _mark_failed(emb: MemoryEmbedding, result: BackfillResult) -> None:
    """Record one row as failed this run (status + counters). Single source for
    failure accounting so the status mark and the counters stay in sync."""
    emb.embedding_status = STATUS_FAILED
    result.failed += 1
    result.failed_items.append(str(emb.memory_item_id))


async def _embed_batch(
    session: AsyncSession,
    rows: list[MemoryEmbedding],
    result: BackfillResult,
    *,
    fail_fast: bool,
    kind: Literal["populate", "create"],
) -> None:
    """Embed one batch of rows in a single provider call and update statuses.

    Parent content for the whole batch is fetched in one query; rows whose
    parent item vanished mid-run are marked ``failed`` (or re-raised on
    ``fail_fast``). A provider error — or a malformed response (wrong number of
    vectors) — fails every row in the batch via :func:`_mark_failed` so
    ``batch_size`` bounds the blast radius, unless ``fail_fast`` re-raises
    first. ``kind`` selects which result counter (``populated``/``created``) a
    successful row increments.
    """
    item_ids = [emb.memory_item_id for emb in rows]
    content_by_id = {
        row.id: row.content
        for row in (
            await session.execute(
                select(MemoryItem.id, MemoryItem.content).where(MemoryItem.id.in_(item_ids))
            )
        )
    }

    to_embed: list[tuple[MemoryEmbedding, str]] = []
    for emb in rows:
        content = content_by_id.get(emb.memory_item_id)
        if content is None:
            # Parent item vanished mid-run (deleted, RLS). Record and move on.
            if fail_fast:
                raise RuntimeError(f"memory_item {emb.memory_item_id} not found during backfill")
            _mark_failed(emb, result)
        else:
            to_embed.append((emb, content))

    if not to_embed:
        return

    texts = [content for _, content in to_embed]
    try:
        vectors = await generate_embeddings(texts)
    except Exception as exc:  # noqa: BLE001 - provider errors are varied
        if fail_fast:
            raise
        log.warning("backfill: embedding failed for %d item(s): %s", len(to_embed), exc)
        for emb, _content in to_embed:
            _mark_failed(emb, result)
        return

    # A length mismatch is a provider contract violation; treat it as a
    # batch-level failure (no partial/inconsistent accounting) rather than
    # letting a strict-zip ValueError escape the per-batch boundary.
    if len(vectors) != len(to_embed):
        msg = f"provider returned {len(vectors)} vectors for {len(to_embed)} inputs"
        if fail_fast:
            raise RuntimeError(msg)
        log.warning("backfill: %s", msg)
        for emb, _content in to_embed:
            _mark_failed(emb, result)
        return

    for (emb, _content), vec in zip(to_embed, vectors, strict=True):
        if vec is None:
            # Provider returned nothing for this item.
            if fail_fast:
                raise RuntimeError(
                    f"embedding provider returned no vector for item {emb.memory_item_id}"
                )
            _mark_failed(emb, result)
            continue
        emb.embedding = vec
        emb.embedding_dim = len(vec)
        emb.embedding_status = STATUS_READY
        if kind == "create":
            result.created += 1
        else:
            result.populated += 1


__all__ = [
    "DEFAULT_BACKFILL_BATCH_SIZE",
    "EMBEDDING_MODEL",
    "EXIT_PROVIDER_DISABLED",
    "MAX_PROVIDER_BATCH_SIZE",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "STATUS_READY",
    "BackfillResult",
    "backfill_embeddings",
    "create_embedding_placeholder",
    "generate_embedding",
    "generate_embeddings",
    "summarize_backfill",
]
