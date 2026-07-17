# Engram

> Trustable memory infrastructure for AI agents, assistants, and teams.

Engram is a standalone memory service that gives AI systems structured, durable, and **trustable** memory. It works for a single long-running assistant, and it is designed for the harder case: multiple agents sharing memory without corrupting, overwriting, or blindly trusting each other.

It's not a flat key-value memory store. Engram is a memory system of record with taxonomy, relationships, temporal validity, review states, provenance, conflict detection, and explainable recall.

An **engram** is the physical trace a memory leaves in brain tissue — the literal substrate of stored memory.

## Why Engram?

Most AI memory layers answer the first-order question:

> Can we store and recall facts?

Engram is built around the harder and more important question:

> Can an AI system trust what it remembers, know where it came from, know whether it is still true, and safely act on it?

That matters for a single assistant remembering a user's preferences across sessions. It matters even more for coding agents, research agents, support agents, operations agents, and multi-agent teams that share institutional knowledge.

Engram is designed as **trustable memory infrastructure**, not just storage.

A lower-authority source can never silently replace a higher-authority memory.

| Feature           | Flat memory stores | Engram                                                                         |
| ----------------- | ------------------ | ------------------------------------------------------------------------------ |
| Memory model      | Flat facts         | Structured taxonomy with wings and rooms                                       |
| Trust model       | Minimal or none    | Review states: proposed → active → disputed → resolved                         |
| Relationships     | Usually none       | Knowledge graph with temporal validity                                         |
| Single-agent use  | Basic recall       | Durable assistant memory with provenance, confidence, and lifecycle            |
| Multi-agent use   | Per-agent silos    | Workspaces, visibility levels, and shared recall                               |
| Conflict handling | Dedup only         | Write-time contradiction detection and resolution                              |
| Provenance        | Minimal            | Source trust, extraction model, subject, verification tracking                 |
| Audit trail       | Overwrites         | Append-first content plus audited metadata events                              |
| Classification    | Basic extraction   | LLM-backed and rule-based classification, tenant-configurable, no-LLM fallback |
| Recall quality    | Similarity only    | Scored ranking with "why recalled" explanations                                |
| Self-hostable     | Varies             | Docker Compose, one command                                                    |

## Who Engram Is For

Engram can be used anywhere an AI system needs durable, inspectable memory.

### Single assistant / single user

Use Engram when one assistant needs to remember things safely across sessions:

* user preferences
* project context
* standing instructions
* important decisions
* recurring workflows
* constraints and invariants
* long-term personal or professional context

A single-agent setup still benefits from Engram's trust model: source authority, confidence, review state, provenance, staleness, and explainable recall.

### Coding agents

Use Engram to give coding agents persistent project memory:

* architectural decisions
* repo conventions
* completed backlog items
* known gotchas
* deployment notes
* test expectations
* prior review decisions
* "do not repeat this mistake" context

### Multi-agent teams

Use Engram when multiple agents need to share memory without creating chaos:

* workspace-level shared knowledge
* private agent notes
* tenant-wide organizational memory
* conflict detection between competing claims
* authority-aware supersession
* memory review and promotion
* audited updates instead of silent overwrites

This is where Engram's design is strongest: single-agent memory is the on-ramp; multi-agent institutional memory is the moat.

## Quickstart

```bash
git clone https://github.com/Zutfen-LLC/engram.git
cd engram
cp .env.example .env  # set your passwords
docker compose up -d
```

This starts Postgres 16 with pgvector, the Engram API service, and a background worker that processes the job queue (embeddings, classification, conflict detection, recall telemetry). The schema migrates automatically on first boot.

Verify that the service is running:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

Create the first API key (auth is off by default for local dev; enable it for production). The printed key uses the `eng_<key_id>_<secret>` format and is shown only once:

```bash
docker compose exec engram-service engram bootstrap-key
```

