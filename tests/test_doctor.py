"""Unit, contract, redaction, and CLI tests for ``engram doctor`` (ENG-LOOP-001A).

These tests use ``httpx.MockTransport`` (the repository's established pattern —
see ``tests/test_eng_audit_002b_recall_benchmark.py`` and
``tests/test_denied_profile_preflight.py``) and do not require a live service
or database. Real-PostgreSQL proofs (worker/lifecycle/recall/receipt evidence,
no-mutation, cross-tenant isolation) live in ``tests/test_doctor_postgres.py``.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from engram.cli import _run_doctor
from engram.doctor import (
    CHECK_ORDER,
    DEFAULT_TIMEOUT_SECONDS,
    DoctorCheck,
    DoctorReport,
    DoctorScope,
    DoctorWindow,
    _build_session_factory,
    _check_capture_remember,
    _check_identity,
    _check_receipts_activity,
    _check_review_backlog,
    _check_service_health,
    _check_service_readiness,
    _dispose_owned_engine_safely,
    _prepare_database_resource,
    aggregate_status,
    normalize_database_url,
    parse_iso8601,
    render_human,
    resolve_base_url,
    resolve_database_url,
    resolve_window,
    run_doctor,
    seconds_to_statement_timeout_ms,
    validate_timeout_seconds,
)
from engram.usage_report import OperationalSnapshot

FIXED_NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)


class _TelemetryEnabledSettings:
    usage_telemetry_enabled = True


def _snapshot_with_funnel(funnel: dict[str, Any]) -> OperationalSnapshot:
    base_funnel = {
        "candidate_observations": 0, "created": 0, "deduped": 0, "superseded": 0,
        "failed": 0, "unresolved_candidates": 0, "candidate_ingests_unresolved": 0,
        "lifecycle_extracted": 0, "time_to_first_success_ms_p50": None,
        "time_to_first_success_ms_p90": None,
    }
    base_funnel.update(funnel)
    return OperationalSnapshot(
        tenant_id="tenant-a", since=FIXED_NOW, until=FIXED_NOW,
        coverage={}, candidate_funnel=base_funnel, worker={}, storage={},
    )


def _pass(check_id: str) -> DoctorCheck:
    return DoctorCheck(id=check_id, status="pass", reason_code="OK", summary="fine")


def _make_full_report(statuses: dict[str, str]) -> DoctorReport:
    checks = []
    for check_id in CHECK_ORDER:
        status = statuses.get(check_id, "pass")
        checks.append(
            DoctorCheck(
                id=check_id,
                status=status,
                reason_code="OK" if status == "pass" else "SOME_ISSUE",
                summary=f"{check_id} is {status}",
                remediation=["do something"] if status != "pass" else [],
            )
        )
    overall, exit_code = aggregate_status(checks)
    return DoctorReport(
        engram_version="0.1.0",
        generated_at=FIXED_NOW,
        window=DoctorWindow(since=FIXED_NOW, until=FIXED_NOW),
        scope=DoctorScope(tenant_id=None, source="deployment"),
        overall_status=overall,
        exit_code=exit_code,
        checks=checks,
        limitations=["some limitation"],
    )


# --- Pure validators ----------------------------------------------------------


def test_validate_timeout_seconds_accepts_positive():
    assert validate_timeout_seconds(5) == 5.0
    assert validate_timeout_seconds(0.5) == 0.5


@pytest.mark.parametrize("value", [0, -1, -0.001])
def test_validate_timeout_seconds_rejects_nonpositive(value: float):
    with pytest.raises(ValueError, match="finite, strictly positive"):
        validate_timeout_seconds(value)


def test_validate_timeout_seconds_rejects_nan():
    with pytest.raises(ValueError, match="finite, strictly positive"):
        validate_timeout_seconds(math.nan)


def test_validate_timeout_seconds_rejects_infinity():
    with pytest.raises(ValueError, match="finite, strictly positive"):
        validate_timeout_seconds(math.inf)
    with pytest.raises(ValueError, match="finite, strictly positive"):
        validate_timeout_seconds(-math.inf)


def test_validate_timeout_seconds_rejects_bool():
    with pytest.raises(ValueError, match="numeric"):
        validate_timeout_seconds(True)  # type: ignore[arg-type]


def test_seconds_to_statement_timeout_ms_sub_millisecond_never_becomes_zero():
    """A validated positive sub-ms timeout must never round down to 0 (no timeout)."""
    assert seconds_to_statement_timeout_ms(0.0001) == 1
    assert seconds_to_statement_timeout_ms(0.0000001) == 1


def test_seconds_to_statement_timeout_ms_exact_second():
    assert seconds_to_statement_timeout_ms(1.0) == 1000


def test_seconds_to_statement_timeout_ms_rounds_up_not_down():
    assert seconds_to_statement_timeout_ms(1.0001) == 1001


def test_parse_iso8601_requires_tzaware():
    with pytest.raises(ValueError, match="timezone-aware"):
        parse_iso8601("2026-07-25T12:00:00", param_name="--since")


def test_parse_iso8601_rejects_bad_format():
    with pytest.raises(ValueError, match="valid ISO-8601"):
        parse_iso8601("not-a-date", param_name="--since")


def test_parse_iso8601_accepts_tzaware_and_normalizes_utc():
    dt = parse_iso8601("2026-07-25T12:00:00+02:00", param_name="--since")
    assert dt.tzinfo == UTC
    assert dt.hour == 10


def test_resolve_window_defaults_24h_before_now():
    since, until = resolve_window(since=None, until=None, now=FIXED_NOW)
    assert until == FIXED_NOW
    assert (until - since).total_seconds() == 24 * 3600


def test_resolve_window_defaults_until_to_now_when_since_given():
    since_arg = datetime(2026, 7, 20, tzinfo=UTC)
    since, until = resolve_window(since=since_arg, until=None, now=FIXED_NOW)
    assert since == since_arg
    assert until == FIXED_NOW


def test_resolve_window_rejects_since_after_until():
    since_arg = datetime(2026, 7, 26, tzinfo=UTC)
    until_arg = datetime(2026, 7, 25, tzinfo=UTC)
    with pytest.raises(ValueError, match="strictly before"):
        resolve_window(since=since_arg, until=until_arg, now=FIXED_NOW)


def test_resolve_window_rejects_since_equal_until():
    same = datetime(2026, 7, 25, tzinfo=UTC)
    with pytest.raises(ValueError, match="strictly before"):
        resolve_window(since=same, until=same, now=FIXED_NOW)


def test_resolve_window_rejects_naive_datetime():
    naive = datetime(2026, 7, 20)  # noqa: DTZ001 - deliberately naive for the test
    with pytest.raises(ValueError, match="timezone-aware"):
        resolve_window(since=naive, until=None, now=FIXED_NOW)


def test_resolve_base_url_prefers_explicit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ENGRAM_BASE_URL", raising=False)

    class _S:
        port = 9999

    assert resolve_base_url("http://explicit:1", settings_obj=_S()) == "http://explicit:1"


def test_resolve_base_url_falls_back_to_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ENGRAM_BASE_URL", "http://from-env:2")

    class _S:
        port = 9999

    assert resolve_base_url(None, settings_obj=_S()) == "http://from-env:2"


def test_resolve_base_url_falls_back_to_loopback_port(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ENGRAM_BASE_URL", raising=False)

    class _S:
        port = 8123

    assert resolve_base_url(None, settings_obj=_S()) == "http://127.0.0.1:8123"


def test_resolve_database_url_precedence(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ENGRAM_OWNER_DATABASE_URL", "postgresql://owner/db")
    monkeypatch.setenv("ENGRAM_DATABASE_URL", "postgresql://app/db")
    assert resolve_database_url(None) == "postgresql://owner/db"
    assert resolve_database_url("postgresql://explicit/db") == "postgresql://explicit/db"

    monkeypatch.delenv("ENGRAM_OWNER_DATABASE_URL")
    assert resolve_database_url(None) == "postgresql://app/db"

    monkeypatch.delenv("ENGRAM_DATABASE_URL")
    assert resolve_database_url(None) is None


def test_normalize_database_url_rewrites_bare_postgresql_scheme():
    """A bare postgresql:// must become postgresql+asyncpg:// (ENG-LOOP-001A-FIX1)."""
    assert (
        normalize_database_url("postgresql://user:pw@host:5432/db")
        == "postgresql+asyncpg://user:pw@host:5432/db"
    )


def test_normalize_database_url_passes_through_explicit_asyncpg_scheme():
    url = "postgresql+asyncpg://user:pw@host:5432/db"
    assert normalize_database_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://user:pw@host:5432/db",
        "postgresql+psycopg2://user:pw@host:5432/db",
        "mysql://user:pw@host:3306/db",
        "sqlite:///local.db",
    ],
)
def test_normalize_database_url_rejects_unsupported_schemes(url: str):
    """Only postgresql:// and postgresql+asyncpg:// are accepted (ENG-LOOP-001A-FIX2)."""
    with pytest.raises(ValueError, match="postgresql"):
        normalize_database_url(url)


def test_normalize_database_url_rejects_unparseable_url():
    with pytest.raises(ValueError):
        normalize_database_url("not a url at all ###")


def test_normalize_database_url_rejection_never_echoes_the_input():
    sentinel = "postgresql+psycopg2://SENTINEL_USER:SENTINEL_PW@sentinel-host/db"
    try:
        normalize_database_url(sentinel)
    except ValueError as exc:
        assert "SENTINEL_USER" not in str(exc)
        assert "SENTINEL_PW" not in str(exc)
        assert "sentinel-host" not in str(exc)
    else:
        pytest.fail("expected normalize_database_url to reject an unsupported driver")


def test_normalize_database_url_preserves_percent_encoded_credentials_and_query():
    url = "postgresql://us%40er:p%40ss@host:5432/db?sslmode=require"
    normalized = normalize_database_url(url)
    assert normalized == "postgresql+asyncpg://us%40er:p%40ss@host:5432/db?sslmode=require"


