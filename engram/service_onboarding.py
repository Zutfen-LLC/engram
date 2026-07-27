"""Atomic service-client workspace, agent, membership, and key onboarding."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from engram.api_key_issuance import issue_api_key
from engram.db import apply_service_client_context
from engram.models import (
    AgentApiKeyProvisioningBinding,
    ApiKey,
    Principal,
    PrincipalProvisioningBinding,
    ServiceAgentKeyIdempotency,
    ServiceClientCredential,
    ServiceProvisioningEvent,
    ServiceWorkspaceAgentIdempotency,
    TenantProvisioningBinding,
    Workspace,
    WorkspaceMember,
    WorkspaceProvisioningBinding,
)
from engram.service_auth import ServiceClientIdentity

_WORKSPACE_SLUG = re.compile(r"^[a-z][a-z0-9-]{0,254}$")
FIXED_AGENT_KEY_SCOPES = ["read", "write"]


async def onboarding_stage(_stage: str) -> None:
    """Rollback-certification seam; callers cannot influence it."""
    return None


@dataclass(frozen=True)
class WorkspaceInput:
    external_ref: str
    name: str
    slug: str


@dataclass(frozen=True)
class AgentInput:
    external_ref: str
    name: str


@dataclass(frozen=True)
class WorkspaceAgentRequest:
    tenant_external_ref: str
    workspace: WorkspaceInput
    agent: AgentInput


@dataclass(frozen=True)
class WorkspaceAgentResult:
    workspace: Workspace
    agent: Principal
    membership: WorkspaceMember
    workspace_created: bool
    agent_created: bool
    membership_created: bool
    idempotency_replayed: bool


@dataclass(frozen=True)
class AgentApiKeyInput:
    external_ref: str
    label: str | None
    replaces_external_ref: str | None


@dataclass(frozen=True)
class AgentKeyRequest:
    tenant_external_ref: str
    workspace_external_ref: str
    agent_external_ref: str
    api_key: AgentApiKeyInput


@dataclass(frozen=True)
class AgentKeyResult:
    api_key: ApiKey
    plaintext: str | None
    created: bool
    replacement: bool
    idempotency_replayed: bool


def validate_workspace_slug(value: str) -> str:
    if _WORKSPACE_SLUG.fullmatch(value) is None:
        raise ValueError("workspace.slug must be a valid lowercase Core slug")
    return value


def validate_api_key_label(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        not value
        or len(value) > 255
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("api_key.label must be from 1 through 255 characters")
    return value


def workspace_agent_request_digest(request: WorkspaceAgentRequest) -> bytes:
    payload = {
        "v": 1,
        "tenant_external_ref": request.tenant_external_ref,
        "workspace": {
            "external_ref": request.workspace.external_ref,
            "name": request.workspace.name,
            "slug": request.workspace.slug,
        },
        "agent": {
            "external_ref": request.agent.external_ref,
            "name": request.agent.name,
            "type": "agent",
        },
        "membership": {"role": "member"},
    }
    return _canonical_digest(payload)


def agent_key_request_digest(request: AgentKeyRequest) -> bytes:
    payload = {
        "v": 1,
        "tenant_external_ref": request.tenant_external_ref,
        "workspace_external_ref": request.workspace_external_ref,
        "agent_external_ref": request.agent_external_ref,
        "api_key": {
            "external_ref": request.api_key.external_ref,
            "label": request.api_key.label,
            "replaces_external_ref": request.api_key.replaces_external_ref,
        },
        "scopes": FIXED_AGENT_KEY_SCOPES,
        "memory_profile_id": None,
    }
    return _canonical_digest(payload)


def _canonical_digest(payload: dict[str, object]) -> bytes:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(serialized.encode("ascii")).digest()


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("ascii")).digest()


def _conflict(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": code, "message": message},
    )


def _tenant_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "code": "TENANT_BINDING_NOT_FOUND",
            "message": "Tenant provisioning binding was not found",
        },
    )


async def _lock(session: AsyncSession, value: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:value, 0))"),
        {"value": value},
    )


async def _resolve_tenant_binding(
    session: AsyncSession, identity: ServiceClientIdentity, external_ref: str
) -> TenantProvisioningBinding:
    binding = await session.scalar(
        select(TenantProvisioningBinding).where(
            TenantProvisioningBinding.service_client_id == identity.id,
            TenantProvisioningBinding.external_ref == external_ref,
        )
    )
    if binding is None:
        raise _tenant_not_found()
    return binding


async def _load_workspace_agent_result(
    session: AsyncSession,
    workspace_binding_id: uuid.UUID,
    principal_binding_id: uuid.UUID,
    membership_id: uuid.UUID,
    *,
    workspace_created: bool,
    agent_created: bool,
    membership_created: bool,
    replayed: bool,
) -> WorkspaceAgentResult:
    workspace_binding = await session.get(WorkspaceProvisioningBinding, workspace_binding_id)
    principal_binding = await session.get(PrincipalProvisioningBinding, principal_binding_id)
    membership = await session.get(WorkspaceMember, membership_id)
    if workspace_binding is None or principal_binding is None or membership is None:
        raise RuntimeError("workspace-agent provisioning binding integrity failure")
    workspace = await session.get(Workspace, workspace_binding.workspace_id)
    agent = await session.get(Principal, principal_binding.principal_id)
    if workspace is None or agent is None:
        raise RuntimeError("workspace-agent provisioning resource integrity failure")
    return WorkspaceAgentResult(
        workspace=workspace,
        agent=agent,
        membership=membership,
        workspace_created=workspace_created,
        agent_created=agent_created,
        membership_created=membership_created,
        idempotency_replayed=replayed,
    )


async def provision_workspace_agent(
    session: AsyncSession,
    identity: ServiceClientIdentity,
    credential: ServiceClientCredential,
    request: WorkspaceAgentRequest,
    idempotency_key: str,
    request_id: str,
) -> WorkspaceAgentResult:
    await apply_service_client_context(session, identity.id)
    await _lock(
        session,
        f"service:{identity.id}:tenant:{request.tenant_external_ref}",
    )
    tenant_binding = await _resolve_tenant_binding(
        session, identity, request.tenant_external_ref
    )
    await _lock(
        session,
        f"service:{identity.id}:tenant-binding:{tenant_binding.id}:"
        f"workspace:{request.workspace.external_ref}",
    )
    await _lock(
        session,
        f"service:{identity.id}:tenant-binding:{tenant_binding.id}:"
        f"agent:{request.agent.external_ref}",
    )

    workspace_binding = await session.scalar(
        select(WorkspaceProvisioningBinding).where(
            WorkspaceProvisioningBinding.service_client_id == identity.id,
            WorkspaceProvisioningBinding.tenant_binding_id == tenant_binding.id,
            WorkspaceProvisioningBinding.external_ref == request.workspace.external_ref,
        )
    )
    workspace_created = False
    if workspace_binding is None:
        workspace_id = uuid.uuid4()
        try:
            await session.execute(
                text(
                    "INSERT INTO workspaces (id, tenant_id, name, slug, created_at) "
                    "VALUES (:id, :tenant_id, :name, :slug, :created_at)"
                ),
                {
                    "id": workspace_id,
                    "tenant_id": tenant_binding.tenant_id,
                    "name": request.workspace.name,
                    "slug": request.workspace.slug,
                    "created_at": datetime.now(UTC),
                },
            )
        except IntegrityError as exc:
            raise _conflict(
                "WORKSPACE_SLUG_CONFLICT", "Workspace slug is already unavailable"
            ) from exc
        await onboarding_stage("workspace_inserted")
        workspace_binding = WorkspaceProvisioningBinding(
            service_client_id=identity.id,
            tenant_binding_id=tenant_binding.id,
            tenant_id=tenant_binding.tenant_id,
            external_ref=request.workspace.external_ref,
            workspace_id=workspace_id,
        )
        session.add(workspace_binding)
        await session.flush()
        await onboarding_stage("workspace_binding_inserted")
        workspace_created = True
    workspace = await session.get(Workspace, workspace_binding.workspace_id)
    if workspace is None:
        raise RuntimeError("workspace binding refers to a missing workspace")
    if (
        workspace.tenant_id != tenant_binding.tenant_id
        or workspace.name != request.workspace.name
        or workspace.slug != request.workspace.slug
    ):
        raise _conflict(
            "WORKSPACE_EXTERNAL_REF_CONFLICT",
            "Workspace external identity conflicts with request",
        )

    principal_binding = await session.scalar(
        select(PrincipalProvisioningBinding).where(
            PrincipalProvisioningBinding.service_client_id == identity.id,
            PrincipalProvisioningBinding.tenant_binding_id == tenant_binding.id,
            PrincipalProvisioningBinding.external_ref == request.agent.external_ref,
        )
    )
    agent_created = False
    if principal_binding is None:
        principal_id = uuid.uuid4()
        try:
            await session.execute(
                text(
                    "INSERT INTO principals "
                    "(id, tenant_id, name, type, internal_key, created_at) "
                    "VALUES (:id, :tenant_id, :name, 'agent', NULL, :created_at)"
                ),
                {
                    "id": principal_id,
                    "tenant_id": tenant_binding.tenant_id,
                    "name": request.agent.name,
                    "created_at": datetime.now(UTC),
                },
            )
        except IntegrityError as exc:
            raise _conflict(
                "AGENT_NAME_CONFLICT", "Agent name is already unavailable"
            ) from exc
        await onboarding_stage("agent_inserted")
        principal_binding = PrincipalProvisioningBinding(
            service_client_id=identity.id,
            tenant_binding_id=tenant_binding.id,
            tenant_id=tenant_binding.tenant_id,
            external_ref=request.agent.external_ref,
            principal_id=principal_id,
        )
        session.add(principal_binding)
        await session.flush()
        await onboarding_stage("principal_binding_inserted")
        agent_created = True
    agent = await session.get(Principal, principal_binding.principal_id)
    if agent is None:
        raise RuntimeError("principal binding refers to a missing principal")
    if (
        agent.tenant_id != tenant_binding.tenant_id
        or agent.name != request.agent.name
        or agent.type != "agent"
        or agent.internal_key is not None
    ):
        raise _conflict(
            "AGENT_EXTERNAL_REF_CONFLICT",
            "Agent external identity conflicts with request",
        )

    await _lock(
        session,
        f"service:{identity.id}:workspace-binding:{workspace_binding.id}:"
        f"principal-binding:{principal_binding.id}",
    )
    membership = await session.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.principal_id == agent.id,
        )
    )
    membership_created = False
    if membership is None:
        membership_id = uuid.uuid4()
        try:
            await session.execute(
                text(
                    "INSERT INTO workspace_members "
                    "(id, workspace_id, principal_id, role, created_at) "
                    "VALUES (:id, :workspace_id, :principal_id, 'member', :created_at)"
                ),
                {
                    "id": membership_id,
                    "workspace_id": workspace.id,
                    "principal_id": agent.id,
                    "created_at": datetime.now(UTC),
                },
            )
        except IntegrityError as exc:
            raise _conflict(
                "WORKSPACE_MEMBERSHIP_CONFLICT",
                "Workspace membership conflicts with request",
            ) from exc
        membership = await session.get(WorkspaceMember, membership_id)
        if membership is None:
            raise RuntimeError("created workspace membership is unavailable")
        await onboarding_stage("workspace_membership_inserted")
        membership_created = True
    if (
        membership.workspace_id != workspace.id
        or membership.principal_id != agent.id
        or membership.role != "member"
    ):
        raise _conflict(
            "WORKSPACE_MEMBERSHIP_CONFLICT",
            "Workspace membership conflicts with request",
        )

    await _lock(session, f"service:{identity.id}:workspace-agent-idem:{idempotency_key}")
    request_digest = workspace_agent_request_digest(request)
    idempotency_digest = _digest(idempotency_key)
    existing_idempotency = await session.scalar(
        select(ServiceWorkspaceAgentIdempotency).where(
            ServiceWorkspaceAgentIdempotency.service_client_id == identity.id,
            ServiceWorkspaceAgentIdempotency.key_digest == idempotency_digest,
        )
    )
    if existing_idempotency is not None:
        if existing_idempotency.request_digest != request_digest:
            raise _conflict(
                "IDEMPOTENCY_KEY_REUSED",
                "Idempotency key was used with a different request",
            )
        result = await _load_workspace_agent_result(
            session,
            existing_idempotency.workspace_binding_id,
            existing_idempotency.principal_binding_id,
            existing_idempotency.workspace_member_id,
            workspace_created=False,
            agent_created=False,
            membership_created=False,
            replayed=True,
        )
        credential.last_used_at = datetime.now(UTC)
        await _workspace_agent_event(
            session,
            identity,
            credential,
            result,
            request,
            request_id,
            "workspace_agent.idempotent_replay",
        )
        return result

    session.add(
        ServiceWorkspaceAgentIdempotency(
            service_client_id=identity.id,
            key_digest=idempotency_digest,
            request_digest=request_digest,
            workspace_binding_id=workspace_binding.id,
            principal_binding_id=principal_binding.id,
            workspace_member_id=membership.id,
        )
    )
    await session.flush()
    await onboarding_stage("workspace_agent_idempotency_inserted")
    credential.last_used_at = datetime.now(UTC)
    result = WorkspaceAgentResult(
        workspace=workspace,
        agent=agent,
        membership=membership,
        workspace_created=workspace_created,
        agent_created=agent_created,
        membership_created=membership_created,
        idempotency_replayed=False,
    )
    if workspace_created:
        await _workspace_agent_event(
            session, identity, credential, result, request, request_id, "workspace.provisioned"
        )
    if agent_created:
        await _workspace_agent_event(
            session, identity, credential, result, request, request_id, "agent.provisioned"
        )
    if membership_created:
        await _workspace_agent_event(
            session,
            identity,
            credential,
            result,
            request,
            request_id,
            "workspace_member.provisioned",
        )
    if not workspace_created and not agent_created and not membership_created:
        await _workspace_agent_event(
            session,
            identity,
            credential,
            result,
            request,
            request_id,
            "workspace_agent.resolved_existing",
        )
    await onboarding_stage("workspace_agent_success_events_inserted")
    await onboarding_stage("credential_last_used_updated")
    return result


async def _resolve_agent_key_resources(
    session: AsyncSession,
    identity: ServiceClientIdentity,
    request: AgentKeyRequest,
) -> tuple[
    TenantProvisioningBinding,
    WorkspaceProvisioningBinding,
    PrincipalProvisioningBinding,
    Workspace,
    Principal,
    WorkspaceMember,
]:
    tenant_binding = await _resolve_tenant_binding(
        session, identity, request.tenant_external_ref
    )
    workspace_binding = await session.scalar(
        select(WorkspaceProvisioningBinding).where(
            WorkspaceProvisioningBinding.service_client_id == identity.id,
            WorkspaceProvisioningBinding.tenant_binding_id == tenant_binding.id,
            WorkspaceProvisioningBinding.external_ref == request.workspace_external_ref,
        )
    )
    principal_binding = await session.scalar(
        select(PrincipalProvisioningBinding).where(
            PrincipalProvisioningBinding.service_client_id == identity.id,
            PrincipalProvisioningBinding.tenant_binding_id == tenant_binding.id,
            PrincipalProvisioningBinding.external_ref == request.agent_external_ref,
        )
    )
    if workspace_binding is None or principal_binding is None:
        raise _conflict(
            "PROVISIONING_CONFLICT", "Provisioned workspace-agent identity was not found"
        )
    workspace = await session.get(Workspace, workspace_binding.workspace_id)
    agent = await session.get(Principal, principal_binding.principal_id)
    membership = await session.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_binding.workspace_id,
            WorkspaceMember.principal_id == principal_binding.principal_id,
        )
    )
    if (
        workspace is None
        or agent is None
        or membership is None
        or workspace.tenant_id != tenant_binding.tenant_id
        or agent.tenant_id != tenant_binding.tenant_id
        or agent.type != "agent"
        or agent.internal_key is not None
        or membership.role != "member"
    ):
        raise _conflict(
            "PROVISIONING_CONFLICT", "Provisioned workspace-agent identity is unavailable"
        )
    return (
        tenant_binding,
        workspace_binding,
        principal_binding,
        workspace,
        agent,
        membership,
    )


async def _load_agent_key_result(
    session: AsyncSession,
    binding_id: uuid.UUID,
    *,
    replayed: bool,
) -> AgentKeyResult:
    binding = await session.get(AgentApiKeyProvisioningBinding, binding_id)
    if binding is None:
        raise RuntimeError("agent API-key binding integrity failure")
    api_key = await session.get(ApiKey, binding.api_key_id)
    if api_key is None:
        raise RuntimeError("agent API-key resource integrity failure")
    return AgentKeyResult(
        api_key=api_key,
        plaintext=None,
        created=False,
        replacement=binding.replaces_binding_id is not None,
        idempotency_replayed=replayed,
    )


async def provision_agent_api_key(
    session: AsyncSession,
    identity: ServiceClientIdentity,
    credential: ServiceClientCredential,
    request: AgentKeyRequest,
    idempotency_key: str,
    request_id: str,
) -> AgentKeyResult:
    await apply_service_client_context(session, identity.id)
    await _lock(
        session, f"service:{identity.id}:tenant:{request.tenant_external_ref}"
    )
    (
        tenant_binding,
        workspace_binding,
        principal_binding,
        _workspace,
        agent,
        _membership,
    ) = await _resolve_agent_key_resources(session, identity, request)
    await _lock(
        session,
        f"service:{identity.id}:agent-key-pair:"
        f"{workspace_binding.id}:{principal_binding.id}",
    )
    await _lock(
        session,
        f"service:{identity.id}:tenant-binding:{tenant_binding.id}:"
        f"api-key:{request.api_key.external_ref}",
    )
    if request.api_key.replaces_external_ref is not None:
        await _lock(
            session,
            f"service:{identity.id}:tenant-binding:{tenant_binding.id}:"
            f"api-key:{request.api_key.replaces_external_ref}",
        )
    await _lock(session, f"service:{identity.id}:agent-key-idem:{idempotency_key}")

    request_digest = agent_key_request_digest(request)
    idempotency_digest = _digest(idempotency_key)
    existing_idempotency = await session.scalar(
        select(ServiceAgentKeyIdempotency).where(
            ServiceAgentKeyIdempotency.service_client_id == identity.id,
            ServiceAgentKeyIdempotency.key_digest == idempotency_digest,
        )
    )
    if existing_idempotency is not None:
        if existing_idempotency.request_digest != request_digest:
            raise _conflict(
                "IDEMPOTENCY_KEY_REUSED",
                "Idempotency key was used with a different request",
            )
        result = await _load_agent_key_result(
            session, existing_idempotency.api_key_binding_id, replayed=True
        )
        credential.last_used_at = datetime.now(UTC)
        await _agent_key_event(
            session,
            identity,
            credential,
            result,
            request,
            request_id,
            "api_key.idempotent_replay",
        )
        return result

    existing_binding = await session.scalar(
        select(AgentApiKeyProvisioningBinding).where(
            AgentApiKeyProvisioningBinding.service_client_id == identity.id,
            AgentApiKeyProvisioningBinding.tenant_binding_id == tenant_binding.id,
            AgentApiKeyProvisioningBinding.external_ref == request.api_key.external_ref,
        )
    )
    if existing_binding is not None:
        expected_replacement = request.api_key.replaces_external_ref
        reconciliation_prior: AgentApiKeyProvisioningBinding | None = None
        if expected_replacement is not None:
            reconciliation_prior = await session.scalar(
                select(AgentApiKeyProvisioningBinding).where(
                    AgentApiKeyProvisioningBinding.service_client_id == identity.id,
                    AgentApiKeyProvisioningBinding.tenant_binding_id == tenant_binding.id,
                    AgentApiKeyProvisioningBinding.external_ref == expected_replacement,
                )
            )
        if (
            existing_binding.workspace_binding_id != workspace_binding.id
            or existing_binding.principal_binding_id != principal_binding.id
            or (
                expected_replacement is None
                and existing_binding.replaces_binding_id is not None
            )
            or (
                expected_replacement is not None
                and (
                    reconciliation_prior is None
                    or existing_binding.replaces_binding_id != reconciliation_prior.id
                )
            )
        ):
            raise _conflict(
                "API_KEY_EXTERNAL_REF_CONFLICT",
                "API-key external identity conflicts with request",
            )
        result = await _load_agent_key_result(
            session, existing_binding.id, replayed=False
        )
        session.add(
            ServiceAgentKeyIdempotency(
                service_client_id=identity.id,
                key_digest=idempotency_digest,
                request_digest=request_digest,
                api_key_binding_id=existing_binding.id,
            )
        )
        credential.last_used_at = datetime.now(UTC)
        await _agent_key_event(
            session,
            identity,
            credential,
            result,
            request,
            request_id,
            "api_key.resolved_existing",
        )
        return result

    active_binding = await session.scalar(
        select(AgentApiKeyProvisioningBinding).where(
            AgentApiKeyProvisioningBinding.service_client_id == identity.id,
            AgentApiKeyProvisioningBinding.workspace_binding_id == workspace_binding.id,
            AgentApiKeyProvisioningBinding.principal_binding_id == principal_binding.id,
            AgentApiKeyProvisioningBinding.status == "active",
        )
    )
    prior_binding: AgentApiKeyProvisioningBinding | None = None
    replacement = request.api_key.replaces_external_ref is not None
    if not replacement:
        if active_binding is not None:
            raise _conflict(
                "ACTIVE_AGENT_KEY_EXISTS",
                "An active service-provisioned agent key already exists",
            )
    else:
        prior_external_ref = request.api_key.replaces_external_ref
        assert prior_external_ref is not None
        prior_binding = await session.scalar(
            select(AgentApiKeyProvisioningBinding).where(
                AgentApiKeyProvisioningBinding.service_client_id == identity.id,
                AgentApiKeyProvisioningBinding.tenant_binding_id == tenant_binding.id,
                AgentApiKeyProvisioningBinding.external_ref == prior_external_ref,
            )
        )
        if prior_binding is None:
            raise _conflict(
                "REPLACEMENT_KEY_NOT_FOUND", "Replacement target was not found"
            )
        if (
            prior_binding.workspace_binding_id != workspace_binding.id
            or prior_binding.principal_binding_id != principal_binding.id
        ):
            raise _conflict(
                "REPLACEMENT_TARGET_MISMATCH",
                "Replacement target does not match the workspace-agent pair",
            )
        if prior_binding.status != "active":
            raise _conflict(
                "REPLACEMENT_KEY_ALREADY_REPLACED",
                "Replacement target was already replaced",
            )
        if active_binding is None or active_binding.id != prior_binding.id:
            raise _conflict(
                "REPLACEMENT_TARGET_MISMATCH",
                "Replacement target is not the active key",
            )
        prior_api_key = await session.get(ApiKey, prior_binding.api_key_id)
        if prior_api_key is None or prior_api_key.revoked_at is not None:
            raise _conflict(
                "REPLACEMENT_KEY_ALREADY_REPLACED",
                "Replacement target is no longer active",
            )
        replaced_at = datetime.now(UTC)
        revoked_id = await session.scalar(
            text(
                "UPDATE api_keys SET revoked_at = :replaced_at "
                "WHERE id = :api_key_id AND revoked_at IS NULL RETURNING id"
            ),
            {"replaced_at": replaced_at, "api_key_id": prior_api_key.id},
        )
        if revoked_id is None:
            raise _conflict(
                "REPLACEMENT_KEY_ALREADY_REPLACED",
                "Replacement target is no longer active",
            )
        await onboarding_stage("prior_api_key_revoked")
        await session.execute(
            text(
                "UPDATE agent_api_key_provisioning_bindings "
                "SET status = 'replaced', replaced_at = :replaced_at "
                "WHERE id = :binding_id AND status = 'active'"
            ),
            {"replaced_at": replaced_at, "binding_id": prior_binding.id},
        )
        await onboarding_stage("prior_api_key_binding_replaced")

    issued = await issue_api_key(
        session,
        tenant_id=tenant_binding.tenant_id,
        principal_id=agent.id,
        scopes=FIXED_AGENT_KEY_SCOPES,
        label=request.api_key.label,
        memory_profile_id=None,
    )
    await onboarding_stage(
        "replacement_api_key_inserted" if replacement else "api_key_inserted"
    )
    new_binding = AgentApiKeyProvisioningBinding(
        service_client_id=identity.id,
        tenant_binding_id=tenant_binding.id,
        tenant_id=tenant_binding.tenant_id,
        workspace_binding_id=workspace_binding.id,
        principal_binding_id=principal_binding.id,
        external_ref=request.api_key.external_ref,
        api_key_id=issued.api_key.id,
        status="active",
        replaces_binding_id=prior_binding.id if prior_binding is not None else None,
    )
    session.add(new_binding)
    await session.flush()
    await onboarding_stage("api_key_binding_inserted")
    session.add(
        ServiceAgentKeyIdempotency(
            service_client_id=identity.id,
            key_digest=idempotency_digest,
            request_digest=request_digest,
            api_key_binding_id=new_binding.id,
        )
    )
    await session.flush()
    await onboarding_stage("agent_key_idempotency_inserted")
    credential.last_used_at = datetime.now(UTC)
    result = AgentKeyResult(
        api_key=issued.api_key,
        plaintext=issued.plaintext,
        created=True,
        replacement=replacement,
        idempotency_replayed=False,
    )
    await _agent_key_event(
        session,
        identity,
        credential,
        result,
        request,
        request_id,
        "api_key.replaced" if replacement else "api_key.provisioned",
    )
    await onboarding_stage("agent_key_success_event_inserted")
    await onboarding_stage("credential_last_used_updated")
    return result


async def record_onboarding_conflict(
    session: AsyncSession,
    identity: ServiceClientIdentity,
    credential: ServiceClientCredential,
    request: WorkspaceAgentRequest | AgentKeyRequest,
    request_id: str,
    reason_code: str,
) -> None:
    if isinstance(request, WorkspaceAgentRequest):
        tenant_ref = request.tenant_external_ref
        workspace_ref = request.workspace.external_ref
        principal_ref = request.agent.external_ref
        api_key_ref = None
    else:
        tenant_ref = request.tenant_external_ref
        workspace_ref = request.workspace_external_ref
        principal_ref = request.agent_external_ref
        api_key_ref = request.api_key.external_ref
    session.add(
        ServiceProvisioningEvent(
            service_client_id=identity.id,
            credential_id=credential.id,
            event_type="onboarding.conflict",
            outcome="failure",
            request_id=request_id[:128],
            reason_code=reason_code[:64],
            external_tenant_ref_digest=_digest(tenant_ref),
            external_workspace_ref_digest=_digest(workspace_ref),
            external_principal_ref_digest=_digest(principal_ref),
            external_api_key_ref_digest=(
                _digest(api_key_ref) if api_key_ref is not None else None
            ),
            details={},
        )
    )


async def _workspace_agent_event(
    session: AsyncSession,
    identity: ServiceClientIdentity,
    credential: ServiceClientCredential,
    result: WorkspaceAgentResult,
    request: WorkspaceAgentRequest,
    request_id: str,
    event_type: str,
) -> None:
    session.add(
        ServiceProvisioningEvent(
            service_client_id=identity.id,
            credential_id=credential.id,
            tenant_id=result.workspace.tenant_id,
            workspace_id=result.workspace.id,
            principal_id=result.agent.id,
            event_type=event_type,
            outcome="success",
            request_id=request_id[:128],
            external_tenant_ref_digest=_digest(request.tenant_external_ref),
            external_workspace_ref_digest=_digest(request.workspace.external_ref),
            external_principal_ref_digest=_digest(request.agent.external_ref),
            details={
                "workspace_created": result.workspace_created,
                "agent_created": result.agent_created,
                "membership_created": result.membership_created,
            },
        )
    )


async def _agent_key_event(
    session: AsyncSession,
    identity: ServiceClientIdentity,
    credential: ServiceClientCredential,
    result: AgentKeyResult,
    request: AgentKeyRequest,
    request_id: str,
    event_type: str,
) -> None:
    session.add(
        ServiceProvisioningEvent(
            service_client_id=identity.id,
            credential_id=credential.id,
            tenant_id=result.api_key.tenant_id,
            principal_id=result.api_key.principal_id,
            api_key_id=result.api_key.id,
            event_type=event_type,
            outcome="success",
            request_id=request_id[:128],
            external_tenant_ref_digest=_digest(request.tenant_external_ref),
            external_workspace_ref_digest=_digest(request.workspace_external_ref),
            external_principal_ref_digest=_digest(request.agent_external_ref),
            external_api_key_ref_digest=_digest(request.api_key.external_ref),
            details={
                "created": result.created,
                "replacement": result.replacement,
                "credential_secret_available": result.created,
            },
        )
    )