For the full walkthrough — auth enablement, backup/restore, upgrades, embeddings,
and troubleshooting — see **[`docs/deployment.md`](docs/deployment.md)**.

See `docs/design.md` for the full architecture.

## Local Python development setup

If you want the repo's `.venv` to be able to import the sibling SDK plus both
adapters directly (so `python -m engram_mcp` and `import engram_hooks` work
without setting `PYTHONPATH`),
run:

```bash
bash scripts/setup-python-dev.sh
# or: make setup-python-dev
```

This bootstraps `./.venv`, then installs these editable local packages into it:

- `sdk/engram-client`
- `adapters/mcp-server`
- `adapters/engram-hooks`

After that, these commands work from the repo checkout:

```bash
.venv/bin/python -m engram_mcp
.venv/bin/engram-mcp
.venv/bin/python -c 'import engram_hooks'
```

## Key Concepts

### Memory Lifecycle

Every memory moves through a review pipeline:

```text
written → proposed → active → disputed → resolved → superseded/archived
```

* **Proposed** memories do not enter startup recall until reviewed or promoted.
* **Active** memories are trusted enough for normal recall.
* **Disputed** memories remain available with warnings where appropriate.
* **Archived** and superseded memories are preserved for audit but excluded from default recall.

Auto-promotion has independent legacy-confidence and server-attested retention-evidence lanes.
The evidence lane uses `min(0.85, 0.20 * source_confidence_prior + 0.80 *
retention_confidence)` for governed kinds only; it changes only `proposed → active`,
never provenance, authority, confidence, or human-verification fields.

`POST /v1/recall` with `mode=startup` runs a bounded, tenant-scoped promotion pass automatically before building the working set (capped at `settings.startup_promotion_limit`, default 20 proposed items per call) — no separate trigger needed for day-to-day recall. For full sweeps of a large proposed backlog, wire the CLI to cron/systemd, or call the admin endpoint on demand:

```bash
# All tenants. Runs as the owner role (bypasses RLS) via ENGRAM_OWNER_DATABASE_URL:
engram promote-proposed

# Single tenant, capped at 1000 candidates:
engram promote-proposed --tenant <tenant-id> --limit 1000

# Exact evaluation with no writes, audit events, or actor creation:
engram promote-proposed --dry-run
```

```text
POST /v1/admin/promote
```

All three entry points (lazy startup recall, CLI, admin endpoint) share one promotion service function and the same gates — none can drift from the others.

Promotion returns per-reason counts:

```text
scanned
promoted
skipped_confidence
skipped_age
skipped_conflict
skipped_dispute            # blocked by another principal's dispute/negative feedback
skipped_conflict_recheck   # blocked by a promotion-time conflict recheck
skipped_disabled
skipped_kind_policy
skipped_evidence_disabled
skipped_no_retention_evidence
skipped_missing_source_prior
skipped_retention_disposition
skipped_taxonomy_confidence
skipped_evidence_score
skipped_evidence_version
skipped_evidence_inconsistent
skipped_review_policy
```

Thresholds come from `tenant_config`:

```text
auto_promote_enabled
auto_promote_evidence_enabled       # existing tenants migrate as false
auto_promote_evidence_threshold     # default 0.70
auto_promote_confidence_threshold
auto_promote_min_age_hours
```

### Trust Model

Trust is not binary. Every memory carries:

* **source_trust** — trust in where the memory came from
* **source_confidence_prior** — immutable write-time confidence selected from source policy
* **taxonomy_confidence** — confidence that kind/wing/room classification is correct
* **retention_confidence** — positive evidence that content is atomic, faithful, and durable
* **authority** — immutable governance rank derived from provenance (inferred 10,
  untrusted agent 20, trusted agent 30, trusted import 40, explicit user 50)

Source trust remains tenant-configurable and affects recall ranking. Authority is fixed by code and
controls whether one memory may supersede another; tuning recall scores cannot invert that hierarchy.
* **memory_confidence** — confidence that the memory is accurate
* **extraction_confidence** — confidence in the extraction process
* **human_verified** — whether a human has confirmed it
* **authority level** — explicit_user > trusted_import > trusted_agent > inferred