def test_normalize_database_url_preserves_ipv6_host():
    url = "postgresql+asyncpg://[::1]:5432/db"
    assert normalize_database_url(url) == url


async def test_build_session_factory_normalizes_and_returns_disposable_engine():
    """An explicit --database-url builds a one-shot, normalized, disposable engine."""
    factory, engine = _build_session_factory("postgresql://user:pw@host:5432/db")
    try:
        assert engine is not None
        assert engine.url.drivername == "postgresql+asyncpg"
        assert callable(factory)
    finally:
        await engine.dispose()


def test_build_session_factory_reuses_global_factory_with_no_engine_to_dispose():
    """``database_url=None`` reuses the process-owned factory; nothing to dispose."""
    factory, engine = _build_session_factory(None)
    assert engine is None
    assert callable(factory)


class _DisposeTrackingEngine:
    def __init__(self) -> None:
        self.dispose_calls = 0

    async def dispose(self) -> None:
        self.dispose_calls += 1


class _ImmediatelyFailingSession:
    """A fake AsyncSession-context-manager whose __aenter__ always raises."""

    async def __aenter__(self) -> _ImmediatelyFailingSession:
        raise RuntimeError("sentinel DB failure")

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


async def test_run_doctor_disposes_one_shot_engine_exactly_once_on_db_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A one-shot engine built from --database-url must be disposed exactly once.

    Even when every DB-evidence query fails, disposal must still happen — the
    engine this run constructed is never leaked (ENG-LOOP-001A-FIX1 / FIX-7).
    """
    fake_engine = _DisposeTrackingEngine()

    def _fake_build_session_factory(database_url: str | None) -> tuple[Any, Any]:
        assert database_url == "postgresql://explicit-host/db"
        return (lambda: _ImmediatelyFailingSession()), fake_engine

    monkeypatch.setattr("engram.doctor._build_session_factory", _fake_build_session_factory)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/ready":
            return httpx.Response(200, json={"status": "ready", "database": "connected"})
        if path == "/whoami":
            return httpx.Response(
                200,
                json={
                    "principal_id": "22222222-2222-2222-2222-222222222222",
                    "principal_type": "admin",
                    "tenant_id": "11111111-1111-1111-1111-111111111111",
                    "scopes": ["admin"], "api_key_id": "k", "memory_profile": None,
                },
            )
        if path == "/v1/review/stats":
            return httpx.Response(
                200,
                json={"by_review_status": {}, "by_kind": {}, "by_confidence": {}, "total": 0},
            )
        if path == "/v1/review/queue":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})

    report = await run_doctor(
        base_url="http://test",
        database_url="postgresql://explicit-host/db",
        http_transport=httpx.MockTransport(handler),
        clock=lambda: FIXED_NOW,
    )
    assert fake_engine.dispose_calls == 1
    by_id = {c.id: c for c in report.checks}
    assert by_id["worker.queue"].status == "unknown"


async def test_run_doctor_disposes_one_shot_engine_exactly_once_on_raised_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disposal must also happen when report construction itself raises."""
    fake_engine = _DisposeTrackingEngine()

    def _fake_build_session_factory(database_url: str | None) -> tuple[Any, Any]:
        return (lambda: _ImmediatelyFailingSession()), fake_engine

    monkeypatch.setattr("engram.doctor._build_session_factory", _fake_build_session_factory)

    def _explode(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("transport exploded")

    with pytest.raises(RuntimeError, match="transport exploded"):
        await run_doctor(
            base_url="http://test",
            database_url="postgresql://explicit-host/db",
            http_transport=httpx.MockTransport(_explode),
            clock=lambda: FIXED_NOW,
        )
    assert fake_engine.dispose_calls == 1


async def test_run_doctor_does_not_call_build_session_factory_when_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller-injected session_factory bypasses one-shot engine construction.

    ``_build_session_factory`` (and thus ``create_async_engine``) must never
    run when a session_factory is injected — there is nothing for run_doctor
    to own or dispose in this path.
    """

    def _fail_if_called(database_url: str | None) -> tuple[Any, Any]:
        raise AssertionError(
            "_build_session_factory must not be called when session_factory is injected"
        )

    monkeypatch.setattr("engram.doctor._build_session_factory", _fail_if_called)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/ready":
            return httpx.Response(200, json={"status": "ready", "database": "connected"})
        if path == "/whoami":
            return httpx.Response(
                200,
                json={
                    "principal_id": "22222222-2222-2222-2222-222222222222",
                    "principal_type": "admin",
                    "tenant_id": "11111111-1111-1111-1111-111111111111",
                    "scopes": ["admin"], "api_key_id": "k", "memory_profile": None,
                },
            )
        if path == "/v1/review/stats":
            return httpx.Response(
                200,
                json={"by_review_status": {}, "by_kind": {}, "by_confidence": {}, "total": 0},
            )
        if path == "/v1/review/queue":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})

    report = await run_doctor(
        base_url="http://test",
        http_transport=httpx.MockTransport(handler),
        session_factory=lambda: _ImmediatelyFailingSession(),
        clock=lambda: FIXED_NOW,
    )
    by_id = {c.id: c for c in report.checks}
    assert by_id["service.health"].status == "pass"
    assert by_id["worker.queue"].status == "unknown"


# --- FIX2-1: safe DB resource construction + bounded disposal -----------------


def test_prepare_database_resource_malformed_url_is_unavailable_not_raised():
    resource = _prepare_database_resource("not a url at all ###", injected_factory=None)
    assert resource.session_factory is None
    assert resource.owned_engine is None
    assert resource.error_type == "DATABASE_RESOURCE_UNAVAILABLE"


def test_prepare_database_resource_unsupported_scheme_is_unavailable():
    resource = _prepare_database_resource(
        "postgresql+psycopg2://user:pw@host/db", injected_factory=None
    )
    assert resource.session_factory is None
    assert resource.owned_engine is None
    assert resource.error_type == "DATABASE_RESOURCE_UNAVAILABLE"


def test_prepare_database_resource_engine_construction_failure_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
):
    def _raise_with_sentinel(database_url: str | None) -> tuple[Any, Any]:
        raise RuntimeError("connect failed: postgresql://SENTINEL_USER:SENTINEL_PW@sentinel-host/db")

    monkeypatch.setattr("engram.doctor._build_session_factory", _raise_with_sentinel)
    resource = _prepare_database_resource("postgresql://x/y", injected_factory=None)
    assert resource.session_factory is None
    assert resource.owned_engine is None
    assert resource.error_type == "DATABASE_RESOURCE_UNAVAILABLE"


def test_prepare_database_resource_injected_factory_bypasses_construction(
    monkeypatch: pytest.MonkeyPatch,
):
    def _fail_if_called(database_url: str | None) -> tuple[Any, Any]:
        raise AssertionError("_build_session_factory must not be called when injected")

    monkeypatch.setattr("engram.doctor._build_session_factory", _fail_if_called)
    sentinel_factory = object()
    resource = _prepare_database_resource("postgresql://x/y", injected_factory=sentinel_factory)
    assert resource.session_factory is sentinel_factory
    assert resource.owned_engine is None
    assert resource.error_type is None


def _healthy_doctor_handler() -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/ready":
            return httpx.Response(200, json={"status": "ready", "database": "connected"})
        if path == "/whoami":
            return httpx.Response(
                200,
                json={
                    "principal_id": "22222222-2222-2222-2222-222222222222",
                    "principal_type": "admin",
                    "tenant_id": "11111111-1111-1111-1111-111111111111",
                    "scopes": ["admin"], "api_key_id": "k", "memory_profile": None,
                },
            )
        if path == "/v1/review/stats":
            return httpx.Response(
                200,
                json={"by_review_status": {}, "by_kind": {}, "by_confidence": {}, "total": 0},
            )
        if path == "/v1/review/queue":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})

    return handler


@pytest.mark.parametrize(
    "bad_database_url",
    [
        "not a url at all ###",
        "mysql://user:pw@host:3306/db",
        "postgresql+psycopg://user:pw@host:5432/db",
    ],
)
async def test_run_doctor_bad_database_url_produces_complete_report(bad_database_url: str):
    """A malformed/unsupported --database-url must never abort report construction.

    Every one of the 11 checks must still be emitted: HTTP-only checks pass
    normally, DB-backed checks degrade to unknown (ENG-LOOP-001A-FIX2 /
    FIX2-1).
    """
    report = await run_doctor(
        base_url="http://test",
        database_url=bad_database_url,
        http_transport=httpx.MockTransport(_healthy_doctor_handler()),
        clock=lambda: FIXED_NOW,
    )
    ids = [c.id for c in report.checks]
    assert ids == list(CHECK_ORDER)
    by_id = {c.id: c for c in report.checks}
    assert by_id["service.health"].status == "pass"
    assert by_id["service.readiness"].status == "pass"
    assert by_id["identity.scopes"].status == "pass"
    assert by_id["config.classification"].status in ("pass", "warn")
    assert by_id["review.backlog"].status == "pass"
    for db_check_id in (
        "worker.queue", "capture.lifecycle", "capture.remember",
        "recall.activity", "receipts.activity",
    ):
        assert by_id[db_check_id].status == "unknown"


async def test_run_doctor_engine_construction_failure_with_sentinels_leaks_nothing(
    monkeypatch: pytest.MonkeyPatch,
):
    def _raise_with_sentinels(database_url: str | None) -> tuple[Any, Any]:
        raise RuntimeError(
            "connect failed: postgresql://SENTINEL_DB_USER:SENTINEL_DB_PW@sentinel-db-host/x"
        )

    monkeypatch.setattr("engram.doctor._build_session_factory", _raise_with_sentinels)
    report = await run_doctor(
        base_url="http://test",
        database_url="postgresql://x/y",
        http_transport=httpx.MockTransport(_healthy_doctor_handler()),
        clock=lambda: FIXED_NOW,
    )
    ids = [c.id for c in report.checks]
    assert ids == list(CHECK_ORDER)
    dumped = report.model_dump_json(by_alias=True)
    assert "SENTINEL_DB_USER" not in dumped
    assert "SENTINEL_DB_PW" not in dumped
    assert "sentinel-db-host" not in dumped


async def test_dispose_owned_engine_safely_noop_when_none():
    await _dispose_owned_engine_safely(None, 1.0)


async def test_dispose_owned_engine_safely_suppresses_sentinel_exception():
    class _ExplodingEngine:
        async def dispose(self) -> None:
            raise RuntimeError("dispose failed: SENTINEL_DISPOSE_SECRET")

    # Must not raise.
    await _dispose_owned_engine_safely(_ExplodingEngine(), 1.0)


async def test_dispose_owned_engine_safely_bounds_a_stalled_dispose():
    class _StallingEngine:
        async def dispose(self) -> None:
            await asyncio.sleep(5.0)

    started = time.monotonic()
    await _dispose_owned_engine_safely(_StallingEngine(), 0.2)
    elapsed = time.monotonic() - started
    assert elapsed < 4.0


async def test_run_doctor_disposal_exception_does_not_alter_completed_report(
    monkeypatch: pytest.MonkeyPatch,
):
    """A disposal exception must not prevent or alter an otherwise-complete report."""

    class _ExplodingDisposeEngine:
        async def dispose(self) -> None:
            raise RuntimeError("dispose failed: SENTINEL_DISPOSE_SECRET")

    def _fake_build_session_factory(database_url: str | None) -> tuple[Any, Any]:
        return (lambda: _ImmediatelyFailingSession()), _ExplodingDisposeEngine()

    monkeypatch.setattr("engram.doctor._build_session_factory", _fake_build_session_factory)
    report = await run_doctor(
        base_url="http://test",
        database_url="postgresql://x/y",
        http_transport=httpx.MockTransport(_healthy_doctor_handler()),
        clock=lambda: FIXED_NOW,
    )
    ids = [c.id for c in report.checks]
    assert ids == list(CHECK_ORDER)
    dumped = report.model_dump_json(by_alias=True)
    assert "SENTINEL_DISPOSE_SECRET" not in dumped


async def test_run_doctor_stalled_disposal_does_not_hang_and_still_returns(
    monkeypatch: pytest.MonkeyPatch,
):
    """A disposal that stalls beyond the bound must not hang run_doctor."""

    class _StallingDisposeEngine:
        async def dispose(self) -> None:
            await asyncio.sleep(5.0)

    def _fake_build_session_factory(database_url: str | None) -> tuple[Any, Any]:
        return (lambda: _ImmediatelyFailingSession()), _StallingDisposeEngine()

    monkeypatch.setattr("engram.doctor._build_session_factory", _fake_build_session_factory)
    started = time.monotonic()
    report = await run_doctor(
        base_url="http://test",
        database_url="postgresql://x/y",
        timeout_seconds=0.2,
        http_transport=httpx.MockTransport(_healthy_doctor_handler()),
        clock=lambda: FIXED_NOW,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 4.0
    ids = [c.id for c in report.checks]
    assert ids == list(CHECK_ORDER)


# --- Status aggregation / exit codes ------------------------------------------


def test_aggregate_status_all_pass_is_healthy():
    checks = [_pass(cid) for cid in CHECK_ORDER]
    assert aggregate_status(checks) == ("healthy", 0)


def test_aggregate_status_warn_is_degraded():
    checks = [_pass(cid) for cid in CHECK_ORDER]
    checks[3] = DoctorCheck(id=CHECK_ORDER[3], status="warn", reason_code="X", summary="x")
    assert aggregate_status(checks) == ("degraded", 1)


def test_aggregate_status_unknown_is_degraded():
    checks = [_pass(cid) for cid in CHECK_ORDER]
    checks[3] = DoctorCheck(id=CHECK_ORDER[3], status="unknown", reason_code="X", summary="x")
    assert aggregate_status(checks) == ("degraded", 1)


def test_aggregate_status_fail_is_unhealthy_even_with_warn():
    checks = [_pass(cid) for cid in CHECK_ORDER]
    checks[1] = DoctorCheck(id=CHECK_ORDER[1], status="warn", reason_code="X", summary="x")
    checks[3] = DoctorCheck(id=CHECK_ORDER[3], status="fail", reason_code="X", summary="x")
    assert aggregate_status(checks) == ("unhealthy", 2)


# --- Report/check model validation --------------------------------------------


def test_reason_code_must_be_uppercase():
    with pytest.raises(ValidationError, match="uppercase"):
        DoctorCheck(id="x", status="pass", reason_code="not_upper", summary="x")


def test_report_enforces_check_order():
    checks = [_pass(cid) for cid in CHECK_ORDER]
    checks[0], checks[1] = checks[1], checks[0]
    with pytest.raises(ValidationError, match="required order"):
        DoctorReport(
            engram_version="0.1.0",
            generated_at=FIXED_NOW,
            window=DoctorWindow(since=FIXED_NOW, until=FIXED_NOW),
            scope=DoctorScope(tenant_id=None, source="deployment"),
            overall_status="healthy",
            exit_code=0,
            checks=checks,
            limitations=[],
        )


def test_report_enforces_overall_status_consistency():
    checks = [_pass(cid) for cid in CHECK_ORDER]
    with pytest.raises(ValidationError, match="inconsistent"):
        DoctorReport(
            engram_version="0.1.0",
            generated_at=FIXED_NOW,
            window=DoctorWindow(since=FIXED_NOW, until=FIXED_NOW),
            scope=DoctorScope(tenant_id=None, source="deployment"),
            overall_status="unhealthy",  # wrong: all checks pass
            exit_code=2,
            checks=checks,
            limitations=[],
        )


def test_report_schema_field_serializes_as_schema():
    report = _make_full_report({})
    dumped = report.model_dump(by_alias=True)
    assert dumped["schema"] == "engram.doctor"
    assert dumped["schema_version"] == "1.2"
    assert "schema_" not in dumped


# --- Human rendering -----------------------------------------------------------


def test_render_human_healthy():
    report = _make_full_report({})
    text = render_human(report)
    assert "overall_status=healthy exit_code=0" in text
    for check_id in CHECK_ORDER:
        assert check_id in text
    assert "Limitations:" in text


def test_render_human_degraded():
    report = _make_full_report({CHECK_ORDER[3]: "warn"})
    text = render_human(report)
    assert "overall_status=degraded exit_code=1" in text
    assert "[WARN   ]" in text
    assert "-> do something" in text


def test_render_human_unhealthy():
    report = _make_full_report({CHECK_ORDER[0]: "fail"})
    text = render_human(report)
    assert "overall_status=unhealthy exit_code=2" in text
    assert "[FAIL   ]" in text


# --- capture.remember status truth table (ENG-LOOP-001A-FIX1 / FIX-3) --------


def test_remember_unresolved_only_warns_not_pass():
    """Unresolved-only candidate activity does not pass."""
    snapshot = _snapshot_with_funnel(
        {"candidate_observations": 1, "unresolved_candidates": 1}
    )
    check = _check_capture_remember(
        settings_obj=_TelemetryEnabledSettings(), snapshot=snapshot,
        tenant_unresolved=False, db_error=None,
    )
    assert check.status == "warn"
    assert check.reason_code == "REMEMBER_OUTCOMES_PENDING"


def test_remember_success_plus_unresolved_does_not_pass():
    """One success plus one unresolved candidate does not pass."""
    snapshot = _snapshot_with_funnel(
        {"candidate_observations": 2, "created": 1, "unresolved_candidates": 1}
    )
    check = _check_capture_remember(
        settings_obj=_TelemetryEnabledSettings(), snapshot=snapshot,
        tenant_unresolved=False, db_error=None,
    )
    assert check.status == "warn"
    assert check.reason_code == "REMEMBER_OUTCOMES_PENDING"


def test_remember_failure_plus_unresolved_is_not_pipeline_failed():
    """A failure plus an unresolved candidate must not claim every attempt failed."""
    snapshot = _snapshot_with_funnel(
        {"candidate_observations": 2, "failed": 1, "unresolved_candidates": 1}
    )
    check = _check_capture_remember(
        settings_obj=_TelemetryEnabledSettings(), snapshot=snapshot,
        tenant_unresolved=False, db_error=None,
    )
    assert check.status == "warn"
    assert check.reason_code == "REMEMBER_OUTCOMES_PENDING"


def test_remember_success_plus_failure_is_partial_failures_warning():
    snapshot = _snapshot_with_funnel(
        {"candidate_observations": 2, "created": 1, "failed": 1}
    )
    check = _check_capture_remember(
        settings_obj=_TelemetryEnabledSettings(), snapshot=snapshot,
        tenant_unresolved=False, db_error=None,
    )
    assert check.status == "warn"
    assert check.reason_code == "REMEMBER_PARTIAL_FAILURES"


def test_remember_success_failure_and_unresolved_mentions_unresolved():
    snapshot = _snapshot_with_funnel(
        {"candidate_observations": 3, "created": 1, "failed": 1, "unresolved_candidates": 1}
    )
    check = _check_capture_remember(
        settings_obj=_TelemetryEnabledSettings(), snapshot=snapshot,
        tenant_unresolved=False, db_error=None,
    )
    assert check.status == "warn"
    assert check.reason_code == "REMEMBER_PARTIAL_FAILURES"
    assert "unresolved" in check.summary


def test_remember_failure_only_no_success_no_unresolved_fails():
    snapshot = _snapshot_with_funnel({"candidate_observations": 1, "failed": 1})
    check = _check_capture_remember(
        settings_obj=_TelemetryEnabledSettings(), snapshot=snapshot,
        tenant_unresolved=False, db_error=None,
    )
    assert check.status == "fail"
    assert check.reason_code == "REMEMBER_PIPELINE_FAILED"


def test_remember_success_only_passes():
    snapshot = _snapshot_with_funnel({"candidate_observations": 1, "created": 1})
    check = _check_capture_remember(
        settings_obj=_TelemetryEnabledSettings(), snapshot=snapshot,
        tenant_unresolved=False, db_error=None,
    )
    assert check.status == "pass"
    assert check.reason_code == "REMEMBER_PIPELINE_HEALTHY"


def test_remember_incomplete_evidence_is_unknown_not_pass():
    """Observations exist but the funnel accounts for no success/failure/unresolved."""
    snapshot = _snapshot_with_funnel({"candidate_observations": 1})
    check = _check_capture_remember(
        settings_obj=_TelemetryEnabledSettings(), snapshot=snapshot,
        tenant_unresolved=False, db_error=None,
    )
    assert check.status == "unknown"
    assert check.reason_code == "REMEMBER_EVIDENCE_INCOMPLETE"


def test_remember_evidence_distinguishes_logical_from_ingest_unresolved():
    snapshot = _snapshot_with_funnel(
        {
            "candidate_observations": 2, "unresolved_candidates": 2,
            "candidate_ingests_unresolved": 1,
        }
    )
    check = _check_capture_remember(
        settings_obj=_TelemetryEnabledSettings(), snapshot=snapshot,
        tenant_unresolved=False, db_error=None,
    )
    assert check.evidence["unresolved_candidates"] == 2
    assert check.evidence["unresolved_ingests"] == 1


# --- receipts.activity precedence (ENG-LOOP-001A-FIX1 / FIX-5) ---------------


class _DarkWriteSettings:
    def __init__(self, enabled: bool) -> None:
        self.context_receipt_dark_write_enabled = enabled


def test_receipts_invalid_receipt_fails_even_when_dark_writes_disabled():
    """Local write configuration must never hide an existing integrity defect."""
    evidence = {
        "startup_recall_count": 1, "receipt_count": 1, "unmatched_startup_recall_count": 0,
        "latest_receipt_at": FIXED_NOW, "latest_verification_status": "invalid",
        "latest_verification_error": None,
    }
    check = _check_receipts_activity(
        settings_obj=_DarkWriteSettings(False), receipts_evidence=evidence,
        tenant_unresolved=False, db_error=None,
    )
    assert check.status == "fail"
    assert check.reason_code == "LATEST_RECEIPT_INVALID"


def test_receipts_unverifiable_is_unknown_not_healthy():
    evidence = {
        "startup_recall_count": 1, "receipt_count": 1, "unmatched_startup_recall_count": 0,
        "latest_receipt_at": FIXED_NOW, "latest_verification_status": None,
        "latest_verification_error": "SomeTransientError",
    }
    check = _check_receipts_activity(
        settings_obj=_DarkWriteSettings(True), receipts_evidence=evidence,
        tenant_unresolved=False, db_error=None,
    )
    assert check.status == "unknown"
    assert check.reason_code == "LATEST_RECEIPT_UNVERIFIABLE"


def test_receipts_unverifiable_takes_precedence_over_disabled():
    evidence = {
        "startup_recall_count": 1, "receipt_count": 1, "unmatched_startup_recall_count": 0,
        "latest_receipt_at": FIXED_NOW, "latest_verification_status": None,
        "latest_verification_error": "SomeTransientError",
    }
    check = _check_receipts_activity(
        settings_obj=_DarkWriteSettings(False), receipts_evidence=evidence,
        tenant_unresolved=False, db_error=None,
    )
    assert check.status == "unknown"
    assert check.reason_code == "LATEST_RECEIPT_UNVERIFIABLE"


def test_receipts_evidence_unavailable_regardless_of_dark_write_setting():
    check = _check_receipts_activity(
        settings_obj=_DarkWriteSettings(False), receipts_evidence=None,
        tenant_unresolved=False, db_error="SomeError",
    )
    assert check.status == "unknown"
    assert check.reason_code == "RECEIPTS_EVIDENCE_UNAVAILABLE"
    assert check.evidence["dark_write_enabled"] is False


# --- Individual check failure does not abort the rest -------------------------


async def test_individual_check_failure_does_not_abort_report():
    """One failing/unreachable check must not prevent later checks from running."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            raise httpx.ConnectError("simulated connect failure", request=request)
        if path == "/ready":
            return httpx.Response(200, json={"status": "ready", "database": "connected"})
        if path == "/whoami":
            return httpx.Response(
                200,
                json={
                    "principal_id": "22222222-2222-2222-2222-222222222222",
                    "principal_type": "admin",
                    "tenant_id": "11111111-1111-1111-1111-111111111111",
                    "scopes": ["admin"],
                    "api_key_id": "should-not-leak",
                    "memory_profile": None,
                },
            )
        if path == "/v1/review/stats":
            return httpx.Response(
                200,
                json={
                    "by_review_status": {"active": 1, "proposed": 0, "disputed": 0},
                    "by_kind": {},
                    "by_confidence": {},
                    "total": 1,
                },
            )
        if path == "/v1/review/queue":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"detail": "no route"})

    transport = httpx.MockTransport(handler)

    def _unreachable_db(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("db unavailable in this unit test")

    report = await run_doctor(
        base_url="http://test",
        http_transport=transport,
        session_factory=_unreachable_db,
        clock=lambda: FIXED_NOW,
    )
    ids = [c.id for c in report.checks]
    assert ids == list(CHECK_ORDER)
    by_id = {c.id: c for c in report.checks}
    # /health failed, but every later check still ran (not omitted, not aborted).
    assert by_id["service.health"].status == "fail"
    assert by_id["service.readiness"].status == "pass"
    assert by_id["identity.scopes"].status == "pass"
    assert by_id["review.backlog"].status == "pass"
    # DB-dependent checks degrade gracefully rather than raising.
    assert by_id["worker.queue"].status == "unknown"
    assert by_id["recall.activity"].status == "unknown"


class _StallingSession:
    """A fake AsyncSession-context-manager whose __aenter__ stalls."""

    def __init__(self, delay_seconds: float) -> None:
        self._delay_seconds = delay_seconds

    async def __aenter__(self) -> _StallingSession:
        await asyncio.sleep(self._delay_seconds)
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


async def test_stalled_session_acquisition_produces_completed_report_not_a_hang():
    """A session factory whose async context entry stalls must not hang doctor.

    The outer database-evidence deadline (ENG-LOOP-001A-FIX1 / FIX-6) bounds
    session ACQUISITION itself, not just individual statements.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/ready":
            return httpx.Response(200, json={"status": "ready", "database": "connected"})
        if path == "/whoami":
            return httpx.Response(
                200,
                json={
                    "principal_id": "22222222-2222-2222-2222-222222222222",
                    "principal_type": "admin",
                    "tenant_id": "11111111-1111-1111-1111-111111111111",
                    "scopes": ["admin"], "api_key_id": "k", "memory_profile": None,
                },
            )
        if path == "/v1/review/stats":
            return httpx.Response(
                200,
                json={"by_review_status": {}, "by_kind": {}, "by_confidence": {}, "total": 0},
            )
        if path == "/v1/review/queue":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})

    def _stalling_factory() -> _StallingSession:
        return _StallingSession(delay_seconds=5.0)

    started = time.monotonic()
    report = await run_doctor(
        base_url="http://test",
        timeout_seconds=0.2,
        http_transport=httpx.MockTransport(handler),
        session_factory=_stalling_factory,
        clock=lambda: FIXED_NOW,
    )
    elapsed = time.monotonic() - started
    # Bounded by the 0.2s deadline, not the session's 5s stall.
    assert elapsed < 4.0

    ids = [c.id for c in report.checks]
    assert ids == list(CHECK_ORDER)
    by_id = {c.id: c for c in report.checks}
    # Every DB-dependent check degrades to unknown rather than the whole
    # report hanging or raising.
    assert by_id["worker.queue"].status == "unknown"
    assert by_id["capture.remember"].status == "unknown"
    assert by_id["recall.activity"].status == "unknown"
    assert by_id["receipts.activity"].status == "unknown"
    # HTTP-only checks are unaffected by the stalled database resource.
    assert by_id["service.health"].status == "pass"
    assert by_id["identity.scopes"].status == "pass"
    assert by_id["review.backlog"].status == "pass"
    # No sentinel/timeout internals leak; only a safe type name may appear.
    dumped = json.dumps(by_id["worker.queue"].evidence)
    assert "5.0" not in dumped
    assert "_StallingSession" not in dumped


# --- JSON stdout purity / DoctorReport is valid JSON ---------------------------


def test_full_report_json_dump_is_valid_json_and_stable_schema():
    report = _make_full_report({})
    dumped = report.model_dump_json(by_alias=True)
    parsed = json.loads(dumped)
    assert parsed["schema"] == "engram.doctor"
    assert parsed["schema_version"] == "1.2"
    assert parsed["profile"] == "automatic_memory_loop"
    assert [c["id"] for c in parsed["checks"]] == list(CHECK_ORDER)
    assert set(parsed.keys()) == {
        "schema", "schema_version", "profile", "engram_version", "generated_at",
        "window", "scope", "overall_status", "exit_code", "checks", "limitations",
    }


# --- HTTP checks (httpx.MockTransport) -----------------------------------------


async def _client(handler: Any, timeout: float = 5.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test", timeout=timeout
    )


async def test_service_health_pass():
    async def _run():
        def handler(request):
            return httpx.Response(200, json={"status": "ok"})

        async with await _client(handler) as client:
            return await _check_service_health(client)

    check = await _run()
    assert check.status == "pass"
    assert check.reason_code == "SERVICE_HEALTHY"


async def test_service_health_unreachable():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    async with await _client(handler) as client:
        check = await _check_service_health(client)
    assert check.status == "fail"
    assert check.reason_code == "SERVICE_UNREACHABLE"


async def test_service_health_timeout():
    def handler(request):
        raise httpx.TimeoutException("timed out", request=request)

    async with await _client(handler) as client:
        check = await _check_service_health(client)
    assert check.status == "fail"
    assert check.reason_code == "SERVICE_UNREACHABLE"


async def test_service_health_malformed_json():
    def handler(request):
        return httpx.Response(200, text="not json")

    async with await _client(handler) as client:
        check = await _check_service_health(client)
    assert check.status == "fail"
    assert check.reason_code == "SERVICE_HEALTH_INVALID"


async def test_service_health_non_ok_status_body():
    def handler(request):
        return httpx.Response(200, json={"status": "degraded"})

    async with await _client(handler) as client:
        check = await _check_service_health(client)
    assert check.status == "fail"
    assert check.reason_code == "SERVICE_HEALTH_INVALID"


async def test_service_health_503():
    def handler(request):
        return httpx.Response(503, json={"status": "unavailable"})

    async with await _client(handler) as client:
        check = await _check_service_health(client)
    assert check.status == "fail"
    assert check.reason_code == "SERVICE_HEALTH_INVALID"
    assert check.evidence["http_status"] == 503


async def test_service_readiness_pass():
    def handler(request):
        return httpx.Response(
            200, json={"status": "ready", "database": "connected", "pgvector": "0.8.0"}
        )

    async with await _client(handler) as client:
        check = await _check_service_readiness(client)
    assert check.status == "pass"
    assert check.reason_code == "SERVICE_READY"
    assert check.evidence["pgvector"] == "0.8.0"


async def test_service_readiness_not_ready_503():
    def handler(request):
        return httpx.Response(
            503,
            json={
                "status": "not_ready",
                "database": "connected",
                "rls": "ok",
                "pgvector": "0.6.0",
                "minimum_required": "0.8.0",
            },
        )

    async with await _client(handler) as client:
        check = await _check_service_readiness(client)
    assert check.status == "fail"
    assert check.reason_code == "SERVICE_NOT_READY"
    assert check.evidence["pgvector"] == "0.6.0"


async def test_service_readiness_malicious_body_never_leaks_free_text():
    """/ready is untrusted input: only known categorical/version values survive."""

    def handler(request):
        return httpx.Response(
            503,
            json={
                "status": "not_ready",
                "database": "postgresql://sentinel_user:SENTINEL_PW@sentinel-host/db",
                "rls": "arbitrary text injected by a hostile server",
                "pgvector": "0.6.0; DROP TABLE tenants;",
                "minimum_required": "not-a-version <script>",
                "reason": "leaked secret token=SENTINEL_TOKEN_VALUE",
                "detail": "SENTINEL_DETAIL_VALUE",
                "error": "SENTINEL_ERROR_VALUE",
                "message": "SENTINEL_MESSAGE_VALUE",
            },
        )

    async with await _client(handler) as client:
        check = await _check_service_readiness(client)
    assert check.status == "fail"
    assert check.reason_code == "SERVICE_NOT_READY"
    rendered = json.dumps(check.evidence)
    for sentinel in (
        "sentinel_user", "SENTINEL_PW", "sentinel-host", "SENTINEL_TOKEN_VALUE",
        "SENTINEL_DETAIL_VALUE", "SENTINEL_ERROR_VALUE", "SENTINEL_MESSAGE_VALUE",
        "DROP TABLE", "<script>", "arbitrary text injected",
    ):
        assert sentinel not in rendered
    # Unvalidated fields are dropped outright, not merely truncated.
    assert "database" not in check.evidence
    assert "rls" not in check.evidence
    assert "pgvector" not in check.evidence
    assert "minimum_required" not in check.evidence
    assert "reason" not in check.evidence
    assert check.evidence["status"] == "not_ready"
    assert check.evidence["http_status"] == 503


async def test_service_readiness_timeout():
    def handler(request):
        raise httpx.TimeoutException("timed out", request=request)

    async with await _client(handler) as client:
        check = await _check_service_readiness(client)
    assert check.status == "fail"
    assert check.reason_code == "SERVICE_READINESS_UNREACHABLE"


async def test_service_readiness_malformed_json():
    def handler(request):
        return httpx.Response(503, text="not json")

    async with await _client(handler) as client:
        check = await _check_service_readiness(client)
    assert check.status == "fail"
    assert check.reason_code == "SERVICE_NOT_READY"
    assert check.evidence["http_status"] == 503
    assert "status" not in check.evidence


async def test_service_readiness_no_tenant_context():
    def handler(request):
        return httpx.Response(
            503, json={"status": "not_ready", "database": "connected", "rls": "no_tenant_context"}
        )

    async with await _client(handler) as client:
        check = await _check_service_readiness(client)
    assert check.status == "fail"
    assert check.evidence["rls"] == "no_tenant_context"
    assert check.evidence["database"] == "connected"


async def test_service_readiness_unreachable():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    async with await _client(handler) as client:
        check = await _check_service_readiness(client)
    assert check.status == "fail"
    assert check.reason_code == "SERVICE_READINESS_UNREACHABLE"


async def test_identity_admin_scope_satisfies_read_and_write():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "principal_id": "22222222-2222-2222-2222-222222222222",
                "principal_type": "admin",
                "tenant_id": "11111111-1111-1111-1111-111111111111",
                "scopes": ["admin"],
                "api_key_id": "secret-key-id",
                "memory_profile": None,
            },
        )

    async with await _client(handler) as client:
        check, tenant_id, source = await _check_identity(client, requested_tenant=None)
    assert check.status == "pass"
    assert check.reason_code == "IDENTITY_READY"
    assert tenant_id == "11111111-1111-1111-1111-111111111111"
    assert source == "whoami"
    # never leak the api_key_id, Authorization header, or credential digest.
    assert "secret-key-id" not in json.dumps(check.evidence)
    assert "api_key_id" not in check.evidence


async def test_identity_read_only_missing_write_fails():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "principal_id": "22222222-2222-2222-2222-222222222222",
                "principal_type": "agent",
                "tenant_id": "11111111-1111-1111-1111-111111111111",
                "scopes": ["read"],
                "api_key_id": "k",
                "memory_profile": None,
            },
        )

    async with await _client(handler) as client:
        check, _, _ = await _check_identity(client, requested_tenant=None)
    assert check.status == "fail"
    assert check.reason_code == "WRITE_SCOPE_MISSING"


async def test_identity_401_authentication_failed():
    def handler(request):
        return httpx.Response(401, json={"detail": "Missing or invalid Authorization header"})

    async with await _client(handler) as client:
        check, tenant_id, source = await _check_identity(client, requested_tenant=None)
    assert check.status == "fail"
    assert check.reason_code == "AUTHENTICATION_FAILED"
    assert tenant_id is None
    assert source == "deployment"


async def test_identity_403_on_whoami_is_read_scope_missing():
    """/whoami itself requires `read`, so a 403 there means read is missing."""

    def handler(request):
        return httpx.Response(403, json={"detail": "Requires scope: read"})

    async with await _client(handler) as client:
        check, tenant_id, source = await _check_identity(client, requested_tenant=None)
    assert check.status == "fail"
    assert check.reason_code == "READ_SCOPE_MISSING"
    assert tenant_id is None
    assert source == "deployment"


async def test_identity_tenant_mismatch_warns_but_retains_explicit_tenant():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "principal_id": "22222222-2222-2222-2222-222222222222",
                "principal_type": "admin",
                "tenant_id": "33333333-3333-3333-3333-333333333333",
                "scopes": ["admin"],
                "api_key_id": "k",
                "memory_profile": None,
            },
        )

    async with await _client(handler) as client:
        check, tenant_id, source = await _check_identity(
            client, requested_tenant="explicit-tenant"
        )
    assert check.status == "warn"
    assert check.reason_code == "TENANT_SCOPE_MISMATCH"
    assert tenant_id == "explicit-tenant"
    assert source == "argument"


# --- FIX2-2: strict /whoami validation ----------------------------------------

_VALID_WHOAMI_PRINCIPAL_ID = "22222222-2222-2222-2222-222222222222"
_VALID_WHOAMI_TENANT_ID = "11111111-1111-1111-1111-111111111111"


def _valid_whoami_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "principal_id": _VALID_WHOAMI_PRINCIPAL_ID,
        "principal_type": "admin",
        "tenant_id": _VALID_WHOAMI_TENANT_ID,
        "scopes": ["admin"],
        "api_key_id": "k",
        "memory_profile": None,
    }
    body.update(overrides)
    return body


async def _identity_check_for(body: Any) -> DoctorCheck:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    async with await _client(handler) as client:
        check, _, _ = await _check_identity(client, requested_tenant=None)
    return check


async def test_identity_valid_unprofiled_body_passes():
    check = await _identity_check_for(_valid_whoami_body())
    assert check.status == "pass"
    assert check.reason_code == "IDENTITY_READY"
    assert check.evidence["memory_profile_slug"] is None
    assert check.evidence["memory_profile_version"] is None


async def test_identity_valid_profiled_body_passes_and_emits_only_validated_fields():
    check = await _identity_check_for(
        _valid_whoami_body(
            memory_profile={
                "id": "44444444-4444-4444-4444-444444444444",
                "slug": "concise-writer",
                "active_revision_id": "55555555-5555-5555-5555-555555555555",
                "version": 3,
            }
        )
    )
    assert check.status == "pass"
    assert check.evidence["memory_profile_slug"] == "concise-writer"
    assert check.evidence["memory_profile_version"] == 3
    # The profile's id/active_revision_id are validated but never emitted.
    assert "44444444-4444-4444-4444-444444444444" not in json.dumps(check.evidence)


async def test_identity_admin_still_satisfies_read_and_write():
    check = await _identity_check_for(_valid_whoami_body(scopes=["admin"]))
    assert check.status == "pass"


@pytest.mark.parametrize(
    "bad_scopes",
    [
        "admin",  # a string, not an array
        {"admin": True},  # a mapping
        1,  # an integer
        ["admin", 7],  # contains a non-string
        ["admin", "SENTINEL_UNKNOWN_SCOPE"],  # contains an unrecognized value
    ],
)
async def test_identity_rejects_malformed_scopes(bad_scopes: Any):
    check = await _identity_check_for(_valid_whoami_body(scopes=bad_scopes))
    assert check.status == "fail"
    assert check.reason_code == "IDENTITY_RESPONSE_INVALID"


async def test_identity_rejects_non_uuid_tenant_id():
    check = await _identity_check_for(_valid_whoami_body(tenant_id="not-a-uuid"))
    assert check.status == "fail"
    assert check.reason_code == "IDENTITY_RESPONSE_INVALID"


async def test_identity_rejects_non_uuid_principal_id():
    check = await _identity_check_for(_valid_whoami_body(principal_id="not-a-uuid"))
    assert check.status == "fail"
    assert check.reason_code == "IDENTITY_RESPONSE_INVALID"


async def test_identity_rejects_arbitrary_principal_type_prose():
    check = await _identity_check_for(
        _valid_whoami_body(principal_type="SENTINEL_CREDENTIAL_LOOKING_VALUE")
    )
    assert check.status == "fail"
    assert check.reason_code == "IDENTITY_RESPONSE_INVALID"


async def test_identity_rejects_memory_profile_as_list():
    check = await _identity_check_for(_valid_whoami_body(memory_profile=["not", "an", "object"]))
    assert check.status == "fail"
    assert check.reason_code == "IDENTITY_RESPONSE_INVALID"


async def test_identity_rejects_memory_profile_slug_violating_grammar():
    check = await _identity_check_for(
        _valid_whoami_body(memory_profile={"slug": "Not Valid Slug!", "version": 1})
    )
    assert check.status == "fail"
    assert check.reason_code == "IDENTITY_RESPONSE_INVALID"


async def test_identity_rejects_memory_profile_slug_too_long():
    check = await _identity_check_for(
        _valid_whoami_body(memory_profile={"slug": "a" * 256, "version": 1})
    )
    assert check.status == "fail"
    assert check.reason_code == "IDENTITY_RESPONSE_INVALID"


@pytest.mark.parametrize("bad_version", ["3", 3.0, True, 0, -1])
async def test_identity_rejects_malformed_memory_profile_version(bad_version: Any):
    check = await _identity_check_for(
        _valid_whoami_body(memory_profile={"slug": "concise-writer", "version": bad_version})
    )
    assert check.status == "fail"
    assert check.reason_code == "IDENTITY_RESPONSE_INVALID"


async def test_run_doctor_malformed_identity_produces_complete_report():
    """A malformed /whoami must degrade only identity, not abort the report."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/ready":
            return httpx.Response(200, json={"status": "ready", "database": "connected"})
        if path == "/whoami":
            return httpx.Response(200, json=_valid_whoami_body(scopes="not-a-list"))
        if path == "/v1/review/stats":
            return httpx.Response(
                200,
                json={"by_review_status": {}, "by_kind": {}, "by_confidence": {}, "total": 0},
            )
        if path == "/v1/review/queue":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})

    report = await run_doctor(
        base_url="http://test",
        http_transport=httpx.MockTransport(handler),
        session_factory=lambda: _ImmediatelyFailingSession(),
        clock=lambda: FIXED_NOW,
    )
    ids = [c.id for c in report.checks]
    assert ids == list(CHECK_ORDER)
    by_id = {c.id: c for c in report.checks}
    assert by_id["identity.scopes"].status == "fail"
    assert by_id["identity.scopes"].reason_code == "IDENTITY_RESPONSE_INVALID"
    assert by_id["service.health"].status == "pass"
    assert by_id["review.backlog"].status == "pass"


