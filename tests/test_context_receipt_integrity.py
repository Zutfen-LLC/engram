"""Pure-Python integrity tests for stored context-receipt verification
(ENG-CONTEXT-002A).

These tests build a ``ContextReceipt`` ORM object directly (no database round
trip for the core cases) and exercise ``verify_context_receipt_record`` to prove
the stored-manifest integrity check detects tampering of every kind:

- tampered JSONB manifest;
- tampered manifest_hash;
- tampered packet_hash;
- wrong schema / version / canonicalization / mode envelope;
- wrong tenant / principal envelope;
- explicit nulls survive a JSONB round trip;
- RFC 8785 canonicalization remains stable after a JSONB reload;
- a negative signed authority (``-0`` collapsed to ``0`` by RFC 8785) remains
  valid;
- no raw content and no raw query is stored in the manifest;
- receipt ID / creation time / retention metadata changes do not affect the
  manifest hash.

The DB-tampering variant (an owner session mutates a stored row) is covered in
``test_context_receipt_store_postgres.py``. App-role mutation remains prohibited
and is proven in ``test_context_receipts_postgres.py``.
"""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from engram.context_manifest import (
    ContextManifestV1,
    compute_manifest_hash,
)
from engram.context_receipts import (
    ContextReceiptIntegrityError,
    verify_context_receipt_record,
)
from engram.models import ContextReceipt
from tests.context_receipt_helpers import build_manifest


def _make_receipt(
    manifest: ContextManifestV1 | None = None,
    *,
    tenant_id: uuid.UUID | None = None,
    principal_id: uuid.UUID | None = None,
    manifest_hash_override: str | None = None,
    packet_hash_override: str | None = None,
    manifest_override: dict[str, Any] | None = None,
    retention_expires_at: datetime | None = None,
) -> ContextReceipt:
    """Construct a ContextReceipt ORM object without a session.

    When a manifest is supplied, tenant/principal default to the manifest's
    subject identity so the receipt is internally consistent unless a test
    deliberately overrides them.
    """
    if manifest is None:
        manifest = build_manifest(
            tenant_id=str(tenant_id or uuid.uuid4()),
            principal_id=str(principal_id or uuid.uuid4()),
            item_ids=[str(uuid.uuid4())],
        )
    tid = tenant_id or uuid.UUID(manifest.subject.tenant_id)
    pid = principal_id or uuid.UUID(manifest.subject.principal_id)
    payload = manifest_override if manifest_override is not None else (
        manifest.model_dump(mode="json", by_alias=True, exclude_none=False)
    )
    return ContextReceipt(
        id=uuid.uuid4(),
        tenant_id=tid,
        principal_id=pid,
        recall_log_id=uuid.uuid4(),
        manifest_schema=manifest.schema_name,
        manifest_schema_version=manifest.schema_version,
        canonicalization=manifest.canonicalization,
        mode=manifest.mode,
        manifest=payload,
        manifest_hash=manifest_hash_override or compute_manifest_hash(manifest),
        packet_hash=packet_hash_override or manifest.packet.hash,
        retention_expires_at=retention_expires_at,
        created_at=datetime.now(UTC),
    )


# ─── Positive ──────────────────────────────────────────────────────────


def test_valid_stored_row_verifies() -> None:
    receipt = _make_receipt()
    parsed = verify_context_receipt_record(receipt)
    assert isinstance(parsed, ContextManifestV1)
    assert parsed.schema_name == receipt.manifest_schema


def test_valid_stored_row_verifies_with_retention() -> None:
    receipt = _make_receipt(
        retention_expires_at=datetime.now(UTC) + timedelta(days=30)
    )
    parsed = verify_context_receipt_record(receipt)
    assert parsed.schema_name == receipt.manifest_schema


def test_rfc8785_canonicalization_stable_after_jsonb_reload() -> None:
    """A manifest stored as JSONB and re-parsed recanonicalizes to its hash."""
    receipt = _make_receipt()
    # Simulate a JSONB round trip: the manifest dict is the JSONB representation.
    # Re-parsing it through the model and recomputing the hash must agree.
    reloaded_manifest = ContextManifestV1.model_validate(receipt.manifest)
    assert compute_manifest_hash(reloaded_manifest) == receipt.manifest_hash
    parsed = verify_context_receipt_record(receipt)
    assert compute_manifest_hash(parsed) == receipt.manifest_hash


