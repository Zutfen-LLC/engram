# Semantic Context Manifest v1

`semantic-context-manifest-v1` is an independent served-context contract.
It does not change the frozen startup `context-manifest-v1` contract.

The semantic manifest is built only from the finalized semantic packet. It
does not retrieve candidates, evaluate admission, load relationships, rerun
packing, or read memory rows. Its order is the finalized response order.

The manifest contains no raw semantic query and no raw memory content. Query
identity is `sha256("engram.semantic-context-manifest-v1/query\\0" + exact UTF-8
query bytes)`. Each item has `served_content_hash`, which hashes its exact
served UTF-8 content. `packet.hash` hashes the exact UTF-8 `working_set`.
`request.request_digest` hashes the RFC 8785 canonical request descriptor.
The receipt envelope holds `manifest_hash`, which hashes RFC 8785 canonical
manifest JSON. The manifest excludes the receipt ID, timestamp, and recall-log
ID.

The `legacy` profile records only its actual trust-weighted serving facts. It
does not invent V2 admission or evidence data. The `governed` and
`exploratory` shapes can represent finalized V2 admission, evidence,
relationship, relevance, utility, and packing facts. This support is for
in-memory conformance only while those profiles remain shadow-only.

The token budget uses one cost for each selected item. The item cost is
`max(1, len(content.encode("utf-8")) // 4)`. The total token cost is the sum of
the item costs. The rendered `[kind] ` prefix and the newline separator do not
consume this token budget. The byte budget uses the exact UTF-8 content byte
count for each selected item.

The manifest models reject unknown fields at every modeled level. They also
validate identity agreement, V2 projection agreement, packing counts, and
profile-specific admission fields. Canonical V2 risk, retention, tier,
next-action, assertion-mode, origin, outcome, profile-key, warning, review,
conflict, relationship-origin, and packing-reason vocabularies are closed at
this contract version. Legacy manifests reject candidate protocol identities
and per-item candidate facts; governed and exploratory manifests require exact
`recall-admission-v2` and `recall-packing-v1` identities and complete selected
item bindings. The parser rejects unsupported schema, schema-version, and
manifest-contract combinations.

`ENGRAM_SEMANTIC_CONTEXT_RECEIPT_DARK_WRITE_ENABLED=false` is the default.
When enabled, only authoritative semantic recall can persist a receipt after
the recall log commits. The writer uses a separate application-role session
and reloads and verifies the immutable row before commit. A write failure is
fail-open and does not change the response, selected packet, or exposure
counters. Shadow comparison does not call the writer.

A semantic receipt proves what Engram served and which recorded policy facts
authorized it. It does not prove truth, reliance, usefulness, or causality.

## Deployment and rollback

Migration 042 widens Context Receipt storage for semantic rows. This widening
is forward-only. The downgrade removes `recall_logs.item_budget`. It retains
semantic receipt rows and the widened receipt constraints.

Keep `ENGRAM_SEMANTIC_CONTEXT_RECEIPT_DARK_WRITE_ENABLED=false` until all API
instances understand migration 042 and the semantic manifest parser. Do not
deploy an old startup-only receipt reader after semantic rows exist.

To roll back the feature, disable semantic capture first. Existing semantic
history remains available to compatible readers. The system does not backfill
historical semantic recalls. Startup receipt capture and rollback behavior do
not change.
