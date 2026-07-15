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
time. If present, our guard is registered as the native hook. If missing, a
~20-line runtime monkey-patch wraps the two memory-tool dispatch sites
(``tool_executor.memory`` and ``agent_runtime_helpers.memory``) so our guard
runs before any native write. In both cases the plugin works with stock Hermes
— no fork, no source editing.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

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

# Hermes internal modules whose memory() dispatch we wrap when the native hook
# is absent. These are imported lazily so the plugin loads on stock Hermes (and
# on machines where Hermes isn't installed at all).
_HERMES_DISPATCH_MODULES = (
    "hermes_agent.tools.tool_executor",
    "hermes_agent.runtime.agent_runtime_helpers",
    # Current Hermes tree / package layout in local installs.
    "agent.tool_executor",
    "agent.agent_runtime_helpers",
)
# Attribute names the memory() dispatch may live under across Hermes versions.
_MEMORY_DISPATCH_ATTRS = ("memory", "execute_memory_tool", "call_memory")


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
    patched_modules: list[str] = field(default_factory=list)
    failure_reason: str | None = None
    detection: dict[str, Any] = field(default_factory=dict)

    @property
    def automatic_capture_active(self) -> bool:
        """True iff Hermes will actually route writes through our guard."""
        return self.native_hook_available or self.compat_shim_installed

    def describe(self) -> str:
        """One-line human-readable summary, used for the startup log line."""
        if self.native_hook_available:
            return f"native prepare_memory_write active (provider={self.detection.get('provider')})"
        if self.compat_shim_installed:
            return f"compatibility shim active (patched={', '.join(self.patched_modules)})"
        return f"automatic capture DISABLED — {self.failure_reason}"


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


# Marker attribute set on our wrapper functions. Its presence on a module
# attribute is how we detect "already patched" so a second install() call
# (e.g. a test harness re-installing, or a plugin loader calling install()
# more than once) never wraps a wrapper — the double-wrap idempotency AC.
_SHIM_MARKER = "__engram_hooks_shim__"


def _patch_memory_dispatch(mod: Any) -> bool:
    """Wrap one module's memory() dispatch to route through our guard.

    Returns ``True`` if a dispatch function is patched (or was already
    patched by a prior :func:`install` call — idempotent no-op), ``False`` if
    no known dispatch attribute exists on ``mod``.

    The wrapper looks up :func:`get_active_hooks` *at call time* rather than
    closing over a fixed ``LifecycleHooks`` instance, so a later ``install()``
    (which rebinds the module-level active hooks) is picked up without
    re-patching. If no hooks are installed when the wrapper fires, it falls
    back to the stateless :func:`prepare_memory_write_guard` so the guard is
    never silently bypassed.
    """
    for attr in _MEMORY_DISPATCH_ATTRS:
        original = getattr(mod, attr, None)
        if not callable(original):
            continue
        if getattr(original, _SHIM_MARKER, False):
            # Already wrapped by a previous install() — idempotent no-op.
            return True

        def wrapper(content: str, *args: Any, _orig: Any = original, **kw: Any) -> Any:
            active = get_active_hooks()
            verdict = (
                active.prepare_memory_write(content, **kw)
                if active is not None
                else prepare_memory_write_guard(content, **kw)
            )
            if not is_allowed(verdict):
                logger.info(
                    "engram-hooks compat shim rejected a memory write: %s",
                    verdict.get("reason"),
                )
                return verdict  # {handled: True, action: reject} — active rejection
            return _orig(content, *args, **kw)

        setattr(wrapper, _SHIM_MARKER, True)
        setattr(mod, attr, wrapper)
        logger.info(
            "engram-hooks compat shim wrapped %s.%s", mod.__name__, attr
        )
        return True
    return False