def test_explicit_nulls_survive_jsonb_round_trip() -> None:
    """Explicit nulls in optional fields surface as JSON null and re-validate."""
    # Build a manifest with an empty items list and null profile fields, then
    # verify the stored JSONB (with explicit nulls) re-parses and re-hashes.
    manifest = build_manifest(
        tenant_id=str(uuid.uuid4()),
        principal_id=str(uuid.uuid4()),
        item_ids=[],
    )
    payload = manifest.model_dump(mode="json", by_alias=True, exclude_none=False)
    # Confirm explicit nulls are present (not omitted).
    assert payload["subject"]["memory_profile_id"] is None
    assert payload["request"]["query_digest"] is None
    assert payload["result"]["message"] is None
    receipt = ContextReceipt(
        id=uuid.uuid4(),
        tenant_id=uuid.UUID(manifest.subject.tenant_id),
        principal_id=uuid.UUID(manifest.subject.principal_id),
        recall_log_id=uuid.uuid4(),
        manifest_schema=manifest.schema_name,
        manifest_schema_version=manifest.schema_version,
        canonicalization=manifest.canonicalization,
        mode=manifest.mode,
        manifest=payload,
        manifest_hash=compute_manifest_hash(manifest),
        packet_hash=manifest.packet.hash,
        retention_expires_at=None,
        created_at=datetime.now(UTC),
    )
    parsed = verify_context_receipt_record(receipt)
    assert parsed.subject.memory_profile_id is None
    assert compute_manifest_hash(parsed) == receipt.manifest_hash


# ─── Negative: tampered manifest ───────────────────────────────────────


def test_tampered_jsonb_manifest_fails() -> None:
    receipt = _make_receipt()
    tampered = copy.deepcopy(dict(receipt.manifest))
    # Mutate a served-content hash inside an item.
    tampered["items"][0]["served_content_hash"] = "sha256:" + "0" * 64
    receipt.manifest = tampered
    with pytest.raises(ContextReceiptIntegrityError):
        verify_context_receipt_record(receipt)


def test_tampered_manifest_hash_fails() -> None:
    receipt = _make_receipt()
    receipt.manifest_hash = "sha256:" + "0" * 64
    with pytest.raises(ContextReceiptIntegrityError):
        verify_context_receipt_record(receipt)


def test_tampered_packet_hash_fails() -> None:
    receipt = _make_receipt()
    receipt.packet_hash = "sha256:" + "0" * 64
    with pytest.raises(ContextReceiptIntegrityError):
        verify_context_receipt_record(receipt)


# ─── Negative: envelope agreement ──────────────────────────────────────


def test_wrong_schema_fails() -> None:
    receipt = _make_receipt()
    receipt.manifest_schema = "engram.something-else"
    with pytest.raises(ContextReceiptIntegrityError):
        verify_context_receipt_record(receipt)


def test_wrong_schema_version_fails() -> None:
    receipt = _make_receipt()
    receipt.manifest_schema_version = "2.0"
    with pytest.raises(ContextReceiptIntegrityError):
        verify_context_receipt_record(receipt)


def test_wrong_canonicalization_fails() -> None:
    receipt = _make_receipt()
    receipt.canonicalization = "json-sort-keys"
    with pytest.raises(ContextReceiptIntegrityError):
        verify_context_receipt_record(receipt)


def test_wrong_mode_fails() -> None:
    receipt = _make_receipt()
    receipt.mode = "semantic"
    with pytest.raises(ContextReceiptIntegrityError):
        verify_context_receipt_record(receipt)


def test_wrong_tenant_envelope_fails() -> None:
    receipt = _make_receipt()
    receipt.tenant_id = uuid.uuid4()  # different from manifest subject
    with pytest.raises(ContextReceiptIntegrityError):
        verify_context_receipt_record(receipt)


def test_wrong_principal_envelope_fails() -> None:
    receipt = _make_receipt()
    receipt.principal_id = uuid.uuid4()
    with pytest.raises(ContextReceiptIntegrityError):
        verify_context_receipt_record(receipt)


def test_manifest_hash_field_inside_manifest_fails() -> None:
    receipt = _make_receipt()
    tampered = copy.deepcopy(dict(receipt.manifest))
    tampered["manifest_hash"] = "sha256:" + "a" * 64
    receipt.manifest = tampered
    with pytest.raises(ContextReceiptIntegrityError):
        verify_context_receipt_record(receipt)


# ─── Privacy: no raw content or query stored ───────────────────────────


def test_no_raw_content_is_stored() -> None:
    marker = "secret-content-must-not-leak-into-manifest"
    manifest = build_manifest(
        tenant_id=str(uuid.uuid4()),
        principal_id=str(uuid.uuid4()),
        item_ids=[str(uuid.uuid4())],
        content=marker,
    )
    payload = manifest.model_dump(mode="json", by_alias=True, exclude_none=False)

    def walk(o: object):
        if isinstance(o, dict):
            for v in o.values():
                yield from walk(v)
        elif isinstance(o, list):
            for v in o:
                yield from walk(v)
        else:
            yield o

    values = [str(v) for v in walk(payload)]
    assert not any(marker in v for v in values), "raw content leaked into manifest"


