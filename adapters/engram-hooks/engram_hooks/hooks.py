r"""engram_hooks.hooks — lifecycle hook implementations + Hermes compatibility shim.

This module is the heart of the engram-hooks companion library. It wires three
Hermes lifecycle events (``pre_compress``, ``sync_turn``, ``session_end``) to a
single routing pipeline:

.. code-block:: text

    candidate ─▶ write-boundary guard ─▶ (reject)  drop, return verdict
                    │
                 (allow)
                    │
                    ▼
              Engram classify ─▶ confidence ≥ threshold ─▶ remember (proposed)
                    │
              confidence < threshold
                    │
                    ▼
              local volatile store (14-day, 2000-cap)

Classification and durable storage are delegated to Engram via the SDK; the
*lifecycle decisions* (when to extract, whether to promote or park locally)
stay here — exactly the split design.md §2 principle 8 calls for.

Compatibility shim
------------------
The upstream ``prepare_memory_write`` hook (PR #59898) is not in stock Hermes.
:func:`install` detects whether it exists on the ``MemoryProvider`` ABC at load
time. If present, the provider's native hook is authoritative. If missing, the
shim wraps ``tools.memory_tool.memory_tool``, the late-imported write boundary
shared by stock Hermes' executor paths. Accepted durable adds are handled by
the active Engram provider and never reach the native store; rejected adds are
blocked at the same boundary. No Hermes source is edited.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from .config import HooksConfig
from .guards import GuardVerdict, is_allowed, prepare_memory_write_guard
from .volatile import VolatileEntry, VolatileStore, store_from_config

logger = logging.getLogger("engram_hooks")


# ---------------------------------------------------------------------------
# Per-session context accumulator.
#
# The classifier on the server receives one candidate at a time. Without
# conversation context, a candidate like "we chose pgvector" is ambiguous —
# is it a decision? a fact? an observation? With context ("we're migrating
# the DB", "evaluating vector stores"), the LLM can correctly classify it as
# a decision in the infrastructure wing.
#
# This accumulator keeps a rolling window of recently-routed candidate
# strings (post-guard, so only quality candidates make it in). It's bounded
# by both entry count and character budget. The context_string() output is
# passed to the server's classify API via the ``context`` parameter, which
# flows into the LLM prompt's ``context`` field.
#
# Design choice: no LLM summarization here. The raw candidate strings are
# already short (sentence-split by _extract_candidates). Sending the last N
# as context is cheap and gives the classifier real signal. This is a
# client-side concern (design.md §2 principle 8) — the server stays generic.
# ---------------------------------------------------------------------------


class _SessionContext:
    """Rolling window of recently-extracted candidates for this session.

    Thread-safe via a lock — ``sync_turn`` can fire concurrently from a
    background task while ``session_end`` runs on the main thread.
    """

    def __init__(self, max_entries: int = 10, max_chars: int = 800) -> None:
        self._max_entries = max_entries
        self._max_chars = max_chars
        self._entries: list[str] = []
        import threading
        self._lock = threading.Lock()

    def add(self, content: str) -> None:
        """Record a candidate that was routed (promoted or parked)."""
        with self._lock:
            self._entries.append(content)
            # Entry-count limit: keep the most recent N.
            if len(self._entries) > self._max_entries:
                self._entries = self._entries[-self._max_entries:]
            # Char budget: trim oldest entries until under the limit.
            while (
                sum(len(e) for e in self._entries) > self._max_chars
                and len(self._entries) > 1
            ):
                self._entries.pop(0)

    def reset(self) -> None:
        """Clear all context — called on new session / initialize()."""
        with self._lock:
            self._entries.clear()

    def context_string(self) -> str:
        """Build a context string for the classify API's ``context`` field.

        Returns "" when empty — the caller passes None in that case so the
        server treats it as "no context" rather than an empty string.
        """
        with self._lock:
            if not self._entries:
                return ""
            return "\n".join(f"- {e}" for e in self._entries)


# PR #59898 adds prepare_memory_write to the Hermes MemoryProvider ABC. We point
# users here from every shim log line so the reason for the monkey-patch is one
# click away.
_UPSTREAM_PR_URL = "https://github.com/NousResearch/hermes-agent/pull/59898"

# Source provenance for the stock-Hermes contract implemented below. Both
# ``agent/tool_executor.py`` and ``agent/agent_runtime_helpers.py`` late-import
# this exact symbol at execution time. The general pre-tool hook can veto a
# call, but can only produce a blocked/error result, so it cannot replace a
# successful accepted write.
HERMES_REFERENCE_REPOSITORY = "NousResearch/hermes-agent"
HERMES_REFERENCE_SHA = "36f2a966c7f9f69987494b867c3dcf96b69a5766"
_HERMES_MEMORY_TOOL_MODULE = "tools.memory_tool"
_HERMES_MEMORY_TOOL_ATTR = "memory_tool"

WriteInterceptor = Callable[..., dict[str, Any] | None]


# ---------------------------------------------------------------------------
# Hook result — a plain summary so Hermes (or a test) can inspect what happened.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class HookResult:
    """Summary of one lifecycle hook invocation.

    Returned by every hook so the caller (Hermes lifecycle bus, a test, or a
    human reading logs) can see how many candidates were routed where. This is
    a dataclass, not a dict, so the fields are typed and discoverable.
    """

    event: str
    extracted: int = 0
    rejected: int = 0
    promoted: int = 0       # compatibility counter: remembered as proposed
    parked: int = 0         # parked in the local volatile store
    errors: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)

    @property
    def remembered(self) -> int:
        """Memories written as proposed (``promoted`` compatibility alias)."""
        return self.promoted


# ---------------------------------------------------------------------------
# Lifecycle hook engine
# ---------------------------------------------------------------------------


class LifecycleHooks:
    """Owns the Engram client + volatile store and routes candidates.

    Construct one instance per plugin load (typically in :func:`install`) and
    call its hook methods from the Hermes lifecycle bus. The Engram SDK client
    is created lazily on first use so the plugin imports cleanly even when
    ``ENGRAM_BASE_URL`` is unset — useful in test/import-only contexts.
    """

    def __init__(self, config: HooksConfig | None = None) -> None:
        self.config = config or HooksConfig()
        self._client: Any = None  # engram_client.EngramClient, lazily created
        self._client_failed = False
        self.volatile: VolatileStore = store_from_config(self.config)
        self._session_context = _SessionContext()

    # ---- client lifecycle ----

    def _get_client(self) -> Any:
        """Lazily create and cache the Engram SDK client.

        Returns ``None`` (and logs once) if ``ENGRAM_BASE_URL`` is unset or the
        SDK isn't importable. Callers check for ``None`` and degrade to
        volatile-only mode — the plugin stays useful without a server.
        """
        if self._client is not None or self._client_failed:
            return self._client
        if not self.config.base_url:
            self._client_failed = True
            logger.warning(
                "ENGRAM_BASE_URL unset — engram-hooks will park candidates in "
                "the local volatile store only (no classify/remember)."
            )
            return None
        try:
            import engram_client
        except ImportError:
            self._client_failed = True
            logger.warning(
                "engram-client SDK not installed — engram-hooks will park "
                "candidates in the local volatile store only."
            )
            return None
        self._client = engram_client.EngramClient(
            self.config.base_url, self.config.api_key, timeout=self.config.timeout
        )
        return self._client

    async def aclose(self) -> None:
        """Close the Engram client if one was opened."""
        if self._client is not None:
            await self._client.close()
            self._client = None

    def reset_session_context(self) -> None:
        """Clear accumulated session context — call on new session start.

        Without this, context from a previous session bleeds into the next
        one's classification, causing cross-topic contamination.
        """
        self._session_context.reset()

    # ---- candidate extraction ----

    @staticmethod
    def _extract_candidates(payload: Any) -> list[str]:
        """Normalize a Hermes lifecycle payload into candidate fact strings.

        Accepts a string, a list of strings, or a dict with ``content``/``text``/
        ``summary`` keys. Splits long strings on newlines and sentence
        boundaries so a multi-fact pre_compress blob becomes individual
        candidates, each routed independently through the guard + classify gate.

        Extraction is deliberately dumb (regex-free sentence split): the
        *intelligence* about what's a fact is the service's job (classify). Here
        we just produce a list of substrings to evaluate.
        """
        if payload is None:
            return []
        # Unwrap common payload shapes.
        if isinstance(payload, dict):
            raw: Any = (
                payload.get("content")
                or payload.get("text")
                or payload.get("summary")
                or payload.get("candidates")
            )
            if isinstance(raw, list):
                return LifecycleHooks._extract_candidates(raw)
            payload = raw
        if isinstance(payload, list):
            out: list[str] = []
            for item in payload:
                out.extend(LifecycleHooks._extract_candidates(item))
            return out
        if not isinstance(payload, str):
            payload = str(payload)
        text = payload.strip()
        if not text:
            return []
        # Split into candidate lines/sentences. A single short fact stays whole.
        candidates: list[str] = []
        for line in text.splitlines():
            line = line.strip().lstrip("-*• ").strip()
            if not line:
                continue
            # Rough sentence split on . ; ! — good enough for routing; the
            # classifier and guard do the real filtering.
            for sentence in _split_sentences(line):
                sentence = sentence.strip()
                if sentence:
                    candidates.append(sentence)
        return candidates

    # ---- the routing pipeline ----

    async def _route_candidate(
        self, content: str, *, source_type: str
    ) -> dict[str, Any]:
        """Route one candidate through guard → classify → remember/volatile.

        Returns a detail dict for the :class:`HookResult`. Every branch is
        terminal — there is no fall-through to an unintended action.
        """
        # One correlation id per candidate (ENG-METER-001), shared by the
        # classify and remember calls below so the server counts this
        # candidate as a single usage-telemetry observation.
        correlation_id = uuid.uuid4()

        # 1. Write-boundary guard. Always returns a verdict (never None).
        verdict: GuardVerdict = prepare_memory_write_guard(content)
        if not is_allowed(verdict):
            return {
                "content": content,
                "route": "rejected",
                "guard_rejected": True,
                "verdict": verdict,
            }

        # 2. Engram classify, if a client is available. Without a client we
        #    park everything in volatile — the plugin degrades gracefully.
        client = self._get_client()
        kind: str | None = verdict.get("kind")
        wing: str | None = verdict.get("wing")
        room: str | None = verdict.get("room")
        taxonomy_confidence = 0.0
        retention_confidence = 0.0
        retention_disposition = "uncertain"
        classification_run_id: Any = None
        ingest_id: Any = None
        classified = False

        if client is not None:
            try:
                # Build context from accumulated session history so the
                # classifier sees this candidate in conversation context.
                ctx = self._session_context.context_string() or None
                resp = await client.classify(
                    content,
                    context=ctx,
                    workspace=self.config.default_workspace,
                    source_type=source_type,
                    correlation_id=correlation_id,
                )
                taxonomy_confidence = resp.taxonomy_confidence
                retention_confidence = resp.retention_confidence
                retention_disposition = resp.retention_disposition
                classification_run_id = resp.classification_run_id
                ingest_id = resp.ingest_id
                classified = True
                # A receipt makes the server taxonomy authoritative.
                kind = resp.suggested_kind
                wing = resp.suggested_wing
                room = resp.suggested_room
            except Exception as exc:  # noqa: BLE001 — classify failures park locally
                logger.warning("classify failed for candidate, parking locally: %s", exc)
        # else: uncertain at 0.0 always parks locally, which is correct.

        detail_evidence = {
            "taxonomy_confidence": taxonomy_confidence,
            "retention_confidence": retention_confidence,
            "retention_disposition": retention_disposition,
            "classification_run_id": classification_run_id,
            "classified": classified,
        }

        if client is not None and retention_disposition == "noise":
            return {"content": content, "route": "rejected", **detail_evidence}

        # 3. Durable-storage gate. At/above threshold → remember as proposed.
        #    This decides whether the candidate is worth a server round-trip.
        store_threshold = self.config.store_confidence_threshold
        assert store_threshold is not None
        if (
            client is not None
            and retention_disposition == "retain"
            and retention_confidence >= store_threshold
        ):
            try:
                await client.remember(
                    content,
                    kind=kind,
                    wing=wing,
                    room=room,
                    workspace=self.config.default_workspace,
                    source_type=source_type,
                    classification_run_id=classification_run_id,
                    correlation_id=correlation_id,
                    ingest_id=ingest_id,
                )
                return {
                    "content": content,
                    "route": "remembered",
                    "kind": kind,
                    **detail_evidence,
                }
            except Exception as exc:  # noqa: BLE001 — remember failure → volatile fallback
                logger.warning("remember failed, parking candidate locally: %s", exc)

        # 4. Park in volatile. This is the default for low-confidence candidates
        #    and the fallback for any Engram error.
        self.volatile.add(
            VolatileEntry(
                content=content,
                source_type=source_type,
                kind=kind,
                wing=wing,
                room=room,
                confidence=retention_confidence or None,
                workspace=self.config.default_workspace,
                reason=(
                    "not durable retention evidence"
                    if client is not None
                    else "no engram client"
                ),
            )
        )
        return {"content": content, "route": "parked", **detail_evidence}

    # ---- public lifecycle hook entry points ----

    async def run_hook(self, event: str, payload: Any) -> HookResult:
        """Generic hook dispatcher invoked for any mapped lifecycle event.

        ``event`` is the Hermes lifecycle event name (see
        :data:`~engram_hooks.config.EVENT_HOOK_MAP`). ``payload`` is whatever
        Hermes passed. Returns a :class:`HookResult` summarizing the routing.
        """
        start = time.monotonic()
        source_type = self.config.source_type_for(event)
        candidates = self._extract_candidates(payload)
        result = HookResult(event=event, extracted=len(candidates))
        guard_rejected = 0
        classified = 0
        candidate_bytes = 0
        for content in candidates:
            candidate_bytes += len(content.encode("utf-8"))
            try:
                detail = await self._route_candidate(content, source_type=source_type)
            except Exception as exc:  # noqa: BLE001 — never let one candidate kill the hook
                result.errors += 1
                result.details.append({"content": content, "route": "error", "error": str(exc)})
                logger.exception("error routing candidate from %s", event)
                continue
            route = detail.get("route")
            if route == "rejected":
                result.rejected += 1
                if detail.get("guard_rejected"):
                    guard_rejected += 1
            elif route == "remembered":
                result.promoted += 1
            elif route == "parked":
                result.parked += 1
            if detail.get("classified"):
                classified += 1

            # Accumulate non-rejected candidates into session context so the
            # next classify call sees conversation history. Rejected candidates
            # are ephemeral noise — don't pollute the context window.
            if route != "rejected":
                self._session_context.add(content)

            result.details.append(detail)
        logger.info(
            "engram-hooks %s: extracted=%d rejected=%d remembered=%d parked=%d errors=%d",
            event,
            result.extracted,
            result.rejected,
            result.remembered,
            result.parked,
            result.errors,
        )

        if self.config.report_lifecycle_telemetry:
            await self._report_lifecycle_summary(
                event=event,
                result=result,
                guard_rejected=guard_rejected,
                classified=classified,
                candidate_bytes=candidate_bytes,
                latency_ms=round((time.monotonic() - start) * 1000),
            )
        return result

    async def _report_lifecycle_summary(
        self,
        *,
        event: str,
        result: HookResult,
        guard_rejected: int,
        classified: int,
        candidate_bytes: int,
        latency_ms: int,
    ) -> None:
        """Best-effort diagnostic summary report (ENG-METER-001).

        Never raises and never mutates ``result`` — a reporting failure (no
        client configured, network error, server error) is logged and
        swallowed so it can never change the caller-visible ``HookResult``.
        """
        client = self._get_client()
        if client is None:
            return
        try:
            await client.report_lifecycle_summary(
                invocation_id=uuid.uuid4(),
                event=event,
                extracted=result.extracted,
                guard_rejected=guard_rejected,
                classified=classified,
                promoted=result.promoted,
                parked=result.parked,
                errors=result.errors,
                candidate_bytes=candidate_bytes,
                latency_ms=latency_ms,
            )
        except Exception as exc:  # noqa: BLE001 — telemetry reporting is best-effort
            logger.warning("lifecycle telemetry summary report failed: %s", exc)

    async def pre_compress(self, payload: Any) -> HookResult:
        """Hook: facts are about to be lost to compression — extract & route."""
        return await self.run_hook("pre_compress", payload)

    async def sync_turn(self, payload: Any) -> HookResult:
        """Hook: a turn completed — extract durable facts from it."""
        return await self.run_hook("sync_turn", payload)

    async def session_end(self, payload: Any) -> HookResult:
        """Hook: session closing — final fact extraction pass."""
        return await self.run_hook("session_end", payload)

    # ---- prepare_memory_write hook implementation (the guard contract) ----

    def prepare_memory_write(
        self, content: str, **kwargs: Any
    ) -> GuardVerdict:
        """Implement the ``prepare_memory_write`` hook (PR #59898 contract).

        This is the same guard as :func:`prepare_memory_write_guard`, exposed as
        a method so it can be registered directly on a ``MemoryProvider``. When
        the native hook exists, Hermes calls this before every write; when it
        doesn't, the compatibility shim routes dispatch through it instead.
        """
        return prepare_memory_write_guard(
            content,
            kind=kwargs.get("kind"),
            wing=kwargs.get("wing"),
            room=kwargs.get("room"),
        )


def _split_sentences(line: str) -> list[str]:
    """Split a line into rough sentences on ``.`` ``;`` ``!`` ``?``.

    Kept simple on purpose: the classifier does the real understanding. We just
    avoid sending a whole paragraph as one candidate. Abbreviations (``e.g.``,
    ``i.e.``) may over-split — that's fine, each piece is still a valid
    candidate for the guard.
    """
    import re

    parts = re.split(r"(?<=[.;!?])\s+", line)
    return [p for p in parts if p]


# ===========================================================================
# Compatibility shim
# ===========================================================================


class AutomaticCaptureUnavailable(RuntimeError):
    """Raised by :func:`install` when automatic capture is required but inactive.

    Set ``HooksConfig.require_automatic_capture=True`` to make this fatal: a
    profile that loads engram-hooks and expects automatic memory capture
    should not silently degrade to "nothing is patched" while still claiming
    the feature works. ``str(exc)`` includes the failure reason and
    remediation guidance; ``exc.status`` carries the full
    :class:`InstallStatus` for programmatic inspection.
    """

    def __init__(self, status: InstallStatus) -> None:
        self.status = status
        super().__init__(
            "engram-hooks: automatic capture was required (require_automatic_capture=True) "
            f"but is not active — {status.failure_reason}. Set "
            "ENGRAM_HOOKS_REQUIRE_AUTOMATIC_CAPTURE=false to disable automatic capture "
            "instead of failing, or ENGRAM_HOOKS_COMPAT_SHIM=false to disable the shim "
            "and rely on native prepare_memory_write only."
        )


@dataclass(slots=True)
class InstallStatus:
    """Structured, inspectable result of one :func:`install` call.

    This is the single source of truth for "what path is active" — logged at
    startup, returned to the caller, and asserted on directly in tests instead
    of scraping log lines.
    """

    native_hook_available: bool
    compat_shim_installed: bool
    activation_mode: Literal[
        "native_prepare", "stock_compat", "recall_only", "incompatible"
    ]
    patched_modules: list[str] = field(default_factory=list)
    failure_reason: str | None = None
    detection: dict[str, Any] = field(default_factory=dict)

    @property
    def automatic_capture_active(self) -> bool:
        """True iff Hermes will route writes through an Engram interceptor."""
        return self.activation_mode in {"native_prepare", "stock_compat"}

    def describe(self) -> str:
        """One-line human-readable summary, used for the startup log line."""
        if self.native_hook_available:
            return f"native prepare_memory_write active (provider={self.detection.get('provider')})"
        if self.compat_shim_installed:
            return f"compatibility shim active (patched={', '.join(self.patched_modules)})"
        return f"{self.activation_mode}: automatic writes INACTIVE — {self.failure_reason}"


def detect_prepare_memory_write() -> dict[str, Any]:
    """Probe the Hermes ``MemoryProvider`` ABC for the ``prepare_memory_write`` hook.

    Returns a dict describing the detection result so callers (and tests) can
    branch on it without re-running the import dance:

    .. code-block:: python

        {
            "hermes_present": bool,
            "hook_present": bool,            # True only if Hermes is present AND has the hook
            "provider": str | None,          # the MemoryProvider qualname, if found
            "error": str | None,             # import error message, if Hermes is absent
        }

    Never raises — a missing Hermes is a normal state (e.g. running tests, or
    running on a machine that only has the plugin installed).
    """
    try:
        provider_cls = _find_memory_provider()
    except ImportError as exc:
        return {
            "hermes_present": False,
            "hook_present": False,
            "provider": None,
            "error": str(exc),
        }
    provider_name = f"{provider_cls.__module__}.{provider_cls.__qualname__}"
    hook_present = callable(getattr(provider_cls, "prepare_memory_write", None))
    return {
        "hermes_present": True,
        "hook_present": hook_present,
        "provider": provider_name,
        "error": None,
    }


def _find_memory_provider() -> type:
    """Import and return the Hermes ``MemoryProvider`` ABC.

    Raises :class:`ImportError` if Hermes isn't installed or the class can't be
    located. The two import paths below cover the current and likely-near-future
    module layouts.
    """
    # Try the most likely locations. Hermes' memory provider ABC sits under one
    # of these depending on version; both resolve to the same class.
    for mod_path, attr in (
        ("hermes_agent.memory.provider", "MemoryProvider"),
        ("hermes_agent.providers.memory", "MemoryProvider"),
        ("hermes_agent.memory", "MemoryProvider"),
        # Current Hermes tree / package layout in local installs.
        ("agent.memory_provider", "MemoryProvider"),
    ):
        try:
            mod = __import__(mod_path, fromlist=[attr])
            cls = getattr(mod, attr)
            if isinstance(cls, type):
                return cls
        except (ImportError, AttributeError):
            continue
    raise ImportError(
        "could not locate MemoryProvider in hermes_agent "
        "(tried hermes_agent.memory.provider, hermes_agent.providers.memory, "
        "hermes_agent.memory)"
    )


# Marker and original attributes are kept on our wrapper so repeated installs
# update only the active callback, never nest wrappers, and disabled mode can
# restore the exact pre-shim function.
_SHIM_MARKER = "__engram_hooks_shim__"
_SHIM_ORIGINAL = "__engram_hooks_original__"
_SHIM_INTERCEPTOR = "__engram_hooks_interceptor__"


def _replacement_result(result: dict[str, Any] | None) -> str:
    """Convert the provider interception contract to stock Hermes JSON."""
    import json

    if not isinstance(result, dict) or result.get("handled") is not True:
        return json.dumps(
            {
                "success": False,
                "error": (
                    "Engram write interceptor did not handle a durable add; "
                    "native write blocked"
                ),
                "provider": "engram",
                "native_write": False,
            },
            ensure_ascii=False,
        )
    replacement = result.get("result")
    if isinstance(replacement, dict):
        payload = dict(replacement)
    elif isinstance(replacement, str):
        payload = {"success": True, "message": replacement}
    else:
        payload = {"success": True, "message": "Handled by Engram"}
    payload.setdefault("provider", "engram")
    payload.setdefault("native_write", False)
    return json.dumps(payload, ensure_ascii=False)


def _batch_contains_add(operations: Any) -> bool:
    """Return true for any batch that could otherwise persist a native add."""
    return isinstance(operations, list) and any(
        isinstance(operation, dict) and operation.get("action") == "add"
        for operation in operations
    )


def _patch_memory_tool(mod: Any, write_interceptor: WriteInterceptor) -> bool:
    """Wrap stock Hermes' shared memory-tool write boundary.

    The active callback lives on the wrapper rather than only in this module's
    globals. Hermes can retain the patched ``tools.memory_tool`` module while a
    plugin reload replaces ``engram_hooks.hooks`` with a new module object. In
    that case the surviving wrapper still resolves the newly installed
    provider, and does not retain or call the provider from the old module.
    """
    import json

    original = getattr(mod, _HERMES_MEMORY_TOOL_ATTR, None)
    if not callable(original):
        return False
    if getattr(original, _SHIM_MARKER, False):
        wrapped = original
        original = getattr(wrapped, _SHIM_ORIGINAL, None)
        if not callable(original):
            return False
        if hasattr(wrapped, _SHIM_INTERCEPTOR):
            setattr(wrapped, _SHIM_INTERCEPTOR, write_interceptor)
            return True
        # Upgrade a wrapper installed by Engram <=0.2.0. That implementation
        # resolved only its defining module's global callback, which becomes
        # stale after a full module replacement.

    def wrapper(
        action: str | None = None,
        target: str | None = "memory",
        content: str | None = None,
        old_text: str | None = None,
        operations: Any = None,
        store: Any = None,
    ) -> str:
        # Stock Hermes batches are atomic. Engram does not yet reconcile
        # replace/remove, so an add-containing batch is rejected as a whole;
        # no prefix is submitted and the native store remains untouched.
        if _batch_contains_add(operations):
            logger.info("engram-hooks rejected unsupported add-containing memory batch")
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        "Engram governs durable adds and does not yet support add-containing "
                        "memory batches; split the durable add into a single memory call"
                    ),
                    "provider": "engram",
                    "native_write": False,
                },
                ensure_ascii=False,
            )

        effective_target = target or "memory"
        if action == "add" and effective_target in {"memory", "user"}:
            interceptor = getattr(wrapper, _SHIM_INTERCEPTOR, None)
            if interceptor is None:
                logger.error(
                    "engram-hooks wrapper is active without a write interceptor; "
                    "blocking native durable add"
                )
                return _replacement_result(None)
            try:
                result = interceptor(
                    action="add",
                    target=effective_target,
                    content=content or "",
                    metadata=None,
                    old_text=old_text,
                )
            except Exception as exc:
                logger.exception("Engram write interception failed; native add blocked")
                return json.dumps(
                    {
                        "success": False,
                        "error": f"Engram write interception failed: {exc}",
                        "provider": "engram",
                        "native_write": False,
                    },
                    ensure_ascii=False,
                )
            return _replacement_result(result)

        return cast(
            str,
            original(
                action=action,
                target=target,
                content=content,
                old_text=old_text,
                operations=operations,
                store=store,
            ),
        )

    setattr(wrapper, _SHIM_MARKER, True)
    setattr(wrapper, _SHIM_ORIGINAL, original)
    setattr(wrapper, _SHIM_INTERCEPTOR, write_interceptor)
    setattr(mod, _HERMES_MEMORY_TOOL_ATTR, wrapper)
    logger.info(
        "engram-hooks stock-Hermes interception active: %s.%s (reference %s@%s)",
        _HERMES_MEMORY_TOOL_MODULE,
        _HERMES_MEMORY_TOOL_ATTR,
        HERMES_REFERENCE_REPOSITORY,
        HERMES_REFERENCE_SHA,
    )
    return True


def _restore_memory_tool() -> bool:
    """Restore a wrapper installed by this module, if one is present."""
    try:
        mod = __import__(_HERMES_MEMORY_TOOL_MODULE, fromlist=["*"])
    except ImportError:
        return False
    current = getattr(mod, _HERMES_MEMORY_TOOL_ATTR, None)
    original = getattr(current, _SHIM_ORIGINAL, None)
    if not getattr(current, _SHIM_MARKER, False) or not callable(original):
        return False
    if hasattr(current, _SHIM_INTERCEPTOR):
        delattr(current, _SHIM_INTERCEPTOR)
    setattr(mod, _HERMES_MEMORY_TOOL_ATTR, original)
    logger.info("engram-hooks restored native %s.memory_tool", _HERMES_MEMORY_TOOL_MODULE)
    return True


def install_compat_shim(
    hooks: LifecycleHooks,
    write_interceptor: WriteInterceptor | None = None,
) -> InstallStatus:
    """Detect ``prepare_memory_write`` and patch dispatch if it's missing.

    It detects whether the hook exists natively (PR #59898), otherwise wraps
    the pinned stock-Hermes ``tools.memory_tool.memory_tool`` boundary.

    ``hooks`` remains explicit at the call site. ``write_interceptor`` must be
    the active provider's governed pre-write callback. The wrapper resolves it
    again at call time, so provider reload updates ownership without rewraps.

    Returns an :class:`InstallStatus` — the same structure :func:`install`
    returns — so callers and tests never have to parse log output.
    """
    global ACTIVE_WRITE_INTERCEPTOR
    del hooks
    ACTIVE_WRITE_INTERCEPTOR = write_interceptor
    detection = detect_prepare_memory_write()

    if write_interceptor is None:
        _restore_memory_tool()
        reason = (
            "no Engram provider write interceptor was registered; refusing to claim "
            "automatic capture"
        )
        return InstallStatus(
            native_hook_available=False,
            compat_shim_installed=False,
            activation_mode="recall_only",
            failure_reason=reason,
            detection=detection,
        )

    if detection["hook_present"]:
        _restore_memory_tool()
        logger.info(
            "prepare_memory_write found natively on %s — using hook directly, "
            "no monkey-patch needed.", detection["provider"]
        )
        return InstallStatus(
            native_hook_available=True,
            compat_shim_installed=False,
            activation_mode="native_prepare",
            detection=detection,
        )

    if not detection["hermes_present"]:
        # No Hermes at all (e.g. tests, or plugin imported outside Hermes).
        # Nothing to patch; the lifecycle hooks still work standalone.
        logger.debug(
            "Hermes not installed ( %s ) — compat shim inactive, lifecycle "
            "hooks available standalone.", detection["error"]
        )
        return InstallStatus(
            native_hook_available=False,
            compat_shim_installed=False,
            activation_mode="recall_only",
            failure_reason=f"Hermes is not installed in this process ({detection['error']})",
            detection=detection,
        )

    # Hermes is present but the hook is missing — this is the case the shim
    # exists for. Log clearly with the PR link so operators understand why
    # their memory tool dispatch is being wrapped.
    logger.warning(
        "prepare_memory_write NOT found on %s — applying runtime compat shim. "
        "This wraps stock Hermes' shared memory_tool boundary to route accepted "
        "adds to Engram and block native persistence. See PR #59898: %s",
        detection["provider"], _UPSTREAM_PR_URL,
    )

    try:
        mod = __import__(_HERMES_MEMORY_TOOL_MODULE, fromlist=["*"])
    except ImportError as exc:
        mod = None
        target_error = repr(exc)
    else:
        target_error = "attribute missing or non-callable"

    if mod is None or not _patch_memory_tool(mod, write_interceptor):
        # Hermes is installed but every known dispatch site is gone or
        # renamed — API drift. Fail loudly with actionable diagnostics rather
        # than a debug-level "no patch applied" that an operator would miss.
        reason = (
            "incompatible Hermes API shape: required stock capture target "
            f"{_HERMES_MEMORY_TOOL_MODULE}.{_HERMES_MEMORY_TOOL_ATTR} unavailable "
            f"({target_error}). Inspected contract: {HERMES_REFERENCE_REPOSITORY}@"
            f"{HERMES_REFERENCE_SHA}; reinstall a compatible Engram plugin or disable "
            "ENGRAM_HOOKS_REQUIRE_AUTOMATIC_CAPTURE only for deliberate recall-only mode."
        )
        logger.error("engram-hooks compat shim FAILED to patch: %s", reason)
        return InstallStatus(
            native_hook_available=False,
            compat_shim_installed=False,
            activation_mode="incompatible",
            failure_reason=reason,
            detection=detection,
        )

    return InstallStatus(
        native_hook_available=False,
        compat_shim_installed=True,
        activation_mode="stock_compat",
        patched_modules=[_HERMES_MEMORY_TOOL_MODULE],
        detection=detection,
    )


# ===========================================================================
# Top-level install entry point
# ===========================================================================


def install(
    config: HooksConfig | None = None,
    *,
    write_interceptor: WriteInterceptor | None = None,
) -> dict[str, Any]:
    """Plugin load entry point: build hooks, apply shim, return state.

    Call this once per process at plugin load (Hermes' plugin discovery
    invokes it, or a human calls it from their Hermes profile/config — see
    ``docs/ops/hermes-dogfood-profile.md``). It constructs the
    :class:`LifecycleHooks` engine, detects/patches ``prepare_memory_write``
    if the profile has the compat shim enabled, and returns a dict the caller
    can log or inspect: ``{"hooks": LifecycleHooks, "status": InstallStatus}``.

    Startup behavior (see also ``HooksConfig``):

    * Native ``prepare_memory_write`` present → registered natively, no patch.
    * Native hook absent, ``enable_compat_shim=True`` (default) → the compat
      shim patches Hermes' shared ``tools.memory_tool.memory_tool`` boundary.
    * Native hook absent, ``enable_compat_shim=False`` → automatic capture is
      disabled; lifecycle hooks (``pre_compress``/``sync_turn``/``session_end``)
      still work if the profile wires them explicitly, but direct
      ``memory()`` calls are not intercepted.
    * ``require_automatic_capture=True`` and neither path ended up active →
      raises :class:`AutomaticCaptureUnavailable` instead of returning, so a
      profile that expects automatic capture cannot silently run without it.

    Idempotent: calling ``install()`` again re-detects and, if the compat
    shim already patched a module, recognizes the existing patch (via a
    marker attribute) instead of double-wrapping it — see
    :func:`_patch_memory_tool`.

    The constructed ``LifecycleHooks`` is also stashed at module level
    (readable via :func:`get_active_hooks`) so the Hermes lifecycle bus can find
    it. Use the accessor rather than reading a module attribute directly: the
    handle is mutated by ``install()``, and a ``from … import`` of the name would
    capture the pre-install value.
    """
    global ACTIVE_HOOKS, ACTIVE_STATUS, ACTIVE_WRITE_INTERCEPTOR
    hooks = LifecycleHooks(config)
    ACTIVE_HOOKS = hooks
    ACTIVE_WRITE_INTERCEPTOR = write_interceptor

    if hooks.config.enable_compat_shim:
        status = install_compat_shim(hooks, write_interceptor)
    else:
        _restore_memory_tool()
        detection = detect_prepare_memory_write()
        if detection["hook_present"] and write_interceptor is not None:
            status = InstallStatus(
                native_hook_available=True,
                compat_shim_installed=False,
                activation_mode="native_prepare",
                detection=detection,
            )
        else:
            status = InstallStatus(
                native_hook_available=False,
                compat_shim_installed=False,
                activation_mode="recall_only",
                failure_reason=(
                    "compat shim disabled (ENGRAM_HOOKS_COMPAT_SHIM=false) and no active "
                    "native prepare_memory_write contract"
                ),
                detection=detection,
            )
    ACTIVE_STATUS = status

    logger.info(
        "engram-hooks installed: base_url=%s volatile=%s active_path=%s",
        hooks.config.base_url or "(unset)", hooks.volatile.path, status.describe(),
    )

    if hooks.config.require_automatic_capture and not status.automatic_capture_active:
        raise AutomaticCaptureUnavailable(status)

    return {"hooks": hooks, "status": status, "shim": status}


def get_active_hooks() -> LifecycleHooks | None:
    """Return the :class:`LifecycleHooks` installed by :func:`install`, or ``None``.

    This is the live accessor for the installed engine. Always prefer it over
    importing ``ACTIVE_HOOKS`` by name — a ``from engram_hooks import
    ACTIVE_HOOKS`` binds the value at import time and won't see a later
    ``install()`` rebind it.
    """
    return ACTIVE_HOOKS


def get_install_status() -> InstallStatus | None:
    """Return the :class:`InstallStatus` from the last :func:`install` call.

    ``None`` if ``install()`` has never run in this process. Operators and
    tests should use this (not log scraping) to check which path is active:
    ``get_install_status().automatic_capture_active`` is the single boolean
    that answers "is Engram actually capturing memory automatically right now".
    """
    return ACTIVE_STATUS


def get_active_write_interceptor() -> WriteInterceptor | None:
    """Return the provider callback currently authoritative for durable adds."""
    return ACTIVE_WRITE_INTERCEPTOR


# Module-level handles set by install(). Read them through the accessor
# functions above so callers always see the post-install value — a
# `from engram_hooks import ACTIVE_HOOKS` binds at import time and would miss
# a later install() rebind.
ACTIVE_HOOKS: LifecycleHooks | None = None
ACTIVE_STATUS: InstallStatus | None = None
ACTIVE_WRITE_INTERCEPTOR: WriteInterceptor | None = None
