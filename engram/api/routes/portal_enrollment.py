"""Fixed Portal installation enrollment endpoint."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, field_validator
from sqlalchemy import text

from engram.api.service_boundary import request_id_for
from engram.config import settings
from engram.db import owner_session_factory
from engram.portal_enrollment_auth import (
    PORTAL_ENROLLMENT,
    PortalEnrollmentIdentity,
)

router = APIRouter()

_KEY_ID_RE = re.compile(r"^[0-9A-Za-z]{22}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IDEMPOTENCY_RE = re.compile(r"^[!-~]{1,255}$")


class EnrollmentCredential(BaseModel):
    model_config = {"extra": "forbid"}

    key_id: str
    secret_digest: str

    @field_validator("key_id")
    @classmethod
    def _valid_key_id(cls, value: str) -> str:
        if _KEY_ID_RE.fullmatch(value) is None:
            raise ValueError("invalid credential key ID")
        return value

    @field_validator("secret_digest")
    @classmethod
    def _valid_digest(cls, value: str) -> str:
        if _DIGEST_RE.fullmatch(value) is None:
            raise ValueError("invalid credential secret digest")
        return value


class PortalInstallationEnrollmentRequest(BaseModel):
    model_config = {"extra": "forbid"}

    installation_external_ref: UUID
    provisioner: EnrollmentCredential
    read_broker: EnrollmentCredential
    review_broker: EnrollmentCredential


class PortalInstallationEnrollmentResponse(BaseModel):
    status: Literal["completed"]
    idempotency_replayed: bool
    provisioner: Literal["ready"] = "ready"
    read_delegation: Literal["ready"] = "ready"
    review_delegation: Literal["ready"] = "ready"


def _sha256(value: str) -> bytes:
    return hashlib.sha256(value.encode("ascii")).digest()


def _request_digest(body: PortalInstallationEnrollmentRequest) -> bytes:
    payload = {
        "installation_external_ref": str(body.installation_external_ref),
        "provisioner": body.provisioner.model_dump(),
        "read_broker": body.read_broker.model_dump(),
        "review_broker": body.review_broker.model_dump(),
        "schema_version": 1,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).digest()


def _unavailable(code: str = "ENROLLMENT_UNAVAILABLE") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"code": code, "message": "Enrollment is unavailable"},
    )


@router.post(
    "/service/portal-installation-enrollments",
    response_model=PortalInstallationEnrollmentResponse,
    openapi_extra={
        "x-engram-auth-class": "portal-enrollment",
        "security": [{"EngramPortalEnrollmentCredential": []}],
    },
)
async def enroll_portal_installation_route(
    body: PortalInstallationEnrollmentRequest,
    request: Request,
    response: Response,
    identity: PortalEnrollmentIdentity = Depends(PORTAL_ENROLLMENT),  # noqa: B008
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> PortalInstallationEnrollmentResponse:
    if (
        not settings.service_provisioning_enabled
        or not settings.delegation_enabled
        or not settings.review_delegation_enabled
        or settings.delegation_max_ttl_seconds < 60
        or settings.review_delegation_max_ttl_seconds < 60
    ):
        raise _unavailable("ENROLLMENT_FEATURES_NOT_READY")
    if idempotency_key is None or _IDEMPOTENCY_RE.fullmatch(idempotency_key) is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_REQUEST", "message": "Invalid request"},
        )
    if len({body.provisioner.key_id, body.read_broker.key_id, body.review_broker.key_id}) != 3:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_REQUEST", "message": "Invalid request"},
        )

    try:
        async with owner_session_factory() as session, session.begin():
            row = (
                await session.execute(
                    text(
                        "SELECT * FROM enroll_portal_installation("
                        ":pairing_digest,:installation_ref,:idempotency_digest,"
                        ":request_digest,:provisioner_key_id,:provisioner_secret_digest,"
                        ":read_key_id,:read_secret_digest,:review_key_id,"
                        ":review_secret_digest,:request_id)"
                    ),
                    {
                        "pairing_digest": identity.secret_digest,
                        "installation_ref": body.installation_external_ref,
                        "idempotency_digest": _sha256(idempotency_key),
                        "request_digest": _request_digest(body),
                        "provisioner_key_id": body.provisioner.key_id,
                        "provisioner_secret_digest": body.provisioner.secret_digest,
                        "read_key_id": body.read_broker.key_id,
                        "read_secret_digest": body.read_broker.secret_digest,
                        "review_key_id": body.review_broker.key_id,
                        "review_secret_digest": body.review_broker.secret_digest,
                        "request_id": request_id_for(request),
                    },
                )
            ).mappings().one()
    except HTTPException:
        raise
    except Exception:
        raise _unavailable() from None

    error_code = row["error_code"]
    if error_code == "ENROLLMENT_CONFLICT":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "ENROLLMENT_CONFLICT", "message": "Enrollment conflicts"},
        )
    if error_code is not None:
        raise _unavailable("ENROLLMENT_FEATURES_NOT_READY")
    replayed = bool(row["idempotency_replayed"])
    response.status_code = status.HTTP_200_OK if replayed else status.HTTP_201_CREATED
    response.headers["Idempotency-Replayed"] = str(replayed).lower()
    return PortalInstallationEnrollmentResponse(
        status="completed", idempotency_replayed=replayed
    )
