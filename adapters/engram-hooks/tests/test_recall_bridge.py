"""Stock-Hermes same-turn recall orchestration tests."""
from __future__ import annotations

import asyncio
import sys
import time
import types
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

from engram_memory.recall_bridge import RecallBridge  # noqa: E402


@dataclass
class _Config:
    base_url: str = "https://engram.example"
    api_key: str = "eng_test"
    recall_enabled: bool = True
    recall_timeout: float = 0.08
    recall_item_budget: int = 5
    recall_byte_budget: int = 8192
    recall_max_context_bytes: int = 12000
    recall_followup_turns: int = 3
    recall_breaker_failures: int = 3
    recall_max_sessions: int = 512


def _response(
    mode: str,
    *,
    content: str | None = None,
    item_id: str | None = None,
) -> Any:
    items = []
    if content is not None:
        items.append(
            {
                "id": item_id or f"{mode}-item",
                "content": content,
                "kind": "observation",
                "review_status": "active",
                "source_trust": 0.8,
                "memory_confidence": 0.7,
                "human_verified": False,
                "score": 0.75 if mode == "semantic" else None,
                "importance": 0.5,
                "pinned": False,
                "reasons": [f"{mode} reason"],
                "warnings": [],
            }
        )
    return SimpleNamespace(items=items, recall_log_id=f"{mode}-log")


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


async def _normal(**kwargs: Any) -> Any:
    mode = kwargs["mode"]
    content = "startup context" if mode == "startup" else f"answer for {kwargs['query']}"
    return _response(mode, content=content)


def test_current_query_and_first_turn_share_one_client() -> None:
    factory = _Factory(_normal)
    bridge = RecallBridge(_Config(), client_factory=factory)
    result = bridge.pre_llm_call(
        user_message="question N", session_id="session-a", is_first_turn=True
    )
    assert result is not None
    assert factory.instances == 1
    assert factory.closes == 1
    assert {call["mode"] for call in factory.calls} == {"startup", "semantic"}
    semantic = next(call for call in factory.calls if call["mode"] == "semantic")
    assert semantic == {
        "mode": "semantic",
        "query": "question N",
        "item_budget": 5,
        "byte_budget": 8192,
    }
    startup = next(call for call in factory.calls if call["mode"] == "startup")
    assert startup == {"mode": "startup", "byte_budget": 8192}
    assert "startup context" in result["context"]
    assert "answer for question N" in result["context"]


def test_later_turn_uses_current_query_and_does_not_repeat_startup() -> None:
    factory = _Factory(_normal)
    bridge = RecallBridge(_Config(), client_factory=factory)
    bridge.pre_llm_call(user_message="question A", session_id="s", is_first_turn=True)
    second = bridge.pre_llm_call(user_message="question B", session_id="s", is_first_turn=False)
    semantic_queries = [call["query"] for call in factory.calls if call["mode"] == "semantic"]
    assert semantic_queries == ["question A", "question B"]
    assert [call["mode"] for call in factory.calls].count("startup") == 1
    assert second is not None
    assert "answer for question A" not in second["context"]


def test_empty_message_and_empty_session_fail_closed_without_client() -> None:
    factory = _Factory(_normal)
    bridge = RecallBridge(_Config(), client_factory=factory)
    assert bridge.pre_llm_call(user_message="", session_id="s") is None
    assert bridge.pre_llm_call(user_message="question", session_id="") is None
    assert factory.calls == []


def test_valid_empty_semantic_is_success_and_resets_failures() -> None:
    async def empty(**kwargs: Any) -> Any:
        return _response(kwargs["mode"])

    factory = _Factory(empty)
    bridge = RecallBridge(_Config(), client_factory=factory)
    assert bridge.pre_llm_call(user_message="q", session_id="s") is None
    assert bridge._sessions["s"].consecutive_failures == 0
    assert bridge._sessions["s"].breaker_open is False


def test_success_resets_a_prior_semantic_failure() -> None:
    attempts = 0

    async def recover(**kwargs: Any) -> Any:
        nonlocal attempts
        if kwargs["mode"] == "startup":
            return _response("startup")
        attempts += 1
        if attempts == 1:
            raise OSError("failed")
        return _response("semantic")

    bridge = RecallBridge(_Config(), client_factory=_Factory(recover))
    bridge.pre_llm_call(user_message="first", session_id="s")
    assert bridge._sessions["s"].consecutive_failures == 1
    bridge.pre_llm_call(user_message="second", session_id="s")
    assert bridge._sessions["s"].consecutive_failures == 0


def test_partial_first_turn_uses_semantic_when_startup_times_out() -> None:
    async def partial(**kwargs: Any) -> Any:
        if kwargs["mode"] == "startup":
            await asyncio.sleep(10)
        return _response("semantic", content="semantic survived")

    config = _Config(recall_timeout=0.04)
    factory = _Factory(partial)
    bridge = RecallBridge(config, client_factory=factory)
    started = time.monotonic()
    result = bridge.pre_llm_call(user_message="q", session_id="s", is_first_turn=True)
    assert time.monotonic() - started < 0.2
    assert result is not None
    assert "semantic survived" in result["context"]
    assert bridge._sessions["s"].startup_loaded is False


