# Authentication domains

Engram uses three non-interchangeable bearer formats.

| Prefix | Identity | Authority | Persistence and caching |
|---|---|---|---|
| `eng_` | Tenant principal | Ordinary API scopes | Key ID plus digest for new keys; successful principals may use the short API-key cache |
| `engsvc_` | Service client | Provisioning or `delegation.issue` permissions | Key ID plus digest; accepted only by service routes |
| `engd_` | Existing bound human principal | Exactly `read`, once | Key ID plus digest; never cached or refreshed |

Delegated credentials use the Bearer-compatible grammar
`engd_<22-character-base62-key-id>_<43-character-URL-safe-secret>`.
Malformed or unknown delegated credentials receive the same generic `401` as
invalid ordinary credentials, but are never interpreted as legacy `eng_` keys.
Service routes use the strict service parser and reject delegated credentials.

Before authentication, the response boundary classifies only a Bearer attempt
whose credential begins with the reserved `engd_` prefix. Classification does
not parse, validate, hash, persist, log, authenticate, or consume the
credential. This means malformed delegated Bearer attempts receive the same
private response contract as valid delegated requests:

- `Cache-Control: no-store`
- `Pragma: no-cache`
- `Referrer-Policy: no-referrer`
- `X-Request-ID: <validated caller ID or generated UUID>`

The boundary establishes the effective request ID once. Delegated
authentication uses that same value for consumption or denial audit evidence,
and the response echoes it. Invalid caller-supplied IDs are replaced rather
than echoed. Ordinary `eng_` API-key responses are unchanged.

Delegated authentication locks the token and every current authority row,
verifies the secret in constant time, transitions the active token to `used`,
appends a bounded event, and commits before the ordinary route receives the
principal. The request then uses the existing tenant/principal RLS context.
Authority invalidation is terminal: restoring a permission, client, credential,
grant, or subject does not reactivate an affected token.

See [service delegation](../ops/service-delegation.md) for issuance, revocation,
single-use behavior, invalidation, and recovery.
