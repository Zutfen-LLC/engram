"""Read-only Context Receipt inspection and verification API (ENG-CONTEXT-003A).

Three endpoints exposing the existing Context Ledger receipt substrate:

  GET /v1/context-receipts
      List the authenticated principal's readable receipts newest-first.

  GET /v1/context-receipts/{receipt_id}
      Inspect one receipt envelope and its exact stored manifest.

  GET /v1/context-receipts/{receipt_id}/verify
      Deterministic read-only verification of the stored artifact and its
      parent recall-log binding.

All routes are read-only, principal-isolated, and profile-narrowed. A missing,
cross-tenant, cross-principal, or profile-ineligible receipt returns the same
non-disclosing 404. Verification is NOT a truth certificate — see the
``limitations`` array in the verify response.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from engram.auth import READ_SCOPE
from engram.context_manifest import ContextManifestV1
from engram.context_receipts import (
    InvalidCursorError,
    get_context_receipt,
    list_context_receipts,
    profile_eligible,
    verify_context_receipt_with_recall_log,
)
from engram.db import get_session
from engram.memory_context import ResolvedMemoryContext, resolve_memory_context
from engram.models import ContextReceipt

router = APIRouter()

_NON_DISCLOSED_404 = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND,
    detail="receipt not found",
)

# The limitations array is identical for every verify response and is never
# derived from request data, so it is a module constant.
_VERIFICATION_LIMITATIONS: list[str] = [
    "Does not prove factual truth.",
    "Does not prove the agent used the context.",
    "Does not prove the context caused an action.",
    "Does not reconstruct or verify historical raw packet bytes.",
    "Does not rehydrate current memory-item content in ENG-CONTEXT-003A.",
]


# ─── List response models ──────────────────────────────────────────────


class ReceiptListItem(BaseModel):
    """Summary of one receipt for the timeline list."""

    id: UUID
    recall_log_id: UUID
    created_at: datetime
    retention_expires_at: datetime | None
    manifest_schema: str
    manifest_schema_version: str
    canonicalization: str
    mode: str
    manifest_hash: str
    packet_hash: str
    manifest_parse_status: Literal["valid", "invalid"]
    item_count: int | None = None
    served_content_byte_count: int | None = None
    rendered_packet_byte_count: int | None = None
    workspace_id: UUID | None = None
    memory_context_version: str | None = None
    memory_profile_id: UUID | None = None
    memory_profile_revision_id: UUID | None = None
    memory_profile_version: int | None = None
    scoring_version: str | None = None
    config_version: str | None = None


class ReceiptListResponse(BaseModel):
    items: list[ReceiptListItem]
    next_cursor: str | None = None


def _try_parse_manifest_summary(
    receipt: ContextReceipt,
) -> dict[str, Any]:
    """Extract manifest-derived summary fields, tolerating parse failure.

    Returns a dict with ``manifest_parse_status``, ``item_count``,
    ``served_content_byte_count``, ``rendered_packet_byte_count``,
    ``workspace_id``, ``memory_context_version``, ``memory_profile_id``,
    ``memory_profile_revision_id``, ``memory_profile_version``,
    ``scoring_version``, and ``config_version``.

    On parse failure, ``manifest_parse_status`` is ``"invalid"`` and the
    manifest-derived fields are ``None``.
    """
    try:
        manifest = ContextManifestV1.model_validate(receipt.manifest)
    except Exception:  # noqa: BLE001 — malformed manifest is a listable state
        return {
            "manifest_parse_status": "invalid",
            "item_count": None,
            "served_content_byte_count": None,
            "rendered_packet_byte_count": None,
            "workspace_id": None,
            "memory_context_version": None,
            "memory_profile_id": None,
            "memory_profile_revision_id": None,
            "memory_profile_version": None,
            "scoring_version": None,
            "config_version": None,
        }

    return {
        "manifest_parse_status": "valid",
        "item_count": manifest.result.item_count,
        "served_content_byte_count": manifest.result.served_content_byte_count,
        "rendered_packet_byte_count": manifest.result.rendered_packet_byte_count,
        "workspace_id": (
            UUID(manifest.request.effective.workspace_id)
            if manifest.request.effective.workspace_id is not None
            else None
        ),
        "memory_context_version": manifest.subject.memory_context_version,
        "memory_profile_id": (
            UUID(manifest.subject.memory_profile_id)
            if manifest.subject.memory_profile_id is not None
            else None
        ),
        "memory_profile_revision_id": (
            UUID(manifest.subject.memory_profile_revision_id)
            if manifest.subject.memory_profile_revision_id is not None
            else None
        ),
        "memory_profile_version": manifest.subject.memory_profile_version,
        "scoring_version": manifest.versions.scoring_version,
        "config_version": manifest.versions.config_version,
    }


def _to_list_item(receipt: ContextReceipt) -> ReceiptListItem:
    summary = _try_parse_manifest_summary(receipt)
    return ReceiptListItem(
        id=receipt.id,
        recall_log_id=receipt.recall_log_id,
        created_at=receipt.created_at,
        retention_expires_at=receipt.retention_expires_at,
        manifest_schema=receipt.manifest_schema,
        manifest_schema_version=receipt.manifest_schema_version,
        canonicalization=receipt.canonicalization,
        mode=receipt.mode,
        manifest_hash=receipt.manifest_hash,
        packet_hash=receipt.packet_hash,
        **summary,
    )


# ─── Detail response model ─────────────────────────────────────────────


class ReceiptDetailResponse(BaseModel):
    """Full receipt envelope with the exact stored manifest JSON object."""

    id: UUID
    recall_log_id: UUID
    created_at: datetime
    retention_expires_at: datetime | None
    manifest_schema: str
    manifest_schema_version: str
    canonicalization: str
    mode: str
    manifest_hash: str
    packet_hash: str
    manifest_parse_status: Literal["valid", "invalid"]
    manifest: dict[str, Any] = Field(
        description=(
            "The exact stored JSON object; no normalization or rehydrated content."
        )
    )


# ─── Verify response models ────────────────────────────────────────────


class VerificationCheckOut(BaseModel):
    code: str
    passed: bool


class ReceiptVerifyResponse(BaseModel):
    receipt_id: UUID
    status: Literal["valid", "invalid"]
    verification_scope: Literal["stored_artifact_and_recall_log_binding"]
    checks: list[VerificationCheckOut]
    failure_code: str | None = None
    stored_manifest_hash: str
    recomputed_manifest_hash: str | None = None
    stored_packet_hash: str
    manifest_packet_hash: str | None = None
    manifest_schema: str | None = None
    manifest_schema_version: str | None = None
    canonicalization: str | None = None
    mode: str | None = None
    limitations: list[str] = Field(
        default_factory=lambda: list(_VERIFICATION_LIMITATIONS)
    )


# ─── Tenant/principal extraction ───────────────────────────────────────


async def _tenant_principal_ids(
    session: AsyncSession,
    memory_context: ResolvedMemoryContext,
) -> tuple[UUID, UUID]:
    """Return (tenant_id, principal_id) from the resolved memory context.

    The memory context is the immutable, authenticated boundary. Its
    tenant_id and principal_id are the explicit ownership predicates
    applied in addition to RLS.
    """
    return memory_context.tenant_id, memory_context.principal_id


# ─── Routes ────────────────────────────────────────────────────────────


@router.get(
    "/context-receipts",
    response_model=ReceiptListResponse,
    dependencies=[Depends(READ_SCOPE)],
)
async def list_receipts(
    limit: int = Query(default=50, ge=1, le=100),  # noqa: B008
    cursor: str | None = Query(default=None),  # noqa: B008
    recall_log_id: UUID | None = Query(default=None),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    memory_context: ResolvedMemoryContext = Depends(resolve_memory_context),  # noqa: B008
) -> ReceiptListResponse:
    """List the authenticated principal's readable receipts newest-first."""
    tenant_id, principal_id = await _tenant_principal_ids(session, memory_context)

    try:
        result = await list_context_receipts(
            session,
            tenant_id=tenant_id,
            principal_id=principal_id,
            limit=limit,
            cursor=cursor,
            recall_log_id=recall_log_id,
            memory_profile_id=memory_context.memory_profile_id,
            memory_profile_revision_id=memory_context.memory_profile_revision_id,
        )
    except InvalidCursorError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid or malformed cursor",
        ) from exc

    items = [_to_list_item(r) for r in result.items]
    return ReceiptListResponse(items=items, next_cursor=result.next_cursor)


