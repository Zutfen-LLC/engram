# Stock Hermes write contract fixture

- Repository: `NousResearch/hermes-agent`
- Revision: `75467998f90ba87adf66e1254a4d163345f23a5f`
- Engram work-start revision: `f9fb2ee1f94103d2aa4f0c36956e6f6925a1dfa0`
- Inspected upstream files:
  - `agent/tool_executor.py` (memory branch around lines 1294–1317)
  - `agent/agent_runtime_helpers.py` (memory branch around lines 2348–2371)
  - `tools/memory_tool.py` (`memory_tool`, lines 959–1027)
  - `agent/memory_manager.py` (`notify_memory_tool_write`, lines 1042–1099)
  - `agent/memory_provider.py` (`on_memory_write`, lines 280–294)
  - `hermes_cli/plugins.py` (`resolve_pre_tool_block`, lines 2228–2280)

The executable fixture preserves the compatibility-relevant shape: both agent
paths define nested execution closures, import `tools.memory_tool.memory_tool`
inside those closures at call time, pass the six stock arguments, and notify
the memory manager only after the tool returns. The store is intentionally
minimal; it records native mutations so tests can prove interception happens
before native persistence. The general `pre_tool_call` hook was rejected as
the implementation path because stock Hermes can only turn its directive into
a blocked/error result, not a successful replacement tool result.
