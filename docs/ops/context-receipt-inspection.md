# Context Receipt Inspection and Verification API (ENG-CONTEXT-003A)

> **Scope:** Read-only REST API exposing the Context Ledger receipt substrate.
> This is the first backend surface for the future management portal.

## Endpoints

### List receipts

```
GET /v1/context-receipts?limit=50&cursor=<opaque>&recall_log_id=<uuid>
```

Lists the authenticated principal's readable receipts, newest-first.

**Query parameters:**

| Parameter       | Type   | Default | Constraints          |
|-----------------|--------|---------|----------------------|
| `limit`         | int    | 50      | 1–100                |
| `cursor`        | string | null    | Opaque keyset cursor |
| `recall_log_id` | UUID   | null    | Optional filter      |

**Profile narrowing:**

- An **unprofiled** credential (both `memory_profile_id` and
  `memory_profile_revision_id` null) may inspect any receipt owned by the same
  tenant and principal.
- A **profile-bound** credential (both non-null) may inspect only receipts
  whose manifest subject carries the exact same `memory_profile_id` AND
  `memory_profile_revision_id` — including null state. A profiled credential
  cannot read unprofiled receipts, receipts from another profile, or receipts
  from an older/newer revision.
- A **partially specified** context (exactly one null, one non-null) is an
  invalid state. The API fails closed with a `401` response — it never
  returns unrestricted receipts from a partial profile context.

**Response:**

```json
{
  "items": [
    {
      "id": "uuid",
      "recall_log_id": "uuid",
      "created_at": "2026-07-24T12:00:00+00:00",
      "retention_expires_at": null,
      "manifest_schema": "engram.context-manifest",
      "manifest_schema_version": "1.0",
      "canonicalization": "rfc8785",
      "mode": "startup",
      "manifest_hash": "sha256:...",
      "packet_hash": "sha256:...",
      "manifest_parse_status": "valid",
      "item_count": 3,
      "served_content_byte_count": 256,
      "rendered_packet_byte_count": 300,
      "workspace_id": null,
      "memory_context_version": "memory-context-v2",
      "memory_profile_id": null,
      "memory_profile_revision_id": null,
      "memory_profile_version": null,
      "scoring_version": "v1",
      "config_version": "v1"
    }
  ],
  "next_cursor": "eyJ2IjoxLCJjcmV..."
}
```

**Cursor behavior:**

- The cursor is an opaque URL-safe base64 payload containing the sort key
  (`created_at`, `id`) of the last row returned.
- Only cursors produced by this API's encoder are accepted. Timezone-aware
  but noncanonical timestamp spellings (Z-form `...Z`, non-UTC offsets like
  `+05:00`, trailing-zero fractional seconds like `.000000`) are rejected,
  even when they represent the same instant. This prevents ambiguity in
  keyset pagination.
- Malformed or unsupported cursors return `422` and are never silently
  ignored.
- Pagination is stable across equal timestamps (ordered by
  `created_at DESC, id DESC`).

**Malformed manifests:**

A malformed manifest does not make the entire timeline unavailable. For a
malformed row, the response includes envelope fields,
`manifest_parse_status="invalid"`, and nullable manifest-derived summary
fields.

**Empty lists:**

An empty list may mean no receipts have been dark-written — not an API
failure. Receipt creation still depends on
`ENGRAM_CONTEXT_RECEIPT_DARK_WRITE_ENABLED`.

### Inspect receipt

```
GET /v1/context-receipts/{receipt_id}
```

Inspects one receipt envelope and its exact stored manifest.

**Response:**

```json
{
  "id": "uuid",
  "recall_log_id": "uuid",
  "created_at": "2026-07-24T12:00:00+00:00",
  "retention_expires_at": null,
  "manifest_schema": "engram.context-manifest",
  "manifest_schema_version": "1.0",
  "canonicalization": "rfc8785",
  "mode": "startup",
  "manifest_hash": "sha256:...",
  "packet_hash": "sha256:...",
  "manifest_parse_status": "valid",
  "manifest": { ... }
}
```

The `manifest` field contains the exact stored JSON object — no
normalization or rehydrated content.

Inspection remains possible even when verification would fail.

**Non-disclosure:**

Cross-principal, cross-tenant, and profile-ineligible IDs return the same
`404` response as a missing receipt.

### Verify receipt

```
GET /v1/context-receipts/{receipt_id}/verify
```

Performs deterministic read-only verification of the stored artifact and its
parent recall-log binding.

**HTTP behavior:**

- `404` when the receipt is inaccessible (same non-disclosure as inspect).
- `200` with `status="valid"` or `status="invalid"` for an accessible receipt.
- Integrity failures are NOT converted to `500`.

**Response (valid):**