async def test_run_doctor_malformed_identity_never_leaks_sentinels():
    """Every rejected identity field's sentinel must never reach any output surface."""
    sentinel_type = "SENTINEL_PRINCIPAL_TYPE_CREDENTIAL_LOOKING"
    sentinel_scope = "SENTINEL_UNKNOWN_SCOPE_VALUE"
    sentinel_slug_input = "SENTINEL_INVALID_SLUG_VALUE"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/ready":
            return httpx.Response(200, json={"status": "ready", "database": "connected"})
        if path == "/whoami":
            return httpx.Response(
                200,
                json=_valid_whoami_body(
                    principal_type=sentinel_type,
                    scopes=["admin", sentinel_scope],
                    memory_profile={"slug": sentinel_slug_input, "version": 1},
                ),
            )
        if path == "/v1/review/stats":
            return httpx.Response(
                200,
                json={"by_review_status": {}, "by_kind": {}, "by_confidence": {}, "total": 0},
            )
        if path == "/v1/review/queue":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})

    report = await run_doctor(
        base_url="http://test",
        http_transport=httpx.MockTransport(handler),
        session_factory=lambda: _ImmediatelyFailingSession(),
        clock=lambda: FIXED_NOW,
    )
    by_id = {c.id: c for c in report.checks}
    assert by_id["identity.scopes"].status == "fail"
    dumped = report.model_dump_json(by_alias=True)
    rendered = render_human(report)
    for sentinel in (sentinel_type, sentinel_scope, sentinel_slug_input):
        assert sentinel not in dumped
        assert sentinel not in rendered


