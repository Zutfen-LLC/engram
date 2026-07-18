"""Pure unit tests for the startup context-receipt dark-write orchestrator
(ENG-CONTEXT-002B).

These tests are DB-free. They exercise:

- the disabled route guard — no receipt-specific work runs at all;
- the executed-result parser — every required key, every malformed value;
- public ``RecallResponse`` defaults never feed the receipt manifest;
- the unified best-effort entrypoint — one result, one log, one telemetry
  attempt per enabled attempt, including ``build_decision_context`` failures;
- the monotonic deadline — primary + telemetry never exceed the configured
  timeout; a hard timeout records ``telemetry_status=skipped_deadline``;
- ``asyncio`` cancellation is NOT swallowed;
- safe logging excludes exception messages and content.

Real-PostgreSQL proofs live in ``test_context_receipt_dark_write_postgres``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from engram.config import settings
from engram.context_manifest import (
    ContextManifestV1,
    compute_manifest_hash,
)
from engram.context_receipt_dark_write import (
    ContextReceiptDarkWriteResult,
    StartupDecisionContextError,
    StartupReceiptDecisionContext,
    StartupReceiptExecutedContext,
    parse_startup_executed_context,
    write_startup_context_receipt_best_effort,
)
from engram.context_receipts import (
    ContextReceiptConflictError,
    ContextReceiptIntegrityError,
)
from engram.memory_context import MEMORY_CONTEXT_VERSION, ResolvedMemoryContext

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
PRINCIPAL = uuid.UUID("00000000-0000-0000-0000-000000000002")
WORKSPACE = uuid.UUID("00000000-0000-0000-0000-000000000003")
PROFILE = uuid.UUID("00000000-0000-0000-0000-000000000004")
PROFILE_REV = uuid.UUID("00000000-0000-0000-0000-000000000005")
ITEM_A = "00000000-0000-0000-0000-000000000010"
ITEM_B = "00000000-0000-0000-0000-000000000011"

RECALL_LOG_ID = "00000000-0000-0000-0000-000000000099"
# A UUID with hex letters so uppercase/noncanonical transforms are observable.
HEX_RECALL_LOG_ID = "11111111-2222-aaaa-bbbb-ccccddddffff"
HEX_WORKSPACE = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeffff1111")


def _unrestricted_context(
    *,
    tenant_id: uuid.UUID = TENANT,
    principal_id: uuid.UUID = PRINCIPAL,
    profile_id: uuid.UUID | None = None,
    profile_revision_id: uuid.UUID | None = None,
    profile_version: int | None = None,
) -> ResolvedMemoryContext:
    return ResolvedMemoryContext(
        version=MEMORY_CONTEXT_VERSION,
        tenant_id=tenant_id,
        principal_id=principal_id,
        api_key_id=None,
        memory_profile_id=profile_id,
        memory_profile_revision_id=profile_revision_id,
        memory_profile_slug=None if profile_id is None else "p",
        memory_profile_version=profile_version,
        include_private=True,
        include_tenant=True,
        include_public=True,
        readable_workspace_ids=None,
        allow_tenant_write=True,
        allow_public_write=True,
        default_write_visibility="private",
        default_write_workspace_id=None,
        writable_workspace_ids=None,
        admin_workspace_bypass=False,
    )


def _item(
    *,
    id_: str = ITEM_A,
    content: str = "hello world",
    kind: str = "fact",
    review_status: str = "active",
    score: float | None = 0.8123,
    reasons: list[str] | None = None,
    warnings: list[str] | None = None,
    pinned: bool = False,
    importance: float = 0.9,
    source_trust: float = 0.8,
    memory_confidence: float = 0.75,
    human_verified: bool = True,
    authority: int = 10,
    visibility: str = "private",
    workspace_id: str | None = None,
    conflict_type: str | None = None,
    conflict_resolution_status: str | None = None,
) -> dict[str, Any]:
    return {
        "id": id_,
        "kind": kind,
        "content": content,
        "review_status": review_status,
        "score": score,
        "reasons": list(reasons) if reasons is not None else ["importance=0.90"],
        "warnings": list(warnings) if warnings is not None else [],
        "pinned": pinned,
        "importance": importance,
        "source_trust": source_trust,
        "memory_confidence": memory_confidence,
        "human_verified": human_verified,
        "authority": authority,
        "visibility": visibility,
        "workspace_id": workspace_id,
        "conflict_type": conflict_type,
        "conflict_resolution_status": conflict_resolution_status,
    }


class _Response:
    """Minimal finalized-response stand-in (satisfies RecallResponseLike)."""

    def __init__(
        self,
        *,
        items: list[dict[str, Any]] | None = None,
        pinned_omitted_count: int = 0,
        omitted_count: int = 0,
        message: str | None = None,
    ) -> None:
        self.items = items if items is not None else [_item(), _item(id_=ITEM_B, content="second")]
        self.working_set = "\n".join(f"[{i['kind']}] {i['content']}" for i in self.items)
        self.item_count = len(self.items)
        self.byte_count = sum(len(i["content"].encode("utf-8")) for i in self.items)
        self.pinned_omitted_count = pinned_omitted_count
        self.omitted_count = omitted_count
        self.message = message


def _valid_raw_result(
    *,
    workspace_id: str | None = None,
    recall_log_id: str = RECALL_LOG_ID,
    effective_byte_budget: int | None = 4096,
    effective_token_budget: int | None = None,
    scoring_version: str = "v1",
    config_version: str = "v1",
    candidate_strategy_version: str = "startup-candidates-v1",
) -> dict[str, Any]:
    """A raw startup engine result that satisfies every required provenance key.

    Mirrors the exact shape ``engram.recall.execute_startup_recall`` returns.
    """
    return {
        "working_set": "",
        "item_count": 0,
        "byte_count": 0,
        "pinned_omitted_count": 0,
        "omitted_count": 0,
        "items": [],
        "scoring_version": scoring_version,
        "config_version": config_version,
        "recall_log_id": recall_log_id,
        "candidate_strategy_version": candidate_strategy_version,
        "workspace_id": workspace_id,
        "effective_byte_budget": effective_byte_budget,
        "effective_token_budget": effective_token_budget,
        "effective_item_budget": None,
    }


class _FakeSession:
    """A minimal async-context-manager session stand-in so the orchestrator's
    dedicated-session path can be exercised without a real PostgreSQL.

    ``apply_rls_context`` is patched out by the tests that use this, so this
    class only needs ``__aenter__``/``__aexit__`` plus the no-op transaction
    helpers the orchestrator calls (``flush``/``refresh``/``commit``).
    """

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def refresh(self, _obj: Any) -> None:
        return None

    async def commit(self) -> None:
        return None


def _patch_session_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the orchestrator's ``async_session_factory`` with a fake
    context-manager factory and stub out ``apply_rls_context`` so the
    dedicated-session path runs without a real DB."""

    def _factory() -> _FakeSession:
        return _FakeSession()

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.async_session_factory", _factory
    )

    async def _noop_rls(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.apply_rls_context", _noop_rls
    )


