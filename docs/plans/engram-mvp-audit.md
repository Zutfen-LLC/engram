# Engram MVP Audit

**Date:** 2026-07-07
**Auditor:** reality-based MVP audit (implementation completeness, docs drift, ops readiness, test coverage, product readiness)
**Audited state:** `main` @ `ee424a7` (identical to audit branch base)
**Scope note:** Security/provenance/trust-model critique is handled by a separate audit. This document flags one RLS enforcement observation for handoff but does not pursue it.

---

## Executive summary

- **The service is dramatically further along than every planning document claims.** All 18 build tasks (T01–T18) are merged. The backlog's `current_state` says "No endpoint is functional yet" — in reality all 38 designed routes exist and work.
- **Verified in this audit by standing the stack up from scratch:** fresh Postgres 16 + pgvector 0.8, `001_init.sql` applied cleanly, service booted, and **all 126 tests pass** against the real database. Core flows (remember → recall → search → taxonomy → export → review stats) verified live by hand. Auth-enabled mode verified live (401 without key, health exempt, 201 with a real bcrypt-hashed key).
- **CI is a false green.** 72 of 126 tests silently skip when no Postgres is present, and CI runs no Postgres. Every DB-touching integration test (remember, search, conflicts, KG, taxonomy, classification) is unverified on every PR. SDK tests (22, all passing locally) and both adapters are entirely outside CI.
- **SDK, MCP adapter, and engram-hooks are real implementations, not scaffolds.** `sdk/engram-client` is a complete async client with 22 passing tests. `adapters/mcp-server` is a complete FastMCP server exposing 18 `engram_*` tools. `adapters/engram-hooks` is ~1,200 lines of implemented Hermes lifecycle hooks. The adapters have **zero tests**.
- **One real cross-component bug found:** the API, SDK, and MCP adapter all declare sensitivity `"confidential"`, but the DB CHECK constraint (and design.md) says `"restricted"`. Writing `sensitivity="confidential"` returns a raw **500 Internal Server Error**. Every client component advertises a value the server cannot store.
- **Two designed behaviors are silently absent:** semantic recall (`POST /v1/recall mode=semantic` returns 422 "not yet implemented" — yet SDK and MCP advertise the mode) and auto-promotion (design and README say "on by default"; the `tenant_config` columns exist but no code consumes them — proposed items never promote).
- **Deployment (T19) has no repo evidence.** No `deploy/`, no `docs/deployment.md`, no backup script, no `.env.example` (the README quickstart's first command fails), `engram init-db` is a print stub. No production instance was reachable from the audit sandbox; nothing in the repo records where or whether one runs.
- **Trust differentiators are inert at default config.** `ENGRAM_EMBEDDING_PROVIDER=none` is the default (and the compose default), which disables semantic search and write-time conflict detection. Pending embedding placeholder rows are created but nothing ever backfills them. The OpenAI paths (embeddings, LLM classification) have never been exercised live.
- **Overall verdict:** Engram is a near-complete, well-tested (locally) Phase 1A–1C+M2 implementation wearing a "nothing works yet" label. Finishing MVP is not a build project — it is a **close-the-gaps project**: one enum bug, two missing designed behaviors, CI that actually tests, a reproducible deployment, and honest docs.

---

## Review summary

**Highest-priority findings**

1. **Sensitivity enum drift → 500s** (`engram/api/routes/memory.py:53`, `sdk/engram-client/engram_client/models.py:20`, `adapters/mcp-server/engram_mcp/server.py:30` say `confidential`; `migrations/001_init.sql:148` says `restricted`). Confirmed live: request with `"confidential"` → 500.
2. **CI never runs the integration suite** (72/126 tests skip without a DB; CI has no DB; SDK/adapters not collected — `testpaths = ["tests"]`).
3. **Auto-promotion is not implemented** despite README/design claiming it is on by default. Once agent writers (hooks) come online, the proposed queue freezes exactly as design §3 warns.
4. **Semantic recall not implemented** but advertised by SDK/MCP type signatures — clients can request a mode that 422s.
5. **Deployment is not reproducible from the repo** — missing `.env.example`, deployment docs, backup, bootstrap-key flow; `init-db` is a stub; the compose header comment gives wrong migration instructions (migrations actually auto-apply via the `docker-entrypoint-initdb.d` mount, first boot only).
6. **Handoff to security audit:** the schema never uses `FORCE ROW LEVEL SECURITY`, and docker-compose connects the app as the table-owner role — Postgres exempts table owners from RLS, so the documented "RLS enforces isolation even when application code is wrong" guarantee does not currently bind the app's own connections. Not pursued here; flagged for the security audit.

**Immediate recommendation:** Fix the enum bug and CI first (they protect everything else), then implement the two missing designed behaviors, then make deployment reproducible and deploy for real dogfooding, then rewrite the stale docs. Do not build anything new beyond that.

**Top blockers to MVP**

| # | Blocker | Type |
|---|---------|------|
| 1 | Sensitivity enum drift + 500-on-constraint-violation | bug |
| 2 | CI green without exercising the API | verification gap |
| 3 | Auto-promotion absent | missing designed behavior |
| 4 | Semantic recall absent but advertised | missing designed behavior / client drift |
| 5 | No reproducible deployment path (T19 unstarted) | ops gap |
| 6 | Embeddings/conflict detection inert by default; no backfill; OpenAI path never exercised | product readiness |

---

## Current-state matrix

Status vocabulary: **absent** / **stubbed** / **partially implemented** / **implemented but unverified** / **implemented and tested** / **live and apparently working** ("live" here = verified against a from-scratch stand-up of `main` during this audit; no remote production instance was reachable).

| Capability | Repo status | Test status | Live status | Docs status | MVP relevance | Notes |
|---|---|---|---|---|---|---|
| Health/readiness | implemented and tested | 2 tests (ready needs DB) | live and working | accurate | in MVP (done) | `/ready` proves DB + RLS session context |
| Remember (write path) | implemented and tested | 19 tests | live and working | README "Status" says not done — **stale** | in MVP (done) | Trust defaults read from `tenant_config`; dedup, supersession, secret denylist all verified |
| Recall (startup) | implemented and tested | 6+8 tests | live and working | accurate | in MVP (done) | Scoring, pinned bypass, reasons, recall_logs, penalty all present |
| Recall (semantic mode) | **absent** | none | 422 "not yet implemented" | design §3/§9 describe it as core; SDK/MCP advertise it | **in MVP — gap** | `memory.py:718` hard-rejects; overlaps `/v1/search` machinery, thin to add |
| Search (keyword/semantic/hybrid) | implemented and tested | 5 tests | live and working (semantic verified only with pgvector 0.8) | accurate | in MVP (done) | Requires pgvector ≥0.8 (`hnsw.iterative_scan`) — errors on 0.6; compose image satisfies this |
| Classification (rules + LLM) | rules: implemented and tested; LLM path: implemented but unverified | 4 tests (rule path) | rules CRUD + classify live | accurate | rules in MVP (done); LLM verify in MVP | OpenAI path never exercised live; default provider `none` |
| Items CRUD + PATCH audit | implemented and tested | 3 + shared tests | live and working | accurate | in MVP (done) | Cursor pagination, item_events audit verified |
| Review/verify/invalidate/supersede | implemented and tested | in test_items/test_conflicts | live and working | accurate | in MVP (done) | |
| Conflict detection/resolution | implemented and tested | 15 tests | live (requires embeddings to be active) | accurate | in MVP (done, but inert at default config) | Skipped when `embedding_provider=none` — i.e., in every default deployment |
| Auto-promotion (Path A age/confidence, Path B quorum) | **absent** (config columns exist, nothing consumes them) | none | proposed items never promote | README/design claim "on by default" — **wrong** | **in MVP — gap** (Path A); Path B post-MVP | `models.py:383-385` columns are dead weight today |
| Feedback endpoint | implemented and tested | 8 tests | live and working | accurate | in MVP (done) | Authority weighting per design |
| Taxonomy / tunnels | implemented and tested | 12 tests | live and working | accurate | in MVP (done) | |
| Knowledge graph | implemented and tested | 13 tests | live and working | accurate | in MVP (done) | Visibility inheritance + auto-backing item implemented |
| Diary | implemented and tested | in test_taxonomy | live and working | accurate | in MVP (done) | |
| Memory hygiene (stale/bulk-archive/stats) | implemented and tested | 5 tests | live and working | accurate | in MVP (done) | |
| Export (CCA) + importers | implemented and tested; importers implemented (CCA importer tested, MemPalace dry-run verified per PR record) | 7 tests | export live | `scripts/README.md` says both importers are **TODO** — **stale** | in MVP (done) | |
| Hard delete + deletion_events tombstones | **absent** | none | no `DELETE /v1/items/{id}` route | design §11 describes it | post-MVP | `deletion_events` table exists, entirely unused |
| Auth / API keys / admin | implemented and tested | 17 tests (unit-level, run without DB) | **live-verified in this audit** (401/exempt-health/201 with real key) | accurate | in MVP (done, minus bootstrap flow) | No bootstrap path: first key requires hand-written SQL; admin endpoints exist but no key exists to call them with |
| Tenant/workspace/principal admin | implemented and tested | in test_auth | live | accurate | in MVP (done) | Create-only; no list/update/delete for tenants/workspaces — acceptable for MVP |
| Embeddings pipeline | partially implemented | indirect | placeholder rows written; **no backfill worker/CLI**; OpenAI call unverified | design implies re-embedding support | in MVP — gap (backfill + live verify) | Items written under `provider=none` stay pending forever |
| SDK (`sdk/engram-client`) | **implemented and tested** — real, not scaffold | 22 tests, all pass (not in CI) | n/a | matches API except sensitivity enum + semantic recall | in MVP (done, needs enum fix + CI wiring) | Async httpx client, context manager, typed models |
| MCP adapter (`adapters/mcp-server`) | **implemented but unverified** — real, not scaffold | **zero tests** | not exercised | README matches code | in MVP (verification only) | 18 `engram_*` FastMCP tools over the SDK; stdio transport |
| engram-hooks (`adapters/engram-hooks`) | **implemented but unverified** — real, not scaffold (~1,200 lines: hooks, guards, volatile store, config, `prepare_memory_write` shim) | **zero tests** | not exercised; depends on pending upstream Hermes PR #59898 | README matches code | **post-MVP** (verification/integration) | Cannot be end-to-end verified without a Hermes runtime |
| Deployment/runtime (T19) | **absent** | n/a | no reachable production instance; no repo evidence one exists | compose header comment wrong; README quickstart references missing `.env.example` | **in MVP — gap** | Dockerfile + compose are sound; everything around them (env example, docs, backup, bootstrap, upgrade story) is missing |
| CI | partially implemented | — | — | backlog claims 4 CI gates incl. compose-validate (true) | in MVP — gap | No DB service ⇒ 72/126 tests skip; SDK/adapters not covered |

---

## Repo/live/docs drift

**Stale docs (docs claim less than exists):**

- `README.md` Status section: "Phase 1A in development", with "Functional endpoints", "CCA import", "Python SDK" unchecked. All are merged and passing tests. Phases 1B, 1C, and M2 code are also merged.
- `docs/backlog.json` `current_state`: "No endpoint is functional yet." All 18 build tasks merged via PRs #1–#20. The entire task list is retrospective fiction as a to-do list.
- `scripts/README.md`: labels both importers "(TODO)". Both exist (`import_cca.py`, `import_mempalace.py`); the MemPalace importer was dry-run-verified against a real palace (205 drawers) per the merged PR record.
- `docker-compose.yml` header comment: "Apply migrations: docker compose exec engram-service psql ..." — wrong; migrations auto-apply via the `./migrations:/docker-entrypoint-initdb.d` mount on first boot of an empty volume.

**Stale docs (docs claim more than exists):**

- README "Auto-promotion (on by default)" and design §3 auto-promotion policy: **not implemented**. Nothing promotes proposed items.
- design §3/§9 semantic recall (incl. "semantic recall includes proposed with warnings"): **not implemented**; `mode=semantic` → 422.
- design §11 hard-delete + `deletion_events` tombstones + cascade: **not implemented** (no DELETE route; table unused).
- design §11 "PII-risk classification" and "read audit: sensitive reads logged": not implemented (PII appears only in a docstring). Marked optional in design; fine to defer, but the doc should say so.
- README quickstart step 3: `cp .env.example .env` — **`.env.example` does not exist**. First command an operator runs fails.

**Client/server drift:**

- Sensitivity: API/SDK/MCP say `normal|sensitive|confidential`; DB CHECK and design say `normal|sensitive|restricted`. `"confidential"` → 500.
- Recall mode: SDK and MCP expose `Literal["startup","semantic"]`; server rejects `semantic`.

**Repo-only behavior (never verified anywhere):**

- OpenAI embedding generation and LLM classification/conflict-classifier paths (all tests run with `provider=none` or pre-inserted vectors).
- MCP adapter and engram-hooks (no tests, no CI, no recorded manual run).

**Parity:**

- `origin/main` == audited working tree; there is no divergent deployed branch **in the repo**. Whether a deployed instance exists at all is unrecorded — the audit sandbox had no route to any production host (no Tailscale), and the repo contains no deployment artifacts, URLs, or runbooks. **Live-vs-repo parity is unknowable from the repo, which is itself the finding.** The local from-scratch stand-up of `main` behaved correctly on every probed route.
- pgvector version is a real parity hazard: on 0.6 the service starts fine but every semantic query 500s (`invalid configuration parameter "hnsw.iterative_scan"`, reproduced in this audit). The pinned compose image (`pgvector/pgvector:pg16`) carries 0.8+, but any operator running their own Postgres must know the floor. `/ready` does not check it.

---

## MVP definition

**MVP = "another operator (or our own fleet) can stand Engram up from the repo, trust CI, write/recall/search memories through the REST API and MCP tools with the trust machinery actually active, and not hit a 500 on any documented input."**

### In MVP

| Item | Rationale |
|---|---|
| All currently-merged capability endpoints (remember, recall-startup, search, items, review, conflicts, feedback, KG, taxonomy, tunnels, diary, hygiene, export, admin, auth) | Already done and tested. Do not re-backlog. |
| Fix sensitivity enum drift + map constraint violations to 422 | Confirmed 500 on documented input, across all three client surfaces. |
| Semantic recall (`mode=semantic`) | Designed core behavior (proposed-item rediscovery is part of the trust story); already advertised by SDK/MCP; thin layer over existing search machinery. Cheaper to finish than to descope across four components + design doc. |
| Auto-promotion Path A (lazy check, confidence+age) | Without it the review pipeline dead-ends; README already promises it. Path A alone unblocks the design's Phase-1A story. |
| CI with a real Postgres (pgvector 0.8) service container; SDK tests and adapter lint/type in CI | The single biggest verification gap. Everything else in MVP is unguarded without it. |
| Embedding backfill CLI + one recorded live verification of the OpenAI embedding + classification path | The differentiators (semantic search, conflict detection) are inert until embeddings flow; pending rows currently never resolve. |
| Deployment completion: `.env.example`, `docs/deployment.md`, bootstrap API-key flow, backup script, real `init-db`, service healthcheck, pgvector-version check in `/ready` | An operator cannot currently stand it up from the README, create a first key without hand-written SQL, or back it up. This is T19, rescoped to repo artifacts + one real deployment. |
| MCP adapter smoke verification (against a running Engram) + minimal tests | MCP is the actual dogfooding interface for agents; the code exists, only verification is missing. |
| Docs truth pass (README status, scripts/README, compose comment, design deltas) | Documentation drift is this audit's reason for existing; leaving it re-poisons the next agent. |

### Not required for MVP

| Item | Rationale |
|---|---|
| Auto-promotion Path B (usage quorum) | Needs multi-agent feedback traffic to mean anything; Path A covers the freeze risk. |
| Hard delete + deletion_events + cascade | GDPR/hosted concern; self-hosted dogfooding can live without physical deletion. Table is ready when needed. |
| PII-risk classification, sensitive-read audit | Marked optional in design; no dogfooding dependency. |
| Admin list/update/delete surfaces, admin console | Create-only admin + SQL is workable for a single-tenant dogfood. Phase 3 material. |
| Multi-way conflict join table | Documented v1 limitation; keep it documented. |
| Local embedding provider, re-embedding migration tooling beyond backfill | No current demand. |

### Post-MVP integration work

| Item | Rationale |
|---|---|
| **engram-hooks / Hermes integration** | The library is written but unverifiable without a Hermes runtime and blocked on upstream PR #59898 (`prepare_memory_write`). Dogfooding can start with MCP tools alone — agents can remember/recall explicitly before lifecycle hooks automate it. Verification, tests, and a real Hermes wiring session are the post-MVP work; **building it is not, because it is built.** |
| MemPalace + CCA production import runs | Importers exist and are dry-run verified; the actual apply-mode migration is an operational event to schedule after the MVP deployment is up, not engineering work. |
| Open-source readiness (Phase 3: examples, quickstarts, auth hardening, Helm) | Explicitly future. |

### The decisive calls (MCP / SDK / Hermes hooks)

- **SDK: in MVP, and effectively done.** It exists, it's tested, it needs the enum fix and CI membership. Not a build item.
- **MCP adapter: in MVP, as verification-only.** It is the dogfooding interface (this is how agents will actually call Engram), it is fully written, and the remaining cost is a smoke test plus a handful of tests. Excluding it would save almost nothing and would leave MVP without a consumer.
- **Hermes hooks: post-MVP.** Necessary for *automated* memory capture, not for *dogfooding viability* — explicit MCP-driven remember/recall dogfoods the product's trust surface fine. It also has an unmerged upstream dependency that Engram cannot control. Ship MVP without it; verify it as the first post-MVP integration.

---

## Existing backlog triage

`docs/backlog.json` (19 tasks, 5 milestones):

| Item | Verdict | Rationale |
|---|---|---|
| T01 schema + health verification | **delete as already done** | Verified this audit: migration applies cleanly from scratch; health/ready live; seeds present. |
| T02 canonicalize/hash | **delete as already done** | Merged, 10 tests pass. |
| T03 remember write path | **delete as already done** | Merged, 19 tests pass; tenant_config-driven defaults confirmed in code and live. |
| T04 recall startup | **delete as already done** | Merged; scoring/pinned/reasons/logs verified live. |
| T05 embeddings + search | **split: mostly done** | Search done and tested. The unfinished residue — backfill of pending rows and live OpenAI verification — becomes BL-006. |
| T06 items CRUD/review/verify | **delete as already done** | Merged and tested. |
| T07 CCA export/import | **delete as already done** | Merged and tested. |
| T08 LLM classification | **split: mostly done** | Rule path done and tested; LLM path exists but has never run live — folded into BL-006 verification. |
| T09 conflict detection | **delete as already done** | Merged, 15 tests pass (with the caveat that it's inert without embeddings — BL-006). |
| T10 recall explanations + feedback | **delete as already done** | Merged, 8 tests pass. |
| T11 MemPalace importer | **delete as already done** (engineering); production apply-run is post-MVP ops | Merged; dry-run verified per PR record. |
| T12 auth + admin | **rewrite → bootstrap gap only** | Auth works (live-verified). What's missing is the first-key bootstrap flow — becomes part of BL-007. |
| T13 KG endpoints | **delete as already done** | Merged, 13 tests pass. |
| T14 taxonomy/tunnels/diary | **delete as already done** | Merged, 12 tests pass. |
| T15 hygiene | **delete as already done** | Merged, 5 tests pass. |
| T16 Python SDK | **delete as already done** (enum fix + CI wiring live in BL-001/BL-005) | Real client, 22 passing tests. |
| T17 MCP adapter | **rewrite → verification task** | Build is done; what remains is smoke verification + minimal tests (BL-008). |
| T18 engram-hooks | **defer post-MVP, rewritten as verification/integration** | Library is written; end-to-end verification requires Hermes runtime + upstream PR (BL-012, post-MVP). |
| T19 deployment | **rewrite** | Still genuinely undone, but rescope: repo deployment artifacts + docs + backup + bootstrap (BL-007), then the actual deploy (BL-009). Original Proxmox-specific provisioning script can stay operator-side. |
| Milestones M1A/M1B/M1C/M2 | **delete as obsolete** | All constituent engineering merged. |
| Milestone M3 | **rewrite** | Survives as BL-007/BL-009. |
| Backlog `current_state` field | **delete as obsolete** | Actively misleading ("No endpoint is functional yet"). |

New items not in the old backlog at all (discovered by this audit): sensitivity enum fix (BL-001), 500→422 error mapping (BL-002), semantic recall (BL-003), auto-promotion (BL-004), CI-with-DB (BL-005), embedding backfill (BL-006), docs truth pass (BL-010), pgvector floor check (part of BL-007).

---

## Recommended sequence

**First (protects everything else, do serially):**
1. BL-005 — CI with real Postgres + SDK/adapters coverage. Every later fix lands under a CI that actually tests it.
2. BL-001 + BL-002 — sensitivity enum fix across all four surfaces + constraint-violation error mapping. Small, confirmed, breaks clients today.

**Then (parallelizable, independent of each other):**
- BL-003 — semantic recall (server) + client literals already match once shipped.
- BL-004 — auto-promotion Path A.
- BL-006 — embedding backfill CLI + live OpenAI verification.
- BL-007 — deployment artifacts (`.env.example`, deployment docs, bootstrap key flow, backup, init-db, healthchecks).
- BL-010 — docs truth pass (can start immediately; cheap).

**Then (needs BL-007; the "make it real" step):**
- BL-009 — deploy to the target host, verify over the network, record the runbook results.
- BL-008 — MCP adapter smoke verification against the deployed (or compose-local) instance + minimal tests.

**Later (post-MVP):**
- BL-011 — production data migration runs (CCA apply, MemPalace apply).
- BL-012 — engram-hooks verification + Hermes integration (blocked on upstream PR #59898 or shim validation against a real Hermes checkout).
- Hard delete, Path B promotion, admin surfaces, Phase 3 open-source work.
