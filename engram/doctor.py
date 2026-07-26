"""Read-only automatic-memory-loop doctor (ENG-LOOP-001A) — ``engram doctor``.

Composes existing health, identity, usage-report, review, recall, and Context
Receipt evidence into one bounded, versioned report answering whether Engram
and its automatic memory loop are healthy, degraded, unhealthy, or
unobservable.

This module is strictly observational: it never inserts, updates, deletes,
promotes, recalls, classifies, embeds, enqueues, retries, reclaims, archives,
verifies, disputes, or supersedes anything. It reuses
:func:`engram.usage_report.build_operational_snapshot` for candidate-funnel,
lifecycle, worker, and storage evidence rather than duplicating that
aggregation SQL, and reuses the existing ``/health``, ``/ready``, ``/whoami``,
``/v1/review/stats``, and ``/v1/review/queue`` HTTP contracts rather than
inventing a parallel observability surface.

Every check function is designed to fail into a safe ``DoctorCheck`` (never
raise) so one unavailable check never aborts the rest of the report.
"""

from __future__ import annotations

import asyncio
import decimal
import math
import os
import re
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from engram import __version__
from engram.context_receipts import verify_context_receipt_with_recall_log
from engram.models import ContextReceipt
from engram.usage_report import OperationalSnapshot, build_operational_snapshot

__all__ = [
    "CHECK_ORDER",
    "DEFAULT_TIMEOUT_SECONDS",
    "DOCTOR_PROFILE",
    "DOCTOR_SCHEMA",
    "DOCTOR_SCHEMA_VERSION",
    "REQUIRED_LIMITATIONS",
    "DoctorCheck",
    "DoctorReport",
    "DoctorScope",
    "DoctorWindow",
    "aggregate_status",
    "normalize_database_url",
    "parse_iso8601",
    "render_human",
    "resolve_base_url",
    "resolve_database_url",
    "resolve_window",
    "run_doctor",
    "seconds_to_statement_timeout_ms",
    "validate_timeout_seconds",
]

T = TypeVar("T")

# --- Report schema constants -------------------------------------------------

DOCTOR_SCHEMA = "engram.doctor"
DOCTOR_SCHEMA_VERSION = "1.0"
DOCTOR_PROFILE = "automatic_memory_loop"

Status = Literal["pass", "warn", "fail", "unknown"]
OverallStatus = Literal["healthy", "degraded", "unhealthy"]
ScopeSource = Literal["argument", "whoami", "deployment"]

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_WINDOW_HOURS = 24

# --- Stable check identifiers (dotted, ordered) ------------------------------

CHECK_SERVICE_HEALTH = "service.health"
CHECK_SERVICE_READINESS = "service.readiness"
CHECK_IDENTITY_SCOPES = "identity.scopes"
CHECK_CONFIG_EMBEDDINGS = "config.embeddings"
CHECK_CONFIG_CLASSIFICATION = "config.classification"
CHECK_WORKER_QUEUE = "worker.queue"
CHECK_CAPTURE_LIFECYCLE = "capture.lifecycle"
CHECK_CAPTURE_REMEMBER = "capture.remember"
CHECK_RECALL_ACTIVITY = "recall.activity"
CHECK_RECEIPTS_ACTIVITY = "receipts.activity"
CHECK_REVIEW_BACKLOG = "review.backlog"

# The required, stable, always-emitted ordering (report_contract.ordering).
CHECK_ORDER: tuple[str, ...] = (
    CHECK_SERVICE_HEALTH,
    CHECK_SERVICE_READINESS,
    CHECK_IDENTITY_SCOPES,
    CHECK_CONFIG_EMBEDDINGS,
    CHECK_CONFIG_CLASSIFICATION,
    CHECK_WORKER_QUEUE,
    CHECK_CAPTURE_LIFECYCLE,
    CHECK_CAPTURE_REMEMBER,
    CHECK_RECALL_ACTIVITY,
    CHECK_RECEIPTS_ACTIVITY,
    CHECK_REVIEW_BACKLOG,
)

# A tenant-scoped DB check whose evidence cannot be attributed to a specific
# tenant when identity resolution fails. Distinct from a per-check
# "*_EVIDENCE_UNAVAILABLE" code, which means the database itself was
# unreachable (a different failure mode).
TENANT_SCOPE_UNRESOLVED = "TENANT_SCOPE_UNRESOLVED"

REQUIRED_LIMITATIONS: tuple[str, ...] = (
    "The report does not inspect a running Hermes or other agent process and "
    "therefore does not prove that an adapter's automatic read or write hooks "
    "are active.",
    "The report does not make a live embedding or classification provider call.",
    "The report does not issue a recall and does not prove that recalled "
    "context entered an agent's final model prompt.",
    "Lifecycle-summary telemetry is client-reported diagnostic evidence and is "
    "not authoritative.",
    "Context Receipts prove what Engram served under a policy; they do not "
    "prove factual truth, model reliance, or causality.",
    "Local configuration checks describe the environment running the CLI. "
    "They must not be represented as authoritative configuration for a "
    "different remote deployment.",
)

_REASON_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


# --- Report models (strict, versioned) ---------------------------------------


class DoctorCheck(BaseModel):
    """One named diagnostic check and its evidence-backed result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    status: Status
    reason_code: str
    summary: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    remediation: list[str] = Field(default_factory=list)

    @field_validator("reason_code")
    @classmethod
    def _validate_reason_code(cls, value: str) -> str:
        if not _REASON_CODE_RE.match(value):
            raise ValueError(
                f"reason_code must be a stable uppercase identifier (got {value!r})"
            )
        return value


class DoctorWindow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    since: datetime
    until: datetime


class DoctorScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str | None
    source: ScopeSource


class DoctorReport(BaseModel):
    """The stable, versioned ``engram.doctor`` machine-readable report."""

    # ``BaseModel.schema()`` is a deprecated v1-compat method, so the field
    # is named ``schema_`` internally and aliased to the wire name ``schema``
    # (populate_by_name lets tests construct the model by either name; every
    # serialization call site below passes ``by_alias=True``).
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    schema_: str = Field(default=DOCTOR_SCHEMA, alias="schema")
    schema_version: str = DOCTOR_SCHEMA_VERSION
    profile: str = DOCTOR_PROFILE
    engram_version: str
    generated_at: datetime
    window: DoctorWindow
    scope: DoctorScope
    overall_status: OverallStatus
    exit_code: int
    checks: list[DoctorCheck]
    limitations: list[str]

    @model_validator(mode="after")
    def _validate_internal_consistency(self) -> DoctorReport:
        actual_order = tuple(c.id for c in self.checks)
        if actual_order != CHECK_ORDER:
            raise ValueError(
                f"doctor checks must appear in the required order {CHECK_ORDER}, "
                f"got {actual_order}"
            )
        expected_overall, expected_exit = aggregate_status(self.checks)
        if expected_overall != self.overall_status or expected_exit != self.exit_code:
            raise ValueError(
                "overall_status/exit_code are inconsistent with the check statuses"
            )
        return self


def aggregate_status(checks: list[DoctorCheck]) -> tuple[OverallStatus, int]:
    """Derive ``(overall_status, exit_code)`` from a list of checks.

    Any ``fail`` -> unhealthy/2. Otherwise any ``warn``/``unknown`` ->
    degraded/1. An all-``pass`` report -> healthy/0.
    """
    if any(c.status == "fail" for c in checks):
        return "unhealthy", 2
    if any(c.status in ("warn", "unknown") for c in checks):
        return "degraded", 1
    return "healthy", 0


def _jsonable(value: Any) -> Any:
    """Recursively coerce raw-SQL/driver values into stable JSON-safe types."""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _check(
    check_id: str,
    status: Status,
    reason_code: str,
    summary: str,
    *,
    evidence: dict[str, Any] | None = None,
    remediation: list[str] | None = None,
) -> DoctorCheck:
    return DoctorCheck(
        id=check_id,
        status=status,
        reason_code=reason_code,
        summary=summary,
        evidence=_jsonable(evidence or {}),
        remediation=list(remediation or []),
    )


# --- Pure validation helpers (unit-testable) ---------------------------------


def validate_timeout_seconds(value: float) -> float:
    """Reject zero, negative, NaN, and infinite timeouts (no silent coercion)."""
    if isinstance(value, bool):
        raise ValueError(f"--timeout-seconds must be numeric (got {value!r})")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"--timeout-seconds must be numeric (got {value!r})") from exc
    if not math.isfinite(numeric) or numeric <= 0:
        raise ValueError(
            "--timeout-seconds must be a finite, strictly positive number "
            f"(got {value!r})"
        )
    return numeric


def seconds_to_statement_timeout_ms(seconds: float) -> int:
    """Convert a validated positive timeout to PostgreSQL statement_timeout ms.

    Uses ceiling (never floor/truncate) with a floor of 1, so an accepted
    positive sub-millisecond timeout can never silently become 0 —
    PostgreSQL interprets ``statement_timeout=0`` as *no timeout*, which
    would defeat the bound entirely (ENG-LOOP-001A-FIX1 / FIX-6).
    """
    return max(1, math.ceil(seconds * 1000))


def parse_iso8601(value: str, *, param_name: str) -> datetime:
    """Parse a timezone-aware ISO-8601 timestamp; reject naive timestamps."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"{param_name} must be a valid ISO-8601 timestamp (got {value!r})"
        ) from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{param_name} must be timezone-aware (got {value!r})")
    return parsed.astimezone(UTC)


