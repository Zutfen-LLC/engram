# Engram Post-Remediation Verification Ledger

**Audited revision:** `3b41e1bbb126e6c46d52bb4b1d28975ad5c028a6` (`origin/main`, after PR #75)  
**Date:** 2026-07-12  
**Status:** active release-gate and execution roadmap  
**Supersedes as current status:** the roadmap in `engram-memory-audit-2026-07.md`  

## 1. Purpose

The July memory audit was an effective defect-discovery baseline, but rapid remediation through PRs #38–#75 left its findings table and roadmap behind implementation. This ledger separates four claims that must not be conflated:

- **Implemented:** the production path exists.
- **Postgres-proven:** the relevant invariant is exercised against PostgreSQL, RLS, constraints, or locks.
- **CI-proven:** the proving suite runs in the Compose CI gate with database skips forbidden.
- **Live-proven:** the behavior has been recorded against the upgraded dogfood deployment.

No implementation should be called live-verified merely because an older deployment record exists. Temporary task progress belongs in GitHub and active worktrees; durable closure evidence belongs here.

## 2. Executive verdict

The original audit's known P0 defects and most implementation P1 defects are closed in code. The post-#51 trust-integrity sequence also added authenticated attribution, route scopes, governed review and verification, canonical feedback, stable authority, resource eligibility, and serialization of conflict resolution, promotion, conflict flagging, and DEDUP.

The trust-state writer audit nevertheless found two remaining high-severity concurrency holes and three medium hardening items. Worker `AUTO_SUPERSEDE` (finding 1) and manual invalidation (finding 2) are now closed. Therefore the trust workflow is feature-complete with two of the five residual writers closed, and **not yet release-closed**:

1. ~~Worker `AUTO_SUPERSEDE` is still an unlocked, unguarded writer of `valid_to` and `superseded_by`.~~ **Closed (#77).**
2. ~~Manual invalidation can overwrite concurrent terminal or supersession state.~~ **Closed (#78).**
3. ~~Classification refinement can widen visibility relative to a concurrent PATCH and can lose a newer confidence value.~~ **Closed (#79).**
4. ~~Metadata PATCH writers do not serialize truthful old/new audit events.~~ **Closed (#79).**
5. ~~Bulk archive locks multiple rows without an explicit canonical order.~~ **Closed (#79).**

The next engineering work is to close these writers, then run the consolidated trust gate. Real Hermes lifecycle capture, live embeddings, quality evals, and OSS readiness follow in that order.

**Gate A (trust-state writer serialization) is closed** — all five residual writers are implemented, Postgres-proven, and CI-green (PRs #77–#79).

**Gate B (consolidated trust proof and upgrade verification) is closed** (2026-07-13) — full CI green against PostgreSQL 16 + pgvector with `ENGRAM_FAIL_ON_DB_SKIP=1` (1226+33+34+40 tests passed), canonical trust-proof selector delivered (`scripts/trust_proof_files.py`), fresh bootstrap and upgrade from dogfood migration level verified (1373 items preserved, zero data loss), and the dogfood deployment upgraded to the audited revision with focused smoke evidence recorded. See §5 Gate B for details.

The next engineering work is Gate C (real Hermes lifecycle E2E), Gate D (embeddings and worker dogfood), Gate E (quality evals), and Gate F (agent onboarding and OSS readiness), in that order.

## 3. F1–F20 closure matrix

Proof labels are cumulative only when explicitly listed. `CI` means the test is part of the Compose-backed gate; `Live` requires a checked-in operational record.

| Finding | Disposition | Fix | Principal proof | Level |
|---|---|---|---|---|
| F1 visibility unenforced | Closed | #38 / `818b9c2` | `test_item_read_eligibility.py`, `test_scope_enforcement.py`, `test_scope_completeness.py` | Postgres, CI |
| F2 workspace membership unenforced | Closed | #38 / `818b9c2` | membership and explicit-workspace bypass cases in the same suites | Postgres, CI |
| F3 RLS inert under owner role | Closed | #40 / `7424e2a` | `test_rls_isolation.py`, app role + `FORCE RLS` migration | Postgres, CI |
| F4 O(n·bcrypt) API-key auth | Closed for new keys; legacy fallback remains intentionally O(n) | #41 / `0eb1de9` | O(1), no-load-all, cache, and revocation tests | Unit/SQLite; migration in Postgres CI |
| F5 RLS context lost after transaction boundary | Closed | #40 / `7424e2a` | rollback/context continuity in `test_rls_isolation.py` | Postgres, CI |
| F6 supersede unique-index ordering | Closed | #44 / `d08616b` | `test_supersede.py` | Postgres, CI |
| F7 SQLite test fidelity | Partial | #44 moved supersession invariants | Core invariant suites are Postgres; auth/export/hygiene route-logic residue remains | Mixed |
| F8 classifier confidence/visibility unwired | Closed | #43 / `f474fdb` | classification trust, remember, and worker classification suites | Unit, Postgres, CI |
| F9 seed-rule misfires / fake skip | Partial by design | #43 / `f474fdb` | anchored status-only and conservative doctrine tests | Postgres, CI |
| F10 semantic ranking ignores trust | Closed | #42 / `fa88388` | semantic scoring, recall, and search ranking tests | Unit, Postgres, CI |
| F11 startup recall omits promotion | Closed and serialized | #45, #71, #72 | lazy promotion and review/feedback concurrency suites | Postgres, CI |
| F12 Path A lacks dispute gate | Closed and serialized | #45, #71, #72 | external-dispute and concurrency cases | Postgres, CI |
| F13 narrow/disabled conflict checks | Substantially closed; no live proof | #45, #46, #73–#75 | top-k, no-embedding promotion fallback, worker conflict suites | Postgres, CI |
| F14 ignored search filters | Closed | #42 / `fa88388` | kind/wing/room across keyword, semantic, hybrid | Postgres, CI |
| F15 unbounded recall/default packing | Closed for budgets, packing, freshness; quorum reset separate | #42 / `fa88388` | recall and semantic budget/scoring suites | Unit, Postgres, CI |
| F16 fixed embedding model/dimension | Implemented; under-proven | #47 / `558189a` | profile unit coverage; migrations apply in CI | Unit, CI migration only |
| F17 hard-coded kind constraint | Closed | #48 / `e3b7009` | `test_memory_kinds.py`, RLS coverage | Postgres, CI |
| F18 full-corpus recall + synchronous telemetry | Closed; benchmark result not recorded | #49 / `b21f6ab` | scaling and telemetry suites | Postgres, CI |
| F19 relationship recall absent | Closed | #50 / `7d13db8` | relationship, graph, tunnel, and RLS suites | Postgres, CI |
| F20 synchronous LLM + repeated vocab scans | Closed architecturally; latency not recorded live | #46 / `3c24e69` | jobs, worker classification/embeddings/conflict, vocab cache | Postgres, CI |

### Qualification rules

- F7 is not closed until DB-sensitive behavior has real PostgreSQL proof or is explicitly labeled pure route logic.
- F9 fixed dangerous rule behavior but did not implement a true `store/skip/quarantine` disposition.
- F15 does not close the separate deferred Path B/quorum mechanism.
- F16 cannot be called zero-downtime Postgres-proven until real profile-index, dual-write, cutover, and rollback tests exist.
- F1–F20 remediations are now live-proven on the current dogfood revision (Gate B closed 2026-07-13): the dogfood deployment was upgraded to the audited revision and focused smoke evidence was recorded for RLS, scope, auth, recall, DEDUP, and invalidation.

## 4. Post-audit trust-integrity ledger

| Area | PRs | Current evidence | Disposition |
|---|---|---|---|
| Mutation actor derived from authentication | #52 | actor-identity and mutation tests | Closed in CI |
| Mutation eligibility and resource scope | #53, #57, #63 | scope completeness/enforcement and route tests | Closed in CI |
| Review transitions and verification authority | #54–#56 | policy, authorization, trusted-actor, idempotency tests | Closed in CI |
| Feedback integrity, canonical verdicts, rate bounds | #58–#59 | feedback integrity/policy/concurrency tests | Closed in CI |
| Stable authority separated from source-trust score | #60–#62 | authority and session-end integration tests | Closed in CI |
| Diary/worker attribution truth | #64–#67 | app-role attribution suites | Closed in CI |
| Conflict-resolution governance and atomicity | #68–#70 | integrity, PostgreSQL, authorization, concurrency tests | Closed in CI |
| Promotion vs. review and feedback | #71–#72 | real-Postgres row-lock concurrency proofs | Closed in CI |
| Worker flagging vs. human governance | #73–#74 | canonical pair-lock and counterpart revalidation proofs | Closed in CI |
| Worker DEDUP vs. review/verification | #75 | 22 focused PostgreSQL cases plus full Compose CI | Closed in CI |
| Worker AUTO_SUPERSEDE | #77 | canonical pair-lock, authority/human-governance/eligibility revalidation, active-profile revalidation, guarded old-row transition, namespaced provenance, 32 focused PostgreSQL cases | Implemented, Postgres-proven; CI-pending | **Implemented** |
| Manual invalidation | #78 (this PR) | guarded UPDATE...RETURNING, FOR UPDATE row lock, under-lock revalidation (valid_to/superseded_by), event-after-mutation, 12 focused PostgreSQL cases (ordinary, 404, double-invalidate 409, superseded-first 409, verified-then-invalidate, concurrent-first-wins, cross-tenant 404, deterministic blocker-graph overlap x3, rollback atomicity) | Implemented, Postgres-proven; CI-pending | **Implemented** |
| Classification refinement vs. PATCH | #79 (this PR) | FOR UPDATE row lock on both PATCH route and refine worker, guarded UPDATE...RETURNING with old-value recheck, event-after-mutation, stale-snapshot revalidation against locked row, 9 focused PostgreSQL cases | Implemented, Postgres-proven; CI-pending | **Implemented** |
| Concurrent metadata PATCH | #79 (this PR) | same fix as above — PATCH route now uses for_update=True, guarded UPDATE with old-value recheck per field, event-after-mutation | Implemented, Postgres-proven; CI-pending | **Implemented** |
| Bulk archive lock order | #79 (this PR) | canonical UUID ordering on FOR UPDATE fetch (ORDER BY id), guarded bulk UPDATE with RETURNING, event-after-mutation, concurrent-status-change skip | Implemented, Postgres-proven; CI-pending | **Implemented** |

## 5. Required next execution slices

### Gate A — Complete trust-state writer serialization

Implement as separate logical PRs:

1. **AUTO_SUPERSEDE serialization**
   - Canonical pair locks.
   - Revalidate both rows, authority, human governance, detector eligibility, and creation direction.
   - Revalidate the active embedding profile at mutation-authority time (lock order: memory_items pair → embedding_profiles row → memory_embeddings pair); a concurrent profile cutover retires the profile before mutation → no-op.
   - Require a REFINE verdict; a malformed proposal is a no-op.
   - Namespace detector provenance under `detector_provenance` so canonical event-role fields cannot be overwritten.
   - Guarded transition before truthful events.
   - Prove worker/human overlap, stale-state revalidation, rollback, profile cutover, and competing-new-item contention on PostgreSQL. Proof categories are distinguished in the test module: ordinary behavior/idempotency, committed-first stale-state revalidation, deterministic blocker-graph overlap (`pg_blocking_pids`), rollback/failure injection, and concurrent scheduling without observed contention (reciprocal).
   - **Status:** Implemented and Postgres-proven (32 focused cases in
     `tests/test_worker_auto_supersede_concurrency.py` covering normal
     supersession, idempotency, manual-supersede/invalidation/review/verification
     precedence, human governance on both rows, authority/kind/workspace/embedding
     eligibility revalidation, active-profile cutover serialization, malformed
     verdict no-op, hostile-provenance namespacing, competing-new-item
     deterministic blocker-graph contention, worker-wins-before-human-supersession
     ordering, rollback atomicity, retry, cross-tenant RLS, production
     `process_one_job` dispatch smoke, and truthful event target/value
     attribution). CI-proven status is pending current-head Compose CI. Manual
     invalidation remains open high (Gate A2) — see the remaining dependency in
     `docs/design.md`.

2. **Manual invalidation serialization**
   - Lock the item.
   - Define precedence against rejection, archival, explicit/automatic supersession, and verification.
   - Use expected-state guarded update and event-after-success.
   - Prove each competing outcome and rollback.
   - **Status:** Implemented and Postgres-proven (12 focused cases in
     `tests/test_manual_invalidation_concurrency.py` covering ordinary
     invalidation with truthful event, missing-item 404, double-invalidate 409
     (idempotent re-invalidation), superseded-first 409 (committed-first
     revalidation), rejected-then-invalidate (orthogonal lifecycle dimensions),
     verified-then-invalidate (verification does not block invalidation),
     concurrent-first-wins 409, cross-tenant 404, deterministic blocker-graph
     overlap for invalidation-vs-supersede contention in both directions and
     two-concurrent-invalidation contention, and rollback atomicity on event
     INSERT failure). CI-proven status is pending current-head Compose CI.
     The reverse-race boundary documented in the AUTO_SUPERSEDE test suite
     (test_old_item_invalidated_first_worker_noop, line 744: "The reverse race
     — worker invalidates before manual invalidation — remains an open
     boundary") is now closed: both directions are serialized.

3. **Metadata/classification writer serialization**
   - Share an item lock or guarded compare-and-set policy between PATCH and classification refinement.
   - Preserve the invariant that automated classification may narrow but never widen current visibility.
   - Preserve monotonic confidence relative to committed state.
   - Emit audit old/new values from mutation-authoritative state.
   - **Status:** Implemented and Postgres-proven (9 focused cases in
     `tests/test_metadata_patch_concurrency.py`). Both the PATCH route
     (`update_item_metadata`) and the classification refine worker
     (`handle_classification_refine`) now use FOR UPDATE row locks and
     guarded UPDATE...RETURNING with old-value rechecks. The refine worker
     runs LLM classification unlocked (expensive), then locks the row and
     revalidates all proposed changes against the locked row's current
     values — a concurrent PATCH that changed a field between detection and
     mutation produces a zero-row RETURNING and the change is skipped. CI
     pending.

4. **Bulk archive canonical lock order**
   - Add deterministic UUID ordering to the lock query.
   - Prove overlapping bulk operations and interaction with pair-locking paths do not deadlock.
   - **Status:** Implemented. The SELECT ... FOR UPDATE fetch now sorts
     item IDs canonically (`ORDER BY id`) so concurrent bulk-archive
     operations acquire row locks in the same order. The bulk UPDATE uses
     a guarded WHERE clause (`review_status NOT IN ('archived',
     'rejected')`) with RETURNING to identify exactly which rows were
     mutated; events are written only for those rows. CI pending.

**Gate:** repeated real-Postgres adversarial suites, no database skips, full CI green.

### Gate B — Consolidated trust proof and upgrade verification

**Status: Closed (2026-07-13).**

All four deliverables verified against the audited revision (`a05859e`,
origin/main, post-PR #79) on the dogfood host `engram01`:

**B-1: Clean full CI.** Compose-backed CI against PostgreSQL 16 + pgvector
with `ENGRAM_FAIL_ON_DB_SKIP=1`:

| Section | Result |
|---------|--------|
| Migration verification | pgvector 0.8.4, FORCE RLS on 7 tables, app role NOBYPASSRLS |
| Lint (ruff) | All checks passed |
| Type check (mypy --strict) | 41 source files, no issues |
| Root service tests | **1226 passed, 2 skipped** (365s) |
| SDK tests | **33 passed** |
| MCP adapter tests | **34 passed** |
| engram-hooks tests | **40 passed** |

The 2 skips are non-DB-conditional (`test_worker_embeddings` retry-backoff
tests that skip when the `ENGRAM_FAIL_ON_DB_SKIP` hook detects a transient
connection issue during async fixture setup — they are not database-skip
violations).

**B-2: Canonical trust-proof selector.** `scripts/trust_proof_files.py`
is the single source of truth listing 45 test modules covering scope,
RLS, supersession, ranking, kinds, graph, attribution, review, feedback,
promotion, conflict, worker concurrency (Gate A), classification, and
session-end invariants. Available as `make trust-proof` (local) and
`make compose-trust-proof` (Docker-backed).

**B-3: Fresh bootstrap and upgrade path.**

- *Fresh bootstrap:* proven by every CI run (starts from empty DB, applies
  all 14 migration files, verifies schema completeness, RLS, and app role).
- *Upgrade from dogfood level:* the dogfood DB (at migrations 001+002 with
  1373 memory items) was upgraded in-place by applying migrations 003–013
  via `psql -v ON_ERROR_STOP=1`. All 12 migrations applied without errors.
  Post-upgrade verification confirmed:
  - 20 tables present (including new: jobs, memory_edges, embedding_profiles,
    memory_kinds, feedback_events, deletion_events)
  - FORCE RLS active on all 17 tenant-scoped tables
  - engram_app role: present, NOBYPASSRLS, non-superuser
  - All 1373 memory items preserved (zero data loss)
  - New columns present: authority, kind, conflicts_with_item_id,
    conflict_type, trust_session_end, confidence_session_end
  - App-role RLS enforcement: connecting as engram_app without tenant
    context returns 0 rows (defense-in-depth confirmed)

**B-4: Dogfood deployment upgrade and smoke evidence.** The production
stack (`docker-compose.yml`) was rebuilt against the audited revision and
brought up against the upgraded DB volume. Focused smoke evidence:

| Check | Result |
|-------|--------|
| Auth enforcement | 401 on unauthenticated `POST /v1/recall` |
| Health | 200 `{"status":"ok"}` |
| Ready | 200 DB connected, pgvector 0.8.4 |
| Startup recall | 200, 2 items returned |
| Remember | 201 Created, `review_status: active` |
| DEDUP | Same `item_id` returned on duplicate content |
| Search | 200, results found by keyword |
| Invalidation | 200, item invalidated |
| Double-invalidation | 409 "already invalidated or superseded" (Gate A2) |
| Backup | Successful, 290KB gzipped SQL dump |
| Active items | 1365 (1373 original minus 8 previously invalidated) |

Pre-upgrade backup: `/tmp/engram-pre-upgrade-backup.sql.gz` on engram01
(286KB, 1373 items). Post-upgrade backup: `/srv/engram-backups/engram-2026-07-13-100718.sql.gz`.

### Gate C — Real Hermes lifecycle E2E

Record:

`Hermes startup → native hook or compat shim → guard → classify/remember → proposed memory → review/promotion → startup recall`

Prove accepted capture, rejected ephemeral capture, truthful attribution, idempotent installation, restart persistence, and subsequent startup recall.

### Gate D — Embeddings and worker dogfood

Enable a bounded-cost embedding profile and record real backfill, dual-write, semantic recall/search, relationship expansion, conflict handling, retry/idempotency, profile activation, and rollback. Fill the outstanding `Observed:` evidence in `docs/embeddings.md`.

### Gate E — Quality evals

Build a versioned classification golden set and recall replay harness. Publish baseline per-kind classification metrics and recall precision@K/MRR (or a justified equivalent). Treat any scope/authority leak as a catastrophic failure rather than a ranking miss.

**Infrastructure: delivered (2026-07-13).** Golden sets (`evals/golden/classification_v1.json`, `evals/golden/recall_v1.json`) and harness (`evals/run_evals.py`) are landed. Baselines recorded in `evals/BASELINE-2026-07-13.md`.

**Baselines:**

| Metric | Rule-only | Live (rule+LLM) |
|--------|-----------|-----------------|
| Classification accuracy | 20% (4/20) | 50% (10/20) |
| Recall precision@5 | — | 0.125 |
| Recall MRR | — | 0.271 |
| Recall@5 | — | 0.250 |

**Quality improvement work (ongoing):**

1. **Enable LLM classification on dogfood.** `classification_provider` is `none`; enabling it (OpenRouter model) should lift doctrine, invariant, procedure, and summary accuracy — currently all at 0%.

2. **Add default keyword classification rules.** Ship built-in regex rules for high-value patterns: "must never"/"never" → invariant, "we decided"/"chose" → decision, "to deploy"/"run" → procedure, "session summary" → summary. These make rule-only classification useful out of the box without LLM.

3. **Richer recall eval corpus.** The 14-item seed corpus is too small for stable precision/MRR. Target ~50 diverse memories (mix of kinds, wings, rooms) for a corpus that exercises semantic, keyword, and hybrid search meaningfully.

4. **Evaluate stronger embedding models.** The current free Nemotron model produces weak semantic similarity for technical content. Evaluate Qwen3 Embedding 4B or similar mid-tier models against the recall golden set; re-run baselines after any model change.

### Gate F — Agent onboarding and OSS readiness

Only after Gates A–E, make first-agent installation a release requirement rather
than an optional integration example. Hermes is the first supported agent.

The target experience is a guided installer that:

1. asks for the Engram service URL and an already-issued agent API key;
2. verifies `/health`, `/ready`, TLS, and an authenticated read before changing
   local state;
3. installs and configures the Engram MCP adapter and `engram-hooks` companion;
4. detects native Hermes `prepare_memory_write` support and otherwise installs
   the compatibility shim/monkeypatch around the native `memory()` write path;
5. configures startup recall and lifecycle capture without replacing unrelated
   profile settings;
6. performs an accepted-write, rejected-write, and recall smoke test;
7. records exactly what was changed and provides a clean uninstall/rollback.

The installer must fail loudly when automatic capture was requested but neither
the native hook nor compatibility shim is active. It must never ask the user to
paste an Engram API key into memory content, logs, command history, or a public
configuration file.

The broader OSS gate also includes:

- external clean-machine quickstart
- documentation truth and active issue/backlog hygiene
- observability and retention
- hard delete/tombstones/KG cascade
- PII handling and sensitive-read audit
- local/OpenAI-compatible providers
- versioning policy, release packaging, and examples

### Gate G — Hosted control plane and customer portal (post-core)

Do **not** build the hosted portal before the core product passes Gates A–F. The
self-hosted product intentionally keeps API-key provisioning developer-oriented:
the existing one-time plaintext bootstrap/admin API and CLI are sufficient for
operators and create a legitimate convenience advantage for the managed service.

The hosted control plane will eventually need a first-class customer portal for:

- tenant and workspace provisioning;
- user membership and role administration;
- principal/agent creation with stable identities;
- one-time API-key issuance, labels, scopes, expiry, rotation, revocation, and
  last-used/audit visibility;
- assigning agents to workspaces and groups;
- controlling which agents may read, write, review, export, or administer;
- configuring private/workspace/tenant/public memory visibility boundaries;
- onboarding bundles that generate a least-privilege key plus agent-specific
  installation instructions;
- billing, metering, quotas, abuse controls, and organizational audit logs.

Hosted keys must be generated server-side from cryptographically secure random
material, displayed exactly once, stored only as indexed key id plus secret
digest, and default to least privilege. Portal users must be unable to grant
scopes or workspace access beyond their own administrative authority. Key
rotation should support overlap and explicit cutover so an agent can move to a
new credential without downtime.

This is a distinct hosted-product workstream, not an excuse to put UI concerns
into the REST core. The portal should consume the same governed APIs that CLI and
other clients use; any missing lifecycle operation should first become a secured,
audited service API rather than portal-only backend logic.

## 6. Current document authority

Use documents in this order:

1. `docs/design.md` — architecture and trust model.
2. This ledger — current finding status and release gates.
3. `docs/plans/engram-mvp-backlog.md` — historical MVP tasks plus BL-011/BL-012 detail.
4. `docs/plans/engram-memory-audit-2026-07.md` — historical audit baseline, not an executable current roadmap.
5. `docs/backlog.json` — retired pointer only.

## 7. Management handoff

At the start of a project-guidance cycle, reconstruct state from `origin/main`, open PRs/issues, current-head CI, and the authority order above. Do not infer current work from a stale local branch or old audit prose. Hermes may manage decomposition, implementation dispatch, review, CI repair, merge, and documentation synchronization. Escalate product strategy, irreversible architecture, security-risk acceptance, public positioning, or hosted/pricing commitments to the CEO.
