"""Atomic service-client tenant and human-principal provisioning."""

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

from engram.db import apply_service_client_context
from engram.models import (
    Principal,
    PrincipalProvisioningBinding,
    ServiceClientCredential,
    ServiceProvisioningEvent,
    ServiceProvisioningIdempotency,
    Tenant,
    TenantProvisioningBinding,
)
from engram.service_auth import ServiceClientIdentity
from engram.tenant_initialization import initialize_tenant

_VISIBLE_ASCII = re.compile(r"^[\x21-\x7e]{1,255}$")
_SLUG = re.compile(r"^[a-z][a-z0-9-]{0,254}$")


@dataclass(frozen=True)
class ProvisionTenantInput:
    external_ref: str
    name: str
    slug: str


@dataclass(frozen=True)
class ProvisionPrincipalInput:
    external_ref: str
    name: str


@dataclass(frozen=True)
class ProvisionRequest:
    tenant: ProvisionTenantInput
    human_principal: ProvisionPrincipalInput


@dataclass(frozen=True)
class ProvisionResult:
    tenant: Tenant
    principal: Principal
    tenant_created: bool
    principal_created: bool
    idempotency_replayed: bool


def validate_external_ref(value: str) -> str:
    if not _VISIBLE_ASCII.fullmatch(value):
        raise ValueError("external_ref must be visible ASCII from 1 through 255 characters")
    return value


def validate_tenant_slug(value: str) -> str:
    if not _SLUG.fullmatch(value):
        raise ValueError("tenant.slug must be lowercase letters, digits, or hyphens")
    return value


def validate_name(value: str, field: str) -> str:
    if not value or len(value) > 255:
        raise ValueError(f"{field} must be from 1 through 255 characters")
    return value


def validate_idempotency_key(value: str | None) -> str:
    if value is None or not _VISIBLE_ASCII.fullmatch(value) or len(value) > 128:
        raise ValueError("Idempotency-Key must be visible ASCII from 1 through 128 characters")
    return value


def canonical_request_digest(request: ProvisionRequest) -> bytes:
    payload = {
        "v": 1,
        "tenant": {
            "external_ref": request.tenant.external_ref,
            "name": request.tenant.name,
            "slug": request.tenant.slug,
        },
        "human_principal": {
            "external_ref": request.human_principal.external_ref,
            "name": request.human_principal.name,
        },
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(serialized.encode("ascii")).digest()


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("ascii")).digest()


def _conflict(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT, detail={"code": code, "message": message}
    )


async def _lock(session: AsyncSession, value: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:value, 0))"), {"value": value}
    )


async def _load_result(
    session: AsyncSession,
    tenant_binding_id: uuid.UUID,
    principal_binding_id: uuid.UUID,
    *,
    tenant_created: bool,
    principal_created: bool,
    replayed: bool,
) -> ProvisionResult:
    tenant_binding = await session.get(TenantProvisioningBinding, tenant_binding_id)
    principal_binding = await session.get(PrincipalProvisioningBinding, principal_binding_id)
    if tenant_binding is None or principal_binding is None:
        raise RuntimeError("provisioning binding integrity failure")
    tenant = await session.get(Tenant, tenant_binding.tenant_id)
    principal = await session.get(Principal, principal_binding.principal_id)
    if tenant is None or principal is None:
        raise RuntimeError("provisioning resource integrity failure")
    return ProvisionResult(tenant, principal, tenant_created, principal_created, replayed)


