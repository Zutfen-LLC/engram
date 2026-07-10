# Engram v2 Architecture & Product Audit

**Date:** 2026-07-10
**Scope:** Full architectural, retrieval, knowledge-organization, scaling, AI-native, multi-agent, DX, operations, testing, and product-strategy review of the post-remediation codebase (`main` @ `7d13db8`).
**Method:** Line-level read of the core service (`engram/`, `migrations/`, routes, worker, jobs, recall/semantic/relationship pipelines, auth, RLS plumbing), plus SDK, MCP adapter, deployment artifacts, tests, and all planning documents. This audit deliberately does **not** re-litigate the 2026-07-08 memory audit (F1–F20) — all of its P0/P1 items are verified as landed. This is the *next-generation* review: what would make Engram the best memory engine available, given that the foundation is now sound.
**Fixed constraints honored:** PostgreSQL + pgvector, append-first model, RLS, and the trust model are treated as locked. No finding proposes replacing them.

---

## 1. Executive Summary

Engram's foundation is now genuinely strong. The ENG-AUD remediation series closed the entire first-generation gap list: read-path visibility is enforced through one shared predicate, RLS is real (non-owner role + `FORCE`), auth is O(1), supersession is atomic, recall is bounded in SQL before Python scoring, telemetry is async and idempotent, classification confidence is wired into stored trust, kinds are a governed registry, and relationship-aware recall exists. The engineering discipline is unusually high: honest docs, versioned scoring, audit events everywhere, Postgres-backed CI.

This audit finds that the next generation of work clusters around five themes:

1. **The trust model is enforced at write time but is largely advisory at mutation time.** Review transitions, verification, metadata patches, supersession, and feedback are open to any principal in the tenant, accept caller-supplied actor identities, and ignore the read/write/admin scope system everywhere except `/v1/admin/*`. An agent can flip its own proposal to `active` with one POST, mark it `human_verified`, and attribute the action to the user — bypassing every promotion gate the last audit cycle so carefully built. This is the single most important finding in this document (V2-T1..T4).

2. **Retrieval is architecturally good and empirically unmeasured.** There are four hand-tuned scoring formulas (startup v1, semantic-v2 trust blend, semantic-v3 relationship blend, the SQL coarse score) and zero retrieval-quality evaluation. Relationship-aware recall — the flagship of the last cycle — expands over a `memory_edges` graph that **no client or process can write to**; in production it is a no-op running on an empty table. The highest-leverage retrieval investment is not another scoring signal; it is an evaluation harness, then edge population, then temporal and contextual retrieval.

3. **Memory only accumulates; it never gets better.** There is no consolidation, no summarization, no canonical-memory selection, no confidence decay, no semantic garbage collection; `retention.sweep` is a logged no-op. For an append-first system this is the defining long-term risk: the working set's signal-to-noise degrades monotonically. A consolidation engine (cluster → synthesize → supersede with `derived_from` provenance) is the biggest product opportunity available, and Engram's trust/audit substrate makes it uniquely defensible.

4. **Multi-agent is a storage story, not yet a collaboration story.** Agents share rows but cannot learn that a memory they rely on was superseded or disputed (no change feed), cannot earn promotion by quorum (Path B unbuilt despite the machinery existing), and there is no notion of per-principal expertise. The moat the README claims — "institutional memory for agent teams" — needs these to be real.

5. **Operations run on logs and hope.** No metrics, no tracing, no job-queue introspection API, dead jobs are invisible without SQL, and the audit/log tables grow without bound. Fine for dogfood; disqualifying for hosted.

The scorecard below reflects this: the trust *model* is the best in the field (8/10) while AI-native capability (5/10) and observability (4/10) lag what the model makes possible. The backlog in §5 sequences the work: **close the mutation-path trust holes first (cheap, urgent), build the measurement substrate second (evals + metrics), then spend the big tokens on consolidation, temporal recall, and multi-agent collaboration — the things that would make OpenAI/Anthropic/DeepMind pick Engram over building their own.**

---

## 2. Architectural Scorecard

| Dimension | Score | Summary |
|---|:---:|---|
| Architecture | **7/10** | Clean layering and shared predicates; loses points for relationship-primitive sprawl and write/read authorization asymmetry |
| Scalability | **6/10** | Bounded recall, async jobs, replica hooks; unbounded log growth, global HNSW, no queue fairness |
| Retrieval | **6/10** | Trust-weighted hybrid + expansion is a real design; unmeasured, expansion graph is empty, no temporal/contextual/diversity dimensions |
| Trust | **8/10** | Best-in-class model on the write path; mutation paths and scope enforcement undermine it |
| Observability | **4/10** | recall_logs + candidate stats + structured logs; no metrics, no tracing, no queue visibility |
| Operations | **5/10** | Honest deploy docs, backups, idempotent migrations, worker reclaim; retention is a stub, no PITR, no ops APIs |
| API / DX | **6/10** | Coherent REST, good error mapping, SDK + MCP; untyped responses, no TS SDK, no batch writes, config sprawl |
| AI-native capabilities | **5/10** | Explainable recall and the review pipeline are genuinely AI-native; synthesis, introspection, temporal reasoning, self-maintenance absent |
| Maintainability | **7/10** | Exceptional doc/test discipline; magic-weight proliferation, 1,400-line route module, fixed-column tenant config |
| Differentiation | **7/10** | Trust/audit/multi-tenancy are real differentiators; at risk because unproven (no benchmarks) and gated behind adoption gaps (extraction, TS SDK) |

### Score rationale

**Architecture — 7.** The service/route/adapter layering is right and the last cycle's shared-predicate work (`memory_access.eligibility_expression` + `eligibility_sql`) is exactly how cross-cutting rules should be built. Deductions: five overlapping relationship mechanisms (`kg_triples`, `memory_edges`, `tunnels`, `conflicts_with_item_id`, `superseded_by`) that each carry their own semantics, storage, and (partial) APIs; `memory.py` at 1,427 lines mixing HTTP concerns with the write-path state machine; runtime `inspect.signature()` dispatch in two hot paths; and the mutation-endpoint authorization gap detailed under Trust.

**Scalability — 6.** The two-stage recall pipeline, deterministic sub-pool candidate selection, async telemetry with a transactional idempotency claim, and `ENGRAM_READ_DATABASE_URL` are the right bones for read scaling. Deductions: `item_events`, `recall_logs`, and `jobs` grow forever with no partitioning or pruning (retention is a no-op); the RLS policies compare `tenant_id::text = current_setting(...)` which forces a per-row cast instead of an indexable UUID comparison; one global HNSW index serves all tenants; the job queue has no per-tenant fairness and `priority` is sorted *after* `run_after` so it is nearly inert; and every startup recall enqueues one telemetry job row (queue write amplification ∝ recall QPS).

**Retrieval — 6.** Trust-weighted semantic scoring, RRF hybrid fusion, skip-not-break budget packing, and explainable reasons are a solid v1. Deductions: nothing measures whether any of it works (no eval set, no metrics like nDCG/recall@k, no A/B substrate — `scoring_version` exists precisely to enable this and nothing consumes it); graph expansion runs against a table with no write path; there is no diversity control (top-k can be five paraphrases of the same fact, especially cross-principal duplicates); no temporal retrieval despite a bi-temporal-ready schema; startup recall cannot be conditioned on task context (no kind/wing/room filters, no session intent); `contradicts` edges score identically to `supports`.

**Trust — 8.** Layered confidence, authority hierarchy, review states, provenance events, conflict machinery, external-dispute gates, top-k promotion rechecks, and downward-only visibility narrowing form the most complete trust model in any memory system I'm aware of. Deductions are all enforcement gaps: any principal can set `review_status='active'` or `human_verified=true` directly; audit actor identity is caller-supplied; scopes are unenforced outside `/v1/admin`; authority is derived from a tenant-configurable float rather than a stored ordinal, so config edits can invert the hierarchy the docs promise.

**Observability — 4.** `recall_logs` (with scoring/config versions), `CandidateStats`, per-reason promotion counts, and consistent structured logging are genuinely good raw material. But there is no `/metrics`, no OpenTelemetry, no way to see queue depth/dead jobs/embedding coverage/cache hit rates without psql, and no worker liveness signal.

**Operations — 5.** `docs/deployment.md`, the bootstrap-key flow, idempotent `init-db` with baselining, nightly pg_dump + restore smoke test, and lease-based job reclaim are all real. Deductions: retention sweep is a stub; no WAL/PITR guidance; no per-tenant export/delete (hosted prerequisite); no admin surface for jobs, embedding-profile state, or tenant health; migration files have a numbering collision (`005_classification_seed_rules.sql` / `005_jobs.sql`) in a lexicographic-order runner.

**API/DX — 6.** The REST surface is coherent, error mapping is documented, and MCP/SDK line up with the design's "thin wrapper" principle. Deductions: ~a third of endpoints declare `response_model=None` and return ad-hoc dicts (items, review, KG detail), so OpenAPI/SDK types are incomplete; Python-only SDK; no batch remember; no request-scoped idempotency keys beyond content-hash dedup; 40+ env settings plus a fixed-column `tenant_config` where every new knob is a migration; the memory-palace vocabulary is charming but doubles the glossary a new developer must absorb.

**AI-native — 5.** Reasons arrays, warnings, working-set budgets, and the propose→review→promote pipeline are designed for machine consumers — that's ahead of most. But the capabilities that only make sense for AI consumers are absent: no synthesis/consolidation, no "why do I believe this" introspection endpoint (all the data exists — nothing composes it), no uncertainty propagation into derived artifacts, no as-of reasoning, no self-maintenance proposals, and the startup working set is a flat `[kind] content` text blob rather than a structured, citable document.

**Maintainability — 7.** Docstrings explain *why*, invariants are written down where they're load-bearing (supersede ordering, RLS GUC scoping), tests run against real Postgres in CI, and the docs-truth discipline is rare and valuable. Deductions: four scoring formulas with independently hand-tuned constants scattered across three modules; `memory.py` needs decomposition; `tenant_config`'s 25 fixed columns resist evolution; two `005_*` migrations.

