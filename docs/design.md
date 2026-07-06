# Engram — Design Document

**Status:** Locked (v2.2, 2026-07-06 — schema completeness)
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

Every memory item has a simple state machine (`review_status`) plus derived signals from other columns and an event log. The lifecycle diagram shows the full trajectory, but only `review_status` is a stored state:

```
observed → proposed → active → recalled → confirmed/stale → superseded/invalidated/archived
```

**What is a state, event, or derived flag:**

| Concept | Type | Where it lives |
|---|---|---|
| `proposed`, `active`, `disputed`, `rejected`, `archived` | **State** (`review_status`) | Column on memory_items |
| `observed` | **Event** | Recorded in item_events (event_type='observed') |
| `recalled` | **Derived flag** | `last_recalled_at IS NOT NULL` |
| `confirmed` | **Derived flag** | `verified_at IS NOT NULL` (human_verified is a convenience alias for this) |
| `stale` | **Derived flag** | Computed from `last_verified_at` (or `valid_from` if never verified): `COALESCE(last_verified_at, valid_from) < now() - stale_after_interval`. Measures whether a memory is unconfirmed, not whether it's unused. `last_recalled_at` feeds only the usage/decay side of recall scoring. NULL `last_recalled_at` does NOT exempt an item from staleness — a never-recalled item is still subject to the staleness check against `valid_from`. |
| `invalidated` | **Derived flag** | `valid_to IS NOT NULL AND superseded_by IS NULL` |
| `superseded` | **Derived flag** | `superseded_by IS NOT NULL` |

This keeps the state machine simple (5 stored states) while the full lifecycle is reconstructable from columns + events.

| State | Meaning | Enters startup recall? | Enters semantic recall? |
|---|---|---|---|
| `proposed` | Written by agent, not yet reviewed | No (auto-promotes per policy below) | Yes (tagged with warnings) |
| `active` | Reviewed and trusted | Yes | Yes |
| `disputed` | Flagged as potentially wrong | Conditional (see below) | Yes (tagged with warnings) |
| `rejected` | Reviewed and rejected (kept for audit) | No | No |
| `archived` | Old/superseded, excluded from default recall | No | No (unless explicitly requested) |

Agents can freely write `proposed` memories. Only `active` memories enter the deterministic startup recall set. High-trust sources (explicit user instruction, verified imports) can write directly to `active`.

### Auto-promotion policy

Without auto-promotion, the proposed queue grows unboundedly while the working set stays frozen — agents write all day but their knowledge never reaches recall. Auto-promotion makes human review exception handling, not the pipeline.

**Auto-promotion conditions** (tenant-configurable, **on by default** with conservative thresholds). An item promotes if it meets EITHER path:

**Path A — Confidence + age (default):**
- `review_status = 'proposed'`
- `memory_confidence >= auto_promote_confidence_threshold` (default 0.7)
- No unresolved conflicts (`conflict_resolution_status IS NULL OR = 'accepted'`). In Phase 1A (before conflict detection exists), this is vacuously true — all proposed items are eligible for promotion.
- Age >= `auto_promote_min_age_hours` (default 72 hours unchallenged)
- No dispute event in `item_events` from another principal

**Path B — Usage-validated (quorum):**
- `review_status = 'proposed'`
- 2+ distinct non-author principals have marked the item "useful" via `/v1/feedback`
- No dispute events

Usage-validated promotion means a memory that multiple agents independently found useful has earned activation — a stronger signal than aging quietly.

A background job (or lazy check on recall) promotes eligible items to `active`. The promotion is logged in `item_events`.

**Disputed high-stakes items:** When a `doctrine` or `invariant` is disputed, it does NOT silently vanish from startup recall. Disputed items of kind `doctrine` or `invariant` stay in startup recall with `warnings: ['disputed — pending resolution']` until resolved. Disputed items of other kinds are excluded from startup recall (standard behavior). This prevents an agent's operating constraints from silently shrinking.

### Semantic recall includes proposed

Semantic recall (`mode=semantic`) returns both `active` and `proposed` items, but proposed items include `warnings: ['unreviewed']`. This allows agents to rediscover their own observations. `rejected` and `archived` items are never returned unless explicitly requested via a `include_archived` parameter.

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

