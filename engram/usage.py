"""Usage/metering telemetry (ENG-METER-001).

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
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from engram.config import settings
from engram.models import UsageEvent

logger = logging.getLogger("engram.usage")

_NIL_UUID = UUID("00000000-0000-0000-0000-000000000000")


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
    dedupe_key: str | None = None,
    job_id: UUID | str | None = None,
    source_type: str | None = None,
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
                dedupe_key=dedupe_key,
                job_id=job_uuid,
                source_type=source_type,
                provider_adapter=provider_adapter,
                provider_host=provider_host,
                model=model,
                embedding_profile=embedding_profile,
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
    except IntegrityError:
        # Duplicate primary key (retried telemetry insert reusing event_id) or
        # duplicate dedupe_key (retried candidate/outcome event) — both are the
        # intended idempotent no-op, not a failure.
        logger.debug(
            "usage telemetry duplicate suppressed op=%s event_id=%s tenant_id=%s",
            operation,
            resolved_event_id,
            tenant_uuid,
        )
        return resolved_event_id
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
    candidate_utf8_bytes: int,
    source_type: str | None,
) -> UUID | None:
    """Record ``candidate.observed`` exactly once per ``correlation_id``.

    Idempotent via ``usage_events``' partial unique index on
    ``(tenant_id, event_type, dedupe_key)``: whichever of classify/remember
    (or a retry of either) reaches this first wins; later calls for the same
    correlation_id are no-ops. A direct ``/v1/remember`` call not preceded by
    ``/v1/classify`` still produces exactly one observation.
    """
    return await record_usage_event_best_effort(
        tenant_id=tenant_id,
        principal_id=principal_id,
        workspace_id=workspace_id,
        event_type="candidate.observed",
        operation="process_memory_candidate",
        status="accepted_for_processing",
        correlation_id=correlation_id,
        dedupe_key=str(correlation_id),
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
    status: str,
    source_type: str | None = None,
    final_kind: str | None = None,
    final_review_status: str | None = None,
    final_visibility: str | None = None,
    classification_mode: str | None = None,
) -> UUID | None:
    """Record ``candidate.outcome`` exactly once per ``correlation_id``.

    ``status`` should be one of ``created``, ``deduped``, ``superseded``,
    ``failed``. Idempotent the same way as :func:`record_candidate_once` — a
    retry with the same correlation id does not double-count. Known
    limitation: because the ledger is append-only, if a first attempt records
    ``failed`` and a later retry of the same correlation id actually succeeds,
    the earlier ``failed`` row is not corrected (see docs/usage-metering.md).
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
        dedupe_key=str(correlation_id),
        source_type=source_type,
        metadata=meta,
    )


async def record_provider_call(
    *,
    tenant_id: UUID | str,
    operation: str,
    status: str,
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
    job_id: UUID | str | None = None,
    event_id: UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> UUID | None:
    """Record one ``provider.call`` event.

    One event = one application-level provider call (a batched embedding
    request is one event with ``input_count=N``, never N events). ``status``
    is one of ``succeeded``, ``failed``, ``fallback``, ``disabled``,
    ``no_usage``. For failed calls, ``metadata`` should carry only a sanitized
    exception class/category — never a raw error message.
    """
    return await record_usage_event_best_effort(
        tenant_id=tenant_id,
        principal_id=principal_id,
        workspace_id=workspace_id,
        event_type="provider.call",
        operation=operation,
        status=status,
        correlation_id=correlation_id,
        job_id=job_id,
        provider_adapter=provider_adapter,
        provider_host=provider_host,
        model=model,
        embedding_profile=embedding_profile,
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
) -> UUID | None:
    """Record one ``retrieval.request`` event.

    ``operation`` is one of ``startup_recall``, ``semantic_recall``,
    ``keyword_search``, ``semantic_search``, ``hybrid_search``. Never stores
    query text. ``recall_logs`` remains the audit source of what was
    recalled; this is a metering summary only.
    """
    meta = {
        k: v
        for k, v in {
            "candidate_count": candidate_count,
            "scoring_version": scoring_version,
            "config_version": config_version,
            "embedding_call_occurred": embedding_call_occurred,
        }.items()
        if v is not None
    }
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
            return ProviderUsage()
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
    "ProviderUsage",
    "Timer",
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
