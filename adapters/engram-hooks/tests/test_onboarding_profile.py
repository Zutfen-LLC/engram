"""Idempotence and preservation tests for the focused YAML editor."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

_HELPER = Path(__file__).resolve().parents[3] / "scripts" / "update-hermes-profile.py"


def test_profile_editor_preserves_unrelated_settings_and_plugins(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """name: existing
memory:
  provider: builtin
  memory_enabled: false
plugins:
  enabled:
    - calculator
model:
  default: example/model
"""
    )
    subprocess.run([sys.executable, str(_HELPER), str(config)], check=True)
    first = config.read_text()
    subprocess.run([sys.executable, str(_HELPER), str(config)], check=True)
    assert config.read_text() == first

    parsed = yaml.safe_load(first)
    assert parsed["memory"] == {"provider": "engram_memory", "memory_enabled": False}
    assert parsed["plugins"]["enabled"] == ["calculator", "engram_memory"]
    assert parsed["model"]["default"] == "example/model"


def test_profile_editor_adds_missing_sections(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("name: minimal\n")
    subprocess.run([sys.executable, str(_HELPER), str(config)], check=True)
    parsed = yaml.safe_load(config.read_text())
    assert parsed["memory"]["provider"] == "engram_memory"
    assert parsed["plugins"]["enabled"] == ["engram_memory"]


def test_profile_editor_preserves_inline_enabled_plugins(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("plugins:\n  enabled: [calculator, alerts] # keep\n")
    subprocess.run([sys.executable, str(_HELPER), str(config)], check=True)
    subprocess.run([sys.executable, str(_HELPER), str(config)], check=True)
    parsed = yaml.safe_load(config.read_text())
    assert parsed["plugins"]["enabled"] == ["calculator", "alerts", "engram_memory"]
    assert "# keep" in config.read_text()