async def _best_effort(
    monkeypatch: pytest.MonkeyPatch,
    *,
    raw_result: dict[str, Any] | None = None,
    response: _Response | None = None,
    memory_context: ResolvedMemoryContext | None = None,
    requested_workspace_supplied: bool = False,
    requested_byte_budget: int | None = None,
    requested_token_budget: int | None = None,
    requested_item_budget: int | None = None,
    enabled: bool = True,
    timeout_seconds: float = 5.0,
) -> ContextReceiptDarkWriteResult:
    """Run the best-effort wrapper with defaults that would otherwise succeed.

    Per-call patches (store/manifest/telemetry) are the caller's
    responsibility.
    """
    monkeypatch.setattr(
        settings, "context_receipt_dark_write_enabled", enabled
    )
    monkeypatch.setattr(
        settings, "context_receipt_dark_write_timeout_seconds", timeout_seconds
    )
    return await write_startup_context_receipt_best_effort(
        response=response or _Response(),
        raw_result=raw_result or _valid_raw_result(),
        memory_context=memory_context or _unrestricted_context(),
        requested_workspace_supplied=requested_workspace_supplied,
        requested_byte_budget=requested_byte_budget,
        requested_token_budget=requested_token_budget,
        requested_item_budget=requested_item_budget,
    )


# ─── Disabled path ──────────────────────────────────────────────────────


