# Engram — Design Document

**Status:** Locked (v2.3, 2026-07-08 — positioning broadened)
**Product:** Trustable memory infrastructure for AI agents, assistants, and teams

> **Implementation status (2026-07-08, post-MVP dogfood).** This document is the
> *design*; it is intentionally not rewritten as features land. The reality, as
> of the BL-010 documentation truth pass: the MVP is **implemented and
> dogfood-deployed**. Inline `> Implementation status:` notes below mark which
> designed behaviors are implemented, dogfood-verified, or deferred. The
> authoritative capability matrix is in `README.md`; the execution backlog and
> post-MVP work are in `docs/plans/engram-mvp-backlog.md`.
>
> Implemented & verified: all 38 REST routes (remember, startup + semantic
> recall, keyword/semantic/hybrid search, item CRUD + audited PATCH,
> review/verify/supersede, conflict detection + resolution, feedback,
> knowledge graph with visibility inheritance, taxonomy, tunnels, diary,
> hygiene, classification, export), API-key auth + admin, RLS, the Python SDK,
> the MCP adapter (dogfood-verified over the network), and a live auth-enabled
> deployment with backup/restore (`docs/ops/dogfood-verification.md`).
>
> Implemented, not yet live-verified: embedding backfill and the OpenAI embedding
> path (mocked-tested only — the live OpenAI checklist in `docs/embeddings.md` is
> not yet recorded; the dogfood runs with embeddings disabled).
>
> Deferred (post-MVP): a recorded end-to-end Hermes dogfood run of engram-hooks'
> automatic lifecycle capture (the shim itself is implemented, unit-tested, and
> profile-wired as of ENG-HERMES-001 — see `docs/ops/hermes-dogfood-profile.md`;
> only the real-Hermes-checkout verification is outstanding), auto-promotion
> Path B (usage quorum), hard-delete + `deletion_events` tombstones, PII-risk
> classification, sensitive-read audit logging, and Phase 3 open-source
> packaging.

---

## 1. What Engram Is

Engram is a standalone memory service that gives AI systems structured, durable, and **trustable** memory. It works for a single long-running assistant, and it is designed for the harder case: multiple agents sharing memory without corrupting, overwriting, or blindly trusting each other.

It is not a flat key-value memory store. Engram is a memory system of record with taxonomy, relationships, temporal validity, review states, provenance, conflict detection, and explainable recall.

The name comes from neuroscience: an **engram** is the physical trace a memory leaves in brain tissue — the literal substrate of stored memory.

### Product thesis

The hard problem in AI memory is not "can we store and recall facts?" — that's solved. The hard problem is: **can an AI system trust what it remembers, know where it came from, know whether it is still true, and safely act on it?**

That matters for a single assistant remembering a user's preferences across sessions. It matters even more for coding agents, research agents, support agents, operations agents, and multi-agent teams that share institutional knowledge.

Engram is designed as a trust system, not just a storage system.

Single-agent memory is the on-ramp. Multi-agent institutional memory is the moat.

### Target users

* **Individual builders / single-agent setups:** developers running one assistant or coding agent that needs durable, self-hosted, auditable memory across sessions
* **Coding-agent users:** users who want agents to retain project context, repo conventions, architectural decisions, backlog state, and prior review decisions
* **Self-hosted agent fleets:** Zutfen LLC's own agent fleet across Hermes profiles
* **Open source:** developers and teams running AI agents that need structured, trustable memory
* **Hosted future:** managed Engram Cloud for users and teams that do not want to self-host

---

## 2. Architectural Principles (Locked)

1. **Standalone service, not agent-coupled.** Engram is its own process, its own repo, its own deployment. Agent frameworks are clients.

2. **Postgres + pgvector as the single storage backend.** No storage abstraction layer. YAGNI until real demand for alternatives.

3. **Multi-tenant from day one.** Every table has `tenant_id`. Row Level Security enforces isolation at the database level — one forgotten WHERE clause cannot cause a cross-tenant leak.

4. **REST core, MCP/SDK as thin wrappers.** The service exposes a clean HTTP API. MCP is one client adapter. Any framework can adopt it.

5. **Content is append-first; metadata changes are audited.** Memory content is never UPDATEd. Supersession/invalidation marks old rows. Metadata changes such as wing, room, visibility, and review status are recorded in `item_events` for full audit trail.

