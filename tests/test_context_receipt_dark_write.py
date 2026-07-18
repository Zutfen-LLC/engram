"""Pure unit tests for the startup context-receipt dark-write orchestrator
(ENG-CONTEXT-002B).

These tests are DB-free. They exercise the orchestrator's:

- ``disabled`` short-circuit (no manifest, no DB session, no telemetry);
- requested/effective descriptor mapping (caller nulls preserved, engine
  values used, effective item budget always null);
- ``workspace_supplied`` Boolean exactness;
- profile IDs/version copied from ``ResolvedMemoryContext``;
- candidate-strategy version copied from the executed result;
- manifest built from the supplied finalized response (mutation boundary);
- timeout returns ``timed_out``;
- ordinary failure returns ``failed`` with a bounded failure_stage and
  exception *type*;
- asyncio cancellation is NOT swallowed;
- safe logging excludes exception messages and content;
- usage telemetry receives only bounded aggregate metadata.

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
    StartupReceiptDecisionContext,
    _build_manifest,
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


# ─── Disabled path ──────────────────────────────────────────────────────


async def test_disabled_returns_disabled_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", False)

    def _boom_session(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not open a DB session when disabled")

    def _boom_telemetry(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not record telemetry when disabled")

    monkeypatch.setattr("engram.context_receipt_dark_write.async_session_factory", _boom_session)
    monkeypatch.setattr("engram.usage.record_context_receipt_dark_write", _boom_telemetry)

    response = _Response()
    memory_context = _unrestricted_context()
    decision_context = _decision_context()
    result = await write_startup_context_receipt_best_effort(
        response=response,
        recall_log_id=uuid.uuid4(),
        memory_context=memory_context,
        decision_context=decision_context,
    )
    assert result.status == "disabled"
    assert result.latency_ms == 0
    assert result.receipt_id is None
    assert result.failure_stage is None
    assert result.exception_type is None


async def test_disabled_does_not_construct_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", False)

    def _boom_build(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not build a manifest when disabled")

    monkeypatch.setattr(
        "engram.context_receipt_dark_write._build_manifest", _boom_build
    )

    result = await write_startup_context_receipt_best_effort(
        response=_Response(),
        recall_log_id=uuid.uuid4(),
        memory_context=_unrestricted_context(),
        decision_context=_decision_context(),
    )
    assert result.status == "disabled"


# ─── Descriptor mapping ─────────────────────────────────────────────────


def test_requested_descriptor_preserves_caller_null_values() -> None:
    memory_context = _unrestricted_context()
    decision_context = _decision_context(
        workspace_supplied=False,
        requested_byte_budget=None,
        requested_token_budget=None,
        requested_item_budget=None,
        effective_byte_budget=4096,
        effective_token_budget=None,
    )
    manifest = _build_manifest(
        response=_Response(),
        memory_context=memory_context,
        decision_context=decision_context,
    )
    requested = manifest.request.requested
    assert requested.workspace_supplied is False
    assert requested.byte_budget is None
    assert requested.token_budget is None
    assert requested.item_budget is None


def test_requested_descriptor_preserves_caller_non_null_values() -> None:
    memory_context = _unrestricted_context()
    decision_context = _decision_context(
        workspace_supplied=True,
        requested_byte_budget=2048,
        requested_token_budget=512,
        requested_item_budget=7,
    )
    manifest = _build_manifest(
        response=_Response(),
        memory_context=memory_context,
        decision_context=decision_context,
    )
    requested = manifest.request.requested
    assert requested.workspace_supplied is True
    assert requested.byte_budget == 2048
    assert requested.token_budget == 512
    assert requested.item_budget == 7


def test_effective_descriptor_uses_engine_values() -> None:
    memory_context = _unrestricted_context()
    decision_context = _decision_context(
        effective_workspace_id=WORKSPACE,
        effective_byte_budget=4096,
        effective_token_budget=1000,
    )
    manifest = _build_manifest(
        response=_Response(),
        memory_context=memory_context,
        decision_context=decision_context,
    )
    effective = manifest.request.effective
    assert effective.workspace_id == str(WORKSPACE)
    assert effective.byte_budget == 4096
    assert effective.token_budget == 1000


def test_effective_item_budget_remains_null() -> None:
    memory_context = _unrestricted_context()
    decision_context = _decision_context(
        requested_item_budget=7,
        effective_byte_budget=4096,
    )
    manifest = _build_manifest(
        response=_Response(),
        memory_context=memory_context,
        decision_context=decision_context,
    )
    # Requested may be non-null (caller asked for one), but effective is always
    # null for startup v1.
    assert manifest.request.requested.item_budget == 7
    assert manifest.request.effective.item_budget is None


def test_workspace_supplied_boolean_is_exact() -> None:
    memory_context = _unrestricted_context()
    for supplied in (True, False):
        decision_context = _decision_context(workspace_supplied=supplied)
        manifest = _build_manifest(
            response=_Response(),
            memory_context=memory_context,
            decision_context=decision_context,
        )
        assert manifest.request.requested.workspace_supplied is supplied


def test_profile_ids_version_copied_from_memory_context() -> None:
    memory_context = _unrestricted_context(
        profile_id=PROFILE,
        profile_revision_id=PROFILE_REV,
        profile_version=3,
    )
    decision_context = _decision_context()
    manifest = _build_manifest(
        response=_Response(),
        memory_context=memory_context,
        decision_context=decision_context,
    )
    subject = manifest.subject
    assert subject.memory_profile_id == str(PROFILE)
    assert subject.memory_profile_revision_id == str(PROFILE_REV)
    assert subject.memory_profile_version == 3
    assert subject.memory_context_version == MEMORY_CONTEXT_VERSION


def test_unprofiled_context_subject_has_null_profile_fields() -> None:
    memory_context = _unrestricted_context()
    decision_context = _decision_context()
    manifest = _build_manifest(
        response=_Response(),
        memory_context=memory_context,
        decision_context=decision_context,
    )
    subject = manifest.subject
    assert subject.memory_profile_id is None
    assert subject.memory_profile_revision_id is None
    assert subject.memory_profile_version is None


def test_candidate_strategy_version_copied_from_executed_result() -> None:
    memory_context = _unrestricted_context()
    decision_context = _decision_context(
        candidate_strategy_version="startup-candidates-v2"
    )
    manifest = _build_manifest(
        response=_Response(),
        memory_context=memory_context,
        decision_context=decision_context,
    )
    assert manifest.versions.candidate_strategy_version == "startup-candidates-v2"


def test_subject_workspace_id_matches_effective_workspace_id() -> None:
    memory_context = _unrestricted_context()
    decision_context = _decision_context(effective_workspace_id=WORKSPACE)
    manifest = _build_manifest(
        response=_Response(),
        memory_context=memory_context,
        decision_context=decision_context,
    )
    assert manifest.subject.workspace_id == str(WORKSPACE)
    assert manifest.subject.workspace_id == manifest.request.effective.workspace_id


# ─── Manifest is built from the supplied finalized response ─────────────


def test_manifest_built_from_supplied_finalized_response() -> None:
    memory_context = _unrestricted_context()
    decision_context = _decision_context(effective_byte_budget=4096)
    items = [_item(content="alpha"), _item(id_=ITEM_B, content="beta")]
    response = _Response(items=items)
    manifest = _build_manifest(
        response=response,
        memory_context=memory_context,
        decision_context=decision_context,
    )
    assert manifest.result.item_count == 2
    assert [i.served_content_hash for i in manifest.items] == [
        # served_content_hash is exact-byte SHA-256 of served content.
        "sha256:" + hashlib.sha256(b"alpha").hexdigest(),
        "sha256:" + hashlib.sha256(b"beta").hexdigest(),
    ]


def test_manifest_is_immutable_to_post_build_response_mutation() -> None:
    """The manifest must reflect the served snapshot at build time; mutating
    the response afterward must not change the already-built manifest."""
    memory_context = _unrestricted_context()
    decision_context = _decision_context(effective_byte_budget=4096)
    items = [_item(content="served-alpha"), _item(id_=ITEM_B, content="served-beta")]
    response = _Response(items=items)
    manifest = _build_manifest(
        response=response,
        memory_context=memory_context,
        decision_context=decision_context,
    )
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


# ─── Timeout / failure / cancellation ────────────────────────────────────


async def test_timeout_returns_timed_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured timeout that fires before the attempt completes returns
    ``timed_out`` and never raises into the route."""
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "context_receipt_dark_write_timeout_seconds", 0.01)
    _patch_session_factory(monkeypatch)

    async def _slow_store(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(10)
        return None

    # Patch store_context_receipt to hang so the timeout fires.
    monkeypatch.setattr(
        "engram.context_receipt_dark_write.store_context_receipt", _slow_store
    )

    result = await write_startup_context_receipt_best_effort(
        response=_Response(),
        recall_log_id=uuid.uuid4(),
        memory_context=_unrestricted_context(),
        decision_context=_decision_context(),
    )
    assert result.status == "timed_out"
    assert result.failure_stage == "timeout"
    assert result.receipt_id is None


async def test_ordinary_failure_returns_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "context_receipt_dark_write_timeout_seconds", 5.0)
    _patch_session_factory(monkeypatch)

    class _StoreError(Exception):
        pass

    async def _raise_store(*args: Any, **kwargs: Any) -> Any:
        raise _StoreError("store blew up with SECRET-SENTINEL-VALUE")

    monkeypatch.setattr("engram.context_receipt_dark_write.store_context_receipt", _raise_store)

    result = await write_startup_context_receipt_best_effort(
        response=_Response(),
        recall_log_id=uuid.uuid4(),
        memory_context=_unrestricted_context(),
        decision_context=_decision_context(),
    )
    assert result.status == "failed"
    assert result.failure_stage == "store"
    assert result.exception_type == "_StoreError"
    assert result.receipt_id is None


