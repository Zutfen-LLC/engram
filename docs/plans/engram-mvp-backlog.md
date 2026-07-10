# Engram MVP Backlog

> **Execution status (updated 2026-07-08 by the BL-010 documentation truth
> pass):** **BL-001 through BL-010 are complete.** The MVP execution backlog is
> closed — the service is implemented, dogfood-deployed, network-verified, and
> backed up (see `docs/ops/dogfood-verification.md`). The entries below are kept
> as the historical record of what each slice was; each carries a `✅ complete`
> marker. The only open work is **post-MVP (BL-011+)** at the bottom of this
> file. This file is now the single execution backlog — `docs/backlog.json` has
> been retired to a pointer.

Grounded in the 2026-07-07 reality audit (`docs/plans/engram-mvp-audit.md`).
Everything already merged and tested (T01–T18 build work) is intentionally
absent. Items are execution-ready slices; BL-001…BL-010 constituted MVP (now
done), BL-011+ are post-MVP.

Baseline this backlog built on and delivered: all 38 designed routes live;
the full suite passes against Postgres 16 + pgvector 0.8; SDK real and
verified; MCP adapter verified (BL-008) and exercised against the dogfood
deployment (BL-009); engram-hooks written but unverified (post-MVP, BL-012).

---

## BL-001: Fix sensitivity enum drift (`confidential` vs `restricted`)

- **Status:** ✅ complete. One sensitivity vocabulary (`normal | sensitive | restricted`) everywhere; `confidential` is rejected with 422, no longer a 500.
- **Objective:** One sensitivity vocabulary everywhere. The DB CHECK constraint and design.md say `normal|sensitive|restricted`; the API route, SDK, and MCP adapter all declare `normal|sensitive|confidential`. Standardize on `restricted` (matches the schema and design; avoids a migration).
- **Why it matters:** Confirmed live: `POST /v1/remember` with `sensitivity="confidential"` returns a raw 500. Every client surface currently advertises a value the server cannot store — the first agent that marks something confidential gets an unexplained server error.
- **Affected files/components:** `engram/api/routes/memory.py:53` (`SensitivityKind`), `sdk/engram-client/engram_client/models.py:20`, `adapters/mcp-server/engram_mcp/server.py:30`, plus any README examples using the old value.
- **Verification requirement:** A test that POSTs each of the three allowed values and asserts 201; a test asserting `confidential` is rejected with 422 by Pydantic (not 500 by the DB); SDK test updated; grep shows zero remaining occurrences of `confidential`.
- **Dependencies:** none (land after BL-005 so CI actually runs the new tests).

## BL-002: Map DB constraint violations to 422, not 500

- **Status:** ✅ complete. Write paths catch `IntegrityError`/CHECK violations and return structured 422 responses; a generic handler prevents unhandled 500s.
- **Objective:** Catch `IntegrityError`/CHECK-violation exceptions on the write paths (`/v1/remember`, PATCH, review, KG, tunnels, diary) and return structured 422 responses; add a generic exception handler so no documented input path can produce an unhandled 500.
- **Why it matters:** The enum bug surfaced as a 500 because the API trusts Pydantic to pre-validate everything the DB enforces. Any future drift between Literal types and CHECK constraints (or a raw SQL client bypassing the SDK) hits the same failure mode. 500s are undebuggable for API consumers.
- **Affected files/components:** `engram/api/app.py` (exception handlers), write-path routes in `engram/api/routes/`.
- **Verification requirement:** Tests that submit DB-invalid payloads (bad kind via raw dict, mismatched tenant on embedding if reachable) and assert 422 with a useful message; no 500 in any test.
- **Dependencies:** BL-005 (CI must run these DB-touching tests).

## BL-003: Implement semantic recall (`POST /v1/recall mode=semantic`)

