# engram-hooks

Companion library and one installable plugin directory that wire stock
[Hermes](https://github.com/NousResearch/hermes-agent) into
[Engram](../../..). Compatibility is pinned to stock Hermes commit
`36f2a966c7f9f69987494b867c3dcf96b69a5766`; no Hermes source patch or fork is
required.

The installed `~/.hermes/plugins/engram_memory/` directory has two independently
loaded faces:

- The **general plugin** owns reads. Its synchronous `pre_llm_call` callback
  performs bounded current-query semantic recall, first-turn startup recall,
  safe evidence rendering, per-session circuit breaking, and compact follow-up
  provenance. Stock Hermes appends this context directly to the current user
  turn.
- The **MemoryProvider** owns write interception and lifecycle capture. Its
  `prefetch()` and `queue_prefetch()` methods are permanent no-ops because stock
  Hermes wraps that path as generic authoritative-reference data.
- The optional **MCP server** remains the explicit interface for search, recall,
  explain, and other user/model-selected operations.

The split (per [design.md](../../../docs/design.md) §2, principle 8):

- **Classification intelligence is a service feature.** Engram owns
  `POST /v1/classify` and `POST /v1/remember`.
- **Lifecycle decisions are client-side.** *When* to extract a fact, *whether*
  to promote it or park it locally, and *what to reject at the write boundary*
  — those live here, because they need in-process visibility the service can't
  have.

## What it does

### Same-turn read path

The general plugin registers exactly `pre_llm_call`, `on_session_start`,
`on_session_reset`, and `on_session_finalize`. On each non-empty current query,
it calls `POST /v1/recall` in semantic mode. The first turn (or a session whose
startup recall has not successfully completed) also starts startup recall;
both requests run concurrently under one aggregate deadline. Results are
normalized into immutable evidence records and rendered as escaped
`<engram-evidence>` quoted-data blocks. Retrieval errors produce less context,
never a stale semantic result from another query.

The read bridge is keyed only by Hermes `session_id`, generation-checks
concurrent turns, caps retained sessions with deterministic LRU eviction, and
opens a per-session breaker after repeated semantic failures. It retains only
content-free item/log provenance for the configured follow-up window. A
per-session gated daemon worker bridges Hermes' synchronous callback to the
async SDK for both ordinary and already-running-event-loop callers. The bridge
has a fixed four-worker cap, so a suspected stuck operation cannot accumulate
threads or block interpreter exit while unrelated gateway sessions retain
bounded capacity.

`ENGRAM_HOOKS_RECALL_ENABLED=false` (the default) makes all four general read
hooks fast no-ops. `HERMES_SAFE_MODE=1` prevents Hermes from loading general
plugins at all, so it also disables automatic Engram reads.

### Write and lifecycle path

Three Hermes lifecycle events are mapped to hook entry points:

| Hermes event | Hook | Engram `source_type` | Purpose |
| --- | --- | --- | --- |
| `pre_compress` | `pre_compress()` | `pre_compress` | Extract facts about to be lost to context compression. |
| `sync_turn` | `sync_turn()` | `sync_turn` | Extract durable facts at the end of a turn. |
| `session_end` | `session_end()` | `session_end` | Final fact-extraction pass when a session closes. |

Each candidate flows through one pipeline:

```
candidate → write-boundary guard → (reject) drop
              │
           (allow)
              │
              ▼
        Engram classify → retain + retention confidence ≥ threshold → remember (proposed)
              │
        transient / uncertain / retain below threshold
              │
              ▼
        local volatile store (14-day retention, 2000-entry cap)
```

### Write-boundary guard

Every candidate — and every direct `memory()` add when the compat shim is
active — passes through `prepare_memory_write_guard`. Direct rejected adds
return a stock-Hermes-safe JSON error and never reach the native writer.
Accepted adds invoke the active provider's governed callback exactly once and
return replacement JSON with `provider: "engram"` and `native_write: false`.
Any batch containing an add is rejected atomically; replace/remove-only calls
retain stock behavior for this focused compatibility slice.

Rejected categories:

- **Ephemeral** — cursor position, "currently editing", selection state, scroll
  position, undo/redo/paste. Stale within a turn.
- **Ambiguous** — "let me…", "maybe…", bare questions, bare code comments, very
  short strings. Not a durable fact.

### Volatile store

Candidates without sufficient durable-retention evidence (below
`ENGRAM_HOOKS_STORE_THRESHOLD`, default `0.65`)
park in a local JSONL file instead of hitting Engram. Defaults: 14-day
retention, 2000-entry cap, oldest evicted first. Recall is dumb substring
search — embeddings are the service's job.

## Install

For an existing Hermes installation and an **already-provisioned Engram agent
key**, use the standalone installer:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/Zutfen-LLC/engram/main/scripts/install-hermes.sh \
  | bash
```

The installer discovers the active profile and live Hermes Python environment
through the Hermes CLI. Key entry is masked and read from `/dev/tty`, so it
does not enter shell history; non-interactive automation can set
`ENGRAM_API_KEY` in the process environment. The default service is
`https://api.engram.zutfen.com`.

Options can be passed to the piped script with `bash -s --`:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/Zutfen-LLC/engram/main/scripts/install-hermes.sh \
  | bash -s -- --profile work --base-url https://engram.example.com \
      --ref main --dry-run
```

`--profile` targets one named profile, `--base-url` selects the service,
`--ref` selects the update channel, and `--dry-run`
prints a sanitized plan without prompting, network access, installation, or
writes. Omit `--dry-run` to install. Before installing anything, the installer
fetches the requested branch, tag, or SHA once, resolves it to an immutable
40-character commit, and uses that exact commit for both Python packages and
the detached plugin checkout. It reports both the requested ref and resolved
commit. Rerunning authenticates again, resolves the channel again, upgrades the
same direct Git dependencies, force-reinstalls the canonical nested plugin,
keeps unrelated plugins/configuration, and consolidates Engram `.env` entries.
Fully exit and relaunch an interactive Hermes CLI afterward; for an installed
gateway, run `hermes gateway restart`.

Once a release is cut, production installations should pin both the fetched
installer and its dependency/plugin ref to that release (replace
`<release-tag>` with a real tag):

```bash
curl -fsSL \
  https://raw.githubusercontent.com/Zutfen-LLC/engram/<release-tag>/scripts/install-hermes.sh \
  | bash -s -- --ref <release-tag>
```

This flow never creates a principal or mints a key. By contrast,
[`scripts/onboard-profile.sh`](../../scripts/onboard-profile.sh) accepts a
user-level key and calls `/v1/agents` to create a new agent principal and scoped
key.

For repository development instead, install the editable packages locally:

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

Hermes itself is **not** a package dependency. The plugin can load without
Hermes for testing; stock-Hermes interception is verified only at the pinned
commit above.

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
| `ENGRAM_HOOKS_STORE_THRESHOLD` | no | `0.65` | `retain` disposition at/above this retention confidence → remember as proposed. |
| `ENGRAM_HOOKS_PROMOTE_THRESHOLD` | no | `0.65` | Deprecated fallback name, used only when the canonical variable is absent. |
| `ENGRAM_HOOKS_WORKSPACE` | no | — | Default workspace for writes. |
| `ENGRAM_HOOKS_COMPAT_SHIM` | no | `true` | Apply the `prepare_memory_write` compat shim on install. Set `false` to disable automatic capture entirely (lifecycle hooks/MCP still work). |
| `ENGRAM_HOOKS_REQUIRE_AUTOMATIC_CAPTURE` | no | `false` | If `true`, `install()` raises `AutomaticCaptureUnavailable` instead of degrading quietly when neither the native hook nor the compat shim ends up active. |
| `ENGRAM_HOOKS_RECALL_ENABLED` | no | `false` | Enable safe automatic reads through the general `pre_llm_call` hook. |
| `ENGRAM_HOOKS_RECALL_TIMEOUT` | no | `1.5` | Aggregate synchronous read deadline in seconds (clamped to `0.1`–`10.0`); independent of `ENGRAM_TIMEOUT`. |
| `ENGRAM_HOOKS_RECALL_ITEM_BUDGET` | no | `5` | Local and semantic item cap (clamped to `1`–`20`). |
| `ENGRAM_HOOKS_RECALL_BYTE_BUDGET` | no | `8192` | Byte budget sent to startup/semantic recall. |
| `ENGRAM_HOOKS_RECALL_MAX_CONTEXT_BYTES` | no | `12000` | Hard UTF-8 cap for the rendered evidence and trace envelope. |
| `ENGRAM_HOOKS_RECALL_FOLLOWUP_TURNS` | no | `3` | Later turns that receive compact item/log provenance (`0`–`10`). |
| `ENGRAM_HOOKS_RECALL_BREAKER_FAILURES` | no | `3` | Consecutive semantic failures that open a breaker for that session. |
| `ENGRAM_HOOKS_RECALL_MAX_SESSIONS` | no | `512` | Maximum read-side session states retained by a plugin module instance. |

¹ If unset, the plugin still loads but parks every candidate in the volatile
store (no classify/remember). This is intentional graceful degradation.

## Usage

### As a Hermes plugin

The standalone installer uses Hermes' native plugin manager to install this
nested plugin and configures both independently loaded faces:

```yaml
memory:
  provider: engram_memory
plugins:
  enabled:
    - engram_memory
```

Selecting only `memory.provider` does not enable automatic reads. The provider
supplies a static system-prompt interpretation policy, but the dynamic evidence
envelope remains self-contained and safe when another provider is selected.

The following library API describes the provider's write compatibility path;
general-plugin registration calls neither `install()` nor any monkeypatch:

```python
from engram_hooks import get_active_hooks, get_install_status, install

# The EngramMemoryProvider does this during construction, registering its
# governed prepare callback explicitly. A standalone install without a provider
# callback deliberately cannot claim automatic writes are active.
result = install(write_interceptor=provider.prepare_memory_write)
status = get_install_status()  # same object as result["status"]
print(status.describe())
# "native prepare_memory_write active (provider=...)" or
# "compatibility shim active (patched=tools.memory_tool)" or
# "recall_only: automatic writes INACTIVE — <reason>" or
# "incompatible: automatic writes INACTIVE — <reason>"

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
wrapping it a second time. The active provider callback is owned by the
surviving wrapper, so even a full `engram_hooks.hooks` module replacement uses
the newest provider instance. Disabling compatibility, or reinstalling without
a provider callback, restores the native boundary rather than retaining a
stale provider — see `_SHIM_MARKER` in `hooks.py`.

`EngramMemoryProvider` does not call `install()` during construction,
discovery, or `hermes memory status`. Stock Hermes activates interception only
when it calls the selected provider's `initialize()` for an agent session.

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
stock Hermes as of 2026-07-06. During selected-provider initialization,
`install()` detects whether it exists on the `MemoryProvider` ABC:

- **Hook present** (PR merged) → registered natively, no patching.
- **Hook missing at the pinned stock revision** → the compatibility shim wraps
  the shared `tools.memory_tool.memory_tool` function that both stock execution
  paths late-import. The write-boundary guard can then return a successful
  replacement result before native persistence. A clear warning identifies the
  exact inspected contract and links to the upstream PR.
- **Hermes not installed** → the shim is inactive; the lifecycle hooks still
  work standalone.

Compatibility is proven only for stock Hermes commit
`36f2a966c7f9f69987494b867c3dcf96b69a5766`. A different Hermes revision must
be inspected and pinned before claiming compatibility; API-shape drift fails
required automatic-capture activation loudly. No fork or source editing is
required for the pinned revision.

```python
from engram_hooks import detect_prepare_memory_write

print(detect_prepare_memory_write())
# {'hermes_present': False, 'hook_present': False, 'provider': None, 'error': ...}
```

## Durable-storage gate

Routing mirrors [design.md](../../../docs/design.md) §4 source-trust defaults:

- `sync_turn` and `pre_compress` are low-trust inferred sources
  (`memory_confidence` 0.4 / 0.3). Remembered candidates remain `proposed` for
  later evidence scoring or human review.
- This library applies a client-side durable-storage gate: candidates below
  `ENGRAM_HOOKS_STORE_THRESHOLD` (default `0.65`) never reach the server and
  park locally. The deprecated `ENGRAM_HOOKS_PROMOTE_THRESHOLD` remains a
  fallback alias. The canonical variable takes precedence, and an explicit
  threshold of `0` is honored.

## See also

- [Engram Python SDK](../../engram-client) — the underlying async client.
- [Engram REST API](../../../engram/api) — server-side route definitions.
- [Design doc](../../../docs/design.md) — trust model (§4) and what is/isn't in
  the service (§5).
