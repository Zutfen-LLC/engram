# Engram Memory Audit — Classification, Storage, Recall, and the Path to Default

**Date:** 2026-07-08
**Scope:** Full audit of the implemented service (`engram/`, `migrations/`, auth, adapters at surface level) with special focus on memory classification, storage, and recall. Includes a prioritized improvement plan for the self-hosted product and a long-term plan for the hosted service.
**Method:** Line-level read of the core service (schema, write path, classification, conflicts, recall, search, promotion, auth, RLS plumbing), cross-checked against `docs/design.md` v2.3, `README.md`, and the MVP backlog. Findings cite `file:line` where they anchor to code. The Hermes hooks adapter and importers were reviewed at surface level only.

---

## 1. Executive summary

Engram's core thesis — trust-modeled, auditable, explainable memory — is genuinely differentiated and mostly implemented. The schema is thoughtful, the append-first + `item_events` audit design is right, the trust/promotion machinery exists, and the docs are unusually honest about verification status.

The audit found one theme that dominates everything else: **the trust and isolation model is enforced on the write path but not on the read path.** Visibility levels, workspace membership, and (in the shipped deployment) even Postgres RLS do not actually constrain what recall and search return. For a product whose tagline is "can an AI system trust what it remembers," the recall paths are currently the least trustworthy component. This must be fixed before open-source launch — it is the exact class of failure the README promises Engram prevents.

Beyond that, the audit identifies:

- **Classification** is architecturally sound (rules → LLM → conservative fallback, with provenance) but functionally shallow: it never feeds `memory_confidence` (so auto-promotion gates on a value classification never touches), runs synchronously on the write path, ignores its own visibility suggestion, and the seed rules actively misclassify. There is no extraction capability, which is the single biggest adoption gap versus competing memory layers.
- **Storage** is a good single-node design with several claims ahead of reality (multi-model embeddings, tenant-configurable kinds, RLS backstop) and clear scaling cliffs (Python-side scoring over the whole corpus, O(n·bcrypt) auth, unbounded log tables).
- **Recall** has the right skeleton (deterministic startup set, explanations, anti-feedback guardrails) but semantic recall ignores the trust model entirely, relationship-aware recall doesn't exist despite being claimed as done, several designed guardrails (quorum reset, default budgets, dispute gate) are unwired, and recall quality is unmeasured — there is no eval harness, which is what "de facto memory layer" status will ultimately be judged on.

Section 8 gives the prioritized roadmap. The one-line version: **P0 = make the read path honor the trust/visibility model and make RLS real; P1 = make classification and recall actually good (and provably good, via evals); P2 = adoption surface (extraction, TS SDK, integrations, benchmarks); P3 = hosted foundations, which mostly fall out of P0 done properly.**

---

## 2. Findings index

| # | Severity | Area | Finding |
|---|----------|------|---------|
| F1 | **P0** | Recall/Search | `visibility` is not enforced on any memory read path — private memories leak tenant-wide |
| F2 | **P0** | Recall/Search | Workspace membership is never enforced; `check_workspace_membership` is dead code |
| F3 | **P0** | Storage/RLS | RLS is inert in the shipped deployment (app connects as table owner; no `FORCE ROW LEVEL SECURITY`) |
| F4 | **P0** | Auth | API-key verification is O(n·bcrypt) over all keys of all tenants, per request |
| F5 | **P1** | Storage | RLS session context is transaction-scoped and silently lost after rollback/commit mid-request |
| F6 | **P1** | Items | ~~`POST /items/{id}/supersede` inserts the replacement before expiring the original — latent unique-index violation on real Postgres~~ **Addressed (ENG-AUD-006):** handler now locks + validates + expires the original *before* inserting the replacement in a single transaction; eligibility (409), authority, dual provenance, and Postgres-backed rollback/unique-index tests added |
| F7 | **P1** | Testing | ~~Several "DB" test suites run against a hand-rolled SQLite schema with no constraints, indexes, or RLS~~ **Partially addressed (ENG-AUD-006):** supersede invariant coverage migrated to real Postgres (tests/test_supersede.py); the SQLite suite (test_items.py) is now route-logic-only with a comment marking it as not certifying DB invariants. Sibling suites (auth/export/hygiene) remain on SQLite and are tracked for a later pass |
| F8 | **P1** | Classification | ~~LLM classification never refines `memory_confidence`; `suggested_visibility` is never applied; confidence floor of 0.7 makes low confidence unrepresentable~~ **Addressed (ENG-AUD-005):** classifier confidence blends into `memory_confidence` (authority-capped, source-type-weighted); `suggested_visibility` applied downward-only on remember; 0.7 floor removed (real `0.0–0.95` signal) |
| F9 | **P1** | Classification | ~~Seed skip-rules misfire (`\b(ok|done|failed)\b` matches almost any agent output) and "skip" still stores the item~~ **Addressed (ENG-AUD-005):** skip rules reworked to whole-message status-only anchors; doctrine requires explicit policy/invariant phrasing; "skip" naming reframed as low-information status. (Deferred: explicit `store/skip/quarantine` disposition — out of scope for this slice) |
| F10 | **P1** | Recall | Semantic recall ranks by similarity only — the trust model is absent from semantic ranking |
| F11 | **P1** | Recall | Auto-promotion is not wired into startup recall (CLI/admin only), contradicting BL-004's status and design §3 |
| F12 | **P1** | Promotion | Path A is missing the "no dispute event from another principal" gate |
| F13 | **P1** | Conflicts | Conflict detection checks only the single nearest neighbor of the *same kind and workspace*, and is entirely disabled when embeddings are off (i.e., in the dogfood deployment) |
| F14 | **P1** | Search | `SearchRequest.wing/room/kind` filters are accepted and silently ignored |
| F15 | **P1** | Recall | Default recall is unbounded; `recall_byte_budget` and `quorum_reset_agent_count` config are dead |
| F16 | **P2** | Storage | `vector(1536)` fixed column contradicts the model-keyed multi-model embedding design; `EMBEDDING_MODEL` is a hardcoded constant |
| F17 | **P2** | Storage | `chk_kind` CHECK constraint contradicts "tenant-configurable taxonomy" and omits design kinds (`procedure`, `summary`) |
| F18 | **P2** | Recall | Scoring loads the entire active corpus into Python; recall is also a write (recall-count updates), blocking read-replica scaling |
| F19 | **P2** | Recall | "Relationship-aware recall" is claimed done (README roadmap layer 3) but recall never touches the KG or tunnels |
| F20 | **P2** | Write path | Sync LLM classification + 6 `DISTINCT` vocabulary scans per unclassified write — **addressed (ENG-AUD-008)** |

