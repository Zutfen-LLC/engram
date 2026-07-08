"""Package entrypoint for ``python -m engram_mcp``.

Hermes and other MCP clients sometimes launch stdio adapters via ``python -m``
instead of the installed console script. Keep this thin wrapper so both
``python -m engram_mcp`` and ``python -m engram_mcp.server`` work.
"""

from __future__ import annotations

from .server import main

if __name__ == "__main__":
    main()
