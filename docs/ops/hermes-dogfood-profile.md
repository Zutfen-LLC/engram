# Hermes dogfood profile — activating engram-hooks (ENG-HERMES-001)

**Status:** engram-hooks (detection, compat shim, guard, install status) is
implemented and unit-tested in this repo. This document is the runbook for
wiring it into a real Hermes checkout and recording a manual smoke result —
that manual step is **not yet recorded** here; do not treat this file as
end-to-end verification until the checklist at the bottom is filled in.

## What this activates

Before this change, the dogfood Hermes profile:

- used `memory.provider=zutfen_memory`, and
- only registered the Engram MCP server for explicit tool calls.

`engram-hooks` (the compatibility shim, guard, and lifecycle hooks) existed in
this repo but nothing in the Hermes profile ever imported or called it — the
monkey-patch was inert.

This slice makes engram-hooks the memory path Hermes actually loads. See
[`profiles/hermes-engram-dogfood.yaml`](../../profiles/hermes-engram-dogfood.yaml)
for the documented profile variant (this repo does not own the real Hermes
profile store, so that file is a copy-paste template, not a live config).

## The three startup paths

`engram_hooks.install()` always ends up in exactly one of these states —
never a silent fourth option:

| State | Condition | What happens |
| --- | --- | --- |
| **native** | Hermes' `MemoryProvider` ABC has `prepare_memory_write` | No patch applied. `install_status.native_hook_available == True`. |
| **compat shim** | Hook absent, `ENGRAM_HOOKS_COMPAT_SHIM=true` (default) | `hermes_agent.tools.tool_executor` and `hermes_agent.runtime.agent_runtime_helpers` are patched. `install_status.compat_shim_installed == True`, `patched_modules` lists both. |
| **disabled / failed** | Hook absent and (shim disabled, Hermes absent, or patch targets missing) | Nothing is patched. `install_status.automatic_capture_active == False`, `failure_reason` explains why. |

Check the state programmatically instead of grepping logs:

```python
from engram_hooks import get_install_status

status = get_install_status()
print(status.describe())               # one-line human summary
print(status.automatic_capture_active)  # the only boolean that matters
```

The same information is logged at startup by `install()` at `INFO` (native or
shim active) or `ERROR` (patch failed) level, tagged `engram_hooks`.

### Failing loudly on purpose

Set `ENGRAM_HOOKS_REQUIRE_AUTOMATIC_CAPTURE=true` in the profile once you want
Hermes to refuse to start rather than silently run without automatic capture:
`install()` raises `engram_hooks.AutomaticCaptureUnavailable` (with the same
`failure_reason` text) instead of returning. Leave it `false` (default) for
environments where explicit MCP dogfooding is an acceptable fallback.

## Disabling the compat shim

If the monkey-patch ever misbehaves against a real Hermes checkout, disable
it without touching code:

```bash
export ENGRAM_HOOKS_COMPAT_SHIM=false
```

`install()` then reports `automatic_capture_active=False` with
`failure_reason="compat shim disabled (ENGRAM_HOOKS_COMPAT_SHIM=false)"` and
leaves Hermes' `memory()` dispatch completely untouched. The three lifecycle
hooks (`pre_compress`/`sync_turn`/`session_end`) and explicit MCP tools keep
working regardless — only the `memory()` interception is affected.

## Automated coverage (runs in CI, no Hermes checkout required)

`adapters/engram-hooks/tests/` builds a fake `hermes_agent` package tree in
`sys.modules` (see `tests/conftest.py`) so the shim's behavior is fully
testable without installing real Hermes:

```bash
pytest -q adapters/engram-hooks/tests
```

Covers: hook detection (present/absent/no-Hermes), compat shim patch
application, guard-reject short-circuiting the original writer, guard-allow
reaching the original writer, `install()`/`install_compat_shim()` idempotency
(no double-wrap), patch failure on missing dispatch sites, and
`require_automatic_capture` fail-loud behavior.

`tests/test_profile_fixture.py` also asserts the checked-in
`profiles/hermes-engram-dogfood.yaml` stays consistent with what `install()`
actually does (references `engram_hooks:install`, keeps `mcp_servers.engram`,
doesn't hardcode `zutfen_memory`) — a lightweight guard against the profile
and the code drifting apart.

## Manual dogfood smoke test (against a real Hermes checkout)

This is the step that proves automatic capture end-to-end, not just unit
tests. Run it against a real Hermes install pointed at the deployed Engram
instance (see `docs/ops/dogfood-verification.md` for the live instance).

1. `pip install -e adapters/engram-hooks`
2. Copy the relevant blocks from `profiles/hermes-engram-dogfood.yaml` into
   your Hermes profile; set `ENGRAM_BASE_URL` / `ENGRAM_API_KEY`.
3. Start Hermes. Confirm the startup log has an `engram_hooks` line reading
   either `native prepare_memory_write active` or `compatibility shim active`.
4. Trigger a Hermes action that should produce a memory write with clear
   durable content (e.g. "always deploy from the release branch, never
   main").
5. Verify via MCP (`engram_search`/`engram_recall`) or `GET /v1/recall` that
   the write reached Engram.
6. Trigger a write with ephemeral/ambiguous content (e.g. "currently editing
   line 42").
7. Verify it does **not** appear in Engram — the guard rejected it before it
   reached the write path.
8. Record the sanitized result below (mirror the format used in
   `docs/ops/dogfood-verification.md`).

### Recorded result

- [ ] Not yet run against a real Hermes checkout. `engram-hooks` is
      implemented and unit-tested (32 tests, `pytest -q
      adapters/engram-hooks/tests`); the manual smoke above is outstanding.
      Update this checklist (date, Hermes commit, startup log excerpt,
      accepted/rejected write outcomes) once it's been exercised for real —
      do not check items above off speculatively.
