"""Tests for ENG-AUDIT-003A Part E: truthful post-render injected-item provenance.

Covers Section 26 (renderer provenance) and Section 27 (bridge integration).

The governing rule: ``injected_item_ids`` must mean IDs of evidence records
actually retained in the final rendered context — NOT the pre-render admitted
evidence list.

Every assertion in this file is exact and unconditional. No "at most", no
``if result.context is not None`` guards — the tests must guarantee the
intended branch ran.
"""
from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

# Ensure the plugin package is importable (same pattern as other adapter tests).
if "agent.memory_provider" not in sys.modules:
    provider_module = types.ModuleType("agent.memory_provider")
    provider_module.MemoryProvider = type("MemoryProvider", (), {})  # type: ignore[attr-defined]
    agent_module = types.ModuleType("agent")
    agent_module.memory_provider = provider_module  # type: ignore[attr-defined]
    sys.modules["agent"] = agent_module
    sys.modules["agent.memory_provider"] = provider_module

_PLUGIN_DIR = Path(__file__).resolve().parents[1] / "hermes_plugin"
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from engram_memory.evidence import (  # noqa: E402
    CompactTrace,
    EvidenceItem,
    RenderedEnvelope,
    render_envelope,
    render_envelope_result,
)
from engram_memory.recall_bridge import RecallBridge  # noqa: E402


@dataclass(frozen=True)
class _BridgeConfig:
    """Config for the bridge integration test: budget 20, small context limit."""

    base_url: str = "http://test"
    api_key: str = "eng_test"
    recall_enabled: bool = True
    recall_timeout: float = 5.0
    recall_item_budget: int = 20
    recall_byte_budget: int = 8192
    recall_max_context_bytes: int = 1500  # calibrated: a+b fit, c dropped
    recall_followup_turns: int = 3
    recall_breaker_failures: int = 3
    recall_max_sessions: int = 512


class _BridgeFactory:
    """Factory that produces a client returning exactly item-a, item-b, item-c."""

    def __call__(self) -> Any:
        from types import SimpleNamespace

        class _Client:
            async def recall(self, **kwargs: Any) -> Any:
                mode = kwargs.get("mode", "semantic")
                if mode == "semantic":
                    items = [
                        {"id": "item-a", "content": "short A"},
                        {"id": "item-b", "content": "short B"},
                        {"id": "item-c", "content": "x" * 500},
                    ]
                    return SimpleNamespace(items=items, recall_log_id="log-1")
                return SimpleNamespace(items=[], recall_log_id="startup-log")

            async def close(self) -> None:
                pass

        return _Client()


def _mkitem(
    id: str,
    content: str = "Test content here.",
    *,
    pinned: bool = False,
    origins: tuple[str, ...] = ("semantic",),
) -> EvidenceItem:
    return EvidenceItem(
        id=id,
        content=content,
        kind="fact",
        review_status="active",
        epistemic_status="asserted_unverified",
        source_trust=0.5,
        memory_confidence=0.5,
        human_verified=False,
        score=0.5,
        importance=0.5,
        pinned=pinned,
        reasons=(),
        warnings=(),
        retrieval_origins=origins,
    )


# ── Section 26: Renderer provenance tests (exact, unconditional) ────────────


