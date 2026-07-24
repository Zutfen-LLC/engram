"""Scaffold metadata endpoint.

``GET /control/v1/meta`` returns only safe scaffold metadata describing the
hard boundaries and the (unimplemented) future hosted capabilities. This lets
the Portal render an honest status page and lets operators verify the boundary
contract without exposing internals.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from engram_control_plane import __version__
from engram_control_plane.config import settings

router = APIRouter(prefix="/control/v1")


class MetaResponse(BaseModel):
    """Safe scaffold metadata + boundary contract.

    Field values are fixed strings describing the CURRENT scaffold state, not
    future behavior. They are intentionally rigid so drift is visible.
    """

    service: str
    api_version: str
    environment: str
    build_sha: str
    database_boundary: str
    identity_status: str
    delegation_status: str
    billing_status: str
    core_access_mode: str


def _meta() -> MetaResponse:
    return MetaResponse(
        service="engram-control-plane",
        api_version=__version__,
        environment=settings.environment,
        build_sha=settings.build_sha,
        # Hard boundary contract (ENG-PORTAL-001A). Fixed values describe the
        # current scaffold, not future behavior.
        database_boundary="dedicated_database",
        identity_status="not_configured",
        delegation_status="not_implemented",
        billing_status="not_configured",
        core_access_mode="service_api_only",
    )


@router.get("/meta", response_model=MetaResponse)
async def meta() -> MetaResponse:
    """Safe scaffold metadata only."""
    return _meta()


# Constants for tests: the documented set of fields and their fixed values.
META_REQUIRED_VALUES: dict[str, str] = {
    "database_boundary": "dedicated_database",
    "identity_status": "not_configured",
    "delegation_status": "not_implemented",
    "billing_status": "not_configured",
    "core_access_mode": "service_api_only",
}

META_REQUIRED_FIELDS: tuple[str, ...] = (
    "service",
    "api_version",
    "environment",
    "build_sha",
    "database_boundary",
    "identity_status",
    "delegation_status",
    "billing_status",
    "core_access_mode",
)