Authority hierarchy governs supersession. A lower-authority source can never silently replace a higher-authority memory.

Supersession is **atomic**: expiring the original and inserting the replacement happen in one transaction (the original is expired *before* the replacement is inserted, so the dedup unique index can never see both as active), and a failure between the two rolls back the original's expiration. The original points forward to its replacement (`superseded_by`), the replacement records what it replaced (`item_events`), and supersede behavior is covered by Postgres-backed tests that enforce the real unique index and RLS policies.

`memory_confidence` starts from source-type defaults (see [`docs/design.md`](docs/design.md)
§4). Taxonomy classification does not change it. New writes also preserve that selected default in
`source_confidence_prior`; existing rows remain `NULL` rather than being historically reinterpreted.

### Recall

Startup recall returns a deterministic, bounded working set of active memories, scored by:

```text
score = importance × 0.30
      + source_trust × 0.25
      + memory_confidence × 0.20
      + recency × 0.15
      + human_verified × 0.10
```

The `recency` component is the larger of two decay signals: recall recency (decay from `last_recalled_at`, subject to the anti-feedback penalty) and freshness (decay from `valid_from`/`created_at`, half-weighted). This gives a fresh, never-recalled memory a modest recency contribution without letting freshness dominate trust/importance.

Pinned items bypass scoring and are included first, up to a ceiling.

Recall is **bounded by default**: omitted `byte_budget` / `item_budget` fall back to the configured defaults (`recall_byte_budget`, `recall_item_budget`) rather than leaving recall unbounded. Explicit caller budgets always override the defaults.

Every recalled item includes a `reasons` array explaining why it was included.

Anti-feedback-loop guardrails prevent the same memories from permanently dominating recall without useful feedback.

Feedback is canonical per principal and item: identical retries are unchanged,
while changing `useful`/`noise` appends a replacement and preserves the prior
row as history. Accepted first verdicts and changes are limited per principal
per UTC day by `tenant_config.feedback_daily_limit` (default 500); exhaustion
returns `429` and `Retry-After`. Optional `recall_log_id` attribution is accepted
only when the caller owns that recall and the item was actually surfaced.

#### Semantic recall & search ranking

Semantic recall (`mode=semantic`) and semantic/hybrid search rank results by a deterministic trust-weighted score (`scoring_version = semantic-v2`), not pure cosine distance:

```text
similarity   = clamp(1.0 − cosine_distance, 0.0, 1.0)
trust_score  = 0.30·source_trust + 0.30·memory_confidence + 0.25·importance
             + 0.10·human_verified + 0.05·review_status_factor   (clamped to [0.05, 1.0])
semantic_score = similarity × trust_score
```

Unresolved conflicts multiply `trust_score` by 0.75; proposed items multiply by 0.85. So a slightly-closer low-trust or unreviewed item cannot outrank a higher-trust memory. Semantic result rows preserve `distance` and additionally expose `similarity_score`, `trust_score`, and the final `score` for transparency.

#### Relationship-aware recall (graph + tunnel expansion)

`POST /v1/recall` with `mode=semantic` doesn't stop at the nearest matches — it also reconstructs the context *around* them. Semantic search finds relevant memories; relationship expansion finds the decisions they were derived from, what they contradict or support, and their neighbors in the same tunnel. That surrounding context is often more valuable than the single closest-matching memory in isolation.

```text
query → semantic retrieval → graph expansion → tunnel expansion → merge → relationship-aware rescoring → budget packing → response
```

