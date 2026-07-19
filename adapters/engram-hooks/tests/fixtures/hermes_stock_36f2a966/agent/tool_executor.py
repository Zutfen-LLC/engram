"""Pinned nested/lazy-import shape from stock agent/tool_executor.py."""
from __future__ import annotations

from typing import Any


def execute_memory(agent: Any, function_args: dict[str, Any]) -> str:
    def _execute(next_args: dict[str, Any]) -> str:
        target = next_args.get("target", "memory")
        operations = next_args.get("operations")
        from tools.memory_tool import memory_tool as _memory_tool

        result = _memory_tool(
            action=next_args.get("action"),
            target=target,
            content=next_args.get("content"),
            old_text=next_args.get("old_text"),
            operations=operations,
            store=agent._memory_store,
        )
        if agent._memory_manager:
            agent._memory_manager.notify_memory_tool_write(
                result,
                next_args,
                build_metadata=lambda: agent._build_memory_write_metadata(
                    task_id="fixture-task",
                    tool_call_id="fixture-tool-call",
                ),
            )
        return result

    return _execute(function_args)
