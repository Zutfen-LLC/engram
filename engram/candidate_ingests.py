"""Create and validate immutable, server-owned candidate ingest identities."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from engram.memory_context import ResolvedMemoryContext, context_provenance
from engram.models import CandidateIngest, CandidateIngestExecution


@dataclass(frozen=True)
class CandidateIdentity:
    tenant_id: UUID
    principal_id: UUID
    workspace_id: UUID | None
    source_type: str
    content_hash: str


class ExecutionContextMismatchError(ValueError):
    """The ingest was already consumed under a different request authority."""


def create_ingest(
    *,
    identity: CandidateIdentity,
    client_correlation_id: UUID | None,
    memory_context: ResolvedMemoryContext | None = None,
) -> CandidateIngest:
    """Build a new server-issued ingest row; the caller owns flush/commit."""
    provenance = context_provenance(memory_context) if memory_context is not None else {}
    provenance.pop("tenant_id", None)
    return CandidateIngest(
        tenant_id=identity.tenant_id,
        principal_id=identity.principal_id,
        workspace_id=identity.workspace_id,
        source_type=identity.source_type,
        content_hash=identity.content_hash,
        client_correlation_id=client_correlation_id,
        **provenance,
    )


async def get_ingest(session: AsyncSession, ingest_id: UUID) -> CandidateIngest | None:
    """Load an immutable candidate-ingest identity through ordinary RLS SELECT.

    Candidate ingests cannot be updated by the application role. Receipt
    serialization is owned by the classification-run row lock, so locking this
    immutable row is both misleading and incompatible with least privilege.
    """
    return (
        await session.execute(
            select(CandidateIngest).where(CandidateIngest.id == ingest_id)
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


async def pin_execution_context(
    session: AsyncSession,
    *,
    ingest: CandidateIngest,
    memory_context: ResolvedMemoryContext,
) -> None:
    """Pin the first remember-time authority without altering ingest provenance."""
    provenance = context_provenance(memory_context)
    provenance.pop("tenant_id", None)
    inserted_id = await session.scalar(
        insert(CandidateIngestExecution)
        .values(
            ingest_id=ingest.id,
            tenant_id=ingest.tenant_id,
            principal_id=ingest.principal_id,
            **provenance,
        )
        .on_conflict_do_nothing(index_elements=[CandidateIngestExecution.ingest_id])
        .returning(CandidateIngestExecution.ingest_id)
    )
    if inserted_id is not None:
        return
    pinned = await session.scalar(
        select(CandidateIngestExecution).where(
            CandidateIngestExecution.ingest_id == ingest.id,
            CandidateIngestExecution.tenant_id == ingest.tenant_id,
        )
    )
    if pinned is None:
        raise RuntimeError("candidate execution context conflict without a pinned row")
    expected = (
        memory_context.api_key_id,
        memory_context.memory_profile_id,
        memory_context.memory_profile_revision_id,
        memory_context.version,
    )
    actual = (
        pinned.api_key_id,
        pinned.memory_profile_id,
        pinned.memory_profile_revision_id,
        pinned.memory_context_version,
    )
    if actual != expected:
        raise ExecutionContextMismatchError
