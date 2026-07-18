# Context Manifest v1 — Canonical Served-Context Contract

> **Status (ENG-CONTEXT-001):** This slice *defines and proves* the contract.
> **Status (ENG-CONTEXT-002A):** The durable receipt *storage substrate*
> (`context_receipts` table, migration 026, ORM model, repository, RLS,
> retention metadata, real-PostgreSQL proofs) is implemented as a
> storage-only foundation.
> **Status (ENG-CONTEXT-002B):** The storage substrate is wired into the
> production startup-recall path as a **default-off, fail-open dark write**.
> When enabled, a successful `mode=startup` recall builds a manifest from the
> *finalized* `RecallResponse` and persists one immutable receipt on a
> dedicated app-role session; the row is reloaded and verified before commit,
> and any receipt failure is swallowed (the recall response is returned
> unchanged). Receipt IDs/hashes remain **invisible to clients** (exposure is
> decided in ENG-CONTEXT-002C); an inspect/verify API lands in
> ENG-CONTEXT-003. Only `mode=startup` is supported. Semantic recall creates
> no receipt while the feature is enabled.

The Context Manifest is the deterministic, versioned artifact beneath the
**Engram Context Ledger**. It lets Engram answer one question with proof:

> **What context did Engram serve, and which Engram policy/version admitted it?**

## 1. Product purpose

When an agent recalls context from Engram, the manifest records — in a
tamper-evident, content-addressed way — the exact packet that was served, the
ordered items it contained, the decision fields that admitted each item
(review status, trust, score, visibility, …), the request that drove
selection, and the policy/version identifiers in effect. Two identical served
packets under an identical decision context produce byte-identical canonical
JSON and therefore an identical `manifest_hash`.

## 2. What the manifest proves

- **What was served:** the exact rendered packet (`working_set`) and its
  per-item content hashes, captured at serve time.
- **Which policy admitted it:** the decision versions (scoring, config,
  candidate strategy, manifest contract, packet render) and the request
  descriptor (requested vs effective budgets, workspace).
- **Deterministic identity:** identical inputs ⇒ identical `manifest_hash`,
  reproducible across processes and across languages (Python and JavaScript).

## 3. What the manifest does NOT prove

> The manifest proves what Engram served and which Engram policy/version
> admitted it. It does not prove that the memory was factually true or that an
> agent relied on it.

It is **not**:

- a truth certificate — memories may be wrong, disputed, or stale;
- proof that any agent used the context, or that a memory caused an action;
- an external cryptographic signature (no KMS, no key attestation in
  ENG-CONTEXT-001);
- retroactive exact replay of past recalls (the current `recall_logs` do not
  preserve the finalized served response; durable receipts begin in
  ENG-CONTEXT-002).

## 4. Deterministic manifest vs volatile receipt envelope

Identical served packets under identical decision context must produce
identical `manifest_hash`. Therefore the **deterministic manifest** contains
*only* served-context data and must **not** contain:

- receipt ID, receipt creation timestamp;
- recall-log ID;
- request ID or trace ID;
- database insertion timestamps;
- job IDs;
- any random or clock-derived identifier.

`manifest_hash` is computed *over* the canonical manifest bytes and is never
placed inside the object being hashed. `packet_hash` **is** included in the
manifest because it is derived purely from the served packet bytes.

Those volatile values belong in a future **receipt envelope** (ENG-CONTEXT-002):

```
ContextManifestV1        ← deterministic, canonicalized, content-addressed
Future ContextReceipt    ← receipt_id, created_at, recall_log_id,
                            manifest, manifest_hash, packet_hash,
                            retention/storage metadata
```

## 5. Normative JSON shape

