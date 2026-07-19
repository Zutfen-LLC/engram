# Stock Hermes write contract fixture

- Repository: `NousResearch/hermes-agent`
- Revision: `36f2a966c7f9f69987494b867c3dcf96b69a5766`
- Engram work-start revision: `906fc1d30128f49d4653c94688f08bde5b0c65b0`
- Inspected upstream files:
  - `agent/tool_executor.py` (memory branch around lines 1296–1322)
  - `agent/agent_runtime_helpers.py` (memory branch around lines 2456–2479)
  - `tools/memory_tool.py` (`memory_tool`, lines 959–1027)
  - `agent/memory_manager.py` (`notify_memory_tool_write`, lines 1063–1120)
  - `agent/memory_provider.py` (`on_memory_write`, lines 280–294)
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
