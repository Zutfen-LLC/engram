# Engram — Design Document

**Status:** Locked (v1, 2026-07-06)
**Product:** Shared structured memory layer for multi-agent AI teams

---

## 1. What Engram Is

Engram is a standalone memory service that gives teams of AI agents a shared, structured, durable brain. It is not a flat key-value memory store — it is an organized knowledge system with taxonomy, relationships, temporal validity, and per-agent scoping.

The name comes from neuroscience: an **engram** is the physical trace a memory leaves in brain tissue — the literal substrate of stored memory.

### Origin

Engram is the successor to MemPalace (ChromaDB-backed, single-instance) and the Zutfen CCA ledger (JSON-in-git). It absorbs both models into a unified Postgres-native service designed for multi-agent, multi-device, multi-tenant use from day one.

### Target users

- **Self-hosted (Phase 1):** Zutfen LLC's own agent fleet across Hermes profiles
- **Open source (Phase 3):** Any team running multiple AI agents (LangChain, CrewAI, AutoGen, custom)
- **Hosted (future):** Managed Engram Cloud for teams that don't want to self-host

---

## 2. Architectural Principles (Locked)

These decisions are made and should not be relitigated without strong reason.

1. **Standalone service, not agent-coupled.** Engram is its own process, its own repo, its own deployment. Agent frameworks are clients. No agent framework is special — not even Hermes.

2. **Postgres + pgvector as the single storage backend.** No storage abstraction layer. YAGNI until real demand for alternatives. Postgres gives us transactions, multi-writer concurrency, relational queries, and vector search in one system.

3. **Multi-tenant from day one.** Every table has `tenant_id`. The cost is one column + one WHERE clause. The benefit is zero-friction hosted product later. Single-tenant self-hosted is just the hosted product with one tenant.

4. **REST core, MCP/SDK as thin wrappers.** The service exposes a clean HTTP API. MCP is one client adapter. Python SDK is another. Any framework can adopt it.

5. **Append-first data model.** Every memory write is an INSERT, never an UPDATE. Supersession/invalidation marks old rows with `valid_to` / `superseded_by`. This gives full audit trail, replay, and time-travel queries.

6. **Profile/workspace scoping is first-class.** Memory items belong to a tenant + workspace + principal. Visibility levels control cross-agent sharing.

7. **The memory model concepts are the product, not the storage.** Wings/rooms/drawers, knowledge graph triples, tunnels, and agent diaries are Engram's product vocabulary. They survive any backend change.

8. **The classification/routing intelligence stays client-side.** Engram does not decide what to remember. The agent (or its memory-routing layer) decides what to promote to Engram. Engram stores, retrieves, and organizes — it does not editorialize.

---

## 3. What Is NOT in Engram

- **Memory classification / routing logic.** The LLM-based extraction, write-boundary guards, volatile recall, and promotion heuristics that zutfen_memory implements are agent-side concerns. They ship as a reference implementation / companion library, not as part of the memory service.
- **Volatile/ephemeral recall.** Pre-compression capture buffers stay local to the agent (14-day retention, file-backed). They are write-buffers, not canonical stores. They don't need centralization.
- **Agent configuration, skills, prompts.** Those are declarative artifacts that belong in version control (git), not in a memory service.
- **Secrets, auth tokens, machine-specific config.** Always local to the machine.

---

## 4. Memory Model

Engram absorbs MemPalace's vocabulary into a unified relational model.

### Core concept: the memory item

A **memory item** is the fundamental unit. It absorbs what MemPalace called "drawers" and what CCA called "ledger entries." Every piece of stored knowledge is a memory item.

