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

import base64
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import or_, select
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
    "ContextReceiptVerificationCheck",
    "ContextReceiptVerificationResult",
    "InvalidCursorError",
    "PartialProfileContextError",
    "VERIFICATION_CHECK_CODES",
    "VERIFICATION_FAILURE_CODES",
    "decode_receipt_cursor",
    "encode_receipt_cursor",
    "get_context_receipt",
    "get_context_receipt_for_recall_log",
    "list_context_receipts",
    "profile_eligible",
    "store_context_receipt",
    "verify_context_receipt_record",
    "verify_context_receipt_with_recall_log",
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
    """A stored receipt failed manifest/envelope integrity verification.

    ``code`` carries a stable failure code (see ``VERIFICATION_FAILURE_CODES``)
    so the REST verify route can return it without parsing the exception
    message.  The message itself remains a human-readable description and is
    never surfaced to API callers.
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code or VERIFICATION_FAILURE_UNKNOWN


class PartialProfileContextError(ContextReceiptError):
    """Exactly one of memory_profile_id and memory_profile_revision_id is null.

    A partially specified profile context is an invalid state — it must not be
    treated as unprofiled (which would return unrestricted receipts). The
    repository raises this so callers can fail closed before executing the
    list query. If this error reaches the REST route, it is translated to the
    same generic invalid-credential behavior used for an incoherent
    ResolvedMemoryContext, without leaking profile details.
    """


# ─── Stable verification check and failure codes ───────────────────────


# Stable check codes (the ``checks[].code`` values in the verify response).
VERIFICATION_CHECK_MANIFEST_PARSE = "manifest_parse"
VERIFICATION_CHECK_MANIFEST_HASH = "manifest_hash"
VERIFICATION_CHECK_PACKET_HASH_BINDING = "packet_hash_binding"
VERIFICATION_CHECK_ENVELOPE_SCHEMA = "envelope_schema"
VERIFICATION_CHECK_ENVELOPE_SCHEMA_VERSION = "envelope_schema_version"
VERIFICATION_CHECK_ENVELOPE_CANONICALIZATION = "envelope_canonicalization"
VERIFICATION_CHECK_ENVELOPE_MODE = "envelope_mode"
VERIFICATION_CHECK_SUBJECT_TENANT = "subject_tenant"
VERIFICATION_CHECK_SUBJECT_PRINCIPAL = "subject_principal"
VERIFICATION_CHECK_EMBEDDED_MANIFEST_HASH_ABSENT = "embedded_manifest_hash_absent"
VERIFICATION_CHECK_RECALL_LOG_EXISTS = "recall_log_exists"
VERIFICATION_CHECK_RECALL_LOG_BINDING = "recall_log_binding"

VERIFICATION_CHECK_CODES: tuple[str, ...] = (
    VERIFICATION_CHECK_MANIFEST_PARSE,
    VERIFICATION_CHECK_MANIFEST_HASH,
    VERIFICATION_CHECK_PACKET_HASH_BINDING,
    VERIFICATION_CHECK_ENVELOPE_SCHEMA,
    VERIFICATION_CHECK_ENVELOPE_SCHEMA_VERSION,
    VERIFICATION_CHECK_ENVELOPE_CANONICALIZATION,
    VERIFICATION_CHECK_ENVELOPE_MODE,
    VERIFICATION_CHECK_SUBJECT_TENANT,
    VERIFICATION_CHECK_SUBJECT_PRINCIPAL,
    VERIFICATION_CHECK_EMBEDDED_MANIFEST_HASH_ABSENT,
    VERIFICATION_CHECK_RECALL_LOG_EXISTS,
    VERIFICATION_CHECK_RECALL_LOG_BINDING,
)

# Stable failure codes (``failure_code`` in the verify response).
VERIFICATION_FAILURE_MANIFEST_PARSE_FAILED = "MANIFEST_PARSE_FAILED"
VERIFICATION_FAILURE_MANIFEST_HASH_MISMATCH = "MANIFEST_HASH_MISMATCH"
VERIFICATION_FAILURE_PACKET_HASH_BINDING_MISMATCH = "PACKET_HASH_BINDING_MISMATCH"
VERIFICATION_FAILURE_ENVELOPE_PROTOCOL_MISMATCH = "ENVELOPE_PROTOCOL_MISMATCH"
VERIFICATION_FAILURE_SUBJECT_OWNERSHIP_MISMATCH = "SUBJECT_OWNERSHIP_MISMATCH"
VERIFICATION_FAILURE_EMBEDDED_MANIFEST_HASH_FORBIDDEN = "EMBEDDED_MANIFEST_HASH_FORBIDDEN"
VERIFICATION_FAILURE_RECALL_LOG_NOT_FOUND = "RECALL_LOG_NOT_FOUND"
VERIFICATION_FAILURE_RECALL_LOG_BINDING_MISMATCH = "RECALL_LOG_BINDING_MISMATCH"
VERIFICATION_FAILURE_UNKNOWN = "UNKNOWN"

VERIFICATION_FAILURE_CODES: tuple[str, ...] = (
    VERIFICATION_FAILURE_MANIFEST_PARSE_FAILED,
    VERIFICATION_FAILURE_MANIFEST_HASH_MISMATCH,
    VERIFICATION_FAILURE_PACKET_HASH_BINDING_MISMATCH,
    VERIFICATION_FAILURE_ENVELOPE_PROTOCOL_MISMATCH,
    VERIFICATION_FAILURE_SUBJECT_OWNERSHIP_MISMATCH,
    VERIFICATION_FAILURE_EMBEDDED_MANIFEST_HASH_FORBIDDEN,
    VERIFICATION_FAILURE_RECALL_LOG_NOT_FOUND,
    VERIFICATION_FAILURE_RECALL_LOG_BINDING_MISMATCH,
)


@dataclass(frozen=True)
class ContextReceiptVerificationCheck:
    """One named verification check and its pass/fail result."""

    code: str
    passed: bool


@dataclass(frozen=True)
class ContextReceiptVerificationResult:
    """Structured result of full receipt verification.

    ``status`` is ``"valid"`` when every check passed, ``"invalid"`` when at
    least one failed.  ``failure_code`` is the stable code of the first failing
    check (or ``None`` when status is valid).  ``manifest`` is the parsed
    :class:`ContextManifestV1` on success, ``None`` on failure.
    """

    status: Literal["valid", "invalid"]
    checks: list[ContextReceiptVerificationCheck] = field(default_factory=list)
    failure_code: str | None = None
    manifest: ContextManifestV1 | None = None
    recomputed_manifest_hash: str | None = None
    manifest_packet_hash: str | None = None


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
    to a nonmatching recall log. Startup query data must remain absent on both
    sides: the manifest has no query, and the recall log's ``query`` column must
    be NULL for a startup receipt (a startup recall log that leaked a query
    cannot be the parent of a startup manifest).

    Compared fields: tenant ID, principal ID, mode, ordered item IDs, effective
    byte budget, effective token budget (both including null state), startup
    query absence (recall_log.query IS NULL), scoring version, config version,
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

    # Effective budgets. Startup recall resolves caller-supplied budgets
    # against defaults FIRST, then writes the resolved values into the recall
    # log (see engram.recall._resolve_recall_budgets and the RecallLog insert).
    # The recall log therefore attests the effective budgets the manifest must
    # match exactly — including the null state. A receipt that claims a
    # decision input the parent recall did not use is an integrity conflict.
    if recall_log.byte_budget != manifest.request.effective.byte_budget:
        raise ContextReceiptConflictError(
            "recall log byte_budget does not match manifest effective byte_budget"
        )
    if recall_log.token_budget != manifest.request.effective.token_budget:
        raise ContextReceiptConflictError(
            "recall log token_budget does not match manifest effective token_budget"
        )

    # Startup query data must remain absent. A startup recall log with a
    # non-null query cannot be the parent of a startup manifest (which has no
    # query).
    if recall_log.query is not None:
        raise ContextReceiptConflictError(
            "recall log query must be null for a startup receipt"
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


def _verify_stored_record(receipt: ContextReceipt) -> ContextReceiptVerificationResult:
    """Verify stored-artifact integrity and return a structured result.

    This is the single source of truth for stored-record verification. It runs
    every check from ``VERIFICATION_CHECK_CODES`` (except ``recall_log_exists``
    and ``recall_log_binding`` which require the parent recall log) and returns
    a :class:`ContextReceiptVerificationResult`.

    On success ``status`` is ``"valid"`` and ``manifest`` is the parsed
    :class:`ContextManifestV1`.  On failure ``status`` is ``"invalid"`` and
    ``failure_code`` is the stable code of the first failing check.

    Every result — including manifest parse failure and embedded-manifest-hash
    failure — contains all 10 stored-record check codes exactly once in
    ``VERIFICATION_CHECK_CODES`` order.
    """
    checks: list[ContextReceiptVerificationCheck] = []
    failure_code: str | None = None
    manifest: ContextManifestV1 | None = None
    recomputed_hash: str | None = None
    packet_hash: str | None = None

    # 0. embedded_manifest_hash_absent — check BEFORE parsing because a
    # smuggled manifest_hash field causes the strict model (extra="forbid")
    # to reject the parse, which would mask the real failure code.
    embedded_hash_forbidden = "manifest_hash" in receipt.manifest

    # 1. manifest_parse
    try:
        manifest = ContextManifestV1.model_validate(receipt.manifest)
        checks.append(
            ContextReceiptVerificationCheck(
                VERIFICATION_CHECK_MANIFEST_PARSE, True
            )
        )
    except Exception:  # noqa: BLE001 — any parse failure is an integrity defect
        # Parse failure: record manifest_parse=False and set failure code.
        checks.append(
            ContextReceiptVerificationCheck(
                VERIFICATION_CHECK_MANIFEST_PARSE, False
            )
        )
        # If the manifest had an embedded manifest_hash field, that is the
        # real defect (not a generic parse failure). The strict model rejects
        # extra fields, so this path is reachable when the field is smuggled.
        if embedded_hash_forbidden:
            failure_code = VERIFICATION_FAILURE_EMBEDDED_MANIFEST_HASH_FORBIDDEN
        else:
            failure_code = VERIFICATION_FAILURE_MANIFEST_PARSE_FAILED

        # On parse failure, the remaining checks cannot be established
        # because there is no parsed manifest to compare against. Set them
        # all to False in canonical order, EXCEPT embedded_manifest_hash_absent
        # which is truthful from the raw stored JSON.
        for code in (
            VERIFICATION_CHECK_MANIFEST_HASH,
            VERIFICATION_CHECK_PACKET_HASH_BINDING,
            VERIFICATION_CHECK_ENVELOPE_SCHEMA,
            VERIFICATION_CHECK_ENVELOPE_SCHEMA_VERSION,
            VERIFICATION_CHECK_ENVELOPE_CANONICALIZATION,
            VERIFICATION_CHECK_ENVELOPE_MODE,
            VERIFICATION_CHECK_SUBJECT_TENANT,
            VERIFICATION_CHECK_SUBJECT_PRINCIPAL,
        ):
            checks.append(ContextReceiptVerificationCheck(code, False))

        # embedded_manifest_hash_absent — truthful from the raw stored JSON.
        checks.append(
            ContextReceiptVerificationCheck(
                VERIFICATION_CHECK_EMBEDDED_MANIFEST_HASH_ABSENT,
                not embedded_hash_forbidden,
            )
        )

        return ContextReceiptVerificationResult(
            status="invalid",
            checks=checks,
            failure_code=failure_code,
            manifest=None,
            recomputed_manifest_hash=None,
            manifest_packet_hash=None,
        )

    assert manifest is not None  # narrowed by the try/except above

    recomputed_hash = compute_manifest_hash(manifest)
    packet_hash = manifest.packet.hash

    def _check(
        code: str, passed: bool, fc: str
    ) -> None:
        nonlocal failure_code
        checks.append(ContextReceiptVerificationCheck(code, passed))
        if not passed and failure_code is None:
            failure_code = fc

    # 2. manifest_hash
    _check(
        VERIFICATION_CHECK_MANIFEST_HASH,
        recomputed_hash == receipt.manifest_hash,
        VERIFICATION_FAILURE_MANIFEST_HASH_MISMATCH,
    )

    # 3. packet_hash_binding
    _check(
        VERIFICATION_CHECK_PACKET_HASH_BINDING,
        manifest.packet.hash == receipt.packet_hash,
        VERIFICATION_FAILURE_PACKET_HASH_BINDING_MISMATCH,
    )

    # 4. envelope_schema
    _check(
        VERIFICATION_CHECK_ENVELOPE_SCHEMA,
        manifest.schema_name == receipt.manifest_schema,
        VERIFICATION_FAILURE_ENVELOPE_PROTOCOL_MISMATCH,
    )

    # 5. envelope_schema_version
    _check(
        VERIFICATION_CHECK_ENVELOPE_SCHEMA_VERSION,
        manifest.schema_version == receipt.manifest_schema_version,
        VERIFICATION_FAILURE_ENVELOPE_PROTOCOL_MISMATCH,
    )

    # 6. envelope_canonicalization
    _check(
        VERIFICATION_CHECK_ENVELOPE_CANONICALIZATION,
        manifest.canonicalization == receipt.canonicalization,
        VERIFICATION_FAILURE_ENVELOPE_PROTOCOL_MISMATCH,
    )

    # 7. envelope_mode
    _check(
        VERIFICATION_CHECK_ENVELOPE_MODE,
        manifest.mode == receipt.mode,
        VERIFICATION_FAILURE_ENVELOPE_PROTOCOL_MISMATCH,
    )

    # 8. subject_tenant
    _check(
        VERIFICATION_CHECK_SUBJECT_TENANT,
        manifest.subject.tenant_id == str(receipt.tenant_id),
        VERIFICATION_FAILURE_SUBJECT_OWNERSHIP_MISMATCH,
    )

    # 9. subject_principal
    _check(
        VERIFICATION_CHECK_SUBJECT_PRINCIPAL,
        manifest.subject.principal_id == str(receipt.principal_id),
        VERIFICATION_FAILURE_SUBJECT_OWNERSHIP_MISMATCH,
    )

    # 10. embedded_manifest_hash_absent — already checked before parse (step 0)
    # so the correct failure code is produced even when the strict model
    # rejects the smuggled field. Here we just record the check result.
    _check(
        VERIFICATION_CHECK_EMBEDDED_MANIFEST_HASH_ABSENT,
        not embedded_hash_forbidden,
        VERIFICATION_FAILURE_EMBEDDED_MANIFEST_HASH_FORBIDDEN,
    )

    status: Literal["valid", "invalid"] = (
        "valid" if failure_code is None else "invalid"
    )
    return ContextReceiptVerificationResult(
        status=status,
        checks=checks,
        failure_code=failure_code,
        manifest=manifest if status == "valid" else None,
        recomputed_manifest_hash=recomputed_hash,
        manifest_packet_hash=packet_hash,
    )


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
    result = _verify_stored_record(receipt)
    if result.status == "valid":
        assert result.manifest is not None
        return result.manifest
    raise ContextReceiptIntegrityError(
        f"stored receipt {receipt.id} failed verification: {result.failure_code}",
        code=result.failure_code,
    )


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


# ─── Cursor pagination (ENG-CONTEXT-003A) ──────────────────────────────


_CURSOR_VERSION = 1
_CURSOR_PAYLOAD_KEYS = frozenset({"v", "created_at", "id"})


class InvalidCursorError(ValueError):
    """Raised when a cursor payload is malformed, unsupported, or unparseable."""


def encode_receipt_cursor(*, created_at: datetime, receipt_id: UUID) -> str:
    """Encode a keyset cursor as opaque URL-safe base64.

    The cursor carries the sort key (``created_at`` DESC, ``id`` DESC) of the
    last row returned, plus a schema version. The payload is canonical JSON so
    the cursor is deterministic for identical inputs.
    """
    if created_at.tzinfo is None:
        raise ValueError("cursor created_at must be timezone-aware")
    payload = {
        "v": _CURSOR_VERSION,
        "created_at": created_at.astimezone(UTC).isoformat(),
        "id": str(receipt_id),
    }
    data = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def decode_receipt_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Decode and validate an opaque keyset cursor.

    Returns ``(created_at, receipt_id)`` with a timezone-aware datetime and a
    validated UUID. Raises :class:`InvalidCursorError` for any malformed
    payload: bad base64, bad JSON, missing/extra fields, unsupported version,
    non-canonical UUID, naive timestamp, or non-canonical timestamp/UUID
    spellings.

    Canonical validation: after decoding and field validation, the entire
    cursor is validated by re-encoding the decoded sort key and requiring
    exact equality with the supplied unpadded cursor. This rejects
    timezone-aware-but-noncanonical representations (Z-form timestamps,
    non-UTC offsets, noncanonical fractional-second spellings) that would
    re-encode differently from the canonical encoder output.
    """
    # Validate base64 first.
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode((cursor + padding).encode())
    except Exception as exc:
        raise InvalidCursorError("invalid cursor encoding") from exc

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidCursorError("invalid cursor JSON") from exc

    if not isinstance(payload, dict):
        raise InvalidCursorError("invalid cursor payload")

    keys = set(payload.keys())
    if keys != _CURSOR_PAYLOAD_KEYS:
        raise InvalidCursorError("invalid cursor fields")

    # Version: reject bool (bool is an int subclass in Python) and require
    # exact integer equality.
    version = payload["v"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise InvalidCursorError("unsupported cursor version")
    if version != _CURSOR_VERSION:
        raise InvalidCursorError("unsupported cursor version")

    # Timestamp: reject naive timestamps. Never assume UTC — a cursor with a
    # naive timestamp is malformed because the encoder always produces tz-aware.
    created_at_str = payload["created_at"]
    if not isinstance(created_at_str, str):
        raise InvalidCursorError("invalid cursor created_at")
    try:
        created_at = datetime.fromisoformat(created_at_str)
    except ValueError as exc:
        raise InvalidCursorError("invalid cursor timestamp") from exc
    if created_at.tzinfo is None:
        raise InvalidCursorError("cursor timestamp must be timezone-aware")

    # UUID: require canonical lowercase 8-4-4-4-12 form.
    id_str = payload["id"]
    if not isinstance(id_str, str):
        raise InvalidCursorError("invalid cursor id")
    try:
        receipt_id = UUID(id_str)
    except (ValueError, AttributeError) as exc:
        raise InvalidCursorError("invalid cursor uuid") from exc
    if id_str != str(receipt_id):
        raise InvalidCursorError("cursor uuid must be canonical lowercase form")

    # Canonical validation: re-encode the decoded sort key and require exact
    # equality with the supplied unpadded cursor. This catches noncanonical
    # timestamp spellings (Z-form, non-UTC offsets, trailing-zero fractional
    # seconds) that represent the same instant but would not reproduce the
    # exact encoder output. JSON key ordering and separators are canonical
    # (sort_keys=True, compact separators), so any payload-level deviation
    # is also caught here.
    canonical = encode_receipt_cursor(
        created_at=created_at, receipt_id=receipt_id
    )
    if canonical != cursor:
        raise InvalidCursorError("cursor is not in canonical encoder form")

    return created_at, receipt_id


# ─── Profile-aware eligibility (ENG-CONTEXT-003A) ──────────────────────


def profile_eligible(
    receipt_manifest: dict[str, object],
    *,
    memory_profile_id: UUID | None,
    memory_profile_revision_id: UUID | None,
) -> bool:
    """Check whether a receipt's manifest subject matches a profile context.

    Unprofiled access (both current IDs ``None``): any receipt already proven to
    belong to the tenant/principal is eligible, regardless of the receipt's
    historical profile identity.  No manifest subject parsing is required.

    Profile-bound access: the manifest's ``memory_profile_id`` and
    ``memory_profile_revision_id`` must both be non-null and exactly equal the
    resolved profile and revision.

    Partially specified current profile context (one None, one non-None) fails
    closed (returns False).

    Profile access is never inferred from slug, version, workspace, or
    principal identity alone.
    """
    # Unprofiled credential: any receipt owned by the same tenant/principal is
    # eligible.  No manifest parsing required — ownership was already proven
    # by the explicit tenant/principal predicates in get/list.
    if memory_profile_id is None and memory_profile_revision_id is None:
        return True

    # Partially specified current profile context: fail closed.
    if memory_profile_id is None or memory_profile_revision_id is None:
        return False

    # Profile-bound: require exact manifest subject match.
    subject = receipt_manifest.get("subject")
    if not isinstance(subject, dict):
        return False

    m_pid = subject.get("memory_profile_id")
    m_rid = subject.get("memory_profile_revision_id")

    def _norm(v: object) -> str | None:
        return str(v) if v is not None else None

    return _norm(m_pid) == str(memory_profile_id) and _norm(m_rid) == str(
        memory_profile_revision_id
    )


# ─── Keyset-paginated timeline (ENG-CONTEXT-003A) ──────────────────────


@dataclass(frozen=True)
class ContextReceiptListResult:
    """Result of :func:`list_context_receipts`."""

    items: list[ContextReceipt]
    next_cursor: str | None


async def list_context_receipts(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    principal_id: UUID,
    limit: int = 50,
    cursor: str | None = None,
    recall_log_id: UUID | None = None,
    memory_profile_id: UUID | None = None,
    memory_profile_revision_id: UUID | None = None,
) -> ContextReceiptListResult:
    """List the principal's readable receipts newest-first.

    Ordered by ``created_at DESC, id DESC`` using a stable keyset cursor so
    pagination never duplicates or omits rows when timestamps are equal.

    Profile narrowing:
      - When the current credential is unprofiled (both ``memory_profile_id``
        and ``memory_profile_revision_id`` are ``None``), no profile filter
        is applied — all receipts owned by the tenant/principal are eligible.
      - When profile-bound (both non-null), only receipts whose manifest
        subject carries the exact same ``memory_profile_id`` AND
        ``memory_profile_revision_id`` (as JSONB string values) are returned.
      - A partially specified context (exactly one null, one non-null) raises
        :class:`PartialProfileContextError` — it is never treated as
        unprofiled.

    Profile filtering is applied as a SQL WHERE clause BEFORE ``LIMIT`` so
    that an empty page means there are genuinely no more eligible rows, and
    ``next_cursor`` is derived from the last eligible row under the filtered
    query.

    Does not perform cryptographic verification for every row (that is the
    verify endpoint's job). A malformed manifest does not make the timeline
    unavailable — the row is still returned with envelope fields intact.

    Does not commit or roll back.
    """
    # Fail closed: a partially specified profile context is invalid.
    # Exactly one of memory_profile_id / memory_profile_revision_id being null
    # must NOT be treated as unprofiled (which would return unrestricted
    # receipts).
    if (memory_profile_id is None) != (memory_profile_revision_id is None):
        raise PartialProfileContextError(
            "partially specified profile context: exactly one of "
            "memory_profile_id and memory_profile_revision_id is null"
        )
    stmt = select(ContextReceipt).where(
        ContextReceipt.tenant_id == tenant_id,
        ContextReceipt.principal_id == principal_id,
    )

    if recall_log_id is not None:
        stmt = stmt.where(ContextReceipt.recall_log_id == recall_log_id)

    # Apply profile narrowing as a SQL filter BEFORE pagination so that
    # an empty page means there are genuinely no more eligible rows.
    profile_bound = (
        memory_profile_id is not None and memory_profile_revision_id is not None
    )
    if profile_bound:
        pid_str = str(memory_profile_id)
        rid_str = str(memory_profile_revision_id)
        stmt = stmt.where(
            ContextReceipt.manifest["subject"]["memory_profile_id"].as_string()
            == pid_str,
            ContextReceipt.manifest["subject"]["memory_profile_revision_id"].as_string()
            == rid_str,
        )

    # Cursor: return rows strictly before (created_at, id) in DESC order.
    # That means: created_at < cursor_created_at OR
    # (created_at == cursor_created_at AND id < cursor_id).
    if cursor is not None:
        cur_created_at, cur_id = decode_receipt_cursor(cursor)
        stmt = stmt.where(
            or_(
                ContextReceipt.created_at < cur_created_at,
                (ContextReceipt.created_at == cur_created_at)
                & (ContextReceipt.id < cur_id),
            )
        )

    # Fetch one extra row to determine whether there is a next page.
    stmt = stmt.order_by(
        ContextReceipt.created_at.desc(),
        ContextReceipt.id.desc(),
    ).limit(limit + 1)

    result = await session.scalars(stmt)
    rows = list(result)
    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = encode_receipt_cursor(
            created_at=last.created_at, receipt_id=last.id
        )

    return ContextReceiptListResult(items=rows, next_cursor=next_cursor)


# ─── Full verification with recall-log binding (ENG-CONTEXT-003A) ──────


def _manifest_failed_parse(result: ContextReceiptVerificationResult) -> bool:
    """True when the stored-record verification failed at the parse step.

    A parse failure means ``manifest`` is None because the JSON could not be
    parsed, not because a downstream check (hash/envelope/ownership) failed.
    """
    return result.failure_code is not None and result.failure_code in (
        VERIFICATION_FAILURE_MANIFEST_PARSE_FAILED,
        VERIFICATION_FAILURE_EMBEDDED_MANIFEST_HASH_FORBIDDEN,
    )


async def verify_context_receipt_with_recall_log(
    session: AsyncSession,
    receipt: ContextReceipt,
    *,
    tenant_id: UUID,
    principal_id: UUID,
) -> ContextReceiptVerificationResult:
    """Verify stored-artifact integrity AND parent recall-log binding.

    Combines:
      1. Stored-record verification (``_verify_stored_record``) — all the
         manifest-hash, packet-hash, envelope, and subject checks.
      2. Parent recall-log binding — loads the owning :class:`RecallLog`
         under explicit tenant/principal predicates and reuses the same
         ``_validate_recall_log_overlap`` contract used at store time.

    Returns a :class:`ContextReceiptVerificationResult` with all 12 checks.
    Never raises ``ContextReceiptIntegrityError`` — a verification failure
    is a structured ``status="invalid"`` result, not an exception. The REST
    route uses this so an integrity defect returns HTTP 200 invalid, not 500.

    The recall-log binding checks (``recall_log_exists`` and
    ``recall_log_binding``) are added to the stored-record checks list.
    """
    stored_result = _verify_stored_record(receipt)
    checks = list(stored_result.checks)

    # When the manifest parsed successfully, we can still check recall-log
    # existence and binding even if another stored-record integrity check
    # already failed. The manifest is preserved by _verify_stored_record only
    # on success; but a manifest that parsed can have failed hash/envelope/
    # ownership checks — in that case the manifest is still usable for
    # recall-log comparison.
    #
    # When the manifest failed to parse, recall_log_binding cannot be
    # established (no manifest to compare), but recall_log_exists must still
    # be checked truthfully.

    manifest: ContextManifestV1 | None = stored_result.manifest

    # If _verify_stored_record set manifest=None due to a non-parse failure
    # (hash/envelope/ownership), re-parse so we can attempt recall-log binding.
    if manifest is None and not _manifest_failed_parse(stored_result):
        try:
            manifest = ContextManifestV1.model_validate(receipt.manifest)
        except Exception:  # noqa: BLE001
            manifest = None

    # recall_log_exists: check independently of stored-record validity.
    recall_log = await session.scalar(
        select(RecallLog).where(
            RecallLog.id == receipt.recall_log_id,
            RecallLog.tenant_id == tenant_id,
            RecallLog.principal_id == principal_id,
        )
    )

    recall_log_exists = recall_log is not None
    checks.append(
        ContextReceiptVerificationCheck(
            VERIFICATION_CHECK_RECALL_LOG_EXISTS, recall_log_exists
        )
    )

    # recall_log_binding: only evaluable when we have both a parsed manifest
    # and the recall log exists. Otherwise it fails.
    binding_ok = False
    if manifest is not None and recall_log_exists:
        assert recall_log is not None  # narrowed by recall_log_exists
        binding_ok = True
        try:
            _validate_recall_log_overlap(
                recall_log=recall_log, manifest=manifest
            )
        except ContextReceiptConflictError:
            binding_ok = False

    checks.append(
        ContextReceiptVerificationCheck(
            VERIFICATION_CHECK_RECALL_LOG_BINDING, binding_ok
        )
    )

    # Determine the overall failure code by precedence:
    # 1. The stored-record failure code (if any) takes precedence.
    # 2. RECALL_LOG_NOT_FOUND if the stored checks passed but the recall log
    #    does not exist.
    # 3. RECALL_LOG_BINDING_MISMATCH if stored checks passed, recall log
    #    exists, but binding failed.
    failure_code = stored_result.failure_code
    if failure_code is None and not recall_log_exists:
        failure_code = VERIFICATION_FAILURE_RECALL_LOG_NOT_FOUND
    elif failure_code is None and not binding_ok:
        failure_code = VERIFICATION_FAILURE_RECALL_LOG_BINDING_MISMATCH

    status: Literal["valid", "invalid"] = (
        "valid" if failure_code is None else "invalid"
    )
    return ContextReceiptVerificationResult(
        status=status,
        checks=checks,
        failure_code=failure_code,
        manifest=manifest if status == "valid" else None,
        recomputed_manifest_hash=stored_result.recomputed_manifest_hash,
        manifest_packet_hash=stored_result.manifest_packet_hash,
    )