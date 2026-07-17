"""Usage/metering telemetry (ENG-METER-001 / ENG-METER-002).

Append-only, best-effort, privacy-preserving observability for dogfood
economics. This module is deliberately narrow: it never changes the result of
a normal Engram operation, never stores raw content/prompts/queries, and never
phones home — every write lands in the customer's own ``usage_events`` table
(migrations/017_usage_events.sql).

Core rules enforced here:

- A telemetry insert failure is logged (operation, event uuid, tenant id,
  exception *type* — never the exception message, which may echo request
  data) and swallowed. It must never raise into the caller.
- Writes use a short-lived app-role session (``engram.db.async_session_factory``),
  never the owner role and never the caller's own session — so an incurred
  provider cost survives the caller's business transaction later rolling
  back, and a telemetry DB error can never poison the caller's session.
- When ``ENGRAM_USAGE_TELEMETRY_ENABLED`` is false (the default), every
  helper below is a cheap no-op that returns ``None`` without opening a
  database session.
- Callers generate the event/call UUID *before* the provider call and pass it
  in as ``event_id``; retrying only the telemetry insert with the same
  ``event_id`` is idempotent (a duplicate primary key is treated as "already
  recorded", not a failure). A genuinely new provider call must use a new
  ``event_id`` — reusing one across two real calls would undercount actual
  provider work.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from engram.config import settings
from engram.models import UsageEvent

logger = logging.getLogger("engram.usage")

_NIL_UUID = UUID("00000000-0000-0000-0000-000000000000")

EmbeddingOutcome = Literal[
    "not_required",
    "not_attempted",
    "disabled",
    "succeeded",
    "failed",
    "unknown",
]
UsageClass = Literal["request", "async_enrichment", "maintenance", "diagnostic", "unknown"]


def embedding_call_occurred_for(outcome: EmbeddingOutcome) -> bool | None:
    """Derive the legacy nullable call Boolean from an embedding outcome."""
    if outcome in ("not_required", "not_attempted", "disabled"):
        return False
    if outcome in ("succeeded", "failed"):
        return True
    return None


def utf8_byte_len(text: str) -> int:
    """UTF-8 byte length of ``text`` — never Python character count."""
    return len(text.encode("utf-8"))


def _as_uuid(value: UUID | str | None) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


# Constraints whose IntegrityError is the intended idempotent no-op: a
# duplicate primary key (``usage_events_pkey``, from a retried insert reusing
# the same ``event_id``) or a duplicate dedupe_key (``idx_usage_events_dedupe``,
# from a retried ingest-keyed candidate.observed / client.lifecycle_summary event). Any
# OTHER integrity failure (foreign-key, CHECK, privilege error surfaced as an
# integrity error) is a real problem that must be logged, not silently
# suppressed as a "duplicate" (ENG-METER-001 blocking correction).
_EXPECTED_UNIQUE_CONSTRAINTS = frozenset({"usage_events_pkey", "idx_usage_events_dedupe"})

# SQLSTATE for a unique-violation (the only one we suppress as idempotent).
_UNIQUE_VIOLATION_SQLSTATE = "23505"


def _dbapi_exc(exc: Exception) -> Any:
    """Return the deepest driver-native exception SQLAlchemy wrapped.

    SQLAlchemy's asyncpg dialect re-wraps the raw ``asyncpg`` exception into
    its own ``AsyncAdapt_asyncpg_dbapi`` error class on ``.orig`` — that
    wrapper forwards ``sqlstate`` but drops ``constraint_name``/
    ``table_name``. The original asyncpg exception (which has both) is
    preserved as ``orig.__cause__`` (SQLAlchemy raises the wrapper ``from``
    it). Mirrors the unwrapping in ``engram.api.errors``.
    """
    orig = getattr(exc, "orig", None)
    if orig is None:
        return exc
    cause = getattr(orig, "__cause__", None)
    return cause if cause is not None else orig


def _sqlstate(exc: Exception) -> str | None:
    native = _dbapi_exc(exc)
    sqlstate = getattr(native, "sqlstate", None)
    return str(sqlstate) if sqlstate is not None else None


def _constraint_name(exc: Exception) -> str | None:
    name = getattr(_dbapi_exc(exc), "constraint_name", None)
    return str(name) if name is not None else None


def _is_expected_unique_violation(exc: Exception) -> bool:
    """True only for the intended idempotent-duplicate IntegrityError.

    Suppresses exactly a unique-violation (SQLSTATE 23505) on the primary key
    or the dedupe_key partial index. Everything else — foreign-key violations,
    CHECK constraints, privilege errors surfaced as integrity errors — is a
    genuine telemetry failure that must be logged, not hidden.
    """
    if _sqlstate(exc) != _UNIQUE_VIOLATION_SQLSTATE:
        return False
    name = _constraint_name(exc)
    return name in _EXPECTED_UNIQUE_CONSTRAINTS


def _json_safe(value: dict[str, Any] | None) -> dict[str, Any]:
    """Best-effort coercion to a JSON-serializable dict for the metadata column.

    Never raises — an unexpected value type (e.g. a UUID) is stringified
    rather than blowing up a telemetry insert.
    """
    if not value:
        return {}
    try:
        json.dumps(value)
        return value
    except TypeError:
        try:
            safe: dict[str, Any] = json.loads(json.dumps(value, default=str))
            return safe
        except Exception:  # noqa: BLE001 - telemetry must never raise
            return {}


async def record_usage_event_best_effort(
    *,
    tenant_id: UUID | str,
    event_type: str,
    operation: str,
    status: str,
    principal_id: UUID | str | None = None,
    workspace_id: UUID | str | None = None,
    correlation_id: UUID | None = None,
    ingest_id: UUID | None = None,
    dedupe_key: str | None = None,
    job_id: UUID | str | None = None,
    source_type: str | None = None,
    provider_adapter: str | None = None,
    provider_host: str | None = None,
    model: str | None = None,
    embedding_profile: str | None = None,
    usage_class: UsageClass | None = None,
    external_call_attempted: bool | None = None,
    input_count: int = 0,
    input_bytes: int = 0,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    latency_ms: int | None = None,
    reported_cost_usd: float | None = None,
    metadata: dict[str, Any] | None = None,
    event_id: UUID | None = None,
) -> UUID | None:
    """Insert one ``usage_events`` row on a dedicated short-lived app-role session.

    Returns the event id on success (including "already recorded" idempotent
    duplicates), or ``None`` when telemetry is disabled or the insert failed.
    The return value is diagnostic only — callers must never branch business
    logic on it.
    """
    if not settings.usage_telemetry_enabled:
        return None

    resolved_event_id = event_id or uuid4()
    tenant_uuid = _as_uuid(tenant_id)
    if tenant_uuid is None:
        logger.warning(
            "usage telemetry skipped: unresolvable tenant_id op=%s event_id=%s",
            operation,
            resolved_event_id,
        )
        return None
    principal_uuid = _as_uuid(principal_id)
    workspace_uuid = _as_uuid(workspace_id)
    job_uuid = _as_uuid(job_id)

    try:
        # Local imports: keep this module importable (and its no-op path free
        # of any DB-engine construction) when telemetry is disabled.
        from engram.db import apply_rls_context, async_session_factory

        async with async_session_factory() as session:
            await apply_rls_context(
                session,
                tenant_id=tenant_uuid,
                principal_id=principal_uuid or _NIL_UUID,
            )
            event = UsageEvent(
                id=resolved_event_id,
                tenant_id=tenant_uuid,
                principal_id=principal_uuid,
                workspace_id=workspace_uuid,
                event_type=event_type,
                operation=operation,
                status=status,
                correlation_id=correlation_id,
                ingest_id=ingest_id,
                dedupe_key=dedupe_key,
                job_id=job_uuid,
                source_type=source_type,
                provider_adapter=provider_adapter,
                provider_host=provider_host,
                model=model,
                embedding_profile=embedding_profile,
                usage_class=usage_class,
                external_call_attempted=external_call_attempted,
                input_count=max(0, input_count),
                input_bytes=max(0, input_bytes),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                latency_ms=latency_ms,
                reported_cost_usd=reported_cost_usd,
                event_metadata=_json_safe(metadata),
            )
            session.add(event)
            await session.commit()
        return resolved_event_id
    except IntegrityError as exc:
        if _is_expected_unique_violation(exc):
            # Duplicate primary key (retried telemetry insert reusing event_id)
            # or duplicate dedupe_key (retried candidate.observed /
            # client.lifecycle_summary event) — the intended idempotent no-op.
            logger.debug(
                "usage telemetry duplicate suppressed op=%s event_id=%s tenant_id=%s",
                operation,
                resolved_event_id,
                tenant_uuid,
            )
            return resolved_event_id
        # Any OTHER integrity failure (foreign-key, CHECK, privilege error
        # surfaced as an integrity error) is a genuine telemetry failure. It is
        # logged — never re-raised, telemetry must not poison the caller — but
        # distinctly, not hidden as a "duplicate". Constraint name is a schema
        # identifier (safe to log); str(exc) is excluded (may echo request data).
        logger.warning(
            "usage telemetry insert failed op=%s event_id=%s tenant_id=%s "
            "exc_type=%s sqlstate=%s constraint=%s",
            operation,
            resolved_event_id,
            tenant_uuid,
            type(exc).__name__,
            _sqlstate(exc),
            _constraint_name(exc),
        )
        return None
    except Exception as exc:  # noqa: BLE001 - telemetry must never raise
        # Deliberately exclude str(exc): it may echo request data (content,
        # prompts, queries). Only the exception *type* is safe to log.
        logger.warning(
            "usage telemetry insert failed op=%s event_id=%s tenant_id=%s exc_type=%s",
            operation,
            resolved_event_id,
            tenant_uuid,
            type(exc).__name__,
        )
        return None


async def record_candidate_once(
    *,
    tenant_id: UUID | str,
    principal_id: UUID | str | None,
    workspace_id: UUID | str | None,
    correlation_id: UUID,
    ingest_id: UUID,
    candidate_utf8_bytes: int,
    source_type: str | None,
) -> UUID | None:
    """Record ``candidate.observed`` exactly once per server-owned ingest.

    Idempotent via ``usage_events``' partial unique index on
    ``(tenant_id, event_type, dedupe_key)``: whichever of classify/remember
    (or a retry of either) reaches this first wins; later calls for the same
    ingest id are no-ops. The client correlation id is trace-only.
    """
    return await record_usage_event_best_effort(
        tenant_id=tenant_id,
        principal_id=principal_id,
        workspace_id=workspace_id,
        event_type="candidate.observed",
        operation="process_memory_candidate",
        status="accepted_for_processing",
        correlation_id=correlation_id,
        ingest_id=ingest_id,
        dedupe_key=f"ingest:{ingest_id}",
        source_type=source_type,
        input_count=1,
        input_bytes=max(0, candidate_utf8_bytes),
    )


async def record_candidate_outcome(
    *,
    tenant_id: UUID | str,
    principal_id: UUID | str | None,
    workspace_id: UUID | str | None,
    correlation_id: UUID,
    ingest_id: UUID | None,
    attempt_id: UUID,
    status: str,
    source_type: str | None = None,
    final_kind: str | None = None,
    final_review_status: str | None = None,
    final_visibility: str | None = None,
    classification_mode: str | None = None,
) -> UUID | None:
    """Record one ``candidate.outcome`` attempt for ``ingest_id``.

    Append-only per attempt: every ``/v1/remember`` invocation receives a
    server-generated ``attempt_id`` used as this event's ID. Retrying only the
    telemetry insert reuses that ID; there is no ``dedupe_key``.
    ``candidate.observed`` stays unique per ingest
    (see :func:`record_candidate_once`), but a candidate may be attempted more
    than once (e.g. a transient ``failed`` attempt followed by a successful
    retry), and both attempts are recorded honestly.

    ``status`` is the terminal outcome of THIS attempt: ``created``,
    ``deduped``, ``superseded``, or ``failed``. The report derives a single
    *logical* outcome per ingest (earliest non-``failed`` attempt,
    else ``failed``) so retries do not distort the failure/create funnel.
    """
    meta = {
        k: v
        for k, v in {
            "final_kind": final_kind,
            "final_review_status": final_review_status,
            "final_visibility": final_visibility,
            "classification_mode": classification_mode,
        }.items()
        if v is not None
    }
    return await record_usage_event_best_effort(
        tenant_id=tenant_id,
        principal_id=principal_id,
        workspace_id=workspace_id,
        event_type="candidate.outcome",
        operation="process_memory_candidate",
        status=status,
        correlation_id=correlation_id,
        ingest_id=ingest_id,
        source_type=source_type,
        metadata=meta,
        event_id=attempt_id,
    )


async def record_ingest_reuse_rejected(
    *,
    tenant_id: UUID | str,
    principal_id: UUID | str | None,
    workspace_id: UUID | str | None,
    correlation_id: UUID | None,
    ingest_id: UUID | None,
    mismatches: tuple[str, ...],
) -> UUID | None:
    """Record privacy-safe diagnostics for incompatible ingest reuse."""
    return await record_usage_event_best_effort(
        tenant_id=tenant_id,
        principal_id=principal_id,
        workspace_id=workspace_id,
        event_type="candidate.ingest_reuse_rejected",
        operation="process_memory_candidate",
        status="rejected",
        correlation_id=correlation_id,
        ingest_id=ingest_id,
        metadata={"mismatch_categories": list(mismatches)},
    )


async def record_provider_call(
    *,
    tenant_id: UUID | str,
    operation: str,
    status: str,
    usage_class: UsageClass,
    external_call_attempted: bool,
    principal_id: UUID | str | None = None,
    workspace_id: UUID | str | None = None,
    provider_adapter: str | None = None,
    provider_host: str | None = None,
    model: str | None = None,
    embedding_profile: str | None = None,
    input_count: int = 0,
    input_bytes: int = 0,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    latency_ms: int | None = None,
    reported_cost_usd: float | None = None,
    correlation_id: UUID | None = None,
    ingest_id: UUID | None = None,
    job_id: UUID | str | None = None,
    event_id: UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> UUID | None:
    """Record one application-level ``provider.call`` operation.

    One event = one application-level provider operation (a batched embedding
    request is one event with ``input_count=N``, never N events). ``status``
    records the provider outcome only: ``succeeded`` (a usable result was
    obtained, including responses that carry no token usage), ``failed`` (the
    provider errored or returned an unusable response), or ``disabled`` (no
    external provider call occurred — the provider is ``none``). Whether the
    APPLICATION fell back to a rule/heuristic after a provider failure is a
    separate concern recorded as ``metadata["application_fallback"] = True``
    (plus a sanitized ``error_type``); it is never conflated with ``status``.
    """
    return await record_usage_event_best_effort(
        tenant_id=tenant_id,
        principal_id=principal_id,
        workspace_id=workspace_id,
        event_type="provider.call",
        operation=operation,
        status=status,
        correlation_id=correlation_id,
        ingest_id=ingest_id,
        job_id=job_id,
        provider_adapter=provider_adapter,
        provider_host=provider_host,
        model=model,
        embedding_profile=embedding_profile,
        usage_class=usage_class,
        external_call_attempted=external_call_attempted,
        input_count=input_count,
        input_bytes=input_bytes,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        latency_ms=latency_ms,
        reported_cost_usd=reported_cost_usd,
        metadata=metadata,
        event_id=event_id,
    )


async def record_retrieval_request(
    *,
    tenant_id: UUID | str,
    principal_id: UUID | str | None,
    workspace_id: UUID | str | None,
    operation: str,
    status: str,
    item_count: int = 0,
    byte_count: int = 0,
    candidate_count: int | None = None,
    latency_ms: int | None = None,
    scoring_version: str | None = None,
    config_version: str | None = None,
    embedding_call_occurred: bool | None = None,
    embedding_outcome: EmbeddingOutcome | None = None,
    memory_context_version: str | None = None,
    memory_profile_id: UUID | str | None = None,
    memory_profile_revision_id: UUID | str | None = None,
    memory_profile_version: int | None = None,
) -> UUID | None:
    """Record one ``retrieval.request`` event (success OR failure).

    ``operation`` is one of ``startup_recall``, ``semantic_recall``,
    ``keyword_search``, ``semantic_search``, ``hybrid_search``. Never stores
    query text. ``recall_logs`` remains the audit source of what was
    recalled; this is a metering summary only.

    ``status`` is ``succeeded`` or ``failed``; failures are recorded too (a
    raised recall/search request previously produced no telemetry row, hiding
    retrieval errors). ``embedding_outcome`` distinguishes whether an external
    embedding provider call occurred, independently of the requested mode:
    ``not_required`` (the mode does not use embeddings), ``not_attempted``
    (execution failed before the provider call), ``disabled`` (the embedding
    abstraction was reached but configuration prevented an external call),
    ``succeeded`` (a usable vector was returned), ``failed`` (an external call
    failed or returned an unusable response), or ``unknown`` (the outer layer
    cannot determine the stage). ``embedding_call_occurred`` is retained for
    backward compatibility and is canonically derived from that outcome.
    """
    if embedding_outcome is not None:
        embedding_call_occurred = embedding_call_occurred_for(embedding_outcome)
    meta: dict[str, Any] = {}
    if embedding_outcome is not None or embedding_call_occurred is not None:
        # Preserve explicit JSON null only for canonical ``unknown``. Legacy
        # callers that supplied neither field retain true field absence.
        meta["embedding_call_occurred"] = embedding_call_occurred
    meta.update({
        k: v
        for k, v in {
            "candidate_count": candidate_count,
            "scoring_version": scoring_version,
            "config_version": config_version,
            "embedding_outcome": embedding_outcome,
            "memory_context_version": memory_context_version,
            "memory_profile_id": str(memory_profile_id) if memory_profile_id else None,
            "memory_profile_revision_id": str(memory_profile_revision_id)
            if memory_profile_revision_id
            else None,
            "memory_profile_version": memory_profile_version,
        }.items()
        if v is not None
    })
    return await record_usage_event_best_effort(
        tenant_id=tenant_id,
        principal_id=principal_id,
        workspace_id=workspace_id,
        event_type="retrieval.request",
        operation=operation,
        status=status,
        input_count=item_count,
        input_bytes=byte_count,
        latency_ms=latency_ms,
        metadata=meta,
    )


async def record_client_lifecycle_summary(
    *,
    tenant_id: UUID | str,
    principal_id: UUID | str | None,
    operation: str,
    status: str,
    invocation_id: UUID,
    extracted: int = 0,
    guard_rejected: int = 0,
    classified: int = 0,
    promoted: int = 0,
    parked: int = 0,
    errors: int = 0,
    candidate_bytes: int = 0,
    latency_ms: int | None = None,
    adapter_version: str | None = None,
) -> UUID | None:
    """Record one ``client.lifecycle_summary`` event.

    Diagnostic and client-reported — never authoritative. ``operation`` is
    the lifecycle event (``sync_turn`` | ``pre_compress`` | ``session_end``).
    Idempotent per ``invocation_id`` so a client retry does not double-count.
    """
    return await record_usage_event_best_effort(
        tenant_id=tenant_id,
        principal_id=principal_id,
        event_type="client.lifecycle_summary",
        operation=operation,
        status=status,
        dedupe_key=str(invocation_id),
        input_count=extracted,
        input_bytes=candidate_bytes,
        latency_ms=latency_ms,
        metadata={
            "authoritative": False,
            "invocation_id": str(invocation_id),
            "guard_rejected": guard_rejected,
            "classified": classified,
            "promoted": promoted,
            "parked": parked,
            "errors": errors,
            "adapter_version": adapter_version,
        },
    )


@dataclass(frozen=True)
class ProviderUsage:
    """Defensively-parsed provider usage/cost, all fields nullable.

    Missing usage is valid (a provider that omits ``usage`` entirely) and
    produces all-``None`` fields rather than raising.
    """

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    reported_cost_usd: float | None = None


_COST_FIELD_NAMES = ("cost", "total_cost", "estimated_cost", "cost_usd")


def _get_numeric(obj: Any, names: tuple[str, ...]) -> float | None:
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    extra = getattr(obj, "model_extra", None)
    if isinstance(extra, dict):
        for name in names:
            if name in extra and extra[name] is not None:
                try:
                    return float(extra[name])
                except (TypeError, ValueError):
                    continue
    return None


def extract_openai_compatible_usage(response: Any) -> ProviderUsage:
    """Defensively extract token usage + provider-reported cost from an
    OpenAI-compatible chat-completion or embeddings response.

    Handles: missing ``usage`` (valid — returns all-None), chat completion
    usage (``prompt_tokens``/``completion_tokens``/``total_tokens``),
    embedding usage (``prompt_tokens`` or ``input_tokens``/``total_tokens``,
    no completion tokens), and provider-specific cost fields that some
    OpenAI-compatible providers attach as extra properties on ``usage`` or
    the top-level response (e.g. ``cost``, ``total_cost``,
    ``estimated_cost``). Never raises — malformed provider extras degrade to
    ``None`` fields rather than failing the caller.
    """
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            # Some OpenAI-compatible providers attach cost only to the
            # top-level response (never on a ``usage`` object). Missing usage
            # is valid, but it must not prevent us from capturing a cost that
            # IS present at the top level.
            return ProviderUsage(reported_cost_usd=_get_numeric(response, _COST_FIELD_NAMES))
        prompt = _get_numeric(usage, ("prompt_tokens", "input_tokens"))
        completion = _get_numeric(usage, ("completion_tokens", "output_tokens"))
        total = _get_numeric(usage, ("total_tokens",))
        cost = _get_numeric(usage, _COST_FIELD_NAMES)
        if cost is None:
            cost = _get_numeric(response, _COST_FIELD_NAMES)
        return ProviderUsage(
            prompt_tokens=int(prompt) if prompt is not None else None,
            completion_tokens=int(completion) if completion is not None else None,
            total_tokens=int(total) if total is not None else None,
            reported_cost_usd=cost,
        )
    except Exception:  # noqa: BLE001 - defensive parsing, never raises
        return ProviderUsage()


_DEFAULT_HOSTS = {"openai": "api.openai.com"}


def safe_provider_identity(adapter: str, base_url: str | None) -> tuple[str, str | None]:
    """Derive a safe ``(logical_adapter, sanitized_hostname)`` pair.

    Preserves the logical adapter (e.g. ``"openai"``) and a bare hostname
    (e.g. ``"api.deepinfra.com"``) — never a URL path, query string, userinfo,
    or credentials. When ``base_url`` is unset, falls back to the SDK's known
    implicit default host for that adapter (still just a hostname, no
    credentials), or ``None`` when there is no such default.
    """
    if not base_url:
        return adapter, _DEFAULT_HOSTS.get(adapter)
    try:
        parsed = urlsplit(base_url)
        host = parsed.hostname
        return adapter, host
    except Exception:  # noqa: BLE001 - never let URL parsing raise
        return adapter, None


class Timer:
    """Tiny monotonic-clock stopwatch for provider-call latency in milliseconds."""

    __slots__ = ("_start",)

    def __init__(self) -> None:
        self._start = time.monotonic()

    def elapsed_ms(self) -> int:
        return max(0, round((time.monotonic() - self._start) * 1000))


__all__ = [
    "EmbeddingOutcome",
    "ProviderUsage",
    "Timer",
    "embedding_call_occurred_for",
    "extract_openai_compatible_usage",
    "record_candidate_once",
    "record_candidate_outcome",
    "record_client_lifecycle_summary",
    "record_provider_call",
    "record_retrieval_request",
    "record_usage_event_best_effort",
    "safe_provider_identity",
    "utf8_byte_len",
]