async def provision_tenant_human(
    session: AsyncSession,
    identity: ServiceClientIdentity,
    credential: ServiceClientCredential,
    request: ProvisionRequest,
    idempotency_key: str,
    request_id: str,
) -> ProvisionResult:
    """Resolve or atomically create a service-owned tenant/user pair.

    The lock order is service+tenant-ref, tenant-ref+principal-ref, then
    service+idempotency-key.  Thus requests with different retry keys for the
    same external identities still serialize before reconciliation.
    """
    await apply_service_client_context(session, identity.id)
    await _lock(session, f"service:{identity.id}:tenant:{request.tenant.external_ref}")
    await _lock(
        session,
        f"service:{identity.id}:tenant:{request.tenant.external_ref}:principal:{request.human_principal.external_ref}",
    )
    await _lock(session, f"service:{identity.id}:idempotency:{idempotency_key}")

    request_digest = canonical_request_digest(request)
    idem_digest = _digest(idempotency_key)
    existing_idem = await session.scalar(
        select(ServiceProvisioningIdempotency).where(
            ServiceProvisioningIdempotency.service_client_id == identity.id,
            ServiceProvisioningIdempotency.key_digest == idem_digest,
        )
    )
    if existing_idem is not None:
        if existing_idem.request_digest != request_digest:
            raise _conflict(
                "IDEMPOTENCY_KEY_REUSED", "Idempotency key was used with a different request"
            )
        result = await _load_result(
            session,
            existing_idem.tenant_binding_id,
            existing_idem.principal_binding_id,
            tenant_created=False,
            principal_created=False,
            replayed=True,
        )
        credential.last_used_at = datetime.now(UTC)
        await _event(
            session,
            identity,
            credential,
            result,
            "provisioning.idempotent_replay",
            request_id,
            request,
        )
        return result

    tenant_binding = await session.scalar(
        select(TenantProvisioningBinding).where(
            TenantProvisioningBinding.service_client_id == identity.id,
            TenantProvisioningBinding.external_ref == request.tenant.external_ref,
        )
    )
    tenant_created = False
    if tenant_binding is None:
        # The unique slug is the only allowed indication of an unbound tenant;
        # no name/slug lookup is used to attach a pre-existing tenant.
        tenant_id = uuid.uuid4()
        tenant = Tenant(
            id=tenant_id,
            name=request.tenant.name,
            slug=request.tenant.slug,
            created_at=datetime.now(UTC),
        )
        try:
            # Do not use INSERT .. RETURNING here. The provisioner may not
            # SELECT an unbound tenant, and the binding is intentionally
            # created immediately after this insert before any tenant read.
            await session.execute(
                text(
                    "INSERT INTO tenants (id, name, slug, created_at) "
                    "VALUES (:id, :name, :slug, :created_at)"
                ),
                {
                    "id": tenant_id,
                    "name": tenant.name,
                    "slug": tenant.slug,
                    "created_at": tenant.created_at,
                },
            )
        except IntegrityError as exc:
            # The caller supplied the slug, so this does not disclose a hidden
            # resource; all other database details stay private.
            raise _conflict("TENANT_SLUG_CONFLICT", "Tenant slug is already unavailable") from exc
        tenant_binding = TenantProvisioningBinding(
            service_client_id=identity.id,
            external_ref=request.tenant.external_ref,
            tenant_id=tenant.id,
        )
        session.add(tenant_binding)
        await session.flush()
        await initialize_tenant(session, tenant)
        tenant_created = True
    else:
        existing_tenant = await session.get(Tenant, tenant_binding.tenant_id)
        if existing_tenant is None:
            raise RuntimeError("tenant binding refers to a missing tenant")
        tenant = existing_tenant
        if tenant.name != request.tenant.name or tenant.slug != request.tenant.slug:
            raise _conflict(
                "PROVISIONING_CONFLICT", "Tenant external identity conflicts with request"
            )

    principal_binding = await session.scalar(
        select(PrincipalProvisioningBinding).where(
            PrincipalProvisioningBinding.service_client_id == identity.id,
            PrincipalProvisioningBinding.tenant_binding_id == tenant_binding.id,
            PrincipalProvisioningBinding.external_ref == request.human_principal.external_ref,
        )
    )
    principal_created = False
    if principal_binding is None:
        principal_id = uuid.uuid4()
        principal = Principal(
            id=principal_id,
            tenant_id=tenant.id,
            name=request.human_principal.name,
            type="user",
        )
        # As with tenant creation, bind before the first ORM SELECT can expose
        # the principal through the role-specific RLS policy.
        try:
            await session.execute(
                text(
                    "INSERT INTO principals (id, tenant_id, name, type, created_at) "
                    "VALUES (:id, :tenant_id, :name, 'user', :created_at)"
                ),
                {
                    "id": principal_id,
                    "tenant_id": tenant.id,
                    "name": principal.name,
                    "created_at": datetime.now(UTC),
                },
            )
        except IntegrityError as exc:
            raise _conflict(
                "PROVISIONING_CONFLICT", "Principal external identity conflicts with request"
            ) from exc
        principal_binding = PrincipalProvisioningBinding(
            service_client_id=identity.id,
            tenant_binding_id=tenant_binding.id,
            tenant_id=tenant.id,
            external_ref=request.human_principal.external_ref,
            principal_id=principal.id,
        )
        session.add(principal_binding)
        await session.flush()
        principal_created = True
    else:
        existing_principal = await session.get(Principal, principal_binding.principal_id)
        if existing_principal is None:
            raise RuntimeError("principal binding refers to a missing principal")
        principal = existing_principal
        if principal.name != request.human_principal.name or principal.type != "user":
            raise _conflict(
                "PROVISIONING_CONFLICT", "Principal external identity conflicts with request"
            )

    session.add(
        ServiceProvisioningIdempotency(
            service_client_id=identity.id,
            key_digest=idem_digest,
            request_digest=request_digest,
            tenant_binding_id=tenant_binding.id,
            principal_binding_id=principal_binding.id,
        )
    )
    credential.last_used_at = datetime.now(UTC)
    result = ProvisionResult(tenant, principal, tenant_created, principal_created, False)
    if tenant_created:
        await _event(
            session, identity, credential, result, "tenant.provisioned", request_id, request
        )
    if principal_created:
        await _event(
            session, identity, credential, result, "principal.provisioned", request_id, request
        )
    if not tenant_created and not principal_created:
        await _event(
            session,
            identity,
            credential,
            result,
            "provisioning.resolved_existing",
            request_id,
            request,
        )
    return result


