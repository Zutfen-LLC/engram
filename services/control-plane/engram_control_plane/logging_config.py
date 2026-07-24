"""Structured logging + request-ID middleware + secret redaction.

Observability requirements (ENG-PORTAL-001A): structured service logs with
service name, environment, build SHA, request ID, method, route template,
status, and duration; bounded dependency-failure logs; and NO credentials,
cookies, tokens, authorization headers, database URLs, or customer content in
logs.
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

# Markers that must never appear in a log line. The SecretRedactionFilter
# scrubs them (case-insensitively) from the formatted message, so even
# printf-style logs are redacted. Synthetic sentinel values are used in tests.
_SENSITIVE_SUBSTRINGS: tuple[str, ...] = (
    "postgresql://",
    "postgresql+asyncpg://",
    "password=",
    "authorization:",
    "cookie:",
    "api-key:",
    "bearer ",
    "eng_",
)

_REDACTED = "[REDACTED]"

# Per-request context for the request id. A module-level dict is used (rather
# than a contextvar) for simplicity and testability; the middleware sets/clears
# it around each request. Non-request logs show "-".
_REQUEST_ID_CTX: dict[str, str] = {"value": "-"}
_SERVICE_CTX: dict[str, str] = {"value": "engram-control-plane"}


class SecretRedactionFilter(logging.Filter):
    """Replace sensitive substrings in log records with ``[REDACTED]``."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        lowered = msg.lower()
        if any(marker in lowered for marker in _SENSITIVE_SUBSTRINGS):
            scrubbed = msg
            for marker in _SENSITIVE_SUBSTRINGS:
                scrubbed = _scrub(scrubbed, marker)
            # Replace record fields so getMessage() yields the scrubbed form.
            record.msg = scrubbed
            record.args = ()
        return True


def _scrub(text_value: str, marker: str) -> str:
    """Best-effort redaction: replace the marker and the trailing value token."""
    lowered = text_value.lower()
    idx = lowered.find(marker)
    if idx == -1:
        return text_value
    start = idx
    end = idx + len(marker)
    # Swallow the trailing value up to the next whitespace.
    while end < len(text_value) and not text_value[end].isspace():
        end += 1
    return text_value[:start] + _REDACTED + text_value[end:]


def configure_logging(level: str, *, service: str = "engram-control-plane") -> logging.Logger:
    """Configure structured logging for the Control Plane.

    Output is single-line ``key=value`` structured text (parsable and
    grep-friendly) carrying service name, environment, build SHA, and request
    id. A :class:`SecretRedactionFilter` is attached to the handler so every
    log line is scrubbed of secrets.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)
    _SERVICE_CTX["value"] = service

    root = logging.getLogger()
    # Reset handlers so re-configuration (tests, multiple worker boots) is clean.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt=(
                "%(asctime)s %(levelname)s "
                "service=%(service)s environment=%(environment)s "
                "build_sha=%(build_sha)s request_id=%(request_id)s "
                "%(name)s %(message)s"
            ),
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    handler.addFilter(SecretRedactionFilter())
    root.addHandler(handler)
    root.setLevel(log_level)

    _install_logrecord_factory()
    return logging.getLogger(service)


def _install_logrecord_factory() -> None:
    """Stamp every LogRecord with the structured fields from live contexts.

    Installed once. A single factory reads the service/env/build_sha/request_id
    contexts so re-installation never drops fields.
    """
    from engram_control_plane.config import settings

    old_factory = logging.getLogRecordFactory()

    def record_factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = old_factory(*args, **kwargs)
        record.service = _SERVICE_CTX["value"]
        record.environment = settings.environment
        record.build_sha = settings.build_sha
        record.request_id = _REQUEST_ID_CTX["value"]
        return record

    logging.setLogRecordFactory(record_factory)


def get_current_request_id() -> str:
    return _REQUEST_ID_CTX["value"]


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Generate/propagate a request id and log a structured access line.

    Adds ``X-Request-ID`` to the response and binds the id into the logging
    context. No request bodies, headers (besides the request id), cookies, or
    query strings are logged.
    """

    def __init__(self, app: ASGIApp, *, service: str = "engram-control-plane") -> None:
        super().__init__(app)
        self._service = service

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        previous = _REQUEST_ID_CTX["value"]
        _REQUEST_ID_CTX["value"] = request_id
        try:
            response = await call_next(request)
        except Exception:
            logging.getLogger(self._service).exception(
                "request_failed method=%s path_template=%s",
                request.method,
                _route_template(request),
            )
            raise
        finally:
            _REQUEST_ID_CTX["value"] = previous

        response.headers["x-request-id"] = request_id
        logging.getLogger(self._service).info(
            "request method=%s path_template=%s status=%s",
            request.method,
            _route_template(request),
            response.status_code,
        )
        return response


def _route_template(request: Request) -> str:
    """Return the route template if matched, else the raw path.

    Logging the *template* (e.g. ``/control/v1/meta``) avoids leaking path
    parameters that might carry identifiers.
    """
    route = request.scope.get("route")
    if route is not None and hasattr(route, "path"):
        return str(route.path)
    return request.url.path
