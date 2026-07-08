"""engram-hooks — Hermes lifecycle hooks companion library for Engram (T18).

Packages the Hermes lifecycle hooks (``pre_compress``, ``sync_turn``,
``session_end``) and the write-boundary guard as a standalone library that
calls Engram's ``classify`` + ``remember`` via the SDK. Successor to the
zutfen_memory plugin.

The public surface mirrors how callers actually use the library:

- :func:`install` — plugin load entry point. Builds the hooks engine, applies
  the ``prepare_memory_write`` compatibility shim, and stashes the active engine
  for retrieval via :func:`get_active_hooks`.
- :class:`LifecycleHooks` — the engine; call ``pre_compress`` / ``sync_turn`` /
  ``session_end`` on it (or wire them to the Hermes lifecycle bus).
- :func:`prepare_memory_write_guard` — the write-boundary guard used by both the
  hooks and the compat shim.
- :class:`VolatileStore` — local file-backed store for low-confidence candidates.
- :class:`HooksConfig` — env-driven configuration.

Typical usage from a Hermes plugin entry point::

    from engram_hooks import install, get_active_hooks

    install()                          # at plugin load
    hooks = get_active_hooks()         # at lifecycle dispatch
    await hooks.sync_turn(payload)     # on the sync_turn event
"""

from __future__ import annotations

from .config import (
    EVENT_HOOK_MAP,
    EVENT_SOURCE_TYPE,
    HookName,
    HooksConfig,
)
from .guards import (
    GuardVerdict,
    is_allowed,
    prepare_memory_write_guard,
)
from .hooks import (
    AutomaticCaptureUnavailable,
    HookResult,
    InstallStatus,
    LifecycleHooks,
    detect_prepare_memory_write,
    get_active_hooks,
    get_install_status,
    install,
    install_compat_shim,
)
from .volatile import VolatileEntry, VolatileStore, store_from_config

__version__ = "0.1.0"

__all__ = [
    # config
    "HooksConfig",
    "EVENT_HOOK_MAP",
    "EVENT_SOURCE_TYPE",
    "HookName",
    # guards
    "GuardVerdict",
    "prepare_memory_write_guard",
    "is_allowed",
    # volatile
    "VolatileStore",
    "VolatileEntry",
    "store_from_config",
    # hooks engine + install
    "LifecycleHooks",
    "HookResult",
    "install",
    "install_compat_shim",
    "detect_prepare_memory_write",
    "get_active_hooks",
    "InstallStatus",
    "get_install_status",
    "AutomaticCaptureUnavailable",
]
