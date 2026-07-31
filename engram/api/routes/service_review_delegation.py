"""Service-authenticated purpose-bound review delegation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text

from engram.api.routes.service_delegation import (
    DelegationRevokeRequest,
    DelegationRevokeResponse,
)
from engram.api.service_boundary import request_id_for
from engram.config import settings
from engram.db import apply_service_client_context, require_provisioner_session_factory
from engram.delegation_auth import (
    ReviewPurpose,
    canonical_review_queue_purpose,
    canonical_review_transition_purpose,
    generate_review_delegation_token,
    sha256_bytes,
    validate_delegation_external_ref,
    validate_delegation_idempotency_key,
    validate_delegation_ttl,
)
from engram.service_auth import ISSUE_REVIEW_DELEGATION, ServiceClientIdentity

router = APIRouter()


class ReviewQueuePurposeRequest(BaseModel):
    model_config = {"extra": "forbid"}
    kind: Literal["review.queue"]


class ReviewTransitionPurposeRequest(BaseModel):
    model_config = {"extra": "forbid"}
    kind: Literal["review.transition"]
    item_id: str
    review_status: Literal["active", "rejected"]
    reason: str | None = None

    @field_validator("item_id")
    @classmethod
    def _canonical_item_id(cls, value: str) -> str:
        purpose = canonical_review_transition_purpose(
            item_id=value,
            review_status="active",
            reason=None,
        )
        assert purpose.target_item_id is not None
        return str(purpose.target_item_id)

    @field_validator("reason")
    @classmethod
    def _bounded_reason(cls, value: str | None) -> str | None:
        from engram.delegation_auth import validate_review_reason

        return validate_review_reason(value)


ReviewPurposeRequest = Annotated[
    ReviewQueuePurposeRequest | ReviewTransitionPurposeRequest,
    Field(discriminator="kind"),
]


class ReviewDelegationIssueRequest(BaseModel):
    model_config = {"extra": "forbid"}
    binding_owner_service_client_slug: str
    tenant_external_ref: str
    principal_external_ref: str
    delegation_external_ref: str
    purpose: ReviewPurposeRequest
    ttl_seconds: int | None = Field(default=None, strict=True)

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


class ReviewDelegationIssueResponse(BaseModel):
    created: bool
    idempotency_replayed: bool
    credential_secret_available: bool
    token: str | None
    scopes: list[Literal["review"]]
    audience: Literal["engram-core"]
    single_use: Literal[True]
    issued_at: datetime
    expires_at: datetime


def _purpose(body: ReviewDelegationIssueRequest) -> ReviewPurpose:
    if isinstance(body.purpose, ReviewQueuePurposeRequest):
        return canonical_review_queue_purpose()
    return canonical_review_transition_purpose(
        item_id=body.purpose.item_id,
        review_status=body.purpose.review_status,
        reason=body.purpose.reason,
    )


def _unresolved_request_digest(
    identity: ServiceClientIdentity,
    body: ReviewDelegationIssueRequest,
    purpose: ReviewPurpose,
    ttl_seconds: int,
) -> bytes:
    payload: dict[str, Any] = {
        "authority_class": "review",
        "binding_owner_service_client_slug": body.binding_owner_service_client_slug,
        "delegation_external_ref": body.delegation_external_ref,
        "issuer_service_client_id": str(identity.id),
        "principal_external_ref": body.principal_external_ref,
        "purpose_digest": purpose.digest.hex(),
        "schema_version": 1,
        "tenant_external_ref": body.tenant_external_ref,
        "ttl_seconds": ttl_seconds,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).digest()


def _review_delegation_error(code: str) -> HTTPException:
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
        detail={
            "code": code,
            "message": "Review delegation request could not be completed",
        },
    )


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "REVIEW_DELEGATION_UNAVAILABLE",
            "message": "Review delegation is unavailable",
        },
    )


@router.post(
    "/service/review-delegations",
    response_model=ReviewDelegationIssueResponse,
    openapi_extra={
        "x-engram-auth-class": "service-client",
        "x-engram-service-permissions": ["delegation.review.issue"],
        "security": [{"EngramServiceCredential": []}],
    },
)
async def issue_review_delegation_route(
    body: ReviewDelegationIssueRequest,
    request: Request,
    response: Response,
    identity: ServiceClientIdentity = Depends(ISSUE_REVIEW_DELEGATION),  # noqa: B008
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ReviewDelegationIssueResponse:
    if not settings.review_delegation_enabled:
        raise _unavailable()
    try:
        valid_key = validate_delegation_idempotency_key(idempotency_key)
        ttl_seconds = validate_delegation_ttl(
            settings.review_delegation_default_ttl_seconds
            if body.ttl_seconds is None
            else body.ttl_seconds,
            settings.review_delegation_max_ttl_seconds,
        )
        purpose = _purpose(body)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_REQUEST", "message": str(exc)},
        ) from exc

    material = generate_review_delegation_token()
    try:
        async with require_provisioner_session_factory()() as session, session.begin():
            await apply_service_client_context(session, identity.id)
            row = (
                await session.execute(
                    text(
                        "SELECT * FROM issue_service_review_delegation("
                        ":credential,:owner_slug,:tenant_ref,:principal_ref,:delegation_ref,"
                        ":idempotency_digest,:request_digest,:key_id,:secret_digest,:ttl,"
                        ":request_id,:purpose_name,:purpose_digest,:target_item_id,"
                        ":target_review_status)"
                    ),
                    {
                        "credential": identity.credential_id,
                        "owner_slug": body.binding_owner_service_client_slug,
                        "tenant_ref": body.tenant_external_ref,
                        "principal_ref": body.principal_external_ref,
                        "delegation_ref": body.delegation_external_ref,
                        "idempotency_digest": sha256_bytes(valid_key),
                        "request_digest": _unresolved_request_digest(
                            identity, body, purpose, ttl_seconds
                        ),
                        "key_id": material.key_id,
                        "secret_digest": material.secret_digest,
                        "ttl": ttl_seconds,
                        "request_id": request_id_for(request),
                        "purpose_name": purpose.name,
                        "purpose_digest": purpose.digest,
                        "target_item_id": purpose.target_item_id,
                        "target_review_status": purpose.target_review_status,
                    },
                )
            ).mappings().one()
    except HTTPException:
        raise
    except Exception:
        raise _unavailable() from None

    if row["error_code"] is not None:
        raise _review_delegation_error(str(row["error_code"]))
    created = bool(row["created"])
    replayed = bool(row["idempotency_replayed"])
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    response.headers["Idempotency-Replayed"] = str(replayed).lower()
    return ReviewDelegationIssueResponse(
        created=created,
        idempotency_replayed=replayed,
        credential_secret_available=created,
        token=material.token if created else None,
        scopes=["review"],
        audience="engram-core",
        single_use=True,
        issued_at=row["issued_at"],
        expires_at=row["expires_at"],
    )


@router.post(
    "/service/review-delegations/revoke",
    response_model=DelegationRevokeResponse,
    openapi_extra={
        "x-engram-auth-class": "service-client",
        "x-engram-service-permissions": ["delegation.review.issue"],
        "security": [{"EngramServiceCredential": []}],
    },
)
async def revoke_review_delegation_route(
    body: DelegationRevokeRequest,
    request: Request,
    identity: ServiceClientIdentity = Depends(ISSUE_REVIEW_DELEGATION),  # noqa: B008
) -> DelegationRevokeResponse:
    if not settings.review_delegation_enabled:
        raise _unavailable()
    try:
        async with require_provisioner_session_factory()() as session, session.begin():
            await apply_service_client_context(session, identity.id)
            row = (
                await session.execute(
                    text(
                        "SELECT * FROM revoke_service_review_delegation("
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
        raise _review_delegation_error(str(row["error_code"]))
    disposition = str(row["disposition"])
    return DelegationRevokeResponse(
        disposition=disposition,  # type: ignore[arg-type]
        revoked=disposition == "revoked",
    )
