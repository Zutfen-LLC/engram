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

This starts Postgres 16 with pgvector and the Engram service. The schema migrates automatically on first boot.

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

Auto-promotion is available for memories that meet tenant-configurable confidence, age, conflict, and feedback thresholds.

Auto-promotion runs on demand. Wire the CLI to cron/systemd, or call the admin endpoint:

```bash
# All tenants. Runs as the owner role (bypasses RLS) via ENGRAM_OWNER_DATABASE_URL:
engram promote-proposed

# Single tenant, capped at 1000 candidates:
engram promote-proposed --tenant <tenant-id> --limit 1000
```

```text
POST /v1/admin/promote
```

Promotion returns per-reason counts:

```text
scanned
promoted
skipped_confidence
skipped_age
skipped_conflict
skipped_disabled
```

Thresholds come from `tenant_config`:

```text
auto_promote_enabled
auto_promote_confidence_threshold
auto_promote_min_age_hours
```

### Trust Model

Trust is not binary. Every memory carries:

* **source_trust** — trust in where the memory came from
* **memory_confidence** — confidence that the memory is accurate
* **extraction_confidence** — confidence in the extraction process
* **human_verified** — whether a human has confirmed it
* **authority level** — explicit_user > trusted_import > trusted_agent > inferred

Authority hierarchy governs supersession. A lower-authority source can never silently replace a higher-authority memory.

### Recall

Startup recall returns a deterministic, bounded working set of active memories, scored by:

```text
score = importance × 0.30
      + source_trust × 0.25
      + memory_confidence × 0.20
      + recency × 0.15
      + human_verified × 0.10
```

Pinned items bypass scoring and are included first, up to a ceiling.

Every recalled item includes a `reasons` array explaining why it was included.

Anti-feedback-loop guardrails prevent the same memories from permanently dominating recall without useful feedback.

### Visibility & Multi-Tenancy

Engram supports visibility scopes for both single-agent and multi-agent deployments.

| Visibility  | Who can read                            |
| ----------- | --------------------------------------- |
| `private`   | Only the principal that wrote it        |
| `workspace` | Any principal in the same workspace     |
| `tenant`    | Any principal in the organization       |
| `public`    | Any authenticated caller, where enabled |

Row Level Security is enforced at the Postgres level — one forgotten `WHERE` clause cannot cause a cross-tenant leak. The runtime service connects as a dedicated non-owner role (`engram_app`) with no `BYPASSRLS`, and every tenant-scoped table uses `FORCE ROW LEVEL SECURITY`, so isolation holds even if the connecting role is the table owner. App-layer visibility/workspace logic is still the primary semantic rule; RLS is defense-in-depth beneath it.

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
The canonical memory service, the full trust workflow, and the agent adapters
all exist and are exercised by a live, network-verified deployment.

> **What "verified" means here:** implemented = code exists and is unit/integration
> tested (CI runs the full suite against Postgres 16 + pgvector 0.8);
> dogfood-verified = exercised against the running deployment recorded in
> [`docs/ops/dogfood-verification.md`](docs/ops/dogfood-verification.md);
> deferred = explicitly post-MVP (see `docs/plans/engram-mvp-backlog.md`).

### MVP capability matrix

| Capability                                                              | Implemented | Dogfood-verified |
| ----------------------------------------------------------------------- | :---------: | :--------------: |
| Schema + migrations (15 tables), RLS, FTS, pgvector storage            |     yes     |       yes        |
| `POST /v1/remember` (trust fields, dedup, supersession, secret guard)   |     yes     |       yes        |
| Startup recall (scoring, pinned bypass, anti-feedback loop, reasons)    |     yes     |       yes        |
| Semantic recall (`mode=semantic`, proposed items tagged `unreviewed`)   |     yes     |   over FTS\*     |
| Keyword / semantic / hybrid search                                      |     yes     |   over FTS\*     |
| Item CRUD, PATCH with audited `item_events`, review/verify/supersede    |     yes     |       yes        |
| Write-time conflict detection + resolution                             |     yes     |       yes        |
| Feedback endpoint + recall explanations + warnings                      |     yes     |       yes        |
| Knowledge graph (visibility inheritance), taxonomy, tunnels, diary      |     yes     |       yes        |
| Memory hygiene (stale detection, bulk-archive, stats)                   |     yes     |       —          |
| LLM + rule-based classification                                         |     yes     |       —          |
| Auto-promotion — Path A (age + confidence + no conflict)                |     yes     |       —          |
| CCA export + importers (CCA, MemPalace — dry-run/apply)                 |     yes     |       —          |
| API-key auth + admin endpoints (scopes, bootstrap flow)                 |     yes     |       yes        |
| Python SDK (async client over REST)                                     |     yes     |       yes        |
| MCP adapter (stdio, all tools)                                          |     yes     |       yes        |
| Embedding backfill (`engram backfill-embeddings`)                       |     yes     |    mocked only   |
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
5. **Open-source readiness** — *in progress.*
   Documentation pass, examples, deployment hardening, security review, and
   hosted-service preparation.

See [`docs/design.md`](docs/design.md) for the full design document and
[`docs/plans/engram-mvp-backlog.md`](docs/plans/engram-mvp-backlog.md) for the
execution backlog and post-MVP work.

## License

MIT
