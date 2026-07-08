"""Guards the checked-in Hermes dogfood profile against drifting from the code.

The real Hermes profile/config store lives outside this repo (per
ENG-HERMES-001's handoff notes), so ``profiles/hermes-engram-dogfood.yaml`` is
a documented template, not something Hermes reads directly in CI. This test
is the "local test fixture that models the profile wiring" the task packet
calls for: it doesn't run Hermes, but it does assert the template says what
the code actually does, so a future refactor (e.g. renaming ``install`` or
dropping the MCP server block) fails a test instead of silently rotting a
runbook nobody re-reads.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_PROFILE_PATH = (
    Path(__file__).resolve().parents[3] / "profiles" / "hermes-engram-dogfood.yaml"
)


def _load_profile() -> dict:
    return yaml.safe_load(_PROFILE_PATH.read_text())


def test_profile_fixture_exists() -> None:
    assert _PROFILE_PATH.is_file(), (
        f"expected the documented Hermes dogfood profile at {_PROFILE_PATH}"
    )


def test_profile_loads_engram_hooks_install_entrypoint() -> None:
    profile = _load_profile()
    plugins = profile.get("plugins", [])
    entrypoints = [p.get("entrypoint") for p in plugins]
    assert "engram_hooks:install" in entrypoints, (
        "profile must call engram_hooks:install() at startup — the whole point "
        "of this slice is that the shim is no longer inert"
    )


def test_profile_no_longer_uses_zutfen_memory() -> None:
    profile = _load_profile()
    assert profile.get("memory", {}).get("provider") != "zutfen_memory"


def test_profile_keeps_mcp_server_registration() -> None:
    profile = _load_profile()
    assert "engram" in profile.get("mcp_servers", {}), (
        "MCP must remain the manual dogfooding interface — must_not_do forbids "
        "removing mcp_servers.engram"
    )


def test_profile_defaults_compat_shim_on_and_hard_fail_off() -> None:
    profile = _load_profile()
    env = profile.get("env", {})
    assert env.get("ENGRAM_HOOKS_COMPAT_SHIM") == "true"
    # require_automatic_capture defaults off so the profile can still start
    # (and fall back to explicit MCP dogfooding) if Hermes' internals drift.
    assert env.get("ENGRAM_HOOKS_REQUIRE_AUTOMATIC_CAPTURE") == "false"
