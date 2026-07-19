"""Pinned stock-Hermes general-plugin manifest compatibility contract."""
from __future__ import annotations

from pathlib import Path

import yaml

HERMES_REFERENCE_REPOSITORY = "NousResearch/hermes-agent"
HERMES_REFERENCE_SHA = "36f2a966c7f9f69987494b867c3dcf96b69a5766"
_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "hermes_plugin"
    / "engram_memory"
    / "plugin.yaml"
)
_PROVENANCE = (
    Path(__file__).parent
    / "fixtures"
    / "hermes_stock_36f2a966"
    / "PROVENANCE.md"
)


def test_stock_write_fixture_records_exact_source_provenance() -> None:
    provenance = _PROVENANCE.read_text()
    assert f"Revision: `{HERMES_REFERENCE_SHA}`" in provenance
    assert "Engram work-start revision: `906fc1d30128f49d4653c94688f08bde5b0c65b0`" in provenance
    for source_path in (
        "agent/tool_executor.py",
        "agent/agent_runtime_helpers.py",
        "tools/memory_tool.py",
        "agent/memory_manager.py",
        "agent/memory_provider.py",
        "hermes_cli/plugins.py",
    ):
        assert source_path in provenance


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