- **Status:** ✅ complete. `mode=semantic` returns active + proposed (tagged `unreviewed`), visibility-scoped, budget-bounded, logged to `recall_logs`; inert when embeddings are disabled.
- **Objective:** Implement the designed semantic recall mode: query-embedding similarity over active **and proposed** items (proposed tagged `warnings: ["unreviewed"]`), visibility-scoped, budget-bounded, excluded `rejected`/`archived` unless `include_archived`, logged to `recall_logs`. Reuse the existing `_semantic_search` machinery in `engram/api/routes/memory.py`.
- **Why it matters:** `mode=semantic` currently returns 422 "not yet implemented" (`memory.py:718`) while the SDK (`RecallMode`) and MCP tool signatures advertise it — a client-visible contract violation. Design §3 makes proposed-item rediscovery part of the trust story; it's also what makes agent memories findable before review.
- **Affected files/components:** `engram/api/routes/memory.py` (recall handler), `engram/recall.py`, `tests/test_recall.py` (new cases), SDK/MCP docstrings if response shape gains fields.
- **Verification requirement:** Tests covering: active + proposed returned with correct warnings; rejected/archived excluded; visibility scoping; deterministic ordering given fixed vectors; empty-embeddings behavior returns a helpful message (mirroring search). Manual smoke via curl recorded in the PR.
- **Dependencies:** BL-005; benefits from BL-006 for live-embedding verification but testable with inserted vectors.

## BL-004: Implement auto-promotion Path A (lazy check on recall)

- **Status:** ✅ complete (hardened by ENG-AUD-007). The `engram promote-proposed` CLI and `POST /v1/admin/promote` endpoint existed from the original BL-004 slice, but the "lazy check on startup recall" was documented, not wired — `POST /v1/recall mode=startup` never invoked promotion, the dispute-event gate didn't exist, and conflict detection at promotion time relied solely on the write-time status. ENG-AUD-007 closed all three gaps: `POST /v1/recall mode=startup` now runs a bounded (`settings.startup_promotion_limit`, default 20), tenant-scoped Path A pass before selecting active items; `has_external_dispute_event` blocks promotion when another principal disputed the item or gave it negative feedback; and a promotion-time conflict recheck (top-k candidates, non-embedding fallback) re-checks conflict risk against currently-active memories rather than trusting the write-time snapshot. See `docs/design.md` §3 for the full gate list. Path B remains post-MVP.
- **Objective:** Implement the design §3 Path A auto-promotion as a lazy check on startup recall (and/or a small CLI/endpoint for batch promotion): proposed items with `memory_confidence >= auto_promote_confidence_threshold`, age ≥ `auto_promote_min_age_hours`, no unresolved conflicts, no dispute events from another principal → promote to `active`, logging an `item_events` row (`event_type='review_change'`, actor `system:auto_promote`). Honor `tenant_config.auto_promote_enabled`.
- **Why it matters:** README and design both state auto-promotion is "on by default"; in reality nothing consumes the `tenant_config.auto_promote_*` columns (`engram/models.py:383-385`) and proposed items never promote. When engram-hooks bring agent writers online, the proposed queue freezes exactly as design §3 warns — agents write all day, recall never learns.
- **Affected files/components:** `engram/recall.py` or new `engram/promotion.py`, `engram/api/routes/memory.py` (recall entry point), `tests/` new test module.
- **Verification requirement:** Tests: eligible proposed item promotes on recall and appears in the working set; under-threshold confidence does not; under-age does not; unresolved conflict blocks; dispute event blocks; `auto_promote_enabled=false` disables; promotion writes an item_event. 
- **Path B (usage quorum) is explicitly out of scope** — post-MVP.
- **Dependencies:** BL-005.

## BL-005: CI that actually exercises the service (Postgres service container + full package coverage)

- **Status:** ✅ complete. CI runs the core suite, SDK suite, and adapter coverage inside the `docker-compose.ci.yml` stack against `pgvector/pgvector:pg16`; silent skip-mode is guarded.
- **Objective:** Add a `pgvector/pgvector:pg16` (0.8+) service container to the CI test job, apply `migrations/001_init.sql`, and run the suite so the 72 currently-skipped DB tests execute and gate merges. Extend CI to run SDK tests (`sdk/engram-client`) and lint/typecheck (and future tests) for both adapters. Fail the job if more than a handful of tests skip (guard against silent regression to skip-mode).
- **Why it matters:** CI is green today while never running a single remember/search/conflict/KG test — 72 of 126 tests skip without a DB, and the SDK's 22 tests plus both adapters are entirely outside CI (`testpaths = ["tests"]`). Every MVP fix in this backlog would otherwise land unverified.
- **Affected files/components:** `.github/workflows/ci.yml`, possibly `pyproject.toml` pytest config, adapter/SDK pyproject dev extras.
- **Verification requirement:** CI run shows ~126 passed / ~0 skipped for the core suite plus SDK suite; a deliberately broken DB test fails the pipeline (demonstrated once in the PR, then reverted).
- **Dependencies:** none — **do this first.**

