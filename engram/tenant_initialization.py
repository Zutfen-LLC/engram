"""Shared, transactional initialization for newly created tenants."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engram.memory_kinds import seed_builtin_kinds
from engram.models import Tenant, TenantConfig


async def initialize_tenant(session: AsyncSession, tenant: Tenant) -> None:
    """Install the complete builtin registry and the active production config.

    This deliberately does not commit.  Callers compose tenant creation with
    their own transaction so no caller can expose a half-initialized tenant.
    """
    await seed_builtin_kinds(session, tenant.id)
    config = await session.scalar(
        select(TenantConfig).where(
            TenantConfig.tenant_id == tenant.id, TenantConfig.active.is_(True)
        )
    )
    if config is None:
        session.add(
            TenantConfig(tenant_id=tenant.id, active=True, auto_promote_evidence_enabled=True)
        )
