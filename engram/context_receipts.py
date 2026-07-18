"""Durable Context Receipt repository (ENG-CONTEXT-002A).

Storage-only substrate for the Engram Context Ledger. This module persists and
retrieves immutable ``context_receipts`` rows — the volatile database envelope
for the deterministic ``ContextManifestV1`` (ENG-CONTEXT-001).

Product boundary
----------------
The receipt proves internal consistency of the stored served-context artifact.
It is NOT:

- a truth certificate (it does not prove a memory was factually true);
- proof that an agent used the context;
- proof that a memory caused an action;
- an external signature or KMS attestation.

Transaction ownership
---------------------
Repository functions NEVER ``commit()``, ``rollback()``, or open their own
session. They may execute statements, flush, and return rows. The caller owns
transaction and savepoint behavior — required so ENG-CONTEXT-002B can make
receipt persistence optional without corrupting the recall transaction.

This module has no FastAPI dependencies and no route code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from engram.context_manifest import (
    CANONICALIZATION,
    MANIFEST_CONTRACT_VERSION,
    PACKET_RENDER_VERSION,
    SCHEMA,
    SCHEMA_VERSION,
    STARTUP_MODE,
    ContextManifestV1,
    compute_manifest_hash,
)
from engram.models import ContextReceipt, RecallLog

__all__ = [
    "ContextReceiptConflictError",
    "ContextReceiptError",
    "ContextReceiptIntegrityError",
    "ContextReceiptRecallLogNotFoundError",
    "ContextReceiptStoreResult",
    "get_context_receipt",
    "get_context_receipt_for_recall_log",
    "store_context_receipt",
    "verify_context_receipt_record",
]


# ─── Public types ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class ContextReceiptStoreResult:
    """Result of :func:`store_context_receipt`.

    ``created`` is ``True`` when a new immutable row was inserted, and ``False``
    when an identical receipt already existed for the recall log (idempotent
    retry). A conflicting retry never returns here — it raises
    :class:`ContextReceiptConflictError`.
    """

    receipt: ContextReceipt
    created: bool


class ContextReceiptError(Exception):
    """Base class for context-receipt repository errors."""


class ContextReceiptRecallLogNotFoundError(ContextReceiptError):
    """The parent recall log was not found for the given tenant/principal/ID."""


class ContextReceiptConflictError(ContextReceiptError):
    """A receipt already exists for this recall log with different contents."""


class ContextReceiptIntegrityError(ContextReceiptError):
    """A stored receipt failed manifest/envelope integrity verification."""


# ─── Manifest/envelope validation ──────────────────────────────────────


def _validate_manifest_identity(
    *,
    tenant_id: UUID,
    principal_id: UUID,
    manifest: ContextManifestV1,
) -> None:
    """Require the manifest to describe the caller-supplied identity and protocol.

    Raises :class:`ContextReceiptConflictError` on any mismatch — a mismatched
    manifest is not normalized, it is rejected. The caller must supply a manifest
    built for the exact tenant/principal and startup contract.
    """
    if manifest.subject.tenant_id != str(tenant_id):
        raise ContextReceiptConflictError(
            "manifest subject tenant_id does not match the caller-supplied tenant_id"
        )
    if manifest.subject.principal_id != str(principal_id):
        raise ContextReceiptConflictError(
            "manifest subject principal_id does not match the caller-supplied "
            "principal_id"
        )
    if manifest.schema_name != SCHEMA:
        raise ContextReceiptConflictError(
            f"manifest schema must be {SCHEMA!r}, got {manifest.schema_name!r}"
        )
    if manifest.schema_version != SCHEMA_VERSION:
        raise ContextReceiptConflictError(
            f"manifest schema_version must be {SCHEMA_VERSION!r}, "
            f"got {manifest.schema_version!r}"
        )
    if manifest.canonicalization != CANONICALIZATION:
        raise ContextReceiptConflictError(
            f"manifest canonicalization must be {CANONICALIZATION!r}, "
            f"got {manifest.canonicalization!r}"
        )
    if manifest.mode != STARTUP_MODE:
        raise ContextReceiptConflictError(
            f"manifest mode must be {STARTUP_MODE!r}, got {manifest.mode!r}"
        )
    if manifest.versions.manifest_contract_version != MANIFEST_CONTRACT_VERSION:
        raise ContextReceiptConflictError(
            "manifest versions.manifest_contract_version must be "
            f"{MANIFEST_CONTRACT_VERSION!r}"
        )
    if manifest.packet.render_version != PACKET_RENDER_VERSION:
        raise ContextReceiptConflictError(
            "manifest packet.render_version must be "
            f"{PACKET_RENDER_VERSION!r}"
        )


def _validate_retention(retention_expires_at: datetime | None) -> None:
    """Require a timezone-aware retention expiry when one is supplied."""
    if retention_expires_at is not None and retention_expires_at.tzinfo is None:
        raise ContextReceiptConflictError(
            "retention_expires_at must be timezone-aware when non-null"
        )


# ─── Recall-log validation ─────────────────────────────────────────────


def _normalize_item_ids(
    recall_log_item_ids: list[UUID] | None,
    *,
    manifest_item_count: int,
) -> list[UUID]:
    """Normalize the recall log's item_ids for ordered comparison.

    For an empty manifest, a NULL recall-log item_ids is treated as an empty
    list. Otherwise NULL is invalid (a non-empty startup recall must record its
    item IDs). Order is preserved exactly.
    """
    if recall_log_item_ids is None:
        if manifest_item_count == 0:
            return []
        raise ContextReceiptConflictError(
            "recall log item_ids is null but the manifest is non-empty"
        )
    return list(recall_log_item_ids)


def _ids_match(log_ids: list[UUID], manifest_ids: list[str]) -> bool:
    """Exact ordered equality of UUID lists (log) vs canonical-string lists (manifest)."""
    if len(log_ids) != len(manifest_ids):
        return False
    for log_id, manifest_id in zip(log_ids, manifest_ids, strict=True):
        if str(log_id) != manifest_id:
            return False
    return True


def _profiles_match(
    log_profile_id: UUID | None,
    log_revision_id: UUID | None,
    *,
    manifest_profile_id: str | None,
    manifest_revision_id: str | None,
) -> bool:
    """Exact null/non-null state comparison for profile identity.

    Does not infer a profile from another field. Both sides must be null
    together or both non-null and equal.
    """
    log_profile = str(log_profile_id) if log_profile_id is not None else None
    log_revision = str(log_revision_id) if log_revision_id is not None else None
    return log_profile == manifest_profile_id and log_revision == manifest_revision_id


def _validate_recall_log_overlap(
    *,
    recall_log: RecallLog,
    manifest: ContextManifestV1,
) -> None:
    """Validate every trustworthy overlap between the recall log and the manifest.

    Raises :class:`ContextReceiptConflictError` when a valid manifest is attached
    to a nonmatching recall log. Startup query data remains absent and is not
    compared (the manifest has no query and the recall log's query column is not
    a trustworthy manifest overlap).

    Compared fields: tenant ID, principal ID, mode, ordered item IDs, effective
    byte budget, effective token budget, scoring version, config version,
    memory-context version, memory-profile ID, memory-profile revision ID.
    """
    if recall_log.tenant_id != _to_uuid(manifest.subject.tenant_id):
        raise ContextReceiptConflictError(
            "recall log tenant_id does not match manifest subject tenant_id"
        )
    if recall_log.principal_id != _to_uuid(manifest.subject.principal_id):
        raise ContextReceiptConflictError(
            "recall log principal_id does not match manifest subject principal_id"
        )
    if recall_log.mode != manifest.mode:
        raise ContextReceiptConflictError(
            f"recall log mode {recall_log.mode!r} does not match manifest mode "
            f"{manifest.mode!r}"
        )

    # Ordered item IDs. Preserve order; normalize NULL to empty only for an
    # empty manifest; otherwise require exact equality.
    log_item_ids = _normalize_item_ids(
        recall_log.item_ids, manifest_item_count=len(manifest.items)
    )
    manifest_item_ids = [item.item_id for item in manifest.items]
    if not _ids_match(log_item_ids, manifest_item_ids):
        raise ContextReceiptConflictError(
            "recall log item_ids do not match the manifest's ordered item IDs"
        )

    # Effective budgets. The recall log records the caller-supplied budgets; the
    # manifest records the effective budgets. Startup v1 resolves byte_budget
    # from settings when the caller supplied None, so a non-None effective
    # byte_budget with a None recall-log byte_budget is the normal defaulting
    # path and is NOT a mismatch. Compare only when the recall log attests a
    # non-None budget — that attested value must equal the effective value.
    if (
        recall_log.byte_budget is not None
        and recall_log.byte_budget != manifest.request.effective.byte_budget
    ):
        raise ContextReceiptConflictError(
            "recall log byte_budget does not match manifest effective byte_budget"
        )
    if (
        recall_log.token_budget is not None
        and recall_log.token_budget != manifest.request.effective.token_budget
    ):
        raise ContextReceiptConflictError(
            "recall log token_budget does not match manifest effective token_budget"
        )

    # Decision versions.
    if recall_log.scoring_version != manifest.versions.scoring_version:
        raise ContextReceiptConflictError(
            "recall log scoring_version does not match manifest scoring_version"
        )
    if recall_log.config_version != manifest.versions.config_version:
        raise ContextReceiptConflictError(
            "recall log config_version does not match manifest config_version"
        )

    # Memory-context version.
    if recall_log.memory_context_version != manifest.subject.memory_context_version:
        raise ContextReceiptConflictError(
            "recall log memory_context_version does not match manifest subject "
            "memory_context_version"
        )

    # Profile identity (null/non-null exact comparison; no inference).
    if not _profiles_match(
        recall_log.memory_profile_id,
        recall_log.memory_profile_revision_id,
        manifest_profile_id=manifest.subject.memory_profile_id,
        manifest_revision_id=manifest.subject.memory_profile_revision_id,
    ):
        raise ContextReceiptConflictError(
            "recall log memory profile identity does not match manifest subject "
            "memory profile identity"
        )


def _to_uuid(value: str) -> UUID:
    return UUID(str(value))


# ─── Stored-record verification ────────────────────────────────────────


def verify_context_receipt_record(receipt: ContextReceipt) -> ContextManifestV1:
    """Verify the integrity of a stored receipt and return its parsed manifest.

    Steps:
      1. Parse ``receipt.manifest`` with ``ContextManifestV1.model_validate``.
      2. Recompute the RFC 8785 manifest hash over the parsed manifest.
      3. Compare it to ``receipt.manifest_hash``.
      4. Compare ``manifest.packet.hash`` to ``receipt.packet_hash``.
      5. Compare manifest schema/version/canonicalization/mode to envelope cols.
      6. Compare manifest subject tenant/principal IDs to receipt ownership.
      7. Confirm no ``manifest_hash`` field exists inside the manifest.
      8. Return the parsed manifest on success.

    Raises :class:`ContextReceiptIntegrityError` on any mismatch. Does not load
    memory items or attempt content rehydration (that belongs to ENG-CONTEXT-003).
    """
    try:
        manifest = ContextManifestV1.model_validate(receipt.manifest)
    except Exception as exc:  # noqa: BLE001 — surface any parse failure as integrity
        raise ContextReceiptIntegrityError(
            "stored manifest JSONB failed ContextManifestV1 validation"
        ) from exc

    recomputed_hash = compute_manifest_hash(manifest)
    if recomputed_hash != receipt.manifest_hash:
        raise ContextReceiptIntegrityError(
            "recomputed manifest hash does not match stored manifest_hash"
        )

    if manifest.packet.hash != receipt.packet_hash:
        raise ContextReceiptIntegrityError(
            "manifest packet.hash does not match stored packet_hash"
        )

    if manifest.schema_name != receipt.manifest_schema:
        raise ContextReceiptIntegrityError(
            "manifest schema does not match envelope manifest_schema"
        )
    if manifest.schema_version != receipt.manifest_schema_version:
        raise ContextReceiptIntegrityError(
            "manifest schema_version does not match envelope manifest_schema_version"
        )
    if manifest.canonicalization != receipt.canonicalization:
        raise ContextReceiptIntegrityError(
            "manifest canonicalization does not match envelope canonicalization"
        )
    if manifest.mode != receipt.mode:
        raise ContextReceiptIntegrityError(
            "manifest mode does not match envelope mode"
        )

    if manifest.subject.tenant_id != str(receipt.tenant_id):
        raise ContextReceiptIntegrityError(
            "manifest subject tenant_id does not match receipt tenant_id"
        )
    if manifest.subject.principal_id != str(receipt.principal_id):
        raise ContextReceiptIntegrityError(
            "manifest subject principal_id does not match receipt principal_id"
        )

    # The manifest must not carry a top-level manifest_hash field. The JSONB
    # object is the manifest representation; verification confirms the stored
    # object does not smuggle the hash into the hashed payload.
    if "manifest_hash" in receipt.manifest:
        raise ContextReceiptIntegrityError(
            "stored manifest contains a top-level manifest_hash field"
        )

    return manifest


# ─── Idempotent insertion ──────────────────────────────────────────────


def _manifest_payload(manifest: ContextManifestV1) -> dict[str, object]:
    """The JSONB payload: the manifest's normative wire shape."""
    return manifest.model_dump(mode="json", by_alias=True, exclude_none=False)