**Differentiation — 7.** Nobody else combines append-first audit, database-enforced tenancy, layered trust, and explainable recall. But differentiation unproven is differentiation at risk: Mem0/Zep publish benchmark numbers, Zep/Graphiti own the "temporal knowledge graph" narrative that Engram's schema could beat, and every competitor ingests raw conversations while Engram requires the client to decide what to remember. The moat is real; the bridge to it isn't built.

---

## 3. Findings

Severity scale: **Critical** (undermines a core product promise), **High** (material capability or integrity gap), **Medium** (meaningful improvement, not urgent), **Low** (polish/simplification).

### 3.1 Trust & authorization integrity

---

**V2-T1 — Review-state transitions have no authorization: the entire promotion pipeline is bypassable with one POST**
*Severity:* **Critical** · *Category:* trust / architecture
*Rationale:* `POST /items/{id}/review` (`engram/api/routes/review.py:279`) lets **any principal in the tenant** set any `review_status`, including `proposed → active`, with no authority check, no scope check, and no eligibility predicate (`_require_item` → `_fetch_item` is deliberately unscoped). Auto-promotion Path A implements six carefully-audited gates (confidence, age, conflict, external dispute, top-k recheck, tenant enable) — and a sync-turn agent can skip all of them by calling the review endpoint on its own item. The same endpoint allows silently *un*-disputing an item another principal disputed. Every trust guarantee downstream of `review_status` (startup recall eligibility, semantic trust factor, CCA ledger) inherits this hole.
*Recommended solution:* Introduce a transition policy on review changes: (a) apply the shared read-eligibility predicate; (b) require `principal.type in (user, admin)` — or an explicit `review` scope — for `proposed→active`, `disputed→active`, and `rejected` transitions; (c) allow any principal to *dispute* (that is collaboration) but never to activate; (d) record the authenticated principal as actor (see V2-T3). Keep agent self-service on `proposed→archived` (withdrawing your own proposal is safe).
*Difficulty:* Low-Medium (one policy function + tests; no schema change).
*Expected impact:* Restores the integrity of the whole review/promotion investment. Without this, the trust model is decorative for any adversarial or buggy agent.

---

**V2-T2 — `human_verified` can be set by any principal, with a caller-supplied `verified_by`**
*Severity:* **Critical** · *Category:* trust
*Rationale:* `POST /items/{id}/verify` (`review.py:312`) sets `human_verified=1` and accepts `verified_by` from the request body, defaulting to the item's own author. An agent can verify its own memory and attribute it to the user's principal. `human_verified` carries a 0.10 weight in startup scoring and the semantic trust blend, and represents the strongest claim in the trust model ("a human confirmed this"). It is currently the *least* protected field in the system.
*Recommended solution:* `verified_by` must be the authenticated principal (drop it from the request body or treat mismatch as 403); require `principal.type in (user, admin)`; agents wanting to endorse an item should use `/v1/feedback` (that's what it's for). Consider renaming the write-path event so agent "confirmations" and human verification are never conflated.
*Difficulty:* Low.
*Expected impact:* Makes the strongest trust signal in the system actually mean what the docs say it means.

---

**V2-T3 — Audit actor identity is caller-supplied throughout the mutation surface**
*Severity:* **High** · *Category:* trust / auditability
*Rationale:* `PATCH /items/{id}`, `supersede`, `invalidate`, and `review` all accept `actor_principal_id` in the body and default it to the *item author's* id (`memory.py:1222`, `review.py:287`), not the authenticated caller. The `item_events` audit trail — the backbone of provenance, the external-dispute gate, and any future forensics — records whatever the caller claims. `has_external_dispute_event` distinguishes "another principal disputed this" by `actor_principal_id`; a malicious or buggy client can therefore fabricate or launder external disputes.
*Recommended solution:* The authenticated `Principal` (already resolved by `get_current_principal` and available in RLS context) becomes the sole source of `actor_principal_id` on every event write. Keep an optional `on_behalf_of` field for legitimate delegation, recorded *separately* and only honored for admin-scoped keys.
*Difficulty:* Low (the value is already in session context; ~6 call sites).
*Expected impact:* Audit trail becomes trustworthy; dispute/quorum gates become unspoofable. Prerequisite for Path B.

---

**V2-T4 — Scopes exist but are enforced only on `/v1/admin/*`**
*Severity:* **High** · *Category:* trust / API
*Rationale:* API keys carry `read/write/admin/export` scopes and `require_scopes()` exists (`auth.py:447`), but the only routes that use it are the four admin endpoints. A `read`-scoped key can `POST /v1/remember`, mutate metadata, supersede items, resolve conflicts, and pull the full `GET /v1/export/cca` dump. Scoped credentials are the natural way to run low-trust agents (exactly Engram's pitch), and today the scopes are a UI fiction.
*Recommended solution:* Blanket route-level dependencies: `read` for recall/search/items GET/export? no — `export` for export, `write` for remember/feedback/KG/diary writes, `review` (new scope) or `admin` for review/verify/resolve/bulk-archive, per V2-T1. One table in the docs mapping route → scope; a test that walks the OpenAPI schema and asserts every route declares a scope.
*Difficulty:* Low.
*Expected impact:* Least-privilege agent deployments become real; closes the gap between documented and actual authz.

---

**V2-T5 — Mutation endpoints skip the visibility eligibility predicate entirely**
*Severity:* **High** · *Category:* trust / architecture
*Rationale:* The last audit cycle locked down every *read* path via `eligibility_expression`, but `_fetch_item` (used by PATCH/supersede/invalidate/review/verify/feedback) is deliberately unscoped beyond RLS tenant isolation (`memory.py:1015-1029`). Any principal can mutate the metadata of, supersede, invalidate, dispute, verify, or leave feedback on another principal's **private** memory (including diary entries, whose read path is so carefully gated). Reads got the shared predicate; writes did not — a classic asymmetry that re-opens the exact leak class F1 closed, on the more dangerous side.
*Recommended solution:* Route all mutation lookups through `_fetch_readable_item` (404 on ineligibility, as reads do), then layer the V2-T1 transition policy on top. The docstring on `_fetch_item` ("internal callers already have a reference") describes an intent the routes don't honor — every one of these is caller-facing.
*Difficulty:* Low (predicate exists; swap the helper + tests per endpoint).
*Expected impact:* Closes the last visibility bypass; makes private truly private.

---

**V2-T6 — Authority hierarchy is a float comparison on tenant-configurable values, not an ordinal**
*Severity:* **Medium** · *Category:* trust / architecture
*Rationale:* Design §4 defines an ordinal hierarchy (`explicit_user > trusted_import > trusted_agent > untrusted_agent > inferred`). The implementation is `new_trust >= old_trust` on `source_trust` floats (`conflicts.py:180`), whose per-source defaults are tenant-editable (`tenant_config.trust_*`). A tenant that sets `trust_sync_turn=0.9` (perhaps to boost recall ranking, not understanding the coupling) silently grants chatty agent turns supersession authority over manual user writes. Ranking weight and governance authority are two different concepts sharing one number.
*Recommended solution:* Store `authority` as an explicit small-int/enum column derived from (source_type, principal_type) at write time; `authority_allows_supersession` compares ordinals; `source_trust` remains a free scoring signal. Backfill via the same mapping. This also makes the authority hierarchy visible in item detail responses — today it's implicit.
*Difficulty:* Medium (migration + backfill + touch supersession/conflict/promotion call sites).
*Expected impact:* The central governance promise ("a lower-authority source can never silently replace a higher-authority memory") becomes structurally true instead of configuration-dependent.

---

**V2-T7 — Feedback importance updates are read-modify-write races, and feedback is unbounded**
*Severity:* **Medium** · *Category:* trust / correctness
*Rationale:* `/v1/feedback` computes `importance = item["importance"] ± delta` from a value read earlier in the request (`memory.py:912`), so concurrent feedback loses updates. Separately, nothing prevents one principal from filing unlimited `useful` events on its own item pair-programmed with a second agent — importance ratchets to 0.95 and, under Path B, two colluding principals would auto-promote anything. Feedback events are also never deduplicated per (principal, item).
*Recommended solution:* Atomic SQL increment with clamps (`importance = LEAST(GREATEST(importance + :d, 0.1), 0.95)`); a partial unique index or upsert on `(item_id, principal_id, verdict)` so each principal's verdict counts once (changing your verdict replaces it); rate cap per principal/day as a safety valve.
*Difficulty:* Low.
*Expected impact:* Feedback becomes a signal that Path B and scoring can safely consume.

### 3.2 Architecture & simplification

---

**V2-A1 — Five relationship primitives, one graph: consolidate on `memory_edges`**
*Severity:* **High** · *Category:* architecture
*Rationale:* The system currently expresses "these memories are related" five ways: `kg_triples` (free-text SPO with temporal validity), `memory_edges` (typed item↔item, retrieval-only), `tunnels` (wing/room ↔ wing/room), `conflicts_with_item_id` + `conflict_type` (single-slot conflict on the item row), and `superseded_by` (single-slot lineage). Each has different visibility semantics, different APIs (or none), and different recall integration. Concrete costs: an item can only record **one** conflict ever (the second contradiction overwrites or is dropped — the deferred "multi-way conflict table" is this problem); supersession lineage and conflict pairs are invisible to graph expansion because they aren't edges; KG triples backed by items duplicate what an edge + subject fields express; tunnels are just untyped edges between taxonomy nodes.
*Recommended solution:* Make `memory_edges` the single relationship substrate. (a) Write conflicts as `contradicts`/`duplicates` edges (keep the item columns as a denormalized "primary unresolved conflict" cache if needed); (b) write supersession as a `supersedes` edge in the same transaction that sets `superseded_by`; (c) back KG triples with `derived_from` edges to their source items (the triple keeps its SPO payload — this is about linkage, not replacing triples); (d) fold tunnels into either edges between "location" anchor rows or keep them but reimplement tunnel expansion over the same traversal code. Result: one traversal engine, one visibility rule, multi-conflict support for free, and graph expansion that sees lineage and disputes.
*Difficulty:* Medium-High (staged: edges-alongside first, cutover later).
*Expected impact:* Simplifies the mental model and the codebase, unlocks multi-conflict tracking, and makes relationship-aware recall dramatically richer with data the system already generates.

---

**V2-A2 — `memory_edges` has no write path: relationship-aware recall runs on an empty graph**
*Severity:* **High** · *Category:* architecture / retrieval
*Rationale:* The design doc says edges are "written directly by whatever process establishes the relationship" — and no such process exists. There is no REST/SDK/MCP endpoint, no worker that emits edges, and no importer that writes them. The entire ENG-AUD-012 expansion pipeline (config, scoring weights, `semantic-v3`) is dead weight in every real deployment. This is the sharpest example of a feature verified against fixtures but not against the product loop.
*Recommended solution:* Three writers, in order of leverage: (1) **system-generated edges** — supersession (`supersedes`), conflict detection (`contradicts`/`duplicates`), and classification refinement can all emit edges today from signals they already compute; (2) an **edge CRUD API + MCP tool** (`engram_link`) so agents can record `derived_from`/`references` when they synthesize or act on memories; (3) the **consolidation engine** (V2-K1) which produces `derived_from` fan-in edges as its core output. Add edge counts to `/v1/taxonomy` or item detail so operators can see the graph exists.
*Difficulty:* Low for (1) and (2); (3) is its own project.
*Expected impact:* Turns the already-built expansion pipeline from a benchmark artifact into the differentiating retrieval feature it was designed to be.

---

**V2-A3 — Four scoring formulas, zero shared framework, all magic numbers**
*Severity:* **Medium** · *Category:* architecture / retrieval
*Rationale:* Startup scoring (5 weights + penalty + floor), the semantic trust blend (5 weights + 2 multiplicative penalties + clamp), the relationship blend (4 weights), and the SQL coarse approximation live in three modules with independently chosen constants (0.30/0.25/0.20…, 0.30/0.30/0.25…, 0.70/0.15/0.10/0.05, 0.75, 0.85, 0.9/0.6/0.3). None was validated against outcomes; several encode the same intent (trust should modulate relevance) with different shapes. Every future retrieval change must reason about four surfaces.
*Recommended solution:* Not premature unification of the math — unify the *plumbing*: a single `scoring` module that owns signal extraction (trust, recency, importance, verification, review factor) and exposes named, versioned profiles; tenant_config points at a profile + overrides rather than raw column-per-weight. Prerequisite and payoff are both the eval harness (V2-R1): once you can measure, you can actually tune, and versioned profiles enable A/B via the `scoring_version` field that already flows to `recall_logs`.
*Difficulty:* Medium.
*Expected impact:* Retrieval iteration velocity; safe tuning; per-tenant scoring experiments.

---

**V2-A4 — Semantic recall still writes telemetry inline in the read transaction**
*Severity:* **Medium** · *Category:* architecture / scaling
*Rationale:* ENG-AUD-011 moved startup recall's counter updates into an idempotent async job specifically because write-on-read is a scalability and replica-safety cliff. Semantic recall kept the inline `UPDATE memory_items SET recall_count=…` (`recall.py:1127-1136`) and takes no advantage of the read engine. As semantic recall becomes the dominant mode (it will — it's the query-driven one), the same cliff returns.
*Recommended solution:* Reuse the existing `recall.telemetry` job and the `telemetry_applied_at` claim (payload already carries `mode`); route candidate selection through `read_session_factory` like startup does.
*Difficulty:* Low (the machinery exists; this is symmetry work).
*Expected impact:* One recall telemetry path; semantic recall becomes replica-servable.

