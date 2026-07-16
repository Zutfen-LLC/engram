"""Compatibility-relevant subset of stock tools/memory_tool.py."""
from __future__ import annotations

import json
from typing import Any


class MemoryStore:
    def __init__(self) -> None:
        self.entries: dict[str, list[str]] = {"memory": [], "user": []}

    def add(self, target: str, content: str) -> dict[str, Any]:
        self.entries[target].append(content)
        return {"success": True, "action": "add", "target": target}


def memory_tool(
    action: str | None = None,
    target: str | None = "memory",
    content: str | None = None,
    old_text: str | None = None,
    operations: list[dict[str, Any]] | None = None,
    store: MemoryStore | None = None,
) -> str:
    del old_text
    if store is None:
        return json.dumps({"success": False, "error": "Memory is not available"})
    target = target or "memory"
    if operations:
        for operation in operations:
            if operation.get("action") == "add":
                store.add(target, str(operation.get("content") or ""))
        return json.dumps({"success": True, "action": "batch"})
    if action == "add" and content:
        return json.dumps(store.add(target, content))
    return json.dumps({"success": False, "error": "unsupported fixture operation"})