6. **Principal/workspace scoping is first-class.** Memory items belong to a tenant, workspace, and principal. Visibility levels and workspace membership control access.

7. **The memory model concepts are the product, not the storage.** Wings/rooms/drawers, knowledge graph triples, tunnels, and diaries are Engram's product vocabulary.

8. **Classification intelligence is a service feature; lifecycle hooks are client-side.** The service provides content classification — what kind, what wing, what room — as an endpoint. Lifecycle hooks such as when to extract, when to promote, and when to write are client-side because they are framework-specific.

9. **Trust is a product feature, not an implementation detail.** Review states, provenance, confidence, conflict detection, and recall explanations are core to the product — they elevate Engram from "memory store" to "trustable memory infrastructure."

10. **Single-agent deployments must remain first-class.** Engram must be useful when there is one user and one assistant. Multi-agent collaboration should be an additive strength, not a prerequisite for value.

---

## 3. Memory Lifecycle

Every memory item has a simple state machine (`review_status`) plus derived signals from other columns and an event log. The lifecycle diagram shows the full trajectory, but only `review_status` is a stored state:

```text
observed → proposed → active → recalled → confirmed/stale → superseded/invalidated/archived
```

**What is a state, event, or derived flag:**

| Concept                                                  | Type                        | Where it lives                                                     |
| -------------------------------------------------------- | --------------------------- | ------------------------------------------------------------------ |
| `proposed`, `active`, `disputed`, `rejected`, `archived` | **State** (`review_status`) | Column on `memory_items`                                           |
| `observed`                                               | **Event**                   | Recorded in `item_events` with `event_type='observed'`             |
| `recalled`                                               | **Derived flag**            | `last_recalled_at IS NOT NULL`                                     |
| `confirmed`                                              | **Derived flag**            | `verified_at IS NOT NULL`; `human_verified` is a convenience alias |
| `stale`                                                  | **Derived flag**            | Computed from `last_verified_at` or `valid_from` if never verified |
| `invalidated`                                            | **Derived flag**            | `valid_to IS NOT NULL AND superseded_by IS NULL`                   |
| `superseded`                                             | **Derived flag**            | `superseded_by IS NOT NULL`                                        |

Staleness measures whether a memory is unconfirmed or aging out of confidence, not whether it is unused. `last_recalled_at` feeds only the usage/decay side of recall scoring. A NULL `last_recalled_at` does not exempt an item from staleness — a never-recalled item is still subject to the staleness check against `valid_from`.

This keeps the state machine simple while the full lifecycle remains reconstructable from columns and events.

| State      | Meaning                                         | Enters startup recall?   | Enters semantic recall?         |
| ---------- | ----------------------------------------------- | ------------------------ | ------------------------------- |
| `proposed` | Written by agent, not yet reviewed              | No, unless auto-promoted | Yes, tagged with warnings       |
| `active`   | Reviewed or trusted                             | Yes                      | Yes                             |
| `disputed` | Flagged as potentially wrong                    | Conditional              | Yes, tagged with warnings       |
| `rejected` | Reviewed and rejected, kept for audit           | No                       | No                              |
| `archived` | Old or superseded, excluded from default recall | No                       | No, unless explicitly requested |

Agents can freely write `proposed` memories. Only `active` memories enter the deterministic startup recall set. High-trust sources such as explicit user instruction and verified imports can write directly to `active`.

For single-agent deployments, this still matters: the assistant should not blindly promote every low-confidence inference into durable operating memory.

For multi-agent deployments, this becomes critical: one agent's guess should not silently become another agent's trusted instruction.

### Auto-promotion policy

> **Implementation status:** Auto-promotion **Path A is implemented** (BL-004,
> hardened by ENG-AUD-007) as a *bounded, tenant-scoped* lazy check inside
> `POST /v1/recall` (`mode=startup`, before active items are selected) plus the
> `engram promote-proposed` CLI and the `POST /v1/admin/promote` endpoint — all
> three share one service function
> (`engram.promotion.auto_promote_proposed_memories`) so the gates below can
> never drift apart between entry points. Honors the `tenant_config.auto_promote_*`
> thresholds. **Path B (usage-validated quorum) is deferred** — post-MVP. The
> machinery (feedback, recall logs) exists; only the quorum-based promotion path
> is not yet wired.

