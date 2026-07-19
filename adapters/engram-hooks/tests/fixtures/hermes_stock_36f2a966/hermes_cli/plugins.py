"""Minimal pinned general-hook registry and pre-tool block resolver."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

_HOOKS: dict[str, list[Callable[..., Any]]] = {}


class PluginContext:
    def register_hook(self, event: str, callback: Callable[..., Any]) -> None:
        _HOOKS.setdefault(event, []).append(callback)


def resolve_pre_tool_block(
    tool_name: str,
    args: dict[str, Any] | None,
    **kwargs: Any,
) -> str | None:
    """Return the first valid stock ``pre_tool_call`` block message."""
    for callback in _HOOKS.get("pre_tool_call", []):
        result = callback(tool_name=tool_name, args=args or {}, **kwargs)
        if not isinstance(result, dict) or result.get("action") != "block":
            continue
        message = result.get("message")
        if isinstance(message, str) and message:
            return message
    return None
