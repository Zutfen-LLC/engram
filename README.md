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

Create the first API key (auth is off by default for local dev; enable it for production):

```bash
docker compose exec engram-service engram bootstrap-key
```

For the full walkthrough — auth enablement, backup/restore, upgrades, embeddings,
and troubleshooting — see **[`docs/deployment.md`](docs/deployment.md)**.

See `docs/design.md` for the full architecture.

## Local Python development setup

If you want the repo's `.venv` to be able to import the sibling SDK and MCP
adapter directly (so `python -m engram_mcp` works without setting `PYTHONPATH`),
run:

```bash
bash scripts/setup-python-dev.sh
# or: make setup-python-dev
```

This bootstraps `./.venv`, then installs these editable local packages into it:

- `sdk/engram-client`
- `adapters/mcp-server`

After that, these commands work from the repo checkout:

```bash
.venv/bin/python -m engram_mcp
.venv/bin/engram-mcp
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
# All tenants, run as the table-owning DB role to bypass RLS:
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

Row Level Security is enforced at the Postgres level — one forgotten `WHERE` clause cannot cause a cross-tenant leak.

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

**Phase 1A — Canonical memory MVP**

* [x] Postgres schema + migrations
* [x] Row Level Security foundation
* [x] Full-text search foundation
* [x] pgvector embedding storage foundation
* [x] FastAPI service skeleton
* [x] Docker Compose deployment
* [ ] Functional endpoints: remember, recall, search, items, export
* [ ] CCA import
* [ ] Python SDK

Phase 1B adds LLM classification, review workflow, and conflict detection.

Phase 1C adds knowledge graph, tunnels, taxonomy browser, and deeper recall tooling.

Phase 2 integrates Engram with Hermes and agent lifecycle hooks.

Phase 3 prepares Engram for broader open-source release.

## Roadmap

Engram is being built in layers:

1. **Canonical memory MVP**
   Durable storage, core schema, REST foundation, recall primitives, import/export.

2. **Trustable memory workflow**
   Classification, review states, promotion, disputes, conflict detection, and provenance.

3. **Rich memory topology**
   Knowledge graph, tunnels, taxonomy browser, and relationship-aware recall.

4. **Agent integration**
   MCP, SDK, Hermes integration, lifecycle hooks, startup recall, semantic recall, and pre-compression memory capture.

5. **Open-source readiness**
   Documentation, examples, deployment hardening, security review, and hosted-service preparation.

See `docs/design.md` for the full design document and `docs/backlog.json` for the implementation roadmap.

## License

MIT