## BL-006: Embedding backfill + one recorded live run of the OpenAI paths

- **Status:** ✅ complete (backfill). `engram backfill-embeddings` is implemented and verified with a mocked provider. ⚠️ The **live OpenAI verification checklist is not yet recorded** (`docs/embeddings.md` "Observed" fields are blank) — that remains outstanding; the dogfood runs with embeddings disabled.
- **Objective:** (a) Add a backfill command (`engram backfill-embeddings` CLI or admin endpoint) that finds `memory_embeddings` rows with `embedding_status='pending'` (and items with no embedding row) and populates vectors via the configured provider, batched, idempotent. (b) Perform and record one live verification of the OpenAI-backed paths: embedding generation on remember, semantic search over real vectors, LLM classification, and the conflict-classifier path.
- **Why it matters:** Engram's differentiators — semantic search and write-time conflict detection — are inert at the default `embedding_provider=none`, and items written before embeddings are enabled stay `pending` forever (`engram/embeddings.py` creates placeholders; nothing resolves them). The OpenAI code paths have never executed anywhere: they are implemented-but-unverified in the most literal sense.
- **Affected files/components:** `engram/cli.py`, `engram/embeddings.py`, new `tests/test_backfill.py` (provider mocked), `docs/deployment.md` section on enabling embeddings.
- **Verification requirement:** Mocked-provider tests for the backfill (pending→ready, idempotency, batch limits); a written record (in the PR or docs) of the live run: N items backfilled, semantic search returning sane results, one conflict detected end-to-end with real embeddings.
- **Dependencies:** BL-005; needs an OpenAI API key at verification time.

## BL-007: Deployment artifacts — make the repo standalone-operable

- **Status:** ✅ complete. `.env.example`, `docs/deployment.md`, the `bootstrap-key` flow, a real `engram init-db`, `deploy/backup.sh`, a Compose healthcheck, and the `/ready` pgvector ≥ 0.8 assertion all exist and are documented.
- **Objective:** Close every gap between "clone the repo" and "running, verifiable, backed-up service":
  - `.env.example` with all `ENGRAM_*`/`POSTGRES_*` vars and comments (README's `cp .env.example .env` currently fails — the file doesn't exist).
  - `docs/deployment.md`: compose deployment, auth enablement, **bootstrap API-key flow**, backup/restore, upgrade notes, troubleshooting.
  - Bootstrap key flow: extend `engram generate-key` (or add `engram bootstrap`) to optionally INSERT the key row for the seeded admin principal given a DB URL — today the first key requires hand-written SQL before any admin endpoint is callable (chicken-and-egg, verified in audit).
  - Real `engram init-db` (execute `migrations/001_init.sql` against the configured DB) — currently a print stub; also the only migration path for non-empty volumes, since the compose initdb mount runs on first boot only. Fix the wrong migration instructions in the compose header comment.
  - `deploy/backup.sh` (pg_dump + retention) and a service healthcheck in compose.
  - `/ready` (or startup log) asserts pgvector ≥ 0.8 — on 0.6 the service boots fine and then 500s every semantic query (reproduced in audit).
- **Why it matters:** Another operator cannot currently stand Engram up from the docs, create a first credential, upgrade a schema, or back anything up. This is the substance of old T19 minus the Proxmox-specific provisioning.
- **Affected files/components:** `.env.example` (new), `docs/deployment.md` (new), `engram/cli.py`, `docker-compose.yml`, `deploy/backup.sh` (new), `engram/api/routes/health.py`.
- **Verification requirement:** Clean-machine walkthrough of `docs/deployment.md` (fresh volume → compose up → bootstrap key → authenticated remember/recall → backup → restore) recorded as a checklist in the doc; `init-db` and `bootstrap` covered by tests where practical.
- **Dependencies:** none (parallel with BL-003/004/006).

## BL-008: MCP adapter verification (smoke + minimal tests)

