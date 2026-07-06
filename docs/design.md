# Engram — Design Document

**Status:** Locked (v2, 2026-07-06 — trust model revision)
**Product:** Trustable institutional memory for multi-agent AI teams

---

## 1. What Engram Is

Engram is a standalone memory service that gives teams of AI agents a shared, structured, durable, and **trustable** brain. It's not a flat key-value memory store — it's an institutional memory system with taxonomy, relationships, temporal validity, review states, provenance, and conflict detection.

The name comes from neuroscience: an **engram** is the physical trace a memory leaves in brain tissue — the literal substrate of stored memory.

### Product thesis

The hard problem in agent memory is not "can we store and recall facts?" — that's solved. The hard problem is: **can agents and humans trust what is stored, know why it was stored, know whether it is still true, and safely act on it?**

Engram is designed as a trust system, not just a storage system.

### Target users

- **Self-hosted (Phase 1):** Zutfen LLC's own agent fleet across Hermes profiles
- **Open source (Phase 3):** Any team running multiple AI agents
- **Hosted (future):** Managed Engram Cloud for teams that don't want to self-host

---

## 2. Architectural Principles (Locked)

1. **Standalone service, not agent-coupled.** Engram is its own process, its own repo, its own deployment. Agent frameworks are clients.

2. **Postgres + pgvector as the single storage backend.** No storage abstraction layer. YAGNI until real demand for alternatives.

3. **Multi-tenant from day one.** Every table has `tenant_id`. Row Level Security enforces isolation at the database level — one forgotten WHERE clause cannot cause a cross-tenant leak.

4. **REST core, MCP/SDK as thin wrappers.** The service exposes a clean HTTP API. MCP is one client adapter. Any framework can adopt it.

5. **Content is append-first; metadata changes are audited.** Memory content is never UPDATEd. Supersession/invalidation marks old rows. Metadata changes (wing, room, visibility, review_status) are recorded in `item_events` for full audit trail.

6. **Profile/workspace scoping is first-class.** Memory items belong to a tenant + workspace + principal. Visibility levels and workspace membership control access.

7. **The memory model concepts are the product, not the storage.** Wings/rooms/drawers, knowledge graph triples, tunnels, and agent diaries are Engram's product vocabulary.

8. **Classification intelligence is a service feature; lifecycle hooks are client-side.** The service provides content classification (what kind? what wing/room?) as an endpoint. Lifecycle hooks (when to extract, when to promote) stay client-side because they are framework-specific.

9. **Trust is a product feature, not an implementation detail.** Review states, provenance, confidence, conflict detection, and recall explanations are core to the product — they elevate Engram from "memory store" to "trustable institutional memory."

---

## 3. Memory Lifecycle

Every memory item moves through a lifecycle:

```
observed → proposed → active → recalled → confirmed/stale → superseded/invalidated/archived
```

| State | Meaning | Enters startup recall? |
|---|---|---|
| `proposed` | Written by agent, not yet reviewed | No |
| `active` | Reviewed and trusted | Yes |
| `disputed` | Flagged as potentially wrong | No |
| `rejected` | Reviewed and rejected (kept for audit) | No |
| `archived` | Old/superseded, excluded from default recall | No |

Agents can freely write `proposed` memories. Only `active` memories enter the deterministic startup recall set. High-trust sources (explicit user instruction, verified imports) can write directly to `active`.

---

## 4. Trust Model

### Confidence layers

| Field | What it means | Example |
|---|---|---|
| `source_trust` | Trust in where this came from | User said it = 0.9; agent guessed = 0.4 |
| `memory_confidence` | Overall confidence this memory is accurate | Verified fact = 0.95; LLM inference = 0.6 |
| `extraction_confidence` | Confidence of the extraction process | Direct quote = 0.9; LLM summary = 0.5 |
| `human_verified` | A human has confirmed this is true | Boolean |

### Source trust defaults

| Source type | Default source_trust | Default review_status |
|---|---|---|
| `manual` (user explicitly said "remember") | 0.9 | `active` |
| `import` / `migration` (from trusted ledger) | 0.8 | `active` |
| `extraction` (LLM-derived from conversation) | 0.5 | `proposed` |
| `sync_turn` (auto-extracted per turn) | 0.4 | `proposed` |
| `pre_compress` (pre-compression capture) | 0.3 | `proposed` |

### Recall ranking formula

Startup recall uses a scoring formula to order items within the budget:

```
score = (importance * 0.25)
      + (source_trust * 0.20)
      + (memory_confidence * 0.20)
      + (recency_bonus * 0.15)
      + (human_verified_bonus * 0.10)
      + (pinned_bonus * 0.10)
```

Where:
- `recency_bonus` = decay function based on `last_recalled_at` (recently recalled = relevant)
- `human_verified_bonus` = 1.0 if verified, 0.0 otherwise
- `pinned_bonus` = 1.0 if pinned, 0.0 otherwise

Pinned items are always included in startup recall if active, regardless of budget (they get their bytes first, then the scorer fills the remainder).

---

## 5. What Is NOT in Engram

**Lifecycle hooks (framework-specific):**
- Pre-compression extraction, turn/session boundary detection, volatile recall. These require in-process visibility the service cannot have. They live in the companion library (engram-hooks).

**What IS in Engram (classification intelligence):**
- Content classification (`POST /v1/classify`). LLM-backed with rule-based fallback. Tenant-configurable rules.
- Auto-classification on remember. When kind/wing/room are omitted, the service classifies before storing.

**Also NOT in Engram:**
- Agent configuration, skills, prompts (belong in version control)
- Secrets, auth tokens, machine-specific config (always local)

---

## 6. Memory Model

### Core concept: the memory item

Each memory item has:
- **Content** — verbatim text + content_hash for dedup
- **Kind** — fact, preference, doctrine, decision, invariant, observation, diary_entry
- **Taxonomy** — optional wing + room
- **Subject** — what this memory is ABOUT (subject_type, subject_id, subject_name), separate from who wrote it
- **Scope** — tenant + workspace + principal
- **Visibility** — private / workspace / tenant / public
- **Trust** — review_status, memory_confidence, source_trust, human_verified
- **Recall signals** — importance, pinned, last_recalled_at, recall_count
- **Provenance** — source_type, source_uri, extracted_by_model, extraction_confidence
- **Conflict tracking** — conflicts_with_item_id, conflict_type, resolution_status
- **Temporal validity** — valid_from, valid_to, superseded_by

### MemPalace succession

| MemPalace concept | Engram representation |
|---|---|
| Wing | `wing` field on memory_items |
| Room | `room` field on memory_items |
| Drawer | A memory_item |
| Knowledge graph fact | Row in `kg_triples` (with visibility inherited from source_item_id) |
| Tunnel | Row in `tunnels` |
| Agent diary entry | memory_item with kind='diary_entry' |
| CCA ledger entry | memory_item with kind IN (doctrine, decision, invariant, preference) |

---

## 7. Postgres Schema

See `migrations/001_init.sql` for the canonical DDL. Key design decisions:

### Tables (12 total)

| Table | Purpose |
|---|---|
| `tenants` | Multi-tenant root |
| `workspaces` | Project/team scoping within a tenant |
| `principals` | Agents and users that write memories |
| `workspace_members` | Role-based workspace membership (owner/admin/member/viewer) |
| `memory_items` | The core: all memories with trust, provenance, review state |
| `memory_embeddings` | Embeddings stored separately (supports multiple models, re-embedding) |
| `kg_triples` | Knowledge graph facts with visibility and review state |
| `tunnels` | Cross-wing/room links |
| `item_events` | Audit trail for metadata mutations (content stays append-first) |
| `classification_rules` | Tenant-configurable classification rules |
| `api_keys` | Authentication credentials |
| `recall_logs` | Recall audit with item_ids for feedback loops |

### Key schema decisions

- **Check constraints** on all enum-like fields (kind, visibility, review_status, source_type, sensitivity, conflict_type) to prevent taxonomy drift.
- **Full-text search** via `content_tsv TSVECTOR` column with auto-update trigger + GIN index. Essential for keyword search on IDs, paths, names.
- **Separate embeddings table** keyed by `(memory_item_id, embedding_model)`. A model change is new rows, not a migration. Old vectors remain queryable during re-embedding.
- **Unique dedup index** on `(tenant_id, workspace_id, principal_id, content_hash) WHERE valid_to IS NULL AND review_status != 'rejected'`. Makes remember idempotent for retries.
- **Row Level Security** on all tenant-scoped tables. Application sets `app.tenant_id` per session; Postgres enforces isolation even if application code is wrong.
- **HNSW with iterative_scan** — requires pgvector 0.8+. Set `hnsw.iterative_scan = strict` at query time to handle filtered (tenant-scoped) queries without recall degradation.