def install_compat_shim(hooks: LifecycleHooks) -> InstallStatus:
    """Detect ``prepare_memory_write`` and patch dispatch if it's missing.

    This is the ~20-line compatibility shim the task spec calls for. It:

    1. Detects whether the hook exists natively (PR #59898 merged).
    2. If yes: logs that we're using it natively and returns.
    3. If no: wraps each memory() dispatch site so our guard runs first.

    ``hooks`` is accepted for backward compatibility / explicitness at the
    call site but the actual patched wrappers dispatch through
    :func:`get_active_hooks` at call time (see :func:`_patch_memory_dispatch`).

    Returns an :class:`InstallStatus` — the same structure :func:`install`
    returns — so callers and tests never have to parse log output.
    """
    detection = detect_prepare_memory_write()

    if detection["hook_present"]:
        logger.info(
            "prepare_memory_write found natively on %s — using hook directly, "
            "no monkey-patch needed.", detection["provider"]
        )
        return InstallStatus(
            native_hook_available=True, compat_shim_installed=False, detection=detection,
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
            failure_reason=f"Hermes is not installed in this process ({detection['error']})",
            detection=detection,
        )

    # Hermes is present but the hook is missing — this is the case the shim
    # exists for. Log clearly with the PR link so operators understand why
    # their memory tool dispatch is being wrapped.
    logger.warning(
        "prepare_memory_write NOT found on %s — applying runtime compat shim. "
        "This monkey-patches the memory() dispatch to route writes through "
        "engram-hooks' write-boundary guard. See PR #59898: %s",
        detection["provider"], _UPSTREAM_PR_URL,
    )

    patched: list[str] = []
    unimportable: list[str] = []
    for mod_path in _HERMES_DISPATCH_MODULES:
        try:
            mod = __import__(mod_path, fromlist=["*"])
        except ImportError:
            logger.debug("Hermes module %s not importable — skipping", mod_path)
            unimportable.append(mod_path)
            continue
        if _patch_memory_dispatch(mod):
            patched.append(mod_path)

    if not patched:
        # Hermes is installed but every known dispatch site is gone or
        # renamed — API drift. Fail loudly with actionable diagnostics rather
        # than a debug-level "no patch applied" that an operator would miss.
        reason = (
            "Hermes is installed but no known memory() dispatch site could be patched "
            f"(tried modules {_HERMES_DISPATCH_MODULES!r}, attributes {_MEMORY_DISPATCH_ATTRS!r}; "
            f"unimportable: {unimportable!r}). This usually means Hermes changed its "
            "internal module layout — update _HERMES_DISPATCH_MODULES / "
            "_MEMORY_DISPATCH_ATTRS in engram_hooks/hooks.py to match the installed "
            "Hermes version."
        )
        logger.error("engram-hooks compat shim FAILED to patch: %s", reason)
        return InstallStatus(
            native_hook_available=False,
            compat_shim_installed=False,
            failure_reason=reason,
            detection=detection,
        )

    return InstallStatus(
        native_hook_available=False,
        compat_shim_installed=True,
        patched_modules=patched,
        detection=detection,
    )


# ===========================================================================
# Top-level install entry point
# ===========================================================================


def install(config: HooksConfig | None = None) -> dict[str, Any]:
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
      shim patches Hermes' memory() dispatch sites.
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
    :func:`_patch_memory_dispatch`.

    The constructed ``LifecycleHooks`` is also stashed at module level
    (readable via :func:`get_active_hooks`) so the Hermes lifecycle bus can find
    it. Use the accessor rather than reading a module attribute directly: the
    handle is mutated by ``install()``, and a ``from … import`` of the name would
    capture the pre-install value.
    """
    global ACTIVE_HOOKS, ACTIVE_STATUS
    hooks = LifecycleHooks(config)
    ACTIVE_HOOKS = hooks

    if hooks.config.enable_compat_shim:
        status = install_compat_shim(hooks)
    else:
        status = InstallStatus(
            native_hook_available=False,
            compat_shim_installed=False,
            failure_reason="compat shim disabled (ENGRAM_HOOKS_COMPAT_SHIM=false)",
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


# Module-level handles set by install(). Read them through the accessor
# functions above so callers always see the post-install value — a
# `from engram_hooks import ACTIVE_HOOKS` binds at import time and would miss
# a later install() rebind.
ACTIVE_HOOKS: LifecycleHooks | None = None
ACTIVE_STATUS: InstallStatus | None = None
