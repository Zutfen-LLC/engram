#!/usr/bin/env python3
"""Idempotently wire the Engram provider and general plugin into Hermes YAML.

This intentionally edits only the relevant scalar/list entries instead of
round-tripping the whole document through a YAML serializer.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path


def _section(lines: list[str], name: str) -> tuple[int, int] | None:
    start = next((i for i, line in enumerate(lines) if line.rstrip() == f"{name}:"), None)
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.strip() and not line.startswith((" ", "\t", "#")):
            end = index
            break
    return start, end


def _ensure_memory(lines: list[str]) -> None:
    bounds = _section(lines, "memory")
    if bounds is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(("memory:", "  provider: engram_memory"))
        return
    start, end = bounds
    provider = next(
        (
            index
            for index in range(start + 1, end)
            if re.match(r"^  provider\s*:", lines[index])
        ),
        None,
    )
    if provider is None:
        lines.insert(start + 1, "  provider: engram_memory")
    else:
        comment = ""
        if " #" in lines[provider]:
            comment = " #" + lines[provider].split(" #", 1)[1]
        lines[provider] = "  provider: engram_memory" + comment


def _ensure_plugin(lines: list[str]) -> None:
    bounds = _section(lines, "plugins")
    if bounds is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(("plugins:", "  enabled:", "    - engram_memory"))
        return
    start, end = bounds
    enabled = next(
        (
            index
            for index in range(start + 1, end)
            if re.match(r"^  enabled\s*:", lines[index])
        ),
        None,
    )
    if enabled is None:
        lines[start + 1 : start + 1] = ["  enabled:", "    - engram_memory"]
        return
    inline = re.match(r"^  enabled\s*:\s*\[(.*?)\]\s*(#.*)?$", lines[enabled])
    if inline:
        values = [value.strip().strip("'\"") for value in inline.group(1).split(",")]
        if "engram_memory" not in values:
            content = inline.group(1).strip()
            content = f"{content}, engram_memory" if content else "engram_memory"
            comment = f" {inline.group(2)}" if inline.group(2) else ""
            lines[enabled] = f"  enabled: [{content}]{comment}"
        return
    item_end = end
    for index in range(enabled + 1, end):
        line = lines[index]
        if line.strip() and not line.startswith(("    ", "\t", "#")):
            item_end = index
            break
    existing = {
        match.group(1).strip("'\"")
        for line in lines[enabled + 1 : item_end]
        if (match := re.match(r"^\s{4}-\s+([^#]+?)\s*(?:#.*)?$", line))
    }
    if "engram_memory" not in existing:
        lines.insert(item_end, "    - engram_memory")


def update_profile(path: Path) -> None:
    """Apply the two stock-Hermes settings while preserving unrelated text."""
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()
    _ensure_memory(lines)
    _ensure_plugin(lines)
    updated = "\n".join(lines) + "\n"
    if updated != original:
        path.write_text(updated, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    update_profile(args.config)


if __name__ == "__main__":
    main()
