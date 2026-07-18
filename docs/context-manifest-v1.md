# Context Manifest v1 — Canonical Served-Context Contract

> **Status (ENG-CONTEXT-001):** This slice *defines and proves* the contract.
> Durable persistence lands in ENG-CONTEXT-002; an inspect/verify API lands in
> ENG-CONTEXT-003. Only `mode="startup"` is supported here.

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
  with **no trailing newline**. The manifest hashes whatever the response
  actually returned: if a response's `working_set` ends with a trailing
  newline, the packet hash reflects it (see vector 010).
- LF vs CRLF differences and trailing-newline presence/absence all change
  `packet_hash` (and therefore `manifest_hash`).

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

## 14. Planned persistence in ENG-CONTEXT-002

ENG-CONTEXT-002 will introduce durable **receipts**: a `context_receipts`
table (and migration) storing the manifest, `manifest_hash`, `packet_hash`,
receipt ID, timestamps, and recall-log reference, with RLS and retention. The
deterministic manifest defined here is the foundation — ENG-CONTEXT-002 wraps
it in a volatile envelope without redesigning the hash contract.

## 15. Planned inspect/verify API in ENG-CONTEXT-003

ENG-CONTEXT-003 will add receipt inspect, verify, diff, and drift endpoints
plus SDK/MCP surfaces. The manifest contract defined here is what those
endpoints will return and verify against.

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
nonnegative counts, and rejects unknown fields. Schema validation alone does
**not** verify packet or item hash preimages — the semantic verifier (Python
and JavaScript) performs those checks.

> Note on golden vectors: vectors 001–009 kept their frozen expected hashes
> unchanged through this correction. Vector 010 was repurposed: its original
> packet had a trailing newline that did not match the `working-set-v1` render
> of its items, which the coherence check now (correctly) rejects. It is now a
> coherent multi-line packet proving exact-byte hashing; its preimage changed
> by necessity, and that is the only frozen-value change.
