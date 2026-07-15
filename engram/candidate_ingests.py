"""Create and validate immutable, server-owned candidate ingest identities."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engram.models import CandidateIngest


@dataclass(frozen=True)
class CandidateIdentity:
    tenant_id: UUID
    principal_id: UUID
    workspace_id: UUID | None
    source_type: str
    content_hash: str


def create_ingest(
    *, identity: CandidateIdentity, client_correlation_id: UUID | None
) -> CandidateIngest:
    """Build a new server-issued ingest row; the caller owns flush/commit."""
    return CandidateIngest(
        tenant_id=identity.tenant_id,
        principal_id=identity.principal_id,
        workspace_id=identity.workspace_id,
        source_type=identity.source_type,
        content_hash=identity.content_hash,
        client_correlation_id=client_correlation_id,
    )


async def lock_ingest(session: AsyncSession, ingest_id: UUID) -> CandidateIngest | None:
    return (
        await session.execute(
            select(CandidateIngest)
            .where(CandidateIngest.id == ingest_id)
            .with_for_update(read=True, key_share=True)
        )
    ).scalar_one_or_none()


def identity_mismatches(
    ingest: CandidateIngest, identity: CandidateIdentity
) -> tuple[str, ...]:
    mismatches: list[str] = []
    if ingest.tenant_id != identity.tenant_id:
        mismatches.append("tenant_mismatch")
    if ingest.principal_id != identity.principal_id:
        mismatches.append("principal_mismatch")
    if ingest.workspace_id != identity.workspace_id:
        mismatches.append("workspace_mismatch")
    if ingest.source_type != identity.source_type:
        mismatches.append("source_type_mismatch")
    if ingest.content_hash != identity.content_hash:
        mismatches.append("content_hash_mismatch")
    return tuple(mismatches)