---

**V2-A5 — The write path is a 240-line route handler; extract a write engine symmetric to the recall engine**
*Severity:* **Medium** · *Category:* architecture / maintainability
*Rationale:* `remember()` orchestrates secret-guard → canonicalize → RLS resolve → classify → kind-governance → trust defaults → confidence blend → visibility narrowing → singleton supersession → insert → dedup recovery → classification event → placeholder embeddings → job enqueue, inline in `routes/memory.py`. Recall got `engram/recall.py`; the write path — the more invariant-dense side — never got its module. Consequences: the CLI/importers/worker can't reuse the exact write semantics (importers have their own scripts), tests go through HTTP, and the two `inspect.signature(generate_embedding)` compatibility shims (`recall.py:960`, `memory.py:799`) hide a provider-interface drift that a module boundary would have forced clean.
*Recommended solution:* `engram/write.py` (`execute_remember(...)`) owning steps 2–9 with a typed result; the route becomes request parsing + response shaping, mirroring recall. Fix the `generate_embedding` signature properly while extracting (one canonical `(text, profile)` signature).
*Difficulty:* Medium (mechanical, high test coverage exists).
*Expected impact:* Reusable write semantics for importers/consolidation/extraction; smaller blast radius for the many findings above that touch this path.

---

**V2-A6 — `visibility='workspace'` with `workspace_id IS NULL` silently means tenant-wide**
*Severity:* **Medium** · *Category:* architecture / trust
*Rationale:* The default write (`visibility="workspace"`, no workspace) produces an item readable by the whole tenant (`memory_access.py:52-58` documents this deliberately). So the *default* memory write has `tenant` semantics labeled `workspace` — the label and the behavior disagree, and an operator auditing visibility distribution by column value will draw wrong conclusions. This is exactly the kind of subtlety that erodes trust in a trust product.
*Recommended solution:* At write time, resolve the discrepancy instead of preserving it: if `visibility='workspace'` and no workspace is given, either store `visibility='tenant'` (honest label) or resolve to the principal's default workspace. Keep the read-predicate fallback for legacy rows; backfill-migrate labels when convenient.
*Difficulty:* Low.
*Expected impact:* Visibility column becomes truthful; simpler mental model; safer future policy changes.

---

**V2-A7 — Migration numbering collision and lexicographic fragility**
*Severity:* **Low** · *Category:* architecture / ops
*Rationale:* `005_classification_seed_rules.sql` and `005_jobs.sql` share a prefix; ordering currently works only because "c" < "j". The runner sorts by filename and has no checksum verification, so an edited historical migration reapplies nowhere and diverges silently across environments.
*Recommended solution:* Renumber (or adopt zero-padded timestamps going forward); record a content hash in `schema_migrations` and warn on drift; add a CI check for duplicate prefixes.
*Difficulty:* Low.
*Expected impact:* Prevents a classic multi-environment schema-drift incident before there are many environments.

### 3.3 Retrieval quality

---

**V2-R1 — No retrieval evaluation harness: every ranking decision is unvalidated**
*Severity:* **High** (the highest-leverage retrieval item) · *Category:* retrieval / testing / strategy
*Rationale:* The system has four scoring formulas, trust penalties (0.75, 0.85), expansion weights, RRF fusion, and freshness half-weighting — none of which has ever been measured against a labeled outcome. There is no golden dataset, no nDCG/recall@k computation, no regression gate on ranking changes, and no way to answer "did semantic-v3 make recall better or worse?" beyond anecdote. The infrastructure for it half-exists: `recall_logs` stores query, item_ids, scoring_version, config_version; `feedback_events` links verdicts to recall logs. Competitors (Mem0, Zep) publish LOCOMO/LongMemEval numbers; Engram cannot currently produce one.
*Recommended solution:* Three layers. (1) **Offline eval harness**: a fixtures corpus + labeled queries (start with 200–500 judgments; include trust-sensitive cases like "low-trust near-duplicate must not outrank verified memory" — the scenarios the README already narrates); run per scoring version in CI, gate on regression. (2) **Public benchmarks**: adapt LongMemEval/LOCOMO ingestion so Engram has published numbers with and without trust weighting — the trust-aware delta *is* the marketing. (3) **Online signal**: a nightly job computing per-tenant feedback-derived precision (useful/(useful+noise) per recall log) exposed via stats.
*Difficulty:* Medium (harness) + Medium (benchmarks).
*Expected impact:* Converts retrieval from faith to engineering; unblocks V2-A3 tuning; produces the numbers the product strategy needs.

---

**V2-R2 — Temporal retrieval: the schema is bi-temporal-ready and no query exposes it**
*Severity:* **High** · *Category:* retrieval / AI-native / differentiation
*Rationale:* Every item has `valid_from`/`valid_to`/`created_at`, supersession chains link versions, and `item_events` timestamps every state change — a complete belief history. Yet no API can answer: "what did we believe about deployment policy on June 1?", "what changed in this workspace this week?", "show me the history of this fact." Zep and Graphiti market bi-temporal knowledge graphs as their core differentiator; Engram has strictly richer temporal data (plus audit, plus trust) and exposes none of it. Agents resuming work after a gap ("what changed since I last ran?") is one of the most common real multi-agent needs.
*Recommended solution:* (a) `as_of` parameter on search/recall (predicate becomes `valid_from <= :as_of AND (valid_to IS NULL OR valid_to > :as_of)` — items whose supersessors postdate `as_of` resolve to the then-active version); (b) `GET /v1/items/{id}/history` walking the supersession chain + events into a version timeline; (c) `GET /v1/changes?since=cursor` (see V2-M1 — same substrate). Recency-windowed search filters (`created_after/before`) fall out for free.
*Difficulty:* Medium (query work; no schema change).
*Expected impact:* A headline differentiator competitors would have to rebuild their storage to match; directly useful to every agent that sleeps between sessions.

---

