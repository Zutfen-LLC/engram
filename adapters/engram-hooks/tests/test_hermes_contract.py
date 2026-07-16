"""Pinned stock-Hermes general-plugin manifest compatibility contract."""
from __future__ import annotations

from pathlib import Path

import yaml

HERMES_REFERENCE_REPOSITORY = "NousResearch/hermes-agent"
HERMES_REFERENCE_SHA = "75467998f90ba87adf66e1254a4d163345f23a5f"
_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "hermes_plugin"
    / "engram_memory"
    / "plugin.yaml"
)


def test_manifest_matches_pinned_stock_general_plugin_contract() -> None:
    manifest = yaml.safe_load(_MANIFEST.read_text())
    assert HERMES_REFERENCE_REPOSITORY == "NousResearch/hermes-agent"
    assert len(HERMES_REFERENCE_SHA) == 40
    assert manifest == {
        "name": "engram_memory",
        "version": "0.2.0",
        "description": (
            "Engram integration for Hermes: safe current-turn evidence recall plus "
            "governed write and lifecycle capture."
        ),
        "kind": "standalone",
        "provides_hooks": [
            "pre_llm_call",
            "on_session_start",
            "on_session_reset",
            "on_session_finalize",
        ],
    }