Source trust is calculated from both `source_type` and the principal's type. `memory_confidence` defaults track `source_trust` defaults so auto-promotion works in Phase 1A without LLM classification. LLM classification (1B) refines `memory_confidence` per-item; in 1A, the defaults below are used.

| source_type | principal.type | Authority | Default source_trust | Default memory_confidence | Default review_status |
|---|---|---|---|---|---|
| `manual` | `user` | explicit_user | 0.9 | 0.9 | `active` |
| `manual` | `agent` | trusted_agent | 0.6 | 0.5 | `proposed` |
| `manual` | `admin` | explicit_user | 0.9 | 0.9 | `active` |
| `import` | `system` | trusted_import | 0.8 | 0.8 | `active` |
| `migration` | `system` | trusted_import | 0.8 | 0.8 | `active` |
| `extraction` | `agent` | inferred | 0.5 | 0.5 | `proposed` |
| `sync_turn` | `agent` | inferred | 0.4 | 0.4 | `proposed` |
| `pre_compress` | `agent` | inferred | 0.3 | 0.3 | `proposed` |

Note: `sync_turn` and `pre_compress` have confidence below the 0.7 auto-promotion threshold — they stay `proposed` until an LLM classification (1B) or human review raises their confidence, or a quorum of 2+ distinct non-author agents marks the item "useful" via `/v1/feedback` (usage-validated promotion — a memory multiple agents independently found useful has earned activation more honestly than one that aged 72 hours quietly). This is intentional: chatty low-confidence sources should not auto-promote without some signal that the memory is actually useful.

**Phase 1A phasing note:** In practice, the frozen-queue concern is resolved by sequencing — Phase 1A's only writers are imports and manual user actions (both default `active`), since agent write paths (`sync_turn`, `pre_compress`) arrive with engram-hooks in Phase 2, by which point Phase 1B's LLM classification refines confidence above the gate. The auto-promotion machinery is ready for when agent writers come online.

All defaults are tenant-configurable via the `tenant_config` table.

**Authority hierarchy** (used in conflict resolution): `explicit_user > trusted_import > trusted_agent > untrusted_agent > inferred`

### Recall ranking formula

Startup recall uses a scoring formula to order items within the budget:

```
score = (importance * 0.30)
      + (source_trust * 0.25)
      + (memory_confidence * 0.20)
      + (recency_bonus * 0.15)
      + (human_verified_bonus * 0.10)
```

Where:
- `recency_bonus` = decay function based on `last_recalled_at` (recently recalled = relevant)
- `human_verified_bonus` = 1.0 if verified, 0.0 otherwise

**Pinning is a pure bypass, not a score component.** Pinned items are not scored by the formula — they're inserted first, outside the scoring pipeline. The previous `pinned_bonus * 0.10` weight has been redistributed (importance +0.05, source_trust +0.05). The bypass mechanism: pinned active items are included first, capped at `max_pinned_tokens` (default 2048). Excess pinned items are ordered by importance × source_trust and dropped; `pinned_omitted_count` in the response tells the caller truncation occurred. After pinned items consume their budget, the scorer fills the remainder.

**Anti-feedback-loop guardrail:** `recency_bonus` rewards recent recall for semantic continuity, but a `repeated_startup_penalty` applies when an item has been recalled in startup mode N times (default 5) without positive feedback via `POST /v1/feedback`. The penalty reduces the recency component by 0.5× per excess recall, preventing the same memories from permanently dominating startup recall.

**Penalty safeguards** (prevent over-punishment in autonomous fleets where humans rarely review):
- **Floor:** The penalty cannot reduce the recency component below 0.1 (an invariant recalled 500 times should never score below a random observation).
- **Multi-agent quorum:** Feedback from 2+ distinct non-author agents counts as a partial penalty reset (0.5× weight), not just user/admin feedback. This prevents penalty accumulation on load-bearing memories in fleets where humans review rarely.
- **Pinned exemption:** Pinned items bypass scoring entirely, so the penalty counter does not apply to them.

**Feedback authority weighting:** To prevent agents from self-entrenching their own memories, feedback is weighted by principal authority:
- `user` feedback: full weight (resets penalty counter, adjusts importance)
- `agent` feedback on own memories: zero weight on penalty reset (an agent marking its own recalled items "useful" does NOT reset the penalty)
- `agent` feedback on another agent's memories: partial weight (0.5×) on importance. 2+ distinct non-author agents together count as a partial reset (quorum, see above).
- Only `user` or `admin` feedback fully resets the `startup_recall_count` penalty counter