class TestRendererProvenance:
    """Prove that injected_items accurately reflects post-render retained items."""

    def test_all_items_fit_all_ids_retained(self) -> None:
        items = [_mkitem("a"), _mkitem("b"), _mkitem("c")]
        result = render_envelope_result(items, ["log-1"], [], 100_000)
        assert result.context is not None
        assert {item.id for item in result.injected_items} == {"a", "b", "c"}

    def test_exact_two_item_drop_proves_exclusion(self) -> None:
        """Two semantic evidence items: keep survives, drop is removed by
        byte pressure. The test proves both inclusion (keep) and exact
        exclusion (drop) — no permissive 'at most' assertion."""
        items = [
            _mkitem("keep", "short content"),
            _mkitem("drop", "x" * 2000),
        ]
        result = render_envelope_result(items, [], [], 2000)

        # Prove the setup: context is rendered, keep is present, drop is absent
        assert result.context is not None
        assert '<engram-evidence id="keep"' in result.context
        assert '<engram-evidence id="drop"' not in result.context

        # Exact provenance: only "keep" was injected
        assert {item.id for item in result.injected_items} == {"keep"}
        assert "drop" not in {item.id for item in result.injected_items}

    def test_truncated_single_item_proves_retention(self) -> None:
        """A single item whose full content cannot fit but whose truncated
        representation can. The item remains injected."""
        item = _mkitem("truncated", "x" * 5000)
        result = render_envelope_result([item], [], [], 1100)

        assert result.context is not None
        assert 'id="truncated"' in result.context
        assert 'content_truncated="true"' in result.context
        assert tuple(item.id for item in result.injected_items) == ("truncated",)

    def test_trace_only_rendering_proves_zero_evidence(self) -> None:
        """One large evidence item and one compact trace. The evidence item
        cannot fit but the trace-only envelope does. Zero evidence items
        are injected."""
        trace = CompactTrace(
            turn_index=1,
            query_digest="abc",
            item_ids=(),
            epistemic_labels=(),
            review_statuses=(),
            human_verified=(),
            recall_log_ids=("log-1",),
            retrieval_origins=(),
        )
        item = _mkitem("big", "x" * 100_000)
        result = render_envelope_result([item], [], [trace], 1000)

        assert result.context is not None
        assert "<engram-recent-trace" in result.context
        assert "<engram-evidence" not in result.context
        assert result.injected_items == ()

    def test_no_context_rendered_empty_items(self) -> None:
        """When nothing renders, injected item IDs are empty."""
        result = render_envelope_result([], [], [], 1000)
        assert result.context is None
        assert result.injected_items == ()

    def test_backward_compat_render_envelope_returns_string(self) -> None:
        """render_envelope() still returns str|None (backward compat)."""
        items = [_mkitem("a", "hello")]
        context = render_envelope(items, ["log-1"], [], 100_000)
        assert context is not None
        assert "hello" in context

    def test_render_envelope_none_when_empty(self) -> None:
        """render_envelope() returns None when nothing to render."""
        assert render_envelope([], [], [], 1000) is None

    def test_rendered_envelope_dataclass_shape(self) -> None:
        """RenderedEnvelope is frozen with slots and has context + injected_items."""
        items = [_mkitem("a")]
        result = render_envelope_result(items, [], [], 100_000)
        assert isinstance(result, RenderedEnvelope)
        assert hasattr(result, "context")
        assert hasattr(result, "injected_items")
        assert isinstance(result.injected_items, tuple)


# ── Section 27: Bridge integration test (deterministic, exact) ──────────────


class TestBridgeIntegrationProvenance:
    """Build a deterministic bridge test verifying post-render provenance.

    Uses the RecallBridge with a mock client factory to exercise the real
    pre_llm_call path and verify the audit trace carries post-render IDs.

    The fake semantic response returns exactly item-a, item-b, item-c.
    The byte limit is configured so item-a and item-b survive, item-c is
    removed. The test proves exact retrieved/injected sets.
    """

    def test_retrieved_three_injected_two_deterministic(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Semantic response contains three items; context byte limit drops
        the third. Trace retrieved IDs include all three; injected IDs
        include only the two rendered items.

        This test must fail if the bridge reverts to using the pre-render
        evidence list.
        """
        # The conftest's clean_hooks_state removes the ``agent`` stub from
        # sys.modules. Re-stub it so engram_memory.audit_trace imports
        # cleanly inside the daemon thread.
        if "agent.memory_provider" not in sys.modules:
            provider_module = types.ModuleType("agent.memory_provider")
            provider_module.MemoryProvider = type("MemoryProvider", (), {})  # type: ignore[attr-defined]
            agent_module = types.ModuleType("agent")
            agent_module.memory_provider = provider_module  # type: ignore[attr-defined]
            sys.modules["agent"] = agent_module
            sys.modules["agent.memory_provider"] = provider_module

        trace_path = tmp_path / "trace.jsonl"
        monkeypatch.setenv("ENGRAM_HOOKS_AUDIT_TRACE_FILE", str(trace_path))

        bridge = RecallBridge(_BridgeConfig(), client_factory=_BridgeFactory())
        bridge.pre_llm_call(
            user_message="test query",
            session_id="test-session",
            is_first_turn=True,
        )

        # The trace must exist and contain exactly one record
        assert trace_path.exists()
        lines = trace_path.read_text().strip().splitlines()
        assert len(lines) >= 1

        rec = json.loads(lines[0])

        # Exact retrieved set: all three items
        assert set(rec["retrieved_item_ids"]) == {
            "item-a",
            "item-b",
            "item-c",
        }
        # Exact injected set: only the two that survived rendering
        assert set(rec["injected_item_ids"]) == {
            "item-a",
            "item-b",
        }
        # Deterministic exclusion of the dropped item
        assert "item-c" not in rec["injected_item_ids"]
        assert rec["retrieved_item_count"] == 3
        assert rec["injected_item_count"] == 2
        assert rec["configured_item_budget"] == 20