@router.get(
    "/context-receipts/{receipt_id}",
    response_model=ReceiptDetailResponse,
    dependencies=[Depends(READ_SCOPE)],
)
async def get_receipt(
    receipt_id: UUID,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    memory_context: ResolvedMemoryContext = Depends(resolve_memory_context),  # noqa: B008
) -> ReceiptDetailResponse:
    """Inspect one receipt envelope and its exact stored manifest."""
    tenant_id, principal_id = await _tenant_principal_ids(session, memory_context)

    receipt = await get_context_receipt(
        session,
        tenant_id=tenant_id,
        principal_id=principal_id,
        receipt_id=receipt_id,
    )
    if receipt is None:
        raise _NON_DISCLOSED_404

    # Profile narrowing: a profile-bound credential may only inspect receipts
    # whose manifest subject exactly matches the resolved profile and revision.
    if not profile_eligible(
        receipt.manifest,
        memory_profile_id=memory_context.memory_profile_id,
        memory_profile_revision_id=memory_context.memory_profile_revision_id,
    ):
        raise _NON_DISCLOSED_404

    try:
        ContextManifestV1.model_validate(receipt.manifest)
        parse_status: Literal["valid", "invalid"] = "valid"
    except Exception:  # noqa: BLE001 — inspection remains possible for invalid rows
        parse_status = "invalid"

    return ReceiptDetailResponse(
        id=receipt.id,
        recall_log_id=receipt.recall_log_id,
        created_at=receipt.created_at,
        retention_expires_at=receipt.retention_expires_at,
        manifest_schema=receipt.manifest_schema,
        manifest_schema_version=receipt.manifest_schema_version,
        canonicalization=receipt.canonicalization,
        mode=receipt.mode,
        manifest_hash=receipt.manifest_hash,
        packet_hash=receipt.packet_hash,
        manifest_parse_status=parse_status,
        manifest=receipt.manifest,
    )


