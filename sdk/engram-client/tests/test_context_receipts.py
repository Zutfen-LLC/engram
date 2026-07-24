"""SDK tests for the context-receipt endpoints (ENG-CONTEXT-003A).

These are transport-level tests using ``httpx.MockTransport`` to verify the
SDK encodes requests and parses responses correctly, including:

- list request encodes limit, cursor, and recall_log_id as query parameters;
- detail request handles 404 using existing SDK conventions;
- verify parses valid and invalid domain results without treating invalid as
  a transport error.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
import pytest

from engram_client import (
    ContextReceiptDetailResponse,
    ContextReceiptListResponse,
    ContextReceiptVerifyResponse,
    EngramClient,
    EngramNotFoundError,
)


def _mock_transport(
    handler: Any,
) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _make_client(handler: Any) -> EngramClient:
    return EngramClient(
        "http://localhost:8000",
        api_key="eng_test_key",
        transport=_mock_transport(handler),
    )


# ─── List ──────────────────────────────────────────────────────────────


async def test_list_encodes_query_params() -> None:
    """list_context_receipts encodes limit, cursor, and recall_log_id."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "00000000-0000-4000-8000-000000000001",
                        "recall_log_id": "00000000-0000-4000-8000-000000000002",
                        "created_at": "2026-07-24T12:00:00+00:00",
                        "retention_expires_at": None,
                        "manifest_schema": "engram.context-manifest",
                        "manifest_schema_version": "1.0",
                        "canonicalization": "rfc8785",
                        "mode": "startup",
                        "manifest_hash": "sha256:" + "a" * 64,
                        "packet_hash": "sha256:" + "b" * 64,
                        "manifest_parse_status": "valid",
                        "item_count": 1,
                    }
                ],
                "next_cursor": None,
            },
        )

    async with _make_client(handler) as client:
        result = await client.list_context_receipts(
            limit=10,
            cursor="abc123",
            recall_log_id=UUID("00000000-0000-4000-8000-000000000002"),
        )

    assert isinstance(result, ContextReceiptListResponse)
    assert len(result.items) == 1
    assert result.items[0].manifest_parse_status == "valid"
    assert result.next_cursor is None

    # Verify query params were sent.
    assert captured["params"]["limit"] == "10"
    assert captured["params"]["cursor"] == "abc123"
    assert captured["params"]["recall_log_id"] == "00000000-0000-4000-8000-000000000002"


async def test_list_default_params() -> None:
    """list_context_receipts with defaults sends only limit=50."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"items": [], "next_cursor": None})

    async with _make_client(handler) as client:
        result = await client.list_context_receipts()

    assert isinstance(result, ContextReceiptListResponse)
    assert len(result.items) == 0
    assert captured["params"]["limit"] == "50"
    assert "cursor" not in captured["params"]
    assert "recall_log_id" not in captured["params"]


# ─── Detail ────────────────────────────────────────────────────────────


async def test_detail_returns_typed_response() -> None:
    """get_context_receipt returns a typed response on success."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "00000000-0000-4000-8000-000000000001",
                "recall_log_id": "00000000-0000-4000-8000-000000000002",
                "created_at": "2026-07-24T12:00:00+00:00",
                "retention_expires_at": None,
                "manifest_schema": "engram.context-manifest",
                "manifest_schema_version": "1.0",
                "canonicalization": "rfc8785",
                "mode": "startup",
                "manifest_hash": "sha256:" + "a" * 64,
                "packet_hash": "sha256:" + "b" * 64,
                "manifest_parse_status": "valid",
                "manifest": {"schema": "engram.context-manifest"},
            },
        )

    async with _make_client(handler) as client:
        result = await client.get_context_receipt(
            receipt_id=UUID("00000000-0000-4000-8000-000000000001")
        )

    assert isinstance(result, ContextReceiptDetailResponse)
    assert result.manifest_parse_status == "valid"
    assert result.manifest == {"schema": "engram.context-manifest"}


async def test_detail_404_raises_not_found_error() -> None:
    """get_context_receipt raises EngramNotFoundError on 404."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "receipt not found"})

    async with _make_client(handler) as client:
        with pytest.raises(EngramNotFoundError):
            await client.get_context_receipt(
                receipt_id=UUID("00000000-0000-4000-8000-000000000099")
            )


# ─── Verify ───────────────────────────────────────────────────────────


async def test_verify_parses_valid_result() -> None:
    """verify_context_receipt parses a valid result without transport error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "receipt_id": "00000000-0000-4000-8000-000000000001",
                "status": "valid",
                "verification_scope": "stored_artifact_and_recall_log_binding",
                "checks": [
                    {"code": "manifest_parse", "passed": True},
                    {"code": "manifest_hash", "passed": True},
                ],
                "failure_code": None,
                "stored_manifest_hash": "sha256:" + "a" * 64,
                "recomputed_manifest_hash": "sha256:" + "a" * 64,
                "stored_packet_hash": "sha256:" + "b" * 64,
                "manifest_packet_hash": "sha256:" + "b" * 64,
                "manifest_schema": "engram.context-manifest",
                "manifest_schema_version": "1.0",
                "canonicalization": "rfc8785",
                "mode": "startup",
                "limitations": [
                    "Does not prove factual truth.",
                ],
            },
        )

    async with _make_client(handler) as client:
        result = await client.verify_context_receipt(
            receipt_id=UUID("00000000-0000-4000-8000-000000000001")
        )

    assert isinstance(result, ContextReceiptVerifyResponse)
    assert result.status == "valid"
    assert result.failure_code is None
    assert len(result.checks) == 2
    assert all(c.passed for c in result.checks)
    assert result.recomputed_manifest_hash == result.stored_manifest_hash


async def test_verify_parses_invalid_result_without_raising() -> None:
    """verify_context_receipt parses an invalid result as a normal 200.

    An integrity failure (status='invalid') is NOT a transport error — it is
    a normal 200 response. The SDK must not raise for it.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "receipt_id": "00000000-0000-4000-8000-000000000001",
                "status": "invalid",
                "verification_scope": "stored_artifact_and_recall_log_binding",
                "checks": [
                    {"code": "manifest_parse", "passed": True},
                    {"code": "manifest_hash", "passed": False},
                ],
                "failure_code": "MANIFEST_HASH_MISMATCH",
                "stored_manifest_hash": "sha256:" + "a" * 64,
                "recomputed_manifest_hash": "sha256:" + "c" * 64,
                "stored_packet_hash": "sha256:" + "b" * 64,
                "manifest_packet_hash": "sha256:" + "b" * 64,
                "manifest_schema": "engram.context-manifest",
                "manifest_schema_version": "1.0",
                "canonicalization": "rfc8785",
                "mode": "startup",
                "limitations": [
                    "Does not prove factual truth.",
                ],
            },
        )

    async with _make_client(handler) as client:
        result = await client.verify_context_receipt(
            receipt_id=UUID("00000000-0000-4000-8000-000000000001")
        )

    assert isinstance(result, ContextReceiptVerifyResponse)
    assert result.status == "invalid"
    assert result.failure_code == "MANIFEST_HASH_MISMATCH"
    assert result.recomputed_manifest_hash != result.stored_manifest_hash
