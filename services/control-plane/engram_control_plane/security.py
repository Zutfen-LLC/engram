"""Sanitized error responses — never leak internals, DB URLs, or credentials.

The Control Plane returns structured, stable JSON errors. Exception internals,
database connection strings, and credentials are never placed in a response
body. Every error response carries the request id so operators can correlate.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


def sanitized_error(
    request: Request,
    *,
    status_code: int,
    error: str,
    detail: str | None = None,
) -> JSONResponse:
    """Build a structured, sanitized JSON error response.

    ``error`` is a stable machine code (e.g. ``"not_ready"``). ``detail`` is a
    human-readable, SAFE message — callers must never pass raw exception text,
    DB URLs, or credentials here.
    """
    from engram_control_plane.logging_config import get_current_request_id

    body: dict[str, Any] = {"error": error}
    if detail:
        body["detail"] = detail
    # request id helps operators correlate without exposing internals.
    rid = get_current_request_id()
    if rid and rid != "-":
        body["request_id"] = rid
    return JSONResponse(status_code=status_code, content=body, headers={"x-request-id": rid})


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler: log the real exception, return a generic 503."""
    import logging

    logging.getLogger("engram-control-plane").exception(
        "unhandled_exception error_type=%s", type(exc).__name__
    )
    return sanitized_error(
        request,
        status_code=503,
        error="internal_error",
        detail="An internal error occurred. See server logs (request id in response header).",
    )
