from __future__ import annotations

import pytest

from engram_mcp.server import build_server


def test_build_server_requires_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENGRAM_BASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="ENGRAM_BASE_URL is required"):
        build_server()


def test_build_server_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENGRAM_BASE_URL", "http://engram.test")
    monkeypatch.delenv("ENGRAM_API_KEY", raising=False)
    monkeypatch.delenv("ENGRAM_TIMEOUT", raising=False)

    server = build_server()

    assert server is not None
