"""Unit, contract, redaction, and CLI tests for ``engram doctor`` (ENG-LOOP-001A).

These tests use ``httpx.MockTransport`` (the repository's established pattern —
see ``tests/test_eng_audit_002b_recall_benchmark.py`` and
``tests/test_denied_profile_preflight.py``) and do not require a live service
or database. Real-PostgreSQL proofs (worker/lifecycle/recall/receipt evidence,
no-mutation, cross-tenant isolation) live in ``tests/test_doctor_postgres.py``.
"""

from __future__ import annotations

import json
import math
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
    _check_identity,
    _check_review_backlog,
    _check_service_health,
    _check_service_readiness,
    aggregate_status,
    parse_iso8601,
    render_human,
    resolve_base_url,
    resolve_database_url,
    resolve_window,
    run_doctor,
    validate_timeout_seconds,
)

FIXED_NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)


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
    assert dumped["schema_version"] == "1.0"
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


# --- JSON stdout purity / DoctorReport is valid JSON ---------------------------


def test_full_report_json_dump_is_valid_json_and_stable_schema():
    report = _make_full_report({})
    dumped = report.model_dump_json(by_alias=True)
    parsed = json.loads(dumped)
    assert parsed["schema"] == "engram.doctor"
    assert parsed["schema_version"] == "1.0"
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
                "principal_id": "p",
                "principal_type": "admin",
                "tenant_id": "tenant-a",
                "scopes": ["admin"],
                "api_key_id": "secret-key-id",
                "memory_profile": None,
            },
        )

    async with await _client(handler) as client:
        check, tenant_id, source = await _check_identity(client, requested_tenant=None)
    assert check.status == "pass"
    assert check.reason_code == "IDENTITY_READY"
    assert tenant_id == "tenant-a"
    assert source == "whoami"
    # never leak the api_key_id, Authorization header, or credential digest.
    assert "secret-key-id" not in json.dumps(check.evidence)
    assert "api_key_id" not in check.evidence


async def test_identity_read_only_missing_write_fails():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "principal_id": "p",
                "principal_type": "agent",
                "tenant_id": "tenant-a",
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
                "principal_id": "p",
                "principal_type": "admin",
                "tenant_id": "whoami-tenant",
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
                        "promotion_blockers": ["conflict_recheck_not_run", "min_age_not_met"],
                    },
                    {
                        "content": "ANOTHER SECRET",
                        "promotion_blockers": ["conflict_recheck_not_run", "min_age_not_met"],
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
    assert {"code": "min_age_not_met", "count": 2} in check.evidence["top_blockers"]
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
                    "principal_id": "p",
                    "principal_type": "admin",
                    "tenant_id": "t",
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
                    "principal_id": "p",
                    "principal_type": "admin",
                    "tenant_id": "t",
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