def _existing_matches(
    existing: ContextReceipt,
    *,
    manifest: ContextManifestV1,
    manifest_hash: str,
    packet_hash: str,
    tenant_id: UUID,
    principal_id: UUID,
    retention_expires_at: datetime | None,
) -> bool:
    """True iff the existing row is an identical idempotent retry.

    Compares canonical manifest bytes (via re-dump), manifest hash, packet hash,
    tenant/principal identity, and retention metadata.
    """
    if existing.tenant_id != tenant_id:
        return False
    if existing.principal_id != principal_id:
        return False
    if existing.manifest_hash != manifest_hash:
        return False
    if existing.packet_hash != packet_hash:
        return False
    if existing.retention_expires_at != retention_expires_at:
        return False
    # Canonical manifest bytes must match. Re-dump the stored manifest through
    # the model so JSONB key ordering / number formatting differences cannot
    # mask a real content change.
    try:
        stored_manifest = ContextManifestV1.model_validate(existing.manifest)
    except Exception:  # noqa: BLE001 — a corrupt stored row is not an identical retry
        return False
    return _manifest_payload(stored_manifest) == _manifest_payload(manifest)


async def _load_recall_log(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    principal_id: UUID,
    recall_log_id: UUID,
) -> RecallLog:
    """Load the parent recall log with explicit tenant/principal/ID predicates."""
    recall_log = await session.scalar(
        select(RecallLog).where(
            RecallLog.id == recall_log_id,
            RecallLog.tenant_id == tenant_id,
            RecallLog.principal_id == principal_id,
        )
    )
    if recall_log is None:
        raise ContextReceiptRecallLogNotFoundError(
            f"recall log {recall_log_id} not found for tenant {tenant_id} "
            f"principal {principal_id}"
        )
    return recall_log


