"""Pinned nested/lazy-import shape from stock agent/tool_executor.py."""
from __future__ import annotations

from typing import Any


def _pre_tool_block(agent: Any, function_args: dict[str, Any]) -> str | None:
    try:
        from hermes_cli.plugins import resolve_pre_tool_block

        return resolve_pre_tool_block(
            "memory",
            function_args,
            session_id=getattr(agent, "session_id", "") or "",
            tool_call_id="fixture-tool-call",
        )
    except Exception:
        return None


def execute_memory(agent: Any, function_args: dict[str, Any]) -> str:
    import json

    block_message = _pre_tool_block(agent, function_args)
    if block_message is not None:
        return json.dumps({"error": block_message}, ensure_ascii=False)

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