async def test_disabled_returns_disabled_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", False)

    def _boom_session(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not open a DB session when disabled")

    def _boom_telemetry(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not record telemetry when disabled")

    def _boom_parser(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not parse executed result when disabled")

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.async_session_factory", _boom_session
    )
    monkeypatch.setattr(
        "engram.usage.record_context_receipt_dark_write", _boom_telemetry
    )
    monkeypatch.setattr(
        "engram.context_receipt_dark_write.parse_startup_executed_context",
        _boom_parser,
    )

    result = await write_startup_context_receipt_best_effort(
        response=_Response(),
        raw_result=_valid_raw_result(),
        memory_context=_unrestricted_context(),
        requested_workspace_supplied=False,
        requested_byte_budget=None,
        requested_token_budget=None,
        requested_item_budget=None,
    )
    assert result.status == "disabled"
    assert result.latency_ms == 0
    assert result.receipt_id is None
    assert result.failure_stage is None
    assert result.exception_type is None
    assert result.telemetry_status is None


async def test_disabled_does_not_construct_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", False)

    def _boom_build(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not build a manifest when disabled")

    monkeypatch.setattr(
        "engram.context_receipt_dark_write._build_manifest", _boom_build
    )

    result = await write_startup_context_receipt_best_effort(
        response=_Response(),
        raw_result=_valid_raw_result(),
        memory_context=_unrestricted_context(),
        requested_workspace_supplied=False,
        requested_byte_budget=None,
        requested_token_budget=None,
        requested_item_budget=None,
    )
    assert result.status == "disabled"


# ─── Executed-result parser ─────────────────────────────────────────────


def _full_raw() -> dict[str, Any]:
    return _valid_raw_result(
        workspace_id=str(WORKSPACE),
        effective_byte_budget=4096,
        effective_token_budget=1000,
    )


def test_parse_valid_result_builds_executed_context() -> None:
    executed = parse_startup_executed_context(_full_raw())
    assert isinstance(executed, StartupReceiptExecutedContext)
    assert executed.recall_log_id == uuid.UUID(RECALL_LOG_ID)
    assert executed.workspace_id == WORKSPACE
    assert executed.scoring_version == "v1"
    assert executed.config_version == "v1"
    assert executed.candidate_strategy_version == "startup-candidates-v1"
    assert executed.effective_byte_budget == 4096
    assert executed.effective_token_budget == 1000
    assert executed.effective_item_budget is None


@pytest.mark.parametrize(
    "missing_key",
    [
        "recall_log_id",
        "workspace_id",
        "scoring_version",
        "config_version",
        "candidate_strategy_version",
        "effective_byte_budget",
        "effective_token_budget",
        "effective_item_budget",
    ],
)
def test_parse_missing_required_key_raises(missing_key: str) -> None:
    raw = _full_raw()
    del raw[missing_key]
    with pytest.raises(StartupDecisionContextError):
        parse_startup_executed_context(raw)


def test_parse_null_recall_log_id_raises() -> None:
    raw = _full_raw()
    raw["recall_log_id"] = None
    with pytest.raises(StartupDecisionContextError):
        parse_startup_executed_context(raw)


def test_parse_malformed_recall_log_id_raises() -> None:
    raw = _full_raw()
    raw["recall_log_id"] = "not-a-uuid"
    with pytest.raises(StartupDecisionContextError):
        parse_startup_executed_context(raw)


def test_parse_noncanonical_uppercase_recall_log_id_raises() -> None:
    raw = _full_raw()
    raw["recall_log_id"] = HEX_RECALL_LOG_ID.upper()
    with pytest.raises(StartupDecisionContextError):
        parse_startup_executed_context(raw)


def test_parse_noncanonical_no_hyphen_recall_log_id_raises() -> None:
    raw = _full_raw()
    raw["recall_log_id"] = HEX_RECALL_LOG_ID.replace("-", "")
    with pytest.raises(StartupDecisionContextError):
        parse_startup_executed_context(raw)


def test_parse_whitespace_padded_recall_log_id_raises() -> None:
    raw = _full_raw()
    raw["recall_log_id"] = f"  {RECALL_LOG_ID}  "
    with pytest.raises(StartupDecisionContextError):
        parse_startup_executed_context(raw)


def test_parse_explicit_null_workspace_is_valid() -> None:
    raw = _full_raw()
    raw["workspace_id"] = None
    executed = parse_startup_executed_context(raw)
    assert executed.workspace_id is None


def test_parse_empty_string_workspace_raises_not_null() -> None:
    raw = _full_raw()
    raw["workspace_id"] = ""
    with pytest.raises(StartupDecisionContextError):
        parse_startup_executed_context(raw)


def test_parse_malformed_workspace_uuid_raises() -> None:
    raw = _full_raw()
    raw["workspace_id"] = "not-a-uuid"
    with pytest.raises(StartupDecisionContextError):
        parse_startup_executed_context(raw)


def test_parse_noncanonical_workspace_uuid_raises() -> None:
    raw = _full_raw()
    raw["workspace_id"] = str(HEX_WORKSPACE).upper()
    with pytest.raises(StartupDecisionContextError):
        parse_startup_executed_context(raw)


def test_parse_blank_scoring_version_raises() -> None:
    raw = _full_raw()
    raw["scoring_version"] = "   "
    with pytest.raises(StartupDecisionContextError):
        parse_startup_executed_context(raw)


def test_parse_boolean_scoring_version_raises() -> None:
    raw = _full_raw()
    raw["scoring_version"] = True  # type: ignore[dict-item]
    with pytest.raises(StartupDecisionContextError):
        parse_startup_executed_context(raw)


def test_parse_non_string_scoring_version_raises() -> None:
    raw = _full_raw()
    raw["scoring_version"] = 1  # type: ignore[dict-item]
    with pytest.raises(StartupDecisionContextError):
        parse_startup_executed_context(raw)


def test_parse_blank_config_version_raises() -> None:
    raw = _full_raw()
    raw["config_version"] = ""
    with pytest.raises(StartupDecisionContextError):
        parse_startup_executed_context(raw)


def test_parse_boolean_config_version_raises() -> None:
    raw = _full_raw()
    raw["config_version"] = False  # type: ignore[dict-item]
    with pytest.raises(StartupDecisionContextError):
        parse_startup_executed_context(raw)


def test_parse_blank_candidate_strategy_raises() -> None:
    raw = _full_raw()
    raw["candidate_strategy_version"] = "\t"
    with pytest.raises(StartupDecisionContextError):
        parse_startup_executed_context(raw)


def test_parse_boolean_candidate_strategy_raises() -> None:
    raw = _full_raw()
    raw["candidate_strategy_version"] = True  # type: ignore[dict-item]
    with pytest.raises(StartupDecisionContextError):
        parse_startup_executed_context(raw)


def test_parse_boolean_byte_budget_raises() -> None:
    raw = _full_raw()
    raw["effective_byte_budget"] = True  # type: ignore[dict-item]
    with pytest.raises(StartupDecisionContextError):
        parse_startup_executed_context(raw)


def test_parse_negative_byte_budget_raises() -> None:
    raw = _full_raw()
    raw["effective_byte_budget"] = -1
    with pytest.raises(StartupDecisionContextError):
        parse_startup_executed_context(raw)


def test_parse_boolean_token_budget_raises() -> None:
    raw = _full_raw()
    raw["effective_token_budget"] = False  # type: ignore[dict-item]
    with pytest.raises(StartupDecisionContextError):
        parse_startup_executed_context(raw)


def test_parse_negative_token_budget_raises() -> None:
    raw = _full_raw()
    raw["effective_token_budget"] = -7
    with pytest.raises(StartupDecisionContextError):
        parse_startup_executed_context(raw)


def test_parse_non_null_effective_item_budget_raises() -> None:
    raw = _full_raw()
    raw["effective_item_budget"] = 3
    with pytest.raises(StartupDecisionContextError):
        parse_startup_executed_context(raw)


def test_parse_returns_frozen_dataclass() -> None:
    import dataclasses

    executed = parse_startup_executed_context(_full_raw())
    with pytest.raises(dataclasses.FrozenInstanceError):
        executed.scoring_version = "v2"  # type: ignore[misc]


# ─── Public defaults must NOT enter evidence ────────────────────────────


async def test_public_response_defaults_do_not_enter_receipt_when_key_missing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Public ``RecallResponse`` defaults remain available but must never
    feed the receipt manifest. When a raw result provenance key is removed,
    the receipt must fail open rather than substitute the response default.
    """
    _patch_session_factory(monkeypatch)

    # Build a response that carries the public compatibility defaults
    # (scoring_version='v1', config_version='v1') — these are the values the
    # route would have fed into the decision context under the old code.
    class _DefaultedResponse(_Response):
        scoring_version = "v1"
        config_version = "v1"

    response = _DefaultedResponse()

    # Remove a required provenance key from the raw result. The response
    # default must NOT mask the absence.
    raw = _valid_raw_result()
    del raw["scoring_version"]

    with caplog.at_level(logging.INFO, logger="engram.context_receipt_dark_write"):
        result = await _best_effort(
            monkeypatch,
            raw_result=raw,
            response=response,
        )

    assert result.status == "failed"
    assert result.failure_stage == "build_decision_context"
    assert result.exception_type == "StartupDecisionContextError"
    assert result.receipt_id is None
    # Public response compatibility defaults remain intact.
    assert response.scoring_version == "v1"
    assert response.config_version == "v1"


# ─── Timeout / failure / cancellation ────────────────────────────────────


async def test_decision_context_failure_within_deadline_returns_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``build_decision_context`` failure is an enabled attempt: it
    produces one ``failed`` result and one bounded usage-event attempt when
    the deadline still has room."""
    monkeypatch.setattr(settings, "usage_telemetry_enabled", True)
    _patch_session_factory(monkeypatch)

    captured: dict[str, Any] = {}

    async def _capture_usage(**kwargs: Any) -> Any:
        captured.update(kwargs)
        # Return a UUID to simulate a successful insertion.
        return uuid.uuid4()

    monkeypatch.setattr(
        "engram.usage.record_context_receipt_dark_write", _capture_usage
    )

    raw = _valid_raw_result()
    del raw["candidate_strategy_version"]

    result = await _best_effort(monkeypatch, raw_result=raw)
    assert result.status == "failed"
    assert result.failure_stage == "build_decision_context"
    assert result.exception_type == "StartupDecisionContextError"
    assert result.verification_status == "failed"
    # Telemetry was attempted (deadline had room) and the helper returned a
    # UUID — the status honestly reports ``recorded``.
    assert result.telemetry_status == "recorded"
    assert captured["status"] == "failed"
    assert captured["failure_stage"] == "build_decision_context"
    assert captured["exception_type"] == "StartupDecisionContextError"


async def test_db_operation_timeout_returns_timed_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured timeout that fires before the DB attempt completes
    returns ``timed_out`` and never raises into the route."""
    _patch_session_factory(monkeypatch)

    async def _slow_store(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(10)
        return None

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.store_context_receipt", _slow_store
    )

    result = await _best_effort(monkeypatch, timeout_seconds=0.01)
    assert result.status == "timed_out"
    assert result.failure_stage == "timeout"
    assert result.receipt_id is None


async def test_ordinary_failure_returns_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session_factory(monkeypatch)

    class _StoreError(Exception):
        pass

    async def _raise_store(*args: Any, **kwargs: Any) -> Any:
        raise _StoreError("store blew up with SECRET-SENTINEL-VALUE")

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.store_context_receipt", _raise_store
    )

    result = await _best_effort(monkeypatch)
    assert result.status == "failed"
    assert result.failure_stage == "store"
    assert result.exception_type == "_StoreError"
    assert result.receipt_id is None


async def test_builder_value_error_returns_failed_build_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_session_factory(monkeypatch)

    def _raise_build(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("bad manifest input SECRET")

    monkeypatch.setattr(
        "engram.context_receipt_dark_write._build_manifest", _raise_build
    )
    result = await _best_effort(monkeypatch)
    assert result.status == "failed"
    assert result.failure_stage == "build_manifest"


async def test_asyncio_cancellation_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """asyncio.CancelledError must propagate out of the best-effort wrapper,
    never be translated into a ``failed`` result."""
    _patch_session_factory(monkeypatch)

    async def _raise_cancel(*args: Any, **kwargs: Any) -> Any:
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.store_context_receipt", _raise_cancel
    )

    with pytest.raises(asyncio.CancelledError):
        await _best_effort(monkeypatch)


async def test_context_receipt_conflict_error_is_fail_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_session_factory(monkeypatch)

    async def _raise_conflict(*args: Any, **kwargs: Any) -> Any:
        raise ContextReceiptConflictError("conflict SECRET")

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.store_context_receipt", _raise_conflict
    )
    result = await _best_effort(monkeypatch)
    assert result.status == "failed"
    assert result.failure_stage == "store"
    assert result.exception_type == "ContextReceiptConflictError"


async def test_context_receipt_integrity_error_is_fail_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_session_factory(monkeypatch)

    from engram.context_receipts import ContextReceiptStoreResult

    class _FakeReceipt:
        def __init__(self) -> None:
            self.id = uuid.uuid4()
            self.recall_log_id = uuid.uuid4()
            self.tenant_id = TENANT
            self.principal_id = PRINCIPAL
            self.manifest_hash = "sha256:" + "0" * 64
            self.packet_hash = "sha256:" + "0" * 64

    async def _fake_store(*args: Any, **kwargs: Any) -> Any:
        return ContextReceiptStoreResult(receipt=_FakeReceipt(), created=True)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.store_context_receipt", _fake_store
    )

    async def _raise_integrity(*args: Any, **kwargs: Any) -> Any:
        raise ContextReceiptIntegrityError("integrity SECRET")

    # Patch verify to raise during the verification step (after store+flush+reload).
    monkeypatch.setattr(
        "engram.context_receipt_dark_write.verify_context_receipt_record",
        _raise_integrity,
    )
    result = await _best_effort(monkeypatch)
    assert result.status == "failed"
    assert result.failure_stage == "verify"
    assert result.exception_type == "ContextReceiptIntegrityError"


async def test_unexpected_helper_exception_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the helper itself violates its no-raise contract with a bare
    Exception, the best-effort boundary catches it."""
    _patch_session_factory(monkeypatch)

    class _SessionCM:
        def __init__(self) -> None:
            raise RuntimeError("unexpected SECRET during session creation")

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    def _factory() -> Any:
        return _SessionCM()

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.async_session_factory", _factory
    )
    result = await _best_effort(monkeypatch)
    assert result.status == "failed"
    assert result.failure_stage == "open_session"
    assert result.exception_type == "RuntimeError"


# ─── Deadline / telemetry boundary ──────────────────────────────────────


async def test_slow_telemetry_bounded_by_remaining_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled telemetry connection must not hold the request beyond the
    configured receipt timeout. The telemetry call is bounded with the
    REMAINING deadline; the receipt result is preserved when telemetry times
    out.
    """
    monkeypatch.setattr(settings, "usage_telemetry_enabled", True)
    _patch_session_factory(monkeypatch)

    # Make the main attempt fail fast so most of the timeout budget remains.
    async def _raise_store(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("fast store failure")

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.store_context_receipt", _raise_store
    )

    telemetry_called = {"n": 0}

    async def _slow_telemetry(**kwargs: Any) -> Any:
        telemetry_called["n"] += 1
        await asyncio.sleep(10)  # would hold far beyond the timeout

    monkeypatch.setattr(
        "engram.usage.record_context_receipt_dark_write", _slow_telemetry
    )

    start = asyncio.get_event_loop().time()
    result = await _best_effort(monkeypatch, timeout_seconds=0.1)
    elapsed = asyncio.get_event_loop().time() - start
    # The receipt result is preserved (the store failure).
    assert result.status == "failed"
    assert result.failure_stage == "store"
    assert result.telemetry_status == "timed_out"
    # Telemetry was called but bounded — total elapsed must be within the
    # configured timeout plus a small grace.
    assert telemetry_called["n"] == 1
    assert elapsed < 2.0, f"telemetry held the request for {elapsed:.2f}s"


async def test_primary_consumes_whole_deadline_skips_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the primary operation consumes the entire deadline, the wrapper
    emits the standardized structured log, records
    ``telemetry_status=skipped_deadline``, and returns promptly without
    extending the total request time to write telemetry.
    """
    monkeypatch.setattr(settings, "usage_telemetry_enabled", True)
    _patch_session_factory(monkeypatch)

    async def _slow_store(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.2)  # consumes the whole 0.1s deadline
        return None

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.store_context_receipt", _slow_store
    )

    telemetry_called = {"n": 0}

    async def _should_not_be_called(**kwargs: Any) -> Any:
        telemetry_called["n"] += 1
        return None

    monkeypatch.setattr(
        "engram.usage.record_context_receipt_dark_write", _should_not_be_called
    )

    result = await _best_effort(monkeypatch, timeout_seconds=0.1)
    assert result.status == "timed_out"
    assert result.failure_stage == "timeout"
    assert result.telemetry_status == "skipped_deadline"
    assert telemetry_called["n"] == 0


async def test_total_elapsed_is_bounded_with_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """The total elapsed time (primary + telemetry) must never exceed the
    configured timeout by more than a small nonflaky grace. Shared CI may
    schedule slowly, so the grace is generous but still bounded."""
    monkeypatch.setattr(settings, "usage_telemetry_enabled", True)
    _patch_session_factory(monkeypatch)

    async def _slow_store(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.05)

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.store_context_receipt", _slow_store
    )

    async def _slow_telemetry(**kwargs: Any) -> Any:
        await asyncio.sleep(0.05)

    monkeypatch.setattr(
        "engram.usage.record_context_receipt_dark_write", _slow_telemetry
    )

    start = asyncio.get_event_loop().time()
    await _best_effort(monkeypatch, timeout_seconds=0.1)
    elapsed = asyncio.get_event_loop().time() - start
    # 0.1s configured timeout + a 2s grace for shared-CI scheduling jitter.
    assert elapsed < 2.0, f"total elapsed {elapsed:.2f}s exceeded the grace"