* **Graph expansion** follows typed, depth-1 edges (`memory_edges`: `derived_from`, `references`, `explains`, `contradicts`, `supports`, `depends_on`, `mentions`) from the top semantic candidates — bounded (5 neighbors/item, 20 total by default), deterministic, no recursion.
* **Tunnel expansion** pulls bounded neighbors from a memory's tunnel-linked `(wing, room)` (20 additions by default) — no full tunnel scan.
* Every expanded candidate passes the same tenant/visibility/review-status trust gate as a direct semantic hit — expansion can only narrow, never bypass.
* Final score blends semantic relevance (70%), relationship strength (15%), tunnel membership (10%), and importance (5%) — semantic relevance still dominates. Reported as `scoring_version = semantic-v3` in `/v1/recall` (mode=semantic) responses and `recall_logs`.
* Every expanded item explains itself: `"linked via derived_from"`, `'same tunnel "Atlas"'`, alongside the existing `reasons` array — merged/tagged with an `origin` (`semantic`, `graph`, `tunnel`, or a combination like `semantic+graph`).
* The existing byte/token/item budget packer is unchanged and authoritative — expanded memories compete for budget exactly like direct hits.

Startup recall is unaffected (no query to anchor expansion from). See `docs/design.md` §9 for the full pipeline, edge-weight mapping, and configuration knobs (`ENGRAM_MAX_GRAPH_*`, `ENGRAM_MAX_TUNNEL_*`, `ENGRAM_RECALL_CANDIDATE_CEILING`).

#### Search filters

`/v1/search` honors `kind`, `wing`, and `room` filters (AND semantics) across keyword, semantic, and hybrid modes. Filters apply before ranking/limit and alongside tenant/read-eligibility scoping, so ineligible rows never displace matches.

### Visibility & Multi-Tenancy

Engram supports visibility scopes for both single-agent and multi-agent deployments.

| Visibility  | Who can read                            |
| ----------- | --------------------------------------- |
| `private`   | Only the principal that wrote it        |
| `workspace` | Any principal in the same workspace     |
| `tenant`    | Any principal in the organization       |
| `public`    | Any authenticated caller, where enabled |

Row Level Security is enforced at the Postgres level — one forgotten `WHERE` clause cannot cause a cross-tenant leak. The runtime service connects as a dedicated non-owner role (`engram_app`) with no `BYPASSRLS`, and every tenant-scoped table uses `FORCE ROW LEVEL SECURITY`, so isolation holds even if the connecting role is the table owner. App-layer visibility/workspace logic is still the primary semantic rule; RLS is defense-in-depth beneath it.

Caller-facing item mutations apply the same eligibility rules as reads. A caller can
therefore mutate a private memory only when it owns that memory, and can mutate a
workspace-visible memory only while eligible for that workspace. Missing and inaccessible
item IDs both return `404 Not Found` so item existence is not disclosed. This eligibility
check does not itself grant privileged review transitions; transition authority and route
scope enforcement are separate trust controls.

### API-Key Scopes & Authorization

Every API key carries an explicit, validated list of **scopes**. Scopes answer
*"may this credential attempt this class of operation?"* — a question
orthogonal to tenant membership, item eligibility, and principal type (agent
vs. user vs. admin), which answer *"may this specific principal perform this
specific action?"*. All four checks are independently enforced; none
substitutes for another.

The canonical scope vocabulary is exactly:

| Scope    | Grants                                                                 |
| -------- | ----------------------------------------------------------------------- |
| `read`   | recall, search, item list/detail, taxonomy, tunnels, KG query/timeline, diary reads, classification |
| `write`  | remember, feedback, item metadata/supersede/invalidate, KG writes, diary writes, tunnel creation, and collaborative review actions (dispute, self-withdrawal) |
| `review` | review queues/hygiene, privileged review decisions (activate, reactivate, reject, non-author archival), human verification, conflict resolution, bulk archival |
| `export` | `GET /v1/export/cca`                                                     |
| `admin`  | tenant/workspace/principal/API-key/memory-kind governance, and every operation above |