Details for each in the sections below.

---

## 3. Trust boundary and isolation (F1–F7) — fix before anything else

### F1. Visibility is unenforced on every memory read path (P0)

The schema, docs, and README present a four-level visibility model (`private | workspace | tenant | public`). It is stored on write and **never consulted on read**:

- Startup recall — `engram/recall.py:147-155` (`_fetch_active_items`) filters on tenant, `review_status`, `valid_to`, and optional workspace slug. No `visibility` filter, no principal check.
- Semantic recall / semantic search — `engram/semantic.py:56-122` filters on embedding model, review status, `valid_to`. No visibility, no workspace, no principal.
- Keyword search — `engram/api/routes/memory.py:320-341` raw SQL, no visibility/workspace/principal filter.
- Item listing — `engram/api/routes/memory.py:970-1026`, same.

Concrete failure: the diary endpoint carefully locks `visibility='private'` and gates `GET /diary/{principal}` to the owner (`engram/api/routes/diary.py:194-232`) — but the same rows are reachable by any principal in the tenant through `/v1/search` and `/v1/recall`. A user-written diary entry gets `review_status='active'` (`diary.py:130`) and will appear in **another agent's startup recall working set**. Agent diary entries (`proposed`) surface through semantic recall, which includes proposed items by design.

Only the KG routes implement visibility inheritance (`engram/api/routes/kg.py:251-264, 323-337`), which proves the intended pattern exists — it just never made it to the main read paths. BL-003's status line ("visibility-scoped") is inaccurate.

**Fix:** one shared eligibility predicate, used by *every* read path (startup recall, semantic recall, all three search modes, list items, hygiene, export):

```sql
visibility = 'tenant'
OR visibility = 'public'
OR (visibility = 'private'   AND principal_id = :caller)
OR (visibility = 'workspace' AND workspace_id IN (
      SELECT workspace_id FROM workspace_members WHERE principal_id = :caller))
```

Implement it once (a SQLAlchemy composable filter + an equivalent SQL fragment for the raw-SQL paths), test it once per path, and make it impossible to add a read path without it (e.g., a repository-layer function that is the only way to build memory read queries). Add a regression test: two principals in one tenant, one private and one workspace memory each; assert every endpoint returns only what the caller may see — **run against real Postgres** (see F7).

### F2. Workspace membership never enforced (P0)

`check_workspace_membership` (`engram/auth.py:205-219`) has zero call sites. Recall accepts any workspace slug in the caller's tenant; search ignores workspace entirely. The `workspace_members` table and role column (`owner|admin|member|viewer`) are pure decoration today. Fold membership into the F1 predicate; decide whether `viewer` restricts writes (recommended: yes — enforce role on `/remember` with an explicit test).

### F3. RLS is inert in the shipped deployment (P0)

`ALTER TABLE ... ENABLE ROW LEVEL SECURITY` does not apply to the table owner. The compose deployment runs migrations *and* the service as the same `POSTGRES_USER=engram` role (`docker-compose.yml`, `.env.example`), so every policy in `migrations/001_init.sql:449-524` is bypassed. There is no `FORCE ROW LEVEL SECURITY` anywhere in the repo, and the README's own CLI note ("run as the table-owning DB role to bypass RLS") confirms the app role is the owner. The headline claim — "one forgotten WHERE clause cannot cause a cross-tenant leak" — is not true of the default deployment.

**Fix (self-hosted now, mandatory for hosted):**
1. Migration adds a dedicated non-owner application role (`engram_app`) with table grants; migrations continue to run as the owner.
2. `ALTER TABLE ... FORCE ROW LEVEL SECURITY` on all tenant-scoped tables (protects even the owner, and makes single-role deployments safe).
3. Compose/docs updated: service connects as `engram_app`; `promote-proposed`-style cross-tenant CLI paths get an explicit `BYPASSRLS` role or per-tenant loops.
4. A CI test that connects as the app role and proves a cross-tenant `SELECT` returns zero rows. Today no test anywhere exercises RLS as a non-owner.