async def test_no_untracked_telemetry_task_remains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telemetry must be awaited inline (no fire-and-forget background task),
    so no untracked task remains after the wrapper returns."""
    monkeypatch.setattr(settings, "usage_telemetry_enabled", True)
    _patch_session_factory(monkeypatch)

    async def _raise_store(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("fail to exercise telemetry path")

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.store_context_receipt", _raise_store
    )

    async def _fast_telemetry(**kwargs: Any) -> Any:
        return None

    monkeypatch.setattr(
        "engram.usage.record_context_receipt_dark_write", _fast_telemetry
    )

    before = len(asyncio.all_tasks())
    await _best_effort(monkeypatch)
    after = len(asyncio.all_tasks())
    # Only the current task may differ; no extra telemetry task should linger.
    assert after <= before + 1


async def test_cancellation_propagates_through_telemetry_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation must propagate through the telemetry wait, not be
    translated into a result."""
    monkeypatch.setattr(settings, "usage_telemetry_enabled", True)
    _patch_session_factory(monkeypatch)

    async def _raise_store(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("fail primary so telemetry runs")

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.store_context_receipt", _raise_store
    )

    async def _slow_telemetry(**kwargs: Any) -> Any:
        await asyncio.sleep(10)

    monkeypatch.setattr(
        "engram.usage.record_context_receipt_dark_write", _slow_telemetry
    )

    async def _runner() -> None:
        await _best_effort(monkeypatch, timeout_seconds=5.0)

    task = asyncio.ensure_future(_runner())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ─── Safe logging excludes exception messages and content ──────────────