**`admin` is a super-scope** — a key carrying `admin` satisfies every other
scope requirement automatically; you never need to also list `read`, `write`,
`review`, or `export` alongside it. Scopes otherwise do **not** imply one
another: `write` does not imply `read`, `review` does not imply `write` or
`read`, and `export` does not imply `read`. A caller missing a required scope
gets `403 Forbidden` with a body like `{"detail": "Requires scope: write"}`
(or `"Requires one of scopes: write, review"`) — scope denial always happens
before any handler-level mutation or eligibility disclosure.

For a memory-profile-bound key, scopes authorize the operation but do not bypass the
profile's active read revision. Even `admin` and `export` remain narrowed on
MemoryItem-backed data. The server pins one `ResolvedMemoryContext` per request; there is no
request field, header, SDK option, MCP argument, or Hermes setting that selects another profile.
Unprofiled keys retain compatibility behavior. Profile-enforced writes are intentionally deferred
to ENG-SCOPE-002C, so a bound key is not yet a complete read/write sandbox.

`POST /v1/items/{item_id}/review` is a mixed-purpose endpoint: an agent with
only `write` may dispute an item or withdraw its own still-`proposed`
proposal, but activating, reactivating, rejecting, or archiving someone
else's proposal is a privileged review decision that requires `review` —
even for a human `user` principal the principal-type policy would otherwise
allow. Scope and principal-type policy are both required; neither one alone
is sufficient (an agent with `review` still cannot activate anything — agent
principal restrictions still apply).

Every route's scope requirement is discoverable in the OpenAPI schema
(`GET /openapi.json`) under the `x-engram-scope-policy` vendor extension,
e.g.:

```json
{ "all_of": ["write"], "admin_satisfies": true }
{ "any_of": ["write", "review"], "admin_satisfies": true,
  "conditional": { "privileged_review_transitions": "review" } }
{ "exempt": true, "reason": "liveness probe" }
```

Only `GET /health` and `GET /ready` are exempt from scope enforcement; every
other application route declares an explicit `all_of`/`any_of` requirement,
and a completeness test fails CI if a new route ships without one.

**Issuing keys.** New-key issuance validates the requested scope list against
the vocabulary above, rejects unknown/misspelled scopes with `422`, dedupes,
and persists them in a canonical order (`read, write, review, export,
admin`). An explicit empty scope list (`"scopes": []`) is allowed and
authenticates, but such a key can only reach `/health`/`/ready`. Omitting
`scopes` entirely defaults to `["read", "write"]`.

```bash
# Full administrator (bootstraps the very first key):
engram bootstrap-key --scopes read,write,admin,export

# Or via the admin API, once you have an admin-scoped key:
curl -X POST http://localhost:8000/v1/admin/api-keys \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "tenant_id": "<tenant-uuid>",
        "principal_id": "<principal-uuid>",
        "scopes": ["review"],
        "label": "human-reviewer"
      }'
```

Common issuance patterns:

| Key purpose                 | `scopes`                    |
| ---------------------------- | ---------------------------- |
| Read-only recall/search agent | `["read"]`                  |
| Write-only capture agent     | `["write"]`                 |
| Human reviewer               | `["review"]`                |
| Export-only integration      | `["export"]`                |
| Full administrator           | `["admin"]`                 |

**Backward compatibility.** Existing keys with `read`/`write`/`export`/`admin`
continue working unchanged under the new matrix, and existing `admin`-only
keys (including bootstrap keys) automatically satisfy `review` via the
super-scope rule — no data migration is needed. Historical rows may contain
scope strings that predate validation (e.g. from hand-inserted keys); those
authenticate normally, and any unrecognized string simply confers no
authority rather than crashing authentication.

**Development mode.** With `ENGRAM_AUTH_ENABLED=false` (the default for local
dev), every request resolves the seeded default admin principal, which
carries `admin` and therefore passes every scope gate — the same runtime
guards run in both modes; auth-disabled mode is not special-cased per route.