**Write-path cost escape valve:** If the trust machinery's per-`remember` cost (dedup check + classification + conflict similarity check) proves too expensive for chatty sources like `sync_turn`, a fast-path exists: low-trust proposed writes (source_trust < 0.5) defer the conflict similarity check to promotion time instead of write time. This is a tenant-configurable flag (`conflict_check_on_write`, default true). This is a planned option, not a Phase 1A deliverable.

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
- **Trust** — review_status, memory_confidence, source_trust, verified_by, verified_at, review_notes (human_verified is a derived convenience: `verified_at IS NOT NULL`)
- **Recall signals** — importance, pinned, last_recalled_at, recall_count, startup_recall_count, last_verified_at
- **Provenance** — source_type, source_session, source_uri, extracted_by_model, extraction_confidence
- **Conflict tracking** — conflicts_with_item_id, conflict_type, conflict_resolution_status
- **Privacy** — sensitivity (normal/sensitive/restricted)
- **External linkage** — external_id, external_source
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

### Tables (15 total)

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
| `recall_logs` | Recall audit with item_ids, scoring_version, config_version for feedback loops and reproducibility |
| `deletion_events` | Tombstone records for hard-deleted items (GDPR/hosted) |
| `feedback_events` | Per-item feedback (useful/noise) from principals — drives penalty resets, importance, quorum |
| `tenant_config` | Versioned tenant-configurable trust defaults, scoring weights, recall policy |

### Key schema decisions

- **Check constraints** on all enum-like fields (kind, visibility, review_status, source_type, sensitivity, conflict_type) to prevent taxonomy drift.
- **Full-text search** via `content_tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', coalesce(content, ''))) STORED` + GIN index. A generated column is used instead of a trigger — it's simpler, cannot drift, and requires no plpgsql function. Essential for keyword search on IDs, paths, names.
- **Separate embeddings table** keyed by `(memory_item_id, embedding_model)` with denormalized `tenant_id` for RLS. A composite FK `(memory_item_id, tenant_id) → memory_items(id, tenant_id)` enforces that the denormalized tenant_id always matches the parent item — this is a multi-tenant safety boundary enforced at the database level. A model change is new rows, not a migration. Old vectors remain queryable during re-embedding. The denormalized tenant_id enables RLS policy enforcement directly on the embeddings table, allowing tenant filtering BEFORE the join to memory_items — critical for HNSW + iterative_scan performance.
- **Unique dedup index** on `(tenant_id, workspace_id, principal_id, content_hash) WHERE valid_to IS NULL AND review_status != 'rejected'` with `NULLS NOT DISTINCT` (Postgres 15+). Makes remember idempotent for retries within scope, including tenant-level memories with NULL workspace_id.
- **Row Level Security** on all tenant-scoped tables. Application uses `SET LOCAL` inside each transaction (pool-safe for PgBouncer); Postgres enforces isolation even when application code is wrong. RLS is enabled on: memory_items, memory_embeddings (denormalized tenant_id), kg_triples, tunnels, item_events, classification_rules, recall_logs, workspace_members, api_keys, tenant_config, deletion_events, feedback_events, workspaces, principals. Semantic search queries filter on embeddings.tenant_id BEFORE joining to memory_items, which is the query to load-test first.
- **HNSW with iterative_scan** — requires pgvector 0.8+. Set `hnsw.iterative_scan = strict` at query time to handle filtered (tenant-scoped) queries without recall degradation.
- **Security-invoker views** — `active_memories` and `cca_ledger` use `WITH (security_invoker = true)` so RLS policies apply through the view. Without this, Postgres views are security-definer by default and can bypass RLS. Requires Postgres 15+ (already required for `NULLS NOT DISTINCT`).

### KG visibility

KG triples inherit visibility from their `source_item_id` at query time. A private memory item that spawns a triple does NOT leak that knowledge — querying the KG checks the source item's visibility against the caller's scope.

**Source-less triple policy:** Every KG triple must be traceable to a memory item. If `POST /v1/kg` is called without `source_item_id`, the endpoint auto-creates a system-generated backing memory item (kind='fact', source_type='extraction', review_status='proposed', visibility='workspace') and links the triple to it. This ensures every triple has a visibility source and an audit trail. Manual triples without provenance are never allowed — they must go through the backing item.

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
| POST | `/v1/kg` | Add triple (requires source_item_id; auto-creates backing item if none provided) |
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

