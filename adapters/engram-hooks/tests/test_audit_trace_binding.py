"""Bridge integration tests for audit trace binding (Blocker B).

These tests prove that:
1. The actual ``recall_bridge.pre_llm_call`` path emits a fully bound trace.
2. The trace contains the real query/session/turn data, not just a direct
   ``emit_audit_trace()`` call.
3. The prompt hash is computed from the actual prompt.
4. The session digest is derived from the actual session.
5. Negative tests reject wrong prompt, retrieved-but-not-injected, etc.

The canonical prompt hashing contract is:

    SHA-256 of the exact UTF-8 prompt after:
    1. converting CRLF/CR to LF;
    2. removing no content;
    3. adding no implicit trailing newline.
"""
from __future__ import annotations

import hashlib
import json
import sys
import types
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

if "agent.memory_provider" not in sys.modules:
    provider_module = types.ModuleType("agent.memory_provider")
    provider_module.MemoryProvider = type("MemoryProvider", (), {})  # type: ignore[attr-defined]
    agent_module = types.ModuleType("agent")
    agent_module.memory_provider = provider_module  # type: ignore[attr-defined]
    sys.modules["agent"] = agent_module
    sys.modules["agent.memory_provider"] = provider_module

_PLUGIN_DIR = Path(__file__).resolve().parents[1] / "hermes_plugin"
sys.path.insert(0, str(_PLUGIN_DIR))

from engram_memory.audit_trace import audit_prompt_sha256, emit_audit_trace  # noqa: E402
from engram_memory.recall_bridge import RecallBridge  # noqa: E402

_TRACE_ENV_VAR = "ENGRAM_HOOKS_AUDIT_TRACE_FILE"

# Canonical prompts (must match run_memory_e2e_audit.py exactly).
RECALL_PROMPT = "What is the controlled Engram recall marker?"
EPISTEMIC_PROMPT = "What color is the sky on February 30th?"
RECALL_PROMPT_SHA256 = audit_prompt_sha256(RECALL_PROMPT)
EPISTEMIC_PROMPT_SHA256 = audit_prompt_sha256(EPISTEMIC_PROMPT)


@dataclass
class _Config:
    base_url: str = "https://engram.example"
    api_key: str = "eng_test"
    recall_enabled: bool = True
    recall_timeout: float = 5.0
    recall_item_budget: int = 5
    recall_byte_budget: int = 8192
    recall_max_context_bytes: int = 12000
    recall_followup_turns: int = 3
    recall_breaker_failures: int = 3
    recall_max_sessions: int = 512


class _Factory:
    def __init__(self, behavior: Callable[..., Awaitable[Any]]) -> None:
        self.behavior = behavior
        self.calls: list[dict[str, Any]] = []
        self.instances = 0
        self.closes = 0

    def __call__(self) -> Any:
        owner = self
        owner.instances += 1

        class Client:
            async def recall(self, **kwargs: Any) -> Any:
                owner.calls.append(kwargs)
                return await owner.behavior(**kwargs)

            async def close(self) -> None:
                owner.closes += 1

        return Client()


def _response_with_item(item_id: str, *, mode: str = "semantic") -> Any:
    items = [
        {
            "id": item_id,
            "content": f"content for {item_id}",
            "kind": "fact",
            "review_status": "active",
            "source_trust": 0.8,
            "memory_confidence": 0.7,
            "human_verified": False,
            "score": 0.75,
            "importance": 0.5,
            "pinned": False,
            "reasons": ["semantic"],
            "warnings": [],
        }
    ]
    return SimpleNamespace(items=items, recall_log_id=f"{mode}-log-123")


async def _respond_with_item(item_id: str) -> Callable[..., Awaitable[Any]]:
    async def behavior(**kwargs: Any) -> Any:
        mode = kwargs["mode"]
        if mode == "semantic":
            return _response_with_item(item_id, mode=mode)
        return SimpleNamespace(items=[], recall_log_id="startup-log")

    return behavior


