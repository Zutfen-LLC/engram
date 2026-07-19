# Stock Hermes write contract fixture

- Repository: `NousResearch/hermes-agent`
- Revision: `36f2a966c7f9f69987494b867c3dcf96b69a5766`
- Engram work-start revision: `906fc1d30128f49d4653c94688f08bde5b0c65b0`
- Discovery correction work-start revision: `85d3b67e61511b0c181f8e8c21704d36c333fa1a`
- Inspected upstream files:
  - `agent/tool_executor.py` (memory branch around lines 1296–1322)
  - `agent/agent_runtime_helpers.py` (memory branch around lines 2456–2479)
  - `tools/memory_tool.py` (`memory_tool`, lines 959–1027)
  - `agent/memory_manager.py` (`notify_memory_tool_write`, lines 1063–1120)
  - `agent/memory_manager.py` (`initialize_all`, lines 1214–1231)
  - `agent/memory_provider.py` (`on_memory_write`, lines 280–294)
  - `agent/agent_init.py` (provider load/availability/`initialize_all`, lines 1448–1514)
  - `plugins/memory/__init__.py` (`discover_memory_providers`,
    `load_memory_provider`, and `_load_provider_from_dir`, lines 147–343)
  - `hermes_cli/memory_setup.py` (`_get_available_providers` and `cmd_status`,
    lines 159–194 and 417–475)
  - `hermes_cli/plugins.py` (`resolve_pre_tool_block`, lines 2228–2280)

The executable fixture preserves the compatibility-relevant shape: both agent
paths define nested execution closures, import `tools.memory_tool.memory_tool`
inside those closures at call time, pass the six stock arguments, and notify
the memory manager only after the tool returns, including the lazy provenance
metadata callback used by this revision. The store is intentionally minimal;
it records native mutations so tests can prove interception happens before
native persistence. The general `pre_tool_call` hook was rejected as the
implementation path because stock Hermes can only turn its directive into a
blocked/error result, not a successful replacement tool result.

The discovery/status fixture is a faithful extraction of the pinned loader and
CLI paths. It preserves the two constructor calls (discovery and explicit
load), swallowed constructor failures, filtering in `_get_available_providers`,
and the resulting `Plugin: NOT installed` status branch. The production startup
path is recorded above: `agent_init.py` loads and registers the selected
provider, then `MemoryManager.initialize_all()` calls `provider.initialize()`.
