"""Security: logs and errors must not expose credentials."""

from __future__ import annotations

import logging

from engram_control_plane.logging_config import (
    _SENSITIVE_SUBSTRINGS,
    SecretRedactionFilter,
    _scrub,
)


def test_secret_substrings_are_scrubbed() -> None:
    """Each sensitive marker is redacted in a log message."""
    filter_ = SecretRedactionFilter()

    for marker in _SENSITIVE_SUBSTRINGS:
        record = logging.LogRecord(
            name="t",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=f"context {marker}supersecret trailing",
            args=(),
            exc_info=None,
        )
        assert filter_.filter(record) is True
        formatted = record.getMessage()
        assert marker not in formatted.lower()
        assert "supersecret" not in formatted


def test_db_url_with_credentials_is_redacted() -> None:
    """A database URL containing a password is scrubbed."""
    filter_ = SecretRedactionFilter()
    record = logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="connecting to postgresql+asyncpg://engram_control_app:hunter2@host/db",
        args=(),
        exc_info=None,
    )
    filter_.filter(record)
    out = record.getMessage()
    assert "hunter2" not in out
    assert "postgresql+asyncpg://" not in out.lower()


def test_scrub_preserves_safe_text() -> None:
    """Non-sensitive text passes through unchanged."""
    assert _scrub("all good here", "postgresql://") == "all good here"
    assert _scrub("nothing to see", "password=") == "nothing to see"


def test_non_sensitive_log_passes_through() -> None:
    filter_ = SecretRedactionFilter()
    record = logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="request completed status=200",
        args=(),
        exc_info=None,
    )
    assert filter_.filter(record) is True
    assert record.getMessage() == "request completed status=200"


def test_error_response_never_contains_credentials() -> None:
    """Sanitized error helpers never echo connection strings."""
    from fastapi import Request
    from starlette.datastructures import Scope

    from engram_control_plane.security import sanitized_error

    scope: Scope = {
        "type": "http",
        "method": "GET",
        "path": "/ready",
        "headers": [],
        "query_string": b"",
    }
    request = Request(scope)
    resp = sanitized_error(
        request, status_code=503, error="not_ready", detail="database_unreachable"
    )
    body = resp.body.decode()
    assert "postgresql" not in body.lower()
    assert resp.status_code == 503