### KG visibility

KG triples inherit visibility from their `source_item_id` at query time. A private memory item that spawns a triple does NOT leak that knowledge — querying the KG checks the source item's visibility against the caller's scope.

---

## 8. API Surface (REST)

### Memory operations

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/remember` | Write a memory item (dedup, supersession, auto-classify, conflict check) |
| POST | `/v1/recall` | Bounded recall: startup (deterministic) or semantic (query-based) |
| POST | `/v1/search` | Keyword (FTS), semantic (pgvector), or hybrid search |
| GET | `/v1/items` | List items with filters (cursor pagination) |
| GET | `/v1/items/{id}` | Full detail with provenance, events, linked KG facts |
| PATCH | `/v1/items/{id}` | Update metadata (creates item_event, never touches content) |
| POST | `/v1/items/{id}/supersede` | Mark superseded + write replacement |
| POST | `/v1/items/{id}/invalidate` | Mark invalid |
| POST | `/v1/items/{id}/review` | Change review_status (proposed → active, dispute, etc.) |
| POST | `/v1/items/{id}/verify` | Mark human-verified |
| POST | `/v1/feedback` | Mark recalled items as useful/noise (recall scoring feedback loop) |

### Classification

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/classify` | Classify raw text: suggest kind, wing, room, confidence |
| GET | `/v1/classification/rules` | List tenant rules |
| POST | `/v1/classification/rules` | Create/update a rule |
| DELETE | `/v1/classification/rules/{id}` | Delete a rule |