async def test_safe_logging_excludes_exception_messages_and_content(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(settings, "usage_telemetry_enabled", False)
    _patch_session_factory(monkeypatch)

    class _StoreError(Exception):
        pass

    secret = "SUPER-SECRET-SENTINEL-XYZ"

    async def _raise_store(*args: Any, **kwargs: Any) -> Any:
        raise _StoreError(f"store failed with {secret}")

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.store_context_receipt", _raise_store
    )

    with caplog.at_level(logging.INFO, logger="engram.context_receipt_dark_write"):
        result = await _best_effort(monkeypatch)
    assert result.status == "failed"
    # The secret sentinel must not appear anywhere in the captured logs.
    for record in caplog.records:
        assert secret not in record.getMessage()
        assert secret not in str(record.args)
    # Raw content must never appear in logs.
    for record in caplog.records:
        assert "hello world" not in record.getMessage()


async def test_no_logger_exception_on_fail_open_path(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``logger.exception`` is prohibited on this fail-open path because
    exception representations may include bound values. The wrapper must use
    safe logs containing exception type only."""
    monkeypatch.setattr(settings, "usage_telemetry_enabled", False)
    _patch_session_factory(monkeypatch)

    async def _raise_store(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("store failed SECRET")

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.store_context_receipt", _raise_store
    )

    with caplog.at_level(logging.DEBUG, logger="engram.context_receipt_dark_write"):
        await _best_effort(monkeypatch)
    # No record may carry an exception traceback (exc_info).
    for record in caplog.records:
        assert record.exc_info is None


async def test_standardized_log_fields_present_on_every_enabled_attempt(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One standardized structured log per enabled attempt, with all required
    fields present."""
    monkeypatch.setattr(settings, "usage_telemetry_enabled", False)
    _patch_session_factory(monkeypatch)

    async def _raise_store(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("forced store failure")

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.store_context_receipt", _raise_store
    )

    with caplog.at_level(logging.INFO, logger="engram.context_receipt_dark_write"):
        await _best_effort(monkeypatch)

    dark_logs = [
        r
        for r in caplog.records
        if r.name == "engram.context_receipt_dark_write"
        and "context_receipt_dark_write" in r.getMessage()
    ]
    assert len(dark_logs) == 1
    msg = dark_logs[0].getMessage()
    for field in (
        "event=context_receipt_dark_write",
        "status=",
        "tenant_id=",
        "principal_id=",
        "mode=startup",
        "latency_ms=",
        "item_count=",
        "failure_stage=",
        "exception_type=",
        "verification_status=",
        "telemetry_status=",
    ):
        assert field in msg, f"standardized log missing {field!r}"


# ─── Usage telemetry receives only bounded aggregate metadata ───────────


async def test_usage_telemetry_receives_only_bounded_aggregate_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "usage_telemetry_enabled", True)
    _patch_session_factory(monkeypatch)

    captured: dict[str, Any] = {}

    async def _capture_usage(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return None

    monkeypatch.setattr(
        "engram.usage.record_context_receipt_dark_write", _capture_usage
    )

    # Force a failure so verification_status is "failed" and failure metadata
    # is captured.
    async def _raise_store(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("SECRET store failure with raw content 'hello world'")

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.store_context_receipt", _raise_store
    )

    result = await _best_effort(monkeypatch)
    assert result.status == "failed"
    # Telemetry must have been called with bounded aggregate metadata.
    assert captured["status"] == "failed"
    assert captured["mode"] == "startup"
    assert captured["failure_stage"] == "store"
    assert captured["exception_type"] == "RuntimeError"
    assert captured["verification_status"] == "failed"
    rendered = str(captured)
    assert "SECRET" not in rendered
    assert "hello world" not in rendered
    assert "content" not in rendered.lower()
    assert "working_set" not in rendered.lower()


async def test_usage_telemetry_failure_does_not_alter_dark_write_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A telemetry insert failure must not alter the dark-write result or
    the recall response."""
    monkeypatch.setattr(settings, "usage_telemetry_enabled", True)
    _patch_session_factory(monkeypatch)

    async def _raise_telemetry(**kwargs: Any) -> Any:
        raise RuntimeError("telemetry SECRET failure")

    monkeypatch.setattr(
        "engram.usage.record_context_receipt_dark_write", _raise_telemetry
    )

    # Force a dark-write failure (no real DB). Telemetry is still attempted
    # (and itself fails); the result must not be altered by the telemetry
    # failure.
    async def _fake_store(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no db SECRET")

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.store_context_receipt", _fake_store
    )

    result = await _best_effort(monkeypatch)
    # The telemetry failure must not have changed the dark-write result.
    assert result.status == "failed"
    assert result.failure_stage == "store"
    assert result.telemetry_status == "failed"


# ─── Telemetry status correctness ───────────────────────────────────────


async def test_telemetry_disabled_reports_disabled_no_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``usage_telemetry_enabled`` is false, ``telemetry_status`` is
    ``disabled`` and the telemetry helper is never called."""
    monkeypatch.setattr(settings, "usage_telemetry_enabled", False)
    _patch_session_factory(monkeypatch)

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("telemetry helper must not be called when disabled")

    monkeypatch.setattr(
        "engram.usage.record_context_receipt_dark_write", _boom
    )

    result = await _best_effort(monkeypatch)
    # The dark-write outcome (created/failed/etc.) is irrelevant to this
    # test — the point is that telemetry_status honestly reports ``disabled``
    # and the helper was never invoked.
    assert result.telemetry_status == "disabled"


async def test_telemetry_returns_none_reports_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the telemetry helper returns ``None`` (insertion failed),
    ``telemetry_status`` is ``failed`` — never ``recorded``."""
    monkeypatch.setattr(settings, "usage_telemetry_enabled", True)
    _patch_session_factory(monkeypatch)

    async def _returns_none(**kwargs: Any) -> Any:
        return None

    monkeypatch.setattr(
        "engram.usage.record_context_receipt_dark_write", _returns_none
    )

    result = await _best_effort(monkeypatch)
    assert result.telemetry_status == "failed"


async def test_telemetry_returns_uuid_reports_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the telemetry helper returns a UUID, ``telemetry_status`` is
    ``recorded``."""
    monkeypatch.setattr(settings, "usage_telemetry_enabled", True)
    _patch_session_factory(monkeypatch)

    async def _returns_uuid(**kwargs: Any) -> Any:
        return uuid.uuid4()

    monkeypatch.setattr(
        "engram.usage.record_context_receipt_dark_write", _returns_uuid
    )

    result = await _best_effort(monkeypatch)
    assert result.telemetry_status == "recorded"


async def test_telemetry_timeout_reports_timed_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the telemetry await exceeds the remaining deadline,
    ``telemetry_status`` is ``timed_out``."""
    monkeypatch.setattr(settings, "usage_telemetry_enabled", True)
    _patch_session_factory(monkeypatch)

    async def _slow_telemetry(**kwargs: Any) -> Any:
        await asyncio.sleep(10)

    monkeypatch.setattr(
        "engram.usage.record_context_receipt_dark_write", _slow_telemetry
    )

    result = await _best_effort(monkeypatch, timeout_seconds=0.1)
    assert result.telemetry_status == "timed_out"


async def test_telemetry_exhausted_deadline_reports_skipped_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the primary operation consumes the entire deadline,
    ``telemetry_status`` is ``skipped_deadline`` and the telemetry helper is
    never called."""
    monkeypatch.setattr(settings, "usage_telemetry_enabled", True)
    _patch_session_factory(monkeypatch)

    async def _slow_store(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.2)  # consumes the whole 0.1s deadline

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.store_context_receipt", _slow_store
    )

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("telemetry helper must not be called when deadline exhausted")

    monkeypatch.setattr(
        "engram.usage.record_context_receipt_dark_write", _boom
    )

    result = await _best_effort(monkeypatch, timeout_seconds=0.1)
    assert result.status == "timed_out"
    assert result.telemetry_status == "skipped_deadline"