async def test_builder_value_error_returns_failed_build_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "context_receipt_dark_write_timeout_seconds", 5.0)

    def _raise_build(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("bad manifest input SECRET")

    monkeypatch.setattr(
        "engram.context_receipt_dark_write._build_manifest", _raise_build
    )
    result = await write_startup_context_receipt_best_effort(
        response=_Response(),
        recall_log_id=uuid.uuid4(),
        memory_context=_unrestricted_context(),
        decision_context=_decision_context(),
    )
    assert result.status == "failed"
    assert result.failure_stage == "build_manifest"


async def test_asyncio_cancellation_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """asyncio.CancelledError must propagate out of the best-effort wrapper,
    never be translated into a ``failed`` result."""
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "context_receipt_dark_write_timeout_seconds", 5.0)
    _patch_session_factory(monkeypatch)

    async def _raise_cancel(*args: Any, **kwargs: Any) -> Any:
        raise asyncio.CancelledError()

    monkeypatch.setattr("engram.context_receipt_dark_write.store_context_receipt", _raise_cancel)

    with pytest.raises(asyncio.CancelledError):
        await write_startup_context_receipt_best_effort(
            response=_Response(),
            recall_log_id=uuid.uuid4(),
            memory_context=_unrestricted_context(),
            decision_context=_decision_context(),
        )


