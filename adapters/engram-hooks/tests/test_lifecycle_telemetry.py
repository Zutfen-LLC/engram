"""Tests for engram-hooks lifecycle-summary telemetry reporting (ENG-METER-001).

Covers: the additive `report_lifecycle_telemetry` config flag (default
False), one aggregate report per lifecycle invocation when enabled, no
candidate text in the report, and reporting failure never changing the
returned HookResult.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from engram_hooks.config import HooksConfig
from engram_hooks.hooks import LifecycleHooks


class _Client:
    def __init__(self, *, classify_disposition: str = "retain", classify_confidence: float = 0.9):
        self.classify_calls: list[dict[str, Any]] = []
        self.remember_calls: list[dict[str, Any]] = []
        self.lifecycle_summary_calls: list[dict[str, Any]] = []
        self._disposition = classify_disposition
        self._confidence = classify_confidence

    async def classify(self, content: str, **kwargs: Any) -> SimpleNamespace:
        self.classify_calls.append({"content": content, **kwargs})
        return SimpleNamespace(
            classification_run_id="run-id",
            ingest_id="ingest-id",
            suggested_kind="fact",
            suggested_wing=None,
            suggested_room=None,
            taxonomy_confidence=0.5,
            retention_confidence=self._confidence,
            retention_disposition=self._disposition,
        )

    async def remember(self, content: str, **kwargs: Any) -> SimpleNamespace:
        self.remember_calls.append({"content": content, **kwargs})
        return SimpleNamespace(status="created", id="item-id")

    async def report_lifecycle_summary(self, **kwargs: Any) -> SimpleNamespace:
        self.lifecycle_summary_calls.append(kwargs)
        return SimpleNamespace(status="succeeded", invocation_id=kwargs["invocation_id"])


def _hooks(tmp_path: Any, client: _Client, *, report_telemetry: bool) -> LifecycleHooks:
    hooks = LifecycleHooks(
        HooksConfig(
            base_url="http://test",
            volatile_path=str(tmp_path / "volatile.jsonl"),
            store_confidence_threshold=0.5,
            report_lifecycle_telemetry=report_telemetry,
        )
    )
    hooks._client = client
    return hooks


def test_default_flag_is_false() -> None:
    assert HooksConfig().report_lifecycle_telemetry is False


def test_env_var_enables_flag(monkeypatch: Any) -> None:
    monkeypatch.setenv("ENGRAM_HOOKS_REPORT_LIFECYCLE_TELEMETRY", "true")
    assert HooksConfig().report_lifecycle_telemetry is True


async def test_disabled_by_default_reports_nothing(tmp_path: Any) -> None:
    client = _Client()
    hooks = _hooks(tmp_path, client, report_telemetry=False)
    result = await hooks.run_hook("sync_turn", "We decided to use Postgres.")
    assert result.promoted == 1
    assert client.lifecycle_summary_calls == []


async def test_enabled_reports_one_aggregate_event_per_invocation(tmp_path: Any) -> None:
    client = _Client()
    hooks = _hooks(tmp_path, client, report_telemetry=True)
    result = await hooks.run_hook(
        "sync_turn", "We decided to use Postgres. Also, the cache expires in 5 minutes."
    )
    assert len(client.lifecycle_summary_calls) == 1
    summary = client.lifecycle_summary_calls[0]
    assert summary["event"] == "sync_turn"
    assert summary["extracted"] == result.extracted
    assert summary["promoted"] == result.promoted
    assert summary["parked"] == result.parked
    assert summary["errors"] == result.errors
    assert summary["candidate_bytes"] > 0
    assert isinstance(summary["invocation_id"], type(summary["invocation_id"]))


async def test_report_never_includes_candidate_text(tmp_path: Any) -> None:
    client = _Client()
    hooks = _hooks(tmp_path, client, report_telemetry=True)
    secret_content = "the secret launch codes are 12345"
    await hooks.run_hook("sync_turn", secret_content)
    assert len(client.lifecycle_summary_calls) == 1
    summary = client.lifecycle_summary_calls[0]
    serialized = repr(summary)
    assert secret_content not in serialized
    assert "content" not in summary
    assert "candidates" not in summary


async def test_reporting_failure_does_not_change_hook_result(tmp_path: Any) -> None:
    class _FailingClient(_Client):
        async def report_lifecycle_summary(self, **kwargs: Any) -> SimpleNamespace:
            raise RuntimeError("telemetry endpoint unreachable")

    client = _FailingClient()
    hooks = _hooks(tmp_path, client, report_telemetry=True)
    result = await hooks.run_hook("sync_turn", "We decided to use Postgres.")
    # The reporting failure must be swallowed — HookResult reflects only the
    # actual routing outcome, unaffected by the telemetry call blowing up.
    assert result.extracted == 1
    assert result.promoted == 1
    assert result.errors == 0


async def test_guard_rejected_candidates_are_counted_in_summary(tmp_path: Any) -> None:
    client = _Client()
    hooks = _hooks(tmp_path, client, report_telemetry=True)
    # A very short candidate is rejected by the write-boundary guard before
    # classify ever runs.
    await hooks.run_hook("sync_turn", "no")
    assert len(client.lifecycle_summary_calls) == 1
    summary = client.lifecycle_summary_calls[0]
    assert summary["guard_rejected"] >= 1
    assert client.classify_calls == []


async def test_no_client_configured_skips_reporting_without_error(tmp_path: Any) -> None:
    hooks = LifecycleHooks(
        HooksConfig(
            base_url="",
            volatile_path=str(tmp_path / "volatile.jsonl"),
            store_confidence_threshold=0.5,
            report_lifecycle_telemetry=True,
        )
    )
    result = await hooks.run_hook("sync_turn", "We decided to use Postgres.")
    assert result.parked == 1