# ─── Deadline coverage for every awaited DB stage ───────────────────────


class _SlowSession:
    """Fake session whose configurable methods sleep to prove the deadline
    bounds session entry, RLS, flush, and cleanup — not just store/refresh/
    commit."""

    def __init__(
        self,
        *,
        slow_enter: float = 0.0,
        slow_rls: float = 0.0,
        slow_flush: float = 0.0,
        slow_exit: float = 0.0,
    ) -> None:
        self._slow_enter = slow_enter
        self._slow_rls = slow_rls
        self._slow_flush = slow_flush
        self._slow_exit = slow_exit

    async def __aenter__(self) -> _SlowSession:
        if self._slow_enter:
            await asyncio.sleep(self._slow_enter)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._slow_exit:
            await asyncio.sleep(self._slow_exit)
        return None

    async def flush(self) -> None:
        if self._slow_flush:
            await asyncio.sleep(self._slow_flush)

    async def refresh(self, _obj: Any) -> None:
        pass

    async def commit(self) -> None:
        pass


def _patch_slow_session(
    monkeypatch: pytest.MonkeyPatch,
    *,
    slow_enter: float = 0.0,
    slow_rls: float = 0.0,
    slow_flush: float = 0.0,
    slow_exit: float = 0.0,
) -> None:
    session = _SlowSession(
        slow_enter=slow_enter,
        slow_rls=slow_rls,
        slow_flush=slow_flush,
        slow_exit=slow_exit,
    )

    def _factory() -> Any:
        return session

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.async_session_factory", _factory
    )

    rls_sleep = slow_rls

    async def _rls(*args: Any, **kwargs: Any) -> None:
        if rls_sleep:
            await asyncio.sleep(rls_sleep)

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.apply_rls_context", _rls
    )