async def test_review_backlog_observed_excludes_universal_marker():
    def handler(request):
        if request.url.path == "/v1/review/stats":
            return httpx.Response(
                200,
                json={
                    "by_review_status": {"active": 2, "proposed": 3, "disputed": 1},
                    "by_kind": {},
                    "by_confidence": {},
                    "total": 6,
                },
            )
        if request.url.path == "/v1/review/queue":
            return httpx.Response(
                200,
                json=[
                    {
                        "content": "TOP SECRET MEMORY CONTENT",
                        "promotion_blockers": ["conflict_recheck_not_run", "age"],
                    },
                    {
                        "content": "ANOTHER SECRET",
                        "promotion_blockers": ["conflict_recheck_not_run", "age"],
                    },
                ],
            )
        return httpx.Response(404, json={})

    async with await _client(handler) as client:
        check = await _check_review_backlog(client)
    assert check.status == "pass"
    assert check.reason_code == "REVIEW_BACKLOG_OBSERVED"
    assert check.evidence["active_count"] == 2
    assert check.evidence["proposed_count"] == 3
    assert check.evidence["disputed_count"] == 1
    blocker_codes = [b["code"] for b in check.evidence["top_blockers"]]
    assert "conflict_recheck_not_run" not in blocker_codes
    assert {"code": "age", "count": 2} in check.evidence["top_blockers"]
    # memory content must never reach evidence.
    evidence_text = json.dumps(check.evidence)
    assert "TOP SECRET" not in evidence_text
    assert "ANOTHER SECRET" not in evidence_text


