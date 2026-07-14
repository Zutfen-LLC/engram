"""Postgres-backed job queue (ENG-AUD-008).

A small, durable, Postgres-only job queue for moving expensive write-path work
off the request path: embedding generation, LLM classification refinement,
embedding-dependent conflict detection, and promotion sweeps.

The queue is intentionally minimal — no Redis/Celery/SQS, no distributed worker
orchestration, no scheduler daemon. Workers claim jobs with
``FOR UPDATE SKIP LOCKED``, which is safe under concurrency. Failures retry with
exponential backoff and go ``dead`` after ``max_attempts``. Stale ``running``
jobs (worker crash) are reclaimable after a lease timeout.

RLS posture (ENG-AUD-002): the ``jobs`` table is tenant-scoped and
FORCE-RLS-protected. These helpers are *queue primitives* and run through
whatever session the caller supplies. The worker (see ``engram/worker.py``)
runs claim/lock/retry/dead bookkeeping through the **owner** session (cross-
tenant queue coordination, which bypasses RLS) and runs each job's payload
through a fresh **app-role** session scoped to the job's tenant. Enqueue from
the request path (``/v1/remember``) runs under the request's app-role tenant
context, which the WITH CHECK policy permits for that tenant only.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from engram.models import Job

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Status vocabulary (matches the CHECK constraint in migrations/005_jobs.sql)
# ---------------------------------------------------------------------------
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_DEAD = "dead"
STATUS_CANCELLED = "cancelled"

DEFAULT_MAX_ATTEMPTS = 5
# Exponential backoff base, in seconds. Retry N waits base * 2**(attempts-1):
# 30s, 60s, 120s, 240s, ... Capped by max_attempts (dead-letter) rather than a
# hard ceiling, so a permanently-failing job stops being retried quickly.
_RETRY_BACKOFF_BASE_SECONDS = 30
# Last error is truncated before being stored, so a provider stack trace cannot
# bloat the row.
_MAX_ERROR_LEN = 4000


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _backoff(attempts: int) -> timedelta:
    """Exponential backoff for the ``attempts``-th retry (1-indexed)."""
    # attempts has already been incremented by claim_next_job; the first failure
    # (attempts == 1) waits one base interval.
    seconds = _RETRY_BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1))
    return timedelta(seconds=seconds)


def _truncate(value: str) -> str:
    return value if len(value) <= _MAX_ERROR_LEN else value[:_MAX_ERROR_LEN]


async def enqueue_job(
    session: AsyncSession,
    *,
    tenant_id: UUID | str,
    job_type: str,
    payload: dict[str, object],
    priority: int = 100,
    run_after: datetime | None = None,
    dedupe_key: str | None = None,
    max_attempts: int | None = None,
) -> UUID:
    """Insert a job row and commit.

    When ``dedupe_key`` is provided it is embedded in ``payload["dedupe_key"]``
    so the partial unique index ``idx_jobs_dedupe`` enforces at most one
    pending/running job per ``(tenant_id, job_type, dedupe_key)``. A duplicate
    enqueue returns the id of the existing pending/running job (idempotent)
    instead of raising.

    ``max_attempts`` defaults to ``settings.job_max_attempts`` (which itself
    defaults to ``DEFAULT_MAX_ATTEMPTS`` = 5). An explicit per-job value
    overrides the deployment-level setting.

    Runs under the caller's session/transaction context. On the request path
    that is the app-role session for the request's tenant, which the jobs
    WITH CHECK policy permits.
    """
    from engram.config import settings

    effective_max_attempts = (
        settings.job_max_attempts if max_attempts is None else max_attempts
    )
    final_payload: dict[str, object] = dict(payload)
    if dedupe_key is not None:
        final_payload["dedupe_key"] = dedupe_key

    job = Job(
        tenant_id=str(tenant_id),
        job_type=job_type,
        status=STATUS_PENDING,
        priority=priority,
        run_after=run_after or _utcnow(),
        max_attempts=effective_max_attempts,
        payload=final_payload,
    )
    session.add(job)

    try:
        await session.flush()
    except IntegrityError:
        # A dedupe-key collision (partial unique index) means an equivalent job
        # is already pending/running. Return that job's id idempotently.
        if dedupe_key is None:
            raise
        await session.rollback()
        existing = (
            await session.execute(
                select(Job.id)
                .where(
                    Job.tenant_id == str(tenant_id),
                    Job.job_type == job_type,
                    text("payload->>'dedupe_key' = :dk"),
                    Job.status.in_([STATUS_PENDING, STATUS_RUNNING]),
                )
                .order_by(Job.created_at.desc())
                .limit(1)
                .params(dk=dedupe_key)
            )
        ).scalar_one_or_none()
        if existing is None:
            # The conflict was not a dedupe-key violation (some other
            # constraint); surface it.
            raise
        logger.debug(
            "enqueue_job dedupe hit tenant=%s type=%s key=%s -> existing %s",
            tenant_id,
            job_type,
            dedupe_key,
            existing,
        )
        await session.commit()
        return existing

    await session.commit()
    logger.info(
        "enqueue_job tenant=%s type=%s id=%s priority=%s",
        tenant_id,
        job_type,
        job.id,
        priority,
    )
    return job.id


async def claim_next_job(
    session: AsyncSession,
    *,
    worker_id: str,
    job_types: list[str] | None = None,
    now: datetime | None = None,
) -> Job | None:
    """Atomically claim the next due pending job, if any.

    Uses a single ``UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED
    LIMIT 1)`` statement, so two workers cannot claim the same job. Increments
    ``attempts``, sets ``status='running'``, ``locked_at``, ``locked_by``, and
    commits before returning — a crash after this point leaves a reclaimable
    ``running`` row rather than a lost job.

    Runs through the owner session so the queue is globally fair across tenants.
    """
    moment = now or _utcnow()
    types_filter = tuple(job_types) if job_types else None

    subq = (
        select(Job.id)
        .where(
            Job.status == STATUS_PENDING,
            Job.run_after <= moment,
        )
        .order_by(Job.run_after.asc(), Job.priority.asc(), Job.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if types_filter:
        subq = subq.where(Job.job_type.in_(types_filter))

    claimed = (
        await session.execute(
            update(Job)
            .where(Job.id == subq)
            .values(
                status=STATUS_RUNNING,
                attempts=Job.attempts + 1,
                locked_at=moment,
                locked_by=worker_id,
                updated_at=moment,
            )
            .returning(Job)
        )
    ).scalar_one_or_none()

    if claimed is None:
        await session.rollback()
        return None

    await session.commit()
    logger.info(
        "claim_next_job worker=%s id=%s type=%s tenant=%s attempt=%s",
        worker_id,
        claimed.id,
        claimed.job_type,
        claimed.tenant_id,
        claimed.attempts,
    )
    return claimed


async def mark_job_succeeded(session: AsyncSession, job_id: UUID | str) -> None:
    """Mark a job succeeded and commit."""
    moment = _utcnow()
    await session.execute(
        update(Job)
        .where(Job.id == str(job_id))
        .values(
            status=STATUS_SUCCEEDED,
            locked_at=None,
            locked_by=None,
            last_error=None,
            completed_at=moment,
            updated_at=moment,
        )
    )
    await session.commit()
    logger.info("mark_job_succeeded id=%s", job_id)


async def mark_job_failed_or_retry(
    session: AsyncSession,
    job_id: UUID | str,
    error: str | BaseException,
    *,
    now: datetime | None = None,
) -> str:
    """Record a failure; retry with backoff or dead-letter if max attempts hit.

    Returns the resulting status (``STATUS_PENDING`` for a retry, ``STATUS_DEAD``
    when the job has exhausted its attempts).
    """
    moment = now or _utcnow()
    job_id_str = str(job_id)
    message = _truncate(str(error))

    job = (
        await session.execute(
            select(Job.attempts, Job.max_attempts).where(Job.id == job_id_str)
        )
    ).one_or_none()
    if job is None:
        await session.rollback()
        logger.warning("mark_job_failed_or_retry id=%s not found", job_id_str)
        return STATUS_DEAD

    attempts, max_attempts = job.tuple()
    if attempts >= max_attempts:
        await session.execute(
            update(Job)
            .where(Job.id == job_id_str)
            .values(
                status=STATUS_DEAD,
                locked_at=None,
                locked_by=None,
                last_error=message,
                completed_at=moment,
                updated_at=moment,
            )
        )
        await session.commit()
        logger.warning(
            "mark_job_failed_or_retry id=%s DEAD after %s attempts: %s",
            job_id_str,
            attempts,
            message,
        )
        return STATUS_DEAD

    run_after = moment + _backoff(attempts)
    await session.execute(
        update(Job)
        .where(Job.id == job_id_str)
        .values(
            status=STATUS_PENDING,
            run_after=run_after,
            locked_at=None,
            locked_by=None,
            last_error=message,
            updated_at=moment,
        )
    )
    await session.commit()
    logger.info(
        "mark_job_failed_or_retry id=%s retry attempt %s -> pending, run_after=%s",
        job_id_str,
        attempts,
        run_after.isoformat(),
    )
    return STATUS_PENDING


async def reclaim_stale_jobs(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    lease_stale_after_seconds: int = 300,
) -> int:
    """Return ``running`` jobs whose lease has expired to ``pending``.

    A worker that crashed mid-job leaves a ``running`` row with a stale
    ``locked_at``. This flips such rows back to ``pending`` (without consuming
    an attempt, since the attempt never completed) so they are reclaimed on the
    next claim cycle. Returns the number of reclaimed jobs.
    """
    moment = now or _utcnow()
    cutoff = moment - timedelta(seconds=lease_stale_after_seconds)
    result = await session.execute(
        update(Job)
        .where(
            Job.status == STATUS_RUNNING,
            Job.locked_at.is_not(None),
            Job.locked_at < cutoff,
        )
        .values(
            status=STATUS_PENDING,
            locked_at=None,
            locked_by=None,
            updated_at=moment,
        )
        .execution_options(synchronize_session=False)
    )
    reclaimed: int = cast("CursorResult[Any]", result).rowcount or 0
    await session.commit()
    if reclaimed:
        logger.info("reclaim_stale_jobs reclaimed %s stale job(s)", reclaimed)
    return reclaimed


async def active_job_exists(
    session: AsyncSession,
    *,
    tenant_id: UUID | str,
    job_type: str,
    dedupe_key: str,
) -> bool:
    """True if a pending/running job matches this ``(tenant, type, dedupe_key)``.

    Used by callers that want to check idempotency without enqueuing.
    """
    existing = (
        await session.execute(
            select(Job.id)
            .where(
                Job.tenant_id == str(tenant_id),
                Job.job_type == job_type,
                text("payload->>'dedupe_key' = :dk"),
                Job.status.in_([STATUS_PENDING, STATUS_RUNNING]),
            )
            .params(dk=dedupe_key)
            .limit(1)
        )
    ).scalar_one_or_none()
    return existing is not None


def job_age(moment: datetime, created_at: datetime) -> float:
    """Seconds between ``created_at`` and ``moment`` (for observability/logging)."""
    return (moment - created_at).total_seconds()


# `func` is re-exported for callers building queue queries; keep the import live.
__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "STATUS_CANCELLED",
    "STATUS_DEAD",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "STATUS_RUNNING",
    "STATUS_SUCCEEDED",
    "active_job_exists",
    "claim_next_job",
    "enqueue_job",
    "func",
    "job_age",
    "mark_job_failed_or_retry",
    "mark_job_succeeded",
    "reclaim_stale_jobs",
]