def resolve_window(
    *, since: datetime | None, until: datetime | None, now: datetime
) -> tuple[datetime, datetime]:
    """Resolve the effective ``(since, until)`` window, defaulting to 24h.

    Both bounds must be timezone-aware; ``since`` must be strictly before
    ``until``.
    """
    effective_until = until if until is not None else now
    effective_since = (
        since if since is not None else effective_until - timedelta(hours=DEFAULT_WINDOW_HOURS)
    )
    if effective_since.tzinfo is None:
        raise ValueError("--since must be timezone-aware")
    if effective_until.tzinfo is None:
        raise ValueError("--until must be timezone-aware")
    effective_since = effective_since.astimezone(UTC)
    effective_until = effective_until.astimezone(UTC)
    if effective_since >= effective_until:
        raise ValueError("--since must be strictly before --until")
    return effective_since, effective_until


def resolve_base_url(explicit: str | None, *, settings_obj: Any) -> str:
    """``--base-url`` -> ``ENGRAM_BASE_URL`` -> loopback on the configured port."""
    if explicit:
        return explicit
    env_value = os.environ.get("ENGRAM_BASE_URL")
    if env_value:
        return env_value
    return f"http://127.0.0.1:{settings_obj.port}"


def resolve_database_url(explicit: str | None) -> str | None:
    """``--database-url`` -> ``ENGRAM_OWNER_DATABASE_URL`` -> ``ENGRAM_DATABASE_URL``.

    Returns ``None`` when nothing resolves, so the caller falls back to the
    process's already-configured owner session factory. Never logged or
    echoed by any caller.
    """
    if explicit:
        return explicit
    return os.environ.get("ENGRAM_OWNER_DATABASE_URL") or os.environ.get("ENGRAM_DATABASE_URL")


def normalize_database_url(url: str) -> str:
    """Rewrite a bare ``postgresql://`` scheme to ``postgresql+asyncpg://``.

    ``create_async_engine`` requires an async-capable driver encoded in the
    URL; a bare ``postgresql://`` resolves to the synchronous psycopg2
    dialect and raises at engine-construction time rather than at connect
    time. ``postgresql+asyncpg://`` (and any other explicit driver, e.g.
    ``postgresql+psycopg://``) is returned unchanged (ENG-LOOP-001A-FIX1 /
    FIX-7).
    """
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


# --- HTTP-based checks --------------------------------------------------------


async def _check_service_health(client: httpx.AsyncClient) -> DoctorCheck:
    try:
        resp = await client.get("/health")
    except httpx.RequestError:
        return _check(
            CHECK_SERVICE_HEALTH,
            "fail",
            "SERVICE_UNREACHABLE",
            "GET /health was unreachable.",
            remediation=[
                "Confirm the service is running and --base-url/ENGRAM_BASE_URL is correct."
            ],
        )
    if resp.status_code != 200:
        return _check(
            CHECK_SERVICE_HEALTH,
            "fail",
            "SERVICE_HEALTH_INVALID",
            f"GET /health returned HTTP {resp.status_code}.",
            evidence={"http_status": resp.status_code},
        )
    try:
        body = resp.json()
    except ValueError:
        return _check(
            CHECK_SERVICE_HEALTH,
            "fail",
            "SERVICE_HEALTH_INVALID",
            "GET /health returned malformed JSON.",
        )
    if not isinstance(body, dict) or body.get("status") != "ok":
        return _check(
            CHECK_SERVICE_HEALTH,
            "fail",
            "SERVICE_HEALTH_INVALID",
            "GET /health did not report status=ok.",
        )
    return _check(
        CHECK_SERVICE_HEALTH,
        "pass",
        "SERVICE_HEALTHY",
        "Service is reachable and healthy.",
        evidence={"http_status": 200},
    )


_READY_STATUS_VALUES = frozenset({"ready", "not_ready"})
_READY_DATABASE_VALUES = frozenset({"connected", "unavailable"})
_READY_RLS_VALUES = frozenset({"ok", "no_tenant_context"})
_VERSION_STRING_RE = re.compile(r"^\d+(\.\d+){0,2}$")


def _safe_readiness_evidence(body: dict[str, Any], http_status: int) -> dict[str, Any]:
    """Allow-list only known categorical/version values from ``/ready``.

    ``/ready`` is treated as untrusted input — it may come from another
    Engram version, a misconfigured proxy, or a hostile server. Only values
    that exactly match a known safe category or a validated version-number
    shape are ever copied into evidence; free-text fields such as ``reason``,
    ``detail``, ``error``, or ``message`` are never copied, regardless of
    their content.
    """
    evidence: dict[str, Any] = {"http_status": http_status}
    status = body.get("status")
    if status in _READY_STATUS_VALUES:
        evidence["status"] = status
    database = body.get("database")
    if database in _READY_DATABASE_VALUES:
        evidence["database"] = database
    rls = body.get("rls")
    if rls in _READY_RLS_VALUES:
        evidence["rls"] = rls
    pgvector = body.get("pgvector")
    if pgvector == "missing" or (
        isinstance(pgvector, str) and _VERSION_STRING_RE.match(pgvector)
    ):
        evidence["pgvector"] = pgvector
    minimum_required = body.get("minimum_required")
    if isinstance(minimum_required, str) and _VERSION_STRING_RE.match(minimum_required):
        evidence["minimum_required"] = minimum_required
    return evidence


async def _check_service_readiness(client: httpx.AsyncClient) -> DoctorCheck:
    try:
        resp = await client.get("/ready")
    except httpx.RequestError:
        return _check(
            CHECK_SERVICE_READINESS,
            "fail",
            "SERVICE_READINESS_UNREACHABLE",
            "GET /ready was unreachable.",
        )
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    evidence = _safe_readiness_evidence(body, resp.status_code)
    if resp.status_code == 200 and evidence.get("status") == "ready":
        return _check(
            CHECK_SERVICE_READINESS, "pass", "SERVICE_READY", "Service reports ready.",
            evidence=evidence,
        )
    # The summary and remediation are always generated locally from validated
    # categories, never from remote prose (e.g. a `reason` field).
    return _check(
        CHECK_SERVICE_READINESS,
        "fail",
        "SERVICE_NOT_READY",
        "Service reports not ready.",
        evidence=evidence,
        remediation=[
            "Check database connectivity, RLS context, and the installed pgvector version."
        ],
    )