def _read_trace_lines(path: Path) -> list[dict[str, object]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# ── Prompt hash canonicalization tests ───────────────────────────────────────


def test_prompt_hash_is_stable() -> None:
    """The canonical prompt hash is deterministic."""
    h1 = audit_prompt_sha256(RECALL_PROMPT)
    h2 = audit_prompt_sha256(RECALL_PROMPT)
    assert h1 == h2
    assert len(h1) == 64
    assert all(c in "0123456789abcdef" for c in h1)


def test_prompt_hash_normalizes_crlf() -> None:
    """CRLF and CR are normalized to LF before hashing."""
    raw = "What is the controlled Engram recall marker?"
    crlf = "What is the controlled Engram recall marker?\r\n"
    assert audit_prompt_sha256(raw) == audit_prompt_sha256(crlf.rstrip())
    # CRLF inside the string is normalized.
    assert audit_prompt_sha256("line1\r\nline2") == audit_prompt_sha256(
        "line1\nline2"
    )


def test_prompt_hash_no_implicit_trailing_newline() -> None:
    """Adding a trailing newline changes the hash."""
    prompt = "test prompt"
    assert audit_prompt_sha256(prompt) != audit_prompt_sha256(prompt + "\n")


# ── Audit metadata validation tests ─────────────────────────────────────────


def test_run_uuid_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A valid UUID run ID is included in the trace."""
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv(_TRACE_ENV_VAR, str(trace_file))
    run_id = str(uuid.uuid4())
    monkeypatch.setenv("ENGRAM_HOOKS_AUDIT_RUN_ID", run_id)
    emit_audit_trace({
        "recall_succeeded": True,
        "recall_log_id": "log-1",
        "retrieved_item_ids": ["item-1"],
        "injected_item_ids": ["item-1"],
    })
    rec = _read_trace_lines(trace_file)[0]
    assert rec["audit_run_id"] == run_id


def test_invalid_run_uuid_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An invalid UUID is recorded as an error, not used."""
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv(_TRACE_ENV_VAR, str(trace_file))
    monkeypatch.setenv("ENGRAM_HOOKS_AUDIT_RUN_ID", "not-a-uuid")
    emit_audit_trace({
        "recall_succeeded": True,
        "recall_log_id": "log-1",
        "retrieved_item_ids": ["item-1"],
        "injected_item_ids": ["item-1"],
    })
    rec = _read_trace_lines(trace_file)[0]
    assert "audit_run_id" not in rec
    assert rec["audit_run_id_error"] == "invalid_uuid"


def test_fixture_enum_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A valid fixture value ('recall' or 'epistemic') is accepted."""
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv(_TRACE_ENV_VAR, str(trace_file))
    monkeypatch.setenv("ENGRAM_HOOKS_AUDIT_FIXTURE", "recall")
    emit_audit_trace({
        "recall_succeeded": True,
        "recall_log_id": "log-1",
        "retrieved_item_ids": ["item-1"],
        "injected_item_ids": ["item-1"],
    })
    rec = _read_trace_lines(trace_file)[0]
    assert rec["audit_fixture"] == "recall"


def test_invalid_fixture_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An invalid fixture value is recorded as an error."""
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv(_TRACE_ENV_VAR, str(trace_file))
    monkeypatch.setenv("ENGRAM_HOOKS_AUDIT_FIXTURE", "bogus")
    emit_audit_trace({
        "recall_succeeded": True,
        "recall_log_id": "log-1",
        "retrieved_item_ids": ["item-1"],
        "injected_item_ids": ["item-1"],
    })
    rec = _read_trace_lines(trace_file)[0]
    assert "audit_fixture" not in rec
    assert rec["audit_fixture_error"] == "invalid_fixture"


def test_expected_prompt_hash_match(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When expected_prompt_sha256 matches actual, match is True."""
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv(_TRACE_ENV_VAR, str(trace_file))
    expected = audit_prompt_sha256(RECALL_PROMPT)
    monkeypatch.setenv("ENGRAM_HOOKS_AUDIT_EXPECTED_PROMPT_SHA256", expected)
    emit_audit_trace({
        "recall_succeeded": True,
        "recall_log_id": "log-1",
        "retrieved_item_ids": ["item-1"],
        "injected_item_ids": ["item-1"],
        "query": RECALL_PROMPT,
    })
    rec = _read_trace_lines(trace_file)[0]
    assert rec["prompt_sha256"] == expected
    assert rec["expected_prompt_sha256_match"] is True


def test_expected_prompt_hash_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When expected_prompt_sha256 does NOT match actual, error_code is set."""
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv(_TRACE_ENV_VAR, str(trace_file))
    wrong_hash = "a" * 64
    monkeypatch.setenv("ENGRAM_HOOKS_AUDIT_EXPECTED_PROMPT_SHA256", wrong_hash)
    emit_audit_trace({
        "recall_succeeded": True,
        "recall_log_id": "log-1",
        "retrieved_item_ids": ["item-1"],
        "injected_item_ids": ["item-1"],
        "query": RECALL_PROMPT,
    })
    rec = _read_trace_lines(trace_file)[0]
    assert rec["expected_prompt_sha256_match"] is False
    assert rec["error_code"] == "audit_prompt_mismatch"


# ── Full pre_llm_call integration test ──────────────────────────────────────


def test_bridge_integration_emits_fully_bound_trace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The actual pre_llm_call path passes real query/session/turn data.

    This test is NOT a direct emit_audit_trace() call — it invokes the
    full RecallBridge.pre_llm_call path which internally calls the trace sink.
    """
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv(_TRACE_ENV_VAR, str(trace_file))
    run_id = str(uuid.uuid4())
    monkeypatch.setenv("ENGRAM_HOOKS_AUDIT_RUN_ID", run_id)
    monkeypatch.setenv("ENGRAM_HOOKS_AUDIT_FIXTURE", "recall")
    monkeypatch.setenv(
        "ENGRAM_HOOKS_AUDIT_EXPECTED_PROMPT_SHA256", RECALL_PROMPT_SHA256
    )

    item_id = "fixture-r-item-001"

    async def behavior(**kwargs: Any) -> Any:
        mode = kwargs["mode"]
        if mode == "semantic":
            return _response_with_item(item_id, mode=mode)
        return SimpleNamespace(items=[], recall_log_id="startup-log")

    factory = _Factory(behavior)
    bridge = RecallBridge(_Config(), client_factory=factory)

    result = bridge.pre_llm_call(
        user_message=RECALL_PROMPT,
        session_id="test-session-abc",
        is_first_turn=True,
    )
    assert result is not None

    records = _read_trace_lines(trace_file)
    assert len(records) == 1
    rec = records[0]

    # Trace schema.
    assert rec["schema"] == "engram.hermes-hook-audit-trace"
    assert rec["schema_version"] == "2.1"

    # Retrieved/injected item IDs reflect the rendered evidence path.
    assert item_id in rec["retrieved_item_ids"]
    assert item_id in rec["injected_item_ids"]

    # Schema 2.1: configured_item_budget is attested.
    assert rec["configured_item_budget"] == 5  # _Config default

    # Prompt hash matches the actual prompt.
    assert rec["prompt_sha256"] == RECALL_PROMPT_SHA256
    assert rec["expected_prompt_sha256_match"] is True

    # Session digest is derived from the actual session.
    expected_session_digest = hashlib.sha256(
        b"test-session-abc"
    ).hexdigest()[:12]
    assert rec["session_id_digest"] == expected_session_digest

    # Turn index reflects the actual hook turn.
    assert rec["turn_index"] == 1

    # Audit binding.
    assert rec["audit_run_id"] == run_id
    assert rec["audit_fixture"] == "recall"

    # Recall log ID present.
    assert rec["recall_log_id"] is not None


# ── Negative integration tests ──────────────────────────────────────────────


def test_wrong_expected_prompt_hash_records_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A wrong expected prompt hash produces a mismatch error_code."""
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv(_TRACE_ENV_VAR, str(trace_file))
    wrong_hash = "b" * 64
    monkeypatch.setenv("ENGRAM_HOOKS_AUDIT_EXPECTED_PROMPT_SHA256", wrong_hash)

    async def behavior(**kwargs: Any) -> Any:
        mode = kwargs["mode"]
        if mode == "semantic":
            return _response_with_item("item-1", mode=mode)
        return SimpleNamespace(items=[], recall_log_id="startup-log")

    factory = _Factory(behavior)
    bridge = RecallBridge(_Config(), client_factory=factory)
    bridge.pre_llm_call(
        user_message=RECALL_PROMPT,
        session_id="s",
        is_first_turn=True,
    )
    rec = _read_trace_lines(trace_file)[0]
    assert rec["expected_prompt_sha256_match"] is False
    assert rec["error_code"] == "audit_prompt_mismatch"


def test_trace_disabled_does_not_break_recall(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When trace is disabled, recall still works normally."""
    monkeypatch.delenv(_TRACE_ENV_VAR, raising=False)
    trace_file = tmp_path / "trace.jsonl"

    async def behavior(**kwargs: Any) -> Any:
        mode = kwargs["mode"]
        if mode == "semantic":
            return _response_with_item("item-1", mode=mode)
        return SimpleNamespace(items=[], recall_log_id="startup-log")

    factory = _Factory(behavior)
    bridge = RecallBridge(_Config(), client_factory=factory)
    result = bridge.pre_llm_call(
        user_message="test query", session_id="s", is_first_turn=True
    )
    assert result is not None
    assert not trace_file.exists()


def test_trace_write_failure_does_not_break_recall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When trace file is unwritable, recall still works."""
    # Point trace at a directory path (unwritable as a file).
    monkeypatch.setenv(_TRACE_ENV_VAR, "/dev/null/nonexistent/dir/trace.jsonl")

    async def behavior(**kwargs: Any) -> Any:
        mode = kwargs["mode"]
        if mode == "semantic":
            return _response_with_item("item-1", mode=mode)
        return SimpleNamespace(items=[], recall_log_id="startup-log")

    factory = _Factory(behavior)
    bridge = RecallBridge(_Config(), client_factory=factory)
    result = bridge.pre_llm_call(
        user_message="test query", session_id="s", is_first_turn=True
    )
    assert result is not None


def test_query_digest_emission(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The query_digest is derived from the actual query."""
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv(_TRACE_ENV_VAR, str(trace_file))

    async def behavior(**kwargs: Any) -> Any:
        mode = kwargs["mode"]
        if mode == "semantic":
            return _response_with_item("item-1", mode=mode)
        return SimpleNamespace(items=[], recall_log_id="startup-log")

    factory = _Factory(behavior)
    bridge = RecallBridge(_Config(), client_factory=factory)
    bridge.pre_llm_call(
        user_message="unique test query 12345",
        session_id="s",
        is_first_turn=True,
    )
    rec = _read_trace_lines(trace_file)[0]
    expected_digest = hashlib.sha256(
        b"unique test query 12345"
    ).hexdigest()[:12]
    assert rec["query_digest"] == expected_digest


def test_turn_index_increments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Turn index reflects the actual hook turn count per session."""
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv(_TRACE_ENV_VAR, str(trace_file))

    async def behavior(**kwargs: Any) -> Any:
        mode = kwargs["mode"]
        if mode == "semantic":
            return _response_with_item("item-1", mode=mode)
        return SimpleNamespace(items=[], recall_log_id="startup-log")

    factory = _Factory(behavior)
    bridge = RecallBridge(_Config(), client_factory=factory)
    bridge.pre_llm_call(user_message="q1", session_id="s", is_first_turn=True)
    bridge.pre_llm_call(user_message="q2", session_id="s", is_first_turn=False)
    records = _read_trace_lines(trace_file)
    assert len(records) == 2
    assert records[0]["turn_index"] == 1
    assert records[1]["turn_index"] == 2
    # Session digest is the same for both (same session).
    assert records[0]["session_id_digest"] == records[1]["session_id_digest"]


def test_no_raw_prompt_or_session_in_trace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No raw prompt or session ID may appear in the trace file."""
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv(_TRACE_ENV_VAR, str(trace_file))

    secret_prompt = "What is the controlled Engram recall marker?"
    secret_session = "super-secret-session-id-xyz"

    async def behavior(**kwargs: Any) -> Any:
        mode = kwargs["mode"]
        if mode == "semantic":
            return _response_with_item("item-1", mode=mode)
        return SimpleNamespace(items=[], recall_log_id="startup-log")

    factory = _Factory(behavior)
    bridge = RecallBridge(_Config(), client_factory=factory)
    bridge.pre_llm_call(
        user_message=secret_prompt,
        session_id=secret_session,
        is_first_turn=True,
    )
    raw = trace_file.read_text()
    assert secret_prompt not in raw
    assert secret_session not in raw