Without auto-promotion, the proposed queue grows unboundedly while the working set stays frozen — agents write all day but their knowledge never reaches recall. Auto-promotion makes human review exception handling, not the pipeline.

**Auto-promotion conditions** are tenant-configurable and on by default with conservative thresholds. An item promotes if it meets either path.

#### Path A — Confidence + age

All of the following must hold (ENG-AUD-007 closes F11/F12/F13 against this
list — every gate below is enforced, not aspirational):

* `review_status = 'proposed'`
* `memory_confidence >= auto_promote_confidence_threshold`, default `0.7`
* Age >= `auto_promote_min_age_hours`, default 72 hours unchallenged
* No unresolved conflict at write time: `conflict_resolution_status IS NULL OR = 'accepted'`
* No dispute event from another principal (`engram.promotion.has_external_dispute_event`) —
  either an `item_events` row with `event_type='review_change'`,
  `field_name='review_status'`, `new_value='disputed'` whose actor is not the
  item's own principal, or a `feedback_events` row with `verdict='noise'`
  recorded by another principal. The item's own creator disputing or giving
  negative feedback on their own item does **not** block promotion — only an
  external signal does.
* A **promotion-time conflict recheck** against currently-active memories
  passes (`engram.conflicts.check_promotion_conflict`) — a write-time "clean"
  status does not guarantee a later active write hasn't since created a
  conflict. The recheck considers up to `settings.promotion_conflict_candidate_k`
  (default 5) plausible active-item candidates, not only the single nearest
  one (top-k, not top-1 — see below). A blocking recheck marks the item
  `conflict_resolution_status='unresolved'`, sets `conflicts_with_item_id`,
  and writes a `conflict_resolution` item event so the block is auditable and
  idempotent (a later scan sees the write-time `skipped_conflict` gate instead
  of re-running the recheck).
* `tenant_config.auto_promote_enabled` is true

**Lazy startup-recall promotion** (F11): every `POST /v1/recall` call with
`mode=startup` runs `engram.promotion.maybe_auto_promote_for_startup_recall`
before `_fetch_active_items`, bounded by `settings.startup_promotion_limit`
(default 20 proposed items scanned per call) so recall latency stays
predictable regardless of how large a tenant's proposed backlog grows. A
disabled tenant (`auto_promote_enabled=false`) pays only a single `COUNT`
query. Semantic recall (`mode=semantic`) does **not** trigger this pass in
this slice — that remains a deliberate scope boundary, not an oversight.

**Conflict candidate selection is top-k, not top-1** (F13): both the recheck
above and `find_promotion_conflict_candidates` scope candidates to the same
tenant/kind, same workspace when the item is workspace-scoped (tenant-wide
otherwise), and fetch up to `k` embedding-nearest active items — not just the
single nearest — since the actual conflicting item is not always the nearest
neighbour by embedding distance.

**Embeddings-off fallback** (F13, partial by design): when the promotion
candidate has no stored embedding (`embedding_provider='none'`, or the row
predates embedding generation), the recheck falls back to a structural
heuristic — active items in scope sharing `subject_type`+`subject_id` or
`subject_name`, whose `content_hash` differs from the candidate's. Any match
is treated conservatively as a block; an exact `content_hash` match (the same
content re-observed, e.g. by a different principal) is never flagged. This
fallback has real limits — it has no semantic understanding and will miss
conflicts that don't share an explicit subject field — but it prevents
"embeddings disabled" from silently disabling conflict checking, which was
the original audit gap.