### F4. O(n·bcrypt) API-key auth (P0 for hosted, P1 now)

`get_current_principal` loads **every non-revoked key across all tenants** and bcrypt-checks each (`engram/auth.py:144-169`). bcrypt is ~100ms per check by design; 50 tenants × 3 keys ≈ 15 seconds per request worst case. It also opens an extra DB session and runs a lookup query on every request even when auth is disabled.

**Fix:** split the key into `eng_<key_id>_<secret>`; store `key_id` (indexed) plus a fast deterministic digest of the secret (SHA-256/HMAC — API keys are high-entropy random, so bcrypt's slow hashing is not needed the way it is for passwords; keep bcrypt only if you accept a per-request cost, looked up by `key_id`). Cache resolved principals in-process with a short TTL and revocation check. This is also the natural place to attach per-key rate limits later (hosted).

### F5. RLS context is lost after rollback/commit mid-request (P1)

`get_session` sets `app.tenant_id`/`app.principal_id` via `set_config(..., is_local=true)` (`engram/db.py:43-53`) — transaction-local. Any statement after a `commit()` or `rollback()` in the same request runs **without tenant context**. Today that's masked by F3 (RLS not applied), but the moment F3 is fixed, the dedup re-query after `IntegrityError` rollback (`memory.py:480-494`) and the conflict-dedup rollback path (`memory.py:569-577`) will silently see zero rows and misbehave. Fix together with F3: re-apply the context after every rollback/commit (a session event listener), or use session-scoped `set_config(..., false)` on a connection checked out per request and reset on check-in.

### F6. Supersede endpoint: latent unique-index violation (P1)

`POST /v1/items/{id}/supersede` (`memory.py:1088-1143`) inserts a full copy of the row — same `(tenant_id, workspace_id, principal_id, content_hash)`, `valid_to = NULL` — *before* expiring the original. `idx_memitems_dedup` (`001_init.sql:380-382`, `NULLS NOT DISTINCT`, partial on `valid_to IS NULL AND review_status != 'rejected'`) should reject that insert on real Postgres. The endpoint's test passes because it runs on SQLite without the index (F7). Fix the ordering (expire first, insert second, in one transaction) and add a Postgres-backed test.

> **Landed (ENG-AUD-006):** the handler now (1) locks the original with `SELECT ... FOR UPDATE`, (2) validates eligibility (409 on already-expired/rejected), (3) validates authority via the centralized `authority_allows_supersession` helper, (4) expires the original *before* inserting the replacement — all in one transaction, so a failed replacement insert rolls back the expiration. Dual provenance events link the original forward and the replacement back. Coverage migrated to real Postgres in `tests/test_supersede.py` (unique-index safety, rollback, dedup interaction, authority, cross-tenant RLS, singleton supersession, eligibility).

### F7. Test fidelity: hand-rolled SQLite schemas (P1)

`tests/test_items.py` (and siblings: auth, export, hygiene…) create their own SQLite DDL with **no CHECK constraints, no unique indexes, no RLS, no generated tsvector** (`tests/test_items.py:16-90`). These tests validate route logic but certify nothing about the real schema — F6 is the proof. Recommendation: converge on the Postgres-backed fixture used by the conflict/remember/search suites for anything that touches `memory_items`, and reserve SQLite for pure-logic units. Add the F1/F3 isolation tests to the Postgres suite. This is cheap insurance and directly protects the product's core claims.

> **Partially landed (ENG-AUD-006):** the supersede coverage that depends on the real unique index/RLS moved to `tests/test_supersede.py` (real Postgres); `tests/test_items.py` is now route-logic-only with a comment marking it as not certifying DB invariants. The sibling SQLite suites (auth/export/hygiene) remain a tracked follow-up.

---

## 4. Memory classification — deep dive

### What's good

- The three-tier design (tenant regex rules → LLM → conservative `fact` fallback) with full provenance recorded to `item_events` (`memory.py:509-538`) is the right shape, and better than most competing layers, which classify opaquely or not at all.
- Vocabulary is grounded in observed data (existing kinds/wings/rooms feed the LLM prompt) rather than free invention, and LLM output is validated against the vocabulary (`classification.py:338-401`).
- The write-time classification event gives you a labeled dataset for free — every classified item records what the classifier saw and chose. This is a real asset (see "evals" below).

### Defects and gaps

**F8 — Classification doesn't feed the trust model.** Design §4: "LLM classification in Phase 1B refines `memory_confidence` per item." In reality `memory_confidence` comes only from `tenant_config` source defaults (`memory.py:432-434`); the classifier's confidence lives only in provenance JSON. Consequences: (a) auto-promotion gates on a number classification never touches, so `sync_turn`/`pre_compress` writes can *never* cross the 0.7 threshold without human review — the exact frozen-queue failure design §3 warns about, re-created; (b) `suggested_visibility` exists on `ClassificationResult` (`classification.py:36`) and is never read — `remember` always uses the request default (`memory.py:462`). Additionally the LLM confidence is clamped `max(0.7, min(0.95, x))` (`classification.py:358`), so the classifier literally cannot express doubt below the promotion threshold; combined with (a) this is currently harmless and precisely why it's dangerous later.

*Fix:* on LLM-classified writes, set `memory_confidence = f(source_default, classifier_confidence)` (e.g., weighted blend, capped by source authority so a chatty source can't self-promote past its ceiling), record old/new in the event, and honor `suggested_visibility` **only downward** (never widen visibility on a suggestion — a classifier must not be able to make something more public). Remove the 0.7 floor; keep the threshold-based conservative fallback.

> **Landed (ENG-AUD-005):** classifier confidence now blends into `memory_confidence` via a source-type-weighted policy (0.5·default + 0.5·classifier for automated sources, 0.85·default + 0.15·classifier for manual/import/migration), capped by `max(default, source_trust)` and clamped to `[0,1]`. `suggested_visibility` is applied downward-only (`engram/classification_trust.py`). The 0.7 floor is removed; confidence is a real `0.0–0.95` signal and below-threshold results are no longer re-floored. The classification event records default/final confidence, requested/suggested/final visibility, and the applied policy flags.

**F9 — Seed rules misfire.** `skip_tool_output` = `\b(passed|failed|ok|done)\b` (priority 10) matches a huge fraction of natural agent output ("the deploy is done", "tests passed after the fix") and forces the conservative `fact` default — silently suppressing the kind/wing/room rules for exactly the content they were written for. Also `kind_doctrine` = `must|should|always|never` will label casual statements as doctrine — the highest-stakes kind (disputed doctrine stays in startup recall). And semantically, "skip" doesn't skip: the item is still stored, just classified as `fact` at 0.6 (`classification.py:194-204`). Either give skip rules a real meaning (reject/quarantine with a `skipped` disposition the caller can see) or rename them. Rework the seeds; better, replace keyword rules as the middle tier (next point).

> **Landed (ENG-AUD-005):** seed rules reworked for all tenants (`005_classification_seed_rules.sql`, mirrored in `001_init.sql`). `skip_tool_output` → `skip_status_only` anchored with `\A...\Z` so it matches only whole-message status text; `skip_single_token` tightened to a whole-string single short token; `kind_doctrine` now requires explicit `doctrine`/`invariant` keywords, `policy:`/`rule:`/`invariant:` labels, or `must (never|always|not)`. The small "preferred approach" (status-only rules + conservative doctrine) was chosen; the explicit `store/conservative/skip/quarantine` disposition is deferred to a later classification redesign.

**Embedding-based classification as the no-LLM middle tier.** Engram already stores embeddings. A k-NN classifier over already-labeled items (label = kind/wing/room of the nearest confirmed neighbors, confidence = neighbor agreement × similarity) gives every tenant a self-improving classifier with zero LLM cost and no rule authoring, sitting between regex and LLM. Human corrections via PATCH become training signal automatically. This is a differentiator no competing layer ships, and it falls out of infrastructure you already have.

**F20 — Classification is synchronous on the write path.** An unclassified `remember` does: 6 `DISTINCT` scans over `memory_items`/`classification_rules` for vocabulary (`classification.py:96-151`), then optionally a blocking OpenAI chat completion, then optionally a second LLM call for conflict classification. For `sync_turn`-style chatty writers this is the difference between "memory is free" and "memory is a tax."

*Fix:* (a) cache vocabulary per tenant with event-based invalidation; (b) split classification into *write-time cheap* (rules/k-NN, provisional) and *async refine* (LLM job updates kind/wing/room/confidence and writes an `item_events` row — the audit trail already supports reclassification cleanly). A simple Postgres job queue (`FOR UPDATE SKIP LOCKED`) avoids new infrastructure and also absorbs embedding generation and promotion (see §6). The append-first model makes async refinement safe: nothing downstream assumes classification is final.

> **Status (ENG-AUD-008, addressed):** F20 is addressed. A Postgres-backed
> `jobs` table + `engram worker` (claim via `FOR UPDATE SKIP LOCKED`, retry with
> backoff, dead-letter after max attempts) moves the expensive write-path work
> off the request path: `/v1/remember` now runs rule-based classification only
> (no inline OpenAI call), creates the embedding placeholder, and enqueues
> `embedding.generate`; the worker then enqueues `conflict.check` once the
> embedding is ready and runs `classification.refine` (LLM) as a job. The six
> `DISTINCT` vocab scans are served from a per-tenant in-process TTL cache
> (`engram.classification`, `ENGRAM_VOCAB_CACHE_TTL_SECONDS`). Exact (content-
> hash) dedup stays synchronous; semantic dedup / auto-supersede / contradiction
> are now eventual state transitions applied by `conflict.check` jobs — so
> `/v1/remember` may return `created` while semantic conflict analysis is
> pending. The service degrades gracefully without a worker (jobs queue;
> semantic recall / refinement / semantic conflict detection lag until
> processed). `retention.sweep` is a documented stub (retention logic deferred).
> No Redis/Celery/SQS, extraction endpoint, k-NN classifier, Path B quorum, or
> hosted control plane is included.

**Extraction is the missing product surface.** Engram classifies *given* content, but the dominant integration question for a memory layer is "here is a conversation — figure out what to remember." Today that burden is entirely on clients (engram-hooks). Design principle 8 says lifecycle hooks are client-side — correct — but *extraction* (transcript → memory candidates with kind/subject/confidence) is classification intelligence, which principle 8 explicitly puts server-side. Recommendation: `POST /v1/extract` accepting a transcript/messages array, returning candidate memories (not writing them) or writing them as `proposed` with `source_type='extraction'` in one call. This single endpoint makes every framework integration ~10 lines and is the #1 thing that closes the gap with Mem0/Zep-class competitors.

**Provider lock-in.** Classification and conflict classification are OpenAI-only (`classification.py:317-335`, `conflicts.py:260-272`); `"local"` is documented in config comments but unimplemented (embeddings raise on it). For a self-hosted trust product this is a credibility gap — many target users self-host precisely to avoid third-party API dependencies. Add a provider interface with: Anthropic, OpenAI, OpenAI-compatible base URL (covers Ollama/vLLM/LM Studio in one stroke), and local sentence-transformers for embeddings. The OpenAI-compatible option is the 80/20.

**PII/sensitivity classification** (already deferred) should be re-prioritized to land *before* hosted, and ideally before broad OSS adoption: the secret guard (`safety.py`) rejects with a 422 today, which for automated writers means the memory (and everything in the same capture batch) is simply lost. Better: redact-and-store with a `redactions` provenance note, or store as `sensitivity='restricted'` pending review — a trust system should quarantine, not drop.

### Classification evals

There is no way to know whether classification is any good, or whether a rule/prompt/model change makes it better or worse. You already log every decision with inputs and provenance. Add: a golden set (a few hundred labeled examples per kind, seeded from dogfood data), a `scripts/eval_classification.py` harness that replays it through rules/k-NN/LLM tiers and reports per-kind precision/recall, and CI tracking of the no-LLM tiers. "De facto" status is won with published numbers, not adjectives.

---

## 5. Storage — deep dive

### What's good

- Postgres + pgvector single-backend is the right call and the locked decisions doc keeps scope honest. Append-first content + `item_events` + `recall_logs` with `scoring_version`/`config_version` is a genuinely auditable design.
- The composite FK from `memory_embeddings(memory_item_id, tenant_id)` → `memory_items(id, tenant_id)` (so RLS can filter embeddings before the HNSW join) shows real care; `NULLS NOT DISTINCT` on the dedup index likewise.
- The minimal migration runner with a `schema_migrations` table and baseline support is appropriately pragmatic.

### Findings

**F16 — Embedding model claims vs. reality.** The design promises model-keyed embeddings supporting model migration and multiple models. In reality: `embedding vector(1536)` fixes the dimension at DDL time (`001_init.sql:184`), `EMBEDDING_MODEL` is a module constant (`embeddings.py:22`), and every read path filters on that constant — a second model can't be stored (wrong dim) or queried (constant). `settings.embedding_dim` exists but only feeds the placeholder row's metadata.

*Fix:* per-model dimensioned storage. Practical options: (a) one table per registered model dimension (created by a small registry migration step), or (b) a single table with an untyped `vector` column — pgvector allows this — plus per-model partial HNSW indexes with fixed-dim casts, or (c) `halfvec` for cost. Add `embedding_models` registry (model name, dim, provider, active flag) in `tenant_config` or globally; make read paths select the tenant's active model. This unlocks re-embedding migration (backfill machinery already exists and is good), local models, and Matryoshka-style dimension choices — and it is much cheaper to do before there is hosted data.

**F17 — Kind vocabulary is hard-coded in a CHECK constraint.** `chk_kind` (`001_init.sql:133-136`) contradicts both the design (which lists `procedure` and `summary` as kinds — both rejected by the DB today) and the "tenant-configurable taxonomy" claim (true for wings/rooms, false for kinds). Kinds drive behavior (singleton supersession, disputed-doctrine recall), so a free-for-all is wrong — but the right shape is a `memory_kinds` reference table (global seed + tenant additions, with per-kind behavior flags: `singleton`, `stays_in_recall_when_disputed`, `default_importance`) instead of DDL. `procedure` in particular matters for the coding-agent audience ("how we deploy" is a procedure, not a fact).

**Scale cliffs to plan for (F18 partially, plus):**
- `item_events` RLS policy is a correlated `EXISTS` per row (`001_init.sql:484-491`) — denormalize `tenant_id` onto `item_events` (matching every other table) before it matters.
- `recall_logs` grows per recall with a `UUID[]` of the whole working set, and `item_events` grows per write; neither has retention. Add tenant-configurable retention + a hygiene job (and for hosted, make log retention a plan feature).
- `content_tsv` is hard-coded `'english'` (`001_init.sql:67-69`) — non-English memory silently gets bad FTS. Per-tenant language in `tenant_config`, with a migration story (regenerate the column or move to a trigger-maintained column).
- Dedup canonicalization lowercases (`canonicalize.py:22`) — fine for prose, wrong for code/config content where case is semantic; consider kind-aware canonicalization, and note the dedup index also ignores `kind`, so identical text stored as `fact` blocks the same text as `doctrine` (per principal). Low priority but worth a decision record.

**Deletion (already deferred, becomes existential for hosted).** `deletion_events` exists in schema; there is no delete endpoint. For GDPR/right-to-erasure the append-first model needs a real answer: hard delete with tombstone + KG cascade + embedding cascade (FK already cascades) + *event-log scrubbing policy* (item_events `old_value/new_value` can contain content-derived text — decide whether tombstoning includes event redaction). Do the design now even if implementation waits; it constrains hosted architecture (backups containing deleted data need retention-bounded rotation — the nightly `pg_dump` regime already gives you a bound; write it down as the stated policy).

**Backups/DR:** fine for dogfood (pg_dump + restore smoke test). For hosted: PITR (WAL archiving) per cluster, per-tenant logical export as a product feature (the CCA export endpoint is a seed of this).

---

## 6. Recall — deep dive

### What's good

- Deterministic, budgeted, *explained* startup recall with pinned bypass and logged scoring/config versions is the strongest part of the product. `reasons` arrays on every item is exactly right and rare in the market.
- The anti-feedback-loop penalty with floor + authority-weighted feedback is thoughtful design most memory layers haven't even noticed they need.

### Findings

**F10 — The trust model vanishes in semantic recall.** `execute_semantic_recall` ranks purely by cosine similarity (`recall.py:498-553`). A `pre_compress` guess at trust 0.3, proposed, unverified, will outrank pinned, human-verified doctrine if it's marginally closer to the query. The `unreviewed` warning is attached, but ordering *is* the product in a token-budgeted world. Semantic recall should rank by `similarity × trust_factor` where `trust_factor` blends the same components as startup scoring (a tenant-configurable exponent/weight, logged in `scoring_version` as `semantic-v2` — the versioning discipline already in place makes this a clean evolution). Same applies to `/v1/search` semantic and hybrid modes: RRF fusion (`memory.py:358-391`) fuses two trust-blind rankings.

**F19 — Relationship-aware recall doesn't exist.** README/roadmap layer 3 claims it's done; `recall.py` never touches `kg_triples` or `tunnels`. Either fix the claim (doc-truth pass discipline) or — better — implement the minimal honest version: after selecting the startup/semantic working set, do a 1-hop expansion via KG triples and tunnels from selected items (budget permitting) with `reasons: ["linked_via <predicate>"]`. This turns the KG from a parallel feature into recall infrastructure, which is the whole reason it exists.

**F11/F12 — Promotion gaps.** (a) BL-004's status says Path A runs "as a lazy check on startup recall"; it doesn't — only the CLI and `POST /v1/admin/promote` call `auto_promote_proposed_memories` (call sites: `cli.py:572`, `admin.py:256`). An operator who doesn't wire cron gets the frozen-queue pathology. Wire the lazy check (cheap: run promotion for the tenant at most once per N minutes, guarded by an advisory lock, before fetching items) or change the docs. (b) `promotion.py:186-202` checks confidence/age/conflict but not design §3's "no dispute event in `item_events` from another principal" — add the gate. (c) Path B (usage quorum) remains deferred; note `quorum_reset_agent_count` (`config.py:49`) and the design's "2+ non-author agents = partial penalty reset" are also unimplemented on the feedback side, so multi-agent fleets currently have *no* path to counteract the startup penalty without a human — the penalty floor is the only safety net.

**F13 — Conflict detection is narrower than it reads.** It compares against exactly one nearest neighbor, restricted to the same kind and same workspace (`conflicts.py:93-124`). A contradiction misfiled under a different kind (classification is fallible — see F9) or written at tenant level vs. workspace level is invisible. And when `embedding_provider=none` — the dogfood default — conflict detection doesn't run at all (`memory.py:563`), so the flagship "write-time contradiction detection" has never run in production. Improvements, in order: check top-k (k≈3–5) not top-1; drop the kind filter (or widen to kind-groups); run the check at promotion time as well (the designed "escape valve" — `conflict_check_on_write=False` currently means the check never happens anywhere); and once F16 lands, a cheap local embedding model means self-hosters get conflict detection out of the box instead of it being an OpenAI-only feature.

**F14/F15 — Small unwired promises.** Search filters (`wing/room/kind` on `SearchRequest`, `memory.py:116-118`) are accepted and ignored — implement or reject them; silently ignoring filters is the worst option for a trust product. Default recall is unbounded (`recall_byte_budget` config is dead; `RecallRequest` budgets default to `None`) — apply the config default when the caller specifies nothing. Budget packing stops at the first item that doesn't fit (`recall.py:194-221`) instead of skipping it — one long memory truncates everything below it. Token estimation is `bytes/4`; fine as fallback, but offer real tokenizer counts as an optional dependency since budgets are the API's contract with context windows.

**Recency bonus penalizes new memories.** `recency_bonus` derives solely from `last_recalled_at` (`recall.py:82-96`), so a never-recalled item scores 0 on 15% of the formula — a systematic bias *against* fresh memories and toward incumbents, the opposite of what "recency" suggests. Blend in freshness from `valid_from`/`created_at` for never-recalled items (e.g., `recency = max(recall_recency, freshness × 0.5)`); keep the penalty machinery on the recall-driven component only.

**F18 — Recall scalability.** Startup recall fetches *every* active item in scope into Python, scores, sorts, then issues an UPDATE against every returned item (`recall.py:257-363`). Consequences at scale: memory/latency proportional to corpus (not budget); the read path is a write path (write amplification on hot items, and it forecloses serving recall from read replicas — the natural first scaling move for a hosted read-heavy service). Fixes: (a) express the score as a SQL expression (it's a linear formula over columns; the penalty term is computable in SQL) with `ORDER BY score DESC LIMIT k×overfetch`, then budget-pack in Python; (b) decouple recall-signal updates — write them to `recall_logs` only (already done) and fold `recall_count`/`last_recalled_at`/`startup_recall_count` updates into an async batch job or a sampled update. This preserves determinism (same corpus + config ⇒ same output) while removing both cliffs.

### Recall evals — the moat, unbuilt

Engram's pitch is recall you can *trust*, but there is no measurement of recall quality — no benchmark harness, no offline replay, no metric on whether `useful`/`noise` feedback correlates with scoring. The data model is already perfect for it: `recall_logs` (what was surfaced, under which scoring/config version) + `feedback_events` (what was actually useful, linked by `recall_log_id`). Build: (1) an offline harness that replays logged recalls under candidate scoring configs and reports feedback-weighted precision; (2) a public benchmark run (LoCoMo, LongMemEval, or the emerging memory-bench suites) with published numbers — this is table stakes for "de facto" credibility, since Mem0/Zep both market benchmark results; (3) surface per-tenant recall-quality stats through the hygiene/stats endpoints so operators can see memory health. This is the highest-leverage medium-term investment in the audit.

---

## 7. Becoming the de facto memory layer — adoption gaps

The trust model is the differentiation; the following are the friction points that decide whether anyone shows up to experience it:

1. **Extraction endpoint** (§4) — turns "integrate Engram" from building a pipeline into calling one route.
2. **TypeScript SDK.** The agent ecosystem's center of gravity (Vercel AI SDK, Mastra, LangGraph.js, MCP clients) is TS-first. A thin generated-from-OpenAPI client with a hand-written ergonomic layer is enough. Publish the OpenAPI spec as an artifact.
3. **MCP over HTTP/SSE.** The adapter is stdio-only; hosted-Engram and remote-agent scenarios need streamable-HTTP MCP with API-key auth. This is also the natural hosted product's front door.
4. **Framework recipes** — runnable examples for Claude Code (memory MCP), LangGraph, CrewAI, Vercel AI SDK; each is a half-day of work and each is an adoption channel.
5. **Local/OpenAI-compatible providers** (§4) — "self-hosted memory that phones OpenAI" undercuts the positioning; this repeatedly surfaces as the top complaint about competing layers.
6. **Published benchmarks + the trust-model story** (§6) — numbers first, narrative second.
7. **Zero-to-memory time.** Quickstart is already good; add a single-container mode (Postgres embedded in the image or a documented `docker run` one-liner) and default-on rule-based everything so the first `remember`→`recall` round trip works with no API keys.
8. **Operational trust for the operator**: Prometheus `/metrics`, structured logs, OTel traces (write path spans: classify/embed/conflict). Currently there is no observability surface at all — self-hosters equate "can't observe" with "can't run in prod."

Non-goals worth restating (the design doc is right): don't chase LangChain-abstraction parity, don't build a notes UI, don't add storage backends. The wedge is *trustable, auditable, multi-agent-safe* memory — everything above serves that wedge.

---

## 8. Hosted service — long-term plan

The locked decisions (standalone service, Postgres-only, REST core, RLS foundational) are compatible with a hosted future; the work is sequencing. Guiding principle: **hosted = self-hosted + control plane**, one codebase, features flag-gated — never a fork.

### Phase H0 — "hosted-ready posture" (do in OSS now; it also hardens self-hosted)
- F1–F5 fixed: visibility/membership enforcement, `FORCE` RLS + non-owner app role, transaction-safe RLS context, O(1) key lookup. These are the tenant-isolation load-bearing walls; retrofitting them under live multi-tenant traffic is how leaks happen.
- **Usage metering events**: count requests, stored bytes, embedding tokens, LLM classification calls per tenant into a `usage_events` (or rolled-up `usage_daily`) table. Cheap now, impossible to backfill later, and immediately useful to self-hosters as stats.
- **Per-tenant rate limits** middleware (keyed by API key), tenant-configurable; protects self-hosted deployments from runaway agents today and becomes plan enforcement later.
- **Async job queue** in Postgres (`SKIP LOCKED`): embeddings, LLM classification refinement, promotion sweeps, retention. One worker container in compose. This removes third-party calls from the write path (latency + partial-failure coupling) and is the unit of horizontal scaling later.
- Observability (§7.8).

### Phase H1 — control plane (first paying tenants)
- **Control plane as a separate small service** (or module behind a flag): signup, tenant provisioning (create tenant + workspace + first key via the existing admin surface), plan limits, Stripe billing fed by the metering tables, minimal admin console (the deferred "admin list/update/delete + console" item belongs here, not in OSS core).
- **Data plane unchanged**: the OSS service, multi-tenant on one Postgres cluster, RLS-enforced (H0). Per-tenant `tenant_config` already carries policy; add plan-derived caps (max items, max recall budget, retention days).
- **Keys and secrets**: per-tenant BYO provider keys (their OpenAI/Anthropic keys for classification/embeddings) stored encrypted (pgcrypto envelope or external KMS) — this converts your largest marginal cost into the customer's, and many teams *require* their own keys for data-governance reasons.
- **Deletion + export**: GDPR erasure (§5) and self-service full-tenant export (extend the CCA exporter). Contractually required from the first paying customer; also the anti-lock-in story that makes adoption safe ("hosted Engram exports to self-hosted Engram losslessly" is a marketing weapon competitors can't copy).
- Managed backups with PITR; restore drills.

### Phase H2 — scale-out (when one cluster isn't enough)
- **Cell architecture**: multiple identical Postgres clusters ("cells"), tenant→cell routing in the control plane. No sharding *within* a tenant, no Citus, no storage abstraction — consistent with locked decision #2, because each cell is still plain Postgres+pgvector. Large tenants get dedicated cells (also the story for compliance/data-residency: pin a tenant's cell to a region).
- Read replicas per cell for recall/search once F18's read-path purity lands.
- SOC 2 posture: the deferred **sensitive-read audit logging** lands here (recall_logs already covers most of it — extend to item GETs of `sensitivity != 'normal'`), plus access reviews on the control plane.
- SLOs: recall p99 latency and working-set determinism become the published contract; the `scoring_version`/`config_version` audit fields make "explainable SLO" a differentiator ("we can tell you exactly why your agent saw what it saw on any date").

### What to decide early (cheap now, expensive later)
1. **Tenant ID in every event/log table** (F-item on `item_events`) — required for cell migration tooling.
2. **Embedding model registry** (F16) — hosted re-embedding at fleet scale needs it.
3. **Deletion semantics** (§5) — constrains backup retention contracts.
4. **API versioning discipline** — `/v1` is in paths already; write down the compatibility policy before OSS adoption creates de facto contracts.

---

## 9. Prioritized roadmap

| Priority | Item | Findings | Effort |
|---|---|---|---|
| **P0** | Shared visibility/membership predicate on all read paths + Postgres-backed isolation tests | F1, F2, F7 | M |
| **P0** | RLS: app role + `FORCE`, transaction-safe context, RLS-as-non-owner CI test | F3, F5 | M |
| **P0** | API key: key-id lookup, O(1) verify, principal cache | F4 | S |
| **P0** | ~~Fix supersede ordering; migrate item-touching tests to Postgres fixtures~~ **Done (ENG-AUD-006)** | F6, F7 | S–M |
| **P1** | ~~Classification → trust wiring: confidence blend, visibility (downward only), remove 0.7 floor; fix seed rules; skip semantics~~ **Done (ENG-AUD-005)** | F8, F9 | M |
| **P1** | Trust-weighted semantic ranking (`semantic-v2`); honor search filters; default budgets; skip-not-break packing; freshness in recency | F10, F14, F15 | M |
| **P1** | Promotion: lazy check on startup recall, dispute gate; conflict check at promotion time; top-k conflict candidates | F11, F12, F13 | M |
| **P1** | Async job queue (embeddings, LLM classify refine, promotion, retention); vocab caching — **done (ENG-AUD-008)** | F20 | M |
| **P1** | Recall eval harness over `recall_logs`+`feedback_events`; classification golden set + eval script | — | M |
| **P2** | Extraction endpoint (`/v1/extract`); provider abstraction (Anthropic, OpenAI-compatible, local embeddings) | — | M–L |
| **P2** | Embedding model registry + per-model storage; kind reference table replacing `chk_kind` | F16, F17 | M |
| **P2** | SQL-side scoring + async recall-count updates; `tenant_id` on `item_events`; retention jobs | F18 | M |
| **P2** | KG/tunnel 1-hop recall expansion (or correct the claim) | F19 | M |
| **P2** | TS SDK, HTTP MCP transport, framework recipes, metrics/tracing, published benchmarks | — | L |
| **P3** | Hosted H0 items not already covered (metering, rate limits); then H1 control plane per §8 | — | L |
| **P3** | Deletion/GDPR design + implementation; sensitive-read audit | — | M |

Effort: S ≈ a day, M ≈ days, L ≈ weeks.

### Documentation truth items (fold into the next docs pass)
- BL-003 "visibility-scoped" — remove until F1 lands.
- BL-004 "lazy check on startup recall" — not implemented; correct or implement.
- README roadmap layer 3 "relationship-aware recall — done" — not implemented.
- README "Row Level Security is enforced at the Postgres level" — qualify until F3 lands (owner bypass in default deployment).
- design.md §7 kinds `procedure`/`summary` vs. `chk_kind`.

---

## 10. Closing assessment

Engram's design documents describe the most complete *trust* model of any memory layer, open or commercial, and the implementation is much further along than typical for a project at this stage — with an audit culture (append-first, event logs, versioned scoring, honest docs) that is itself a durable advantage. The gap is that the trust model currently governs how memories are *written and reviewed* but not how they are *read*, and quality is asserted rather than measured. Close the read-path enforcement gap (P0), wire classification into the trust loop and trust into semantic recall (P1), and start publishing measured recall quality — and Engram has a legitimate claim to being the memory layer others get compared against. The hosted service then becomes an exercise in adding a control plane to an already-multi-tenant-safe core, rather than a security retrofit.
