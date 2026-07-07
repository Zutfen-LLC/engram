"""Lifecycle hook implementations + Hermes compatibility shim.

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
from dataclasses import dataclass, field
from typing import Any

from .config import HooksConfig
from .guards import GuardVerdict, is_allowed, prepare_memory_write_guard
from .volatile import VolatileEntry, VolatileStore, store_from_config

logger = logging.getLogger("engram_hooks")

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
    promoted: int = 0       # written to Engram as proposed
    parked: int = 0         # parked in the local volatile store
    errors: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)


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
            import engram_client  # type: ignore[import-not-found]
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
        # 1. Write-boundary guard. Always returns a verdict (never None).
        verdict: GuardVerdict = prepare_memory_write_guard(content)
        if not is_allowed(verdict):
            return {"content": content, "route": "rejected", "verdict": verdict}

        # 2. Engram classify, if a client is available. Without a client we
        #    park everything in volatile — the plugin degrades gracefully.
        client = self._get_client()
        kind: str | None = verdict.get("kind")
        wing: str | None = verdict.get("wing")
        room: str | None = verdict.get("room")
        confidence: float = 0.0

        if client is not None:
            try:
                resp = await client.classify(
                    content, context=source_type, workspace=self.config.default_workspace
                )
                confidence = resp.confidence
                # Enrich with the classifier's suggestions if the guard didn't
                # already supply taxonomy. Guard taxonomy wins (it's explicit).
                kind = kind or resp.suggested_kind
                wing = wing or resp.suggested_wing
                room = room or resp.suggested_room
            except Exception as exc:  # noqa: BLE001 — classify failures park locally
                logger.warning("classify failed for candidate, parking locally: %s", exc)
                confidence = 0.0
        # else: confidence stays 0.0 → always parks locally, which is correct.

        # 3. Promotion gate. At/above threshold → remember as proposed. The
        #    server applies its own 0.7 auto-promotion gate on top; we just
        #    decide whether the candidate is worth a server round-trip at all.
        if client is not None and confidence >= self.config.promote_confidence_threshold:
            try:
                await client.remember(
                    content,
                    kind=kind,
                    wing=wing,
                    room=room,
                    workspace=self.config.default_workspace,
                    source_type=source_type,
                )
                return {
                    "content": content,
                    "route": "promoted",
                    "confidence": confidence,
                    "kind": kind,
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
                confidence=confidence or None,
                workspace=self.config.default_workspace,
                reason="below promotion threshold" if client is not None else "no engram client",
            )
        )
        return {"content": content, "route": "parked", "confidence": confidence}

    # ---- public lifecycle hook entry points ----

    async def run_hook(self, event: str, payload: Any) -> HookResult:
        """Generic hook dispatcher invoked for any mapped lifecycle event.

        ``event`` is the Hermes lifecycle event name (see
        :data:`~engram_hooks.config.EVENT_HOOK_MAP`). ``payload`` is whatever
        Hermes passed. Returns a :class:`HookResult` summarizing the routing.
        """
        source_type = self.config.source_type_for(event)
        candidates = self._extract_candidates(payload)
        result = HookResult(event=event, extracted=len(candidates))
        for content in candidates:
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
            elif route == "promoted":
                result.promoted += 1
            elif route == "parked":
                result.parked += 1
            result.details.append(detail)
        logger.info(
            "engram-hooks %s: extracted=%d rejected=%d promoted=%d parked=%d errors=%d",
            event, result.extracted, result.rejected, result.promoted,
            result.parked, result.errors,
        )
        return result

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


def _patch_memory_dispatch(mod: Any, hooks: LifecycleHooks) -> bool:
    """Wrap one module's memory() dispatch to route through our guard.

    Returns ``True`` if a dispatch function was found and patched, ``False``
    otherwise. The wrapper calls :meth:`LifecycleHooks.prepare_memory_write`
    (i.e. the active-rejection guard) before delegating to the original; a
    ``reject`` verdict short-circuits the native write entirely.
    """
    for attr in _MEMORY_DISPATCH_ATTRS:
        original = getattr(mod, attr, None)
        if not callable(original):
            continue

        def wrapper(content: str, *args: Any, _orig: Any = original,
                    _hooks: LifecycleHooks = hooks, **kw: Any) -> Any:
            verdict = _hooks.prepare_memory_write(content, **kw)
            if not is_allowed(verdict):
                logger.info(
                    "engram-hooks compat shim rejected a memory write: %s",
                    verdict.get("reason"),
                )
                return verdict  # {handled: True, action: reject} — active rejection
            return _orig(content, *args, **kw)

        setattr(mod, attr, wrapper)
        logger.info(
            "engram-hooks compat shim wrapped %s.%s", mod.__name__, attr
        )
        return True
    return False


def install_compat_shim(hooks: LifecycleHooks) -> dict[str, Any]:
    """Detect ``prepare_memory_write`` and patch dispatch if it's missing.

    This is the ~20-line compatibility shim the task spec calls for. It:

    1. Detects whether the hook exists natively (PR #59898 merged).
    2. If yes: logs that we're using it natively and returns.
    3. If no: wraps each memory() dispatch site so our guard runs first.

    The result dict records what happened, for logging and tests.
    """
    detection = detect_prepare_memory_write()

    if detection["hook_present"]:
        logger.info(
            "prepare_memory_write found natively on %s — using hook directly, "
            "no monkey-patch needed.", detection["provider"]
        )
        return {"shim_applied": False, "detection": detection}

    if not detection["hermes_present"]:
        # No Hermes at all (e.g. tests, or plugin imported outside Hermes).
        # Nothing to patch; the lifecycle hooks still work standalone.
        logger.debug(
            "Hermes not installed ( %s ) — compat shim inactive, lifecycle "
            "hooks available standalone.", detection["error"]
        )
        return {"shim_applied": False, "detection": detection}

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
    for mod_path in _HERMES_DISPATCH_MODULES:
        try:
            mod = __import__(mod_path, fromlist=["*"])
        except ImportError:
            logger.debug("Hermes module %s not importable — skipping", mod_path)
            continue
        if _patch_memory_dispatch(mod, hooks):
            patched.append(mod_path)

    if not patched:
        logger.warning(
            "engram-hooks compat shim could not locate any memory() dispatch in "
            "%s — no patch applied. The lifecycle hooks still work; only the "
            "write-boundary guard on direct memory() calls is inactive.",
            ", ".join(_HERMES_DISPATCH_MODULES),
        )

    return {"shim_applied": bool(patched), "patched_modules": patched, "detection": detection}


# ===========================================================================
# Top-level install entry point
# ===========================================================================


def install(config: HooksConfig | None = None) -> dict[str, Any]:
    """Plugin load entry point: build hooks, apply shim, return state.

    Call this once at plugin load (Hermes' plugin discovery invokes it, or a
    human calls it from their Hermes config). It constructs the
    :class:`LifecycleHooks` engine, applies the compatibility shim if needed,
    and returns a dict the caller can log or inspect.

    The constructed ``LifecycleHooks`` is also stashed at module level
    (readable via :func:`get_active_hooks`) so the Hermes lifecycle bus can find
    it. Use the accessor rather than reading a module attribute directly: the
    handle is mutated by ``install()``, and a ``from … import`` of the name would
    capture the pre-install value.
    """
    global ACTIVE_HOOKS
    hooks = LifecycleHooks(config)
    ACTIVE_HOOKS = hooks
    shim_result = install_compat_shim(hooks) if hooks.config.enable_compat_shim else {
        "shim_applied": False, "detection": {"hermes_present": False},
    }
    logger.info(
        "engram-hooks installed: base_url=%s volatile=%s shim_applied=%s",
        hooks.config.base_url or "(unset)", hooks.volatile.path,
        shim_result.get("shim_applied", False),
    )
    return {"hooks": hooks, "shim": shim_result}


def get_active_hooks() -> LifecycleHooks | None:
    """Return the :class:`LifecycleHooks` installed by :func:`install`, or ``None``.

    This is the live accessor for the installed engine. Always prefer it over
    importing ``ACTIVE_HOOKS`` by name — a ``from engram_hooks import
    ACTIVE_HOOKS`` binds the value at import time and won't see a later
    ``install()`` rebind it.
    """
    return ACTIVE_HOOKS


# Module-level handle to the active LifecycleHooks, set by install(). Read it
# through get_active_hooks() so callers always see the post-install value. The
# Hermes lifecycle bus dispatches pre_compress / sync_turn / session_end here.
ACTIVE_HOOKS: LifecycleHooks | None = None