async def store_context_receipt(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    principal_id: UUID,
    recall_log_id: UUID,
    manifest: ContextManifestV1,
    retention_expires_at: datetime | None = None,
    receipt_id: UUID | None = None,
) -> ContextReceiptStoreResult:
    """Persist an immutable receipt for one recall log, idempotently.

    Accepts a validated :class:`ContextManifestV1` (not an arbitrary dict).
    Computes ``manifest_hash`` from the manifest and reads ``packet_hash`` from
    ``manifest.packet.hash`` — caller-supplied hashes that disagree with the
    manifest are never accepted.

    Behavior:
      - **First insertion:** insert one immutable row, return ``created=True``.
      - **Identical retry:** load the existing row, verify its stored integrity,
        return the existing row with ``created=False``.
      - **Conflicting retry:** raise :class:`ContextReceiptConflictError`. The
        existing row is never updated.

    Concurrent insertion uses PostgreSQL ``INSERT ... ON CONFLICT DO NOTHING``
    against the unique ``recall_log_id`` so two concurrent transactions with
    identical inputs produce exactly one row (one creation, one idempotent
    retrieval). Concurrent different manifests produce one stored row and one
    explicit conflict — no overwrite, no second row.

    This function does NOT commit, roll back, or open its own session. The
    caller owns transaction and savepoint behavior.
    """
    _validate_manifest_identity(
        tenant_id=tenant_id, principal_id=principal_id, manifest=manifest
    )
    _validate_retention(retention_expires_at)

    recall_log = await _load_recall_log(
        session,
        tenant_id=tenant_id,
        principal_id=principal_id,
        recall_log_id=recall_log_id,
    )
    _validate_recall_log_overlap(recall_log=recall_log, manifest=manifest)

    manifest_hash = compute_manifest_hash(manifest)
    packet_hash = manifest.packet.hash
    payload = _manifest_payload(manifest)

    values: dict[str, object] = {
        "tenant_id": tenant_id,
        "principal_id": principal_id,
        "recall_log_id": recall_log_id,
        "manifest_schema": manifest.schema_name,
        "manifest_schema_version": manifest.schema_version,
        "canonicalization": manifest.canonicalization,
        "mode": manifest.mode,
        "manifest": payload,
        "manifest_hash": manifest_hash,
        "packet_hash": packet_hash,
        "retention_expires_at": retention_expires_at,
    }
    if receipt_id is not None:
        values["id"] = receipt_id

    inserted_id = await session.scalar(
        insert(ContextReceipt)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[ContextReceipt.recall_log_id])
        .returning(ContextReceipt.id)
    )

    if inserted_id is not None:
        # Flush so the returned ORM object reflects the INSERT (default created_at
        # etc.) without committing. Re-load via the session identity map.
        await session.flush()
        receipt = await session.scalar(
            select(ContextReceipt).where(ContextReceipt.id == inserted_id)
        )
        assert receipt is not None  # just inserted in this transaction
        return ContextReceiptStoreResult(receipt=receipt, created=True)

    # ON CONFLICT DO NOTHING: a row already exists for this recall_log_id.
    existing = await session.scalar(
        select(ContextReceipt).where(
            ContextReceipt.recall_log_id == recall_log_id,
            ContextReceipt.tenant_id == tenant_id,
            ContextReceipt.principal_id == principal_id,
        )
    )
    if existing is None:
        # The conflicting row belongs to a different tenant/principal (RLS or a
        # cross-ownership attempt). That is a conflict, not an idempotent retry.
        raise ContextReceiptConflictError(
            "a receipt already exists for this recall_log_id under a different "
            "tenant/principal identity"
        )

    # Verify the existing row's stored integrity before comparing.
    verify_context_receipt_record(existing)

    if not _existing_matches(
        existing,
        manifest=manifest,
        manifest_hash=manifest_hash,
        packet_hash=packet_hash,
        tenant_id=tenant_id,
        principal_id=principal_id,
        retention_expires_at=retention_expires_at,
    ):
        raise ContextReceiptConflictError(
            "a receipt already exists for this recall_log_id with different "
            "contents (manifest, hash, packet, identity, or retention metadata)"
        )

    return ContextReceiptStoreResult(receipt=existing, created=False)