async def record_provisioning_conflict(
    session: AsyncSession,
    identity: ServiceClientIdentity,
    credential: ServiceClientCredential,
    request: ProvisionRequest,
    request_id: str,
    reason_code: str,
) -> None:
    """Append a bounded deterministic-conflict audit row after savepoint rollback.

    The provisioning work is executed in a savepoint. On a deterministic 409,
    that savepoint rolls back every resource mutation; this append-only row is
    then committed by the surrounding authenticated transaction. It contains
    only stable codes and SHA-256 external-reference digests.
    """
    session.add(
        ServiceProvisioningEvent(
            service_client_id=identity.id,
            credential_id=credential.id,
            event_type="provisioning.conflict",
            outcome="failure",
            request_id=request_id[:128],
            reason_code=reason_code,
            external_tenant_ref_digest=_digest(request.tenant.external_ref),
            external_principal_ref_digest=_digest(request.human_principal.external_ref),
            details={},
        )
    )


async def _event(
    session: AsyncSession,
    identity: ServiceClientIdentity,
    credential: ServiceClientCredential,
    result: ProvisionResult,
    event_type: str,
    request_id: str,
    request: ProvisionRequest | None = None,
) -> None:
    session.add(
        ServiceProvisioningEvent(
            service_client_id=identity.id,
            credential_id=credential.id,
            tenant_id=result.tenant.id,
            principal_id=result.principal.id,
            event_type=event_type,
            outcome="success",
            request_id=request_id[:128],
            external_tenant_ref_digest=(
                _digest(request.tenant.external_ref) if request is not None else None
            ),
            external_principal_ref_digest=(
                _digest(request.human_principal.external_ref) if request is not None else None
            ),
            details={},
        )
    )
