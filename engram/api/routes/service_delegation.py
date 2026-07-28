"""Service-authenticated delegated-token issuance and revocation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, field_validator
from sqlalchemy import text

from engram.api.service_boundary import request_id_for
from engram.config import settings
from engram.db import apply_service_client_context, require_provisioner_session_factory
from engram.delegation_auth import (
    generate_delegation_token,
    sha256_bytes,
    validate_delegation_external_ref,
    validate_delegation_idempotency_key,
    validate_delegation_ttl,
)
from engram.service_auth import ISSUE_DELEGATION, ServiceClientIdentity

router = APIRouter()

RevocationReason = Literal[
    "request_completed",
    "response_uncertain",
    "session_revoked",
    "membership_changed",
    "entitlement_changed",
    "operator_action",
    "credential_rotation",
]


class DelegationIssueRequest(BaseModel):
    model_config = {"extra": "forbid"}
    binding_owner_service_client_slug: str
    tenant_external_ref: str
    principal_external_ref: str
    delegation_external_ref: str
    ttl_seconds: int | None = None

    _tenant_ref = field_validator("tenant_external_ref")(validate_delegation_external_ref)
    _principal_ref = field_validator("principal_external_ref")(validate_delegation_external_ref)
    _delegation_ref = field_validator("delegation_external_ref")(
        validate_delegation_external_ref
    )

    @field_validator("binding_owner_service_client_slug")
    @classmethod
    def _binding_owner_slug(cls, value: str) -> str:
        from engram.service_auth import validate_service_client_slug

        return validate_service_client_slug(value)


class DelegationIssueResponse(BaseModel):
    created: bool
    idempotency_replayed: bool
    credential_secret_available: bool
    token: str | None
    scopes: list[Literal["read"]]
    audience: Literal["engram-core"]
    single_use: Literal[True]
    issued_at: datetime
    expires_at: datetime


class DelegationRevokeRequest(BaseModel):
    model_config = {"extra": "forbid"}
    binding_owner_service_client_slug: str
    tenant_external_ref: str
    principal_external_ref: str
    delegation_external_ref: str
    reason: RevocationReason

    _tenant_ref = field_validator("tenant_external_ref")(validate_delegation_external_ref)
    _principal_ref = field_validator("principal_external_ref")(validate_delegation_external_ref)
    _delegation_ref = field_validator("delegation_external_ref")(
        validate_delegation_external_ref
    )

    @field_validator("binding_owner_service_client_slug")
    @classmethod
    def _binding_owner_slug(cls, value: str) -> str:
        from engram.service_auth import validate_service_client_slug

        return validate_service_client_slug(value)


class DelegationRevokeResponse(BaseModel):
    disposition: Literal["revoked", "already_revoked", "already_used", "not_found"]
    revoked: bool


def _unresolved_request_digest(
    identity: ServiceClientIdentity, body: DelegationIssueRequest, ttl_seconds: int
) -> bytes:
    """Digest the caller-visible request; the function also validates resolved identity."""
    payload = {
        "binding_owner_service_client_slug": body.binding_owner_service_client_slug,
        "delegation_external_ref": body.delegation_external_ref,
        "issuer_service_client_id": str(identity.id),
        "principal_external_ref": body.principal_external_ref,
        "schema_version": 1,
        "tenant_external_ref": body.tenant_external_ref,
        "ttl_seconds": ttl_seconds,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).digest()


def _delegation_error(code: str) -> HTTPException:
    if code in {"DELEGATION_EXTERNAL_REF_CONFLICT", "IDEMPOTENCY_KEY_REUSED"}:
        http_status = status.HTTP_409_CONFLICT
    elif code in {
        "DELEGATION_GRANT_NOT_FOUND",
        "BINDING_OWNER_NOT_FOUND",
        "TENANT_BINDING_NOT_FOUND",
        "PRINCIPAL_BINDING_NOT_FOUND",
    }:
        http_status = status.HTTP_404_NOT_FOUND
    elif code == "DELEGATION_SUBJECT_INVALID":
        http_status = status.HTTP_422_UNPROCESSABLE_ENTITY
    elif code in {"SERVICE_UNAUTHORIZED", "SERVICE_FORBIDDEN"}:
        http_status = (
            status.HTTP_401_UNAUTHORIZED
            if code == "SERVICE_UNAUTHORIZED"
            else status.HTTP_403_FORBIDDEN
        )
    else:
        http_status = status.HTTP_409_CONFLICT
    return HTTPException(
        status_code=http_status,
        detail={"code": code, "message": "Delegation request could not be completed"},
    )


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"code": "DELEGATION_UNAVAILABLE", "message": "Delegation is unavailable"},
    )


@router.post(
    "/service/delegations",
    response_model=DelegationIssueResponse,
    openapi_extra={
        "x-engram-auth-class": "service-client",
        "x-engram-service-permissions": ["delegation.issue"],
        "security": [{"EngramServiceCredential": []}],
    },
)
async def issue_delegation_route(
    body: DelegationIssueRequest,
    request: Request,
    response: Response,
    identity: ServiceClientIdentity = Depends(ISSUE_DELEGATION),  # noqa: B008
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> DelegationIssueResponse:
    if not settings.delegation_enabled:
        raise _unavailable()
    try:
        valid_key = validate_delegation_idempotency_key(idempotency_key)
        ttl_seconds = validate_delegation_ttl(
            settings.delegation_default_ttl_seconds
            if body.ttl_seconds is None
            else body.ttl_seconds,
            settings.delegation_max_ttl_seconds,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_REQUEST", "message": str(exc)},
        ) from exc

    material = generate_delegation_token()
    try:
        async with require_provisioner_session_factory()() as session, session.begin():
            await apply_service_client_context(session, identity.id)
            row = (
                await session.execute(
                    text(
                        "SELECT * FROM issue_service_delegation("
                        ":credential,:owner_slug,:tenant_ref,:principal_ref,:delegation_ref,"
                        ":idempotency_digest,:request_digest,:key_id,:secret_digest,:ttl,"
                        ":request_id)"
                    ),
                    {
                        "credential": identity.credential_id,
                        "owner_slug": body.binding_owner_service_client_slug,
                        "tenant_ref": body.tenant_external_ref,
                        "principal_ref": body.principal_external_ref,
                        "delegation_ref": body.delegation_external_ref,
                        "idempotency_digest": sha256_bytes(valid_key),
                        "request_digest": _unresolved_request_digest(
                            identity, body, ttl_seconds
                        ),
                        "key_id": material.key_id,
                        "secret_digest": material.secret_digest,
                        "ttl": ttl_seconds,
                        "request_id": request_id_for(request),
                    },
                )
            ).mappings().one()
    except HTTPException:
        raise
    except Exception:
        raise _unavailable() from None

    if row["error_code"] is not None:
        raise _delegation_error(str(row["error_code"]))
    created = bool(row["created"])
    replayed = bool(row["idempotency_replayed"])
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    response.headers["Idempotency-Replayed"] = str(replayed).lower()
    return DelegationIssueResponse(
        created=created,
        idempotency_replayed=replayed,
        credential_secret_available=created,
        token=material.token if created else None,
        scopes=["read"],
        audience="engram-core",
        single_use=True,
        issued_at=row["issued_at"],
        expires_at=row["expires_at"],
    )


@router.post(
    "/service/delegations/revoke",
    response_model=DelegationRevokeResponse,
    openapi_extra={
        "x-engram-auth-class": "service-client",
        "x-engram-service-permissions": ["delegation.issue"],
        "security": [{"EngramServiceCredential": []}],
    },
)
async def revoke_delegation_route(
    body: DelegationRevokeRequest,
    request: Request,
    identity: ServiceClientIdentity = Depends(ISSUE_DELEGATION),  # noqa: B008
) -> DelegationRevokeResponse:
    if not settings.delegation_enabled:
        raise _unavailable()
    try:
        async with require_provisioner_session_factory()() as session, session.begin():
            await apply_service_client_context(session, identity.id)
            row = (
                await session.execute(
                    text(
                        "SELECT * FROM revoke_service_delegation("
                        ":credential,:owner_slug,:tenant_ref,:principal_ref,"
                        ":delegation_ref,:reason,:request_id)"
                    ),
                    {
                        "credential": identity.credential_id,
                        "owner_slug": body.binding_owner_service_client_slug,
                        "tenant_ref": body.tenant_external_ref,
                        "principal_ref": body.principal_external_ref,
                        "delegation_ref": body.delegation_external_ref,
                        "reason": body.reason,
                        "request_id": request_id_for(request),
                    },
                )
            ).mappings().one()
    except HTTPException:
        raise
    except Exception:
        raise _unavailable() from None
    if row["error_code"] is not None:
        raise _delegation_error(str(row["error_code"]))
    disposition = str(row["disposition"])
    return DelegationRevokeResponse(
        disposition=disposition,  # type: ignore[arg-type]
        revoked=disposition == "revoked",
    )