async def test_slow_session_enter_bounded_by_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session ``__aenter__`` (connection acquisition) is bounded by the
    shared deadline."""
    _patch_slow_session(monkeypatch, slow_enter=10.0)

    start = asyncio.get_event_loop().time()
    result = await _best_effort(monkeypatch, timeout_seconds=0.05)
    elapsed = asyncio.get_event_loop().time() - start
    assert result.status == "timed_out"
    assert result.failure_stage == "timeout"
    assert elapsed < 2.0


async def test_slow_apply_rls_bounded_by_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``apply_rls_context`` is bounded by the shared deadline."""
    _patch_slow_session(monkeypatch, slow_rls=10.0)

    start = asyncio.get_event_loop().time()
    result = await _best_effort(monkeypatch, timeout_seconds=0.05)
    elapsed = asyncio.get_event_loop().time() - start
    assert result.status == "timed_out"
    assert result.failure_stage == "timeout"
    assert elapsed < 2.0


async def test_slow_flush_bounded_by_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``session.flush()`` is bounded by the shared deadline."""
    # A fake session whose flush hangs past the deadline.
    class _SlowFlushSession(_FakeSession):
        async def flush(self) -> None:
            await asyncio.sleep(10)

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.async_session_factory",
        lambda: _SlowFlushSession(),
    )

    async def _noop_rls(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.apply_rls_context", _noop_rls
    )

    async def _fast_store(*args: Any, **kwargs: Any) -> Any:
        from engram.context_receipts import ContextReceiptStoreResult

        class _R:
            id = uuid.uuid4()
            recall_log_id = uuid.UUID(RECALL_LOG_ID)
            tenant_id = TENANT
            principal_id = PRINCIPAL
            manifest_hash = "sha256:" + "0" * 64
            packet_hash = "sha256:" + "0" * 64

        return ContextReceiptStoreResult(receipt=_R(), created=True)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.store_context_receipt", _fast_store
    )

    start = asyncio.get_event_loop().time()
    result = await _best_effort(monkeypatch, timeout_seconds=0.05)
    elapsed = asyncio.get_event_loop().time() - start
    assert result.status == "timed_out"
    assert result.failure_stage == "timeout"
    assert elapsed < 2.0


async def test_slow_session_cleanup_bounded_by_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session ``__aexit__`` (rollback/close after a store failure) is
    bounded by the shared deadline — a stalled cleanup cannot hold the
    request beyond the configured timeout."""
    _patch_slow_session(monkeypatch, slow_exit=10.0)

    async def _raise_store(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("forced store failure so cleanup runs")

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.store_context_receipt", _raise_store
    )

    start = asyncio.get_event_loop().time()
    result = await _best_effort(monkeypatch, timeout_seconds=0.1)
    elapsed = asyncio.get_event_loop().time() - start
    # The store failure (not timeout) is the result — cleanup is best-effort.
    assert result.status == "failed"
    assert result.failure_stage == "store"
    # But the slow cleanup was bounded — total elapsed stays within grace.
    assert elapsed < 2.0, f"slow cleanup held request for {elapsed:.2f}s"