Engram's scopes are a custom bearer-token vocabulary, not OAuth2 — there is
no token endpoint, no refresh flow, and no third-party identity provider
integration.

### Classification

Two endpoints serve classification with distinct roles:

* **`POST /v1/classify`** returns taxonomy suggestions plus independent retention evidence and stores a one-hour, server-attested receipt. `confidence` remains a deprecated alias of `taxonomy_confidence`.
* **`POST /v1/remember`** may bind that receipt with `classification_run_id`; write scope is still required and the server validates principal, content, source, workspace, expiry, and kind.

Taxonomy and retention confidences are separately clamped to `0.0–0.95`. Retention dispositions are:
`retain` for atomic information useful beyond the current working moment; `transient` for session-local
utility; `noise` for acknowledgements, status/process chatter, and repeated tool output; and `uncertain`
when durable value cannot be judged safely. Retention confidence measures only the positive case for
durable retention, so confidence that content is noise does not raise it. Retention evidence is not
external-truth verification, does not affect recall ranking, and does not authorize a write.

The classifier's `suggested_visibility` is applied **downward only** — it may narrow the requested scope (e.g. `tenant` → `private`) but never widen it. Invalid or absent suggestions preserve the caller's requested visibility unchanged.

Every receipt-bound write records its receipt/version, both confidence layers, source prior,
visibility decision, reason, and allowlisted, context-sanitized provider provenance. A receipt is
permanently consumed even if its bound item is later deleted. Dedup binding requires matching source and
governed kind and can only narrow the existing visibility. Content is never mutated. Promotion Path A
v2 consumes this bound evidence through its independently gated retention-evidence lane.

Seed classification rules are intentionally conservative: "skip" rules are whole-message *status-only* matchers (bare `ok`, `done`, `passed`) that don't fire on status words inside meaningful sentences, and doctrine classification requires explicit policy/invariant phrasing rather than casual modal verbs.

### Memory Kinds

`kind` is governed by a tenant-scoped `memory_kinds` registry, not a hard-coded
enum. Every tenant is seeded with nine built-in kinds — `fact`, `preference`,
`doctrine`, `decision`, `invariant`, `observation`, `diary_entry`,
`procedure`, `summary` — each carrying behavior flags (`singleton`,
`requires_review`, `stays_in_recall_when_disputed`, `default_importance`)
that drive supersession, initial review status, and disputed-recall
inclusion. Classification and `/v1/remember` validate against the tenant's
*enabled* registry rows; an unknown or disabled kind is rejected with a 422,
never silently coerced to `fact`.

Tenant admins can add governed custom kinds without a schema migration:

```
GET    /v1/admin/memory-kinds            # list all (enabled + disabled)
POST   /v1/admin/memory-kinds            # create a custom kind (admin scope)
PATCH  /v1/admin/memory-kinds/{name}     # edit flags, enable/disable
```

Custom kind names must match `^[a-z][a-z0-9_]{0,63}$` and may not shadow a
built-in name. `name` is immutable after creation. Disabling a kind blocks
new writes/classification into it but never touches existing memories of
that kind — deletion is intentionally unsupported (disabling is sufficient).
See `docs/design.md` § Memory kinds for the full built-in behavior table.

## Architecture

* **Postgres 16 + pgvector** — single storage backend
* **FastAPI** — REST core
* **MCP adapter** — exposes Engram to MCP-compatible clients
* **Python SDK** — thin client wrapper over the REST API
* **Multi-tenant from day one** — `tenant_id` on every tenant-scoped table
* **Row Level Security** — database-enforced isolation
* **Append-first content** — memory content is never silently overwritten
* **Audited metadata events** — review status, visibility, taxonomy, and lifecycle changes are logged
* **Separate embeddings table** — model-keyed embeddings support re-embedding without schema churn
* **Full-text search** — generated `tsvector` column and GIN index
* **Tenant-configurable policy** — scoring weights, trust defaults, recall policy, and promotion thresholds
* **Postgres job queue** — `/v1/remember` enqueues embedding generation and LLM classification refinement; a worker (`engram worker`) drains the queue off the request path so memory stays cheap per write

