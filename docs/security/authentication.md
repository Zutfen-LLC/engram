# Authentication domains

Engram uses three non-interchangeable bearer formats.

| Prefix | Identity | Authority | Persistence and caching |
|---|---|---|---|
| `eng_` | Tenant principal | Ordinary API scopes | Key ID plus digest for new keys; successful principals may use the short API-key cache |
| `engsvc_` | Service client | Provisioning or `delegation.issue` permissions | Key ID plus digest; accepted only by service routes |
| `engd_` | Existing bound human principal | Exactly `read`, once | Key ID plus digest; never cached or refreshed |

Malformed or unknown delegated credentials receive the same generic `401` as
invalid ordinary credentials, but are never interpreted as legacy `eng_` keys.
Service routes use the strict service parser and reject delegated credentials.

Delegated authentication locks the token and every current authority row,
verifies the secret in constant time, transitions the active token to `used`,
appends a bounded event, and commits before the ordinary route receives the
principal. The request then uses the existing tenant/principal RLS context.

See [service delegation](../ops/service-delegation.md) for issuance, revocation,
single-use behavior, invalidation, and recovery.