async def test_review_backlog_scope_unavailable_on_403():
    def handler(request):
        return httpx.Response(403, json={"detail": "Requires scope: review"})

    async with await _client(handler) as client:
        check = await _check_review_backlog(client)
    assert check.status == "unknown"
    assert check.reason_code == "REVIEW_SCOPE_UNAVAILABLE"


async def test_review_backlog_unavailable_on_500():
    def handler(request):
        return httpx.Response(500, json={"detail": "boom"})

    async with await _client(handler) as client:
        check = await _check_review_backlog(client)
    assert check.status == "unknown"
    assert check.reason_code == "REVIEW_BACKLOG_UNAVAILABLE"


def _stats_handler(by_review_status: Any, total: Any = 1) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/review/stats":
            return httpx.Response(
                200,
                json={
                    "by_review_status": by_review_status, "by_kind": {}, "by_confidence": {},
                    "total": total,
                },
            )
        if request.url.path == "/v1/review/queue":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})

    return handler


@pytest.mark.parametrize(
    "by_review_status",
    [
        [1, 2, 3],
        "active",
        None,
    ],
)
async def test_review_backlog_stats_by_status_wrong_shape(by_review_status: Any):
    async with await _client(_stats_handler(by_review_status)) as client:
        check = await _check_review_backlog(client)
    assert check.status == "unknown"
    assert check.reason_code == "REVIEW_BACKLOG_UNAVAILABLE"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("active", "3"),  # numeric-looking string, not an int
        ("active", -1),  # negative
        ("active", True),  # bool must not be accepted as an int
        ("proposed", 1.5),  # float
        ("disputed", [1]),  # list
    ],
)
async def test_review_backlog_stats_count_wrong_type_or_negative(field: str, value: Any):
    by_status = {"active": 1, "proposed": 0, "disputed": 0}
    by_status[field] = value
    async with await _client(_stats_handler(by_status)) as client:
        check = await _check_review_backlog(client)
    assert check.status == "unknown"
    assert check.reason_code == "REVIEW_BACKLOG_UNAVAILABLE"


