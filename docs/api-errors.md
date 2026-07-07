# API error contract

Engram's REST API returns a stable JSON body for every error response. This
document is the contract client code (including the SDK) can rely on.

## Shape

Every error response is a JSON object with a `detail` key:

```json
{"detail": <string | object | array>}
```

`detail` takes one of three shapes, depending on where the error originated:

1. **Plain string** — an explicit business-logic error raised by a route
   (e.g. `HTTPException(status_code=404, detail="Item not found")`).

   ```json
   {"detail": "Item not found"}
   ```

2. **Structured object** — a database constraint failure classified by the
   centralized error handlers in `engram/api/errors.py`.

   ```json
   {
     "detail": {
       "message": "request rejected by database constraint (check_violation): chk_kind",
       "code": "check_violation",
       "constraint": "chk_kind"
     }
   }
   ```

   `code` is one of: `check_violation`, `not_null_violation`,
   `foreign_key_violation`, `unique_violation`, `exclusion_violation`,
   `invalid_value`. `constraint` is the Postgres constraint name and is
   omitted when the database didn't report one (e.g. malformed literals).

3. **Pydantic validation array** — FastAPI's default 422 body for request
   bodies that fail schema validation (wrong type, missing required field,
   etc.), unrelated to the database:

   ```json
   {"detail": [{"loc": ["body", "limit"], "msg": "...", "type": "..."}]}
   ```

## Status code mapping

| Situation | Status | `detail` shape |
|---|---|---|
| Request fails Pydantic schema validation | 422 | array |
| Route-level business rule (not found, missing context, etc.) | 404 / 403 / 422 | string |
| DB `CHECK` constraint violation (SQLSTATE `23514`) | 422 | object, `code=check_violation` |
| DB `NOT NULL` violation (SQLSTATE `23502`) | 422 | object, `code=not_null_violation` |
| DB foreign-key violation — unknown client-provided ID/slug (SQLSTATE `23503`) | 422 | object, `code=foreign_key_violation` |
| DB unique-constraint conflict not handled by route-level dedup logic (SQLSTATE `23505`) | 409 | object, `code=unique_violation` |
| DB exclusion-constraint conflict (SQLSTATE `23P01`) | 409 | object, `code=exclusion_violation` |
| Malformed literal value (bad enum/UUID text, out-of-range number, bad date — SQLSTATE class `22`) | 422 | object, `code=invalid_value` |
| Anything else (connection failure, driver bug, unrecognized SQLSTATE) | 500 | plain text (no body contract) |

The `remember` and diary-write endpoints have one documented exception: a
unique-index hit on the write-path dedup index (`idx_memitems_dedup`) is
**not** an error — it's normal idempotent-retry behavior and returns
`200`/`201` with `status: "deduped"`.

## Design notes

- Centralized handling classifies `IntegrityError`/`DataError` by Postgres
  SQLSTATE rather than string-matching driver messages, so classification is
  stable across Postgres error message wording. An unrecognized SQLSTATE is
  never guessed into a 4xx — it's left to surface as the 500 it actually is,
  so genuine server faults are never mislabeled as client errors.
- Routes that need business logic before deciding whether a constraint hit
  is expected (e.g. `remember`'s dedup check) still catch `IntegrityError`
  themselves; they re-raise anything that isn't the case they're handling so
  it reaches the centralized classifier.

## SDK mapping

`engram_client.EngramClient` raises a typed exception per status code:

| Status | Exception |
|---|---|
| 401 / 403 | `EngramAuthError` |
| 404 | `EngramNotFoundError` |
| 422 | `EngramValidationError` |
| 409 and other 4xx | `EngramClientError` |
| 5xx | `EngramServerError` |

All of these subclass `EngramHTTPError`, which exposes `.status_code`,
`.detail` (the parsed `detail` value — string, object, or array, as above),
and `.body` (the full parsed response body).