async def test_context_receipt_conflict_error_is_fail_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "context_receipt_dark_write_timeout_seconds", 5.0)
    _patch_session_factory(monkeypatch)

    async def _raise_conflict(*args: Any, **kwargs: Any) -> Any:
        raise ContextReceiptConflictError("conflict SECRET")

    monkeypatch.setattr("engram.context_receipt_dark_write.store_context_receipt", _raise_conflict)
    result = await write_startup_context_receipt_best_effort(
        response=_Response(),
        recall_log_id=uuid.uuid4(),
        memory_context=_unrestricted_context(),
        decision_context=_decision_context(),
    )
    assert result.status == "failed"
    assert result.failure_stage == "store"
    assert result.exception_type == "ContextReceiptConflictError"


async def test_context_receipt_integrity_error_is_fail_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "context_receipt_dark_write_timeout_seconds", 5.0)
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
    result = await write_startup_context_receipt_best_effort(
        response=_Response(),
        recall_log_id=uuid.uuid4(),
        memory_context=_unrestricted_context(),
        decision_context=_decision_context(),
    )
    assert result.status == "failed"
    assert result.failure_stage == "verify"
    assert result.exception_type == "ContextReceiptIntegrityError"


async def test_unexpected_helper_exception_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the helper itself violates its no-raise contract with a bare
    Exception, the route-level guard catches it; here we simulate by making
    the wrapper itself raise via a patched settings attribute access."""
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "context_receipt_dark_write_timeout_seconds", 5.0)

    # Simulate the enabled check raising a non-Exception (SystemExit) — but
    # SystemExit is NOT an Exception subclass, so it should propagate. Test
    # that a plain Exception inside _write_startup_context_receipt_once is
    # handled: patch session creation to raise a bare Exception before any
    # stage is reached.
    call_count = {"n": 0}

    class _SessionCM:
        def __init__(self) -> None:
            raise RuntimeError("unexpected SECRET during session creation")

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    def _factory() -> Any:
        call_count["n"] += 1
        return _SessionCM()

    monkeypatch.setattr(
        "engram.context_receipt_dark_write.async_session_factory", _factory
    )
    result = await write_startup_context_receipt_best_effort(
        response=_Response(),
        recall_log_id=uuid.uuid4(),
        memory_context=_unrestricted_context(),
        decision_context=_decision_context(),
    )
    assert result.status == "failed"
    assert result.failure_stage == "open_session"
    assert result.exception_type == "RuntimeError"


# ─── Safe logging excludes exception messages and content ──────────────


async def test_safe_logging_excludes_exception_messages_and_content(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "context_receipt_dark_write_timeout_seconds", 5.0)
    monkeypatch.setattr(settings, "usage_telemetry_enabled", False)

    class _StoreError(Exception):
        pass

    secret = "SUPER-SECRET-SENTINEL-XYZ"

    async def _raise_store(*args: Any, **kwargs: Any) -> Any:
        raise _StoreError(f"store failed with {secret}")

    monkeypatch.setattr("engram.context_receipt_dark_write.store_context_receipt", _raise_store)

    with caplog.at_level(logging.WARNING, logger="engram.context_receipt_dark_write"):
        result = await write_startup_context_receipt_best_effort(
            response=_Response(),
            recall_log_id=uuid.uuid4(),
            memory_context=_unrestricted_context(),
            decision_context=_decision_context(),
        )
    assert result.status == "failed"
    # The secret sentinel must not appear anywhere in the captured logs.
    for record in caplog.records:
        assert secret not in record.getMessage()
        assert secret not in str(record.args)
    # Raw content must never appear in logs.
    for record in caplog.records:
        assert "hello world" not in record.getMessage()


async def test_no_logger_exception_on_fail_open_path(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``logger.exception`` is prohibited on this fail-open path because
    exception representations may include bound values. The wrapper must use
    safe warning logs containing exception type only."""
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "context_receipt_dark_write_timeout_seconds", 5.0)
    monkeypatch.setattr(settings, "usage_telemetry_enabled", False)

    async def _raise_store(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("store failed SECRET")

    monkeypatch.setattr("engram.context_receipt_dark_write.store_context_receipt", _raise_store)

    with caplog.at_level(logging.DEBUG, logger="engram.context_receipt_dark_write"):
        await write_startup_context_receipt_best_effort(
            response=_Response(),
            recall_log_id=uuid.uuid4(),
            memory_context=_unrestricted_context(),
            decision_context=_decision_context(),
        )
    # No record may carry an exception traceback (exc_info).
    for record in caplog.records:
        assert record.exc_info is None


# ─── Usage telemetry receives only bounded aggregate metadata ───────────


async def test_usage_telemetry_receives_only_bounded_aggregate_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "context_receipt_dark_write_timeout_seconds", 5.0)
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

    monkeypatch.setattr("engram.context_receipt_dark_write.store_context_receipt", _raise_store)

    result = await write_startup_context_receipt_best_effort(
        response=_Response(),
        recall_log_id=uuid.uuid4(),
        memory_context=_unrestricted_context(),
        decision_context=_decision_context(),
    )
    assert result.status == "failed"
    # Telemetry must have been called with bounded aggregate metadata. The
    # wrapper (record_context_receipt_dark_write) is patched here, so the
    # captured kwargs are the bounded inputs the orchestrator passes — NOT
    # event_type (which the wrapper itself would add when it calls
    # record_usage_event_best_effort).
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
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "context_receipt_dark_write_timeout_seconds", 5.0)
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

    monkeypatch.setattr("engram.context_receipt_dark_write.store_context_receipt", _fake_store)

    result = await write_startup_context_receipt_best_effort(
        response=_Response(),
        recall_log_id=uuid.uuid4(),
        memory_context=_unrestricted_context(),
        decision_context=_decision_context(),
    )
    # The telemetry failure must not have changed the dark-write result.
    assert result.status == "failed"
    assert result.failure_stage == "store"