```json
{
  "receipt_id": "uuid",
  "status": "valid",
  "verification_scope": "stored_artifact_and_recall_log_binding",
  "checks": [
    {"code": "manifest_parse", "passed": true},
    {"code": "manifest_hash", "passed": true},
    {"code": "packet_hash_binding", "passed": true},
    {"code": "envelope_schema", "passed": true},
    {"code": "envelope_schema_version", "passed": true},
    {"code": "envelope_canonicalization", "passed": true},
    {"code": "envelope_mode", "passed": true},
    {"code": "subject_tenant", "passed": true},
    {"code": "subject_principal", "passed": true},
    {"code": "embedded_manifest_hash_absent", "passed": true},
    {"code": "recall_log_exists", "passed": true},
    {"code": "recall_log_binding", "passed": true}
  ],
  "failure_code": null,
  "stored_manifest_hash": "sha256:...",
  "recomputed_manifest_hash": "sha256:...",
  "stored_packet_hash": "sha256:...",
  "manifest_packet_hash": "sha256:...",
  "limitations": [
    "Does not prove factual truth.",
    "Does not prove the agent used the context.",
    "Does not prove the context caused an action.",
    "Does not reconstruct or verify historical raw packet bytes.",
    "Does not rehydrate current memory-item content in ENG-CONTEXT-003A."
  ]
}
```

**Response (invalid):**

```json
{
  "status": "invalid",
  "failure_code": "MANIFEST_HASH_MISMATCH",
  ...
}
```

## Verification checks

Every verification result — valid or invalid — includes all 12 check codes
exactly once in canonical order:

1. `manifest_parse`
2. `manifest_hash`
3. `packet_hash_binding`
4. `envelope_schema`
5. `envelope_schema_version`
6. `envelope_canonicalization`
7. `envelope_mode`
8. `subject_tenant`
9. `subject_principal`
10. `embedded_manifest_hash_absent`
11. `recall_log_exists`
12. `recall_log_binding`

On a manifest parse failure, checks that require a parsed manifest are set
to `false` (they cannot be established), while `embedded_manifest_hash_absent`
is set truthfully from the raw stored JSON. This guarantees a complete matrix
for every outcome — no abbreviated check lists.

| Check code                    | Description                                    |
|-------------------------------|------------------------------------------------|
| `manifest_parse`              | The stored JSONB parses as `ContextManifestV1` |
| `manifest_hash`               | Recomputed RFC 8785 hash matches stored hash   |
| `packet_hash_binding`         | `manifest.packet.hash` matches `packet_hash`   |
| `envelope_schema`             | Manifest schema matches envelope column        |
| `envelope_schema_version`     | Manifest schema_version matches envelope       |
| `envelope_canonicalization`   | Canonicalization matches envelope column       |
| `envelope_mode`               | Mode matches envelope column                   |
| `subject_tenant`              | Manifest subject tenant matches receipt        |
| `subject_principal`           | Manifest subject principal matches receipt     |
| `embedded_manifest_hash_absent` | No `manifest_hash` field inside the manifest |
| `recall_log_exists`           | The parent recall log exists under ownership   |
| `recall_log_binding`          | Recall-log overlap fields match the manifest   |

## Stable failure codes

| Code                                   | First failing check                     |
|----------------------------------------|-----------------------------------------|
| `MANIFEST_PARSE_FAILED`                | `manifest_parse`                        |
| `MANIFEST_HASH_MISMATCH`               | `manifest_hash`                         |
| `PACKET_HASH_BINDING_MISMATCH`         | `packet_hash_binding`                   |
| `ENVELOPE_PROTOCOL_MISMATCH`           | `envelope_schema`/`version`/`canon`/`mode` |
| `SUBJECT_OWNERSHIP_MISMATCH`           | `subject_tenant` or `subject_principal` |
| `EMBEDDED_MANIFEST_HASH_FORBIDDEN`     | `embedded_manifest_hash_absent`         |
| `RECALL_LOG_NOT_FOUND`                 | `recall_log_exists`                     |
| `RECALL_LOG_BINDING_MISMATCH`          | `recall_log_binding`                    |

## Scope and auth

All three routes require the `read` scope. The `admin` super-scope satisfies
the read scope but does NOT bypass principal isolation — an admin principal
sees only receipts owned by that principal in this slice.

## Privacy

- No raw memory content, raw query text, or raw working_set is added to
  responses.
- Manifest JSON, item IDs, packet hashes, and exception text are never logged
  at info level.
- Database exception details are never returned.
- Verification is not a truth certificate.

## SDK usage

```python
from engram_client import EngramClient

async with EngramClient("https://api.engram.example", api_key="eng_...") as client:
    # List receipts
    result = await client.list_context_receipts(limit=10)

    # Inspect one receipt
    detail = await client.get_context_receipt(receipt_id)

    # Verify
    verify = await client.verify_context_receipt(receipt_id)
    if verify.status == "invalid":
        print(f"Integrity failure: {verify.failure_code}")
```

## Receipt creation

Receipts are created by the dark-write path
(`ENGRAM_CONTEXT_RECEIPT_DARK_WRITE_ENABLED`). This API does not create,
modify, or delete receipts. No production recall or receipt-write behavior
changed in this slice.
