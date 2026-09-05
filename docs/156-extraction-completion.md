# Issue #156: Structured extraction

Branch: `feat/156-structured-extraction`.
Baseline: `7f66c810c9cd4c65458b16fbd18c9491e9c5e9e3`.

## Delivered contract

`POST /v1/extract` accepts bounded structured messages. It supports preview
and idempotent proposed writes. `GET /v1/extract/{run_id}` retrieves a stored
receipt. OpenAPI declares write and read scopes. The Python SDK exposes
`extract()` and `get_extraction()` with matching typed models.

The schema version is `engram.extraction.v1`. The prompt version is
`engram.extract.3`. The live evaluation used the OpenAI-compatible adapter at
`api.deepinfra.com`. Both configured and returned model names were
`deepseek-ai/DeepSeek-V4-Flash`.

Migration 036 adds extraction receipts and item links that are append-only
to the runtime app role. `engram_app` has SELECT and INSERT only. Migration
036 explicitly revokes UPDATE and DELETE because migration 003 grants full
DML to future tables. Reapplication preserves these effective privileges.
The UPDATE triggers and link constraints provide additional protection.
Owner/migration-role administrative deletion remains possible. Each write
binds a normalized content hash, ingest, item, candidate, and extraction run.
The receipt stores taxonomy and retention output with structured provenance.
It does not create trusted promotion evidence in `classification_runs`.

See [the ADR](adr-156-structured-extraction.md) and
[the JSON schema](schemas/extraction-v1.json).

## Provenance and the #162D boundary

The required regression supplies a doctrine-like instruction about bypassing
security review. The controlled extractor suggests `fact`. The result keeps
its direct-user assertion mode, exact evidence, shared root, and literal
temporal, scope, and security cues. The schema has no risk, consequence, or
admission field. Extra authority fields fail validation. Suggested kind does
not set risk. Source cues do not assign memory sensitivity.

This gives #157 a structured origin, attribution basis, evidence references,
input hashes, source identity, context spans, and explicit cues. #157 does not
need to recover these fields from free-text memory content. Receipt hashes
attest to the stored process result. They do not prove factual correctness.

The tests verify that an agent's direct-user capture retains source authority
10 and the configured `sync_turn` confidence prior. It starts as proposed.
Extraction retention output does not populate trusted retention evidence on
the item. Existing production admission and promotion policy remain in place.

## Hermes capture and rollback

`ENGRAM_HOOKS_STRUCTURED_EXTRACTION` defaults to false. When enabled, the hook
sends complete structured messages through the SDK. Missing roles remain
unknown. Local guards and the service secret denylist run before network
submission or volatile storage.

| Input | Existing pipeline | Flagged extraction |
| --- | --- | --- |
| User: "I no longer prefer dark mode." | Candidate text with lifecycle source type | Direct statement; user role; exact message span |
| Assistant: "I infer that the user wants light mode." | Candidate text and rolling text context | Assistant inference; separate assertion mode |
| Both messages in one turn | No structured shared-root evidence | One shared batch evidence root |
| Provider or server failure | Existing classifier fallback | Saved structured request and retry key; zero claimed durable writes |

`HookResult` serializes `written_proposed`. `promoted` remains a compatibility
property. The integration test follows Hermes hooks → SDK → real API →
PostgreSQL. A separate rollback branch follows the actual
`/v1/classify` → `/v1/remember` path.

Setting the flag to false restores the existing capture pipeline. Migration
downgrade removes extraction tables while preserving memory items and policy
state. Upgrade, reapplication, downgrade, and re-upgrade are tested. The
full-schema rollback test compares written memory, tenant configuration, and
memory kinds before and after downgrade and re-upgrade.

## Live golden evaluation

The frozen set contains 19 synthetic cases. Eighteen requests reached the
provider. The secret case was rejected before provider submission.
The final run met all expected HTTP outcomes.

| Metric | Final run |
| --- | ---: |
| Candidate precision / recall / F1 | 100% / 100% / 100% |
| Attribution accuracy | 100% |
| Evidence-span validity / coverage | 100% / 100% |
| Explicit-cue coverage | 100% |
| Kind accuracy | 78.9% |
| Retention-label accuracy | 89.5% |
| Duplicate rate | 0% |
| Abstention rate | 15.8% |
| Median / maximum provider latency | 4.410 s / 10.909 s |
| Input / output tokens | 26,220 / 3,167 |
| Provider-reported cost | $0.002690244 |
| Cost-reporting coverage | 100% |

[The final record](../evals/extraction/live-v1.json) includes per-case output,
receipt hashes, versions, and the golden-file hash. Its metrics and receipt
hashes are checked by tests. The earlier `live-prompt*.json` records remain
available. They include a provider failure and invalid abstention codes.
Unknown costs remain null. Their reported partial costs are not presented as
complete totals. The final run is not evidence of perfect provider reliability.

The prompt changed during development. The golden set did not change.
These results are development measurements, not a held-out certification.
Lexical matching can reject valid paraphrases. Taxonomy and retention remain
imperfect. Caller-supplied roles do not authenticate speakers. Context spans
do not prove referent resolution. Batch roots do not prove independence
between separate requests. No default rollout or admission change is justified
by these results.

## Verification record

All database verification uses repository Docker Compose PostgreSQL 16 with
pgvector. Extraction API tests use the non-owner application role under FORCE
RLS. They cover tenant, principal, workspace, profile, and source-reference
boundaries. They include a real profile-bound API key.

The targeted suite covers concurrent retries, distinct-key deduplication,
partial candidate failure, failed final commits, linkage, immutability,
secret rejection, provider failures, unknown attribution, cue correction,
pronoun context, SDK/OpenAPI consistency, Hermes fallback, and rollback.

The append-only regression in `tests/test_extraction.py` is included in the
canonical trust-proof set. It checks PostgreSQL's effective privilege matrix
for both extraction tables after initial migration and reapplication. It
creates a valid run and link through the API. The same tenant/principal can
read both rows as `engram_app`. PostgreSQL denies UPDATE and DELETE on each
owned row with SQLSTATE `42501` and a table-permission error. Both rows remain
unchanged after each attempt, including the run deletion that would cascade
to the link if DELETE were allowed. This proves the privilege boundary
independently of the UPDATE trigger and cross-principal RLS denial.
The focused extraction suite passed all 33 tests against Compose PostgreSQL.

`make check VENV_BIN=/usr/local/bin` passed inside the CI Compose container:
3,539 root tests passed, with 34 expected skips and no database skips. The
additional artifact, sensitivity-boundary, and full-schema rollback checks
also passed against Compose PostgreSQL.

The final handoff requires the complete `make compose-ci` flow on the final
commit. This flow includes service, SDK, MCP, hooks, migration, and RLS checks.
The final branch commit and execution results are reported with the handoff.
