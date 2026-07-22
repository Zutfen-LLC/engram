"""Tests for the opt-in structured audit trace sink.

The trace sink must:
- be disabled when ``ENGRAM_HOOKS_AUDIT_TRACE_FILE`` is unset
- never change recall behavior
- write one sanitized JSON record per hook execution
- use append-safe JSON Lines
- create files with mode 0600
- never include secrets, raw content, or raw exception text
- fail open (trace write failure never raises)
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import types
from pathlib import Path

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

from engram_memory.audit_trace import emit_audit_trace  # noqa: E402

_TRACE_ENV_VAR = "ENGRAM_HOOKS_AUDIT_TRACE_FILE"


@pytest.fixture
def clean_trace_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the trace env var is unset unless a test sets it explicitly."""
    monkeypatch.delenv(_TRACE_ENV_VAR, raising=False)


def _basic_outcome(
    *,
    retrieved: list[str] | None = None,
    injected: list[str] | None = None,
    recall_succeeded: bool = True,
    error_code: str | None = None,
) -> dict[str, object]:
    if retrieved is None:
        retrieved = ["item-semantic-1"]
    if injected is None:
        injected = ["item-semantic-1"]
    return {
        "recall_succeeded": recall_succeeded,
        "recall_log_id": "log-abc-123",
        "retrieved_item_ids": retrieved,
        "injected_item_ids": injected,
        "error_code": error_code,
    }


def _read_trace_lines(path: Path) -> list[dict[str, object]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_trace_disabled_by_default(
    clean_trace_env: None,
    tmp_path: Path,
) -> None:
    del clean_trace_env
    trace_file = tmp_path / "trace.jsonl"
    emit_audit_trace(_basic_outcome())
    assert not trace_file.exists()


def test_trace_writes_valid_jsonl(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv(_TRACE_ENV_VAR, str(trace_file))
    emit_audit_trace(_basic_outcome())
    records = _read_trace_lines(trace_file)
    assert len(records) == 1
    rec = records[0]
    assert rec["schema"] == "engram.hermes-hook-audit-trace"
    assert rec["schema_version"] == "2.0"
    assert rec["hook"] == "pre_llm_call"
    assert rec["provider"] == "engram"
    assert rec["recall_enabled"] is True
    assert rec["recall_succeeded"] is True
    assert rec["recall_log_id"] == "log-abc-123"
    assert rec["retrieved_item_ids"] == ["item-semantic-1"]
    assert rec["injected_item_ids"] == ["item-semantic-1"]
    assert rec["retrieved_item_count"] == 1
    assert rec["injected_item_count"] == 1
    assert rec["native_memory_used"] is False
    assert rec["error_code"] is None
    assert isinstance(rec["timestamp"], str)
    assert rec["timestamp"].endswith("+00:00")


def test_trace_appends_multiple_records(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv(_TRACE_ENV_VAR, str(trace_file))
    emit_audit_trace(_basic_outcome())
    emit_audit_trace(_basic_outcome())
    records = _read_trace_lines(trace_file)
    assert len(records) == 2


def test_trace_contains_no_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv(_TRACE_ENV_VAR, str(trace_file))
    # Deliberately include secret-looking and content-like fields that must
    # never appear in the trace record.
    outcome = _basic_outcome()
    outcome["api_key"] = "super-secret-key"
    outcome["base_url"] = "https://engram.example.internal"
    outcome["content"] = "The user's private memory content."
    outcome["authorization"] = "Bearer abc123"
    outcome["raw_content"] = "another secret blob"
    emit_audit_trace(outcome)
    raw_text = trace_file.read_text(encoding="utf-8")
    assert "super-secret-key" not in raw_text
    assert "engram.example.internal" not in raw_text
    assert "private memory content" not in raw_text
    assert "Bearer abc123" not in raw_text
    assert "another secret blob" not in raw_text
    record = _read_trace_lines(trace_file)[0]
    lowered_keys = {k.lower() for k in record}
    for forbidden in (
        "api_key",
        "base_url",
        "content",
        "authorization",
        "raw_content",
        "secret",
        "token",
        "password",
    ):
        assert forbidden not in lowered_keys, f"secret key leaked: {forbidden}"


def test_trace_distinguishes_retrieved_and_injected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv(_TRACE_ENV_VAR, str(trace_file))
    # Two items retrieved, only one survived context packing.
    emit_audit_trace(
        _basic_outcome(
            retrieved=["item-a", "item-b"],
            injected=["item-a"],
        )
    )
    rec = _read_trace_lines(trace_file)[0]
    assert rec["retrieved_item_ids"] == ["item-a", "item-b"]
    assert rec["injected_item_ids"] == ["item-a"]
    assert rec["retrieved_item_count"] == 2
    assert rec["injected_item_count"] == 1


def test_trace_file_mode_0600(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv(_TRACE_ENV_VAR, str(trace_file))
    emit_audit_trace(_basic_outcome())
    mode = stat.S_IMODE(trace_file.stat().st_mode)
    assert mode == 0o600


def test_trace_fails_open_silently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Point the trace file at an unwritable path (a directory).
    monkeypatch.setenv(_TRACE_ENV_VAR, str(tmp_path))
    # Must not raise.
    emit_audit_trace(_basic_outcome())


def test_trace_error_code_categorical(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv(_TRACE_ENV_VAR, str(trace_file))
    emit_audit_trace(
        _basic_outcome(
            recall_succeeded=False,
            retrieved=[],
            injected=[],
            error_code="remote_failure",
        )
    )
    rec = _read_trace_lines(trace_file)[0]
    assert rec["recall_succeeded"] is False
    assert rec["error_code"] == "remote_failure"
    assert rec["retrieved_item_count"] == 0
    assert rec["injected_item_count"] == 0
    raw_text = trace_file.read_text(encoding="utf-8")
    # Must not contain a stack trace or raw exception text.
    assert "Traceback" not in raw_text
    assert "Exception" not in raw_text


def test_trace_empty_env_var_is_noop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv(_TRACE_ENV_VAR, "")
    emit_audit_trace(_basic_outcome())
    assert not trace_file.exists()


def test_trace_profile_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv(_TRACE_ENV_VAR, str(trace_file))
    outcome = _basic_outcome()
    outcome["profile"] = "engram-test-profile"
    emit_audit_trace(outcome)
    rec = _read_trace_lines(trace_file)[0]
    assert rec["profile"] == "engram-test-profile"


def test_trace_profile_none_when_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv(_TRACE_ENV_VAR, str(trace_file))
    emit_audit_trace(_basic_outcome())
    rec = _read_trace_lines(trace_file)[0]
    assert rec["profile"] is None


def test_trace_preserves_existing_file_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Appending to an existing file must not relax its mode."""
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv(_TRACE_ENV_VAR, str(trace_file))
    # Pre-create with a restrictive mode.
    trace_file.touch(mode=0o600)
    os.chmod(trace_file, 0o600)
    emit_audit_trace(_basic_outcome())
    mode = stat.S_IMODE(trace_file.stat().st_mode)
    assert mode == 0o600


def test_trace_works_in_tempdir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        trace_file = Path(tmp) / "trace.jsonl"
        monkeypatch.setenv(_TRACE_ENV_VAR, str(trace_file))
        emit_audit_trace(_basic_outcome())
        assert trace_file.exists()
        records = _read_trace_lines(trace_file)
        assert len(records) == 1