def test_no_raw_query_is_stored() -> None:
    manifest = build_manifest(
        tenant_id=str(uuid.uuid4()),
        principal_id=str(uuid.uuid4()),
        item_ids=[str(uuid.uuid4())],
    )
    payload = manifest.model_dump(mode="json", by_alias=True, exclude_none=False)
    # Startup manifests have no query field at all; query_digest is null.
    assert "query" not in payload
    assert payload["request"]["query_digest"] is None


# ─── Determinism: receipt metadata is outside the manifest hash ────────


def test_receipt_id_change_does_not_affect_manifest_hash() -> None:
    manifest = build_manifest(
        tenant_id=str(uuid.uuid4()),
        principal_id=str(uuid.uuid4()),
        item_ids=[str(uuid.uuid4())],
    )
    mh = compute_manifest_hash(manifest)
    r1 = _make_receipt(manifest)
    r2 = _make_receipt(manifest)
    r2.id = uuid.uuid4()
    # The manifest hash is computed over the manifest, not the receipt envelope.
    assert r1.manifest_hash == mh
    assert r2.manifest_hash == mh
    assert verify_context_receipt_record(r1).model_dump(
        mode="json", by_alias=True, exclude_none=False
    ) == verify_context_receipt_record(r2).model_dump(
        mode="json", by_alias=True, exclude_none=False
    )


def test_created_time_change_does_not_affect_manifest_hash() -> None:
    manifest = build_manifest(
        tenant_id=str(uuid.uuid4()),
        principal_id=str(uuid.uuid4()),
        item_ids=[str(uuid.uuid4())],
    )
    mh = compute_manifest_hash(manifest)
    r1 = _make_receipt(manifest)
    r2 = _make_receipt(manifest)
    r2.created_at = datetime.now(UTC) + timedelta(days=1)
    assert r1.manifest_hash == mh
    assert r2.manifest_hash == mh


def test_retention_change_does_not_affect_manifest_hash() -> None:
    manifest = build_manifest(
        tenant_id=str(uuid.uuid4()),
        principal_id=str(uuid.uuid4()),
        item_ids=[str(uuid.uuid4())],
    )
    mh = compute_manifest_hash(manifest)
    r1 = _make_receipt(manifest, retention_expires_at=None)
    r2 = _make_receipt(
        manifest, retention_expires_at=datetime.now(UTC) + timedelta(days=30)
    )
    assert r1.manifest_hash == mh
    assert r2.manifest_hash == mh


def test_recall_log_id_change_does_not_affect_manifest_hash() -> None:
    manifest = build_manifest(
        tenant_id=str(uuid.uuid4()),
        principal_id=str(uuid.uuid4()),
        item_ids=[str(uuid.uuid4())],
    )
    mh = compute_manifest_hash(manifest)
    r1 = _make_receipt(manifest)
    r2 = _make_receipt(manifest)
    r2.recall_log_id = uuid.uuid4()
    assert r1.manifest_hash == mh
    assert r2.manifest_hash == mh


# ─── RFC 8785 number format: negative signed zero ──────────────────────


def test_negative_signed_zero_authority_remains_valid() -> None:
    """RFC 8785 collapses ``-0`` to ``0``; a zero authority round-trips.

    Authority is an int, so this primarily exercises that the canonicalizer
    handles zero without instability. The manifest hash must be stable across a
    JSONB round trip when an item carries authority 0.
    """
    from engram.context_manifest import build_startup_context_manifest_v1
    from tests.context_receipt_helpers import (
        ManifestResponse,
        make_item,
        make_request_input,
        make_subject,
        make_versions,
    )

    tid = str(uuid.uuid4())
    pid = str(uuid.uuid4())
    iid = str(uuid.uuid4())
    items = [make_item(ordinal=0, item_id=iid, content="c", authority=0)]
    response = ManifestResponse(items=items)
    manifest = build_startup_context_manifest_v1(
        response=response,
        subject_context=make_subject(tenant_id=tid, principal_id=pid),
        request_context=make_request_input(byte_budget=8192),
        decision_versions=make_versions(),
    )
    receipt = _make_receipt(manifest)
    parsed = verify_context_receipt_record(receipt)
    assert parsed.items[0].authority == 0
    assert compute_manifest_hash(parsed) == receipt.manifest_hash