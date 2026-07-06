# Engram

> Trustable institutional memory for multi-agent AI teams.

Engram is a standalone memory service that gives teams of AI agents a shared, structured, durable, and **trustable** brain. It's not a flat key-value memory store — it's an institutional memory system with taxonomy, relationships, temporal validity, review states, provenance, and conflict detection.

An **engram** is the physical trace a memory leaves in brain tissue — the literal substrate of stored memory.

## Why Engram?

Most agent memory layers (mem0, Letta, Zep) store flat facts per agent. Engram is built for **teams** of agents that need to share knowledge and trust what's stored. The hard problem isn't "can we store and recall facts?" — that's solved. The hard problem is: **can agents and humans trust what is stored, know why it was stored, know whether it is still true, and safely act on it?**

A lower-authority source can never silently replace a higher-authority memory.

| Feature | Flat stores | Engram |
|---------|------------|--------|
| Memory model | Flat facts | Structured taxonomy (wings/rooms) |
| Trust model | None | Review states: proposed → active → disputed → resolved |
| Relationships | None | Knowledge graph with temporal validity |
| Multi-agent | Per-agent silos | Workspaces with visibility levels + RLS |
| Conflict handling | Dedup only | Write-time contradiction detection + resolution |
| Provenance | Minimal | Source trust, extraction model, verification tracking |
| Audit trail | Overwrites | Append-first content + audited metadata events |
| Classification | Basic extraction | LLM-backed + rule-based, tenant-configurable, no-LLM fallback |
| Recall quality | Similarity only | Scored ranking with "why recalled" explanations |
| Self-hostable | Varies | Docker Compose, one command |

## Quickstart (self-hosted)

```bash
git clone https://github.com/Zutfen-LLC/engram.git
cd engram
cp .env.example .env  # set your passwords
docker compose up -d
```

This starts Postgres 16 (with pgvector) and the Engram service. The schema migrates automatically on first boot. See `docs/design.md` for the full architecture.

```bash
# Verify it's running
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

## Key Concepts

### Memory Lifecycle

Every memory moves through a review pipeline:

```
written → proposed → active → (disputed → resolved) → superseded/archived
```

- **Proposed** memories don't enter startup recall until reviewed.
- **Auto-promotion** (on by default): proposed items with confidence ≥ 0.7, no conflicts, and 72h unchallenged auto-promote to active.
- **Disputed** doctrine/invariant stays in recall with warnings — operating constraints don't silently vanish.

### Trust Model

Trust isn't binary. Every memory carries:

- **source_trust** — trust in where it came from (user said it vs agent guessed)
- **memory_confidence** — overall confidence this is accurate
- **human_verified** — a human has confirmed this
- **authority level** — explicit_user > trusted_import > trusted_agent > inferred

Authority hierarchy governs supersession: a lower-authority source can never silently replace a higher-authority memory.

### Recall

Startup recall returns a deterministic, bounded working set of active memories, scored by:

```
score = importance × 0.30 + source_trust × 0.25 + memory_confidence × 0.20
      + recency × 0.15 + human_verified × 0.10
```

Pinned items bypass scoring (included first, up to a ceiling). Every recalled item includes a `reasons` array explaining why it was included. Anti-feedback-loop guardrails prevent the same memories from permanently dominating recall.

### Visibility & Multi-Tenancy

| Visibility | Who can read |
|---|---|
| `private` | Only the agent that wrote it |
| `workspace` | Any agent in the same workspace (default) |
| `tenant` | Any agent in the organization |
| `public` | Any authenticated caller |

Row Level Security enforced at the Postgres level — one forgotten WHERE clause can't cause a cross-tenant leak.

## Architecture

- **Postgres 16 + pgvector** — single storage backend, no abstraction layer
- **FastAPI** — REST core, MCP and SDK are thin wrappers
- **Multi-tenant from day one** — tenant_id on every table, RLS on all tenant-scoped tables
- **Append-first** — content is never UPDATEd; metadata changes are audited in `item_events`
- **Separate embeddings table** — model-keyed, supports re-embedding without migration
- **Full-text search** — generated tsvector column + GIN index for keyword search
- **Tenant-configurable** — scoring weights, trust defaults, and recall policy stored per-tenant, versioned for audit

See `docs/design.md` for the full design document and `docs/backlog.json` for the implementation roadmap.

## Status

**Phase 1A — Canonical memory MVP (in development)**

- [x] Postgres schema + migrations (15 tables, RLS, FTS, pgvector)
- [x] FastAPI service skeleton + route stubs
- [x] Docker Compose deployment
- [ ] Functional endpoints (remember, recall, search, items, export)
- [ ] CCA import
- [ ] Python SDK

Phase 1B adds LLM classification, review workflow, conflict detection. Phase 1C adds knowledge graph, tunnels, taxonomy browser. Phase 2 integrates with Hermes. Phase 3 prepares for open-source release.

## Vocabulary

Engram uses evocative naming drawn from memory palace traditions:

| Engram term | Plain-language equivalent |
|---|---|
| Wing | Domain / category |
| Room | Subcategory |
| Memory item | A stored memory |
| Tunnel | Cross-category link |
| Diary | Agent-private journal |
| Doctrine | Standing instruction / operating rule |
| Invariant | Must-remain-true constraint |

## License

MIT