async def test_manifest_construction_consuming_deadline_still_bounds_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If synchronous manifest construction consumes the entire deadline,
    the code still attempts session entry but the deadline check fires
    immediately (no unbounded DB wait)."""
    import time as _time

    _patch_session_factory(monkeypatch)

    # Save the real builder before patching so the slow wrapper produces a
    # valid manifest (not a TypeError from wrong kwargs).
    from engram.context_receipt_dark_write import _build_manifest as _real_build

    def _slow_build(*args: Any, **kwargs: Any) -> Any:
        # Consume the whole deadline synchronously.
        _time.sleep(0.15)
        return _real_build(*args, **kwargs)

    monkeypatch.setattr(
        "engram.context_receipt_dark_write._build_manifest", _slow_build
    )

    # Make session entry hang if it somehow ran unbounded.
    def _factory() -> Any:
        class _Hang:
            async def __aenter__(self) -> Any:
                await asyncio.sleep(10)
                return self

            async def __aexit__(self, *exc: Any) -> None:
                pass

        return _Hang()

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.async_session_factory", _factory
    )

    start = asyncio.get_event_loop().time()
    result = await _best_effort(monkeypatch, timeout_seconds=0.1)
    elapsed = asyncio.get_event_loop().time() - start
    # The session entry is bounded — the deadline fires before the 10s hang.
    assert result.status == "timed_out"
    assert result.failure_stage == "timeout"
    assert elapsed < 2.0


def _decision_context(
    *,
    workspace_supplied: bool = False,
    requested_byte_budget: int | None = None,
    requested_token_budget: int | None = None,
    requested_item_budget: int | None = None,
    effective_workspace_id: uuid.UUID | None = None,
    effective_byte_budget: int | None = 4096,
    effective_token_budget: int | None = None,
    scoring_version: str = "v1",
    config_version: str = "v1",
    candidate_strategy_version: str = "startup-candidates-v1",
) -> StartupReceiptDecisionContext:
    return StartupReceiptDecisionContext(
        workspace_supplied=workspace_supplied,
        requested_byte_budget=requested_byte_budget,
        requested_token_budget=requested_token_budget,
        requested_item_budget=requested_item_budget,
        effective_workspace_id=effective_workspace_id,
        effective_byte_budget=effective_byte_budget,
        effective_token_budget=effective_token_budget,
        effective_item_budget=None,
        scoring_version=scoring_version,
        config_version=config_version,
        candidate_strategy_version=candidate_strategy_version,
    )


def _build_manifest(
    *,
    response: _Response | None = None,
    memory_context: ResolvedMemoryContext | None = None,
    decision_context: StartupReceiptDecisionContext | None = None,
) -> ContextManifestV1:
    from engram.context_receipt_dark_write import _build_manifest as _bm

    return _bm(
        response=response or _Response(),
        memory_context=memory_context or _unrestricted_context(),
        decision_context=decision_context or _decision_context(),
    )


def test_requested_descriptor_preserves_caller_null_values() -> None:
    manifest = _build_manifest(
        decision_context=_decision_context(
            workspace_supplied=False,
            requested_byte_budget=None,
            requested_token_budget=None,
            requested_item_budget=None,
        ),
    )
    requested = manifest.request.requested
    assert requested.workspace_supplied is False
    assert requested.byte_budget is None
    assert requested.token_budget is None
    assert requested.item_budget is None


def test_requested_descriptor_preserves_caller_non_null_values() -> None:
    manifest = _build_manifest(
        decision_context=_decision_context(
            workspace_supplied=True,
            requested_byte_budget=2048,
            requested_token_budget=512,
            requested_item_budget=7,
        ),
    )
    requested = manifest.request.requested
    assert requested.workspace_supplied is True
    assert requested.byte_budget == 2048
    assert requested.token_budget == 512
    assert requested.item_budget == 7


def test_effective_descriptor_uses_engine_values() -> None:
    manifest = _build_manifest(
        decision_context=_decision_context(
            effective_workspace_id=WORKSPACE,
            effective_byte_budget=4096,
            effective_token_budget=1000,
        ),
    )
    effective = manifest.request.effective
    assert effective.workspace_id == str(WORKSPACE)
    assert effective.byte_budget == 4096
    assert effective.token_budget == 1000


def test_effective_item_budget_remains_null() -> None:
    manifest = _build_manifest(
        decision_context=_decision_context(
            requested_item_budget=7,
            effective_byte_budget=4096,
        ),
    )
    # Requested may be non-null (caller asked for one), but effective is always
    # null for startup v1.
    assert manifest.request.requested.item_budget == 7
    assert manifest.request.effective.item_budget is None


def test_workspace_supplied_boolean_is_exact() -> None:
    for supplied in (True, False):
        manifest = _build_manifest(
            decision_context=_decision_context(workspace_supplied=supplied),
        )
        assert manifest.request.requested.workspace_supplied is supplied


def test_profile_ids_version_copied_from_memory_context() -> None:
    memory_context = _unrestricted_context(
        profile_id=PROFILE,
        profile_revision_id=PROFILE_REV,
        profile_version=3,
    )
    manifest = _build_manifest(memory_context=memory_context)
    subject = manifest.subject
    assert subject.memory_profile_id == str(PROFILE)
    assert subject.memory_profile_revision_id == str(PROFILE_REV)
    assert subject.memory_profile_version == 3
    assert subject.memory_context_version == MEMORY_CONTEXT_VERSION


def test_unprofiled_context_subject_has_null_profile_fields() -> None:
    manifest = _build_manifest()
    subject = manifest.subject
    assert subject.memory_profile_id is None
    assert subject.memory_profile_revision_id is None
    assert subject.memory_profile_version is None


def test_candidate_strategy_version_copied_from_executed_result() -> None:
    manifest = _build_manifest(
        decision_context=_decision_context(
            candidate_strategy_version="startup-candidates-v2"
        ),
    )
    assert manifest.versions.candidate_strategy_version == "startup-candidates-v2"


def test_subject_workspace_id_matches_effective_workspace_id() -> None:
    manifest = _build_manifest(
        decision_context=_decision_context(effective_workspace_id=WORKSPACE),
    )
    assert manifest.subject.workspace_id == str(WORKSPACE)
    assert manifest.subject.workspace_id == manifest.request.effective.workspace_id


# ─── Manifest is built from the supplied finalized response ─────────────


def test_manifest_built_from_supplied_finalized_response() -> None:
    items = [_item(content="alpha"), _item(id_=ITEM_B, content="beta")]
    response = _Response(items=items)
    manifest = _build_manifest(response=response)
    assert manifest.result.item_count == 2
    assert [i.served_content_hash for i in manifest.items] == [
        # served_content_hash is exact-byte SHA-256 of served content.
        "sha256:" + hashlib.sha256(b"alpha").hexdigest(),
        "sha256:" + hashlib.sha256(b"beta").hexdigest(),
    ]


def test_manifest_is_immutable_to_post_build_response_mutation() -> None:
    """The manifest must reflect the served snapshot at build time; mutating
    the response afterward must not change the already-built manifest."""
    items = [_item(content="served-alpha"), _item(id_=ITEM_B, content="served-beta")]
    response = _Response(items=items)
    manifest = _build_manifest(response=response)
    original_hash = compute_manifest_hash(manifest)
    # Mutate the underlying response after the build.
    response.items[0]["content"] = "MUTATED-alpha"
    response.items.clear()
    response.working_set = ""
    response.item_count = 0
    # Re-dump the manifest — it must be unchanged (it is a snapshot).
    dumped = manifest.model_dump(mode="json", exclude_none=False, by_alias=True)
    rebuilt = ContextManifestV1.model_validate(dumped)
    rebuilt_hash = compute_manifest_hash(rebuilt)
    assert rebuilt_hash == original_hash


# ─── Config: NaN / ±Infinity timeout validator ─────────────────────────


def test_config_rejects_nan_timeout() -> None:
    from engram.config import Settings

    with pytest.raises((ValidationError, ValueError)):
        Settings(context_receipt_dark_write_timeout_seconds=float("nan"))


def test_config_rejects_positive_infinity_timeout() -> None:
    from engram.config import Settings

    with pytest.raises((ValidationError, ValueError)):
        Settings(context_receipt_dark_write_timeout_seconds=float("inf"))


def test_config_rejects_negative_infinity_timeout() -> None:
    from engram.config import Settings

    with pytest.raises((ValidationError, ValueError)):
        Settings(context_receipt_dark_write_timeout_seconds=float("-inf"))


def test_config_accepts_valid_finite_positive_timeout() -> None:
    from engram.config import Settings

    s = Settings(context_receipt_dark_write_timeout_seconds=2.5)
    assert s.context_receipt_dark_write_timeout_seconds == 2.5
