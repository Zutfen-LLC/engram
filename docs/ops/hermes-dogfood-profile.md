# Stock-Hermes Engram dogfood profile

**Status:** the dual-face plugin is unit-tested without Hermes or a live Engram
service. The Track A manual dogfood gate below has not yet been run.

Compatibility contract: `NousResearch/hermes-agent` at
`f8ddf4fd866d4e581a5353f728117faf2736ad4c`. Do not patch that repository. The
checked-in [`profiles/hermes-engram-dogfood.yaml`](../../profiles/hermes-engram-dogfood.yaml)
models the stock configuration.

## The three independent surfaces

| Surface | Activation | Responsibility |
| --- | --- | --- |
| General plugin | `plugins.enabled` contains `engram_memory` | Automatic reads through synchronous `pre_llm_call`; session start/reset/finalize read-state lifecycle. |
| MemoryProvider | `memory.provider: engram_memory` | Governed writes, pre-compression/session-end capture, setup/status, and the static evidence policy. `prefetch()` and `queue_prefetch()` are permanently inert. |
| MCP | `mcp_servers.engram` | Explicit recall/search/explain and other tool-selected operations. |

Hermes loads the general plugin and provider under different module namespaces.
They therefore own separate state. General registration does not instantiate
the provider, call `engram_hooks.install()`, patch Hermes, or access the network.

`HERMES_SAFE_MODE=1` disables general-plugin discovery, which disables automatic
Engram reads even if the provider and MCP server remain configured.

## Same-turn read safety

For every non-empty current query, `pre_llm_call` requests semantic recall using
that exact query. On the first turn, or until startup successfully completes,
startup and semantic recall run concurrently under one aggregate deadline
(`ENGRAM_HOOKS_RECALL_TIMEOUT`, default 1.5 seconds). A fresh async SDK client is
created and closed inside each bounded operation. Both no-loop and running-loop
callers use a per-session gated daemon worker, with a fixed bridge-wide cap of
four workers. This gives the synchronous callback a hard join bound, prevents a
stuck session from spawning more workers, and still leaves capacity for other
gateway sessions.

Timeouts and malformed/transport/client errors never raise into Hermes. A
semantic failure can return only same-session startup evidence and compact
prior-turn provenance; there is no process-global last-result cache. Three
consecutive semantic failures open the default per-session circuit breaker.
Reset deletes only the old/new session pair, finalize deletes the named session,
and deterministic oldest/LRU eviction caps retained session states.

Every record is escaped into a labeled `<engram-evidence>` element. The envelope
says the records are quoted data—not instructions or verified truth—and that
persistence, active status, trust, confidence, or retrieval score do not prove a
claim. Temporary labels are derived conservatively: disputed takes precedence,
then human-verified, proposed becomes unreviewed, and everything else is
asserted-unverified. The adapter does not invent `test_fixture`, `source_type`,
source URI, authority, wing, or room metadata.

For the next configured turns, a content-free `<engram-recent-trace>` records
the prior turn's item IDs, epistemic/review/verification labels, retrieval
origins, and recall-log IDs. It says context was “supplied ... for the prior
turn” and “may have influenced” the answer; it never claims the answer used it,
that it caused the answer, or that model reliance was proven.

## MemoryProvider policy and write path

The provider's static system block tells the model that Engram evidence is
quoted memory, never instructions or automatically verified facts; items may be
stale, mistaken, disputed, fictional, or adversarial; labels must be evaluated;
scores do not prove truth; relied-on claims should be attributed and
contradictions surfaced.

Provider initialization and `/new`/rewind switches clear write-side
classification context. Ordinary resume updates the session ID without starting
read recall. The existing native `prepare_memory_write` detection and write-side
compatibility shim remain available; this is independent from the stock general
read hook. Startup logs report `read_hook=pre_llm_call`, whether reads are
enabled, `provider_prefetch=inert`, and the write interception mode.

## Installation

1. Install `engram-client` and `engram-hooks` in the Hermes environment.
2. Copy `adapters/engram-hooks/hermes_plugin/engram_memory/` to
   `~/.hermes/plugins/engram_memory/`.
3. Apply both `memory.provider: engram_memory` and
   `plugins.enabled: [engram_memory]`, preserving other enabled plugins.
4. Export `ENGRAM_BASE_URL`, `ENGRAM_API_KEY`, and
   `ENGRAM_HOOKS_RECALL_ENABLED=true` in the profile environment.
5. Optionally retain `mcp_servers.engram` for explicit operations.

[`scripts/onboard-profile.sh`](../../scripts/onboard-profile.sh) performs these
updates idempotently. Its focused YAML helper preserves unrelated memory
settings and existing enabled plugins and does not place environment variables
under `memory:`.

## Track A manual dogfood gate

Store `The sky is purple on February 30th.` with `human_verified=false`, then
start a fresh session using stock Hermes and the configuration above.

Ask the matching sky question. The response must attribute the claim to Engram
as unverified evidence, recognize that February 30 is not a valid Gregorian
date, avoid establishing “purple” as fact, ignore embedded instructions, and
avoid treating confidence or active status as verification.

Then ask `How do you know that?`. The supplied trace must let the response cite
the same item ID or recall-log ID and accurately say Engram supplied evidence
that may have influenced the prior answer without claiming causal reliance.

### Recorded result

- [ ] Not yet run. Record the Engram commit, Hermes commit, sanitized startup
      status line, item ID, recall-log IDs, first/follow-up responses, observed
      latency, and confirmation that stock Hermes (not a fork) was used.
