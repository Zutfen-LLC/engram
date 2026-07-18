# Context Manifest v1 — Conformance Vectors

Language-neutral golden vectors for the Engram **Context Manifest** contract
(`engram.context-manifest`, `context-manifest-v1`). See
[`docs/context-manifest-v1.md`](../../docs/context-manifest-v1.md) for the
normative contract.

## What this directory proves

Each vector pins a *frozen* expected output for a finalized recall response +
decision context. Two independent verifiers — one Python, one JavaScript —
must reproduce **every** expected hash from the vector's inputs alone:

| Value | Independently recomputed by both verifiers from… |
|---|---|
| `manifest_hash` | RFC 8785 canonical bytes of `expected.manifest` |
| `packet_hash` | exact UTF-8 bytes of `input.response.working_set` |
| `request_digest` | RFC 8785 canonical bytes of the request descriptor (without `request_digest`) |
| `served_content_hash[i]` | exact UTF-8 bytes of `input.response.items[i].content` |
| `canonical_json` | the canonical bytes of the manifest object |

The JavaScript verifier implements RFC 8785 JCS and SHA-256 from scratch using
only the Node standard library — it never calls the Python implementation.
Agreement between the two verifiers is the cross-language proof that the
contract is language-neutral.

## Vector inventory

| # | File | Scenario |
|---|---|---|
| 1 | `001-empty-startup.json` | Empty startup recall (empty packet → SHA-256 of zero bytes). |
| 2 | `002-mixed-pinned-scored.json` | Mixed pinned (`score=null`) and scored items. |
| 3 | `003-profile-bound-workspace.json` | Profile-bound workspace recall (non-null profile + workspace). |
| 4 | `004-unicode-and-escaping.json` | Unicode content and JSON escaping (emoji, non-ASCII, quotes, backslash, newline). |
| 5 | `005-null-fields.json` | Null optional fields (explicit `null` serialization). |
| 6 | `006-number-boundaries.json` | `-0` (→ `0`), integer-valued floats, small/large finite floats. |
| 7 | `007-key-order-equivalence.json` | Key insertion order does not change canonical bytes. |
| 8 | `008-item-order-change.json` | Item-order change → distinct `manifest_hash` and `packet_hash`. |
| 9 | `009-whitespace-case-content.json` | Whitespace/case content: exact-byte `served_content_hash` (no normalization). |
| 10 | `010-embedded-newline-content.json` | Embedded LF inside an item's content is preserved verbatim in a coherent `working-set-v1` packet (no trailing packet newline; LF separates rendered items). Proves embedded LF is preserved exactly in `served_content_hash` and `packet_hash`. Trailing-packet-newline and CRLF-separator variants are rejected by the builder — they are negative cases, not valid vectors. |

## Negative fixtures

`negative/*.json` is a **language-neutral rejection set**. Each fixture carries
a valid base `input` with exactly one field mutated to violate the v1 contract
(canonical UUID, SHA-256, visibility vocabulary, nonnegative count/budget,
profile all-or-none coherence, strict scalar types, startup invariants). Both
verifiers must reject every fixture — agreement is the cross-language rejection
proof. Negative fixtures carry NO expected valid hashes.

| Fixture | Rejected invariant |
|---|---|
| `malformed-tenant-uuid` / `uppercase-tenant-uuid` | canonical UUID (subject) |
| `malformed-item-uuid` | canonical UUID (item) |
| `invalid-visibility` | visibility vocabulary |
| `negative-response-item-count` / `negative-response-byte-count` / `negative-omission-count` | nonnegative counts |
| `negative-requested-budget` / `negative-effective-budget` | nonnegative budgets |
| `non-null-effective-startup-item-budget` | startup v1 effective `item_budget` is null |
| `profile-*-only` / `profile-*-no-*` (six variants) | profile all-or-none coherence |
| `malformed-sha256` / `uppercase-sha256` | canonical SHA-256 |
| `string-boolean` / `mixed-type-reasons` | strict scalar typing |

## Running the verifiers

Both verifiers independently reconstruct the manifest from each vector's
inputs, validate response coherence (item count, byte count, working-set-v1
render), and re-derive every hash. The Python verifier additionally validates
each frozen manifest against the normative JSON Schema and round-trips it
through `model_validate`/`model_validate_json`.

```bash
# Python: build + schema-validate + wire round-trip + all hash re-derivation
python scripts/verify_context_manifest_vectors.py

# JavaScript: full independent reconstruction + coherence + RFC 8785 + SHA-256
#             (Node stdlib only, no npm install)
node conformance/context-manifest-v1/verify.mjs

# Negative fixtures (rejection proofs): Python and JavaScript must both reject
python scripts/verify_context_manifest_negatives.py
node conformance/context-manifest-v1/verify_negatives.mjs

# Cross-language agreement driver (runs both negative runners and checks exit 0)
python conformance/context-manifest-v1/run_cross_language.py

# Normative JSON Schema drift guard
python scripts/generate_context_manifest_schema.py --check
```

All must exit 0. The hosted CI `conformance-vectors` job runs all of the above
against the same checked-in artifacts on every change; the parallel `lock-drift`
job verifies `uv.lock` matches `pyproject.toml`.

## Regenerating the schema

The checked-in `schemas/context-manifest-v1.schema.json` is **generated** from
the strict wire model — never hand-edited — so it cannot drift:

```bash
python scripts/generate_context_manifest_schema.py            # regenerate
python scripts/generate_context_manifest_schema.py --check     # drift guard
```

## Regenerating vectors

Vectors are generated by `scripts/generate_context_manifest_vectors.py`,
which freezes the expected hashes from the reference Python builder:

```bash
python scripts/generate_context_manifest_vectors.py
```

**Golden vectors are immutable once released.** A field addition/removal,
semantic change, canonicalization change, render change, or number-format
change requires an explicit contract-version decision — not a silent rewrite
of published expected hashes. See `docs/context-manifest-v1.md` §Versioning.