Each memory item has:
- **Content** — the verbatim text of the memory
- **Kind** — what type of memory it is (fact, preference, doctrine, decision, invariant, observation, diary_entry)
- **Taxonomy** — optional wing + room (user-defined organization, inheriting MemPalace's abstraction-layer model)
- **Scope** — tenant + workspace + principal ownership
- **Visibility** — private (agent-only) / workspace (shared) / tenant (global)
- **Temporal validity** — valid_from, valid_to (null = currently true)
- **Provenance** — who wrote it, when, from what source type
- **Embedding** — vector for semantic search

### Derived concepts

| MemPalace concept | Engram representation |
|---|---|
| Wing | `wing` field on memory_items (free text, user-defined taxonomy) |
| Room | `room` field on memory_items |
| Drawer | A memory_item (they are the same thing) |
| Knowledge graph fact | Row in `kg_triples` (subject-predicate-object + temporal validity) |
| Tunnel | Row in `tunnels` (cross-wing/room link) |
| Agent diary entry | memory_item with kind='diary_entry', scoped to principal |
| CCA ledger entry | memory_item with kind IN ('doctrine','decision','invariant','preference') |

The CCA ledger export is a **view** over memory_items — no separate table. The git-export workflow queries this view and renders JSON for human review.

---

## 5. Postgres Schema

### Core tables

```sql
-- Enable extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS vector;

-- ============ Identity & scoping ============

CREATE TABLE tenants (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name        TEXT NOT NULL,
    slug        TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE workspaces (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    slug        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(tenant_id, slug)
);

CREATE TABLE principals (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,          -- e.g. "orchestrator", "support-agent"
    type        TEXT NOT NULL DEFAULT 'agent',  -- agent | user
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(tenant_id, name)
);

-- ============ Memory items (the core) ============

CREATE TABLE memory_items (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID REFERENCES workspaces(id) ON DELETE SET NULL,
    principal_id    UUID NOT NULL REFERENCES principals(id),

    -- Content
    content         TEXT NOT NULL,
    content_hash    TEXT NOT NULL,       -- SHA-256 of canonicalized content, for dedup
    kind            TEXT NOT NULL,       -- fact|preference|doctrine|decision|invariant|observation|diary_entry
    wing            TEXT,                -- user-defined taxonomy (optional)
    room            TEXT,

    -- Scoping
    visibility      TEXT NOT NULL DEFAULT 'workspace',  -- private|workspace|tenant|public

    -- Provenance
    source_type     TEXT NOT NULL DEFAULT 'manual',  -- manual|sync_turn|pre_compress|session_end|import|migration
    source_session  TEXT,

    -- Semantic
    embedding       vector(1536),        -- configurable dimension; pgvector HNSW index

    -- Temporal validity (append-first model)
    valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to        TIMESTAMPTZ,         -- NULL = currently valid
    superseded_by   UUID REFERENCES memory_items(id),

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ Knowledge graph ============

CREATE TABLE kg_triples (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID REFERENCES workspaces(id) ON DELETE SET NULL,
    subject         TEXT NOT NULL,
    predicate       TEXT NOT NULL,
    object          TEXT NOT NULL,
    valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to        TIMESTAMPTZ,
    source_item_id  UUID REFERENCES memory_items(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ Tunnels (cross-domain links) ============

CREATE TABLE tunnels (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    source_wing     TEXT NOT NULL,
    source_room     TEXT NOT NULL,
    target_wing     TEXT NOT NULL,
    target_room     TEXT NOT NULL,
    label           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ Agent diaries ============

-- Diaries are memory_items with kind='diary_entry'.
-- No separate table needed; scoped by principal_id.

-- ============ Auth ============

CREATE TABLE api_keys (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    principal_id    UUID REFERENCES principals(id),
    key_hash        TEXT NOT NULL,       -- bcrypt/argon2 hash
    scopes          TEXT[] NOT NULL DEFAULT '{read,write}',
    label           TEXT,                -- human-readable name
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at      TIMESTAMPTZ
);

-- ============ Recall audit ============

CREATE TABLE recall_logs (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    principal_id    UUID NOT NULL REFERENCES principals(id),
    mode            TEXT NOT NULL,       -- startup|semantic|keyword
    query           TEXT,
    item_ids        UUID[],
    byte_budget     INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### Indexes

```sql
-- Memory items: scoped lookups
CREATE INDEX idx_memitems_tenant_workspace ON memory_items(tenant_id, workspace_id);
CREATE INDEX idx_memitems_taxonomi        ON memory_items(tenant_id, wing, room);
CREATE INDEX idx_memitems_kind            ON memory_items(tenant_id, kind);
CREATE INDEX idx_memitems_active           ON memory_items(tenant_id, valid_to) WHERE valid_to IS NULL;
CREATE INDEX idx_memitems_hash             ON memory_items(tenant_id, content_hash);
CREATE INDEX idx_memitems_principal        ON memory_items(tenant_id, principal_id);

-- Semantic search (HNSW for high-recall approximate NN)
CREATE INDEX idx_memitems_embedding ON memory_items
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Knowledge graph
CREATE INDEX idx_kg_subject     ON kg_triples(tenant_id, subject);
CREATE INDEX idx_kg_predicate   ON kg_triples(tenant_id, predicate);
CREATE INDEX idx_kg_active      ON kg_triples(tenant_id, valid_to) WHERE valid_to IS NULL;
```

### CCA view (ledger export projection)

```sql
CREATE VIEW cca_ledger AS
SELECT
    id, tenant_id, workspace_id, principal_id,
    content, kind, wing, room, visibility,
    source_type, source_session,
    valid_from, valid_to, superseded_by, created_at
FROM memory_items
WHERE kind IN ('doctrine', 'decision', 'invariant', 'preference')
  AND valid_to IS NULL;
```

---

## 6. API Surface (REST)

### Memory operations

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/remember` | Write a memory item (dedup, supersession, canonicalization) |
| POST | `/v1/recall` | Bounded recall: `mode=startup` (deterministic working set) or `mode=semantic` (query-based) |
| POST | `/v1/search` | Keyword, semantic, or hybrid search |
| GET | `/v1/items` | List items with filters (workspace, kind, wing, principal, status) |
| GET | `/v1/items/{id}` | Full detail with provenance, evidence, linked KG facts |
| PATCH | `/v1/items/{id}` | Update metadata (wing, room, visibility) — not content |
| POST | `/v1/items/{id}/supersede` | Mark superseded + write replacement |
| POST | `/v1/items/{id}/invalidate` | Mark invalid (set valid_to) |

### Knowledge graph

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/kg` | Add triple |
| GET | `/v1/kg/query` | Query by entity (subject/object/predicate), with temporal filter |
| POST | `/v1/kg/invalidate` | Mark triple invalid |
| GET | `/v1/kg/timeline` | Chronological timeline for an entity |

### Taxonomy & navigation

| Method | Path | Description |
|--------|------|-------------|
| GET | `/v1/taxonomy` | Wing → room → item count hierarchy |
| GET | `/v1/tunnels` | Cross-wing links |
| POST | `/v1/tunnels` | Create tunnel |

### Diary

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/diary` | Write diary entry (creates memory_item kind=diary_entry) |
| GET | `/v1/diary/{principal}` | Read diary entries |

### Export & operations

| Method | Path | Description |
|--------|------|-------------|
| GET | `/v1/export/cca` | Export CCA ledger as JSON (for git-tracked review) |
| GET | `/health` | Liveness |
| GET | `/ready` | Readiness (DB connectivity check) |

---

## 7. Repo Structure

```
engram/
├── README.md
├── docs/
│   └── design.md              ← this document
├── engram/                    ← Python package (the service)
│   ├── __init__.py
│   ├── config.py              ← env/config management
│   ├── db.py                  ← SQLAlchemy session, connection pool
│   ├── models.py              ← ORM models
│   ├── auth.py                ← API key auth
│   ├── embeddings.py          ← embedding generation + pgvector ops
│   ├── canonicalize.py        ← content hashing, dedup, supersession logic
│   ├── recall.py              ← startup/semantic recall selection logic
│   └── api/
│       ├── __init__.py
│       ├── app.py             ← FastAPI app factory
│       └── routes/
│           ├── memory.py      ← remember/recall/search/items
│           ├── kg.py          ← knowledge graph endpoints
│           ├── taxonomy.py    ← taxonomy/tunnels
│           ├── diary.py
│           └── export.py      ← CCA export
├── migrations/
│   └── 001_init.sql           ← raw DDL
├── sdk/
│   └── engram-client/         ← Python SDK (thin HTTP client)
│       └── ...
├── scripts/
│   ├── import_mempalace.py    ← migrate from ChromaDB
│   ├── import_cca.py          ← migrate from JSON ledger
│   └── export_cca.py          ← render ledger to git-friendly JSON
├── adapters/
│   └── mcp-server/            ← MCP adapter (for Hermes + other MCP clients)
├── docker-compose.yml         ← self-hosted: engram + postgres
├── Dockerfile
└── pyproject.toml
```

---

## 8. Visibility & Scoping Model

This is what makes multi-agent work.

| Visibility | Who can read | Use case |
|---|---|---|
| `private` | Only the principal that wrote it | Agent's own working notes, diary |
| `workspace` | Any principal in the same workspace | Shared project knowledge (default) |
| `tenant` | Any principal in the tenant | Cross-project org knowledge |
| `public` | Any authenticated caller (future: unauthenticated) | Shared/public knowledge base |

**Recall semantics:**
- `mode=startup` returns: the principal's active private items + workspace-shared active items + tenant-global active items, bounded by byte/item budget, deterministic ordering.
- `mode=semantic` returns: items matching the query vector, filtered by the caller's visibility scope.

---

## 9. Hosting (Zutfen self-hosted Phase 1)

- Dedicated VM on Proxmox (host TBD — pm01 or pm03 candidate)
- Docker Compose: Engram service + Postgres 16 with pgvector
- Reachable over Tailscale only
- Per-profile API keys with scoped principals
- Nightly `pg_dump` backup

---

## 10. Phased Delivery

### Phase 1 — Core service for Zutfen dogfooding
- Postgres schema + migrations
- REST API (remember, recall, search, KG, export)
- Python SDK
- Migration importers (dry-run)
- Docker Compose deployment
- Bootstrap Zutfen tenant + workspaces + principals
- Migrate existing MemPalace + CCA data

### Phase 2 — Hermes integration
- MCP adapter (replaces MemPalace MCP server)
- zutfen_memory routing layer → writes to Engram instead of files/ChromaDB
- Hermes config integration
- Retire MemPalace ChromaDB + CCA JSON files

### Phase 3 — Open source readiness
- Naming, docs, README, examples
- Multi-framework quickstarts (LangChain, CrewAI, plain Python)
- Auth hardening (API keys → OAuth/org model for hosted)
- Reference implementation of classification/routing as companion package
- Helm chart / cloud deployment artifacts

---

## 11. Differentiation

| Feature | mem0 | Letta/MemGPT | Zep | **Engram** |
|---|---|---|---|---|
| Storage | Vector DB | SQLite/Postgres | Graph+vector | **Postgres+pgvector** |
| Memory model | Flat facts | Agent-scoped blocks | Temporal graph | **Structured taxonomy + KG + diary** |
| Multi-agent | Per-agent | Per-agent session | Per-user | **Multi-tenant, workspace-scoped, visibility levels** |
| Relationships | No | No | Yes (graph) | **Yes (KG + tunnels)** |
| Temporal validity | No | No | Yes | **Yes (valid_from/valid_to)** |
| Audit trail | No | Partial | No | **Yes (append-first, provenance)** |
| Self-hostable | Yes | Yes | Yes | **Yes (Docker Compose)** |

Engram's wedge: structured organizational memory for agent teams, not just flat per-agent recall.