REST is the core interface. MCP and SDK integrations are adapters.

Agent frameworks are clients.

## MCP Adapter

Engram includes an MCP server adapter for MCP-compatible clients such as Hermes, Claude Desktop, and other agent runtimes.

The MCP adapter exposes tools for:

* remembering memories
* startup and semantic recall
* keyword, semantic, and hybrid search
* classification
* knowledge-graph queries
* knowledge-graph additions
* private diary entries

See `adapters/mcp-server/README.md` for MCP setup and tool signatures.

## Vocabulary

Engram uses evocative naming drawn from memory palace traditions.

| Engram term | Plain-language equivalent             |
| ----------- | ------------------------------------- |
| Wing        | Domain / category                     |
| Room        | Subcategory                           |
| Memory item | A stored memory                       |
| Tunnel      | Cross-category link                   |
| Diary       | Principal-private journal             |
| Doctrine    | Standing instruction / operating rule |
| Invariant   | Must-remain-true constraint           |

## Status

Engram's MVP is **implemented and dogfood-deployed** — not a skeleton or a plan.
The canonical memory service and agent adapters are exercised by a live,
network-verified deployment. The current trust workflow is extensively covered
by PostgreSQL-backed CI; deployment verification predates the latest trust and
concurrency remediation, so those changes are not yet claimed as live-proven.

> **What "verified" means here:** implemented = code exists and is unit/integration
> tested (CI runs the full suite against Postgres 16 + pgvector 0.8);
> dogfood-verified = exercised against the running deployment recorded in
> [`docs/ops/dogfood-verification.md`](docs/ops/dogfood-verification.md);
> deferred = explicitly post-MVP (see `docs/plans/engram-mvp-backlog.md`).

### MVP capability matrix

| Capability                                                              | Implemented | Dogfood-verified |
| ----------------------------------------------------------------------- | :---------: | :--------------: |
| Schema + migrations (19 tables), RLS, FTS, pgvector storage            |     yes     |       yes        |
| `POST /v1/remember` (trust fields, dedup, supersession, secret guard)   |     yes     |       yes        |
| Startup recall (scoring, pinned bypass, anti-feedback loop, reasons)    |     yes     |       yes        |
| Semantic recall (`mode=semantic`, proposed items tagged `unreviewed`)   |     yes     |   over FTS\*     |
| Keyword / semantic / hybrid search                                      |     yes     |   over FTS\*     |
| Item CRUD, PATCH with audited `item_events`, review/verify/supersede    |     yes     |       yes        |
| Write-time conflict detection + resolution                             |     yes     |       yes        |
| Feedback endpoint + recall explanations + warnings                      |     yes     |       yes        |
| Knowledge graph (visibility inheritance), taxonomy, tunnels, diary      |     yes     |       yes        |
| Relationship-aware recall (bounded graph + tunnel expansion)            |     yes     |   over FTS\*     |
| Memory hygiene (stale detection, bulk-archive, stats)                   |     yes     |       —          |
| LLM + rule-based classification                                         |     yes     |       —          |
| Auto-promotion — Path A (age + confidence + no conflict)                |     yes     |       —          |
| CCA export + importers (CCA, MemPalace — dry-run/apply)                 |     yes     |       —          |
| API-key auth + admin endpoints (scopes, bootstrap flow)                 |     yes     |       yes        |
| Python SDK (async client over REST)                                     |     yes     |       yes        |
| MCP adapter (stdio, all tools)                                          |     yes     |       yes        |
| Profile-keyed embeddings + zero-downtime re-embedding                   |     yes     |    mocked only   |
| Postgres job queue + `engram worker` (async embeddings, classification refine, conflict check) | yes |       —          |
| Deployment artifacts (Compose, `.env.example`, backup, `init-db`)       |     yes     |       yes        |
| **Dogfood deployment** (auth-enabled, network-reachable, backed up)     |     yes     |       yes        |