def test_timeout_opens_per_session_breaker_and_suppresses_network() -> None:
    async def failing(**kwargs: Any) -> Any:
        if kwargs["mode"] == "startup":
            return _response("startup", content="safe startup")
        raise OSError("transport")

    factory = _Factory(failing)
    bridge = RecallBridge(_Config(recall_breaker_failures=3), client_factory=factory)
    for turn in range(3):
        bridge.pre_llm_call(user_message=f"q{turn}", session_id="bad")
    calls_before = len(factory.calls)
    fallback = bridge.pre_llm_call(user_message="q4", session_id="bad")
    assert len(factory.calls) == calls_before
    assert bridge._sessions["bad"].breaker_open is True
    assert fallback is not None and "safe startup" in fallback["context"]

    healthy = bridge.pre_llm_call(user_message="q", session_id="other")
    assert healthy is not None
    assert len(factory.calls) > calls_before

    bridge.on_session_reset(old_session_id="bad", new_session_id="bad-new")
    assert "bad" not in bridge._sessions
    assert bridge.pre_llm_call(user_message="after reset", session_id="bad-new") is not None


def test_malformed_response_fails_closed_and_logs_no_content(caplog) -> None:
    secret_query = "question with private raw text"

    async def malformed(**kwargs: Any) -> Any:
        del kwargs
        return SimpleNamespace(items=None, recall_log_id="log")

    bridge = RecallBridge(_Config(), client_factory=_Factory(malformed))
    assert bridge.pre_llm_call(user_message=secret_query, session_id="s") is None
    assert secret_query not in caplog.text
    assert "The sky is purple" not in caplog.text


def test_reset_clears_only_old_and_new_sessions_and_refetches_startup() -> None:
    factory = _Factory(_normal)
    bridge = RecallBridge(_Config(), client_factory=factory)
    bridge.pre_llm_call(user_message="a", session_id="old")
    bridge.pre_llm_call(user_message="b", session_id="other")
    bridge.on_session_reset(
        session_id="new", old_session_id="old", new_session_id="new", reason="new_session"
    )
    assert "old" not in bridge._sessions
    assert "other" in bridge._sessions
    bridge.pre_llm_call(user_message="c", session_id="new")
    startup_calls = [call for call in factory.calls if call["mode"] == "startup"]
    assert len(startup_calls) == 3


def test_finalize_deletes_only_target_session() -> None:
    factory = _Factory(_normal)
    bridge = RecallBridge(_Config(), client_factory=factory)
    bridge.on_session_start("a", model="x")
    bridge.on_session_start("b", model="x")
    bridge.on_session_finalize("a", platform="cli")
    assert set(bridge._sessions) == {"b"}


@pytest.mark.asyncio
async def test_sync_hook_is_safe_inside_running_event_loop() -> None:
    factory = _Factory(_normal)
    bridge = RecallBridge(_Config(), client_factory=factory)
    result = bridge.pre_llm_call(user_message="inside loop", session_id="s")
    assert result is not None
    assert "answer for inside loop" in result["context"]


@pytest.mark.asyncio
async def test_suspected_stuck_worker_does_not_create_more_threads() -> None:
    async def stuck(**kwargs: Any) -> Any:
        del kwargs
        time.sleep(2)
        return _response("semantic", content="too late")

    config = _Config(recall_timeout=0.03)
    factory = _Factory(stuck)
    bridge = RecallBridge(config, client_factory=factory)
    started = time.monotonic()
    assert bridge.pre_llm_call(user_message="one", session_id="s") is None
    assert time.monotonic() - started < 0.15
    assert bridge.pre_llm_call(user_message="two", session_id="s") is None
    assert factory.instances == 1


def test_followup_trace_contains_ids_not_content_and_expires() -> None:
    config = _Config(recall_followup_turns=2)
    factory = _Factory(_normal)
    bridge = RecallBridge(config, client_factory=factory)
    first = bridge.pre_llm_call(user_message="first", session_id="s")
    assert first is not None
    second = bridge.pre_llm_call(user_message="second", session_id="s")
    assert second is not None
    assert "supplied item" in second["context"]
    assert "semantic-log" in second["context"]
    assert len(bridge._sessions["s"].recent_traces) == 1
    bridge.pre_llm_call(user_message="third", session_id="s")
    fourth = bridge.pre_llm_call(user_message="fourth", session_id="s")
    assert fourth is not None
    assert 'prior_turn="1"' not in fourth["context"]


def test_lru_session_cap_does_not_mix_evidence() -> None:
    factory = _Factory(_normal)
    bridge = RecallBridge(_Config(recall_max_sessions=2), client_factory=factory)
    bridge.pre_llm_call(user_message="one", session_id="one")
    bridge.pre_llm_call(user_message="two", session_id="two")
    bridge.pre_llm_call(user_message="three", session_id="three")
    assert len(bridge._sessions) == 2
    assert "one" not in bridge._sessions


def test_disabled_hooks_are_fast_noops() -> None:
    factory = _Factory(_normal)
    bridge = RecallBridge(_Config(recall_enabled=False), client_factory=factory)
    bridge.on_session_start("s")
    bridge.on_session_reset(session_id="s", old_session_id="old", new_session_id="new")
    bridge.on_session_finalize("s")
    assert bridge.pre_llm_call(user_message="q", session_id="s") is None
    assert factory.calls == []
    assert bridge._sessions == {}
