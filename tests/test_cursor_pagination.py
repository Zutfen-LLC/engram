"""Unit tests for cursor pagination encode/decode (Correction A — ENG-AUDIT-001-FIX3).

These are pure-function tests — no database or HTTP layer required. They cover
the round-trip behaviour, timezone normalization, and malformed-input rejection
of the items-list pagination cursor.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime

import pytest

from engram.api.routes.memory import (
    InvalidCursorError,
    ItemsCursor,
    _encode_cursor,
    decode_items_cursor,
)


def _make_item(*, created_at: datetime | None = None, item_id=None) -> dict:
    """Build a minimal item dict matching the cursor encoder's expectations."""
    return {
        "created_at": created_at or datetime.now(UTC),
        "id": item_id or uuid.uuid4(),
    }


def test_decode_cursor_returns_typed_datetime() -> None:
    """Round-trip encode→decode produces an ItemsCursor with tz-aware datetime."""
    ts = datetime.fromisoformat("2026-07-22T12:00:00+00:00")
    item_id = uuid.uuid4()
    cursor = _encode_cursor(_make_item(created_at=ts, item_id=item_id))

    decoded = decode_items_cursor(cursor)

    assert isinstance(decoded, ItemsCursor)
    assert decoded.created_at.tzinfo is not None
    assert decoded.item_id == item_id


def test_cursor_handles_trailing_z_suffix() -> None:
    """Encoder produces ISO with Z suffix and decoder normalizes it."""
    item_id = uuid.uuid4()

    # The encoder uses astimezone(UTC).isoformat(), which produces +00:00.
    # Simulate an external encoder that produces a Z suffix by crafting a raw payload.
    payload = json.dumps(
        {"created_at": "2026-07-22T12:00:00Z", "id": str(item_id)},
        separators=(",", ":"),
    ).encode()
    z_cursor = base64.urlsafe_b64encode(payload).decode().rstrip("=")

    decoded = decode_items_cursor(z_cursor)

    assert decoded.created_at == datetime.fromisoformat("2026-07-22T12:00:00+00:00")
    assert decoded.created_at.tzinfo is not None
    assert decoded.item_id == item_id


def test_naive_timestamp_normalized_to_utc() -> None:
    """A cursor with a naive timestamp is normalized to UTC."""
    item_id = uuid.uuid4()
    payload = json.dumps(
        {"created_at": "2026-07-22T12:00:00", "id": str(item_id)},
        separators=(",", ":"),
    ).encode()
    cursor = base64.urlsafe_b64encode(payload).decode().rstrip("=")

    decoded = decode_items_cursor(cursor)

    assert decoded.created_at.tzinfo is not None
    assert decoded.created_at.utcoffset() is not None
    assert decoded.created_at == datetime.fromisoformat("2026-07-22T12:00:00+00:00")


def test_malformed_timestamp_raises_invalid_cursor() -> None:
    """A non-ISO timestamp string raises InvalidCursorError."""
    item_id = uuid.uuid4()
    payload = json.dumps(
        {"created_at": "not-a-timestamp", "id": str(item_id)},
        separators=(",", ":"),
    ).encode()
    cursor = base64.urlsafe_b64encode(payload).decode().rstrip("=")

    with pytest.raises(InvalidCursorError):
        decode_items_cursor(cursor)


def test_malformed_uuid_raises_invalid_cursor() -> None:
    """A non-UUID item_id raises InvalidCursorError."""
    payload = json.dumps(
        {"created_at": "2026-07-22T12:00:00+00:00", "id": "not-a-uuid"},
        separators=(",", ":"),
    ).encode()
    cursor = base64.urlsafe_b64encode(payload).decode().rstrip("=")

    with pytest.raises(InvalidCursorError):
        decode_items_cursor(cursor)


def test_invalid_base64_raises() -> None:
    """Garbage input that cannot be decoded as base64/JSON raises ValueError."""
    with pytest.raises(ValueError):
        decode_items_cursor("!!!not-valid-base64-or-json!!!")


def test_stable_ordering_same_timestamp() -> None:
    """Two items with the same created_at get different cursors distinguished by UUID."""
    ts = datetime.fromisoformat("2026-07-22T12:00:00+00:00")
    item_a = _make_item(created_at=ts, item_id=uuid.uuid4())
    item_b = _make_item(created_at=ts, item_id=uuid.uuid4())

    cursor_a = _encode_cursor(item_a)
    cursor_b = _encode_cursor(item_b)

    assert cursor_a != cursor_b

    decoded_a = decode_items_cursor(cursor_a)
    decoded_b = decode_items_cursor(cursor_b)

    assert decoded_a.created_at == decoded_b.created_at == ts
    assert decoded_a.item_id != decoded_b.item_id
