"""Uniform response contract for service and delegated requests."""

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
_AUTHORIZATION_HEADER = b"authorization"
_DELEGATION_PREFIXES = ("engd_", "engdr_")


def is_service_request(request: Request) -> bool:
    return request.url.path.startswith(_SERVICE_PREFIX)


def is_delegated_request(request: Request) -> bool:
    """Classify a delegated Bearer attempt without retaining its credential."""
    for name, raw_value in request.scope.get("headers", ()):
        if name.lower() != _AUTHORIZATION_HEADER:
            continue
        value = raw_value.decode("latin-1")
        scheme, separator, credential = value.partition(" ")
        if (
            separator
            and scheme.lower() == "bearer"
            and credential.startswith(_DELEGATION_PREFIXES)
        ):
            return True
    return False


def effective_request_id(candidate: str | None) -> str:
    """Preserve one valid visible-ASCII identifier or generate a UUID."""
    if candidate is not None and _REQUEST_ID.fullmatch(candidate):
        return candidate
    return str(uuid.uuid4())


def request_id_for(request: Request) -> str:
    """Return the pre-validation request ID established by the middleware."""
    request_id = getattr(request.state, "sensitive_request_id", None)
    if isinstance(request_id, str):
        return request_id
    return effective_request_id(request.headers.get("X-Request-ID"))


def apply_sensitive_response_headers(response: Response, request_id: str) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Request-ID"] = request_id
    return response


class SensitiveResponseBoundaryMiddleware(BaseHTTPMiddleware):
    """Install a safe request ID for service and delegated request boundaries."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        service_request = is_service_request(request)
        delegated_request = is_delegated_request(request)
        if not service_request and not delegated_request:
            return await call_next(request)
        request.state.sensitive_request_id = effective_request_id(
            request.headers.get("X-Request-ID")
        )
        try:
            response = await call_next(request)
        except Exception:
            # ServerErrorMiddleware sits outside user middleware. Convert an
            # otherwise-unhandled sensitive failure here so even this last
            # failure phase keeps the public response contract.
            if service_request:
                detail = {
                    "code": "SERVICE_UNAVAILABLE",
                    "message": "Provisioning is unavailable",
                }
            else:
                detail = {
                    "code": "DELEGATED_REQUEST_UNAVAILABLE",
                    "message": "Request unavailable",
                }
            response = JSONResponse(status_code=503, content={"detail": detail})
        return apply_sensitive_response_headers(response, request_id_for(request))


# Compatibility alias for existing service-route imports.
ServiceResponseBoundaryMiddleware = SensitiveResponseBoundaryMiddleware
apply_service_response_headers = apply_sensitive_response_headers


async def service_request_validation_handler(request: Request, exc: Exception) -> Response:
    """Keep ordinary validation untouched while sanitizing service validation."""
    if not isinstance(exc, RequestValidationError):
        raise exc
    if not is_service_request(request):
        return await request_validation_exception_handler(request, exc)
    return apply_sensitive_response_headers(
        JSONResponse(
            status_code=422,
            content={"detail": {"code": "INVALID_REQUEST", "message": "Invalid request"}},
        ),
        request_id_for(request),
    )