@pytest.mark.parametrize("bad_total", ["1", 1.5, True, -1, [1], {"n": 1}])
async def test_review_backlog_stats_total_wrong_type(bad_total: Any):
    async with await _client(
        _stats_handler({"active": 1, "proposed": 0, "disputed": 0}, total=bad_total)
    ) as client:
        check = await _check_review_backlog(client)
    assert check.status == "unknown"
    assert check.reason_code == "REVIEW_BACKLOG_UNAVAILABLE"


async def test_review_backlog_stats_missing_total_is_invalid():
    """``total`` is required — a response omitting it entirely is incomplete,

    not implicitly zero (ENG-LOOP-001A-FIX2 / FIX2-3).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/review/stats":
            return httpx.Response(
                200,
                json={
                    "by_review_status": {"active": 1, "proposed": 0, "disputed": 0},
                    "by_kind": {}, "by_confidence": {},
                },
            )
        if request.url.path == "/v1/review/queue":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})

    async with await _client(handler) as client:
        check = await _check_review_backlog(client)
    assert check.status == "unknown"
    assert check.reason_code == "REVIEW_BACKLOG_UNAVAILABLE"


async def test_review_backlog_stats_selected_counts_exceed_total():
    async with await _client(
        _stats_handler({"active": 5, "proposed": 5, "disputed": 5}, total=3)
    ) as client:
        check = await _check_review_backlog(client)
    assert check.status == "unknown"
    assert check.reason_code == "REVIEW_BACKLOG_UNAVAILABLE"


async def test_review_backlog_stats_missing_status_key_defaults_to_zero():
    """A missing bucket key means zero, not an invalidating shape error."""
    async with await _client(_stats_handler({"active": 2}, total=2)) as client:
        check = await _check_review_backlog(client)
    assert check.status == "pass"
    assert check.evidence["proposed_count"] == 0
    assert check.evidence["disputed_count"] == 0


def _valid_stats_and_queue_handler(queue_response: httpx.Response) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/review/stats":
            return httpx.Response(
                200,
                json={
                    "by_review_status": {"active": 1, "proposed": 0, "disputed": 0},
                    "by_kind": {}, "by_confidence": {}, "total": 1,
                },
            )
        if request.url.path == "/v1/review/queue":
            return queue_response
        return httpx.Response(404, json={})

    return handler


async def test_review_backlog_queue_500_is_unknown_not_fail():
    handler = _valid_stats_and_queue_handler(httpx.Response(500, json={"detail": "boom"}))
    async with await _client(handler) as client:
        check = await _check_review_backlog(client)
    assert check.status == "unknown"
    assert check.reason_code == "REVIEW_QUEUE_UNAVAILABLE"
    # Partial safe stats evidence is retained even though the queue failed.
    assert check.evidence["active_count"] == 1


async def test_review_backlog_queue_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/review/stats":
            return httpx.Response(
                200,
                json={
                    "by_review_status": {"active": 1, "proposed": 0, "disputed": 0},
                    "by_kind": {}, "by_confidence": {}, "total": 1,
                },
            )
        raise httpx.TimeoutException("timed out", request=request)

    async with await _client(handler) as client:
        check = await _check_review_backlog(client)
    assert check.status == "unknown"
    assert check.reason_code == "REVIEW_QUEUE_UNAVAILABLE"


async def test_review_backlog_queue_malformed_json():
    handler = _valid_stats_and_queue_handler(httpx.Response(200, text="not json"))
    async with await _client(handler) as client:
        check = await _check_review_backlog(client)
    assert check.status == "unknown"
    assert check.reason_code == "REVIEW_QUEUE_UNAVAILABLE"


async def test_review_backlog_queue_non_list_shape():
    handler = _valid_stats_and_queue_handler(httpx.Response(200, json={"items": []}))
    async with await _client(handler) as client:
        check = await _check_review_backlog(client)
    assert check.status == "unknown"
    assert check.reason_code == "REVIEW_QUEUE_UNAVAILABLE"


async def test_review_backlog_queue_item_blockers_not_a_list():
    handler = _valid_stats_and_queue_handler(
        httpx.Response(200, json=[{"promotion_blockers": "min_age_not_met"}])
    )
    async with await _client(handler) as client:
        check = await _check_review_backlog(client)
    assert check.status == "unknown"
    assert check.reason_code == "REVIEW_QUEUE_UNAVAILABLE"


async def test_review_backlog_queue_blocker_list_has_non_string():
    handler = _valid_stats_and_queue_handler(
        httpx.Response(200, json=[{"promotion_blockers": ["min_age_not_met", 42]}])
    )
    async with await _client(handler) as client:
        check = await _check_review_backlog(client)
    assert check.status == "unknown"
    assert check.reason_code == "REVIEW_QUEUE_UNAVAILABLE"


async def test_review_backlog_queue_item_missing_promotion_blockers_key():
    """``promotion_blockers`` is required — no longer defaults to [] (ENG-LOOP-001A-FIX2)."""
    handler = _valid_stats_and_queue_handler(
        httpx.Response(200, json=[{"content": "no blockers key at all"}])
    )
    async with await _client(handler) as client:
        check = await _check_review_backlog(client)
    assert check.status == "unknown"
    assert check.reason_code == "REVIEW_QUEUE_UNAVAILABLE"


@pytest.mark.parametrize(
    "bad_blockers",
    [None, "age", {"age": True}, 1],
)
async def test_review_backlog_queue_promotion_blockers_wrong_type(bad_blockers: Any):
    handler = _valid_stats_and_queue_handler(
        httpx.Response(200, json=[{"promotion_blockers": bad_blockers}])
    )
    async with await _client(handler) as client:
        check = await _check_review_backlog(client)
    assert check.status == "unknown"
    assert check.reason_code == "REVIEW_QUEUE_UNAVAILABLE"


async def test_review_backlog_queue_unknown_blocker_code_is_invalid():
    """An unrecognized blocker string invalidates the sample, never passes through."""
    handler = _valid_stats_and_queue_handler(
        httpx.Response(200, json=[{"promotion_blockers": ["age", "SENTINEL_UNKNOWN_BLOCKER"]}])
    )
    async with await _client(handler) as client:
        check = await _check_review_backlog(client)
    assert check.status == "unknown"
    assert check.reason_code == "REVIEW_QUEUE_UNAVAILABLE"
    assert "SENTINEL_UNKNOWN_BLOCKER" not in json.dumps(check.evidence)


async def test_review_backlog_queue_credential_and_content_sentinel_blockers_never_leak():
    """A blocker string containing credential/memory-content sentinels must never be echoed."""
    sentinel_blocker = "SENTINEL_DB_PASSWORD_LOOKING_VALUE"
    handler = _valid_stats_and_queue_handler(
        httpx.Response(200, json=[{"promotion_blockers": ["age", sentinel_blocker]}])
    )
    async with await _client(handler) as client:
        check = await _check_review_backlog(client)
    assert check.status == "unknown"
    assert sentinel_blocker not in json.dumps(check.evidence)
    assert sentinel_blocker not in check.summary


async def test_review_backlog_all_canonical_blocker_codes_accepted():
    from engram.promotion import PROMOTION_BLOCKER_CODES

    items = [{"promotion_blockers": [code]} for code in sorted(PROMOTION_BLOCKER_CODES)]
    handler = _valid_stats_and_queue_handler(httpx.Response(200, json=items))
    async with await _client(handler) as client:
        check = await _check_review_backlog(client)
    assert check.status == "pass"
    assert check.reason_code == "REVIEW_BACKLOG_OBSERVED"


async def test_review_backlog_top_blockers_deterministic_tie_break():
    """Equal counts break ties by ascending blocker code, not insertion order."""
    items = [
        {"promotion_blockers": ["taxonomy_confidence", "conflict_recheck_not_run"]},
        {"promotion_blockers": ["age"]},
    ]
    handler = _valid_stats_and_queue_handler(httpx.Response(200, json=items))
    async with await _client(handler) as client:
        check = await _check_review_backlog(client)
    assert check.status == "pass"
    codes = [b["code"] for b in check.evidence["top_blockers"]]
    assert codes == ["age", "taxonomy_confidence"]


async def test_review_backlog_failure_does_not_abort_other_checks():
    """A malformed review response must not prevent the other 10 checks from running."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/ready":
            return httpx.Response(200, json={"status": "ready", "database": "connected"})
        if path == "/whoami":
            return httpx.Response(
                200,
                json={
                    "principal_id": "22222222-2222-2222-2222-222222222222",
                    "principal_type": "admin",
                    "tenant_id": "11111111-1111-1111-1111-111111111111",
                    "scopes": ["admin"], "api_key_id": "k", "memory_profile": None,
                },
            )
        if path == "/v1/review/stats":
            return httpx.Response(200, json={"by_review_status": "not-a-mapping", "total": 1})
        return httpx.Response(404, json={})

    def _unreachable_db(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("db unavailable in this unit test")

    report = await run_doctor(
        base_url="http://test",
        http_transport=httpx.MockTransport(handler),
        session_factory=_unreachable_db,
        clock=lambda: FIXED_NOW,
    )
    ids = [c.id for c in report.checks]
    assert ids == list(CHECK_ORDER)
    by_id = {c.id: c for c in report.checks}
    assert by_id["review.backlog"].status == "unknown"
    assert by_id["review.backlog"].reason_code == "REVIEW_BACKLOG_UNAVAILABLE"
    assert by_id["service.health"].status == "pass"
    assert by_id["identity.scopes"].status == "pass"


async def test_run_doctor_rejected_review_blocker_sentinel_never_leaks_anywhere():
    """A rejected/unknown review blocker sentinel must never reach any output surface."""
    sentinel_blocker = "SENTINEL_MEMORY_CONTENT_LOOKING_BLOCKER"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/ready":
            return httpx.Response(200, json={"status": "ready", "database": "connected"})
        if path == "/whoami":
            return httpx.Response(200, json=_valid_whoami_body())
        if path == "/v1/review/stats":
            return httpx.Response(
                200,
                json={"by_review_status": {"active": 1}, "by_kind": {}, "total": 1},
            )
        if path == "/v1/review/queue":
            return httpx.Response(
                200, json=[{"promotion_blockers": ["age", sentinel_blocker]}]
            )
        return httpx.Response(404, json={})

    report = await run_doctor(
        base_url="http://test",
        http_transport=httpx.MockTransport(handler),
        session_factory=lambda: _ImmediatelyFailingSession(),
        clock=lambda: FIXED_NOW,
    )
    by_id = {c.id: c for c in report.checks}
    assert by_id["review.backlog"].status == "unknown"
    assert by_id["review.backlog"].reason_code == "REVIEW_QUEUE_UNAVAILABLE"
    dumped = report.model_dump_json(by_alias=True)
    rendered = render_human(report)
    assert sentinel_blocker not in dumped
    assert sentinel_blocker not in rendered


# --- Redaction -----------------------------------------------------------------


_SENTINEL_API_KEY = "eng_SENTINEL_KEY_ID_sentinel-secret-material-do-not-leak"
_SENTINEL_DB_PASSWORD = "SENTINEL_DB_PASSWORD_VALUE"
_SENTINEL_DB_USER = "sentinel_db_user"


async def test_run_doctor_never_leaks_api_key():
    def handler(request: httpx.Request) -> httpx.Response:
        # A malicious/misconfigured server echoing the Authorization header
        # back must still never cause the CLI-side report to leak it — the
        # api_key is only ever placed in the outgoing header, never read back
        # from any response field by doctor's own code.
        auth = request.headers.get("authorization", "")
        assert _SENTINEL_API_KEY in auth  # prove the header really was sent
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/ready":
            return httpx.Response(200, json={"status": "ready", "database": "connected"})
        if path == "/whoami":
            return httpx.Response(
                200,
                json={
                    "principal_id": "22222222-2222-2222-2222-222222222222",
                    "principal_type": "admin",
                    "tenant_id": "11111111-1111-1111-1111-111111111111",
                    "scopes": ["admin"],
                    "api_key_id": "k",
                    "memory_profile": None,
                },
            )
        if path == "/v1/review/stats":
            return httpx.Response(
                200,
                json={"by_review_status": {}, "by_kind": {}, "by_confidence": {}, "total": 0},
            )
        if path == "/v1/review/queue":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})

    def _unreachable_db(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no db in this test")

    report = await run_doctor(
        base_url="http://test",
        api_key=_SENTINEL_API_KEY,
        http_transport=httpx.MockTransport(handler),
        session_factory=_unreachable_db,
        clock=lambda: FIXED_NOW,
    )
    dumped = report.model_dump_json(by_alias=True)
    rendered = render_human(report)
    assert _SENTINEL_API_KEY not in dumped
    assert _SENTINEL_API_KEY not in rendered


async def test_run_doctor_sanitizes_db_exception_to_type_name_only():
    class _LeakyConnectionError(Exception):
        pass

    def _factory(*args: Any, **kwargs: Any) -> Any:
        raise _LeakyConnectionError(
            f"connection failed: postgresql://{_SENTINEL_DB_USER}:{_SENTINEL_DB_PASSWORD}@host/db"
        )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/ready":
            return httpx.Response(200, json={"status": "ready", "database": "connected"})
        if path == "/whoami":
            return httpx.Response(
                200,
                json={
                    "principal_id": "22222222-2222-2222-2222-222222222222",
                    "principal_type": "admin",
                    "tenant_id": "11111111-1111-1111-1111-111111111111",
                    "scopes": ["admin"],
                    "api_key_id": "k",
                    "memory_profile": None,
                },
            )
        if path == "/v1/review/stats":
            return httpx.Response(
                200,
                json={"by_review_status": {}, "by_kind": {}, "by_confidence": {}, "total": 0},
            )
        if path == "/v1/review/queue":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})

    report = await run_doctor(
        base_url="http://test",
        http_transport=httpx.MockTransport(handler),
        session_factory=_factory,
        clock=lambda: FIXED_NOW,
    )
    dumped = report.model_dump_json(by_alias=True)
    assert _SENTINEL_DB_PASSWORD not in dumped
    assert _SENTINEL_DB_USER not in dumped
    assert "_LeakyConnectionError" in dumped