# ─── Fix #3b: telemetry bounded within the configured timeout ──────────


async def test_slow_telemetry_bounded_by_remaining_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled telemetry connection must not hold the request beyond the
    configured receipt timeout (Fix #3b). The telemetry call is wrapped in a
    ``wait_for`` with the remaining deadline; the receipt result is preserved
    when telemetry times out.
    """
    monkeypatch.setattr(settings, "context_receipt_dark_write_enabled", True)
    monkeypatch.setattr(settings, "context_receipt_dark_write_timeout_seconds", 0.1)
    monkeypatch.setattr(settings, "usage_telemetry_enabled", True)
    _patch_session_factory(monkeypatch)

    # Make the main attempt fail fast so most of the timeout budget remains.
    async def _raise_store(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("fast store failure")

    monkeypatch.setattr("engram.context_receipt_dark_write.store_context_receipt", _raise_store)

    telemetry_called = {"n": 0}

    async def _slow_telemetry(**kwargs: Any) -> Any:
        telemetry_called["n"] += 1
        await asyncio.sleep(10)  # would hold far beyond the timeout

    monkeypatch.setattr("engram.usage.record_context_receipt_dark_write", _slow_telemetry)

    start = asyncio.get_event_loop().time()
    result = await write_startup_context_receipt_best_effort(
        response=_Response(),
        recall_log_id=uuid.uuid4(),
        memory_context=_unrestricted_context(),
        decision_context=_decision_context(),
    )
    elapsed = asyncio.get_event_loop().time() - start
    # The receipt result is preserved (the store failure).
    assert result.status == "failed"
    assert result.failure_stage == "store"
    # Telemetry was called but bounded — total elapsed must be within the
    # configured timeout (0.1s) plus a small grace.
    assert telemetry_called["n"] == 1
    assert elapsed < 2.0, f"telemetry held the request for {elapsed:.2f}s"


# ─── Fix #3a: timeout validator rejects NaN / ±Infinity ────────────────


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


# ─── Fix #4: decision-context factory requires exact engine keys ────────


def test_decision_context_factory_missing_candidate_strategy_raises() -> None:
    from engram.context_receipt_dark_write import (
        StartupDecisionContextError,
        build_startup_decision_context_from_result,
    )

    result = {
        "effective_byte_budget": 4096,
        "effective_token_budget": None,
        "effective_item_budget": None,
    }
    with pytest.raises(StartupDecisionContextError):
        build_startup_decision_context_from_result(
            req_workspace_supplied=False,
            req_byte_budget=None,
            req_token_budget=None,
            req_item_budget=None,
            result=result,
            scoring_version="v1",
            config_version="v1",
        )


def test_decision_context_factory_missing_effective_byte_budget_raises() -> None:
    from engram.context_receipt_dark_write import (
        StartupDecisionContextError,
        build_startup_decision_context_from_result,
    )

    result = {
        "candidate_strategy_version": "startup-candidates-v1",
        "effective_token_budget": None,
        "effective_item_budget": None,
    }
    with pytest.raises(StartupDecisionContextError):
        build_startup_decision_context_from_result(
            req_workspace_supplied=False,
            req_byte_budget=None,
            req_token_budget=None,
            req_item_budget=None,
            result=result,
            scoring_version="v1",
            config_version="v1",
        )


def test_decision_context_factory_missing_effective_item_budget_raises() -> None:
    from engram.context_receipt_dark_write import (
        StartupDecisionContextError,
        build_startup_decision_context_from_result,
    )

    result = {
        "candidate_strategy_version": "startup-candidates-v1",
        "effective_byte_budget": 4096,
        "effective_token_budget": None,
    }
    with pytest.raises(StartupDecisionContextError):
        build_startup_decision_context_from_result(
            req_workspace_supplied=False,
            req_byte_budget=None,
            req_token_budget=None,
            req_item_budget=None,
            result=result,
            scoring_version="v1",
            config_version="v1",
        )


def test_decision_context_factory_non_null_effective_item_budget_raises() -> None:
    from engram.context_receipt_dark_write import (
        StartupDecisionContextError,
        build_startup_decision_context_from_result,
    )

    result = {
        "candidate_strategy_version": "startup-candidates-v1",
        "effective_byte_budget": 4096,
        "effective_token_budget": None,
        "effective_item_budget": 7,  # must be None for startup
    }
    with pytest.raises(StartupDecisionContextError):
        build_startup_decision_context_from_result(
            req_workspace_supplied=False,
            req_byte_budget=None,
            req_token_budget=None,
            req_item_budget=None,
            result=result,
            scoring_version="v1",
            config_version="v1",
        )


def test_decision_context_factory_wrong_type_candidate_strategy_raises() -> None:
    from engram.context_receipt_dark_write import (
        StartupDecisionContextError,
        build_startup_decision_context_from_result,
    )

    result = {
        "candidate_strategy_version": 123,  # must be str
        "effective_byte_budget": 4096,
        "effective_token_budget": None,
        "effective_item_budget": None,
    }
    with pytest.raises(StartupDecisionContextError):
        build_startup_decision_context_from_result(
            req_workspace_supplied=False,
            req_byte_budget=None,
            req_token_budget=None,
            req_item_budget=None,
            result=result,
            scoring_version="v1",
            config_version="v1",
        )


def test_decision_context_factory_valid_result_builds_context() -> None:
    from engram.context_receipt_dark_write import (
        build_startup_decision_context_from_result,
    )

    result = {
        "candidate_strategy_version": "startup-candidates-v1",
        "effective_byte_budget": 4096,
        "effective_token_budget": None,
        "effective_item_budget": None,
        "workspace_id": None,
    }
    dc = build_startup_decision_context_from_result(
        req_workspace_supplied=True,
        req_byte_budget=2048,
        req_token_budget=None,
        req_item_budget=5,
        result=result,
        scoring_version="v1",
        config_version="v1",
    )
    assert dc.candidate_strategy_version == "startup-candidates-v1"
    assert dc.effective_byte_budget == 4096
    assert dc.effective_token_budget is None
    assert dc.effective_item_budget is None
    assert dc.effective_workspace_id is None
    assert dc.workspace_supplied is True
    assert dc.requested_byte_budget == 2048
    assert dc.requested_item_budget == 5