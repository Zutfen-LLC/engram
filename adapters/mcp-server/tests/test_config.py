"""Configuration and startup-failure behavior.

The adapter must fail fast at ``build_server`` time when ``ENGRAM_BASE_URL`` is
unset, with an actionable message — not silently at the first tool call.
"""

from __future__ import annotations

import runpy

import pytest

from engram_mcp import server
from engram_mcp.server import build_server


def test_missing_base_url_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENGRAM_BASE_URL", raising=False)
    with pytest.raises(RuntimeError):
        build_server()


def test_missing_base_url_message_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The error must name the env var and give an example so operators can fix it."""
    monkeypatch.delenv("ENGRAM_BASE_URL", raising=False)
    with pytest.raises(RuntimeError) as exc_info:
        build_server()
    message = str(exc_info.value)
    assert "ENGRAM_BASE_URL" in message
    assert "http" in message  # hints at a URL


def test_config_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENGRAM_BASE_URL", "http://engram.example:9000")
    monkeypatch.setenv("ENGRAM_API_KEY", "eng_secret")
    monkeypatch.setenv("ENGRAM_TIMEOUT", "12.5")

    base_url, api_key, timeout = server._config()

    assert base_url == "http://engram.example:9000"
    assert api_key == "eng_secret"
    assert timeout == 12.5


def test_missing_api_key_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENGRAM_BASE_URL", "http://engram.test")
    monkeypatch.delenv("ENGRAM_API_KEY", raising=False)
    monkeypatch.delenv("ENGRAM_TIMEOUT", raising=False)

    _base_url, api_key, _timeout = server._config()
    assert api_key is None


def test_invalid_timeout_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENGRAM_BASE_URL", "http://engram.test")
    monkeypatch.setenv("ENGRAM_TIMEOUT", "not-a-number")

    _base_url, _api_key, timeout = server._config()
    assert timeout == server._DEFAULT_TIMEOUT


def test_injected_client_skips_env_config(
    monkeypatch: pytest.MonkeyPatch,
    mock_client,
) -> None:
    """build_server(client=...) must not require ENGRAM_BASE_URL."""
    monkeypatch.delenv("ENGRAM_BASE_URL", raising=False)
    # No RuntimeError: the injected client means env config is bypassed.
    mcp = build_server(client=mock_client)
    assert mcp is not None


def test_python_m_engram_mcp_dispatches_to_server_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``python -m engram_mcp`` must delegate to the real server entrypoint."""
    called: list[str] = []

    def fake_main() -> None:
        called.append("main")

    monkeypatch.setattr(server, "main", fake_main)
    runpy.run_module("engram_mcp", run_name="__main__", alter_sys=True)

    assert called == ["main"]