# --- CLI-level tests (monkeypatched run_doctor; no real network/DB) ----------


async def test_cli_json_stdout_is_pure_json(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    report = _make_full_report({})

    async def _fake_run_doctor(**kwargs: Any) -> DoctorReport:
        return report

    monkeypatch.setattr("engram.doctor.run_doctor", _fake_run_doctor)
    exit_code = await _run_doctor(
        base_url="http://test",
        tenant=None,
        since=None,
        until=None,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        database_url=None,
        as_json=True,
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    parsed = json.loads(out)  # raises if any non-JSON text is mixed in
    assert parsed["schema"] == "engram.doctor"


async def test_cli_human_output_default(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    report = _make_full_report({CHECK_ORDER[2]: "fail"})

    async def _fake_run_doctor(**kwargs: Any) -> DoctorReport:
        return report

    monkeypatch.setattr("engram.doctor.run_doctor", _fake_run_doctor)
    exit_code = await _run_doctor(
        base_url="http://test",
        tenant=None,
        since=None,
        until=None,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        database_url=None,
        as_json=False,
    )
    out = capsys.readouterr().out
    assert exit_code == 2
    assert "overall_status=unhealthy" in out


def _make_fake_run_doctor(report: DoctorReport) -> Any:
    async def _fake_run_doctor(**kwargs: Any) -> DoctorReport:
        return report

    return _fake_run_doctor


async def test_cli_exit_code_matches_report_for_each_severity(monkeypatch: pytest.MonkeyPatch):
    for statuses, expected_exit in (
        ({}, 0),
        ({CHECK_ORDER[0]: "warn"}, 1),
        ({CHECK_ORDER[0]: "fail"}, 2),
    ):
        report = _make_full_report(statuses)
        monkeypatch.setattr("engram.doctor.run_doctor", _make_fake_run_doctor(report))
        exit_code = await _run_doctor(
            base_url="http://test",
            tenant=None,
            since=None,
            until=None,
            timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
            database_url=None,
            as_json=True,
        )
        assert exit_code == expected_exit


async def test_cli_construction_failure_exits_2_without_raw_exception_text(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    async def _raising_run_doctor(**kwargs: Any) -> DoctorReport:
        raise RuntimeError(f"internal failure containing {_SENTINEL_DB_PASSWORD}")

    monkeypatch.setattr("engram.doctor.run_doctor", _raising_run_doctor)
    exit_code = await _run_doctor(
        base_url="http://test",
        tenant=None,
        since=None,
        until=None,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        database_url=None,
        as_json=True,
    )
    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert _SENTINEL_DB_PASSWORD not in captured.err
    assert "RuntimeError" in captured.err


def test_cli_rejects_invalid_timeout_argument(capsys: pytest.CaptureFixture[str]):
    import sys

    from engram.cli import main

    argv = ["engram", "doctor", "--timeout-seconds", "0"]
    old_argv = sys.argv
    sys.argv = argv
    try:
        with pytest.raises(SystemExit) as exc_info:
            main()
    finally:
        sys.argv = old_argv
    assert exc_info.value.code == 2
    assert "finite, strictly positive" in capsys.readouterr().err


def test_cli_rejects_invalid_since_argument(capsys: pytest.CaptureFixture[str]):
    import sys

    from engram.cli import main

    argv = ["engram", "doctor", "--since", "not-a-date"]
    old_argv = sys.argv
    sys.argv = argv
    try:
        with pytest.raises(SystemExit) as exc_info:
            main()
    finally:
        sys.argv = old_argv
    assert exc_info.value.code == 2
    assert "ISO-8601" in capsys.readouterr().err


def test_cli_rejects_invalid_tenant_argument(capsys: pytest.CaptureFixture[str]):
    import sys

    from engram.cli import main

    argv = ["engram", "doctor", "--tenant", "not-a-uuid"]
    old_argv = sys.argv
    sys.argv = argv
    try:
        with pytest.raises(SystemExit) as exc_info:
            main()
    finally:
        sys.argv = old_argv
    assert exc_info.value.code == 2
    assert "UUID" in capsys.readouterr().err


# --- promotion.readiness (ENG-PROMOTION-003A) ----------------------------------


def _promotion_evidence(
    *,
    proposed_total: int = 3,
    terminal: int = 1,
    time_dependent: int = 2,
    size: int = 3,
) -> dict[str, Any]:
    return {
        "auto_promote_enabled": True,
        "evidence_lane_enabled": False,
        "proposed_total": proposed_total,
        "by_source_type": {"sync_turn": 2, "manual": 1},
        "by_kind": {"fact": 3},
        "age_buckets": {"lt_24h": 1, "gt_30d": 2},
        "startup_window": {
            "limit": 20,
            "size": size,
            "terminal_under_current_policy": terminal,
            "time_dependent": time_dependent,
            "blocker_counts": [{"code": "age", "count": time_dependent}],
            "evidence_states": {"none": size},
            "job_states": {"missing": size},
        },
        "starvation_risk": bool(size) and terminal * 2 > size,
        "evidence_note": "retention_confidence is the classifier's durability estimate",
    }


def test_promotion_readiness_check_passes_when_not_starved():
    from engram.doctor import _check_promotion_readiness

    check = _check_promotion_readiness(
        evidence=_promotion_evidence(), tenant_unresolved=False, db_error=None
    )
    assert check.id == "promotion.readiness"
    assert check.status == "pass"
    assert check.reason_code == "PROMOTION_READINESS_OBSERVED"
    assert check.evidence["startup_window"]["terminal_under_current_policy"] == 1


def test_promotion_readiness_check_warns_on_starvation():
    from engram.doctor import _check_promotion_readiness

    # 3 of 4 window items terminal — a strict majority.
    check = _check_promotion_readiness(
        evidence=_promotion_evidence(terminal=3, time_dependent=1, size=4),
        tenant_unresolved=False,
        db_error=None,
    )
    assert check.status == "warn"
    assert check.reason_code == "PROMOTION_SCAN_STARVATION"
    assert "terminal under current policy" in check.summary
    # Since ENG-PROMOTION-003B the lazy pass rotates fairly, so the warning
    # must state blocked backlog, not claim later proposals are starved.
    assert "starving later proposals" in check.summary
    assert check.remediation, "starvation must carry remediation"


def test_promotion_readiness_check_exact_half_is_not_starvation():
    from engram.doctor import _check_promotion_readiness

    # 2 of 4 — dominated requires a strict majority.
    check = _check_promotion_readiness(
        evidence=_promotion_evidence(terminal=2, time_dependent=2, size=4),
        tenant_unresolved=False,
        db_error=None,
    )
    assert check.status == "pass"


def test_promotion_readiness_check_unknown_without_tenant_scope():
    from engram.doctor import _check_promotion_readiness

    check = _check_promotion_readiness(
        evidence=None, tenant_unresolved=True, db_error=None
    )
    assert check.status == "unknown"
    assert check.reason_code == "TENANT_SCOPE_UNRESOLVED"


def test_promotion_readiness_check_unknown_when_evidence_unavailable():
    from engram.doctor import _check_promotion_readiness

    check = _check_promotion_readiness(
        evidence=None, tenant_unresolved=False, db_error="OperationalError"
    )
    assert check.status == "unknown"
    assert check.reason_code == "PROMOTION_EVIDENCE_UNAVAILABLE"
    assert check.evidence == {"error_type": "OperationalError"}


def test_promotion_readiness_check_empty_window_passes():
    from engram.doctor import _check_promotion_readiness

    check = _check_promotion_readiness(
        evidence=_promotion_evidence(proposed_total=0, terminal=0, time_dependent=0, size=0),
        tenant_unresolved=False,
        db_error=None,
    )
    assert check.status == "pass"
    assert check.evidence["startup_window"]["size"] == 0