@router.get(
    "/context-receipts/{receipt_id}/verify",
    response_model=ReceiptVerifyResponse,
    dependencies=[Depends(READ_SCOPE)],
)
async def verify_receipt(
    receipt_id: UUID,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    memory_context: ResolvedMemoryContext = Depends(resolve_memory_context),  # noqa: B008
) -> ReceiptVerifyResponse:
    """Perform deterministic read-only verification of one stored receipt."""
    tenant_id, principal_id = await _tenant_principal_ids(session, memory_context)

    receipt = await get_context_receipt(
        session,
        tenant_id=tenant_id,
        principal_id=principal_id,
        receipt_id=receipt_id,
    )
    if receipt is None:
        raise _NON_DISCLOSED_404

    if not profile_eligible(
        receipt.manifest,
        memory_profile_id=memory_context.memory_profile_id,
        memory_profile_revision_id=memory_context.memory_profile_revision_id,
    ):
        raise _NON_DISCLOSED_404

    result = await verify_context_receipt_with_recall_log(
        session,
        receipt,
        tenant_id=tenant_id,
        principal_id=principal_id,
    )

    # Extract manifest-level fields from the result (or envelope on failure).
    manifest_schema: str | None = receipt.manifest_schema
    manifest_schema_version: str | None = receipt.manifest_schema_version
    canonicalization: str | None = receipt.canonicalization
    mode: str | None = receipt.mode

    # On valid verification, prefer the parsed manifest's values for
    # consistency with the verify result. On invalid, envelope cols remain
    # authoritative.
    if result.manifest is not None:
        manifest_schema = result.manifest.schema_name
        manifest_schema_version = result.manifest.schema_version
        canonicalization = result.manifest.canonicalization
        mode = result.manifest.mode

    return ReceiptVerifyResponse(
        receipt_id=receipt.id,
        status=result.status,
        verification_scope="stored_artifact_and_recall_log_binding",
        checks=[
            VerificationCheckOut(code=c.code, passed=c.passed) for c in result.checks
        ],
        failure_code=result.failure_code,
        stored_manifest_hash=receipt.manifest_hash,
        recomputed_manifest_hash=result.recomputed_manifest_hash,
        stored_packet_hash=receipt.packet_hash,
        manifest_packet_hash=result.manifest_packet_hash,
        manifest_schema=manifest_schema,
        manifest_schema_version=manifest_schema_version,
        canonicalization=canonicalization,
        mode=mode,
    )