- **Status:** ✅ complete. The adapter ships unit + integration tests in CI and was smoke-tested against the dogfood deployment (`engram_remember`/`recall`/`search` round trips).
- **Objective:** Verify the existing MCP server against a running Engram: launch over stdio, list tools, exercise `engram_remember`, `engram_recall`, `engram_search`, `engram_kg_add`/`engram_kg_query`, and one failure case (server down, bad key). Add a minimal test suite (FastMCP in-process client with the SDK pointed at a test server, or mocked SDK) and wire into CI.
- **Why it matters:** The MCP adapter is the actual dogfooding interface — it is how agents will call Engram — and it currently has zero tests and no recorded run. It is one enum-drift bug away from broken (BL-001 fixes the known one; tests catch the next).
- **Affected files/components:** `adapters/mcp-server/` (new `tests/`), `.github/workflows/ci.yml`.
- **Verification requirement:** Recorded manual smoke against a live compose instance (tool list + one round-trip per core tool); CI-run tests for tool registration and request/response mapping.
- **Dependencies:** BL-001 (enum fix), BL-005 (CI), a running instance (local compose is sufficient; BL-009 not required).

## BL-009: Deploy for dogfooding and verify over the network

- **Status:** ✅ complete. The instance is live, auth-enabled, network-reachable, backed up, and restore-tested; the sanitized record is in `docs/ops/dogfood-verification.md`.
- **Objective:** Stand up the production instance (target host per operator preference — the old plan said Proxmox VM + Tailscale), following `docs/deployment.md` exactly: compose up, migrations applied, auth **enabled**, bootstrap key issued, backup cron installed. Verify health/ready/openapi and one authenticated remember→recall round-trip from a client machine over the network. Record the instance URL and runbook results in `docs/deployment.md` (or an ops-private equivalent).
- **Why it matters:** There is currently no evidence in the repo that any live instance exists, and no way for an agent or teammate to find one if it does. MVP means dogfooding; dogfooding needs a durable, reachable, authenticated instance. This is also the forcing function that proves BL-007's docs are honest.
- **Affected files/components:** deployment target (ops), `docs/deployment.md` (verification record), optionally `deploy/` provisioning script if the operator wants it in-repo.
- **Verification requirement:** The runbook checklist executed on the real host with outcomes recorded; `curl https?://<host>:8000/ready` from a second machine; restore-from-backup tested once.
- **Dependencies:** BL-007; BL-001/BL-002 should be deployed with it (deploy from main after they merge).

## BL-010: Documentation truth pass

- **Status:** ✅ complete. This pass. README, scripts/README, design.md (implementation-status annotations), deployment.md (runbook corrections), embeddings.md, the MCP README, the mvp-backlog, and the retired backlog.json are all aligned to the dogfood-deployment reality.
- **Objective:** Make every doc describe the audited reality:
  - `README.md` Status section: Phase 1A–1C and M2 build work complete; list the real remaining gaps (this backlog); fix the quickstart once `.env.example` exists; qualify or remove the "auto-promotion on by default" claim until BL-004 merges.
  - `scripts/README.md`: remove "(TODO)" from both importers; document dry-run/apply usage.
  - `docs/design.md`: add implementation-status annotations for §3 auto-promotion (BL-004), §3/§9 semantic recall (BL-003), §11 hard-delete/PII/read-audit (not implemented, deferred).
  - `docs/backlog.json`: replace with a pointer to this backlog (or delete); its `current_state` ("No endpoint is functional yet") is the single most misleading sentence in the repo.
- **Why it matters:** The planning docs are what a future execution agent reads first; today they describe a project ~18 merged PRs behind reality in one direction and several unimplemented promises ahead of it in the other. This audit exists because of that drift; leaving the sources of drift in place recreates the problem.
- **Affected files/components:** `README.md`, `scripts/README.md`, `docs/design.md`, `docs/backlog.json`.
- **Verification requirement:** Every claim in README Status and scripts/README is checkable against code or CI; no doc references `.env.example` until it exists (or lands in the same PR as BL-007).
- **Dependencies:** none to start; final wording for auto-promotion/semantic recall sections depends on whether BL-003/BL-004 have merged.

---

## Post-MVP

## BL-011: Production data migration runs (CCA + MemPalace apply-mode)