async def _check_identity(
    client: httpx.AsyncClient, *, requested_tenant: str | None
) -> tuple[DoctorCheck, str | None, ScopeSource]:
    """Resolve identity/scopes and the effective tenant scope for DB checks."""

    def _tenant_and_source(whoami_tenant: str | None) -> tuple[str | None, ScopeSource]:
        if requested_tenant is not None:
            return requested_tenant, "argument"
        if whoami_tenant:
            return whoami_tenant, "whoami"
        return None, "deployment"

    try:
        resp = await client.get("/whoami")
    except httpx.RequestError:
        check = _check(
            CHECK_IDENTITY_SCOPES, "fail", "IDENTITY_UNREACHABLE", "GET /whoami was unreachable."
        )
        tenant_id, source = _tenant_and_source(None)
        return check, tenant_id, source

    if resp.status_code == 401:
        check = _check(
            CHECK_IDENTITY_SCOPES, "fail", "AUTHENTICATION_FAILED",
            "Authentication failed (401).",
        )
        tenant_id, source = _tenant_and_source(None)
        return check, tenant_id, source

    if resp.status_code == 403:
        # /whoami itself only requires the `read` scope, so a 403 here means
        # the effective credential is missing `read` before the handler (and
        # its scopes list) is ever reached.
        check = _check(
            CHECK_IDENTITY_SCOPES, "fail", "READ_SCOPE_MISSING",
            "The effective credential is missing required scope: read.",
        )
        tenant_id, source = _tenant_and_source(None)
        return check, tenant_id, source

    if resp.status_code != 200:
        check = _check(
            CHECK_IDENTITY_SCOPES,
            "fail",
            "IDENTITY_RESPONSE_INVALID",
            f"GET /whoami returned HTTP {resp.status_code}.",
            evidence={"http_status": resp.status_code},
        )
        tenant_id, source = _tenant_and_source(None)
        return check, tenant_id, source

    try:
        body = resp.json()
    except ValueError:
        check = _check(
            CHECK_IDENTITY_SCOPES, "fail", "IDENTITY_RESPONSE_INVALID",
            "GET /whoami returned malformed JSON.",
        )
        tenant_id, source = _tenant_and_source(None)
        return check, tenant_id, source

    if not isinstance(body, dict):
        check = _check(
            CHECK_IDENTITY_SCOPES, "fail", "IDENTITY_RESPONSE_INVALID",
            "GET /whoami returned an unexpected JSON shape.",
        )
        tenant_id, source = _tenant_and_source(None)
        return check, tenant_id, source

    scopes = {str(s) for s in (body.get("scopes") or [])}
    has_read = "admin" in scopes or "read" in scopes
    has_write = "admin" in scopes or "write" in scopes
    whoami_tenant = body.get("tenant_id")
    profile = body.get("memory_profile")
    evidence: dict[str, Any] = {
        "tenant_id": whoami_tenant,
        "principal_type": body.get("principal_type"),
        "scopes": sorted(scopes),
        "memory_profile_slug": profile.get("slug") if isinstance(profile, dict) else None,
        "memory_profile_version": profile.get("version") if isinstance(profile, dict) else None,
    }
    tenant_id, source = _tenant_and_source(whoami_tenant)

    if not has_read or not has_write:
        missing = [name for name, ok in (("read", has_read), ("write", has_write)) if not ok]
        code = "READ_SCOPE_MISSING" if "read" in missing else "WRITE_SCOPE_MISSING"
        check = _check(
            CHECK_IDENTITY_SCOPES,
            "fail",
            code,
            f"Effective credential is missing required scope(s): {', '.join(missing)}.",
            evidence=evidence,
        )
        return check, tenant_id, source

    if (
        requested_tenant is not None
        and whoami_tenant is not None
        and str(requested_tenant) != str(whoami_tenant)
    ):
        check = _check(
            CHECK_IDENTITY_SCOPES,
            "warn",
            "TENANT_SCOPE_MISMATCH",
            "The --tenant argument differs from the HTTP credential's tenant; "
            "database-level evidence uses the explicit --tenant value.",
            evidence=evidence,
        )
        return check, tenant_id, source

    check = _check(
        CHECK_IDENTITY_SCOPES,
        "pass",
        "IDENTITY_READY",
        "Identity resolved with read and write scope.",
        evidence=evidence,
    )
    return check, tenant_id, source


_MAX_REVIEW_QUEUE_SAMPLE = 100