```json
{
  "schema": "engram.context-manifest",
  "schema_version": "1.0",
  "canonicalization": "rfc8785",
  "mode": "startup",
  "subject": {
    "tenant_id": "uuid",
    "principal_id": "uuid",
    "workspace_id": "uuid-or-null",
    "memory_context_version": "memory-context-v2",
    "memory_profile_id": "uuid-or-null",
    "memory_profile_revision_id": "uuid-or-null",
    "memory_profile_version": 3
  },
  "request": {
    "requested": {
      "workspace_supplied": false,
      "byte_budget": null,
      "token_budget": null,
      "item_budget": null
    },
    "effective": {
      "workspace_id": null,
      "byte_budget": 65536,
      "token_budget": null,
      "item_budget": null
    },
    "query_digest": null,
    "request_digest": "sha256:..."
  },
  "versions": {
    "scoring_version": "v1",
    "config_version": "v1",
    "candidate_strategy_version": "startup-candidates-v1",
    "manifest_contract_version": "context-manifest-v1",
    "packet_render_version": "working-set-v1"
  },
  "result": {
    "item_count": 2,
    "served_content_byte_count": 123,
    "rendered_packet_byte_count": 147,
    "pinned_omitted_count": 0,
    "omitted_count": 4,
    "message": null
  },
  "packet": {
    "media_type": "text/plain; charset=utf-8",
    "render_version": "working-set-v1",
    "hash": "sha256:..."
  },
  "items": [
    {
      "ordinal": 0,
      "item_id": "uuid",
      "kind": "fact",
      "served_content_hash": "sha256:...",
      "review_status": "active",
      "authority": 10,
      "visibility": "private",
      "workspace_id": null,
      "score": 0.8123,
      "reasons": ["importance=0.90"],
      "warnings": [],
      "pinned": false,
      "importance": 0.9,
      "source_trust": 0.8,
      "memory_confidence": 0.75,
      "human_verified": true,
      "conflict_type": null,
      "conflict_resolution_status": null
    }
  ]
}
```

`subject.workspace_id` is the *resolved authorized* workspace reference, not a
caller slug. An unprofiled context uses null profile fields (all three profile
fields null together). `result.served_content_byte_count` is the sum of served
item content byte sizes (the recall `byte_count` semantics);
`rendered_packet_byte_count` is the exact UTF-8 size of `working_set`. The two
are not interchangeable.

Item array order is the exact response order; `ordinal` equals the array
position. `score` may be null for pinned items. Reason/warning array order is
significant (never alphabetized). Full memory `content`, `conflicts_with_item_id`,
review notes, source URIs, provenance payloads, secrets, and embeddings are
absent — only `served_content_hash` represents content.

### Normative wire round-trip

The model emits the normative wire shape and parses that exact shape back.
`ContextManifestV1.model_validate` and `model_validate_json` accept the emitted
wire object (including the top-level `"schema"` key) without renaming. The
`"schema"` key is a bidirectional alias of the internal `schema_name` field
(it collides with `BaseModel.schema`); `populate_by_name=True` also permits
Python construction by field name.

All stable protocol markers are **required `Literal` constants** on the wire:
`schema` (`"engram.context-manifest"`), `schema_version` (`"1.0"`),
`canonicalization` (`"rfc8785"`), `mode` (`"startup"`),
`subject.memory_context_version` (`"memory-context-v2"`),
`versions.manifest_contract_version` (`"context-manifest-v1"`),
`versions.packet_render_version` and `packet.render_version`
(`"working-set-v1"`), and `packet.media_type`
(`"text/plain; charset=utf-8"`). They are required (not defaulted) so the JSON
Schema and a parsed wire manifest enforce them.

The strict wire parser requires **canonical UUID/hash representations** — it
does not normalize a noncanonical (e.g. uppercase) UUID or hash into a
different canonical object. UUIDs match `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`;
hashes match `^sha256:[0-9a-f]{64}$`. `visibility` is constrained to the
storage CHECK vocabulary (`private`, `workspace`, `tenant`, `public`).
Counts, ordinals, byte sizes, and budgets are nonnegative. `authority` is
intentionally NOT range-constrained (storage has no CHECK range on it).