Bounded working set for session initialization. 

**Determinism contract:** Startup recall is "deterministic given state" — same corpus + same item states + same config version = same output. It is NOT "deterministic across time" because recall mutates state (last_recalled_at, recall_count) which feeds the scoring formula. For audit reproducibility, `recall_logs` records `scoring_version` and `config_version` so any past recall can be replayed.

**Eligibility:** `review_status = 'active' AND valid_to IS NULL`, filtered by caller's visibility scope. (Disputed doctrine/invariant items are included with warnings per Section 3.)

**Ordering:** Scored by the recall ranking formula (Section 4). Pinned items included first.

**Budget:** Accepts `byte_budget` or `token_budget` (approximated as bytes/4). Default from config.

**Output:** `working_set` (rendered text), `item_count`, `byte_count`, `omitted_count`, `pinned_omitted_count`, `scoring_version`, `config_version`, plus per-item `reasons` array explaining why each item was included.

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

- **duplicate** → auto-dedup (return existing item)
- **refine** → conditional supersession (see below)
- **contradict** → flag conflict, set `conflicts_with_item_id`, `conflict_type='contradiction'`, `conflict_resolution_status='unresolved'`

### Refine supersession rules

Auto-supersession on "refine" is **conditional**, not automatic:

| Condition | Action |
|---|---|
| New item has high source_trust AND classifier confidence ≥ 0.8 | Auto-supersede (mark old, write new) |
| New item has medium confidence OR source_trust mismatch | Proposed supersession: write new as `proposed`, link via `conflicts_with_item_id` with `conflict_type='stale'`, require review |
| New item is from a lower-authority source than old | Never auto-supersede. Flag as `conflict_type='scope_overlap'` for manual review |

Authority hierarchy: `explicit_user > trusted_import > trusted_agent > untrusted_agent > inferred`. A lower-authority source can never silently replace a higher-authority memory.

**1:N conflict limitation (v1):** `conflicts_with_item_id` is a single column, meaning an item can directly conflict with one other item. Three agents writing mutually contradictory versions can't be fully represented in v1 — only pairwise conflicts are tracked. A `memory_conflicts` join table is the eventual solution for multi-way conflicts; for 1B, the single-column approach covers the common case (new vs existing). Document this as a known v1 limitation.

### Tenant-configurable trust constants

All trust defaults and formula weights are tenant-configurable, not hardcoded:
- Source trust defaults (the table above) are defaults overridable via tenant config
- Scoring formula weights are versioned and stored in tenant config
- `recall_logs` records `scoring_version` and `config_version` for full audit reproducibility — any past recall can be replayed with the config that produced it
- This allows A/B testing different scoring policies and rolling forward/backward without losing auditability

### Resolution

Conflicts surface in `GET /v1/review/conflicts`. Resolution via `POST /v1/items/{id}/resolve-conflict` sets the resolution status and optionally invalidates the loser.

---

## 11. Privacy & Safety

- **Sensitivity field** on every memory item: `normal`, `sensitive`, `restricted`.
- **Secret-pattern denylist**: pre-write check blocks content matching common secret patterns (API keys, tokens, passwords).
- **PII-risk classification**: optional LLM check flags content likely containing PII.
- **Hard-delete support**: `DELETE /v1/items/{id}` physically removes the item. Because `item_events` has a FK to `memory_items`, physical deletion would break the audit trail. Instead, a `deletion_events` table captures tombstone metadata (deleted_item_id, content_hash, deleted_by, reason, deleted_at) WITHOUT storing the deleted content. This proves deletion occurred for GDPR compliance without orphaning FK references. **Cascade behavior:** Hard-deleting an item that is `source_item_id` for KG triples cascades: those triples are invalidated (`valid_to = now()`) or deleted, and recorded in the tombstone. Hard-deleting an item in a `superseded_by` chain nullifies the FK (`ON DELETE SET NULL` is the column constraint), so supersession chains don't break — the chain simply loses the deleted node. Note: this means a predecessor whose successor was hard-deleted changes from "superseded" to "invalidated" in derived lifecycle queries (its `valid_to` is still set, so it does not resurrect). The tombstone preserves the original audit trail.
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
