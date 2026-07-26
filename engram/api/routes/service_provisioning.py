"""Public, service-authenticated control-plane provisioning endpoint."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, field_validator

from engram.db import require_provisioner_session_factory
from engram.models import ServiceClientCredential
from engram.provisioning import (
    ProvisionPrincipalInput,
    ProvisionRequest,
    ProvisionTenantInput,
    provision_tenant_human,
    validate_external_ref,
    validate_idempotency_key,
    validate_name,
    validate_tenant_slug,
)
from engram.service_auth import PROVISION_TENANT_HUMAN, ServiceClientIdentity

router = APIRouter()


class TenantInput(BaseModel):
    model_config = {"extra": "forbid"}
    external_ref: str
    name: str
    slug: str

    _external_ref = field_validator("external_ref")(validate_external_ref)
    _name = field_validator("name")(lambda value: validate_name(value, "tenant.name"))
    _slug = field_validator("slug")(validate_tenant_slug)


class HumanPrincipalInput(BaseModel):
    model_config = {"extra": "forbid"}
    external_ref: str
    name: str

    _external_ref = field_validator("external_ref")(validate_external_ref)
    _name = field_validator("name")(lambda value: validate_name(value, "human_principal.name"))


class ServiceProvisionRequest(BaseModel):
    model_config = {"extra": "forbid"}
    tenant: TenantInput
    human_principal: HumanPrincipalInput


class TenantOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str


class PrincipalOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    type: str


class CreatedOut(BaseModel):
    tenant: bool
    principal: bool


class ServiceProvisionResponse(BaseModel):
    tenant: TenantOut
    principal: PrincipalOut
    created: CreatedOut
    idempotency_replayed: bool


@router.post(
    "/service/provisioning/tenant-human",
    response_model=ServiceProvisionResponse,
    openapi_extra={
        "x-engram-auth-class": "service-client",
        "x-engram-service-permissions": ["tenant.provision", "principal.provision"],
        "security": [{"EngramServiceCredential": []}],
    },
)
async def provision_tenant_human_route(
    body: ServiceProvisionRequest,
    response: Response,
    identity: ServiceClientIdentity = Depends(PROVISION_TENANT_HUMAN),  # noqa: B008
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    request_id: str | None = Header(default=None, alias="X-Request-ID"),
) -> ServiceProvisionResponse:
    try:
        valid_idempotency_key = validate_idempotency_key(idempotency_key)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_REQUEST", "message": str(exc)},
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        ) from exc
    request = ProvisionRequest(
        tenant=ProvisionTenantInput(**body.tenant.model_dump()),
        human_principal=ProvisionPrincipalInput(**body.human_principal.model_dump()),
    )
    effective_request_id = (
        request_id
        if request_id and request_id.isascii() and len(request_id) <= 128
        else str(uuid.uuid4())
    )
    try:
        async with require_provisioner_session_factory()() as session, session.begin():
            credential = await session.get(ServiceClientCredential, identity.credential_id)
            if credential is None:
                # Credential state may have changed after authentication;
                # do not perform provisioning with stale authority.
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={
                        "code": "SERVICE_UNAUTHORIZED",
                        "message": "Service authentication failed",
                    },
                )
            result = await provision_tenant_human(
                session,
                identity,
                credential,
                request,
                valid_idempotency_key,
                effective_request_id,
            )
    except HTTPException as exc:
        exc.headers = {
            **(exc.headers or {}),
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "X-Request-ID": effective_request_id,
        }
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "SERVICE_UNAVAILABLE", "message": "Provisioning is unavailable"},
            headers={
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                "Referrer-Policy": "no-referrer",
                "X-Request-ID": effective_request_id,
            },
        ) from None

    response.status_code = (
        status.HTTP_200_OK
        if result.idempotency_replayed
        or (not result.tenant_created and not result.principal_created)
        else status.HTTP_201_CREATED
    )
    response.headers.update(
        {
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "Idempotency-Replayed": str(result.idempotency_replayed).lower(),
            "X-Request-ID": effective_request_id,
        }
    )
    return ServiceProvisionResponse(
        tenant=TenantOut(id=result.tenant.id, name=result.tenant.name, slug=result.tenant.slug),
        principal=PrincipalOut(
            id=result.principal.id,
            tenant_id=result.principal.tenant_id,
            name=result.principal.name,
            type="user",
        ),
        created=CreatedOut(tenant=result.tenant_created, principal=result.principal_created),
        idempotency_replayed=result.idempotency_replayed,
    )