**V2-R3 — Contextual startup recall: the working set cannot be conditioned on the task**
*Severity:* **High** · *Category:* retrieval
*Rationale:* Startup recall returns one global-ranked set per (tenant, principal, workspace). A coding agent starting a deploy task and the same agent starting a refactor get identical working sets. There are no kind/wing/room filters on recall (search has them; recall doesn't), no way to weight a declared task context, and no per-kind budget shaping (e.g., "always include doctrine, cap observations at 20%"). This is the difference between "memory dump" and "briefing."
*Recommended solution:* (a) Add `kind`/`wing`/`room` filters (trivial — the predicate helpers exist). (b) Add optional `context` text to startup mode: embed it and blend a similarity term into the startup score (weights via a scoring profile per V2-A3) — this creates a genuine "hybrid startup" mode that stays deterministic given the same context. (c) Structured budget allocation by kind class (pinned → doctrine/invariant → decisions/preferences → rest), replacing the flat sort with sectioned packing, which also improves the working-set document (V2-N3).
*Difficulty:* Low (a), Medium (b, c).
*Expected impact:* The single biggest perceived-quality jump for agent users; makes byte budgets spend on relevance instead of global importance.

---

**V2-R4 — No retrieval diversity: top-k rewards redundancy, and cross-principal duplicates make it worse**
*Severity:* **Medium** · *Category:* retrieval / knowledge organization
*Rationale:* Dedup is exact-content-hash per (tenant, workspace, principal) — three agents observing the same fact create three active rows, all embedding-near-identical, all individually trust-scored. Semantic recall then happily spends 3 budget slots on one fact. There is no MMR/diversity term anywhere, and budget packing is pure score order.
*Recommended solution:* Two complementary moves: (a) cheap MMR-style penalty during budget packing (penalize candidates whose embedding similarity to an already-selected item exceeds ~0.95; vectors are already fetched); (b) canonicalization from the consolidation engine (V2-K1) so duplicates get merged at rest rather than filtered per query. Also record cross-principal duplicates as `duplicates` edges when conflict detection sees verdict=DUPLICATE across principals (today it dedups only within the write scope).
*Difficulty:* Low (a), bundled (b).
*Expected impact:* Budget efficiency (more distinct facts per token) — directly measurable once V2-R1 exists.

---

**V2-R5 — `contradicts` edges and unresolved conflicts are ranking noise instead of surfaced tension**
*Severity:* **Medium** · *Category:* retrieval / AI-native
*Rationale:* In relationship expansion, a `contradicts` edge confers the same +0.6-weight bonus as `supports`; in the trust blend, an unresolved conflict multiplies trust by 0.75. So a contradiction makes both parties slightly *less* likely to be recalled, and when one side is recalled the agent is never told the other side exists (the warning says "unresolved conflicts" with no pointer). For an AI consumer, the pair *is* the information: "we have two conflicting beliefs about X, here they are, here's their provenance" is exactly what a reasoning agent needs.
*Recommended solution:* When a recalled item has an unresolved conflict or an incoming `contradicts` edge, attach the counterpart to the response (id, content snippet, trust summary, since-when) — as a structured `tensions` field rather than a mystery warning; ensure contradiction pairs are co-packed under budget when either is selected. Expansion scoring should treat `contradicts` as a *pairing* rule, not a bonus weight.
*Difficulty:* Medium.
*Expected impact:* Turns conflict machinery from an internal gate into a visible reasoning aid — an AI-native feature no flat memory store can copy.

---

**V2-R6 — No query planning or adaptive budgets; hybrid is the caller's problem**
*Severity:* **Medium** · *Category:* retrieval
*Rationale:* The caller picks `keyword|semantic|hybrid` and fixed budgets. Short entity-like queries want keyword-weighted retrieval; conceptual queries want semantic; "what do we know about X since May" wants filters + temporal. Agents shouldn't need to know this. Similarly, budgets are static while the useful answer size varies wildly (a preferences lookup needs 3 items; "brief me on this project" needs 50).
*Recommended solution:* A `mode=auto` that routes by cheap query features (length, quoted phrases, presence of subject-registry matches, dates) — deterministic rules first, no LLM required; an `answer_budget: small|medium|large` abstraction over byte/token budgets; stop-early packing when score drops off a cliff (score-gap cutoff) so weak tails don't fill budgets just because they exist.
*Difficulty:* Medium.
*Expected impact:* Better default behavior for the 90% of callers who won't tune; measurable via V2-R1.

---

**V2-R7 — Token budgets are bytes÷4 everywhere**
*Severity:* **Low** · *Category:* retrieval / DX
*Rationale:* The budget contract is the agent-facing product surface, and `max(1, bytes // 4)` misestimates code, CJK, and URLs badly (2–3× error). Agents doing careful context management will overflow or under-fill their windows.
*Recommended solution:* Pluggable token counter (tiktoken-compatible when available, bytes/4 fallback), reported in the response (`token_count`, `token_estimator`) so callers know which contract they got.
*Difficulty:* Low.
*Expected impact:* Honest budgets; matters more as budgets shrink (edge/small-model callers).

### 3.4 Knowledge organization & lifecycle

---

**V2-K1 — No consolidation: memory accumulates but never improves**
*Severity:* **High** (the flagship opportunity) · *Category:* knowledge organization / AI-native
*Rationale:* Engram can store, trust-rank, and relate memories — but 500 observations about a repo stay 500 observations forever. There is no machinery to cluster related low-level memories, synthesize a higher-confidence summary, and retire the constituents. The `summary` kind exists unused; `derived_from` edges exist unwritten; supersession exists but only 1:1. Every long-running deployment will watch recall quality decay as budget slots fill with fragmentary, redundant, aging observations. This is the difference between a memory *store* and a memory *system* — and it is precisely where Engram's substrate (provenance edges, review states, authority caps, audit events) turns a risky LLM operation into a governed one: consolidations enter as `proposed`, cite their sources, inherit floor-of-sources confidence, and are reviewable/reversible like everything else.
*Recommended solution:* A `consolidation.sweep` job per (tenant, wing/room or subject): (1) cluster active low-importance/high-count memories by embedding similarity + subject; (2) LLM-synthesize a summary memory (`kind=summary` or the dominant kind) with extraction provenance; (3) write `derived_from` edges to every constituent; (4) propose (never auto-activate) the summary; (5) on activation (human or Path A/B), archive constituents via supersession-to-summary (n:1 — the edge substrate handles what the single `superseded_by` column can't). Confidence propagation per V2-N2. Start with the safest cluster type: same-subject `observation` fan-in.
*Difficulty:* High (but decomposable; depends on V2-A2 edges + V2-A5 write engine).
*Expected impact:* Memory that gets better with age — the strongest possible differentiator vs. every competitor, and the feature that makes "millions of memories" a strength instead of a liability.

---

**V2-K2 — No aging, decay, or semantic GC; retention is a logged no-op**
*Severity:* **Medium-High** · *Category:* knowledge organization / operations
*Rationale:* `handle_retention_sweep` logs "no-op (retention logic deferred)" (`worker.py:620`). Staleness produces only a warning string; confidence never decays; nothing ever archives an unrecalled, unverified, low-importance observation from eight months ago. Meanwhile `item_events`, `recall_logs`, and completed `jobs` rows accumulate forever. Append-first is a content-immutability principle, not an obligation to keep everything in the *hot* set.
*Recommended solution:* Implement the sweep in three tiers: (1) **queue hygiene** — delete/archive `succeeded` jobs older than N days, compact dead jobs to a summary; (2) **memory aging** — tenant-policy-driven archival proposals for items failing a staleness predicate (never verified, not recalled in N days, confidence < threshold, not pinned/doctrine/invariant), written as reviewable bulk-archive proposals rather than silent deletes; (3) **log retention** — partition-or-prune policy for `recall_logs`/`item_events` beyond a horizon (keep events for live items, aggregate old recall logs). Confidence decay should be *virtual* (computed at scoring time from `last_verified_at`) rather than mutating rows — cheaper and reversible.
*Difficulty:* Medium.
*Expected impact:* Bounded storage growth; recall stays clean without manual hygiene calls; the hosted cost model becomes predictable.

---

**V2-K3 — Taxonomy (wings/rooms) is ungoverned free text and will drift**
*Severity:* **Medium** · *Category:* knowledge organization
*Rationale:* `kind` got a governed registry (ENG-AUD-010) because ungoverned enums drift; `wing`/`room` have the identical problem and no registry. Classification writes whatever vocabulary it has cached; agents write whatever strings they like; `project`/`projects`/`Project` become three wings; tunnels reference (wing, room) coordinates that nothing validates, so a typo'd tunnel silently expands nothing. As memory grows, taxonomy quality *is* retrieval quality (filters, tunnel expansion, browsing all key on it).
*Recommended solution:* Mirror the kinds pattern lightly: a `taxonomy_nodes` registry (tenant, wing, room, description, enabled, merged_into) seeded from existing distinct values; writes validate-or-propose (unknown coordinates auto-register as `proposed` nodes rather than 422 — friendlier than kinds); admin merge/rename operations that bulk-update items and tunnels with audit events. The vocab cache already exists to serve it.
*Difficulty:* Medium.
*Expected impact:* Durable navigation structure; tunnel expansion that can be trusted; the substrate a future browse/console UI needs.

---

**V2-K4 — Cross-principal duplicate collapse and canonical selection are missing**
*Severity:* **Medium** · *Category:* knowledge organization
*Rationale:* Covered from the retrieval side in V2-R4; the organizational side is that nothing designates a *canonical* row when several principals hold the same fact at different trust levels. Feedback, verification, and importance then fragment across copies (the user verifies one agent's copy; the other agents' copies stay unverified).
*Recommended solution:* When conflict detection returns DUPLICATE across principals, link copies with `duplicates` edges and elect a canonical (highest authority, then verified, then earliest); recall prefers canonicals and aggregates trust signals across the duplicate set; verification/feedback on any copy propagates along `duplicates` edges.
*Difficulty:* Medium (depends on V2-A1/A2).
*Expected impact:* Trust signals concentrate instead of fragmenting; dedup becomes semantic rather than byte-exact.

### 3.5 Long-term scaling

---

**V2-S1 — RLS policies force a per-row text cast; use an indexable UUID comparison**
*Severity:* **Medium** (cheap, compounding) · *Category:* scaling
*Rationale:* Every policy reads `tenant_id::text = current_setting('app.tenant_id', true)` (`001_init.sql:472+`). Casting the column (not the setting) means the policy predicate can't use the btree indexes on `tenant_id` and evaluates a cast per candidate row on every query, on every tenant-scoped table, forever. At millions of rows this is a real constant factor on the hottest predicate in the system.
*Recommended solution:* Migrate policies to `tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid` (cast the setting once; comparison stays uuid=uuid and index-eligible). Verify with `EXPLAIN` on a representative recall query as the app role; keep a test asserting empty-context yields zero rows (NULLIF preserves that).
*Difficulty:* Low (one migration recreating policies).
*Expected impact:* Cheaper every-query baseline; removes a footgun before data volume makes it visible.

---

**V2-S2 — One global HNSW index across all tenants**
*Severity:* **Medium** (watchlist, not urgent) · *Category:* scaling
*Rationale:* All tenants' vectors share `idx_embeddings_hnsw`. `iterative_scan=strict_order` keeps filtered queries *correct*, but a small tenant's query must iterate past a large tenant's neighbors — latency for small tenants degrades as any tenant grows, and index build/maintenance is global. This is the known pgvector multi-tenancy trade-off.
*Recommended solution:* Not a rebuild now — a measured plan: (1) add per-tenant vector counts + semantic-latency histograms (V2-O1) to see the skew; (2) when a threshold is crossed, adopt list-partitioning of `memory_embeddings` by tenant bucket (hash) with per-partition HNSW — pgvector supports this well and the profile-cutover machinery (dual-write/backfill/validate) already built for re-embedding is exactly the tool for an index-topology migration too; (3) document the ceiling in deployment docs so self-hosters know when to shard.
*Difficulty:* Low now (instrumentation + plan), High later (execution).
*Expected impact:* Predictable p99 for semantic recall at thousands of tenants; reuses the embedding-migration muscle the team already built.

---

**V2-S3 — Job queue: no tenant fairness, and `priority` is sorted after `run_after`**
*Severity:* **Medium** · *Category:* scaling / architecture
*Rationale:* `claim_next_job` orders by `(run_after, priority, created_at)` (`jobs.py:186`). Since `run_after` is a timestamp with microsecond resolution, `priority` almost never breaks a tie — it is effectively dead. And claims are globally FIFO: one tenant enqueueing a 50k-item embedding backfill starves every other tenant's embedding and classification jobs for hours. Cross-tenant fairness is the entire reason claims run through the owner session; the fairness itself was never implemented.
*Recommended solution:* (a) Order by `(priority, run_after, created_at)` with time-bucketed `run_after` (or `run_after <= now` as a filter, which it already is — so just put priority first); (b) fairness via per-tenant concurrency caps (skip tenants with ≥N running jobs of a type — one indexed count) or simple weighted round-robin over tenants with due jobs; (c) give bulk backfills a low priority class so interactive writes always win.
*Difficulty:* Low-Medium.
*Expected impact:* Multi-tenant worker behavior that matches the multi-tenant storage story; no noisy-neighbor job starvation.

---

**V2-S4 — One telemetry job row per startup recall: queue write amplification**
*Severity:* **Medium** · *Category:* scaling
*Rationale:* Every startup recall inserts a job row (plus later update+claim writes) — the jobs table churns at recall QPS, which at fleet scale is the highest-frequency event in the system. The queue is the wrong shape for high-frequency, loss-tolerant counter increments; it's built for durable, retryable work.
*Recommended solution:* Batch at the worker: telemetry jobs already carry `recall_log_id`; switch the enqueue to a lightweight append (e.g., rely on `recall_logs.telemetry_applied_at IS NULL` itself as the queue — a periodic worker query `WHERE telemetry_applied_at IS NULL LIMIT N` applies counters in batches with the same idempotency claim, no jobs row at all). This deletes a table's worth of churn and keeps exactly the same guarantees.
*Difficulty:* Low-Medium.
*Expected impact:* Jobs table stays proportional to real work; recall hot path sheds an insert.

---

**V2-S5 — Hot mutable counters live on the immutable-content row**
*Severity:* **Low-Medium** · *Category:* scaling
*Rationale:* `memory_items` is append-first in spirit, but `recall_count`, `startup_recall_count`, `last_recalled_at`, and `importance` mutate in place on the same wide row as content + tsvector. Every counter bump dead-tuples a row that carries a generated tsvector and long text — write amplification and table bloat that VACUUM must continually repair, on the table every read touches.
*Recommended solution:* When V2-S4 lands, move recall counters (and possibly importance) to a slim side table (`memory_item_stats`, PK item_id) updated by the batch telemetry worker; scoring reads join it (or the coarse SQL score uses it directly). Not urgent — schedule with the first partitioning work.
*Difficulty:* Medium.
*Expected impact:* memory_items becomes truly cold/append-mostly; cheaper vacuums; cleaner mental model (identity vs. usage stats).

---

**V2-S6 — No per-tenant quotas, rate limits, or usage metering**
*Severity:* **Medium** (prerequisite for hosted) · *Category:* scaling / operations
*Rationale:* Nothing bounds a tenant's item count, embedding spend, job volume, or request rate. Phase 4 (hosted) needs metering for billing and limits for protection; self-hosted fleets need them to stop one runaway agent from flooding memory.
*Recommended solution:* Per-tenant counters (items, embeddings, jobs/day, requests/min) maintained cheaply (the stats table from V2-S5 generalizes); soft limits → 429 with structured error; per-API-key rate limits attach naturally to the key cache. Meter first, enforce later.
*Difficulty:* Medium.
*Expected impact:* Hosted-readiness; blast-radius control for agent bugs (an agent stuck in a remember-loop is a matter of time).

### 3.6 AI-native capabilities

---

**V2-N1 — Memory introspection: a "why do I believe this" endpoint**
*Severity:* **High** · *Category:* AI-native
*Rationale:* Engram already stores everything needed to answer the question no other memory system can: who wrote it, from what source, how it was classified and with what confidence, what verified/disputed/superseded it, what it was derived from, what conflicts with it. But an agent must issue 4–6 calls and join the results itself. A single composed endpoint makes provenance *operational* for agents ("before acting on this invariant, check its pedigree") and is the natural UI for human review too.
*Recommended solution:* `GET /v1/items/{id}/explain` returning a structured dossier: trust vector with plain-language derivation ("confidence 0.72 = source default 0.5 blended with classifier 0.9, capped by source authority"), lineage (supersession chain + derived_from tree, depth-bounded), standing (conflicts, disputes, feedback tallies, recall usage), and freshness (age, last verification, staleness verdict). Everything is a read over existing tables; the classification events already store the blend inputs as JSON.
*Difficulty:* Low-Medium.
*Expected impact:* The most demoable AI-native feature available for its cost; makes the trust model *visible* — which is the product.

---

**V2-N2 — Uncertainty propagation for derived artifacts**
*Severity:* **Medium** · *Category:* AI-native / trust
*Rationale:* KG triples, summaries, and (future) consolidations are derived from source memories, but their `confidence` is set independently and never revisited: a triple extracted from a memory that is later disputed or superseded keeps its confidence forever. There is no rule for combining source confidences into derived confidence, so synthesis (V2-K1) has no principled trust story without this.
*Recommended solution:* Define and enforce one propagation rule: derived confidence ≤ min(source confidences) at creation (conservative floor); a worker reacting to source review-status changes (dispute/supersede/reject) re-flags derived artifacts (`stale` embedding-status pattern reused: mark `derivation_stale`, surface as a warning, queue re-synthesis). Requires `derived_from` edges (V2-A2) as the dependency graph.
*Difficulty:* Medium.
*Expected impact:* Derived knowledge can be trusted transitively — mandatory before consolidation ships, valuable for KG triples today.

---

**V2-N3 — The working set is a flat text blob; make it a structured, citable briefing**
*Severity:* **Medium** · *Category:* AI-native / DX
*Rationale:* `working_set` is `"[kind] content"` lines joined by newlines (`recall.py:750`). No sections, no stable citation handles, no inline warnings, no separation of doctrine from observations. Agents that want to cite a memory in their reasoning (enabling precise feedback and usage attribution) have nothing to cite; prompts that want invariants pinned at the top must re-sort client-side.
*Recommended solution:* A `working_set_format: text|structured` option where structured returns sectioned markdown (Constraints & doctrine → Decisions → Preferences → Recent context) with per-item short handles (`[m:9f3c]`), warnings inline, and a footer of omitted-count/tensions. Pair with a `used_memories` field on `/v1/feedback` so agents can report which handles they actually relied on (feeds V2-M3 Path B and scoring).
*Difficulty:* Low-Medium.
*Expected impact:* Better prompts out of the box; closes the loop between recall and feedback — the data flywheel every scoring improvement needs.

---

**V2-N4 — Agent self-maintenance as reviewable proposals**
*Severity:* **Medium** · *Category:* AI-native
*Rationale:* Agents are the ones who notice memory problems ("these two overlap", "this is obsolete", "this should be workspace-visible") but their only tools are blunt direct mutations (which V2-T1..T5 will rightly restrict) or nothing. The trust model already has the perfect primitive for untrusted suggestions: `proposed` state + review.
*Recommended solution:* A `POST /v1/proposals` surface (or generalize review): propose-merge(ids, rationale), propose-archive(id, rationale), propose-reclassify(id, kind/wing/room), propose-visibility-change. Proposals are queryable (review queue already exists), approvable by authorized principals, and executable server-side with full audit. Quorum approval (2+ non-author agents) can auto-apply low-risk types — reusing the Path B machinery.
*Difficulty:* Medium.
*Expected impact:* Memory hygiene work distributes to the fleet safely; the trust pipeline becomes a general governance engine, not just a write gate.

### 3.7 Multi-agent collaboration

---

**V2-M1 — No change awareness: agents can't know their beliefs were invalidated**
*Severity:* **High** · *Category:* multi-agent
*Rationale:* Agent B loads a working set at 09:00; at 09:30 agent A supersedes a decision B is acting on, or the user disputes an invariant B cached. B discovers this only if/when it recalls again — and nothing tells it *that* it should. For long-running agents this is the top real-world failure mode of shared memory ("acting on stale institutional knowledge"), and Engram has the raw material (`item_events` is a total-ordered change log per item) with no consumption surface.
*Recommended solution:* Cursor-based change feed: `GET /v1/changes?since=<cursor>&workspace=…` streaming eligibility-filtered events (supersede, dispute, resolve, verify, archive, new-active) with item snapshots — a straightforward indexed read over `item_events` joined to eligibility. Then a convenience on recall: pass `known_since` (or the previous `recall_log_id`) and get a `changed_items` delta alongside the working set. Push/webhooks can come later; polling with cursors covers agent loops fine.
*Difficulty:* Medium.
*Expected impact:* The multi-agent story becomes real: shared memory with cache-invalidation semantics. No competitor offers this with trust context attached.

---

**V2-M2 — Path B (usage-quorum promotion) remains unbuilt while all its inputs exist**
*Severity:* **Medium** · *Category:* multi-agent / trust
*Rationale:* Feedback events with principal identity, external-dispute detection, per-reason promotion counters, and the shared promotion service all exist; Path B is a WHERE clause and a gate away ("2+ distinct non-author principals marked useful, no disputes"). It is also the promotion path that showcases multi-agent value (fleet behavior surfacing useful knowledge without human review) — the differentiator the design doc names. Blocked only by feedback integrity (V2-T7) and actor integrity (V2-T3).
*Recommended solution:* Implement in `promotion.py` alongside Path A with its own counters (`promoted_path_b`, `skipped_quorum`), quorum size from tenant_config, same conflict recheck. Sequence after V2-T3/T7.
*Difficulty:* Low (after prerequisites).
*Expected impact:* Closes a documented promise; activates the feedback flywheel.

---

**V2-M3 — No expertise model: all agents are interchangeable authorities**
*Severity:* **Medium** · *Category:* multi-agent
*Rationale:* Atlas's observations about infrastructure and Hermes's observations about infrastructure carry identical weight, even if Atlas has written 500 verified ops memories and Hermes none. Principals have only `type`; there is no per-domain track record, so trust is source-*type*-aware but not source-*identity*-aware — a big untapped signal in a system that already logs verification and feedback outcomes per principal.
*Recommended solution:* Derived (never manually set) per-principal reliability stats: by wing/subject, track authored-memory outcomes (verified rate, dispute rate, feedback ratio). Expose in `/v1/admin/principals` and (later) blend a small reliability factor into trust scoring via a scoring profile (V2-A3). This also enables **expertise routing**: "which principal should review this proposal?" — pick the highest-reliability principal in the wing.
*Difficulty:* Medium.
*Expected impact:* Trust becomes earned per-domain, not declared per-type — a uniquely defensible multi-agent capability given the audit substrate.

### 3.8 Developer experience

---

**V2-D1 — Untyped responses on a third of the API undermine OpenAPI, SDK, and docs**
*Severity:* **Medium** · *Category:* DX
*Rationale:* `response_model=None` with ad-hoc dicts on items list/detail, PATCH, supersede, invalidate, review, verify, classification rules, export. Consequences: the OpenAPI schema (the "understand Engram in an afternoon" artifact) has holes exactly where the trust model is most visible (item detail with events!); the SDK returns raw dicts for these; drift between server and client is silent.
*Recommended solution:* Define `MemoryItemOut`, `ItemEventOut`, `ItemDetailResponse`, etc. (one module, reused by SDK models); FastAPI then validates and documents them. Do it when touching these routes for V2-T1/T5 anyway.
*Difficulty:* Low-Medium.
*Expected impact:* Complete OpenAPI → typed SDKs (incl. generated TS) → docs that match reality mechanically.

---

**V2-D2 — Python-only SDK; no TypeScript**
*Severity:* **Medium** · *Category:* DX / strategy
*Rationale:* The agent-framework ecosystem is at least half TypeScript (LangGraph.js, Vercel AI SDK, Mastra, custom Node agents). MCP covers interactive agents but not programmatic integration. Every competitor ships TS.
*Recommended solution:* After V2-D1, generate a TS client from OpenAPI and hand-polish the ergonomic layer (auth, retries, typed errors) — keep it as thin as the Python SDK per the design principle.
*Difficulty:* Low-Medium (after V2-D1).
*Expected impact:* Removes a hard adoption filter.

---

**V2-D3 — No batch write API**
*Severity:* **Medium** · *Category:* DX / scaling
*Rationale:* `remember` is one item per HTTP call. Extraction hooks, importers, and pre-compression capture naturally produce 5–50 memories at once; today that's N round-trips, N transactions, N classification passes. The importers bypass the API entirely (scripts) partly for this reason — which means imports skip API-level guarantees.
*Recommended solution:* `POST /v1/remember/batch` (array in, per-item results out with independent success/dedup/error status), sharing the extracted write engine (V2-A5), one transaction per small chunk, jobs enqueued in bulk.
*Difficulty:* Low-Medium (after V2-A5).
*Expected impact:* Chatty capture becomes cheap; importers can move onto the API.

---

**V2-D4 — Tenant policy is 25 fixed columns; every knob is a migration**
*Severity:* **Medium** · *Category:* DX / architecture
*Rationale:* `tenant_config` hard-codes each weight/threshold as a column. Adding per-tenant recall budgets (a noted gap in `_resolve_recall_budgets`), scoring-profile selection (V2-A3), retention policy (V2-K2), or quorum size (V2-M2) each require DDL. The versioning story (`config_version`) is good; the shape fights it.
*Recommended solution:* Evolve to `policy JSONB` validated by a Pydantic schema (defaults + overrides), keeping `config_version` semantics; migrate columns into the document; new knobs become schema fields. Config reads already flow through one loader per module, so the blast radius is contained.
*Difficulty:* Medium.
*Expected impact:* Tenant policy can evolve at product speed; per-tenant budgets/profiles/retention unblock several findings above.

---

**V2-D5 — Onboarding: no runnable example corpus or guided walkthrough**
*Severity:* **Low-Medium** · *Category:* DX
*Rationale:* The deployment docs are strong, but a developer's afternoon needs a *populated* instance: nothing ships a demo dataset, a seeded multi-agent scenario, or a script that demonstrates propose→dispute→resolve→promote→recall-with-reasons — the product's actual pitch. The memory-palace vocabulary (wing/room/tunnel/drawer/engram) also fronts the learning curve; the glossary exists but API docs should lead with plain terms.
*Recommended solution:* `engram seed-demo` (or `examples/`): a scripted scenario with two agents + one user exercising the full trust loop, ending with an `explain` (V2-N1) output; a 10-minute tutorial doc walking it. Cheap, high conversion value.
*Difficulty:* Low.
*Expected impact:* "Afternoon to understand" becomes literally true; doubles as an eval/demo fixture.

### 3.9 Operations & observability

---

**V2-O1 — No metrics or tracing**
*Severity:* **High** · *Category:* operations
*Rationale:* There is no `/metrics`, no OpenTelemetry, and therefore no way to see recall latency percentiles, candidate-pool efficiency, queue depth/age, dead-job counts, embedding coverage (% items with ready vectors per profile), cache hit rates (API-key, vocab), or per-tenant usage — the exact quantities every other finding says "measure first" about. The structured log lines exist but logs are not dashboards.
*Recommended solution:* Prometheus endpoint (or OTel metrics) with a first shelf of ~15 series: request latency/status by route; recall candidate stats (already computed as `CandidateStats` — just export them); jobs by status/type + oldest-pending age; embedding coverage; worker last-claim timestamp (liveness); token/byte budget utilization. Trace spans on the recall pipeline stages (candidates → scoring → expansion → packing) and worker handlers. Ship a starter Grafana dashboard JSON in `deploy/`.
*Difficulty:* Medium.
*Expected impact:* Prerequisite for scaling decisions (V2-S2), queue health (V2-S3), retrieval telemetry (V2-R1 online layer), and hosted SLOs.

---

**V2-O2 — The job queue has no operational surface**
*Severity:* **Medium** · *Category:* operations
*Rationale:* Dead jobs are silent (a dead embedding job = a memory invisibly missing from semantic recall until someone runs SQL); there's no list/retry/cancel API; `engram worker` health is unobservable. The admin API manages tenants/keys/kinds but not the async machinery that the write path now depends on.
*Recommended solution:* `GET /v1/admin/jobs?status=dead|pending&type=…`, `POST /v1/admin/jobs/{id}/retry`, counts endpoint; a `/ready`-adjacent worker heartbeat check (alert when oldest pending job age exceeds a threshold); dead-letter count in metrics (V2-O1).
*Difficulty:* Low.
*Expected impact:* Async failures become operable instead of archaeological.

---

**V2-O3 — Backup story stops at nightly pg_dump; no PITR, no per-tenant restore/export**
*Severity:* **Medium** · *Category:* operations
*Rationale:* Nightly dumps + restore smoke test is a good MVP posture; it means up to 24h data loss and all-or-nothing restore. Hosted (and serious self-hosted fleets) need point-in-time recovery, and multi-tenant operations need *tenant-granular* export/restore ("tenant X deleted their memories by mistake" cannot mean restoring every tenant to yesterday). The CCA export exists but isn't a full-fidelity tenant snapshot (no events, embeddings, edges, config).
*Recommended solution:* (1) Document WAL archiving/PITR (wal-g/pgBackRest) in `docs/deployment.md` as the recommended production posture; (2) `engram export-tenant` / `import-tenant`: full-fidelity, versioned JSONL per table, eligibility-safe, idempotent import — which also becomes the hosted-migration and GDPR-portability tool.
*Difficulty:* Low (docs) + Medium (tenant snapshot).
*Expected impact:* Real disaster stories covered; unlocks hosted operations and data portability as a feature.

---

**V2-O4 — Deletion (GDPR/hard-delete) remains deferred with growing exposure**
*Severity:* **Medium** · *Category:* operations / trust
*Rationale:* `deletion_events` tombstones are designed and the table exists; the endpoint doesn't. As dogfood data grows and hosted plans approach, "we cannot actually delete a memory" becomes a legal and product liability — and secrets *do* slip past denylists eventually.
*Recommended solution:* Implement the already-designed slice: hard delete with tombstone (id + content_hash + actor + reason), cascade to embeddings/edges/events (or event redaction), KG detach, admin-scoped + per-item; per-tenant purge rides on V2-O3's machinery.
*Difficulty:* Medium.
*Expected impact:* Compliance-readiness; the incident-response tool you want *before* the incident.

### 3.10 Testing

---

**V2-Q1 — No property-based tests for the invariant-dense cores**
*Severity:* **Medium** · *Category:* testing
*Rationale:* The suite is broad and example-based, but the components with algebraic invariants are exactly where examples miss: budget packing (never exceed budgets; monotone under budget increase; skip-not-break), scoring (bounded output; monotone in each signal; penalty floor), supersession chains (no cycles; exactly one active per family), eligibility (no principal ever widens by adding filters), canonicalize/hash (idempotent).
*Recommended solution:* Hypothesis suites for the pure functions (`score_item`, `_enforce_budget`, `compute_semantic_trust_score`, `blend_memory_confidence`, `narrow_visibility` — all deliberately DB-free already, which makes this cheap) plus stateful tests for supersede/dedup sequences against Postgres.
*Difficulty:* Low-Medium.
*Expected impact:* Confidence in the exact places manual review keeps finding subtle issues (e.g., V2-T7's race).

---

**V2-Q2 — No concurrency tests for the write-path races**
*Severity:* **Medium** · *Category:* testing
*Rationale:* The dedup unique index, expire-before-insert supersession, `FOR UPDATE SKIP LOCKED` claims, and RLS-context-per-transaction plumbing are all concurrency mechanisms tested only sequentially. Two simultaneous identical remembers, concurrent supersede vs. PATCH, competing workers, and feedback races (V2-T7) are the incidents waiting.
*Recommended solution:* A small `asyncio.gather`-based concurrency suite against Postgres: N parallel identical remembers → exactly one row; parallel supersedes → one winner + one 409; two workers, one job; parallel feedback → importance equals sum of clamped deltas.
*Difficulty:* Medium.
*Expected impact:* The strongest guarantees in the docs become tested guarantees.

---

**V2-Q3 — No performance regression benchmarks**
*Severity:* **Medium** · *Category:* testing / scaling
*Rationale:* `test_recall_scaling.py` asserts query-count/shape properties, but nothing measures latency at realistic corpus sizes (100k–1M items, 10–100 tenants) or catches a 3× regression from an innocent predicate change. Scaling claims (bounded recall) are structural, not empirical.
*Recommended solution:* A `benchmarks/` harness (seeded synthetic corpus generator + pytest-benchmark or a standalone script) run nightly/on-demand, not per-PR: startup recall p50/p99, semantic recall with/without expansion, remember throughput, worker drain rate. Store results as CI artifacts; alert on threshold. The corpus generator doubles as the eval-harness substrate (V2-R1).
*Difficulty:* Medium.
*Expected impact:* Scaling regressions caught before users report them; data for the V2-S2 decision.

---

**V2-Q4 — Residual SQLite suites certify route logic against a schema that isn't the product's**
*Severity:* **Low** · *Category:* testing
*Rationale:* Acknowledged in the last audit (F7, partially addressed): auth/export/hygiene suites still run on hand-rolled SQLite with no constraints/RLS. Now that CI has Postgres service containers, the remaining SQLite fixtures are pure risk (they pass where Postgres would fail) with no remaining benefit.
*Recommended solution:* Finish the migration; delete the SQLite fixture path; keep `ENGRAM_FAIL_ON_DB_SKIP` guarding skips.
*Difficulty:* Low.
*Expected impact:* One test reality.

### 3.11 Product strategy

---

**V2-P1 — No extraction pipeline: every competitor ingests conversations; Engram ingests conclusions**
*Severity:* **High** · *Category:* strategy
*Rationale:* Mem0, Zep, Letta, and LangGraph memory all accept raw conversation turns and extract memories automatically. Engram's design places lifecycle hooks client-side (a locked decision, and a sound one), but that decision concerns *when* to capture — it does not preclude the service offering *what to extract* as a capability. Today a new adopter must build their own extraction before Engram stores anything interesting; that is the single largest adoption filter, and it wastes Engram's best asset: extracted memories are exactly the low-authority, needs-review content the trust pipeline was built to govern.
*Recommended solution:* `POST /v1/extract`: submit a transcript/document; the service (async job, like classification refine) proposes atomic candidate memories — each with kind/wing/room suggestions, extraction confidence, subject detection, and `source_uri`/`source_session` provenance — returned for client confirmation or written directly as `proposed` with `source_type=extraction` (caller's choice). Hooks then reduce to "post the buffer at the right moments," which is trivially portable to any framework. Dedup/conflict machinery already handles the noise.
*Difficulty:* Medium-High (prompt engineering + eval more than plumbing; reuses classification's provider infra and V2-D3 batch writes).
*Expected impact:* Closes the adoption gap with the exact feature competitors have, but governed — "extraction with a review pipeline" is a stronger claim than extraction alone.

---

**V2-P2 — The trust story is unquantified: define and publish trust metrics**
*Severity:* **Medium** · *Category:* strategy
*Rationale:* "Trust-aware memory" is the positioning, but nothing measures whether trust weighting *prevents bad outcomes*: how often did recall surface memories that were later disputed/superseded? How much does trust-weighting reduce that vs. pure similarity? These are computable from `recall_logs` + subsequent `item_events` — retroactively, today. Benchmarks (V2-R1) prove retrieval quality; trust metrics prove the *differentiator*.
*Recommended solution:* Define 3–4 headline metrics: *regret rate* (recalled items later invalidated/disputed within N days), *trust lift* (regret with vs. without trust weighting, replayable offline since recall_logs stores the query), *provenance coverage* (% of recalled items with source attribution), *time-to-correction* (dispute → resolution latency). Compute in the stats endpoint; publish alongside benchmarks.
*Difficulty:* Medium.
*Expected impact:* The moat becomes a number. Nobody else can publish these because nobody else stores what they require.

---

**V2-P3 — Positioning risk: "temporal knowledge graph" narrative is being ceded to Zep/Graphiti**
*Severity:* **Medium** · *Category:* strategy
*Rationale:* Engram's schema is strictly more capable temporally (bi-temporal validity + total audit order + supersession lineage) than the systems marketing themselves on temporal knowledge graphs, yet exposes none of it (V2-R2). Meanwhile the KG triple store is the least-integrated corner of the product (no semantic search over triples, no path queries, no recall integration beyond timeline endpoint).
*Recommended solution:* Ship V2-R2 (as-of recall + history) and V2-M1 (change feed), then tell that story explicitly in positioning: "every memory system remembers; Engram remembers *what you believed, when, and why it changed*." Defer deep KG features (path queries, graph embeddings) until edges are populated and used.
*Difficulty:* — (narrative consequence of R2/M1).
*Expected impact:* Occupies the temporal ground with better fundamentals than the incumbents of that narrative.

---

## 4. Strategic Opportunities

These are the trajectory-changing bets, in recommended order of commitment. Each is a program, not a ticket; the backlog (§5) decomposes the first steps.

### SO-1 · The Consolidation Engine — memory that improves with age
Cluster → synthesize → propose → supersede-with-provenance (V2-K1, V2-N2, V2-A2). Every competitor's store degrades as it grows; an Engram deployment would *sharpen*: observations compost into verified summaries, duplicates collapse into canonicals, and the audit trail preserves every derivation. The trust substrate is what makes this safe to automate (proposals, authority caps, review) — competitors would have to build Engram's whole trust layer first to copy it. This is the "best memory engine" feature.

### SO-2 · Time-travel memory — own the temporal narrative
As-of recall, item history, change feeds, trust-metric replay (V2-R2, V2-M1, V2-P2/P3). The schema already paid the storage cost; this is query and API work that converts an internal audit capability into agent-facing superpowers ("what changed since I slept?", "what did we believe when we made this decision?") and a positioning wedge against Zep/Graphiti.

### SO-3 · The Measurement Platform — make quality provable
Eval harness + public benchmarks + trust metrics + retrieval telemetry + performance benchmarks (V2-R1, V2-P2, V2-O1, V2-Q3). Everything else in retrieval and scoring is guesswork until this exists, and the published numbers are the adoption story. Cheapest of the four bets; do it first or in parallel with everything.

### SO-4 · Governed collaboration — make multi-agent the moat it claims to be
Change feeds, Path B quorum, expertise/reliability tracking, self-maintenance proposals, structured working sets with usage attribution (V2-M1..M3, V2-N3, V2-N4). Individually medium items; together they form the answer to "why not just give each agent its own vector store?" — which is the question every multi-agent team asks.

### SO-5 · Extraction through the trust pipeline — the adoption unlock
`/v1/extract` + batch writes + TS SDK + demo scenario (V2-P1, V2-D2, V2-D3, V2-D5). Not differentiating by itself — it's table stakes done the Engram way — but it is the bridge across which every new user reaches the differentiated features. Without it, SO-1..4 have a small audience.

### SO-6 · One graph — collapse five relationship primitives into `memory_edges`
(V2-A1, V2-A2, V2-K4.) A simplification bet rather than a feature bet: less code, fewer concepts, multi-conflict support, and a single traversal engine that supersession, conflicts, derivations, KG backing, and tunnels all feed. It quietly underwrites SO-1 (derivation edges), SO-2 (lineage), and SO-4 (duplicate/canonical propagation).

**The review-standard question** — "what would OpenAI/Anthropic/DeepMind still need to choose Engram?" — is answered by exactly these: provable retrieval quality (SO-3), memory that self-organizes at scale (SO-1), temporal/causal introspection (SO-2), and safe multi-agent write governance (SO-4) on top of the tenancy/audit substrate that already exists. None of them would blink at building CRUD + pgvector themselves; they would not want to rebuild a governed, measurable, self-consolidating institutional memory.

---

## 5. Prioritized Engineering Backlog

Sequenced in five phases. Every item is scoped to be independently landable; IDs are stable for tracking. "Scope" is rough engineer-time at this codebase's demonstrated velocity.

### Phase 0 — Trust integrity (do immediately; small, urgent, blocking)

| ID | Item | Rationale | Depends on | Scope |
|---|---|---|---|---|
| V2-BL-001 | Authenticated actor identity on all mutation events (V2-T3) | Audit trail + dispute gates are spoofable today | — | S (≤1 wk) |
| V2-BL-002 | Eligibility predicate on all mutation endpoints (V2-T5) | Private memories are tenant-mutable | — | S |
| V2-BL-003 | Review-transition authorization policy (V2-T1) + human-only verify (V2-T2) | Promotion pipeline & human_verified bypassable | BL-001 | S-M (1–2 wk) |
| V2-BL-004 | Enforce scopes across all routes; add `review` scope (V2-T4) | Least-privilege keys are fiction | BL-003 | S |
| V2-BL-005 | Atomic + deduped + rate-capped feedback (V2-T7) | Race + collusion holes; Path B prerequisite | — | S |
| V2-BL-006 | Ordinal `authority` column; supersession compares ordinals (V2-T6) | Config edits can invert the authority promise | — | M (2 wk, incl. migration) |

*Sequencing note:* BL-001..005 are one focused sprint touching the same routes — land as one series with a shared authorization test suite (two-principal fixtures asserting every mutation path).

### Phase 1 — Measurement substrate (parallel with Phase 0; enables everything after)

| ID | Item | Rationale | Depends on | Scope |
|---|---|---|---|---|
| V2-BL-010 | Retrieval eval harness: labeled corpus + nDCG/recall@k in CI (V2-R1) | All scoring is unvalidated; gates every future ranking change | — | M (2–3 wk) |
| V2-BL-011 | Prometheus metrics + starter dashboard (V2-O1) | Blind ops; prerequisite for scaling decisions | — | M (2 wk) |
| V2-BL-012 | Job admin API + worker heartbeat + dead-job alerting (V2-O2) | Async failures currently invisible | BL-011 | S |
| V2-BL-013 | Public benchmark runs (LongMemEval/LOCOMO) + trust-lift metric (V2-R1, V2-P2) | The adoption numbers | BL-010 | M |
| V2-BL-014 | Synthetic corpus generator + perf benchmark suite (V2-Q3) | Scaling regressions; feeds V2-S2 decision | — | M |
| V2-BL-015 | RLS policy UUID-comparison migration (V2-S1) | Cheap, compounding query cost; verify with EXPLAIN | — | S |

### Phase 2 — Retrieval & write-path architecture

| ID | Item | Rationale | Depends on | Scope |
|---|---|---|---|---|
| V2-BL-020 | Extract `engram/write.py` write engine; fix `generate_embedding` signature (V2-A5) | Reuse for batch/extract/consolidation; kills introspection hacks | — | M |
| V2-BL-021 | Edge write path: system-emitted edges (supersede/conflict) + edge CRUD API + MCP tool (V2-A2) | Relationship recall currently runs on an empty graph | — | M |
| V2-BL-022 | Startup recall filters (kind/wing/room) + optional context conditioning (V2-R3) | Task-relevant working sets | BL-010 (to measure) | M |
| V2-BL-023 | Async semantic-recall telemetry + read-engine routing (V2-A4) | Symmetry with startup; replica-servable semantic recall | — | S |
| V2-BL-024 | MMR-style diversity in budget packing (V2-R4a) | Budget efficiency; measurable | BL-010 | S |
| V2-BL-025 | Contradiction pairing in recall responses (`tensions`) (V2-R5) | Conflict machinery becomes agent-visible | — | M |
| V2-BL-026 | As-of recall/search + `GET /items/{id}/history` (V2-R2) | Temporal differentiator; schema-ready | — | M (2–3 wk) |
| V2-BL-027 | Scoring-profile consolidation; tenant profile selection (V2-A3) | Tunable, versioned ranking | BL-010, V2-D4 helpful | M |
| V2-BL-028 | Real token counting, pluggable (V2-R7) | Honest budgets | — | S |
| V2-BL-029 | Typed response models across items/review/KG (V2-D1) | OpenAPI completeness; unblocks TS SDK | do with BL-002/003 | M |

### Phase 3 — Knowledge lifecycle & AI-native

| ID | Item | Rationale | Depends on | Scope |
|---|---|---|---|---|
| V2-BL-030 | Retention sweep v1: job pruning + log retention + aging-archival proposals (V2-K2) | Unbounded growth; recall hygiene | BL-011 | M |
| V2-BL-031 | `GET /items/{id}/explain` introspection dossier (V2-N1) | Highest demo-value/cost ratio in the audit | BL-021 (richer with edges) | S-M |
| V2-BL-032 | Uncertainty propagation + derivation-staleness worker (V2-N2) | Trust story for derived artifacts; consolidation prerequisite | BL-021 | M |
| V2-BL-033 | Consolidation engine v1: same-subject observation fan-in (V2-K1) | The flagship: memory that improves with age | BL-020, 021, 032 | L (4–6 wk) |
| V2-BL-034 | Cross-principal duplicate canonicalization (V2-K4, V2-R4b) | Trust-signal concentration | BL-021, 033 patterns | M |
| V2-BL-035 | Structured working set + `used_memories` feedback attribution (V2-N3) | Citable briefings; feedback flywheel | BL-022 | M |
| V2-BL-036 | Taxonomy registry with propose-on-write + merge/rename ops (V2-K3) | Stops vocabulary drift before it's expensive | — | M |
| V2-BL-037 | Self-maintenance proposals API (V2-N4) | Fleet-distributed hygiene, safely | BL-003 (authz), BL-033 | M |

### Phase 4 — Multi-agent & adoption

| ID | Item | Rationale | Depends on | Scope |
|---|---|---|---|---|
| V2-BL-040 | Change feed (`GET /v1/changes?since=`) + recall delta (V2-M1) | Cache-invalidation semantics for shared memory | BL-001 (event integrity) | M |
| V2-BL-041 | Path B quorum promotion (V2-M2) | Documented promise; multi-agent showcase | BL-001, 005 | S |
| V2-BL-042 | Principal reliability stats + expertise routing (V2-M3) | Identity-aware trust | BL-011 | M |
| V2-BL-043 | Batch remember API (V2-D3) | Cheap capture; importer migration | BL-020 | S |
| V2-BL-044 | `/v1/extract` extraction-as-a-service (V2-P1) | The adoption unlock, governed | BL-020, 043 | L (3–5 wk) |
| V2-BL-045 | TypeScript SDK (generated + polished) (V2-D2) | Ecosystem coverage | BL-029 | M |
| V2-BL-046 | Demo scenario + `seed-demo` + trust-loop tutorial (V2-D5) | Afternoon-to-understand | BL-031 shines here | S |
| V2-BL-047 | Tenant policy → validated JSONB document (V2-D4) | Product-speed config evolution | — | M |

### Phase 5 — Scale & hosted readiness

| ID | Item | Rationale | Depends on | Scope |
|---|---|---|---|---|
| V2-BL-050 | Queue fairness: priority-first ordering + per-tenant caps (V2-S3) | Noisy-neighbor protection | BL-011 (to observe) | S-M |
| V2-BL-051 | Batched recall telemetry (drop per-recall job rows) (V2-S4) | Queue write amplification | BL-023 | S-M |
| V2-BL-052 | Hot-counter side table (`memory_item_stats`) (V2-S5) | Append-mostly memory_items; vacuum relief | BL-051 | M |
| V2-BL-053 | Per-tenant usage metering → quotas/rate limits (V2-S6) | Hosted prerequisite; blast-radius control | BL-011 | M |
| V2-BL-054 | Tenant snapshot export/import + PITR docs (V2-O3) | Tenant-granular DR; portability | — | M |
| V2-BL-055 | Hard delete + tombstones (already-designed slice) (V2-O4) | Compliance; incident response | — | M |
| V2-BL-056 | Vector-index partitioning plan + trigger thresholds (V2-S2) | Executed only when BL-011/014 data says so | BL-011, 014 | S (plan) / L (exec) |
| V2-BL-057 | Property-based + concurrency test suites (V2-Q1, Q2); retire SQLite fixtures (V2-Q4) | Guarantees become tested | — | M |
| V2-BL-058 | Migration renumber + checksum verification (V2-A7) | Multi-env schema-drift prevention | — | S |
| V2-BL-059 | Visibility label truthfulness on default writes (V2-A6) | Honest visibility column | — | S |
| V2-BL-060 | Relationship-primitive consolidation cutover (V2-A1 full) | One graph; staged after BL-021 proves the substrate | BL-021, 033 | L |

### Sequencing summary

```
Phase 0 (trust integrity)  ──┐  1 sprint, non-negotiable
Phase 1 (measurement)      ──┼─ parallel; everything downstream cites it
Phase 2 (retrieval/write)  ──┘  edges + temporal + context are the quarter's retrieval story
Phase 3 (lifecycle/AI-native)   consolidation is the flagship; explain/tensions are quick wins inside it
Phase 4 (multi-agent/adoption)  change feed + Path B make the moat real; extract + TS open the funnel
Phase 5 (scale/hosted)          data-driven; most items wait for Phase 1 telemetry to justify them
```

---

## 6. Closing note on review standard

Challenged assumptions worth recording:

- **"Append-first means keep everything hot"** — no; it means content immutability. Consolidation and aging (SO-1, V2-K2) are compatible with, and required by, append-first at scale.
- **"Trust weights are the trust model"** — no; enforcement is. Phase 0 exists because the most sophisticated scoring in the field is worth little if `review_status` is writable by anyone.
- **"More retrieval features next"** — no; measurement next. Every ranking change until V2-BL-010 lands is a coin flip with good documentation.
- **"The KG/tunnels/edges triad is product richness"** — partially; it is also three half-integrated graphs. One substrate, fully integrated, beats three vocabularies.
- **"Client-side hooks mean no server extraction"** — the locked decision governs *when*, not *what*. `/v1/extract` respects the boundary and closes the funnel gap.

The foundation earned this audit's assumption of maturity. The next cycle's bar is different: not "does it hold," but "can it prove it's the best" — and the path there is measurement, consolidation, time, and governed collaboration.