- **Objective:** Execute `scripts/import_cca.py --apply` (~40 ledger entries) and `scripts/import_mempalace.py --apply` (~200–900 drawers + KG triples + tunnels) against the deployed instance; verify counts, dedup behavior, taxonomy shape, and export round-trip afterward.
- **Why it matters:** This is the moment Engram becomes the system of record. It is operational work, not engineering — both importers are built, and MemPalace was dry-run-verified against a real palace.
- **Affected files/components:** none (ops); import logs recorded.
- **Verification requirement:** Post-import `GET /v1/review/stats` and `/v1/taxonomy` match dry-run predictions; `GET /v1/export/cca` returns the migrated ledger.
- **Dependencies:** BL-009 (live instance), BL-006 (embeddings active so imports get vectors — or run backfill after).

## BL-012: engram-hooks verification and Hermes integration

- **Status:** ✅ compat shim activated, unit-tested, and profile-wired (ENG-HERMES-001). ⚠️ End-to-end verification against a real Hermes checkout is still outstanding.
- **Objective:** Verify the already-written engram-hooks library end-to-end: unit tests for guards/volatile store/config/promotion gates (no Hermes needed); then an integration session against a real Hermes checkout exercising the `prepare_memory_write` shim path (upstream PR NousResearch/hermes-agent#59898 still pending) and the three lifecycle hooks writing through classify → remember; wire into a real profile.
- **What ENG-HERMES-001 completed:** `detect_prepare_memory_write()`, `install_compat_shim()`, and `install()` are wired so engram-hooks is no longer inert — `install()` runs at Hermes startup (see `profiles/hermes-engram-dogfood.yaml`), picks native `prepare_memory_write` when present, otherwise idempotently monkey-patches `hermes_agent.tools.tool_executor` / `hermes_agent.runtime.agent_runtime_helpers`, and exposes a structured `InstallStatus` (`native_hook_available`, `compat_shim_installed`, `patched_modules`, `failure_reason`, `automatic_capture_active`). `ENGRAM_HOOKS_REQUIRE_AUTOMATIC_CAPTURE` makes a profile that expects automatic capture fail loudly instead of silently degrading. 37 tests in `adapters/engram-hooks/tests/` cover detection (native/absent/no-Hermes), patch application, guard-allow reaching the original writer, guard-reject short-circuiting it, install idempotency (no double-wrap), patch failure on API drift, and the profile fixture staying consistent with the code — all against a fake `hermes_agent` module tree, no real Hermes checkout needed.
- **What's still outstanding:** a recorded session against a real Hermes checkout — startup log confirming which path activated, an accepted write reaching Engram, a rejected write not reaching it, and (once BL-004 auto-promotion is live) a startup recall containing the promoted memory. See the "Recorded result" checklist in `docs/ops/hermes-dogfood-profile.md` — it is intentionally left unchecked.
- **Why it matters:** Automated memory capture is the end-state product loop — but full end-to-end verification remains post-MVP because explicit MCP-driven dogfooding works without it and a real Hermes checkout isn't available in this repo's CI.
- **Affected files/components:** `adapters/engram-hooks/` (`hooks.py`, `config.py`, `tests/`), `profiles/hermes-engram-dogfood.yaml`, `docs/ops/hermes-dogfood-profile.md`, CI (`Dockerfile`, `scripts/run_ci.py`).
- **Verification requirement:** Unit suite in CI (done); a recorded Hermes session showing extraction → classification → proposed write → (BL-004) auto-promotion → startup recall containing the memory; shim behavior verified on a Hermes version without the hook (done via fake-module tests, pending real-Hermes confirmation).
- **Dependencies:** BL-005, BL-008, BL-009; externally, Hermes PR #59898 or shim validation in lieu (shim validation is now implemented; the real-Hermes confirmation session remains).

---

## Explicitly not backlogged (already done — verified in audit)

Remember, startup recall, keyword/semantic/hybrid search, items CRUD + PATCH audit, review/verify/invalidate/supersede, conflict detection + resolution, feedback, KG with visibility inheritance, taxonomy, tunnels, diary, hygiene (stale/bulk-archive/stats), CCA export, both importers (as code), API-key auth + admin create endpoints, RLS session plumbing, schema + migrations, Docker Compose skeleton, Python SDK, MCP server code, engram-hooks code, rule-based classification. Do not re-open these as build tasks.

## Explicitly deferred (post-MVP, no backlog entry yet)

Auto-promotion Path B (usage quorum), hard delete + `deletion_events` tombstones + KG cascade, PII-risk classification, sensitive-read audit logging, admin list/update/delete + console, multi-way conflict table, local embedding provider, Helm/cloud artifacts, Phase 3 open-source packaging.
