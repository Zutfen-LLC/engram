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

`ENGRAM_SEMANTIC_CONTEXT_RECEIPT_DARK_WRITE_ENABLED=false` is the default.
When enabled, only authoritative semantic recall can persist a receipt after
the recall log commits. The writer uses a separate application-role session
and reloads and verifies the immutable row before commit. A write failure is
fail-open and does not change the response, selected packet, or exposure
counters. Shadow comparison does not call the writer.

A semantic receipt proves what Engram served and which recorded policy facts
authorized it. It does not prove truth, reliance, usefulness, or causality.