### Knowledge graph

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/kg` | Add triple (visibility inherited from source item) |
| GET | `/v1/kg/query` | Query by entity, with temporal + visibility filtering |
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
| POST | `/v1/diary` | Write diary entry (kind=diary_entry, visibility=private) |
| GET | `/v1/diary/{principal}` | Read diary entries |

### Review & governance

| Method | Path | Description |
|--------|------|-------------|
| GET | `/v1/review/queue` | Items awaiting review (proposed status) |
| GET | `/v1/review/conflicts` | Items with unresolved conflicts |
| POST | `/v1/items/{id}/resolve-conflict` | Resolve a conflict (accept/reject/merge) |

### Export & operations

| Method | Path | Description |
|--------|------|-------------|
| GET | `/v1/export/cca` | Export CCA ledger as JSON (git-friendly) |
| GET | `/health` | Liveness |
| GET | `/ready` | Readiness (DB check) |

---

## 9. Recall Policy

### Startup recall

Deterministic, bounded working set for session initialization.

**Eligibility:** `review_status = 'active' AND valid_to IS NULL`, filtered by caller's visibility scope.

**Ordering:** Scored by the recall ranking formula (Section 4). Pinned items included first.

**Budget:** Accepts `byte_budget` or `token_budget` (approximated as bytes/4). Default from config.

**Output:** `working_set` (rendered text), `item_count`, `byte_count`, `omitted_count`, plus per-item `reasons` array explaining why each item was included.

### Semantic recall

Query-vector matches filtered by visibility scope, scored by cosine similarity + kind weight + recency.

### Why-recalled explanations

Every recalled item includes a `reasons` array:
```json
{
  "score": 0.87,
  "reasons": ["matched workspace", "kind=invariant", "human verified", "semantic similarity 0.78", "pinned"],
  "warnings": ["not confirmed in 90 days"]
}
```

---

## 10. Conflict Handling

### Detection at write time

When `POST /v1/remember` writes a new item, it runs a semantic similarity check against active items in the same scope. Above a threshold, the classifier is asked: "does this contradict, refine, or duplicate?"

- **duplicate** → auto-dedup (existing behavior)
- **refine** → auto-supersede (mark old, write new)
- **contradict** → flag conflict, set `conflicts_with_item_id`, `conflict_type='contradiction'`, `conflict_resolution_status='unresolved'`

### Resolution

Conflicts surface in `GET /v1/review/conflicts`. Resolution via `POST /v1/items/{id}/resolve-conflict` sets the resolution status and optionally invalidates the loser.

---

## 11. Privacy & Safety

- **Sensitivity field** on every memory item: `normal`, `sensitive`, `restricted`.
- **Secret-pattern denylist**: pre-write check blocks content matching common secret patterns (API keys, tokens, passwords).
- **PII-risk classification**: optional LLM check flags content likely containing PII.
- **Hard-delete support**: `DELETE /v1/items/{id}` physically removes (for GDPR/hosted future). Logged in item_events.
- **Read audit**: sensitive reads are logged to recall_logs.

---

## 12. Repo Structure

```
engram/
├── docs/
│   ├── design.md
│   └── backlog.json
├── engram/
│   ├── __init__.py
│   ├── config.py
│   ├── db.py
│   ├── models.py
│   ├── auth.py
│   ├── embeddings.py
│   ├── canonicalize.py
│   ├── classification.py
│   ├── recall.py
│   ├── conflicts.py            ← write-time conflict detection
│   └── api/
│       ├── app.py
│       └── routes/
│           ├── memory.py
│           ├── classify.py
│           ├── review.py       ← review queue + conflict resolution
│           ├── kg.py
│           ├── taxonomy.py
│           ├── diary.py
│           └── export.py
├── migrations/
│   └── 001_init.sql
├── sdk/engram-client/
├── adapters/
│   ├── mcp-server/
│   └── engram-hooks/           ← Hermes lifecycle hooks companion library
├── scripts/
├── docker-compose.yml
├── Dockerfile
└── pyproject.toml
```

---

## 13. Phased Delivery

### Phase 1A — Canonical memory MVP
- Schema with trust fields, review states, FTS, RLS
- `remember`, `recall` (startup mode), `search` (keyword + basic semantic)
- `items` CRUD with cursor pagination
- CCA export
- Import CCA only
- Docker Compose deployment
- Rule-based classification only (no LLM yet)
- **No KG, no LLM classification, no conflict detection yet**

### Phase 1B — Trust and classification
- LLM-backed classification
- Review workflow (propose → activate → dispute → resolve)
- Conflict detection at write time
- Recall explanations ("why recalled")
- Semantic search with HNSW iterative_scan
- Import MemPalace
- Feedback endpoint for usage-informed recall scoring

### Phase 1C — Graph and navigation
- KG triples (with visibility inheritance)
- Tunnels
- Taxonomy browser
- Diary views
- Memory hygiene tools (stale detection, archival)

### Phase 2 — Hermes integration
- MCP adapter (replaces MemPalace MCP server)
- engram-hooks companion library (lifecycle hooks calling classify + remember)
- Hermes config integration
- Retire MemPalace ChromaDB + CCA JSON files

### Phase 3 — Open source readiness
- Naming, docs, README, examples
- Multi-framework quickstarts
- Auth hardening (OAuth/org model for hosted)
- Admin console (review queue, conflict queue, recall logs)
- Helm chart / cloud deployment artifacts

---

## 14. Differentiation

| Feature | mem0 | Letta/MemGPT | Zep/Graphiti | **Engram** |
|---|---|---|---|---|
| Storage | Vector DB | SQLite/Postgres | Graph+vector | **Postgres+pgvector** |
| Memory model | Flat facts | Agent-scoped blocks | Temporal graph | **Structured taxonomy + KG + diary** |
| Multi-agent scoping | Per-agent | Per-agent session | Per-user | **Multi-tenant, workspace-scoped, visibility levels + RLS** |
| Temporal validity | No | No | Yes | **Yes (valid_from/valid_to)** |
| Review / trust states | No | No | No | **Yes (proposed/active/disputed/rejected/archived)** |
| Conflict detection | Basic (dedup on write) | No | Partial | **Yes (contradiction detection, resolution workflow)** |
| Classification | Basic extraction | No | No | **LLM + rule-based, tenant-configurable, no-LLM fallback** |
| Provenance | Minimal | Minimal | Partial | **Source trust, extraction model, verification tracking** |
| Recall explanations | No | No | No | **Yes ("why recalled" reasons array)** |
| Self-hostable | Yes | Yes | Yes | **Yes (Docker Compose)** |

**Engram's wedge:** Trustable institutional memory for agent teams — with review states, provenance, conflict detection, and scoped recall. Not just "store and retrieve," but "know what to trust and why."

---

## 15. Vocabulary

Engram keeps MemPalace's evocative vocabulary. For external audiences, the docs and README provide plain-language equivalents:

| Engram term | Plain-language equivalent |
|---|---|
| Wing | Domain / category |
| Room | Subcategory |
| Drawer / memory item | Memory |
| Tunnel | Cross-category link |
| Diary | Agent-private journal |
| Doctrine | Standing instruction / operating rule |
| Invariant | Must-remain-true constraint |
