"""Env-driven configuration for the engram-hooks companion library.

Maps Hermes lifecycle events to hook entry points and Engram ``source_type``
values, and reads connection + promotion thresholds from the environment.

Design references
-----------------
- ``docs/design.md`` §4 (Source trust defaults): ``sync_turn``/``pre_compress``
  are low-trust, inferred sources (memory_confidence 0.4 / 0.3). They default to
  ``review_status='proposed'`` and stay below the 0.7 auto-promotion gate until
  an LLM classification (1B) or human review raises their confidence.
- The Hermes lifecycle events we hook are ``pre_compress``, ``sync_turn``, and
  ``session_end``. Each maps to one of our hook entry points and to an Engram
  ``source_type`` so the service can apply the right trust defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# ---------------------------------------------------------------------------
# Hermes lifecycle event -> hook entry point + Engram source_type mapping.
# Kept module-level (frozen data) so callers can introspect it without an
# instance, and so the README table can be generated from one source of truth.
# ---------------------------------------------------------------------------

# Hook names exposed by this library. Each is wired to a Hermes lifecycle event
# in ``EVENT_HOOK_MAP`` below.
HookName = Literal["pre_compress", "sync_turn", "session_end"]

# Hermes lifecycle event name -> the hook entry point that handles it.
# Hermes emits these event names on its lifecycle hook bus; we map them to the
# async functions in ``engram_hooks.hooks``.
EVENT_HOOK_MAP: dict[str, HookName] = {
    "pre_compress": "pre_compress",
    "sync_turn": "sync_turn",
    "session_end": "session_end",
}

# Hermes lifecycle event name -> the Engram ``source_type`` recorded on writes
# produced from that event. The server uses source_type (together with the
# principal type) to pick source_trust / memory_confidence / review_status
# defaults — see design.md §4.
EVENT_SOURCE_TYPE: dict[str, Literal["sync_turn", "pre_compress", "extraction"]] = {
    "pre_compress": "pre_compress",
    "sync_turn": "sync_turn",
    # session_end has no dedicated source_type; facts extracted at session close
    # are recorded as extraction (inferred, defaults to proposed).
    "session_end": "extraction",
}


def _env(name: str, default: str) -> str:
    """Read a string env var, falling back to ``default``."""
    import os

    return os.environ.get(name, default)


def _env_float(name: str, default: float) -> float:
    """Parse an env var as float, falling back to ``default`` on any error."""
    raw = _env(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    """Parse an env var as int, falling back to ``default`` on any error."""
    raw = _env(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    """Parse an env var as bool (``1``/``true``/``yes`` -> True)."""
    raw = _env(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(slots=True)
class HooksConfig:
    """Runtime configuration for engram-hooks.

    All fields default from the environment so the plugin works zero-config in a
    container that already exports ``ENGRAM_*`` vars (Hermes passes its MCP
    server ``env:`` block through to spawned processes).
    """

    # Engram REST API connection. base_url is required at call time; if unset
    # we surface a clear error rather than failing deep inside httpx.
    base_url: str = field(default_factory=lambda: _env("ENGRAM_BASE_URL", ""))
    api_key: str | None = field(default_factory=lambda: _env("ENGRAM_API_KEY", "") or None)
    timeout: float = field(default_factory=lambda: _env_float("ENGRAM_TIMEOUT", 30.0))

    # Volatile (local) store location. Defaults into the Hermes data dir when
    # present, else the OS temp dir. 14-day retention / 2000-entry cap match the
    # design.md volatile-recall spec.
    volatile_path: str = field(
        default_factory=lambda: _env(
            "ENGRAM_HOOKS_VOLATILE_PATH",
            _default_volatile_path(),
        )
    )
    volatile_retention_days: int = field(
        default_factory=lambda: _env_int("ENGRAM_HOOKS_VOLATILE_RETENTION_DAYS", 14)
    )
    volatile_cap: int = field(default_factory=lambda: _env_int("ENGRAM_HOOKS_VOLATILE_CAP", 2000))

    # Promotion gate. classify() returns a confidence in [0,1]; items at or above
    # this threshold are written to Engram as ``proposed`` (the service then
    # applies its own auto-promotion gate at 0.7). Below it, candidates land in
    # the local volatile store instead — matching the "promotion gates" AC.
    promote_confidence_threshold: float = field(
        default_factory=lambda: _env_float("ENGRAM_HOOKS_PROMOTE_THRESHOLD", 0.6)
    )

    # Workspace/visibility defaults applied when Engram classify doesn't supply
    # wing/room/visibility. ``workspace`` is forwarded to remember() so writes
    # land in the right scope.
    default_workspace: str | None = field(
        default_factory=lambda: _env("ENGRAM_HOOKS_WORKSPACE", "") or None
    )

    # When True (default), emit the prepare_memory_write compat shim on install.
    # Disable for environments that already ship PR #59898, or if the shim
    # causes trouble and you want the plugin to fall back to volatile-only
    # lifecycle hooks without touching Hermes' memory dispatch at all.
    enable_compat_shim: bool = field(
        default_factory=lambda: _env_bool("ENGRAM_HOOKS_COMPAT_SHIM", True)
    )

    # When True, a profile that loads engram-hooks is asserting "automatic
    # memory capture is active" and means it. install() raises
    # AutomaticCaptureUnavailable if, after detection + shim installation,
    # neither the native prepare_memory_write hook nor the compat shim ended
    # up active — instead of silently degrading while the profile still
    # claims automatic capture works. Off by default so the library keeps
    # working standalone (tests, no-Hermes contexts) without opting into a
    # hard failure mode.
    require_automatic_capture: bool = field(
        default_factory=lambda: _env_bool("ENGRAM_HOOKS_REQUIRE_AUTOMATIC_CAPTURE", False)
    )

    def source_type_for(self, event: str) -> Literal["sync_turn", "pre_compress", "extraction"]:
        """Resolve the Engram source_type for a Hermes lifecycle ``event``."""
        if event not in EVENT_SOURCE_TYPE:
            # Unknown events default to extraction (inferred, proposed). This is
            # the safest Engram default for agent-sourced content.
            return "extraction"
        return EVENT_SOURCE_TYPE[event]

    def hook_for(self, event: str) -> HookName | None:
        """Resolve the hook entry point name for a Hermes lifecycle ``event``."""
        return EVENT_HOOK_MAP.get(event)


def _default_volatile_path() -> str:
    """Pick a sensible default volatile-store path.

    Prefers ``$HERMES_DATA_DIR/engram-volatile.jsonl`` (Hermes convention), then
    ``~/.hermes/engram-volatile.jsonl``, then the OS temp dir. The file is
    created lazily on first write, so just returning a path here is safe.
    """
    import os
    import tempfile

    hermes_data = os.environ.get("HERMES_DATA_DIR")
    if hermes_data:
        return os.path.join(hermes_data, "engram-volatile.jsonl")
    home = os.path.expanduser("~")
    hermes_home = os.path.join(home, ".hermes")
    # ~/.hermes is the conventional Hermes config/data root; use it if present,
    # otherwise fall back to the temp dir so we never write outside a known dir.
    if os.path.isdir(hermes_home):
        return os.path.join(hermes_home, "engram-volatile.jsonl")
    return os.path.join(tempfile.gettempdir(), "engram-volatile.jsonl")