## 6. Canonicalization algorithm

Canonical JSON bytes are produced with **RFC 8785 (JSON Canonicalization
Scheme / JCS)** semantics via the pinned [`rfc8785`](https://pypi.org/project/rfc8785/)
library (Trail of Bits; pure-Python; zero runtime dependencies; Apache-2.0;
packaged in Debian and Gentoo; full ECMAScript/ryu number serialization and
UTF-16 member ordering).

JCS semantics:

- UTF-8 encoded, **no byte-order mark**, no insignificant whitespace;
- object members ordered by **UTF-16 code unit** of the member name;
- array order preserved;
- JSON string escaping is ECMAScript-compatible; non-ASCII characters (≥
  U+0080) are **not** escaped — the exact Unicode scalar sequence is preserved
  (no Unicode normalization);
- number serialization is ECMAScript `Number.prototype.toString()`;
- `-0` is serialized canonically as `0` (RFC 8785 §3.2.2.3);
- `NaN` and `+Infinity`/`-Infinity` are **rejected** (they are not valid JSON).

**`json.dumps(sort_keys=True)` is NOT a valid substitute.** It sorts by
Unicode code point (not UTF-16 code units) and does not implement the JCS
number format. The two orderings diverge for keys whose UTF-16 and code-point
orders differ (e.g. certain supplementary-plane characters).

### Dependency decision

`rfc8785` was chosen over [`jcs`](https://pypi.org/project/jcs/) (the other
spec-clean Python implementation, authored by an RFC 8785 co-author). `jcs`
has had no release since 2022-04; `rfc8785` is actively maintained, is packaged
in major distributions, and has full floating-point support. The dependency is
narrowly scoped (only the manifest contract uses it), pinned `>=0.1.4`, and
adds no transitive runtime dependencies.

## 7. Exact hash preimages

All SHA-256 values use `sha256:<64 lowercase hexadecimal characters>` (single
shared helper).

| Hash | Preimage (exact bytes) |
|---|---|
| `manifest_hash` | SHA-256 of the RFC 8785 canonical bytes of the manifest object (without any `manifest_hash` field). |
| `packet.hash` (`packet_hash`) | SHA-256 of the exact UTF-8 bytes of `response.working_set`. |
| `served_content_hash` | SHA-256 of the exact UTF-8 bytes of the served item `content`. |
| `request_digest` | SHA-256 of the RFC 8785 canonical bytes of the request descriptor (without the `request_digest` field). |

`served_content_hash` deliberately does **not** reuse
`engram.canonicalize.content_hash()`. The dedup hash normalizes whitespace and
case (it is for detecting near-duplicate writes); the manifest content hash
must detect **any** exact served-content byte change. No whitespace, line
ending, case, Unicode, or trailing-newline normalization is applied.

The **empty packet** hashes as the SHA-256 of zero bytes
(`sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`).

## 8. UTF-8 and newline behavior

- All string hashing is over **exact UTF-8 bytes** — no normalization.
- `working_set` rendering is `[kind] content` lines joined with `LF` (`\n`),
  with **no trailing newline**. `working-set-v1` adds no final newline.
- `LF` **separates** rendered items; an `LF` **inside** an item's `content` is
  valid and preserved exactly in `served_content_hash`, the rendered packet, and
  therefore `packet_hash` (see vector 010).
- A trailing newline introduced **outside** an item's content, or a `CRLF`
  separator between items, makes the finalized response **incoherent** with
  `working-set-v1`: the builder rejects it before manifest construction. They
  are negative cases, not valid vectors. (Packet hashing still uses the exact
  finalized response bytes after coherence succeeds.)

## 9. Field-level privacy decisions

The manifest intentionally excludes:

| Excluded | Why |
|---|---|
| Raw memory `content` | Privacy; `served_content_hash` detects byte changes without retaining content. |
| Raw semantic query | Privacy; startup `query_digest` is null and a raw query is never stored. |
| Full policy JSON / workspace grant lists | Minimization; `subject` carries only resolved references. |
| Excluded/rejected candidate IDs | Minimization; only bounded aggregate `omitted_count`/`pinned_omitted_count`. |
| `conflicts_with_item_id` | A counterpart may not be independently eligible; its ID would be misleading. |
| Source URIs, review notes, provenance payloads | Minimization / privacy. |
| Credentials, API keys, secrets | Safety (never stored anywhere in Engram). |
| Embedding vectors | Size / privacy. |

Strict Pydantic v2 models (`extra="forbid"`) reject unknown fields, so a
caller cannot inject any of the above into the hashed object. All float fields
use `allow_inf_nan=False`, rejecting NaN/±Infinity. UUIDs are stored as
lowercase canonical strings and validated; hash fields are validated against
`^sha256:[0-9a-f]{64}$`.

## 10. Versioning rules

- A field addition, removal, semantic change, canonicalization change, render
  change, or number-format change requires an **explicit contract-version
  decision**.
- Never silently change the meaning of `context-manifest-v1`.
- Backward-compatible consumers may ignore fields **only** when the schema
  version explicitly allows that behavior; the strict producer models still
  forbid accidental unknown fields.
- Hashes always identify one exact schema/canonicalization contract.
- `working-set-v1` defines the existing packet rendering and is unchanged by
  this slice.
- Git commit SHAs are NOT hashed as a product contract version (they are not
  stable protocol versions). Git provenance may later live in a receipt
  envelope.

## 11. Backward compatibility

ENG-CONTEXT-001 is additive at the API surface: the five new served item
fields (`authority`, `visibility`, `workspace_id`, `conflict_type`,
`conflict_resolution_status`) are appended to the recall item dictionaries
that already flow through `RecallResponse.items` (typed `list[dict[str, Any]]`).
No existing field, selection rule, ordering, score, or rendering changed. The
Python SDK treats recall items as dictionaries, so this is not an SDK breaking
change. The manifest module is new code with no callers in production paths
until ENG-CONTEXT-002.

## 12. Golden vectors

Ten language-neutral vectors are checked in at
[`conformance/context-manifest-v1/vectors/`](../conformance/context-manifest-v1/vectors/).
Each pins `manifest_hash`, `packet_hash`, `request_digest`, per-item
`served_content_hash`, and the canonical JSON bytes. See the conformance
[`README.md`](../conformance/context-manifest-v1/README.md) for the inventory
and how to run both verifiers.

## 13. Startup-only support in ENG-CONTEXT-001

Only `mode="startup"` is supported. The builder
(`build_startup_context_manifest_v1`) hard-sets `mode="startup"` and rejects
any other mode. A future semantic mode will be added as a **separate** builder
without discarding this contract. Semantic recall item dictionaries are kept
field-aligned with startup so the future semantic manifest can reuse the same
field vocabulary.

## 14. Durable receipt storage (ENG-CONTEXT-002A)

ENG-CONTEXT-002A introduces the durable **receipt storage substrate**. The
deterministic manifest defined here is the hashed artifact; the receipt is its
volatile persistence envelope:

```
ContextManifestV1        ← deterministic, RFC 8785-canonicalized,
                           content-addressed by manifest_hash
ContextReceipt           ← receipt_id, recall_log identity, tenant/principal
                           ownership, created_at, retention_expires_at,
                           manifest (JSONB), manifest_hash, packet_hash
```

The receipt row lives in the `context_receipts` table (migration 026), is
one-to-one with `recall_logs` via a composite foreign key
`(tenant_id, principal_id, recall_log_id) → recall_logs(tenant_id,
principal_id, id)` with `ON DELETE RESTRICT`, is FORCE RLS-protected (both
tenant and principal required), and is append-only from the application's
perspective (app role SELECT/INSERT only; UPDATE/DELETE revoked). Receipt ID,
creation time, recall-log ID, and retention metadata are deliberately **outside**
the manifest hash. No raw memory content, raw `working_set`, or raw query text
is stored — only the manifest JSONB. Verification parses the stored JSONB back
through `ContextManifestV1`, recanonicalizes under RFC 8785, and compares the
recomputed hash to `receipt.manifest_hash` (see
`engram.context_receipts.verify_context_receipt_record`).

**ENG-CONTEXT-002A is storage-only.** It does not create receipts during a
production recall; receipt creation (dark writes) begins in ENG-CONTEXT-002B,
and retrieval / authorized rehydration begins in ENG-CONTEXT-003.

### Retention metadata (002A)

`retention_expires_at` is metadata only in this slice: `NULL` means no expiry
has been assigned; a non-NULL value is the earliest time at which a *future*
retention process MAY evaluate the receipt for deletion. This slice does not
delete expired rows and does not change retention after insert. No tenant
retention configuration is introduced here.

### Migration safety

Migration 026 is additive and safe to re-apply (`CREATE TABLE IF NOT EXISTS`,
guarded constraints, guarded policy recreation). Rolling application code back
leaves an unused additive table; do not drop the table if receipts have been
inserted. This slice itself creates no production receipts.

## 14a. Startup dark writes (ENG-CONTEXT-002B)

ENG-CONTEXT-002B wires the storage substrate into the production
startup-recall path as a **default-off, fail-open** dark write. No migration
is added — it reuses the migration 026 table.

### Disabled-path guarantee

When `ENGRAM_CONTEXT_RECEIPT_DARK_WRITE_ENABLED=false` (the default), the
recall route performs **no receipt-specific work at all**. The outer route
guard checks the flag before any receipt-specific parsing or validation, so
none of these run while disabled: executed-result provenance parsing,
`ContextManifestV1` construction, the dedicated receipt DB session, the
`context_receipt.dark_write` usage event, or the receipt structured log. The
orchestrator's own disabled check is retained as defense in depth.

### Executed-result provenance contract

The receipt is built **only** from required executed-result values attested
by the startup engine. The orchestrator parses these keys from the raw
startup result and rejects (fail-open, no receipt) if any is missing or
malformed:

- `recall_log_id` — canonical UUID;
- `workspace_id` — explicit `null` or canonical UUID (missing is an error;
  empty string is an error, not null);
- `scoring_version`, `config_version`, `candidate_strategy_version` —
  nonempty strings;
- `effective_byte_budget`, `effective_token_budget` — `null` or nonnegative
  integer;
- `effective_item_budget` — must be exactly `null` for startup v1.

A missing field is **never** inferred to a default. Public `RecallResponse`
compatibility defaults (e.g. `scoring_version='v1'`) remain available to
clients but **never feed the receipt manifest** — the receipt path uses the
raw executed values after strict validation.

### Finalized-response snapshot boundary

The route finalizes **one** `RecallResponse` object before any dark-write
work. That same object is passed to `build_startup_context_manifest_v1` and
returned to the caller. The manifest is built **only** from the finalized
response — never from ORM `MemoryItem` rows, a fresh memory query,
`recall_logs.item_ids` alone, reconstructed content, or a second
independently assembled response dictionary. No database mutation after
response construction may affect the manifest: a later memory mutation
cannot alter the stored served snapshot.

### Effective vs requested decision context

The manifest's request descriptor carries two views of the recall decision:

- **requested** — the caller's exact request (before server defaults):
  `workspace_supplied`, `byte_budget`, `token_budget`, `item_budget`;
- **effective** — what startup actually used (after applying defaults and
  workspace resolution): `workspace_id`, `byte_budget`, `token_budget`,
  `item_budget` (always `null` for startup v1).

The startup engine exposes `effective_byte_budget`, `effective_token_budget`,
and `effective_item_budget` (always `null`) as internal-only result values
that are the exact values used by budget enforcement and written to
`RecallLog`. They are **not** added to `RecallResponse`. The orchestrator
does not recompute default budgets — the engine is authoritative for the
values it actually used.

### Unified best-effort entrypoint

The route calls a single best-effort entrypoint that owns the complete
enabled sequence: flag check, monotonic-deadline start, executed-result
parsing, decision-context construction, manifest construction, dedicated
app-role session, RLS, storage, reload, verification, commit, bounded usage
telemetry, and the standardized structured log. The route does not
duplicate evidence parsing. A `build_decision_context` failure (missing or
malformed provenance) uses the same fail-open result, structured log, and
bounded usage-event attempt as any other enabled failure.

### Dedicated transaction isolation

Dark-write persistence uses a fresh, short-lived, non-owner app-role session
(`engram.db.async_session_factory`), not the caller's request session. RLS
is applied with `apply_rls_context(tenant_id, principal_id)`. Rationale: the
recall log is already committed; a receipt database error must not poison
the request session; rollback must affect only the optional receipt attempt.

### Verification before commit

Inside the dedicated session the orchestrator: applies RLS, calls
`store_context_receipt`, flushes, forces a database reload
(`session.refresh(receipt)`), verifies the reloaded record, compares the
verified reloaded manifest's canonical JSON bytes to the original built
manifest's canonical JSON bytes, and commits **only after** verification
succeeds. On any mismatch it raises `ContextReceiptIntegrityError`
internally, rolls back the dedicated transaction, emits failure
observability, and returns the original `RecallResponse`. A newly inserted
row does not commit if immediate verification fails.

### Total deadline

`ENGRAM_CONTEXT_RECEIPT_DARK_WRITE_TIMEOUT_SECONDS` (default 1.0) is a
single monotonic deadline that starts **before** executed-result validation
and covers every stage of the enabled attempt, including the bounded
usage-event attempt. Each awaited stage runs against the remaining deadline,
so no single stage can consume the entire configured timeout.

### Fail-open + no public exposure

All ordinary manifest, database, integrity, telemetry, and timeout failures
are fail-open: the route returns the exact successful startup
`RecallResponse`. asyncio cancellation is **not** swallowed (it propagates
normally); only ordinary `Exception` subclasses are fail-open. No
`receipt_id`, `manifest_hash`, `packet_hash`, `receipt_status`,
`receipt_error`, or `verification_status` is added to `RecallResponse` (or
the SDK/MCP/Hermes contracts). Exposure is decided in ENG-CONTEXT-002C.

### Semantic exclusion

Semantic recall (`mode=semantic`) never invokes the dark write, never creates
a `context_receipts` row, and never emits a startup-receipt event, regardless
of whether the feature is enabled. No semantic Context Manifest is
implemented in this slice.

### Observability

**Structured logs are authoritative for every enabled attempt.** Exactly one
bounded structured log per enabled attempt carries `event`, `status`,
`tenant_id`, `principal_id`, `mode=startup`, `latency_ms`, `item_count`,
`failure_stage`, `exception_type`, `verification_status`, and
`telemetry_status` (plus `recall_log_id`/`receipt_id` once known). Failure
logs carry only the bounded `failure_stage` and exception *type* — never the
message. A bounded `context_receipt.dark_write` usage event is recorded
**best-effort** when `ENGRAM_USAGE_TELEMETRY_ENABLED=true` and sufficient
deadline remains. A hard timeout that exhausts the total deadline records
`telemetry_status=skipped_deadline` in the structured log and writes **no**
usage-event row. Neither source carries raw content, `working_set`, query
text, manifest JSON, canonical JSON, or exception messages.

## 15. Planned inspect/verify API in ENG-CONTEXT-003

ENG-CONTEXT-003 will add receipt inspect, verify, diff, and drift endpoints
plus SDK/MCP surfaces. The manifest contract defined here is what those
endpoints will return and verify against. Retrieval and authorized rehydration
begin only in ENG-CONTEXT-003.

## 16. Contract integrity (ENG-CONTEXT-001 correction)

Three invariants harden the contract so it cannot silently describe an
incoherent or wrongly typed response:

### Finalized-response coherence

Before constructing a manifest, the builder proves the finalized response is
internally consistent:

- `response.item_count == len(response.items)`;
- `response.byte_count == sum(len(content.encode("utf-8")) for each item)`;
- `response.working_set ==` the `working-set-v1` render of `response.items`
  (exact item order, kind, content, LF separators, no trailing newline,
  embedded newlines in content preserved, exact Unicode and whitespace).

The render comparison is an **integrity check** for `working-set-v1`. The
actual `packet.hash` is still computed directly over
`response.working_set.encode("utf-8")` — the reconstructed string is never
substituted as the hash preimage. A contradictory response (count, byte, or
packet mismatch; trailing newline; LF-vs-CRLF) is rejected before the manifest
is built.

### Startup subject/request coherence

- Startup `request.query_digest` must be `null`.
- `subject.workspace_id` must equal `request.effective.workspace_id`.
- `request.effective.item_budget` must be `null` for startup v1. A caller may
  *request* an item budget (the shared request model exposes it), but startup
  v1 does not enforce one, so the manifest must not falsely attest one.

### Strict typing (no silent coercion)

The builder validates the selected manifest fields exactly: `id`, `kind`,
`content`, `review_status`, and `visibility` must be strings; `authority` an
integer (not a Boolean); `pinned`/`human_verified` actual Booleans;
`reasons`/`warnings` lists of only strings; optional workspace/conflict fields
strings or null; score/trust fields finite numbers (not Booleans). Malformed
values are rejected — `"false"` is not coerced to `True`, `1` is not coerced to
a Boolean, a string is not iterated into a list of characters. Extra additive
fields on the loose recall item dictionary are ignored; only the selected
manifest fields are validated.

### Normative JSON Schema (source of truth)

The checked-in `schemas/context-manifest-v1.schema.json` is **generated** from
the strict wire model (`scripts/generate_context_manifest_schema.py`), not
hand-edited, so it cannot drift. A drift test and a `--check` mode fail CI if
the checked-in schema differs from the model. The schema enforces the required
`Literal` constants, canonical UUID/hash patterns, the `visibility` enum,
nonnegative counts, **profile all-or-none coherence** (see above), and rejects
unknown fields. `Draft202012Validator.check_schema` proves the generated schema
is itself valid Draft 2020-12 JSON Schema. Schema validation alone does
**not** verify packet or item hash preimages — the semantic verifier (Python
and JavaScript) performs those checks, and both reject the same shared negative
fixtures (canonical UUID/hash/visibility/count/budget/profile/typing violations).

> Note on golden vectors: vectors 001–009 kept their frozen expected hashes
> unchanged through the final conformance correction. Vector 010 was renamed
> from `010-lf-crlf-trailing-newline` to `010-embedded-newline-content`
> (filename, `name`, and `description` only); it always was a coherent
> multi-line packet proving exact-byte hashing of embedded-LF content, and its
> frozen expected hashes are unchanged. A programmatic golden-preservation test
> in `tests/test_context_manifest_golden.py` asserts every valid vector's
> `(manifest_hash, packet_hash)` equals the correction-start values.

### Profile all-or-none coherence (schema-encoded)

`memory_profile_id`, `memory_profile_revision_id`, and `memory_profile_version`
must be **all null** (unprofiled context) **or all non-null and valid**
(profiled context). The Python model enforces this with a `model_validator`;
the normative JSON Schema encodes it deterministically with a `oneOf` on the
`ContextManifestSubjectV1` definition (augmented after `model_json_schema`,
which cannot emit the semantic validator). Both the strict parser and the
normative schema therefore reject every partial-profile combination; the
Python and JavaScript conformance verifiers reject the same combinations
through the shared negative-fixture set.