# ─── Retrieval ─────────────────────────────────────────────────────────


async def get_context_receipt(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    principal_id: UUID,
    receipt_id: UUID,
) -> ContextReceipt | None:
    """Retrieve a receipt by its ID, scoped to the owning tenant/principal.

    Returns ``None`` when no receipt exists for the given ID under the supplied
    identity. Explicit ownership predicates are applied in addition to RLS
    (defense in depth). Does not commit or roll back.
    """
    receipt = await session.scalar(
        select(ContextReceipt).where(
            ContextReceipt.id == receipt_id,
            ContextReceipt.tenant_id == tenant_id,
            ContextReceipt.principal_id == principal_id,
        )
    )
    return receipt


async def get_context_receipt_for_recall_log(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    principal_id: UUID,
    recall_log_id: UUID,
) -> ContextReceipt | None:
    """Retrieve the (unique) receipt for a recall log, scoped to the owner.

    Returns ``None`` when no receipt exists for the recall log under the
    supplied identity. Explicit ownership predicates are applied in addition to
    RLS (defense in depth). Does not commit or roll back.
    """
    receipt = await session.scalar(
        select(ContextReceipt).where(
            ContextReceipt.recall_log_id == recall_log_id,
            ContextReceipt.tenant_id == tenant_id,
            ContextReceipt.principal_id == principal_id,
        )
    )
    return receipt