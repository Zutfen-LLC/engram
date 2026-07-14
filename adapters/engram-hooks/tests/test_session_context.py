"""Tests for per-session context accumulation in the lifecycle hooks.

These prove that the ``context`` parameter passed to ``client.classify()``
carries accumulated conversation history (not the source_type string), that
rejected candidates don't pollute the context, and that ``reset_session_context``
clears the accumulator between sessions.

Uses the same RecordingClient pattern as test_session_end.py — no network.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from engram_hooks.config import HooksConfig
from engram_hooks.hooks import LifecycleHooks, _SessionContext

# ---------------------------------------------------------------------------
# RecordingClient — captures classify() calls so we can assert on the context
# parameter that was actually sent.
# ---------------------------------------------------------------------------


class ContextRecordingClient:
    """Mock SDK client that records every classify() call's context argument."""

    def __init__(self, confidence: float = 0.9) -> None:
        self.classify_calls: list[dict[str, Any]] = []
        self.remember_calls: list[dict[str, Any]] = []
        self._confidence = confidence

    async def classify(
        self, content: str, *, context: str | None = None, **kw: Any
    ) -> SimpleNamespace:
        self.classify_calls.append({"content": content, "context": context, **kw})
        return SimpleNamespace(
            taxonomy_confidence=self._confidence,
            retention_confidence=self._confidence,
            retention_disposition="retain",
            classification_run_id="run-id",
            suggested_kind="fact",
            suggested_wing=None,
            suggested_room=None,
        )

    async def remember(self, content: str, **kw: Any) -> SimpleNamespace:
        self.remember_calls.append({"content": content, **kw})
        return SimpleNamespace(status="created", id="fake-id", review_status="proposed")

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Unit tests: _SessionContext
# ---------------------------------------------------------------------------


