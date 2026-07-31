"""Fixed Portal installation enrollment endpoint."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator
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


class PortalCredentialRotationRequest(BaseModel):
    model_config = {"extra": "forbid"}

    installation_external_ref: UUID
    expected_credential_generation: int = Field(ge=1)
    rotation_external_ref: UUID
    provisioner: EnrollmentCredential
    read_broker: EnrollmentCredential
    review_broker: EnrollmentCredential


class PortalCredentialRotationResponse(BaseModel):
    status: Literal["active"]
    credential_generation: int
    idempotency_replayed: bool


class PortalEnrollmentStatusResponse(BaseModel):
    status: Literal["active", "terminated"]
    credential_generation: int
    provisioner: Literal["ready", "not_ready"]
    read_delegation: Literal["ready", "not_ready"]
    review_delegation: Literal["ready", "not_ready"]


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


def _rotation_request_digest(body: PortalCredentialRotationRequest) -> bytes:
    payload = {
        "expected_credential_generation": body.expected_credential_generation,
        "installation_external_ref": str(body.installation_external_ref),
        "provisioner": body.provisioner.model_dump(),
        "read_broker": body.read_broker.model_dump(),
        "review_broker": body.review_broker.model_dump(),
        "rotation_external_ref": str(body.rotation_external_ref),
        "schema_version": 1,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).digest()


def _unavailable(code: str = "ENROLLMENT_UNAVAILABLE") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"code": code, "message": "Enrollment is unavailable"},
    )


def _invalid_request() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"code": "INVALID_REQUEST", "message": "Invalid request"},
    )


def _enrollment_error(code: str) -> HTTPException:
    if code == "ENROLLMENT_TERMINATED":
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": code, "message": "Enrollment is terminated"},
        )
    if code == "ENROLLMENT_NOT_FOUND":
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": code, "message": "Enrollment was not found"},
        )
    if code in {
        "ENROLLMENT_CONFLICT",
        "ROTATION_CONFLICT",
        "STALE_CREDENTIAL_GENERATION",
    }:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": code, "message": "Enrollment request conflicts"},
        )
    if code == "INVALID_REQUEST":
        return _invalid_request()
    return _unavailable("ENROLLMENT_FEATURES_NOT_READY")


def _features_ready() -> bool:
    return (
        settings.service_provisioning_enabled
        and settings.delegation_enabled
        and settings.review_delegation_enabled
        and settings.delegation_max_ttl_seconds >= 60
        and settings.review_delegation_max_ttl_seconds >= 60
    )


def _validate_idempotency_key(idempotency_key: str | None) -> str:
    if idempotency_key is None or _IDEMPOTENCY_RE.fullmatch(idempotency_key) is None:
        raise _invalid_request()
    return idempotency_key


def _validate_distinct_credentials(*credentials: EnrollmentCredential) -> None:
    if len({credential.key_id for credential in credentials}) != len(credentials):
        raise _invalid_request()
    if len({credential.secret_digest for credential in credentials}) != len(credentials):
        raise _invalid_request()


def _readiness(ready: object) -> Literal["ready", "not_ready"]:
    return "ready" if bool(ready) else "not_ready"


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
    if not _features_ready():
        raise _unavailable("ENROLLMENT_FEATURES_NOT_READY")
    idempotency_key = _validate_idempotency_key(idempotency_key)
    _validate_distinct_credentials(body.provisioner, body.read_broker, body.review_broker)

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
    if error_code is not None:
        raise _enrollment_error(str(error_code))
    replayed = bool(row["idempotency_replayed"])
    response.status_code = status.HTTP_200_OK if replayed else status.HTTP_201_CREATED
    response.headers["Idempotency-Replayed"] = str(replayed).lower()
    return PortalInstallationEnrollmentResponse(
        status="completed", idempotency_replayed=replayed
    )


@router.post(
    "/service/portal-installation-enrollments/rotate-credentials",
    response_model=PortalCredentialRotationResponse,
    openapi_extra={
        "x-engram-auth-class": "portal-enrollment",
        "security": [{"EngramPortalEnrollmentCredential": []}],
    },
)
async def rotate_portal_installation_credentials_route(
    body: PortalCredentialRotationRequest,
    request: Request,
    response: Response,
    identity: PortalEnrollmentIdentity = Depends(PORTAL_ENROLLMENT),  # noqa: B008
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> PortalCredentialRotationResponse:
    if not _features_ready():
        raise _unavailable("ENROLLMENT_FEATURES_NOT_READY")
    idempotency_key = _validate_idempotency_key(idempotency_key)
    _validate_distinct_credentials(body.provisioner, body.read_broker, body.review_broker)

    try:
        async with owner_session_factory() as session, session.begin():
            row = (
                await session.execute(
                    text(
                        "SELECT * FROM rotate_portal_installation_credentials("
                        ":pairing_digest,:installation_ref,:expected_generation,"
                        ":rotation_ref,:idempotency_digest,:request_digest,"
                        ":provisioner_key_id,:provisioner_secret_digest,"
                        ":read_key_id,:read_secret_digest,:review_key_id,"
                        ":review_secret_digest,:request_id)"
                    ),
                    {
                        "pairing_digest": identity.secret_digest,
                        "installation_ref": body.installation_external_ref,
                        "expected_generation": body.expected_credential_generation,
                        "rotation_ref": body.rotation_external_ref,
                        "idempotency_digest": _sha256(idempotency_key),
                        "request_digest": _rotation_request_digest(body),
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

    if row["error_code"] is not None:
        raise _enrollment_error(str(row["error_code"]))
    replayed = bool(row["idempotency_replayed"])
    response.status_code = status.HTTP_200_OK if replayed else status.HTTP_201_CREATED
    response.headers["Idempotency-Replayed"] = str(replayed).lower()
    return PortalCredentialRotationResponse(
        status="active",
        credential_generation=int(row["resulting_credential_generation"]),
        idempotency_replayed=replayed,
    )


@router.get(
    "/service/portal-installation-enrollments/{installation_external_ref}",
    response_model=PortalEnrollmentStatusResponse,
    openapi_extra={
        "x-engram-auth-class": "portal-enrollment",
        "security": [{"EngramPortalEnrollmentCredential": []}],
    },
)
async def portal_installation_enrollment_status_route(
    installation_external_ref: UUID,
    identity: PortalEnrollmentIdentity = Depends(PORTAL_ENROLLMENT),  # noqa: B008
) -> PortalEnrollmentStatusResponse:
    if not _features_ready():
        raise _unavailable("ENROLLMENT_FEATURES_NOT_READY")
    try:
        async with owner_session_factory() as session, session.begin():
            row = (
                await session.execute(
                    text(
                        "SELECT enrollment.status,enrollment.credential_generation,"
                        "bool_and(client.status='active' AND credential.status='active') "
                        "FILTER (WHERE enrolled.role='provisioner') AS provisioner_ready,"
                        "bool_and(client.status='active' AND credential.status='active' "
                        "AND grant_row.status='active') "
                        "FILTER (WHERE enrolled.role='read_broker') AS read_ready,"
                        "bool_and(client.status='active' AND credential.status='active' "
                        "AND grant_row.status='active') "
                        "FILTER (WHERE enrolled.role='review_broker') AS review_ready "
                        "FROM portal_installation_enrollments enrollment "
                        "JOIN portal_installation_enrollment_clients enrolled "
                        "ON enrolled.enrollment_id=enrollment.id "
                        "JOIN service_clients client ON client.id=enrolled.service_client_id "
                        "JOIN service_client_credentials credential "
                        "ON credential.id=enrolled.service_credential_id "
                        "LEFT JOIN service_delegation_grants grant_row "
                        "ON grant_row.issuer_service_client_id=enrolled.service_client_id "
                        "AND grant_row.authority_class=CASE enrolled.role "
                        "WHEN 'read_broker' THEN 'read' "
                        "WHEN 'review_broker' THEN 'review' ELSE NULL END "
                        "WHERE enrollment.enrollment_secret_digest=:pairing_digest "
                        "AND enrollment.installation_external_ref=:installation_ref "
                        "GROUP BY enrollment.status,enrollment.credential_generation"
                    ),
                    {
                        "pairing_digest": identity.secret_digest,
                        "installation_ref": installation_external_ref,
                    },
                )
            ).mappings().one_or_none()
    except Exception:
        raise _unavailable() from None
    if row is None:
        raise _enrollment_error("ENROLLMENT_NOT_FOUND")
    return PortalEnrollmentStatusResponse(
        status=row["status"],
        credential_generation=int(row["credential_generation"]),
        provisioner=_readiness(row["provisioner_ready"]),
        read_delegation=_readiness(row["read_ready"]),
        review_delegation=_readiness(row["review_ready"]),
    )
