"""Public, service-authenticated control-plane provisioning endpoint."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import select

from engram.api.service_boundary import request_id_for
from engram.auth import reset_principal_cache, validate_principal_name
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
    PROVISION_AGENT_API_KEY,
    PROVISION_TENANT_HUMAN,
    PROVISION_WORKSPACE_AGENT,
    ServiceClientIdentity,
    lock_and_validate_service_authority,
)
from engram.service_onboarding import (
    AgentApiKeyInput,
    AgentInput,
    AgentKeyRequest,
    WorkspaceAgentRequest,
    WorkspaceInput,
    provision_agent_api_key,
    provision_workspace_agent,
    record_onboarding_conflict,
    validate_api_key_label,
    validate_workspace_slug,
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


class WorkspaceProvisionInput(BaseModel):
    model_config = {"extra": "forbid"}
    external_ref: str
    name: str
    slug: str

    _external_ref = field_validator("external_ref")(validate_external_ref)
    _name = field_validator("name")(lambda value: validate_name(value, "workspace.name"))
    _slug = field_validator("slug")(validate_workspace_slug)


class AgentProvisionInput(BaseModel):
    model_config = {"extra": "forbid"}
    external_ref: str
    name: str

    _external_ref = field_validator("external_ref")(validate_external_ref)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        validate_name(value, "agent.name")
        validate_principal_name(value)
        return value


class ServiceWorkspaceAgentRequest(BaseModel):
    model_config = {"extra": "forbid"}
    tenant_external_ref: str
    workspace: WorkspaceProvisionInput
    agent: AgentProvisionInput

    _tenant_external_ref = field_validator("tenant_external_ref")(validate_external_ref)


class WorkspaceOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    slug: str


class AgentOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    type: Literal["agent"]


class MembershipOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    principal_id: uuid.UUID
    role: Literal["member"]


class WorkspaceAgentCreatedOut(BaseModel):
    workspace: bool
    agent: bool
    membership: bool


class ServiceWorkspaceAgentResponse(BaseModel):
    workspace: WorkspaceOut
    agent: AgentOut
    membership: MembershipOut
    created: WorkspaceAgentCreatedOut
    idempotency_replayed: bool


class AgentApiKeyProvisionInput(BaseModel):
    model_config = {"extra": "forbid"}
    external_ref: str
    label: str | None = None
    replaces_external_ref: str | None = None

    _external_ref = field_validator("external_ref")(validate_external_ref)
    _label = field_validator("label")(validate_api_key_label)
    _replaces_external_ref = field_validator("replaces_external_ref")(
        lambda value: validate_external_ref(value) if value is not None else None
    )

    @model_validator(mode="after")
    def _replacement_refs_differ(self) -> AgentApiKeyProvisionInput:
        if (
            self.replaces_external_ref is not None
            and self.replaces_external_ref == self.external_ref
        ):
            raise ValueError("replacement external references must differ")
        return self


class ServiceAgentKeyRequest(BaseModel):
    model_config = {"extra": "forbid"}
    tenant_external_ref: str
    workspace_external_ref: str
    agent_external_ref: str
    api_key: AgentApiKeyProvisionInput

    _tenant_external_ref = field_validator("tenant_external_ref")(validate_external_ref)
    _workspace_external_ref = field_validator("workspace_external_ref")(validate_external_ref)
    _agent_external_ref = field_validator("agent_external_ref")(validate_external_ref)


class ServiceAgentApiKeyOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    principal_id: uuid.UUID
    scopes: list[str]
    label: str | None
    memory_profile_id: None = None
    revoked: bool


class ServiceAgentKeyResponse(BaseModel):
    api_key: ServiceAgentApiKeyOut
    created: bool
    replacement: bool
    idempotency_replayed: bool
    credential_secret_available: bool
    key: str | None


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


@router.post(
    "/service/provisioning/workspace-agent",
    response_model=ServiceWorkspaceAgentResponse,
    openapi_extra={
        "x-engram-auth-class": "service-client",
        "x-engram-service-permissions": ["workspace.provision", "agent.provision"],
        "security": [{"EngramServiceCredential": []}],
    },
)
async def provision_workspace_agent_route(
    body: ServiceWorkspaceAgentRequest,
    request_http: Request,
    response: Response,
    identity: ServiceClientIdentity = Depends(PROVISION_WORKSPACE_AGENT),  # noqa: B008
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ServiceWorkspaceAgentResponse:
    effective_request_id = request_id_for(request_http)
    try:
        valid_idempotency_key = validate_idempotency_key(idempotency_key)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_REQUEST", "message": str(exc)},
        ) from exc
    request = WorkspaceAgentRequest(
        tenant_external_ref=body.tenant_external_ref,
        workspace=WorkspaceInput(**body.workspace.model_dump()),
        agent=AgentInput(**body.agent.model_dump()),
    )
    conflict: HTTPException | None = None
    try:
        async with require_provisioner_session_factory()() as session, session.begin():
            locked_identity = await lock_and_validate_service_authority(
                session, identity, ("workspace.provision", "agent.provision")
            )
            credential = await session.scalar(
                select(ServiceClientCredential)
                .where(ServiceClientCredential.id == locked_identity.credential_id)
                .with_for_update()
            )
            if credential is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={
                        "code": "SERVICE_UNAUTHORIZED",
                        "message": "Service authentication failed",
                    },
                )
            await apply_service_client_context(session, locked_identity.id)
            try:
                async with session.begin_nested():
                    result = await provision_workspace_agent(
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
                await record_onboarding_conflict(
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
        status.HTTP_201_CREATED
        if result.workspace_created or result.agent_created or result.membership_created
        else status.HTTP_200_OK
    )
    response.headers["Idempotency-Replayed"] = str(result.idempotency_replayed).lower()
    return ServiceWorkspaceAgentResponse(
        workspace=WorkspaceOut(
            id=result.workspace.id,
            tenant_id=result.workspace.tenant_id,
            name=result.workspace.name,
            slug=result.workspace.slug,
        ),
        agent=AgentOut(
            id=result.agent.id,
            tenant_id=result.agent.tenant_id,
            name=result.agent.name,
            type="agent",
        ),
        membership=MembershipOut(
            id=result.membership.id,
            workspace_id=result.membership.workspace_id,
            principal_id=result.membership.principal_id,
            role="member",
        ),
        created=WorkspaceAgentCreatedOut(
            workspace=result.workspace_created,
            agent=result.agent_created,
            membership=result.membership_created,
        ),
        idempotency_replayed=result.idempotency_replayed,
    )


@router.post(
    "/service/provisioning/agent-api-key",
    response_model=ServiceAgentKeyResponse,
    openapi_extra={
        "x-engram-auth-class": "service-client",
        "x-engram-service-permissions": ["api_key.provision"],
        "security": [{"EngramServiceCredential": []}],
    },
)
async def provision_agent_api_key_route(
    body: ServiceAgentKeyRequest,
    request_http: Request,
    response: Response,
    identity: ServiceClientIdentity = Depends(PROVISION_AGENT_API_KEY),  # noqa: B008
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ServiceAgentKeyResponse:
    effective_request_id = request_id_for(request_http)
    try:
        valid_idempotency_key = validate_idempotency_key(idempotency_key)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_REQUEST", "message": str(exc)},
        ) from exc
    request = AgentKeyRequest(
        tenant_external_ref=body.tenant_external_ref,
        workspace_external_ref=body.workspace_external_ref,
        agent_external_ref=body.agent_external_ref,
        api_key=AgentApiKeyInput(**body.api_key.model_dump()),
    )
    conflict: HTTPException | None = None
    try:
        async with require_provisioner_session_factory()() as session, session.begin():
            locked_identity = await lock_and_validate_service_authority(
                session, identity, ("api_key.provision",)
            )
            credential = await session.scalar(
                select(ServiceClientCredential)
                .where(ServiceClientCredential.id == locked_identity.credential_id)
                .with_for_update()
            )
            if credential is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={
                        "code": "SERVICE_UNAUTHORIZED",
                        "message": "Service authentication failed",
                    },
                )
            await apply_service_client_context(session, locked_identity.id)
            try:
                async with session.begin_nested():
                    result = await provision_agent_api_key(
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
                await record_onboarding_conflict(
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

    if result.created and result.replacement:
        reset_principal_cache()
    response.status_code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    response.headers["Idempotency-Replayed"] = str(result.idempotency_replayed).lower()
    if result.api_key.principal_id is None:
        raise RuntimeError("service-provisioned agent key has no principal")
    return ServiceAgentKeyResponse(
        api_key=ServiceAgentApiKeyOut(
            id=result.api_key.id,
            tenant_id=result.api_key.tenant_id,
            principal_id=result.api_key.principal_id,
            scopes=list(result.api_key.scopes),
            label=result.api_key.label,
            memory_profile_id=None,
            revoked=result.api_key.revoked_at is not None,
        ),
        created=result.created,
        replacement=result.replacement,
        idempotency_replayed=result.idempotency_replayed,
        credential_secret_available=result.created,
        key=result.plaintext if result.created else None,
    )