`PromotionResult` (and the admin endpoint's `PromotionResponse`) now report
`skipped_dispute` and `skipped_conflict_recheck` counts alongside the
existing `skipped_confidence` / `skipped_age` / `skipped_conflict` /
`skipped_disabled`, so promotion is auditable without log scraping.

#### Path B — Usage-validated quorum

* `review_status = 'proposed'`
* 2+ distinct non-author principals have marked the item useful via `/v1/feedback`
* No dispute events

Usage-validated promotion means a memory that multiple agents independently found useful has earned activation — a stronger signal than aging quietly.

In single-agent deployments, Path A is the normal path. In multi-agent deployments, Path B allows fleet behavior to surface useful memories without requiring constant human review.

A background job, scheduled CLI invocation, or lazy check on recall promotes eligible items to `active`. The promotion is logged in `item_events`.

**Disputed high-stakes items:** When a `doctrine` or `invariant` is disputed, it does not silently vanish from startup recall. Disputed items of kind `doctrine` or `invariant` stay in startup recall with warnings such as `['disputed — pending resolution']` until resolved. Disputed items of other kinds are excluded from startup recall by default.

This prevents an assistant's operating constraints from silently shrinking.

### Semantic recall includes proposed

> **Implementation status:** Semantic recall (`POST /v1/recall mode=semantic`) is
> **implemented** (BL-003): query-embedding similarity over active **and
> proposed** items (proposed tagged `warnings: ["unreviewed"]`), visibility-scoped,
> budget-bounded, excluded `rejected`/`archived` unless requested, and logged to
> `recall_logs`. It reuses the search embedding machinery, so it is inert when
> `ENGRAM_EMBEDDING_PROVIDER=none` (the dogfood default) — keyword/startup recall
> are unaffected.

Semantic recall (`mode=semantic`) returns both `active` and `proposed` items, but proposed items include warnings such as `['unreviewed']`.

This allows agents to rediscover their own observations without treating them as trusted startup context.

`rejected` and `archived` items are never returned unless explicitly requested through an explicit archive/history parameter.

---

## 4. Trust Model

### Confidence layers

| Field                   | What it means                              | Example                                   |
| ----------------------- | ------------------------------------------ | ----------------------------------------- |
| `source_trust`          | Trust in where this came from              | User said it = 0.9; agent guessed = 0.4   |
| `memory_confidence`     | Overall confidence this memory is accurate | Verified fact = 0.95; LLM inference = 0.6 |
| `extraction_confidence` | Confidence of the extraction process       | Direct quote = 0.9; LLM summary = 0.5     |
| `human_verified`        | A human has confirmed this is true         | Boolean                                   |

Trust is not binary. It is layered so that Engram can distinguish between:

* what was said
* who said it
* how it was extracted
* how confident the system is
* whether a human verified it
* whether it has been challenged or superseded

### Source trust defaults

Source trust is calculated from both `source_type` and the principal's type. `memory_confidence` defaults track `source_trust` defaults so auto-promotion works in Phase 1A without LLM classification. LLM classification in Phase 1B refines `memory_confidence` per item. In Phase 1A, the defaults below are used.

| source_type    | principal.type | Authority      | Default source_trust | Default memory_confidence | Default review_status |
| -------------- | -------------- | -------------- | -------------------- | ------------------------- | --------------------- |
| `manual`       | `user`         | explicit_user  | 0.9                  | 0.9                       | `active`              |
| `manual`       | `agent`        | trusted_agent  | 0.6                  | 0.5                       | `proposed`            |
| `manual`       | `admin`        | explicit_user  | 0.9                  | 0.9                       | `active`              |
| `import`       | `system`       | trusted_import | 0.8                  | 0.8                       | `active`              |
| `migration`    | `system`       | trusted_import | 0.8                  | 0.8                       | `active`              |
| `extraction`   | `agent`        | inferred       | 0.5                  | 0.5                       | `proposed`            |
| `sync_turn`    | `agent`        | inferred       | 0.4                  | 0.4                       | `proposed`            |
| `pre_compress` | `agent`        | inferred       | 0.3                  | 0.3                       | `proposed`            |

`sync_turn` and `pre_compress` have confidence below the default 0.7 auto-promotion threshold. They stay `proposed` until LLM classification or human review raises their confidence, or until a quorum of 2+ distinct non-author principals marks the item useful via `/v1/feedback`.

This is intentional: chatty low-confidence sources should not auto-promote without some signal that the memory is actually useful.

**Phase 1A phasing note:** In practice, the frozen-queue concern is resolved by sequencing. Phase 1A's only writers are imports and manual user actions, both of which default to `active`. Agent write paths such as `sync_turn` and `pre_compress` arrive with Engram hooks in Phase 2, by which point Phase 1B's LLM classification refines confidence above the gate. The auto-promotion machinery is ready for when agent writers come online.

All defaults are tenant-configurable through the `tenant_config` table.

### Authority hierarchy

Authority hierarchy is used in conflict resolution and supersession:

```text
explicit_user > trusted_import > trusted_agent > untrusted_agent > inferred
```

A lower-authority source can never silently replace a higher-authority memory.

Examples:

* A coding agent cannot silently override an explicit user instruction.
* A low-confidence extracted summary cannot silently replace a verified project decision.
* An inferred preference cannot silently replace a manually supplied user preference.
* An agent-private observation cannot silently become tenant-wide doctrine.

### Single-agent trust still matters

Even with only one assistant and one user, trust machinery is valuable because the assistant may write memories from different sources:

* explicit user statements
* summaries of conversations
* inferred preferences
* imported project documentation
* compressed session notes
* tool observations
* failed assumptions
* decisions that later become stale

A single assistant should not treat all of those equally.

### Multi-agent trust matters more

With multiple agents, trust machinery becomes mandatory. Shared memory without provenance and authority rules will eventually produce:

* overwritten instructions
* conflicting project assumptions
* agents reinforcing each other's guesses
* stale decisions treated as current
* private observations leaking into shared recall
* one agent's speculative summary becoming another agent's operating rule

Engram is designed to prevent those failure modes.

---

## 5. Recall

Startup recall uses a scoring formula to order items within the budget:

```text
score = (importance * 0.30)
      + (source_trust * 0.25)
      + (memory_confidence * 0.20)
      + (recency_bonus * 0.15)
      + (human_verified_bonus * 0.10)
```

Where:

* `recency_bonus` = decay function based on `last_recalled_at`
* `human_verified_bonus` = 1.0 if verified, 0.0 otherwise

### Pinned memories

Pinning is a pure bypass, not a score component.

Pinned items are not scored by the formula. They are inserted first, outside the scoring pipeline. Pinned active items are included first, capped at `max_pinned_tokens`, default 2048.

Excess pinned items are ordered by `importance × source_trust` and dropped. `pinned_omitted_count` in the response tells the caller truncation occurred.

After pinned items consume their budget, the scorer fills the remainder.

### Anti-feedback-loop guardrail

`recency_bonus` rewards recent recall for semantic continuity, but a `repeated_startup_penalty` applies when an item has been recalled in startup mode N times, default 5, without positive feedback via `POST /v1/feedback`.

The penalty reduces the recency component by 0.5× per excess recall, preventing the same memories from permanently dominating startup recall.

### Penalty safeguards

To prevent over-punishment in autonomous fleets where humans rarely review:

* **Floor:** The penalty cannot reduce the recency component below 0.1.
* **Multi-agent quorum:** Feedback from 2+ distinct non-author agents counts as a partial penalty reset at 0.5× weight.
* **Pinned exemption:** Pinned items bypass scoring entirely, so the penalty counter does not apply to them.

An invariant recalled 500 times should never score below a random observation.

### Feedback authority weighting

To prevent agents from self-entrenching their own memories, feedback is weighted by principal authority:

* `user` feedback: full weight; resets penalty counter and adjusts importance
* `admin` feedback: full weight
* `agent` feedback on own memories: zero weight on penalty reset
* `agent` feedback on another agent's memories: partial weight on importance
* 2+ distinct non-author agents together count as a partial reset

Only `user` or `admin` feedback fully resets the `startup_recall_count` penalty counter.

### Recall explanations

Every recalled item includes a `reasons` array explaining why it was included.

Examples:

```text
["pinned"]
["high_importance", "human_verified"]
["recently_recalled", "high_source_trust"]
["semantic_match", "unreviewed"]
["disputed — pending resolution"]
```

Recall should be inspectable. Agents and humans should be able to see why a memory appeared in context.

---

## 6. Visibility, Scope, and Tenancy

Engram is designed for both simple and complex deployments.

A single user can run one tenant, one workspace, and one assistant.

A team can run one tenant with multiple workspaces and multiple agents.

A hosted deployment can run many tenants with strict isolation.

### Visibility levels

| Visibility  | Who can read                            |
| ----------- | --------------------------------------- |
| `private`   | Only the principal that wrote it        |
| `workspace` | Any principal in the same workspace     |
| `tenant`    | Any principal in the organization       |
| `public`    | Any authenticated caller, where enabled |

The default visibility is `workspace`.

### Principal model

A principal is any actor that can write, recall, or review memory.

Principal types include:

* `user`
* `agent`
* `admin`
* `system`

Memory items belong to:

* tenant
* workspace
* principal
* visibility scope

### Row Level Security

Row Level Security is enforced at the Postgres level.

One forgotten `WHERE` clause cannot cause a cross-tenant leak.

Application-level filtering is still required for correctness and performance, but database-level RLS is the security backstop.

---

## 7. Memory Topology

Engram's memory model is intentionally more structured than a flat vector store.

### Wings and rooms

Wings and rooms provide a memory-palace taxonomy:

* **Wing:** top-level domain or category
* **Room:** subcategory within a wing

Examples:

| Wing      | Room           | Memory type               |
| --------- | -------------- | ------------------------- |
| `project` | `architecture` | design decision           |
| `project` | `backlog`      | completed or pending work |
| `user`    | `preferences`  | user preference           |
| `ops`     | `deployment`   | infrastructure note       |
| `product` | `positioning`  | strategic decision        |
| `agent`   | `behavior`     | standing instruction      |

The taxonomy is tenant-configurable.

### Memory kinds

Memory items can have kinds such as:

* `fact`
* `preference`
* `decision`
* `doctrine`
* `invariant`
* `procedure`
* `observation`
* `summary`
* `diary_entry`

Kinds affect review behavior, recall behavior, and dispute handling.

### Tunnels

Tunnels are cross-category links between memories, rooms, or concepts.

They support the product vocabulary of "this memory belongs here, but it is related to that."

Examples:

* a product decision linked to the GitHub issue that caused it
* a user preference linked to a recurring workflow
* an architecture decision linked to a deployment invariant
* a project memory linked to a coding-agent instruction

Tunnels are planned for Phase 1C.

### Knowledge graph triples

Knowledge graph triples represent explicit relationships with temporal validity.

Example:

```text
subject: "Engram"
predicate: "uses_storage_backend"
object: "Postgres + pgvector"
valid_from: "2026-07-06"
valid_to: null
```

Triples are backed by memory items so that graph facts retain provenance, confidence, and review state.

---

## 8. Write Path

The write path must balance trust, cost, and latency.

A typical write path:

1. Receive memory candidate.
2. Normalize input.
3. Apply source trust defaults.
4. Classify kind, wing, room, and visibility.
5. Deduplicate against existing memories.
6. Check for contradiction or supersession candidates.
7. Persist append-first memory item.
8. Record event in `item_events`.
9. Return memory id, review status, confidence, and warnings.

### Write-path cost escape valve

If the trust machinery's per-`remember` cost proves too expensive for chatty sources such as `sync_turn`, a fast path exists.

Low-trust proposed writes with `source_trust < 0.5` may defer the conflict similarity check to promotion time instead of write time.

This is tenant-configurable via `conflict_check_on_write`, default true.

This is a planned option, not a Phase 1A deliverable.

---

## 9. Read Path

Engram supports multiple recall and search modes.

### Startup recall

Startup recall returns a deterministic, bounded working set of active memories for an agent or assistant.

Use cases:

* load user preferences at session start
* load project constraints
* load standing instructions
* load recent relevant decisions
* load pinned invariants
* load workspace context

Startup recall prioritizes trust, importance, confidence, recency, and verification.

### Semantic recall

Semantic recall is query-driven and may include proposed memories with warnings.

Use cases:

* "What do we know about this project?"
* "Have we already made a decision about this?"
* "What did the user say about deployment preferences?"
* "What are the known gotchas for this repo?"

### Search

Search supports:

* keyword search
* semantic search
* hybrid search

Search can be filtered by:

* tenant
* workspace
* principal
* visibility
* wing
* room
* kind
* review status
* temporal validity
* archived status

---

## 10. Interfaces

### REST API

REST is the core interface.

All other integrations are thin wrappers.

The service exposes APIs for:

* remember
* recall
* search
* item inspection
* classification
* review
* feedback
* promotion
* export
* health and readiness

### Python SDK

The Python SDK is a thin async client over the REST API.

It should not hide the product model. SDK users should still understand review states, visibility, trust, and recall modes.

### MCP adapter

The MCP adapter exposes Engram tools to MCP-compatible clients such as Hermes, Claude Desktop, and other agent runtimes.

MCP tools include:

* `engram_remember`
* `engram_recall`
* `engram_search`
* `engram_classify`
* `engram_kg_query`
* `engram_kg_add`
* `engram_diary_write`

The MCP adapter is a client of the SDK. The SDK is a client of the REST API. The REST API is the canonical interface.

### Framework integration

Agent frameworks are clients.

Engram should not require Hermes, Claude Desktop, LangGraph, AutoGen, CrewAI, OpenAI Assistants, or any other specific framework.

Framework-specific lifecycle hooks belong in adapters.

---

## 11. Architecture

* **Postgres 16 + pgvector** — single storage backend, no abstraction layer
* **FastAPI** — REST core
* **Multi-tenant from day one** — `tenant_id` on every tenant-scoped table
* **RLS on tenant-scoped tables** — database-enforced isolation
* **Append-first content** — content is never silently overwritten
* **Audited metadata events** — lifecycle and metadata changes recorded in `item_events`
* **Separate embeddings table** — model-keyed, supports re-embedding without migration
* **Full-text search** — generated `tsvector` column and GIN index
* **Tenant-configurable policy** — scoring weights, trust defaults, and recall policy stored per tenant
* **REST core, SDK/MCP wrappers** — framework-agnostic by design

### Storage

Postgres is the source of truth.

pgvector supports semantic recall.

Generated `tsvector` columns support full-text search.

No storage abstraction layer is planned until real demand exists.

### Embeddings

> **Implementation status:** The separate `memory_embeddings` table, write-path
> generation, semantic search, conflict-detection similarity, and the
> `engram backfill-embeddings` command (BL-006) are **implemented**. The provider
> is `none` by default; with the OpenAI provider, embeddings are generated on
> `remember` and backfilled idempotently. The backfill is verified with a mocked
> provider; the **live OpenAI path has not been recorded-verified** (see the
> checklist in `docs/embeddings.md`). The dogfood deployment runs with embeddings
> disabled intentionally.

Embeddings are stored separately from memory items.

Embeddings are keyed by model so that re-embedding does not require rewriting memory content or changing the memory item schema.

This supports:

* model migration
* multiple embedding models
* stale embedding detection
* backfill jobs
* embedding regeneration

### Auditability

> **Implementation status:** Append-first content and audited metadata events
> (`item_events`) are **implemented**. **Hard delete** (with `deletion_events`
> tombstones and KG cascade), **PII-risk classification**, and **sensitive-read
> audit logging** are **designed but deferred** — post-MVP. Today, removal is via
> supersession/invalidation (soft removal that preserves the audit trail), not
> physical row deletion.

Memory content is append-first.

Content changes create new memory rows or supersession links.

Metadata changes are recorded as events.

Auditability is part of the trust model, not a compliance afterthought.

---

## 12. Vocabulary

Engram uses evocative naming drawn from memory palace traditions.

| Engram term | Plain-language equivalent             |
| ----------- | ------------------------------------- |
| Wing        | Domain / category                     |
| Room        | Subcategory                           |
| Drawer      | Optional deeper grouping              |
| Memory item | A stored memory                       |
| Tunnel      | Cross-category link                   |
| Diary       | Principal-private journal             |
| Doctrine    | Standing instruction / operating rule |
| Invariant   | Must-remain-true constraint           |

Vocabulary should be evocative in product surfaces but never obscure in API documentation. Every evocative term should have a plain-language equivalent.

---

## 13. Roadmap

### Phase 1A — Canonical memory MVP

> **Implementation status: done.**

Goal: establish the durable memory substrate.

Includes:

* Postgres schema and migrations
* Row Level Security foundation
* full-text search foundation
* pgvector embedding storage foundation
* FastAPI service skeleton
* Docker Compose deployment
* functional endpoints for remember, recall, search, items, and export
* CCA import
* Python SDK

### Phase 1B — Trustable memory workflow

> **Implementation status: done.**

Goal: make memory reviewable and reliable.

Includes:

* LLM classification
* rule-based classification fallback
* review workflow
* promotion workflow
* dispute workflow
* conflict detection
* provenance enrichment
* confidence refinement
* feedback endpoints

### Phase 1C — Rich memory topology

> **Implementation status: done.**

Goal: move beyond flat recall into structured institutional memory.

Includes:

* knowledge graph
* tunnels
* taxonomy browser
* relationship-aware recall
* temporal graph queries
* richer memory inspection

### Phase 2 — Agent integration

> **Implementation status: partial.** The Python SDK, MCP adapter, startup recall,
> and semantic recall are **done and dogfood-verified**. Hermes lifecycle hooks
> (`engram-hooks`) — detection, compatibility shim, guard, idempotent install,
> structured status — are **implemented and unit-tested**
> (ENG-HERMES-001; no longer blocked on the upstream Hermes
> `prepare_memory_write` PR, which is used natively if present and
> monkey-patched around otherwise). A documented Hermes dogfood profile now
> loads it (`docs/ops/hermes-dogfood-profile.md`,
> `profiles/hermes-engram-dogfood.yaml`). What remains post-MVP is a recorded
> end-to-end run against a real Hermes checkout; explicit MCP-driven memory
> capture works today independent of that.

Goal: make Engram useful in real agent workflows.

Includes:

* Hermes integration
* MCP hardening
* lifecycle hooks
* startup recall
* semantic recall
* pre-compression memory capture
* sync-turn ingestion
* coding-agent context patterns

### Phase 3 — Open-source readiness

> **Implementation status: in progress** (this documentation truth pass is part of
> it). Deployment artifacts and a dogfood deployment are landed; the remaining
> security review, example integrations, and release packaging are pending.

Goal: make Engram broadly usable outside Zutfen LLC.

Includes:

* documentation pass
* README positioning
* quickstart examples
* single-agent setup guide
* multi-agent setup guide
* deployment hardening
* security review
* example integrations
* release packaging

### Phase 4 — Hosted future

Goal: managed Engram for users and teams that do not want to self-host.

Includes:

* hosted tenant provisioning
* auth and billing
* managed backups
* hosted admin UI
* usage metering
* organizational controls

---

## 14. Non-Goals

Engram is not:

* a chatbot
* an agent runtime
* a vector database replacement
* a generic document store
* a private notes app
* a prompt manager
* a workflow engine
* a universal knowledge-management UI

Engram is memory infrastructure.

Agent runtimes decide when to write, when to recall, and how to act.

Engram decides how memories are stored, trusted, reviewed, searched, related, and recalled.

---

## 15. Product Positioning

Engram should be described as:

> Trustable memory infrastructure for AI agents, assistants, and teams.

Secondary descriptions:

* durable, auditable memory for AI systems
* self-hostable memory for assistants and coding agents
* institutional memory for multi-agent teams
* explainable recall with provenance and trust
* memory for AI that needs to remember safely

Avoid positioning Engram as only:

* multi-agent memory
* a vector memory store
* a chatbot memory plugin
* a LangChain-style memory abstraction
* a hosted SaaS product before the self-hosted foundation is complete

The correct positioning is broad at the top and specific in the differentiation:

> Engram works for one assistant. It was designed for the harder case: many agents sharing memory safely.

---

## 16. Locked Decisions

The following decisions are locked for this design version:

1. Engram is a standalone service.
2. Postgres + pgvector is the only storage backend.
3. REST is the canonical interface.
4. MCP and SDK are thin wrappers.
5. Multi-tenancy and RLS are foundational.
6. Content is append-first.
7. Metadata changes are audited.
8. Trust model is core product surface.
9. Review states are first-class.
10. Authority hierarchy governs supersession.
11. Single-agent use is first-class.
12. Multi-agent collaboration is a primary differentiator.
13. Lifecycle hooks are client-side.
14. Classification is a service feature.
15. Memory topology is product vocabulary, not implementation trivia.

---

## 17. Summary

Engram is trustable memory infrastructure for AI.

It gives a single assistant durable, inspectable memory across sessions.

It gives coding agents persistent project context without relying on fragile prompt stuffing.

It gives multi-agent teams shared institutional memory with provenance, authority, conflict handling, review states, temporal validity, and explainable recall.

The core belief is simple:

> AI systems should not merely remember. They should remember safely.
