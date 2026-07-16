"""Bounded stock-Hermes ``pre_llm_call`` bridge for Engram recall."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import queue
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .evidence import CompactTrace, EvidenceItem, merge_evidence, normalize_items, render_envelope

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SessionRecallState:
    """All read state for one Hermes gateway session."""

    generation: int = 0
    turn_index: int = 0
    startup_loaded: bool = False
    startup_evidence: tuple[EvidenceItem, ...] = ()
    startup_recall_log_ids: tuple[str, ...] = ()
    recent_traces: deque[CompactTrace] = field(default_factory=deque)
    consecutive_failures: int = 0
    breaker_open: bool = False
    last_touched_monotonic: float = field(default_factory=time.monotonic)


@dataclass(frozen=True, slots=True)
class _RecallPart:
    completed: bool
    evidence: tuple[EvidenceItem, ...] = ()
    recall_log_id: str | None = None


class _FetchDisposition(StrEnum):
    SUCCESS = "success"
    REMOTE_FAILURE = "remote_failure"
    REMOTE_DEADLINE_EXCEEDED = "remote_deadline_exceeded"
    SAME_SESSION_IN_FLIGHT = "same_session_in_flight"
    LOCAL_CAPACITY_UNAVAILABLE = "local_capacity_unavailable"
    LOCAL_WORKER_START_FAILED = "local_worker_start_failed"
    BREAKER_OPEN = "breaker_open"
    STALE_GENERATION_DISCARDED = "stale_generation_discarded"


@dataclass(frozen=True, slots=True)
class _FetchOutcome:
    startup: _RecallPart
    semantic: _RecallPart
    disposition: _FetchDisposition
    semantic_attempted: bool
    semantic_completed: bool


@dataclass(slots=True)
class _DaemonProgress:
    """Thread-safe partial result snapshot for an outer join deadline."""

    startup: _RecallPart = field(default_factory=lambda: _RecallPart(False))
    semantic: _RecallPart = field(default_factory=lambda: _RecallPart(False))
    semantic_attempted: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def mark_semantic_attempted(self) -> None:
        with self.lock:
            self.semantic_attempted = True

    def record(self, name: str, part: _RecallPart) -> None:
        with self.lock:
            if name == "startup":
                self.startup = part
            else:
                self.semantic = part

    def snapshot(self, failure_disposition: _FetchDisposition) -> _FetchOutcome:
        with self.lock:
            disposition = (
                _FetchDisposition.SUCCESS if self.semantic.completed else failure_disposition
            )
            return _FetchOutcome(
                self.startup,
                self.semantic,
                disposition,
                self.semantic_attempted,
                self.semantic.completed,
            )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _field(response: Any, name: str) -> Any:
    if isinstance(response, Mapping):
        return response.get(name)
    return getattr(response, name, None)


class RecallBridge:
    """Own session-scoped read state for one general-plugin module load."""

    _THREAD_MARGIN_SECONDS = 0.05
    _MAX_DAEMON_WORKERS = 4

    def __init__(
        self,
        config: Any,
        *,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.config = config
        self._client_factory = client_factory or self._new_client
        self._sessions: dict[str, SessionRecallState] = {}
        self._active_sessions: dict[str, int] = {}
        self._lock = threading.RLock()
        self._worker_lock = threading.Lock()
        self._worker_sessions: set[str] = set()
        self._worker_slots = threading.BoundedSemaphore(self._MAX_DAEMON_WORKERS)
        logger.info(
            "Engram recall config: enabled=%s timeout=%.2fs item_budget=%d "
            "byte_budget=%d max_context_bytes=%d followup_turns=%d "
            "breaker_failures=%d max_sessions=%d",
            config.recall_enabled,
            config.recall_timeout,
            config.recall_item_budget,
            config.recall_byte_budget,
            config.recall_max_context_bytes,
            config.recall_followup_turns,
            config.recall_breaker_failures,
            config.recall_max_sessions,
        )

    def _new_client(self) -> Any:
        import engram_client

        return engram_client.EngramClient(
            self.config.base_url,
            self.config.api_key,
            timeout=self.config.recall_timeout,
        )

    def _evict_if_needed(self) -> None:
        while len(self._sessions) > self.config.recall_max_sessions:
            candidates = (
                (session_id, state)
                for session_id, state in self._sessions.items()
                if session_id not in self._active_sessions
            )
            oldest = min(
                candidates,
                key=lambda pair: (pair[1].last_touched_monotonic, pair[0]),
                default=None,
            )
            if oldest is None:
                return
            del self._sessions[oldest[0]]

    def _state(self, session_id: str) -> SessionRecallState:
        state = self._sessions.get(session_id)
        if state is None:
            state = SessionRecallState()
            self._sessions[session_id] = state
            self._evict_if_needed()
        state.last_touched_monotonic = time.monotonic()
        return state

    def on_session_start(self, session_id: str = "", **kwargs: Any) -> None:
        """Create clean state for this session without performing recall."""
        del kwargs
        if not self.config.recall_enabled or not session_id:
            return
        with self._lock:
            self._sessions[session_id] = SessionRecallState()
            self._evict_if_needed()

    def on_session_reset(
        self,
        session_id: str = "",
        old_session_id: str = "",
        new_session_id: str = "",
        **kwargs: Any,
    ) -> None:
        """Clear only the old/new session pair supplied by stock Hermes."""
        del kwargs
        if not self.config.recall_enabled:
            return
        target = new_session_id or session_id
        with self._lock:
            if old_session_id:
                self._sessions.pop(old_session_id, None)
            if target:
                self._sessions.pop(target, None)

    def on_session_finalize(self, session_id: str | None = "", **kwargs: Any) -> None:
        """Release dead session state from a long-lived gateway process."""
        del kwargs
        if not self.config.recall_enabled or not session_id:
            return
        with self._lock:
            self._sessions.pop(session_id, None)

    async def _recall_part(self, client: Any, mode: str, query: str) -> _RecallPart:
        if mode == "semantic":
            response = await client.recall(
                mode="semantic",
                query=query,
                item_budget=self.config.recall_item_budget,
                byte_budget=self.config.recall_byte_budget,
            )
        else:
            response = await client.recall(
                mode="startup",
                byte_budget=self.config.recall_byte_budget,
            )
        evidence = normalize_items(_field(response, "items"), mode)
        raw_log_id = _field(response, "recall_log_id")
        log_id = raw_log_id.strip()[:256] if isinstance(raw_log_id, str) else None
        return _RecallPart(completed=True, evidence=evidence, recall_log_id=log_id or None)

    async def _fetch_async(
        self,
        query: str,
        want_startup: bool,
        record_part: Callable[[str, _RecallPart], None],
    ) -> _FetchOutcome:
        client = self._client_factory()
        tasks: dict[str, asyncio.Task[_RecallPart]] = {
            "semantic": asyncio.create_task(self._recall_part(client, "semantic", query))
        }
        if want_startup:
            tasks["startup"] = asyncio.create_task(self._recall_part(client, "startup", query))
        parts = {"startup": _RecallPart(False), "semantic": _RecallPart(False)}
        semantic_pending = False
        try:
            done, pending = await asyncio.wait(
                set(tasks.values()), timeout=self.config.recall_timeout
            )
            semantic_pending = tasks["semantic"] in pending
            for task in pending:
                task.cancel()
            for name, task in tasks.items():
                if task not in done or task.cancelled():
                    continue
                try:
                    parts[name] = task.result()
                    record_part(name, parts[name])
                except Exception:  # noqa: BLE001 - retrieval fails closed
                    pass
        finally:
            with suppress(Exception):
                await client.close()
        semantic_completed = parts["semantic"].completed
        disposition = (
            _FetchDisposition.SUCCESS
            if semantic_completed
            else _FetchDisposition.REMOTE_DEADLINE_EXCEEDED
            if semantic_pending
            else _FetchDisposition.REMOTE_FAILURE
        )
        return _FetchOutcome(
            parts["startup"],
            parts["semantic"],
            disposition,
            semantic_attempted=True,
            semantic_completed=semantic_completed,
        )

    def _run_in_daemon(
        self, session_id: str, query: str, want_startup: bool
    ) -> _FetchOutcome:
        with self._worker_lock:
            if session_id in self._worker_sessions:
                return _FetchOutcome(
                    _RecallPart(False),
                    _RecallPart(False),
                    _FetchDisposition.SAME_SESSION_IN_FLIGHT,
                    semantic_attempted=False,
                    semantic_completed=False,
                )
            if not self._worker_slots.acquire(blocking=False):
                return _FetchOutcome(
                    _RecallPart(False),
                    _RecallPart(False),
                    _FetchDisposition.LOCAL_CAPACITY_UNAVAILABLE,
                    semantic_attempted=False,
                    semantic_completed=False,
                )
            self._worker_sessions.add(session_id)

        results: queue.Queue[_FetchOutcome] = queue.Queue(maxsize=1)
        progress = _DaemonProgress()

        def run() -> None:
            try:
                results.put_nowait(
                    asyncio.run(self._fetch_async(query, want_startup, progress.record))
                )
            except BaseException:  # daemon boundary must always release the gate
                pass
            finally:
                with self._worker_lock:
                    self._worker_sessions.discard(session_id)
                    self._worker_slots.release()

        worker = threading.Thread(
            target=run, name=f"engram-recall-{_digest(session_id)}", daemon=True
        )
        try:
            worker.start()
        except Exception:  # noqa: BLE001 - thread startup fails closed
            with self._worker_lock:
                self._worker_sessions.discard(session_id)
                self._worker_slots.release()
            return _FetchOutcome(
                _RecallPart(False),
                _RecallPart(False),
                _FetchDisposition.LOCAL_WORKER_START_FAILED,
                semantic_attempted=False,
                semantic_completed=False,
            )
        progress.mark_semantic_attempted()
        worker.join(self.config.recall_timeout + self._THREAD_MARGIN_SECONDS)
        if worker.is_alive():
            return progress.snapshot(_FetchDisposition.REMOTE_DEADLINE_EXCEEDED)
        try:
            return results.get_nowait()
        except queue.Empty:
            return progress.snapshot(_FetchDisposition.REMOTE_FAILURE)

    def _bounded_fetch(
        self, session_id: str, query: str, want_startup: bool
    ) -> _FetchOutcome:
        # Always use the one gated daemon worker. This gives both no-loop and
        # already-running-loop callers the same hard synchronous return bound,
        # including if cancellation or client shutdown itself becomes stuck.
        return self._run_in_daemon(session_id, query, want_startup)

    def _live_traces(self, state: SessionRecallState) -> tuple[CompactTrace, ...]:
        minimum_turn = state.turn_index - self.config.recall_followup_turns
        while state.recent_traces and state.recent_traces[0].turn_index < minimum_turn:
            state.recent_traces.popleft()
        return tuple(state.recent_traces)

    @staticmethod
    def _trace(
        turn_index: int,
        query_digest: str,
        items: tuple[EvidenceItem, ...],
        log_ids: tuple[str, ...],
    ) -> CompactTrace:
        return CompactTrace(
            turn_index=turn_index,
            query_digest=query_digest,
            item_ids=tuple(item.id for item in items),
            epistemic_labels=tuple(item.epistemic_status for item in items),
            review_statuses=tuple(item.review_status for item in items),
            human_verified=tuple(item.human_verified for item in items),
            recall_log_ids=log_ids,
            retrieval_origins=tuple(item.retrieval_origins for item in items),
        )

    @staticmethod
    def _dedupe_adjacent_trace(
        trace: CompactTrace, previous: CompactTrace | None
    ) -> CompactTrace | None:
        if previous is None:
            return trace
        previous_items = set(previous.item_ids)
        keep = [
            index
            for index, item_id in enumerate(trace.item_ids)
            if item_id not in previous_items
        ]
        log_ids = tuple(
            log_id for log_id in trace.recall_log_ids if log_id not in previous.recall_log_ids
        )
        if not keep and not log_ids:
            return None
        return CompactTrace(
            turn_index=trace.turn_index,
            query_digest=trace.query_digest,
            item_ids=tuple(trace.item_ids[index] for index in keep),
            epistemic_labels=tuple(trace.epistemic_labels[index] for index in keep),
            review_statuses=tuple(trace.review_statuses[index] for index in keep),
            human_verified=tuple(trace.human_verified[index] for index in keep),
            recall_log_ids=log_ids,
            retrieval_origins=tuple(trace.retrieval_origins[index] for index in keep),
        )

    def pre_llm_call(
        self,
        user_message: str = "",
        session_id: str = "",
        is_first_turn: bool = False,
        **kwargs: Any,
    ) -> dict[str, str] | None:
        """Recall the current query and return safe same-turn plugin context."""
        del kwargs
        if not self.config.recall_enabled or not session_id:
            return None
        query = user_message.strip() if isinstance(user_message, str) else ""
        if not query:
            return None
        query_digest = _digest(query)

        with self._lock:
            state = self._state(session_id)
            state.turn_index += 1
            turn_index = state.turn_index
            traces = self._live_traces(state)
            if state.breaker_open:
                evidence = merge_evidence(
                    state.startup_evidence, (), self.config.recall_item_budget
                )
                context = render_envelope(
                    evidence,
                    state.startup_recall_log_ids,
                    traces,
                    self.config.recall_max_context_bytes,
                )
                logger.info(
                    "Engram recall: session=%s query=%s turn=%d generation=%d "
                    "disposition=%s failures=%d breaker=%s elapsed_ms=%d",
                    _digest(session_id),
                    query_digest,
                    turn_index,
                    state.generation,
                    _FetchDisposition.BREAKER_OPEN.value,
                    state.consecutive_failures,
                    "open",
                    0,
                )
                return {"context": context} if context else None
            state.generation += 1
            generation = state.generation
            want_startup = is_first_turn or not state.startup_loaded
            self._active_sessions[session_id] = self._active_sessions.get(session_id, 0) + 1

        started = time.monotonic()
        outcome = self._bounded_fetch(session_id, query, want_startup)
        elapsed_ms = round((time.monotonic() - started) * 1000)

        with self._lock:
            active_count = self._active_sessions.get(session_id, 0)
            if active_count <= 1:
                self._active_sessions.pop(session_id, None)
            else:
                self._active_sessions[session_id] = active_count - 1
            current_state = self._sessions.get(session_id)
            if current_state is None or current_state.generation != generation:
                logger.info(
                    "Engram recall: session=%s query=%s turn=%d generation=%d "
                    "disposition=%s failures=%d breaker=%s elapsed_ms=%d",
                    _digest(session_id),
                    query_digest,
                    turn_index,
                    generation,
                    _FetchDisposition.STALE_GENERATION_DISCARDED.value,
                    current_state.consecutive_failures if current_state is not None else 0,
                    "open"
                    if current_state is not None and current_state.breaker_open
                    else "closed",
                    elapsed_ms,
                )
                return None
            state = current_state
            state.last_touched_monotonic = time.monotonic()
            if outcome.startup.completed:
                state.startup_loaded = True
                state.startup_evidence = outcome.startup.evidence
                state.startup_recall_log_ids = tuple(
                    value for value in (outcome.startup.recall_log_id,) if value
                )
            if outcome.semantic_completed:
                state.consecutive_failures = 0
                state.breaker_open = False
            elif outcome.semantic_attempted:
                state.consecutive_failures += 1
                state.breaker_open = (
                    state.consecutive_failures >= self.config.recall_breaker_failures
                )

            if outcome.semantic_completed:
                startup_now = outcome.startup.evidence if outcome.startup.completed else ()
                evidence = merge_evidence(
                    startup_now,
                    outcome.semantic.evidence,
                    self.config.recall_item_budget,
                )
                log_ids = tuple(
                    dict.fromkeys(
                        value
                        for value in (
                            outcome.startup.recall_log_id,
                            outcome.semantic.recall_log_id,
                        )
                        if value
                    )
                )
            else:
                evidence = merge_evidence(
                    state.startup_evidence, (), self.config.recall_item_budget
                )
                log_ids = state.startup_recall_log_ids

            context = render_envelope(
                evidence,
                log_ids,
                traces,
                self.config.recall_max_context_bytes,
            )
            if context and evidence and self.config.recall_followup_turns:
                trace = self._trace(turn_index, query_digest, evidence, log_ids)
                previous = state.recent_traces[-1] if state.recent_traces else None
                deduped_trace = self._dedupe_adjacent_trace(trace, previous)
                if deduped_trace is not None:
                    state.recent_traces.append(deduped_trace)
            logger.info(
                "Engram recall: session=%s query=%s turn=%d generation=%d "
                "disposition=%s failures=%d breaker=%s elapsed_ms=%d",
                _digest(session_id),
                query_digest,
                turn_index,
                generation,
                outcome.disposition.value,
                state.consecutive_failures,
                "open" if state.breaker_open else "closed",
                elapsed_ms,
            )
            self._evict_if_needed()
            return {"context": context} if context else None
