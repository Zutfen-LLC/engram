"""Uniform response contract for public service-client routes."""

from __future__ import annotations

import re
import uuid

from fastapi import Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

_SERVICE_PREFIX = "/v1/service/"
_REQUEST_ID = re.compile(r"^[\x21-\x7e]{1,128}$")


def is_service_request(request: Request) -> bool:
    return request.url.path.startswith(_SERVICE_PREFIX)


def request_id_for(request: Request) -> str:
    """Return the pre-validation request ID established by the middleware."""
    request_id = getattr(request.state, "service_request_id", None)
    if isinstance(request_id, str):
        return request_id
    return str(uuid.uuid4())


def apply_service_response_headers(response: Response, request_id: str) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Request-ID"] = request_id
    return response


class ServiceResponseBoundaryMiddleware(BaseHTTPMiddleware):
    """Install a safe request ID before FastAPI parses headers or a body."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        if not is_service_request(request):
            return await call_next(request)
        candidate = request.headers.get("X-Request-ID")
        if candidate is not None and _REQUEST_ID.fullmatch(candidate):
            request.state.service_request_id = candidate
        else:
            request.state.service_request_id = str(uuid.uuid4())
        try:
            response = await call_next(request)
        except Exception:
            # ServerErrorMiddleware sits outside user middleware.  Convert an
            # otherwise-unhandled service failure here so even this last
            # failure phase keeps the public service response contract.
            response = JSONResponse(
                status_code=503,
                content={
                    "detail": {
                        "code": "SERVICE_UNAVAILABLE",
                        "message": "Provisioning is unavailable",
                    }
                },
            )
        return apply_service_response_headers(response, request_id_for(request))


async def service_request_validation_handler(
    request: Request, exc: Exception
) -> Response:
    """Keep ordinary validation untouched while sanitizing service validation."""
    if not isinstance(exc, RequestValidationError):
        raise exc
    if not is_service_request(request):
        return await request_validation_exception_handler(request, exc)
    return apply_service_response_headers(
        JSONResponse(
            status_code=422,
            content={"detail": {"code": "INVALID_REQUEST", "message": "Invalid request"}},
        ),
        request_id_for(request),
    )
