"""Public, service-authenticated control-plane provisioning endpoint."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, field_validator
from sqlalchemy import select

from engram.api.service_boundary import request_id_for
from engram.db import apply_service_client_context, require_provisioner_session_factory
from engram.models import ServiceClientCredential
from engram.provisioning import (
    ProvisionPrincipalInput,
    ProvisionRequest,
    ProvisionTenantInput,
    provision_tenant_human,
    record_provisioning_conflict,
    validate_external_ref,
    validate_idempotency_key,
    validate_name,
    validate_tenant_slug,
)
from engram.service_auth import (
    PROVISION_TENANT_HUMAN,
    ServiceClientIdentity,
    lock_and_validate_service_authority,
)

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
    request_http: Request,
    response: Response,
    identity: ServiceClientIdentity = Depends(PROVISION_TENANT_HUMAN),  # noqa: B008
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ServiceProvisionResponse:
    effective_request_id = request_id_for(request_http)
    try:
        valid_idempotency_key = validate_idempotency_key(idempotency_key)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_REQUEST", "message": str(exc)},
        ) from exc
    request = ProvisionRequest(
        tenant=ProvisionTenantInput(**body.tenant.model_dump()),
        human_principal=ProvisionPrincipalInput(**body.human_principal.model_dump()),
    )
    conflict: HTTPException | None = None
    try:
        async with require_provisioner_session_factory()() as session, session.begin():
            locked_identity = await lock_and_validate_service_authority(
                session, identity, ("tenant.provision", "principal.provision")
            )
            credential = await session.scalar(
                select(ServiceClientCredential)
                .where(ServiceClientCredential.id == locked_identity.credential_id)
                .with_for_update()
            )
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
            # Keep the RLS context outside the nested resource savepoint so a
            # deterministic conflict can roll back resources yet still append
            # its bounded conflict event in the outer transaction.
            await apply_service_client_context(session, locked_identity.id)
            try:
                async with session.begin_nested():
                    result = await provision_tenant_human(
                        session,
                        locked_identity,
                        credential,
                        request,
                        valid_idempotency_key,
                        effective_request_id,
                    )
            except HTTPException as exc:
                if exc.status_code != status.HTTP_409_CONFLICT:
                    raise
                reason = (
                    exc.detail.get("code", "PROVISIONING_CONFLICT")
                    if isinstance(exc.detail, dict)
                    else "PROVISIONING_CONFLICT"
                )
                await record_provisioning_conflict(
                    session,
                    locked_identity,
                    credential,
                    request,
                    effective_request_id,
                    str(reason),
                )
                conflict = exc
        if conflict is not None:
            raise conflict
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "SERVICE_UNAVAILABLE", "message": "Provisioning is unavailable"},
        ) from None

    response.status_code = (
        status.HTTP_200_OK
        if result.idempotency_replayed
        or (not result.tenant_created and not result.principal_created)
        else status.HTTP_201_CREATED
    )
    response.headers["Idempotency-Replayed"] = str(result.idempotency_replayed).lower()
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
