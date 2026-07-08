# engram-hooks

Companion library that wires [Hermes](https://github.com/NousResearch/hermes-agent)
lifecycle hooks into [Engram](../../..) — the trustable institutional memory
service. It is the successor to the `zutfen_memory` plugin.

The split (per [design.md](../../../docs/design.md) §2, principle 8):

- **Classification intelligence is a service feature.** Engram owns
  `POST /v1/classify` and `POST /v1/remember`.
- **Lifecycle decisions are client-side.** *When* to extract a fact, *whether*
  to promote it or park it locally, and *what to reject at the write boundary*
  — those live here, because they need in-process visibility the service can't
  have.

## What it does

Three Hermes lifecycle events are mapped to hook entry points:

| Hermes event | Hook | Engram `source_type` | Purpose |
| --- | --- | --- | --- |
| `pre_compress` | `pre_compress()` | `pre_compress` | Extract facts about to be lost to context compression. |
| `sync_turn` | `sync_turn()` | `sync_turn` | Extract durable facts at the end of a turn. |
| `session_end` | `session_end()` | `extraction` | Final fact-extraction pass when a session closes. |

Each candidate flows through one pipeline:

```
candidate → write-boundary guard → (reject) drop
              │
           (allow)
              │
              ▼
        Engram classify → confidence ≥ threshold → remember (proposed)
              │
        confidence < threshold
              │
              ▼
        local volatile store (14-day retention, 2000-entry cap)
```

### Write-boundary guard

Every candidate — and every direct `memory()` write when the compat shim is
active — passes through `prepare_memory_write_guard`. It **actively rejects**
ambiguous and ephemeral candidates by returning `{"handled": True, "action":
"reject", ...}`. It never *passes through* (returning `None`), because that
would let the write proceed unchanged — the exact failure mode this library
exists to prevent.

Rejected categories:

- **Ephemeral** — cursor position, "currently editing", selection state, scroll
  position, undo/redo/paste. Stale within a turn.
- **Ambiguous** — "let me…", "maybe…", bare questions, bare code comments, very
  short strings. Not a durable fact.

### Volatile store

Low-confidence candidates (below `ENGRAM_HOOKS_PROMOTE_THRESHOLD`, default `0.6`)
park in a local JSONL file instead of hitting Engram. Defaults: 14-day
retention, 2000-entry cap, oldest evicted first. Recall is dumb substring
search — embeddings are the service's job.

## Install

From the repository root:

```bash
# preferred local-dev bootstrap (installs sibling SDK + both adapters into ./.venv)
bash scripts/setup-python-dev.sh

# direct adapter install if the sibling SDK is already installed in the target env
uv pip install --python .venv/bin/python -e adapters/engram-hooks
```

`engram-hooks` depends on `engram-client`, but the repo's canonical dev flow is
to install the sibling SDK into the same environment first via
`scripts/setup-python-dev.sh`. The adapter metadata deliberately uses the normal
package dependency name (`engram-client>=0.1.0`) instead of a relative `file:`
URL so editable installs do not fail during wheel metadata generation.

Hermes itself is **not** a dependency — the plugin loads on stock Hermes (or
with no Hermes installed at all, for testing).

## Configuration

All config is env-driven so the plugin works zero-config inside a container that
already exports `ENGRAM_*` vars (Hermes passes its MCP server `env:` block
through to spawned processes).

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `ENGRAM_BASE_URL` | yes¹ | — | Engram REST API base URL. |
| `ENGRAM_API_KEY` | no | — | Bearer token. |
| `ENGRAM_TIMEOUT` | no | `30` | Per-request timeout (s). |
| `ENGRAM_HOOKS_VOLATILE_PATH` | no | `$HERMES_DATA_DIR/engram-volatile.jsonl` (else `~/.hermes/…`, else temp dir) | Volatile store file. |
| `ENGRAM_HOOKS_VOLATILE_RETENTION_DAYS` | no | `14` | Volatile entry retention. |
| `ENGRAM_HOOKS_VOLATILE_CAP` | no | `2000` | Max volatile entries. |
| `ENGRAM_HOOKS_PROMOTE_THRESHOLD` | no | `0.6` | classify() confidence ≥ this → `remember` (proposed). |
| `ENGRAM_HOOKS_WORKSPACE` | no | — | Default workspace for writes. |
| `ENGRAM_HOOKS_COMPAT_SHIM` | no | `true` | Apply the `prepare_memory_write` compat shim on install. Set `false` to disable automatic capture entirely (lifecycle hooks/MCP still work). |
| `ENGRAM_HOOKS_REQUIRE_AUTOMATIC_CAPTURE` | no | `false` | If `true`, `install()` raises `AutomaticCaptureUnavailable` instead of degrading quietly when neither the native hook nor the compat shim ends up active. |

¹ If unset, the plugin still loads but parks every candidate in the volatile
store (no classify/remember). This is intentional graceful degradation.

## Usage

### As a Hermes plugin

```python
from engram_hooks import install, get_active_hooks, get_install_status

# At plugin load: build the engine, detect native prepare_memory_write vs.
# apply the compat shim, and return/log which path is active.
result = install()
status = get_install_status()  # same object as result["status"]
print(status.describe())
# "native prepare_memory_write active (provider=...)" or
# "compatibility shim active (patched=hermes_agent.tools.tool_executor, ...)" or
# "automatic capture DISABLED — <reason>"

# On each lifecycle event (wire to the Hermes lifecycle bus):
hooks = get_active_hooks()
result = await hooks.sync_turn(payload)
# HookResult(event='sync_turn', extracted=N, rejected=N, promoted=N, parked=N)
```

> Use `get_active_hooks()` rather than importing `ACTIVE_HOOKS` by name — the
> handle is rebound by `install()`, and a `from … import ACTIVE_HOOKS` would
> capture the pre-install value (`None`).

`install()` is idempotent: calling it again (e.g. a Hermes plugin-reload path)
re-detects and recognizes an already-patched dispatch site instead of
wrapping it a second time — see `_SHIM_MARKER` in `hooks.py`.

Set `ENGRAM_HOOKS_REQUIRE_AUTOMATIC_CAPTURE=true` to make `install()` raise
`AutomaticCaptureUnavailable` instead of returning when neither the native
hook nor the compat shim ends up active — for profiles where "engram-hooks is
loaded" is supposed to mean "automatic capture actually works," not "the
import didn't crash." See
[`docs/ops/hermes-dogfood-profile.md`](../../docs/ops/hermes-dogfood-profile.md)
for the full runbook, including the documented profile template at
[`profiles/hermes-engram-dogfood.yaml`](../../profiles/hermes-engram-dogfood.yaml)
and how to disable the shim if it causes trouble.

### Standalone (no Hermes)

```python
import asyncio
from engram_hooks import LifecycleHooks, HooksConfig

hooks = LifecycleHooks(HooksConfig(base_url="http://localhost:8000"))

async def main():
    await hooks.pre_compress("Always use lowercase table names.")
    print(hooks.volatile.search("table"))  # local recall
    await hooks.aclose()

asyncio.run(main())
```

### The guard directly

```python
from engram_hooks import prepare_memory_write_guard, is_allowed

verdict = prepare_memory_write_guard("currently editing line 42")
assert verdict["handled"] is True      # took ownership
assert verdict["action"] == "reject"   # actively rejected, not passed through
assert not is_allowed(verdict)
```

## Compatibility shim

The upstream `prepare_memory_write` hook ([PR
#59898](https://github.com/NousResearch/hermes-agent/pull/59898)) is **not** in
stock Hermes as of 2026-07-06. `install()` detects whether it exists on the
`MemoryProvider` ABC at load time:

- **Hook present** (PR merged) → registered natively, no patching.
- **Hook missing** → a ~20-line runtime monkey-patch wraps the `memory()`
  dispatch in `hermes_agent.tools.tool_executor` and
  `hermes_agent.runtime.agent_runtime_helpers` so the write-boundary guard runs
  before every native write. A clear warning is logged with the PR link.
- **Hermes not installed** → the shim is inactive; the lifecycle hooks still
  work standalone.

This makes Engram work with any Hermes version — no fork, no source editing.

```python
from engram_hooks import detect_prepare_memory_write

print(detect_prepare_memory_write())
# {'hermes_present': False, 'hook_present': False, 'provider': None, 'error': ...}
```

## Promotion gates

Routing mirrors [design.md](../../../docs/design.md) §4 source-trust defaults:

- `sync_turn` and `pre_compress` are low-trust inferred sources
  (`memory_confidence` 0.4 / 0.3). They stay `proposed` on the server until an
  LLM classification or human review raises their confidence above the 0.7
  auto-promotion gate.
- This library adds a **client-side** gate on top: candidates below
  `ENGRAM_HOOKS_PROMOTE_THRESHOLD` (default `0.6`) never reach the server at all
  — they park locally. This keeps chatty sources from flooding the proposed
  queue while still preserving potentially-useful observations for ~14 days.

## See also

- [Engram Python SDK](../../engram-client) — the underlying async client.
- [Engram REST API](../../../engram/api) — server-side route definitions.
- [Design doc](../../../docs/design.md) — trust model (§4) and what is/isn't in
  the service (§5).
