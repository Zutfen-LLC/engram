"""Unit coverage for the semantic Context Receipt dark writer."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from engram.config import settings
from engram.context_receipts import ContextReceiptStoreResult
from engram.memory_context import MEMORY_CONTEXT_VERSION, ResolvedMemoryContext
from engram.recall import SemanticPacketEvaluation
from engram.recall_profiles import LEGACY_PROFILE
from engram.semantic_context_receipt_dark_write import (
    write_semantic_context_receipt_best_effort,
)

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
PRINCIPAL = uuid.UUID("00000000-0000-0000-0000-000000000002")
RECALL_LOG = uuid.UUID("00000000-0000-0000-0000-000000000099")


def context() -> ResolvedMemoryContext:
    return ResolvedMemoryContext(
        version=MEMORY_CONTEXT_VERSION,
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        api_key_id=None,
        memory_profile_id=None,
        memory_profile_revision_id=None,
        memory_profile_slug=None,
        memory_profile_version=None,
        include_private=True,
        include_tenant=True,
        include_public=True,
        readable_workspace_ids=None,
        allow_tenant_write=True,
        allow_public_write=True,
        default_write_visibility="private",
        default_write_workspace_id=None,
        writable_workspace_ids=None,
    )


def item() -> dict[str, Any]:
    return {
        "id": "00000000-0000-0000-0000-000000000010",
        "kind": "fact",
        "content": "abcd",
        "review_status": "active",
        "authority": 10,
        "visibility": "private",
        "workspace_id": None,
        "pinned": False,
        "score": 0.8,
        "trust_score": 0.9,
        "relevance_score": None,
        "utility_score": None,
        "reasons": [],
        "warnings": [],
        "warning_codes": None,
        "conflict_type": None,
        "conflict_resolution_status": None,
        "admission": None,
        "evidence": None,
        "relationship": None,
        "packing_reason": None,
    }


def raw() -> dict[str, Any]:
    evaluation = SemanticPacketEvaluation(
        profile=LEGACY_PROFILE,
        items=[item()],
        working_set="[fact] abcd",
        candidate_count=1,
        omitted_by_admission={},
        item_count=1,
        byte_count=4,
        byte_budget=4,
        token_budget=1,
        item_budget=1,
    )
    return {
        "_semantic_evaluation": evaluation,
        "recall_log_id": str(RECALL_LOG),
        "workspace_id": None,
        "effective_byte_budget": 4,
        "effective_token_budget": 1,
        "effective_item_budget": 1,
        "scoring_version": "semantic-v3",
        "signals_version": None,
        "config_version": "v1",
        "item_count": 1,
        "byte_count": 4,
        "omitted_count": 0,
        "message": None,
    }


@dataclass
class FakeReceipt:
    id: uuid.UUID
    recall_log_id: uuid.UUID = RECALL_LOG
    tenant_id: uuid.UUID = TENANT
    principal_id: uuid.UUID = PRINCIPAL


class FakeSession:
    def __init__(self, failure: str | None = None) -> None:
        self.failure = failure

    async def __aenter__(self) -> FakeSession:
        if self.failure == "open_session":
            raise RuntimeError("secret open failure")
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def flush(self) -> None:
        if self.failure == "flush":
            raise RuntimeError("secret flush failure")

    async def refresh(self, value: Any) -> None:
        if self.failure == "reload":
            raise RuntimeError("secret reload failure")

    async def commit(self) -> None:
        if self.failure == "commit":
            raise RuntimeError("secret commit failure")


async def run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    failure: str | None = None,
    timeout: float = 1.0,
    created: bool = True,
) -> Any:
    monkeypatch.setattr(settings, "semantic_context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "usage_telemetry_enabled", False)
    monkeypatch.setattr(settings, "context_receipt_dark_write_timeout_seconds", timeout)
    monkeypatch.setattr(
        "engram.semantic_context_receipt_dark_write.async_session_factory",
        lambda: FakeSession(failure),
    )

    async def rls(*args: Any, **kwargs: Any) -> None:
        if failure == "apply_rls":
            raise RuntimeError("secret RLS failure")

    async def store(*args: Any, **kwargs: Any) -> ContextReceiptStoreResult:
        if failure == "store":
            raise RuntimeError("secret store failure")
        if failure == "timeout":
            await asyncio.sleep(10)
        return ContextReceiptStoreResult(
            receipt=FakeReceipt(uuid.uuid4()),
            created=created,  # type: ignore[arg-type]
        )

    def verify(*args: Any, **kwargs: Any) -> None:
        if failure == "verify":
            raise RuntimeError("secret verify failure")

    monkeypatch.setattr("engram.semantic_context_receipt_dark_write.apply_rls_context", rls)
    monkeypatch.setattr("engram.semantic_context_receipt_dark_write.store_context_receipt", store)
    monkeypatch.setattr("engram.semantic_context_receipt_dark_write._verify_reloaded", verify)
    return await write_semantic_context_receipt_best_effort(
        raw_result=raw(),
        memory_context=context(),
        query="secret query",
        workspace_supplied=False,
        requested_byte_budget=4,
        requested_token_budget=1,
        requested_item_budget=1,
    )


async def test_success_reports_created_and_safe_observability(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="engram.semantic_context_receipt_dark_write"):
        result = await run(monkeypatch)
    assert result.status == "created"
    assert result.verification_status == "passed"
    assert result.telemetry_status == "disabled"
    assert "mode=semantic" in caplog.text
    assert "item_count=1 byte_count=4" in caplog.text
    assert "secret query" not in caplog.text
    assert "abcd" not in caplog.text


async def test_identical_storage_retry_reports_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await run(monkeypatch, created=False)
    assert result.status == "idempotent"
    assert result.receipt_id is not None
    assert result.verification_status == "passed"


@pytest.mark.parametrize(
    ("failure", "stage"),
    [
        ("open_session", "open_session"),
        ("apply_rls", "apply_rls"),
        ("store", "store"),
        ("flush", "store"),
        ("reload", "reload"),
        ("verify", "verify"),
        ("commit", "commit"),
    ],
)
async def test_each_database_failure_is_bounded_and_fail_open(
    monkeypatch: pytest.MonkeyPatch, failure: str, stage: str
) -> None:
    result = await run(monkeypatch, failure=failure)
    assert result.status == "failed"
    assert result.failure_stage == stage
    assert result.receipt_id is None


async def test_total_timeout_is_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    result = await run(monkeypatch, failure="timeout", timeout=0.01)
    assert result.status == "timed_out"
    assert result.failure_stage == "timeout"
    assert result.telemetry_status == "skipped_deadline"


async def test_manifest_failure_is_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "semantic_context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "usage_telemetry_enabled", False)

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("secret manifest failure")

    monkeypatch.setattr(
        "engram.semantic_context_receipt_dark_write.build_semantic_context_manifest_v1",
        fail,
    )
    result = await write_semantic_context_receipt_best_effort(
        raw_result=raw(),
        memory_context=context(),
        query="secret query",
        workspace_supplied=False,
        requested_byte_budget=None,
        requested_token_budget=None,
        requested_item_budget=None,
    )
    assert result.status == "failed"
    assert result.failure_stage == "build_manifest"


async def test_cancellation_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "semantic_context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "usage_telemetry_enabled", False)
    monkeypatch.setattr(
        "engram.semantic_context_receipt_dark_write.async_session_factory",
        lambda: FakeSession(),
    )

    async def cancel(*args: Any, **kwargs: Any) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr("engram.semantic_context_receipt_dark_write.apply_rls_context", cancel)
    with pytest.raises(asyncio.CancelledError):
        await write_semantic_context_receipt_best_effort(
            raw_result=raw(),
            memory_context=context(),
            query="q",
            workspace_supplied=False,
            requested_byte_budget=None,
            requested_token_budget=None,
            requested_item_budget=None,
        )


async def test_disabled_does_no_manifest_or_session_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "semantic_context_receipt_dark_write_enabled", False)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("disabled semantic writer did work")

    monkeypatch.setattr(
        "engram.semantic_context_receipt_dark_write.async_session_factory", forbidden
    )
    monkeypatch.setattr(
        "engram.semantic_context_receipt_dark_write.build_semantic_context_manifest_v1",
        forbidden,
    )
    result = await write_semantic_context_receipt_best_effort(
        raw_result={},
        memory_context=context(),
        query="q",
        workspace_supplied=False,
        requested_byte_budget=None,
        requested_token_budget=None,
        requested_item_budget=None,
    )
    assert result.status == "disabled"
