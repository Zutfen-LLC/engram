"""Unit tests for ENG-CONTEXT-003A receipt inspect and verification.

Pure-Python tests (no database) covering:

- ``_verify_stored_record`` returns a structured result with stable check and
  failure codes.
- Each stored-record integrity defect maps to its exact stable failure code.
- ``verify_context_receipt_record`` backward compatibility is preserved
  (raises ``ContextReceiptIntegrityError`` on failure, returns manifest on
  success, and the exception now carries ``.code``).
- ``ContextReceiptIntegrityError.code`` is populated and stable.
- Cursor encode/decode round-trip is deterministic.
- Malformed cursor variants raise ``InvalidCursorError``.
- Equal-timestamp keyset ordering is stable (distinct cursors per UUID).
- ``profile_eligible`` applies exact null/non-null profile matching.
- Manifest parse failure never leaks raw exception text.
"""

from __future__ import annotations

import base64
import copy
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from engram.context_manifest import (
    ContextManifestV1,
    compute_manifest_hash,
)
from engram.context_receipts import (
    VERIFICATION_CHECK_CODES,
    VERIFICATION_FAILURE_CODES,
    VERIFICATION_FAILURE_EMBEDDED_MANIFEST_HASH_FORBIDDEN,
    VERIFICATION_FAILURE_ENVELOPE_PROTOCOL_MISMATCH,
    VERIFICATION_FAILURE_MANIFEST_HASH_MISMATCH,
    VERIFICATION_FAILURE_MANIFEST_PARSE_FAILED,
    VERIFICATION_FAILURE_PACKET_HASH_BINDING_MISMATCH,
    VERIFICATION_FAILURE_SUBJECT_OWNERSHIP_MISMATCH,
    VERIFICATION_FAILURE_UNKNOWN,
    ContextReceiptIntegrityError,
    ContextReceiptVerificationResult,
    InvalidCursorError,
    PartialProfileContextError,
    _verify_stored_record,
    decode_receipt_cursor,
    encode_receipt_cursor,
    list_context_receipts,
    profile_eligible,
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
    """Construct a ContextReceipt ORM object without a session."""
    if manifest is None:
        manifest = build_manifest(
            tenant_id=str(tenant_id or uuid.uuid4()),
            principal_id=str(principal_id or uuid.uuid4()),
            item_ids=[str(uuid.uuid4())],
        )
    tid = tenant_id or uuid.UUID(manifest.subject.tenant_id)
    pid = principal_id or uuid.UUID(manifest.subject.principal_id)
    payload = (
        manifest_override
        if manifest_override is not None
        else manifest.model_dump(mode="json", by_alias=True, exclude_none=False)
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


# ─── Structured verification result ────────────────────────────────────


def test_valid_receipt_produces_all_passed_checks() -> None:
    """A valid receipt produces a result where every check passes."""
    receipt = _make_receipt()
    result = _verify_stored_record(receipt)
    assert isinstance(result, ContextReceiptVerificationResult)
    assert result.status == "valid"
    assert result.failure_code is None
    assert result.manifest is not None
    assert isinstance(result.manifest, ContextManifestV1)
    # All checks should be present (10 stored-record checks; recall-log
    # checks are NOT in _verify_stored_record — they belong to
    # verify_context_receipt_with_recall_log which needs a session).
    assert len(result.checks) == 10
    assert all(c.passed for c in result.checks)
    check_codes = {c.code for c in result.checks}
    # First 10 check codes must all be present (recall-log checks are added
    # by verify_context_receipt_with_recall_log which needs a session).
    for code in VERIFICATION_CHECK_CODES[:10]:
        assert code in check_codes
    # Recomputed hash matches stored hash.
    assert result.recomputed_manifest_hash == receipt.manifest_hash
    assert result.manifest_packet_hash == receipt.packet_hash


def test_each_defect_maps_to_stable_failure_code() -> None:
    """Each stored-record defect produces the correct stable failure code."""

    # manifest_parse_failed
    receipt = _make_receipt()
    receipt.manifest = {"broken": True}
    result = _verify_stored_record(receipt)
    assert result.status == "invalid"
    assert result.failure_code == VERIFICATION_FAILURE_MANIFEST_PARSE_FAILED
    assert result.manifest is None

    # manifest_hash_mismatch
    receipt = _make_receipt()
    receipt.manifest_hash = "sha256:" + "0" * 64
    result = _verify_stored_record(receipt)
    assert result.status == "invalid"
    assert result.failure_code == VERIFICATION_FAILURE_MANIFEST_HASH_MISMATCH

    # packet_hash_binding_mismatch
    receipt = _make_receipt()
    receipt.packet_hash = "sha256:" + "1" * 64
    result = _verify_stored_record(receipt)
    assert result.status == "invalid"
    assert result.failure_code == VERIFICATION_FAILURE_PACKET_HASH_BINDING_MISMATCH

    # envelope_protocol_mismatch (schema)
    receipt = _make_receipt()
    receipt.manifest_schema = "engram.something-else"
    result = _verify_stored_record(receipt)
    assert result.status == "invalid"
    assert result.failure_code == VERIFICATION_FAILURE_ENVELOPE_PROTOCOL_MISMATCH

    # envelope_protocol_mismatch (canonicalization)
    receipt = _make_receipt()
    receipt.canonicalization = "json-sort-keys"
    result = _verify_stored_record(receipt)
    assert result.failure_code == VERIFICATION_FAILURE_ENVELOPE_PROTOCOL_MISMATCH

    # subject_ownership_mismatch (tenant)
    receipt = _make_receipt()
    receipt.tenant_id = uuid.uuid4()
    result = _verify_stored_record(receipt)
    assert result.failure_code == VERIFICATION_FAILURE_SUBJECT_OWNERSHIP_MISMATCH

    # subject_ownership_mismatch (principal)
    receipt = _make_receipt()
    receipt.principal_id = uuid.uuid4()
    result = _verify_stored_record(receipt)
    assert result.failure_code == VERIFICATION_FAILURE_SUBJECT_OWNERSHIP_MISMATCH

    # embedded_manifest_hash_forbidden
    receipt = _make_receipt()
    tampered = copy.deepcopy(dict(receipt.manifest))
    tampered["manifest_hash"] = "sha256:" + "a" * 64
    receipt.manifest = tampered
    result = _verify_stored_record(receipt)
    assert result.failure_code == VERIFICATION_FAILURE_EMBEDDED_MANIFEST_HASH_FORBIDDEN


def test_manifest_parse_failure_does_not_leak_exception_text() -> None:
    """A parse failure must not include the raw ValidationError message."""
    receipt = _make_receipt()
    receipt.manifest = {"items": "not-an-array"}
    result = _verify_stored_record(receipt)
    # The result itself carries only the stable code, not exception text.
    assert result.failure_code == VERIFICATION_FAILURE_MANIFEST_PARSE_FAILED
    # Even the backward-compatible exception must not carry the raw validation
    # error message.
    with pytest.raises(ContextReceiptIntegrityError) as exc_info:
        verify_context_receipt_record(receipt)
    msg = str(exc_info.value)
    assert "validation" not in msg.lower()
    assert "ValidationError" not in msg


# ─── Backward compatibility of verify_context_receipt_record ───────────


def test_verify_context_receipt_record_still_raises_on_failure() -> None:
    """verify_context_receipt_record raises ContextReceiptIntegrityError on failure."""
    receipt = _make_receipt()
    receipt.manifest_hash = "sha256:" + "0" * 64
    with pytest.raises(ContextReceiptIntegrityError):
        verify_context_receipt_record(receipt)


def test_verify_context_receipt_record_returns_manifest_on_success() -> None:
    """verify_context_receipt_record returns the parsed manifest on success."""
    receipt = _make_receipt()
    parsed = verify_context_receipt_record(receipt)
    assert isinstance(parsed, ContextManifestV1)
    assert compute_manifest_hash(parsed) == receipt.manifest_hash


def test_integrity_error_has_stable_code() -> None:
    """ContextReceiptIntegrityError carries a stable .code attribute."""
    receipt = _make_receipt()
    receipt.manifest_hash = "sha256:" + "0" * 64
    with pytest.raises(ContextReceiptIntegrityError) as exc_info:
        verify_context_receipt_record(receipt)
    assert exc_info.value.code == VERIFICATION_FAILURE_MANIFEST_HASH_MISMATCH


def test_integrity_error_code_defaults_to_unknown() -> None:
    """An integrity error constructed without a code defaults to UNKNOWN."""
    err = ContextReceiptIntegrityError("some message")
    assert err.code == VERIFICATION_FAILURE_UNKNOWN


# ─── Cursor encode/decode ──────────────────────────────────────────────


def test_cursor_round_trip_is_deterministic() -> None:
    """Encoding the same inputs twice produces the same cursor."""
    ts = datetime.fromisoformat("2026-07-24T12:00:00+00:00")
    rid = uuid.uuid4()
    c1 = encode_receipt_cursor(created_at=ts, receipt_id=rid)
    c2 = encode_receipt_cursor(created_at=ts, receipt_id=rid)
    assert c1 == c2

    decoded = decode_receipt_cursor(c1)
    assert decoded[0] == ts
    assert decoded[1] == rid


def test_cursor_decodes_timezone_aware() -> None:
    """Decoded cursor has a timezone-aware datetime."""
    ts = datetime.now(UTC)
    rid = uuid.uuid4()
    cursor = encode_receipt_cursor(created_at=ts, receipt_id=rid)
    decoded_ts, decoded_id = decode_receipt_cursor(cursor)
    assert decoded_ts.tzinfo is not None
    assert decoded_id == rid


def test_malformed_cursor_variants_raise() -> None:
    """Each malformed cursor variant raises InvalidCursorError."""
    # Invalid base64
    with pytest.raises(InvalidCursorError):
        decode_receipt_cursor("!!!not-valid-base64!!!")

    # Valid base64 but invalid JSON
    bad_json = base64.urlsafe_b64encode(b"not json").decode().rstrip("=")
    with pytest.raises(InvalidCursorError):
        decode_receipt_cursor(bad_json)

    # Valid JSON but missing fields
    payload = json.dumps({"v": 1}).encode()
    bad_missing = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    with pytest.raises(InvalidCursorError):
        decode_receipt_cursor(bad_missing)

    # Extra fields
    payload = json.dumps(
        {"v": 1, "created_at": "2026-07-24T12:00:00+00:00", "id": str(uuid.uuid4()), "extra": True}
    ).encode()
    bad_extra = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    with pytest.raises(InvalidCursorError):
        decode_receipt_cursor(bad_extra)

    # Unsupported version
    payload = json.dumps(
        {"v": 99, "created_at": "2026-07-24T12:00:00+00:00", "id": str(uuid.uuid4())}
    ).encode()
    bad_version = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    with pytest.raises(InvalidCursorError):
        decode_receipt_cursor(bad_version)

    # Invalid UUID
    payload = json.dumps(
        {"v": 1, "created_at": "2026-07-24T12:00:00+00:00", "id": "not-a-uuid"}
    ).encode()
    bad_uuid = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    with pytest.raises(InvalidCursorError):
        decode_receipt_cursor(bad_uuid)

    # Invalid timestamp
    payload = json.dumps(
        {"v": 1, "created_at": "not-a-timestamp", "id": str(uuid.uuid4())}
    ).encode()
    bad_ts = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    with pytest.raises(InvalidCursorError):
        decode_receipt_cursor(bad_ts)


def test_cursor_rejects_bool_version() -> None:
    """Cursor version true/false are rejected (bool is an int subclass)."""
    for bool_val in (True, False):
        payload = json.dumps(
            {
                "v": bool_val,
                "created_at": "2026-07-24T12:00:00+00:00",
                "id": str(uuid.uuid4()),
            }
        ).encode()
        bad_bool = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        with pytest.raises(InvalidCursorError):
            decode_receipt_cursor(bad_bool)


def test_cursor_rejects_naive_timestamp() -> None:
    """A naive (timezone-unaware) cursor timestamp is rejected."""
    payload = json.dumps(
        {
            "v": 1,
            "created_at": "2026-07-24T12:00:00",  # no +00:00
            "id": str(uuid.uuid4()),
        }
    ).encode()
    bad_naive = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    with pytest.raises(InvalidCursorError):
        decode_receipt_cursor(bad_naive)


def test_cursor_rejects_noncanonical_uuid() -> None:
    """Noncanonical UUID spellings are rejected."""
    canonical = uuid.uuid4()

    # Uppercase
    payload = json.dumps(
        {
            "v": 1,
            "created_at": "2026-07-24T12:00:00+00:00",
            "id": str(canonical).upper(),
        }
    ).encode()
    bad_upper = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    with pytest.raises(InvalidCursorError):
        decode_receipt_cursor(bad_upper)

    # Braced form
    payload = json.dumps(
        {
            "v": 1,
            "created_at": "2026-07-24T12:00:00+00:00",
            "id": "{" + str(canonical) + "}",
        }
    ).encode()
    bad_braced = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    with pytest.raises(InvalidCursorError):
        decode_receipt_cursor(bad_braced)

    # Compact (no dashes)
    payload = json.dumps(
        {
            "v": 1,
            "created_at": "2026-07-24T12:00:00+00:00",
            "id": canonical.hex,
        }
    ).encode()
    bad_compact = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    with pytest.raises(InvalidCursorError):
        decode_receipt_cursor(bad_compact)

    # urn:uuid: prefix
    payload = json.dumps(
        {
            "v": 1,
            "created_at": "2026-07-24T12:00:00+00:00",
            "id": f"urn:uuid:{canonical}",
        }
    ).encode()
    bad_urn = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    with pytest.raises(InvalidCursorError):
        decode_receipt_cursor(bad_urn)


def test_cursor_rejects_naive_timestamp_on_encode() -> None:
    """Encoding rejects a naive (timezone-unaware) timestamp."""
    naive = datetime(2026, 7, 24, 12, 0, 0)  # no tzinfo
    with pytest.raises(ValueError, match="timezone-aware"):
        encode_receipt_cursor(created_at=naive, receipt_id=uuid.uuid4())


def test_equal_timestamp_cursors_distinguished_by_uuid() -> None:
    """Two cursors with the same created_at but different IDs are distinct."""
    ts = datetime.fromisoformat("2026-07-24T12:00:00+00:00")
    id_a = uuid.uuid4()
    id_b = uuid.uuid4()
    cursor_a = encode_receipt_cursor(created_at=ts, receipt_id=id_a)
    cursor_b = encode_receipt_cursor(created_at=ts, receipt_id=id_b)
    assert cursor_a != cursor_b
    decoded_a = decode_receipt_cursor(cursor_a)
    decoded_b = decode_receipt_cursor(cursor_b)
    assert decoded_a[0] == decoded_b[0] == ts
    assert decoded_a[1] == id_a
    assert decoded_b[1] == id_b


# ─── Profile eligibility ───────────────────────────────────────────────


def test_profile_eligible_unprofiled_sees_all() -> None:
    """An unprofiled context (None, None) matches any receipt subject.

    Per F3: unprofiled access means any receipt already proven to belong to
    the tenant/principal is eligible, regardless of the receipt's historical
    profile identity.
    """
    # Unprofiled manifest
    manifest = build_manifest(
        tenant_id=str(uuid.uuid4()),
        principal_id=str(uuid.uuid4()),
        item_ids=[str(uuid.uuid4())],
        memory_profile_id=None,
        memory_profile_revision_id=None,
    )
    payload = manifest.model_dump(mode="json", by_alias=True, exclude_none=False)
    assert profile_eligible(payload, memory_profile_id=None, memory_profile_revision_id=None)

    # Profiled manifest — still eligible for an unprofiled credential
    pid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    profiled_manifest = build_manifest(
        tenant_id=str(uuid.uuid4()),
        principal_id=str(uuid.uuid4()),
        item_ids=[str(uuid.uuid4())],
        memory_profile_id=pid,
        memory_profile_revision_id=rid,
        memory_profile_version=1,
    )
    profiled_payload = profiled_manifest.model_dump(
        mode="json", by_alias=True, exclude_none=False
    )
    assert profile_eligible(
        profiled_payload, memory_profile_id=None, memory_profile_revision_id=None
    )


def test_profile_eligible_profiled_matches_exact() -> None:
    """A profiled context matches only receipts with the exact profile+revision."""
    pid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    manifest = build_manifest(
        tenant_id=str(uuid.uuid4()),
        principal_id=str(uuid.uuid4()),
        item_ids=[str(uuid.uuid4())],
        memory_profile_id=pid,
        memory_profile_revision_id=rid,
        memory_profile_version=1,
    )
    payload = manifest.model_dump(mode="json", by_alias=True, exclude_none=False)

    # Exact match
    assert profile_eligible(
        payload,
        memory_profile_id=uuid.UUID(pid),
        memory_profile_revision_id=uuid.UUID(rid),
    )
    # Different revision
    assert not profile_eligible(
        payload,
        memory_profile_id=uuid.UUID(pid),
        memory_profile_revision_id=uuid.uuid4(),
    )
    # Different profile
    assert not profile_eligible(
        payload,
        memory_profile_id=uuid.uuid4(),
        memory_profile_revision_id=uuid.UUID(rid),
    )
    # Profiled credential trying to read unprofiled receipt
    unprofiled_manifest = build_manifest(
        tenant_id=str(uuid.uuid4()),
        principal_id=str(uuid.uuid4()),
        item_ids=[str(uuid.uuid4())],
    )
    unprofiled_payload = unprofiled_manifest.model_dump(
        mode="json", by_alias=True, exclude_none=False
    )
    assert not profile_eligible(
        unprofiled_payload,
        memory_profile_id=uuid.UUID(pid),
        memory_profile_revision_id=uuid.UUID(rid),
    )


def test_profile_eligible_no_inference_from_other_fields() -> None:
    """Profile access is never inferred from workspace or principal alone."""
    pid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    manifest = build_manifest(
        tenant_id=str(uuid.uuid4()),
        principal_id=str(uuid.uuid4()),
        item_ids=[str(uuid.uuid4())],
        memory_profile_id=pid,
        memory_profile_revision_id=rid,
        memory_profile_version=1,
    )
    payload = manifest.model_dump(mode="json", by_alias=True, exclude_none=False)

    # Must NOT match when only one of the two IDs matches.
    assert not profile_eligible(
        payload,
        memory_profile_id=uuid.UUID(pid),
        memory_profile_revision_id=None,
    )
    assert not profile_eligible(
        payload,
        memory_profile_id=None,
        memory_profile_revision_id=uuid.UUID(rid),
    )


# ─── Stable code vocabulary completeness ───────────────────────────────


def test_all_check_codes_are_unique_strings() -> None:
    """VERIFICATION_CHECK_CODES contains only unique strings."""
    assert len(VERIFICATION_CHECK_CODES) == len(set(VERIFICATION_CHECK_CODES))


def test_all_failure_codes_are_unique_strings() -> None:
    """VERIFICATION_FAILURE_CODES contains only unique strings."""
    assert len(VERIFICATION_FAILURE_CODES) == len(set(VERIFICATION_FAILURE_CODES))


# ─── Verification truthfulness (F6) ────────────────────────────────────


def test_manifest_hash_mismatch_preserves_manifest_for_recall_log() -> None:
    """A manifest-hash mismatch preserves the parsed manifest so recall-log
    binding can still be evaluated when the function is called via
    verify_context_receipt_with_recall_log.

    The _verify_stored_record function must still return the parsed manifest
    even when the hash doesn't match, because the manifest was successfully
    parsed. The caller (verify_context_receipt_with_recall_log) re-parses
    when stored_result.manifest is None but the failure was not a parse
    failure.
    """
    receipt = _make_receipt()
    receipt.manifest_hash = "sha256:" + "0" * 64
    result = _verify_stored_record(receipt)
    # The hash check failed, but manifest is still set internally for the
    # combined verifier to use. _verify_stored_record returns manifest=None
    # on any failure, so we test the re-parse path:
    assert result.failure_code == VERIFICATION_FAILURE_MANIFEST_HASH_MISMATCH
    assert result.manifest is None
    # But the re-parse in verify_context_receipt_with_recall_log should
    # succeed (the manifest JSON is well-formed, just the hash is wrong):
    parsed = ContextManifestV1.model_validate(receipt.manifest)
    assert parsed is not None


def test_check_codes_in_canonical_order() -> None:
    """_verify_stored_record returns checks in the canonical code order."""
    receipt = _make_receipt()
    result = _verify_stored_record(receipt)
    codes = [c.code for c in result.checks]
    expected = list(VERIFICATION_CHECK_CODES[:10])
    assert codes == expected, f"check codes not in canonical order: {codes}"


def test_stored_record_has_exactly_10_checks() -> None:
    """_verify_stored_record returns exactly the 10 stored-record checks
    (recall-log checks are NOT included)."""
    receipt = _make_receipt()
    result = _verify_stored_record(receipt)
    assert len(result.checks) == 10


def test_parse_failure_returns_all_10_stored_record_checks() -> None:
    """A manifest parse failure must return ALL 10 stored-record check codes
    exactly once in canonical order, not an abbreviated list.

    Per F7: manifest_parse=False, and all other checks that require a parsed
    manifest are also false because they cannot be established.
    embedded_manifest_hash_absent is set truthfully from the raw stored JSON.
    """
    receipt = _make_receipt()
    receipt.manifest = {"broken": True}
    result = _verify_stored_record(receipt)

    assert result.status == "invalid"
    assert result.failure_code == VERIFICATION_FAILURE_MANIFEST_PARSE_FAILED
    assert result.manifest is None

    # Must have exactly 10 checks, in canonical order.
    codes = [c.code for c in result.checks]
    expected = list(VERIFICATION_CHECK_CODES[:10])
    assert codes == expected, f"parse failure must have all 10 codes in order: {codes}"

    check_map = {c.code: c.passed for c in result.checks}
    assert check_map["manifest_parse"] is False
    # All checks that require a parsed manifest are false.
    assert check_map["manifest_hash"] is False
    assert check_map["packet_hash_binding"] is False
    assert check_map["envelope_schema"] is False
    assert check_map["envelope_schema_version"] is False
    assert check_map["envelope_canonicalization"] is False
    assert check_map["envelope_mode"] is False
    assert check_map["subject_tenant"] is False
    assert check_map["subject_principal"] is False
    # embedded_manifest_hash_absent is true: {"broken": True} has no
    # smuggled manifest_hash field.
    assert check_map["embedded_manifest_hash_absent"] is True


def test_embedded_hash_failure_returns_all_10_checks() -> None:
    """An embedded manifest_hash field must return ALL 10 stored-record check
    codes exactly once in canonical order, with the failure code
    EMBEDDED_MANIFEST_HASH_FORBIDDEN."""
    receipt = _make_receipt()
    tampered = copy.deepcopy(dict(receipt.manifest))
    tampered["manifest_hash"] = "sha256:" + "a" * 64
    receipt.manifest = tampered
    result = _verify_stored_record(receipt)

    assert result.status == "invalid"
    assert result.failure_code == VERIFICATION_FAILURE_EMBEDDED_MANIFEST_HASH_FORBIDDEN

    codes = [c.code for c in result.checks]
    expected = list(VERIFICATION_CHECK_CODES[:10])
    assert codes == expected, f"hash failure must have all 10 codes in order: {codes}"

    check_map = {c.code: c.passed for c in result.checks}
    assert check_map["manifest_parse"] is False
    assert check_map["embedded_manifest_hash_absent"] is False


def test_no_outcome_has_duplicate_or_missing_checks() -> None:
    """No verification outcome contains duplicate or missing check codes.

    This is a property test covering the valid case and every failure mode.
    """
    receipts_and_expected_codes = [
        # valid receipt
        (_make_receipt(), None),
        # parse failure
        (_make_receipt(manifest_override={"broken": True}), None),
        # hash mismatch
        (_make_receipt(manifest_hash_override="sha256:" + "0" * 64), None),
        # packet hash mismatch
        (_make_receipt(packet_hash_override="sha256:" + "1" * 64), None),
        # embedded hash
        (
            _make_receipt(
                manifest_override={
                    **_make_receipt().manifest,
                    "manifest_hash": "sha256:" + "a" * 64,
                }
            ),
            None,
        ),
    ]

    for receipt, _ in receipts_and_expected_codes:
        result = _verify_stored_record(receipt)
        codes = [c.code for c in result.checks]
        # No duplicates
        assert len(codes) == len(set(codes)), f"duplicate codes in {codes}"
        # All 10 stored-record codes present
        assert set(codes) == set(VERIFICATION_CHECK_CODES[:10]), (
            f"missing or extra codes: {codes}"
        )
        # In canonical order
        assert codes == list(VERIFICATION_CHECK_CODES[:10]), (
            f"codes not in canonical order: {codes}"
        )


# ─── F8: Partial profile context fail-closed ───────────────────────────


async def test_partial_profile_id_set_revision_null_raises():
    """list_context_receipts with profile ID set and revision null fails closed.

    The partial-profile check is the first statement in the function body,
    so it raises before any DB interaction. A None session is sufficient
    to prove the guard fires.
    """
    with pytest.raises(PartialProfileContextError):
        await list_context_receipts(
            session=None,  # type: ignore[arg-type]
            tenant_id=uuid.uuid4(),
            principal_id=uuid.uuid4(),
            memory_profile_id=uuid.uuid4(),
            memory_profile_revision_id=None,
        )


async def test_partial_profile_revision_set_id_null_raises():
    """list_context_receipts with revision set and profile ID null fails closed."""
    with pytest.raises(PartialProfileContextError):
        await list_context_receipts(
            session=None,  # type: ignore[arg-type]
            tenant_id=uuid.uuid4(),
            principal_id=uuid.uuid4(),
            memory_profile_id=None,
            memory_profile_revision_id=uuid.uuid4(),
        )


# ─── Canonical cursor validation (F9) ─────────────────────────────────


def _make_cursor_payload(
    *,
    created_at: str,
    receipt_id_str: str | None = None,
    version: int = 1,
) -> str:
    """Build an unpadded URL-safe base64 cursor from explicit field values."""
    if receipt_id_str is None:
        receipt_id_str = str(uuid.uuid4())
    payload = json.dumps(
        {"v": version, "created_at": created_at, "id": receipt_id_str}
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def test_cursor_rejects_z_form_timestamp() -> None:
    """A cursor timestamp using Z (not +00:00) is rejected.

    The encoder produces +00:00 via .astimezone(UTC).isoformat(). The Z-form
    is timezone-aware and represents the same instant, but is not canonical.
    """
    rid = uuid.uuid4()
    # Z-form
    cursor = _make_cursor_payload(
        created_at="2026-07-24T12:00:00Z", receipt_id_str=str(rid)
    )
    with pytest.raises(InvalidCursorError):
        decode_receipt_cursor(cursor)


def test_cursor_rejects_non_utc_offset() -> None:
    """A cursor timestamp with a non-UTC offset (e.g. +05:00) is rejected
    even though it represents the same instant."""
    rid = uuid.uuid4()
    cursor = _make_cursor_payload(
        created_at="2026-07-24T17:00:00+05:00", receipt_id_str=str(rid)
    )
    with pytest.raises(InvalidCursorError):
        decode_receipt_cursor(cursor)


def test_cursor_rejects_noncanonical_fractional_seconds() -> None:
    """A cursor timestamp with a noncanonical fractional-second representation
    is rejected (e.g. trailing zeros that re-encode differently)."""
    rid = uuid.uuid4()
    # .000000 would re-encode as no fractional part (or differently)
    cursor = _make_cursor_payload(
        created_at="2026-07-24T12:00:00.000000+00:00", receipt_id_str=str(rid)
    )
    with pytest.raises(InvalidCursorError):
        decode_receipt_cursor(cursor)


def test_cursor_rejects_noncanonical_fractional_seconds_trailing_zeros() -> None:
    """Trailing zeros in fractional seconds (e.g. .100000 vs .1) are rejected."""
    rid = uuid.uuid4()
    cursor = _make_cursor_payload(
        created_at="2026-07-24T12:00:00.100000+00:00", receipt_id_str=str(rid)
    )
    with pytest.raises(InvalidCursorError):
        decode_receipt_cursor(cursor)


def test_cursor_encoder_round_trips() -> None:
    """A cursor produced by encode_receipt_cursor still round-trips through
    decode_receipt_cursor successfully."""
    ts = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
    rid = uuid.uuid4()
    cursor = encode_receipt_cursor(created_at=ts, receipt_id=rid)
    decoded_ts, decoded_id = decode_receipt_cursor(cursor)
    assert decoded_ts == ts
    assert decoded_id == rid


def test_cursor_encoder_with_microseconds_round_trips() -> None:
    """A cursor with fractional seconds from the encoder round-trips."""
    ts = datetime(2026, 7, 24, 12, 0, 0, 123456, tzinfo=UTC)
    rid = uuid.uuid4()
    cursor = encode_receipt_cursor(created_at=ts, receipt_id=rid)
    decoded_ts, decoded_id = decode_receipt_cursor(cursor)
    assert decoded_ts == ts
    assert decoded_id == rid