\* Semantic/vector recall and search are implemented and tested, but the
dogfood deployment runs with `ENGRAM_EMBEDDING_PROVIDER=none` (intentionally
disabled for initial dogfooding). Keyword/FTS recall and search are verified
live over the network. The live OpenAI embedding path has **not** been
recorded-verified yet — see `docs/embeddings.md`.

### Explicitly deferred (post-MVP)

These are intentionally out of scope for the MVP and tracked in
`docs/plans/engram-mvp-backlog.md`:

* **Production data migration runs** (CCA + MemPalace `--apply` against the live
  instance) — BL-011. The importers are built; only the operational import is pending.
* **engram-hooks / Hermes automatic lifecycle capture** — BL-012 /
  ENG-HERMES-001. The compatibility shim (native-hook detection, monkey-patch,
  guard, idempotent `install()`, structured status) is implemented and unit
  tested (`pytest -q adapters/engram-hooks/tests`), and a documented Hermes
  dogfood profile now loads it (`profiles/hermes-engram-dogfood.yaml`,
  `docs/ops/hermes-dogfood-profile.md`). What's **not yet done**: a recorded
  end-to-end run against a real Hermes checkout confirming an automatic write
  reaches the deployed Engram instance. Explicit MCP-driven dogfooding works
  today regardless.
* Auto-promotion **Path B** (usage-validated quorum).
* Hard delete + `deletion_events` tombstones + KG cascade.
* PII-risk classification and sensitive-read audit logging.
* Admin list/update/delete + console; multi-way conflict table.
* Local (non-OpenAI) embedding provider; Helm/cloud artifacts.
* Phase 3 open-source packaging hardening.

## Dogfooding

Engram runs on a dedicated VM ("`engram01`"), reachable over a Tailscale mesh,
with auth enabled, a bootstrap API key issued, nightly `pg_dump` backups, and a
restore smoke test. The full sanitized verification record — deployment,
network health, authenticated remember→recall round trips, MCP adapter smoke,
backup/restore — lives in
[`docs/ops/dogfood-verification.md`](docs/ops/dogfood-verification.md).

The dogfood interface is the **MCP adapter**: agents call `engram_remember`,
`engram_recall`, and `engram_search` over stdio against the deployed instance.
See [`adapters/mcp-server/README.md`](adapters/mcp-server/README.md) for setup
and the Hermes config example.

> Hostnames, IPs, and API keys are deliberately omitted from the public repo.
> Operators hold the real values in a secret manager.

## Roadmap

Engram is being built in layers. The first four are largely landed; layer five
(open-source readiness) is the active frontier.

1. **Canonical memory MVP** — *done.*
   Durable storage, core schema, REST foundation, recall primitives, import/export.
2. **Trustable memory workflow** — *done.*
   Classification, review states, promotion (Path A), disputes, conflict detection, provenance.
3. **Rich memory topology** — *done.*
   Knowledge graph, tunnels, taxonomy browser, and relationship-aware recall.
4. **Agent integration** — *SDK and MCP done and verified; Hermes automatic
   lifecycle capture (`engram-hooks`) written but unverified (post-MVP).*
   MCP, SDK, startup + semantic recall, and pre-compression/sync-turn capture
   (the latter awaiting engram-hooks verification).
5. **Verification and open-source readiness** — *in progress.*
   Post-remediation trust closure, real Hermes lifecycle capture, live
   embeddings, quality evaluations, examples, deployment hardening, security
   review, and hosted-service preparation. See the active verification ledger
   in `docs/plans/post-remediation-verification-2026-07.md`.

See [`docs/design.md`](docs/design.md) for the full design document and
[`docs/plans/engram-mvp-backlog.md`](docs/plans/engram-mvp-backlog.md) for the
execution backlog and post-MVP work.

## License

MIT