class TestSessionContextUnit:
    """Pure unit tests for the _SessionContext accumulator."""

    def test_empty_context_returns_empty_string(self) -> None:
        ctx = _SessionContext()
        assert ctx.context_string() == ""

    def test_add_single_entry(self) -> None:
        ctx = _SessionContext()
        ctx.add("We decided to use PostgreSQL")
        result = ctx.context_string()
        assert "- We decided to use PostgreSQL" in result

    def test_multiple_entries_accumulate(self) -> None:
        ctx = _SessionContext()
        ctx.add("First fact")
        ctx.add("Second fact")
        ctx.add("Third fact")
        result = ctx.context_string()
        assert "- First fact" in result
        assert "- Second fact" in result
        assert "- Third fact" in result

    def test_max_entries_evicts_oldest(self) -> None:
        ctx = _SessionContext(max_entries=3)
        ctx.add("entry-1")
        ctx.add("entry-2")
        ctx.add("entry-3")
        ctx.add("entry-4")
        result = ctx.context_string()
        assert "entry-1" not in result
        assert "entry-2" in result
        assert "entry-3" in result
        assert "entry-4" in result

    def test_max_chars_evicts_oldest(self) -> None:
        ctx = _SessionContext(max_entries=100, max_chars=50)
        ctx.add("A" * 30)  # 30 chars
        ctx.add("B" * 30)  # total 60+ — exceeds 50, A should be evicted
        result = ctx.context_string()
        assert "AAAA" not in result
        assert "BBBB" in result

    def test_reset_clears_all(self) -> None:
        ctx = _SessionContext()
        ctx.add("persisted fact")
        ctx.add("another fact")
        assert ctx.context_string() != ""
        ctx.reset()
        assert ctx.context_string() == ""

    def test_thread_safe(self) -> None:
        """Concurrent adds don't corrupt the internal list."""
        import threading

        ctx = _SessionContext(max_entries=200, max_chars=10000)
        errors: list[Exception] = []

        def writer(start: int) -> None:
            try:
                for i in range(50):
                    ctx.add(f"thread-{start}-entry-{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # context_string should be callable without error
        result = ctx.context_string()
        assert len(result) > 0


# ---------------------------------------------------------------------------
# Integration tests: context flows through the routing pipeline
# ---------------------------------------------------------------------------


def _make_hooks(tmp_path, confidence: float = 0.9) -> tuple[LifecycleHooks, ContextRecordingClient]:
    """Build a LifecycleHooks with a recording client injected."""
    hooks = LifecycleHooks(
        HooksConfig(
            base_url="http://engram.test",
            volatile_path=str(tmp_path / "volatile.jsonl"),
        )
    )
    recorder = ContextRecordingClient(confidence=confidence)
    hooks._client = recorder
    return hooks, recorder


class TestContextAccumulationInRouting:
    """Prove that the context parameter carries conversation history."""

    async def test_first_candidate_has_no_context(self, tmp_path) -> None:
        """The very first classify call should have context=None."""
        hooks, recorder = _make_hooks(tmp_path)
        await hooks.run_hook("sync_turn", "We are migrating the database to pgvector")

        assert len(recorder.classify_calls) == 1
        assert recorder.classify_calls[0]["context"] is None

    async def test_second_candidate_has_first_as_context(self, tmp_path) -> None:
        """After the first candidate routes, the second should see it in context."""
        hooks, recorder = _make_hooks(tmp_path)

        await hooks.run_hook("sync_turn", "We are migrating the database to pgvector")
        await hooks.run_hook("sync_turn", "We chose pgvector over Milvus for simplicity")

        assert len(recorder.classify_calls) == 2
        # Second call should have the first candidate in context.
        ctx = recorder.classify_calls[1]["context"]
        assert ctx is not None
        assert "migrating the database" in ctx

    async def test_context_grows_across_three_turns(self, tmp_path) -> None:
        """Context should accumulate across multiple turns."""
        hooks, recorder = _make_hooks(tmp_path)

        await hooks.run_hook("sync_turn", "We are migrating the database")
        await hooks.run_hook("sync_turn", "We chose pgvector over Milvus")
        await hooks.run_hook("sync_turn", "The migration is complete and tests pass")

        assert len(recorder.classify_calls) == 3
        # Third call should have both prior candidates in context.
        ctx = recorder.classify_calls[2]["context"]
        assert ctx is not None
        assert "migrating the database" in ctx
        assert "chose pgvector" in ctx

    async def test_context_is_not_source_type_string(self, tmp_path) -> None:
        """The context should be conversation history, NOT 'sync_turn' or 'session_end'."""
        hooks, recorder = _make_hooks(tmp_path)

        await hooks.run_hook("sync_turn", "First durable fact in this session")
        await hooks.run_hook("sync_turn", "Second durable fact that builds on the first")

        ctx = recorder.classify_calls[1]["context"]
        # The old bug was context="sync_turn" — that should never happen now.
        assert ctx != "sync_turn"
        assert ctx != "session_end"
        assert ctx != "pre_compress"
        assert "First durable fact" in ctx

    async def test_rejected_candidates_do_not_pollute_context(self, tmp_path) -> None:
        """Ephemeral/ambiguous candidates rejected by the guard should NOT enter context."""
        hooks, recorder = _make_hooks(tmp_path)

        # This should be rejected by the guard (ambiguous — starts with "let me")
        await hooks.run_hook("sync_turn", "Let me check that for you")
        # This should pass the guard and be promoted
        await hooks.run_hook("sync_turn", "The API uses bearer token authentication")

        assert len(recorder.classify_calls) == 1  # only the second candidate reached classify
        ctx = recorder.classify_calls[0]["context"]
        # The rejected candidate should NOT be in the context.
        assert ctx is None or "Let me check" not in ctx

    async def test_context_carries_correct_facts_not_noise(self, tmp_path) -> None:
        """Multiple facts in one payload should all enter the context."""
        hooks, recorder = _make_hooks(tmp_path)

        # Multi-sentence payload → multiple candidates
        await hooks.run_hook(
            "sync_turn",
            "The deployment uses Docker Compose. We use nginx as reverse proxy. "
            "SSL certificates are managed by certbot.",
        )
        # All three sentences should be in the context for the next turn.
        await hooks.run_hook("sync_turn", "The reverse proxy terminates TLS on port 443")

        ctx = recorder.classify_calls[-1]["context"]
        assert ctx is not None
        assert "Docker Compose" in ctx
        assert "nginx" in ctx
        assert "certbot" in ctx


class TestContextReset:
    """Prove that reset_session_context clears the accumulator."""

    async def test_reset_clears_context_between_sessions(self, tmp_path) -> None:
        """After reset, the next classify call should have context=None."""
        hooks, recorder = _make_hooks(tmp_path)

        # Session 1: accumulate some context.
        await hooks.run_hook("sync_turn", "Session one fact about infrastructure")
        assert hooks._session_context.context_string() != ""

        # New session: reset.
        hooks.reset_session_context()
        assert hooks._session_context.context_string() == ""

        # The next candidate should have no context.
        await hooks.run_hook("sync_turn", "Session two fact about something else")
        ctx = recorder.classify_calls[-1]["context"]
        assert ctx is None

    async def test_reset_prevents_cross_session_bleed(self, tmp_path) -> None:
        """Facts from session 1 should NOT appear in session 2's context."""
        hooks, recorder = _make_hooks(tmp_path)

        await hooks.run_hook("sync_turn", "PostgreSQL runs on port 5432 in production")
        hooks.reset_session_context()
        await hooks.run_hook("sync_turn", "The frontend uses React with TypeScript")

        ctx = recorder.classify_calls[-1]["context"]
        assert ctx is None
        assert "PostgreSQL" not in (ctx or "")


class TestContextWithMultipleCandidates:
    """Context accumulation within a single run_hook call (multi-sentence payload)."""

    async def test_multi_sentence_first_call_no_context(self, tmp_path) -> None:
        """Even with multiple candidates, the first one should have no context.

        Each candidate within a single run_hook call is routed sequentially,
        so the second candidate should see the first in context.
        """
        hooks, recorder = _make_hooks(tmp_path)

        payload = "First durable fact about Redis. Second durable fact about caching."
        await hooks.run_hook("sync_turn", payload)

        assert len(recorder.classify_calls) == 2
        # First candidate: no context.
        assert recorder.classify_calls[0]["context"] is None
        # Second candidate: first should be in context.
        ctx = recorder.classify_calls[1]["context"]
        assert ctx is not None
        assert "First durable fact" in ctx

    async def test_context_does_not_grow_unbounded(self, tmp_path) -> None:
        """After many turns, the context stays within the char/entry budget."""
        hooks, recorder = _make_hooks(tmp_path)

        # Route 30 turns — exceeds the default 10-entry / 800-char window.
        for i in range(30):
            await hooks.run_hook("sync_turn", f"Durable fact number {i} about topic {i}")

        # At the point candidate #29 was classified, candidates #0-#18 should
        # have been evicted (they were added to context before #29). #28 should
        # be present because it was routed (and added to context) before #29.
        ctx = recorder.classify_calls[29]["context"]
        assert ctx is not None
        # Early facts should have been evicted.
        assert "fact number 0" not in ctx
        assert "fact number 5" not in ctx
        # The immediately preceding fact should be present.
        assert "fact number 28" in ctx
        # Context should be within the char budget (with the "- " prefix per line).
        assert len(ctx) <= 1200  # generous upper bound for 10 entries × ~100 chars
