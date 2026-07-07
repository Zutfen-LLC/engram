"""MCP server adapter exposing Engram memory tools (T17).

Public API::

    from engram_mcp import build_server, main
"""

from __future__ import annotations

from .server import build_server, main

__all__ = ["build_server", "main"]