def _nonneg_int_or_none(value: Any) -> int | None:
    """Strict non-negative integer check: rejects bool, str, float, negatives."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _validate_review_stats(body: Any) -> dict[str, int] | None:
    """Validate the review/stats payload; ``None`` means invalid/untrustworthy.

    ``body`` is untrusted: a malformed but HTTP-200 response must not abort
    report construction, nor coerce non-numeric/negative/boolean counts into
    numbers. A missing status key means zero (a real hygiene report omits
    counts for empty buckets); a present-but-wrong-typed value is rejected.
    """
    if not isinstance(body, dict):
        return None
    by_status = body.get("by_review_status")
    if not isinstance(by_status, dict):
        return None
    result: dict[str, int] = {}
    for key in ("active", "proposed", "disputed"):
        parsed = _nonneg_int_or_none(by_status.get(key, 0))
        if parsed is None:
            return None
        result[key] = parsed
    total = _nonneg_int_or_none(body.get("total", 0))
    if total is None:
        return None
    result["total"] = total
    return result


def _validate_review_queue(body: Any) -> list[list[str]] | None:
    """Validate the bounded review/queue payload; ``None`` means invalid.

    Returns each item's ``promotion_blockers`` list (only field ever read —
    everything else, including ``content``, is discarded immediately without
    being copied anywhere). A single malformed item invalidates the whole
    sample rather than being silently skipped into a false PASS.
    """
    if not isinstance(body, list) or len(body) > _MAX_REVIEW_QUEUE_SAMPLE:
        return None
    blocker_lists: list[list[str]] = []
    for item in body:
        if not isinstance(item, dict):
            return None
        blockers = item.get("promotion_blockers", [])
        if not isinstance(blockers, list) or not all(isinstance(b, str) for b in blockers):
            return None
        blocker_lists.append(blockers)
    return blocker_lists


def _rank_blockers(blocker_lists: list[list[str]]) -> list[dict[str, Any]]:
    """Top-5 blockers, excluding the universal marker, deterministically ordered."""
    counter: Counter[str] = Counter()
    for blockers in blocker_lists:
        for blocker in blockers:
            if blocker == "conflict_recheck_not_run":
                continue
            counter[blocker] += 1
    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    return [{"code": code, "count": n} for code, n in ranked]


async def _check_review_backlog(client: httpx.AsyncClient) -> DoctorCheck:
    try:
        stats_resp = await client.get("/v1/review/stats")
    except httpx.RequestError:
        return _check(
            CHECK_REVIEW_BACKLOG, "unknown", "REVIEW_BACKLOG_UNAVAILABLE",
            "GET /v1/review/stats was unreachable.",
        )
    if stats_resp.status_code == 403:
        return _check(
            CHECK_REVIEW_BACKLOG,
            "unknown",
            "REVIEW_SCOPE_UNAVAILABLE",
            "Credential lacks review authority; review backlog cannot be assessed.",
        )
    if stats_resp.status_code != 200:
        return _check(
            CHECK_REVIEW_BACKLOG,
            "unknown",
            "REVIEW_BACKLOG_UNAVAILABLE",
            f"GET /v1/review/stats returned HTTP {stats_resp.status_code}.",
            evidence={"http_status": stats_resp.status_code},
        )
    try:
        stats_body = stats_resp.json()
    except ValueError:
        return _check(
            CHECK_REVIEW_BACKLOG, "unknown", "REVIEW_BACKLOG_UNAVAILABLE",
            "GET /v1/review/stats returned malformed JSON.",
        )

    validated_stats = _validate_review_stats(stats_body)
    if validated_stats is None:
        return _check(
            CHECK_REVIEW_BACKLOG, "unknown", "REVIEW_BACKLOG_UNAVAILABLE",
            "GET /v1/review/stats returned an invalid shape.",
        )

    evidence: dict[str, Any] = {
        "active_count": validated_stats["active"],
        "proposed_count": validated_stats["proposed"],
        "disputed_count": validated_stats["disputed"],
        "total_count": validated_stats["total"],
    }

    # A PASS requires BOTH stats and the bounded queue to be successfully
    # retrieved and validated; partial (stats-only) evidence may still be
    # returned alongside an `unknown` status when the queue portion fails.
    queue_error: str | None = None
    queue_body: Any = None
    try:
        queue_resp = await client.get(
            "/v1/review/queue", params={"limit": _MAX_REVIEW_QUEUE_SAMPLE}
        )
    except httpx.RequestError:
        queue_error = "GET /v1/review/queue was unreachable."
    else:
        if queue_resp.status_code != 200:
            queue_error = f"GET /v1/review/queue returned HTTP {queue_resp.status_code}."
        else:
            try:
                queue_body = queue_resp.json()
            except ValueError:
                queue_error = "GET /v1/review/queue returned malformed JSON."

    blocker_lists = None
    if queue_error is None:
        blocker_lists = _validate_review_queue(queue_body)
        if blocker_lists is None:
            queue_error = "GET /v1/review/queue returned an invalid shape."

    if queue_error is not None or blocker_lists is None:
        return _check(
            CHECK_REVIEW_BACKLOG,
            "unknown",
            "REVIEW_QUEUE_UNAVAILABLE",
            queue_error or "GET /v1/review/queue returned an invalid shape.",
            evidence=evidence,
        )

    evidence["queue_sample_size"] = len(blocker_lists)
    evidence["top_blockers"] = _rank_blockers(blocker_lists)
    evidence["promotion_preview_limitation"] = (
        "conflict_recheck_not_run is excluded from blocker ranking; promotion "
        "preview never runs the conflict recheck."
    )
    return _check(
        CHECK_REVIEW_BACKLOG,
        "pass",
        "REVIEW_BACKLOG_OBSERVED",
        f"{evidence['total_count']} current item(s) observed "
        f"({evidence['active_count']} active, {evidence['proposed_count']} proposed, "
        f"{evidence['disputed_count']} disputed).",
        evidence=evidence,
    )


# --- Local process configuration checks (no HTTP, no DB, no provider call) --


def _check_config_embeddings(
    settings_obj: Any, *, snapshot: OperationalSnapshot | None
) -> DoctorCheck:
    provider = settings_obj.embedding_provider
    api_key_present = bool(settings_obj.openai_api_key)
    base_url_present = bool(settings_obj.openai_base_url)
    storage = snapshot.storage if snapshot is not None else None
    evidence: dict[str, Any] = {
        "provider": provider,
        "model": settings_obj.embedding_model,
        "dimensions": settings_obj.embedding_dim,
        "api_key_present": api_key_present,
        "base_url_present": base_url_present,
        "writable_profile_count": storage.get("embedding_profiles_writable") if storage else None,
        "embeddings_ready": storage.get("embeddings_ready") if storage else None,
        "embeddings_pending": storage.get("embeddings_pending") if storage else None,
        "embeddings_failed": storage.get("embeddings_failed") if storage else None,
    }
    if provider == "none":
        return _check(
            CHECK_CONFIG_EMBEDDINGS,
            "warn",
            "EMBEDDINGS_DISABLED",
            "Embeddings are intentionally disabled (provider=none).",
            evidence=evidence,
            remediation=[
                "Set ENGRAM_EMBEDDING_PROVIDER=openai and configure "
                "ENGRAM_OPENAI_API_KEY to enable semantic enrichment."
            ],
        )
    if provider != "openai":
        return _check(
            CHECK_CONFIG_EMBEDDINGS,
            "fail",
            "EMBEDDING_CONFIG_INVALID",
            f"Unsupported embedding provider {provider!r}.",
            evidence=evidence,
            remediation=["Set ENGRAM_EMBEDDING_PROVIDER to 'openai' or 'none'."],
        )
    if not api_key_present:
        return _check(
            CHECK_CONFIG_EMBEDDINGS,
            "fail",
            "EMBEDDING_CONFIG_INVALID",
            "Embedding provider is enabled but ENGRAM_OPENAI_API_KEY is not set.",
            evidence=evidence,
            remediation=["Set ENGRAM_OPENAI_API_KEY."],
        )
    failed = evidence["embeddings_failed"]
    if isinstance(failed, int) and failed > 0:
        return _check(
            CHECK_CONFIG_EMBEDDINGS,
            "warn",
            "EMBEDDING_FAILURES_PRESENT",
            f"{failed} embedding row(s) are in a failed state.",
            evidence=evidence,
            remediation=["Run 'engram backfill-embeddings --retry-failed' to retry failed rows."],
        )
    return _check(
        CHECK_CONFIG_EMBEDDINGS,
        "pass",
        "EMBEDDINGS_READY",
        "Embedding provider is configured and ready.",
        evidence=evidence,
    )


def _check_config_classification(settings_obj: Any) -> DoctorCheck:
    provider = settings_obj.classification_provider
    credential_present = bool(settings_obj.classification_api_key or settings_obj.openai_api_key)
    base_url_present = bool(settings_obj.classification_base_url or settings_obj.openai_base_url)
    evidence: dict[str, Any] = {
        "provider": provider,
        "model": settings_obj.classification_model,
        "credential_present": credential_present,
        "base_url_present": base_url_present,
    }
    if provider == "none":
        return _check(
            CHECK_CONFIG_CLASSIFICATION,
            "warn",
            "CLASSIFICATION_RULE_ONLY",
            "Classification is rule-based only (provider=none).",
            evidence=evidence,
            remediation=[
                "Set ENGRAM_CLASSIFICATION_PROVIDER=openai to enable LLM-refined "
                "classification."
            ],
        )
    if provider != "openai":
        return _check(
            CHECK_CONFIG_CLASSIFICATION,
            "fail",
            "CLASSIFICATION_CONFIG_INVALID",
            f"Unsupported classification provider {provider!r}.",
            evidence=evidence,
            remediation=["Set ENGRAM_CLASSIFICATION_PROVIDER to 'openai' or 'none'."],
        )
    if not credential_present:
        return _check(
            CHECK_CONFIG_CLASSIFICATION,
            "fail",
            "CLASSIFICATION_CONFIG_INVALID",
            "Classification provider is enabled but no credential is configured.",
            evidence=evidence,
            remediation=["Set ENGRAM_CLASSIFICATION_API_KEY or ENGRAM_OPENAI_API_KEY."],
        )
    return _check(
        CHECK_CONFIG_CLASSIFICATION,
        "pass",
        "CLASSIFICATION_READY",
        "Classification provider is configured.",
        evidence=evidence,
    )


# --- Bounded, read-only PostgreSQL inspection --------------------------------


async def _isolated(session: AsyncSession, coro: Awaitable[T]) -> tuple[T | None, str | None]:
    """Run ``coro`` inside a SAVEPOINT so its failure cannot poison later queries.

    Returns ``(result, None)`` on success or ``(None, error_type_name)`` on
    failure. Never raises: one failed or unavailable check must not abort the
    remaining checks. Only the exception's type name is ever surfaced —
    connection/provider exceptions can embed URLs or credentials in their
    message text.
    """
    try:
        async with session.begin_nested():
            result = await coro
        return result, None
    except Exception as exc:  # noqa: BLE001 - see docstring
        return None, type(exc).__name__


async def _job_health_counts(
    session: AsyncSession,
    *,
    tenant_id: str | None,
    lease_stale_after_seconds: int,
    now: datetime,
) -> dict[str, Any]:
    dead_where = ["status = 'dead'"]
    dead_params: dict[str, Any] = {}
    if tenant_id is not None:
        dead_where.append("tenant_id = :tenant_id")
        dead_params["tenant_id"] = tenant_id
    dead_count = await session.scalar(
        text(f"SELECT count(*) FROM jobs WHERE {' AND '.join(dead_where)}"), dead_params
    )

    stale_where = ["status = 'running'", "locked_at IS NOT NULL", "locked_at < :cutoff"]
    stale_params: dict[str, Any] = {
        "cutoff": now - timedelta(seconds=lease_stale_after_seconds)
    }
    if tenant_id is not None:
        stale_where.append("tenant_id = :tenant_id")
        stale_params["tenant_id"] = tenant_id
    stale_count = await session.scalar(
        text(f"SELECT count(*) FROM jobs WHERE {' AND '.join(stale_where)}"), stale_params
    )

    # Only jobs that are actually DUE (run_after <= effective now) count as
    # backlog — a retry sleeping under backoff or a job intentionally
    # scheduled in the future is not yet runnable and must not be diagnosed
    # as an aged backlog just because it is old (ENG-LOOP-001A-FIX1 / FIX-4).
    due_where = ["status = 'pending'", "run_after <= :now"]
    due_params: dict[str, Any] = {"now": now}
    if tenant_id is not None:
        due_where.append("tenant_id = :tenant_id")
        due_params["tenant_id"] = tenant_id
    due_row = (
        await session.execute(
            text(
                "SELECT count(*) AS n, min(run_after) AS oldest_run_after "
                f"FROM jobs WHERE {' AND '.join(due_where)}"
            ),
            due_params,
        )
    ).mappings().one()
    oldest_run_after = due_row["oldest_run_after"]
    oldest_due_pending_age_seconds = (
        (now - oldest_run_after).total_seconds() if oldest_run_after is not None else None
    )

    return {
        "dead_jobs": int(dead_count or 0),
        "stale_running_jobs": int(stale_count or 0),
        "due_pending_jobs": int(due_row["n"] or 0),
        "oldest_due_pending_age_seconds": oldest_due_pending_age_seconds,
    }


async def _lifecycle_summary_extra(
    session: AsyncSession, *, tenant_id: str | None, since: datetime, until: datetime
) -> dict[str, Any]:
    where = [
        "event_type = 'client.lifecycle_summary'",
        "created_at >= :since",
        "created_at < :until",
    ]
    params: dict[str, Any] = {"since": since, "until": until}
    if tenant_id is not None:
        where.append("tenant_id = :tenant_id")
        params["tenant_id"] = tenant_id
    row = (
        await session.execute(
            text(
                "SELECT count(*) AS n, max(created_at) AS latest_at, "
                "COALESCE(sum((metadata->>'errors')::bigint), 0) AS total_errors "
                f"FROM usage_events WHERE {' AND '.join(where)}"
            ),
            params,
        )
    ).mappings().one()
    return {
        "count": int(row["n"] or 0),
        "latest_at": row["latest_at"],
        "total_errors": int(row["total_errors"] or 0),
    }


async def _recall_activity_counts(
    session: AsyncSession, *, tenant_id: str | None, since: datetime, until: datetime
) -> dict[str, Any]:
    where = ["created_at >= :since", "created_at < :until"]
    params: dict[str, Any] = {"since": since, "until": until}
    if tenant_id is not None:
        where.append("tenant_id = :tenant_id")
        params["tenant_id"] = tenant_id
    row = (
        await session.execute(
            text(
                "SELECT count(*) FILTER (WHERE mode = 'startup') AS startup_count, "
                "count(*) FILTER (WHERE mode = 'semantic') AS semantic_count, "
                "max(created_at) AS latest_at "
                f"FROM recall_logs WHERE {' AND '.join(where)}"
            ),
            params,
        )
    ).mappings().one()
    return {
        "startup_count": int(row["startup_count"] or 0),
        "semantic_count": int(row["semantic_count"] or 0),
        "latest_at": row["latest_at"],
    }


async def _receipts_activity_evidence(
    session: AsyncSession, *, tenant_id: str | None, since: datetime, until: datetime
) -> dict[str, Any]:
    recall_where = ["mode = 'startup'", "created_at >= :since", "created_at < :until"]
    recall_params: dict[str, Any] = {"since": since, "until": until}
    if tenant_id is not None:
        recall_where.append("tenant_id = :tenant_id")
        recall_params["tenant_id"] = tenant_id
    startup_recall_count = await session.scalar(
        text(f"SELECT count(*) FROM recall_logs WHERE {' AND '.join(recall_where)}"),
        recall_params,
    )

    receipt_where = ["created_at >= :since", "created_at < :until"]
    receipt_params: dict[str, Any] = {"since": since, "until": until}
    if tenant_id is not None:
        receipt_where.append("tenant_id = :tenant_id")
        receipt_params["tenant_id"] = tenant_id
    receipt_count = await session.scalar(
        text(f"SELECT count(*) FROM context_receipts WHERE {' AND '.join(receipt_where)}"),
        receipt_params,
    )

    unmatched_where = [
        "rl.mode = 'startup'",
        "rl.created_at >= :since",
        "rl.created_at < :until",
        "NOT EXISTS (SELECT 1 FROM context_receipts cr WHERE cr.recall_log_id = rl.id)",
    ]
    unmatched_params: dict[str, Any] = {"since": since, "until": until}
    if tenant_id is not None:
        unmatched_where.append("rl.tenant_id = :tenant_id")
        unmatched_params["tenant_id"] = tenant_id
    unmatched_count = await session.scalar(
        text(f"SELECT count(*) FROM recall_logs rl WHERE {' AND '.join(unmatched_where)}"),
        unmatched_params,
    )

    # Scoped to the SAME [since, until) window as every other figure here —
    # otherwise a report for an older window could fail because of a receipt
    # entirely outside that window (ENG-LOOP-001A-FIX1 / FIX-5). Deterministic
    # tie-break (id DESC) so two receipts sharing one created_at timestamp
    # resolve identically on every run.
    latest_stmt = (
        select(ContextReceipt)
        .where(ContextReceipt.created_at >= since, ContextReceipt.created_at < until)
        .order_by(ContextReceipt.created_at.desc(), ContextReceipt.id.desc())
        .limit(1)
    )
    if tenant_id is not None:
        latest_stmt = latest_stmt.where(ContextReceipt.tenant_id == tenant_id)
    latest_receipt = (await session.execute(latest_stmt)).scalars().first()

    latest_verification_status: str | None = None
    latest_verification_error: str | None = None
    latest_receipt_at = None
    if latest_receipt is not None:
        latest_receipt_at = latest_receipt.created_at
        # Verification is isolated from the count queries above: a technical
        # failure verifying the receipt (e.g. a transient error loading the
        # parent recall log) must not be conflated with stored-artifact
        # corruption, and must not erase the already-gathered counts.
        try:
            result = await verify_context_receipt_with_recall_log(
                session,
                latest_receipt,
                tenant_id=latest_receipt.tenant_id,
                principal_id=latest_receipt.principal_id,
            )
            latest_verification_status = result.status
        except Exception as exc:  # noqa: BLE001 - see docstring
            latest_verification_error = type(exc).__name__

    return {
        "startup_recall_count": int(startup_recall_count or 0),
        "receipt_count": int(receipt_count or 0),
        "unmatched_startup_recall_count": int(unmatched_count or 0),
        "latest_receipt_at": latest_receipt_at,
        "latest_verification_status": latest_verification_status,
        "latest_verification_error": latest_verification_error,
    }


# --- DB-backed checks (composed from snapshot + supplementary evidence) -----


def _check_worker_queue(
    *,
    snapshot: OperationalSnapshot | None,
    job_health: dict[str, Any] | None,
    tenant_unresolved: bool,
    db_error: str | None,
    lease_stale_after_seconds: int,
) -> DoctorCheck:
    if tenant_unresolved:
        evidence: dict[str, Any] = {}
        if snapshot is not None:
            evidence["oldest_pending_age_seconds"] = snapshot.worker.get(
                "oldest_pending_age_seconds"
            )
        if job_health is not None:
            evidence.update(job_health)
        return _check(
            CHECK_WORKER_QUEUE,
            "unknown",
            TENANT_SCOPE_UNRESOLVED,
            "Tenant scope could not be resolved; worker evidence is deployment-wide only.",
            evidence=evidence,
        )
    if snapshot is None or job_health is None:
        return _check(
            CHECK_WORKER_QUEUE,
            "unknown",
            "WORKER_EVIDENCE_UNAVAILABLE",
            "Worker queue evidence unavailable (database inspection failed).",
            evidence={"error_type": db_error},
        )
    dead = job_health["dead_jobs"]
    stale = job_health["stale_running_jobs"]
    due_pending = job_health["due_pending_jobs"]
    oldest_due_pending = job_health["oldest_due_pending_age_seconds"]
    evidence = {
        "dead_jobs": dead,
        "stale_running_jobs": stale,
        "due_pending_jobs": due_pending,
        "oldest_due_pending_age_seconds": oldest_due_pending,
        # Legacy usage-report figure (oldest PENDING job by created_at,
        # regardless of run_after) retained for transparency only — the
        # status decision below uses only DUE pending work.
        "oldest_pending_age_seconds": snapshot.worker.get("oldest_pending_age_seconds"),
    }
    if dead > 0:
        return _check(
            CHECK_WORKER_QUEUE,
            "fail",
            "DEAD_JOBS_PRESENT",
            f"{dead} job(s) exhausted retries and are dead.",
            evidence=evidence,
            remediation=["Inspect dead jobs and re-enqueue or resolve the underlying failure."],
        )
    if stale > 0:
        return _check(
            CHECK_WORKER_QUEUE,
            "fail",
            "STALE_RUNNING_JOBS",
            f"{stale} job(s) are running past the lease timeout.",
            evidence=evidence,
            remediation=[
                "Confirm a worker process is running; stale jobs are reclaimed automatically."
            ],
        )
    oldest_due_pending_numeric = (
        float(oldest_due_pending) if oldest_due_pending is not None else None
    )
    if (
        oldest_due_pending_numeric is not None
        and oldest_due_pending_numeric > lease_stale_after_seconds
    ):
        return _check(
            CHECK_WORKER_QUEUE,
            "warn",
            "PENDING_BACKLOG_AGED",
            f"Oldest due pending job has waited {int(oldest_due_pending_numeric)}s "
            f"past its scheduled run time ({due_pending} job(s) currently due).",
            evidence=evidence,
            remediation=["Confirm the worker ('engram worker') is running."],
        )
    return _check(
        CHECK_WORKER_QUEUE,
        "pass",
        "WORKER_HEALTHY",
        "No dead or stale jobs; pending backlog is fresh.",
        evidence=evidence,
    )


def _check_capture_lifecycle(
    *,
    settings_obj: Any,
    snapshot: OperationalSnapshot | None,
    lifecycle_extra: dict[str, Any] | None,
    tenant_unresolved: bool,
    db_error: str | None,
) -> DoctorCheck:
    if not settings_obj.usage_telemetry_enabled:
        return _check(
            CHECK_CAPTURE_LIFECYCLE,
            "unknown",
            "USAGE_TELEMETRY_DISABLED",
            "Usage telemetry is disabled on this process; lifecycle evidence is unavailable.",
            remediation=["Set ENGRAM_USAGE_TELEMETRY_ENABLED=true to collect lifecycle evidence."],
        )
    if tenant_unresolved:
        return _check(
            CHECK_CAPTURE_LIFECYCLE,
            "unknown",
            TENANT_SCOPE_UNRESOLVED,
            "Tenant scope could not be resolved; lifecycle evidence cannot be scoped.",
        )
    if snapshot is None or lifecycle_extra is None:
        return _check(
            CHECK_CAPTURE_LIFECYCLE,
            "unknown",
            "LIFECYCLE_EVIDENCE_UNAVAILABLE",
            "Lifecycle evidence unavailable (database inspection failed).",
            evidence={"error_type": db_error},
        )
    funnel = snapshot.candidate_funnel
    evidence: dict[str, Any] = {
        "lifecycle_extracted": funnel.get("lifecycle_extracted"),
        "lifecycle_guard_rejected": funnel.get("lifecycle_guard_rejected"),
        "lifecycle_classified": funnel.get("lifecycle_classified"),
        "lifecycle_parked": funnel.get("lifecycle_parked"),
        "lifecycle_summary_count": lifecycle_extra["count"],
        "latest_lifecycle_summary_at": lifecycle_extra["latest_at"],
        "lifecycle_errors_reported": lifecycle_extra["total_errors"],
    }
    if lifecycle_extra["count"] == 0:
        return _check(
            CHECK_CAPTURE_LIFECYCLE,
            "warn",
            "NO_LIFECYCLE_EVIDENCE",
            "Telemetry is enabled but no lifecycle summary was reported in the window.",
            evidence=evidence,
        )
    if lifecycle_extra["total_errors"] > 0:
        return _check(
            CHECK_CAPTURE_LIFECYCLE,
            "warn",
            "LIFECYCLE_ERRORS_REPORTED",
            f"{lifecycle_extra['total_errors']} lifecycle error(s) reported in the window.",
            evidence=evidence,
        )
    return _check(
        CHECK_CAPTURE_LIFECYCLE,
        "pass",
        "LIFECYCLE_EVIDENCE_RECENT",
        "Recent, error-free lifecycle evidence observed.",
        evidence=evidence,
    )


def _check_capture_remember(
    *,
    settings_obj: Any,
    snapshot: OperationalSnapshot | None,
    tenant_unresolved: bool,
    db_error: str | None,
) -> DoctorCheck:
    if not settings_obj.usage_telemetry_enabled:
        return _check(
            CHECK_CAPTURE_REMEMBER,
            "unknown",
            "REMEMBER_EVIDENCE_UNAVAILABLE",
            "Usage telemetry is disabled; no authoritative remember-pipeline evidence exists.",
            remediation=[
                "Set ENGRAM_USAGE_TELEMETRY_ENABLED=true to collect remember-pipeline evidence."
            ],
        )
    if tenant_unresolved:
        return _check(
            CHECK_CAPTURE_REMEMBER,
            "unknown",
            TENANT_SCOPE_UNRESOLVED,
            "Tenant scope could not be resolved; remember-pipeline evidence cannot be scoped.",
        )
    if snapshot is None:
        return _check(
            CHECK_CAPTURE_REMEMBER,
            "unknown",
            "REMEMBER_EVIDENCE_UNAVAILABLE",
            "Remember-pipeline evidence unavailable (database inspection failed).",
            evidence={"error_type": db_error},
        )
    funnel = snapshot.candidate_funnel
    observations = int(funnel.get("candidate_observations", 0) or 0)
    created = int(funnel.get("created", 0) or 0)
    deduped = int(funnel.get("deduped", 0) or 0)
    superseded = int(funnel.get("superseded", 0) or 0)
    failed = int(funnel.get("failed", 0) or 0)
    # The full LOGICAL unresolved-candidate count (ingest-based AND legacy
    # correlation-only candidates with no terminal outcome yet) drives the
    # status decision below; a narrower ingest-only count is also preserved
    # in evidence for operators who want that specific breakdown.
    unresolved = int(funnel.get("unresolved_candidates", 0) or 0)
    unresolved_ingests = int(funnel.get("candidate_ingests_unresolved", 0) or 0)
    lifecycle_extracted = int(funnel.get("lifecycle_extracted", 0) or 0)
    success = created + deduped + superseded
    evidence: dict[str, Any] = {
        "candidate_observations": observations,
        "created": created,
        "deduped": deduped,
        "superseded": superseded,
        "failed": failed,
        "unresolved_candidates": unresolved,
        "unresolved_ingests": unresolved_ingests,
        "time_to_first_success_ms_p50": funnel.get("time_to_first_success_ms_p50"),
        "time_to_first_success_ms_p90": funnel.get("time_to_first_success_ms_p90"),
    }
    if observations == 0:
        if lifecycle_extracted > 0:
            return _check(
                CHECK_CAPTURE_REMEMBER,
                "warn",
                "CAPTURE_NOT_REACHING_SERVER",
                "Lifecycle extraction was observed but no candidate reached the server.",
                evidence=evidence,
            )
        return _check(
            CHECK_CAPTURE_REMEMBER,
            "unknown",
            "REMEMBER_EVIDENCE_UNAVAILABLE",
            "No candidate activity observed in the window.",
            evidence=evidence,
        )

    has_success = success > 0
    has_failed = failed > 0
    has_unresolved = unresolved > 0

    # An unresolved logical candidate (no terminal outcome yet) is never
    # silently folded into success or failure — only a genuinely resolved
    # terminal outcome can produce PASS or FAIL.
    if has_success and has_failed:
        summary = f"{failed} remember attempt(s) failed in the window."
        if has_unresolved:
            summary += f" {unresolved} candidate(s) also remain unresolved."
        return _check(
            CHECK_CAPTURE_REMEMBER, "warn", "REMEMBER_PARTIAL_FAILURES", summary,
            evidence=evidence,
        )
    if has_failed:
        if has_unresolved:
            return _check(
                CHECK_CAPTURE_REMEMBER,
                "warn",
                "REMEMBER_OUTCOMES_PENDING",
                f"{failed} failed and {unresolved} unresolved candidate(s); "
                "no successful outcome yet in the window.",
                evidence=evidence,
            )
        return _check(
            CHECK_CAPTURE_REMEMBER,
            "fail",
            "REMEMBER_PIPELINE_FAILED",
            "Every remember attempt in the window failed.",
            evidence=evidence,
            remediation=["Inspect worker/service logs for the remember pipeline."],
        )
    if has_unresolved:
        return _check(
            CHECK_CAPTURE_REMEMBER,
            "warn",
            "REMEMBER_OUTCOMES_PENDING",
            f"{unresolved} candidate(s) have not yet resolved to a terminal outcome.",
            evidence=evidence,
        )
    if has_success:
        return _check(
            CHECK_CAPTURE_REMEMBER,
            "pass",
            "REMEMBER_PIPELINE_HEALTHY",
            "Candidates are reaching the server and resolving successfully.",
            evidence=evidence,
        )
    return _check(
        CHECK_CAPTURE_REMEMBER,
        "unknown",
        "REMEMBER_EVIDENCE_INCOMPLETE",
        "Candidate observations exist but no success, failure, or unresolved "
        "outcome could be established from available evidence.",
        evidence=evidence,
    )


def _check_recall_activity(
    *,
    recall_evidence: dict[str, Any] | None,
    tenant_unresolved: bool,
    db_error: str | None,
) -> DoctorCheck:
    if tenant_unresolved:
        return _check(
            CHECK_RECALL_ACTIVITY,
            "unknown",
            TENANT_SCOPE_UNRESOLVED,
            "Tenant scope could not be resolved; recall evidence cannot be scoped.",
        )
    if recall_evidence is None:
        return _check(
            CHECK_RECALL_ACTIVITY,
            "unknown",
            "RECALL_EVIDENCE_UNAVAILABLE",
            "Recall evidence unavailable (database inspection failed).",
            evidence={"error_type": db_error},
        )
    total = recall_evidence["startup_count"] + recall_evidence["semantic_count"]
    evidence = dict(recall_evidence)
    if total == 0:
        return _check(
            CHECK_RECALL_ACTIVITY,
            "warn",
            "NO_RECALL_ACTIVITY",
            "No recall activity observed in the window.",
            evidence=evidence,
        )
    return _check(
        CHECK_RECALL_ACTIVITY,
        "pass",
        "RECALL_ACTIVITY_RECENT",
        f"{total} recall(s) observed in the window.",
        evidence=evidence,
    )


def _check_receipts_activity(
    *,
    settings_obj: Any,
    receipts_evidence: dict[str, Any] | None,
    tenant_unresolved: bool,
    db_error: str | None,
) -> DoctorCheck:
    """Evaluate receipt integrity for the requested window.

    Precedence (ENG-LOOP-001A-FIX1 / FIX-5): stored-artifact corruption of the
    latest IN-WINDOW receipt is reported as a failure regardless of whether
    new receipt dark writes happen to be enabled on THIS process right now —
    local write configuration must never hide an existing integrity defect.
    ``RECEIPTS_DISABLED`` is reached only once no invalid/unverifiable
    receipt has been detected.
    """
    dark_write_enabled = bool(settings_obj.context_receipt_dark_write_enabled)
    if tenant_unresolved:
        return _check(
            CHECK_RECEIPTS_ACTIVITY,
            "unknown",
            TENANT_SCOPE_UNRESOLVED,
            "Tenant scope could not be resolved; receipt evidence cannot be scoped.",
            evidence={"dark_write_enabled": dark_write_enabled},
        )
    if receipts_evidence is None:
        return _check(
            CHECK_RECEIPTS_ACTIVITY,
            "unknown",
            "RECEIPTS_EVIDENCE_UNAVAILABLE",
            "Receipt evidence unavailable (database inspection failed).",
            evidence={"dark_write_enabled": dark_write_enabled, "error_type": db_error},
        )
    evidence: dict[str, Any] = {"dark_write_enabled": dark_write_enabled, **receipts_evidence}
    if receipts_evidence["latest_verification_status"] == "invalid":
        return _check(
            CHECK_RECEIPTS_ACTIVITY,
            "fail",
            "LATEST_RECEIPT_INVALID",
            "The latest Context Receipt failed deterministic verification.",
            evidence=evidence,
            remediation=[
                "Investigate the receipt repository/verification path for data corruption."
            ],
        )
    if receipts_evidence.get("latest_verification_error"):
        return _check(
            CHECK_RECEIPTS_ACTIVITY,
            "unknown",
            "LATEST_RECEIPT_UNVERIFIABLE",
            "The latest Context Receipt could not be verified (not a confirmed "
            "corruption finding).",
            evidence=evidence,
        )
    if not dark_write_enabled:
        return _check(
            CHECK_RECEIPTS_ACTIVITY,
            "warn",
            "RECEIPTS_DISABLED",
            "Context Receipt dark writes are disabled on this process.",
            evidence=evidence,
            remediation=[
                "Set ENGRAM_CONTEXT_RECEIPT_DARK_WRITE_ENABLED=true to enable receipt writes."
            ],
        )
    if receipts_evidence["startup_recall_count"] == 0:
        return _check(
            CHECK_RECEIPTS_ACTIVITY,
            "unknown",
            "NO_STARTUP_RECALL_TO_ASSESS",
            "No startup recalls in the window to assess receipt creation.",
            evidence=evidence,
        )
    if (
        receipts_evidence["receipt_count"] == 0
        or receipts_evidence["unmatched_startup_recall_count"] > 0
    ):
        return _check(
            CHECK_RECEIPTS_ACTIVITY,
            "warn",
            "RECEIPT_GAP_DETECTED",
            "Startup recalls occurred without a matching Context Receipt "
            "(receipt writes are fail-open).",
            evidence=evidence,
        )
    return _check(
        CHECK_RECEIPTS_ACTIVITY,
        "pass",
        "RECEIPTS_HEALTHY",
        "Startup recalls have matching, valid Context Receipts.",
        evidence=evidence,
    )


# --- Session-factory construction --------------------------------------------


def _build_session_factory(
    database_url: str | None,
) -> tuple[Callable[[], Any], AsyncEngine | None]:
    """Build a session factory, plus the ad hoc engine (if any) that backs it.

    ``database_url is None`` reuses the process's already-configured,
    caller-owned ``owner_session_factory`` — the second element is ``None``
    so the caller never disposes an engine it does not own. An explicit
    ``database_url`` builds a one-shot engine for this run only; the caller
    is responsible for disposing it exactly once (ENG-LOOP-001A-FIX1 /
    FIX-7), since nothing else references or reuses it.
    """
    if database_url is None:
        from engram.db import owner_session_factory

        return owner_session_factory, None
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(
        normalize_database_url(database_url), echo=False, pool_size=1, max_overflow=0
    )
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False), engine


def _default_clock() -> datetime:
    return datetime.now(UTC)


def _default_settings() -> Any:
    from engram.config import settings

    return settings


# --- Orchestrator -------------------------------------------------------------


@dataclass(frozen=True)
class _DbEvidence:
    snapshot: OperationalSnapshot | None
    job_health: dict[str, Any] | None
    lifecycle_extra: dict[str, Any] | None
    recall_evidence: dict[str, Any] | None
    receipts_evidence: dict[str, Any] | None
    error_type: str | None


async def _gather_db_evidence(
    session_factory: Callable[[], Any],
    *,
    tenant_id: str | None,
    since: datetime,
    until: datetime,
    now: datetime,
    lease_stale_after_seconds: int,
    timeout_seconds: float,
) -> _DbEvidence:
    try:
        # One bounded deadline covers session acquisition, transaction setup,
        # every evidence query, and rollback — a session factory whose async
        # context entry itself stalls (not just a slow query) can otherwise
        # hang doctor indefinitely (ENG-LOOP-001A-FIX1 / FIX-6).
        async with asyncio.timeout(timeout_seconds):
            async with session_factory() as session:
                if session.bind is not None and session.bind.dialect.name == "postgresql":
                    await session.execute(text("SET TRANSACTION READ ONLY"))
                    # Bound every statement in this transaction so a hung/slow
                    # query cannot hang doctor indefinitely, matching the HTTP
                    # client's own --timeout-seconds bound.
                    await session.execute(
                        text("SELECT set_config('statement_timeout', :ms, true)"),
                        {"ms": str(seconds_to_statement_timeout_ms(timeout_seconds))},
                    )

                snapshot, err1 = await _isolated(
                    session,
                    build_operational_snapshot(
                        session, tenant_id=tenant_id, since=since, until=until
                    ),
                )
                job_health, err2 = await _isolated(
                    session,
                    _job_health_counts(
                        session,
                        tenant_id=tenant_id,
                        lease_stale_after_seconds=lease_stale_after_seconds,
                        now=now,
                    ),
                )
                lifecycle_extra, err3 = await _isolated(
                    session,
                    _lifecycle_summary_extra(
                        session, tenant_id=tenant_id, since=since, until=until
                    ),
                )
                recall_evidence, err4 = await _isolated(
                    session,
                    _recall_activity_counts(session, tenant_id=tenant_id, since=since, until=until),
                )
                receipts_evidence, err5 = await _isolated(
                    session,
                    _receipts_activity_evidence(
                        session, tenant_id=tenant_id, since=since, until=until
                    ),
                )
                await session.rollback()
        error_type = next((e for e in (err1, err2, err3, err4, err5) if e), None)
        return _DbEvidence(
            snapshot=snapshot,
            job_health=job_health,
            lifecycle_extra=lifecycle_extra,
            recall_evidence=recall_evidence,
            receipts_evidence=receipts_evidence,
            error_type=error_type,
        )
    except Exception as exc:  # noqa: BLE001 - DB inspection must never abort the report
        # Never surface str(exc): a connection/provider/timeout exception can
        # embed a DSN, hostname, username, or password.
        return _DbEvidence(
            snapshot=None,
            job_health=None,
            lifecycle_extra=None,
            recall_evidence=None,
            receipts_evidence=None,
            error_type=type(exc).__name__,
        )


async def run_doctor(
    *,
    base_url: str,
    api_key: str | None = None,
    tenant: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    database_url: str | None = None,
    http_transport: httpx.BaseTransport | None = None,
    session_factory: Callable[[], Any] | None = None,
    clock: Callable[[], datetime] | None = None,
    settings_obj: Any | None = None,
) -> DoctorReport:
    """Build the full doctor report. Never mutates memory, config, or queues.

    Every dependency (HTTP transport, DB session factory, clock, settings,
    timeout) is injectable so tests can exercise this without a live service
    or database.
    """
    timeout_seconds = validate_timeout_seconds(timeout_seconds)
    now = (clock or _default_clock)()
    resolved_since, resolved_until = resolve_window(since=since, until=until, now=now)
    effective_settings = settings_obj if settings_obj is not None else _default_settings()
    one_shot_engine: AsyncEngine | None = None
    if session_factory is not None:
        factory = session_factory
    else:
        factory, one_shot_engine = _build_session_factory(database_url)

    try:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        client_kwargs: dict[str, Any] = {
            "base_url": base_url,
            "timeout": timeout_seconds,
            "headers": headers,
        }
        if http_transport is not None:
            client_kwargs["transport"] = http_transport

        checks: list[DoctorCheck] = []
        async with httpx.AsyncClient(**client_kwargs) as client:
            checks.append(await _check_service_health(client))
            checks.append(await _check_service_readiness(client))

            identity_check, effective_tenant, tenant_source = await _check_identity(
                client, requested_tenant=tenant
            )
            checks.append(identity_check)
            tenant_unresolved = effective_tenant is None

            db_evidence = await _gather_db_evidence(
                factory,
                tenant_id=effective_tenant,
                since=resolved_since,
                until=resolved_until,
                now=now,
                lease_stale_after_seconds=effective_settings.job_lease_stale_after_seconds,
                timeout_seconds=timeout_seconds,
            )

            checks.append(
                _check_config_embeddings(effective_settings, snapshot=db_evidence.snapshot)
            )
            checks.append(_check_config_classification(effective_settings))
            checks.append(
                _check_worker_queue(
                    snapshot=db_evidence.snapshot,
                    job_health=db_evidence.job_health,
                    tenant_unresolved=tenant_unresolved,
                    db_error=db_evidence.error_type,
                    lease_stale_after_seconds=effective_settings.job_lease_stale_after_seconds,
                )
            )
            checks.append(
                _check_capture_lifecycle(
                    settings_obj=effective_settings,
                    snapshot=db_evidence.snapshot,
                    lifecycle_extra=db_evidence.lifecycle_extra,
                    tenant_unresolved=tenant_unresolved,
                    db_error=db_evidence.error_type,
                )
            )
            checks.append(
                _check_capture_remember(
                    settings_obj=effective_settings,
                    snapshot=db_evidence.snapshot,
                    tenant_unresolved=tenant_unresolved,
                    db_error=db_evidence.error_type,
                )
            )
            checks.append(
                _check_recall_activity(
                    recall_evidence=db_evidence.recall_evidence,
                    tenant_unresolved=tenant_unresolved,
                    db_error=db_evidence.error_type,
                )
            )
            checks.append(
                _check_receipts_activity(
                    settings_obj=effective_settings,
                    receipts_evidence=db_evidence.receipts_evidence,
                    tenant_unresolved=tenant_unresolved,
                    db_error=db_evidence.error_type,
                )
            )
            checks.append(await _check_review_backlog(client))

        overall_status, exit_code = aggregate_status(checks)
        return DoctorReport(
            engram_version=__version__,
            generated_at=now,
            window=DoctorWindow(since=resolved_since, until=resolved_until),
            scope=DoctorScope(tenant_id=effective_tenant, source=tenant_source),
            overall_status=overall_status,
            exit_code=exit_code,
            checks=checks,
            limitations=list(REQUIRED_LIMITATIONS),
        )
    finally:
        # Only an engine THIS call constructed (an explicit --database-url,
        # never the injected/global owner_session_factory) is ours to
        # dispose. Disposed exactly once, on every path: success, a raised
        # exception, or the internal DB-evidence timeout (ENG-LOOP-001A-FIX1
        # / FIX-7).
        if one_shot_engine is not None:
            await one_shot_engine.dispose()


# --- Rendering -----------------------------------------------------------------


def render_human(report: DoctorReport) -> str:
    """Concise human-readable rendering: one overall line, one line per check."""
    lines = [
        f"engram doctor — overall_status={report.overall_status} exit_code={report.exit_code}",
        f"schema={report.schema_} v{report.schema_version} profile={report.profile} "
        f"engram_version={report.engram_version}",
        f"generated_at={report.generated_at.isoformat()}",
        f"window: {report.window.since.isoformat()} .. {report.window.until.isoformat()}",
        f"scope: tenant_id={report.scope.tenant_id or '(unresolved)'} source={report.scope.source}",
        "",
    ]
    for check in report.checks:
        lines.append(
            f"[{check.status.upper():7s}] {check.id:24s} {check.reason_code:32s} {check.summary}"
        )
        for action in check.remediation:
            lines.append(f"           -> {action}")
    if report.limitations:
        lines.append("")
        lines.append("Limitations:")
        for limitation in report.limitations:
            lines.append(f"  - {limitation}")
    return "\n".join(lines)
