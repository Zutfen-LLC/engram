# Service delegation

Service delegation lets an external control plane perform exactly one ordinary
Core read as an existing service-bound human. It does not create a principal,
tenant, workspace, membership, API key, or memory profile.

## Credential domains

Core has four separate bearer grammars:

- `eng_` is an ordinary tenant-principal API key with persisted scopes.
- `engsvc_` authenticates a non-principal service client to narrow provisioning
  or delegation routes.
- `engd_` is an opaque, database-backed, single-use delegated credential. It
  authenticates as one existing bound `type=user` principal with scope exactly
  `read` and audience `engram-core`. Its exact Bearer-compatible grammar is
  `engd_<22-character-base62-key-id>_<43-character-URL-safe-secret>`.
- `engdr_` is an opaque, database-backed, single-use review credential. It
  has scope exactly `review`. It is bound to one `review.queue` or
  `review.transition` purpose. Its grammar is
  `engdr_<22-character-base62-key-id>_<43-character-URL-safe-secret>`.

An `engd_` credential is never a service credential or ordinary API key.
Service routes accept only strict `engsvc_` credentials. Delegated material is
dispatched before ordinary API-key parsing and never reaches the legacy bcrypt
fallback or principal cache.

## Broker and binding owner

Use separate service clients:

- The binding owner provisions and owns the tenant and human bindings. It keeps
  the five provisioning permissions and does not need `delegation.issue`.
- The broker receives only `delegation.issue`. It cannot provision or mutate
  the binding owner's Core resources.

Grant the broker permission explicitly, then create an owner-controlled grant:

```bash
engram service-client set-permissions control-plane-broker \
  --permission delegation.issue

engram delegation-grant create \
  --issuer control-plane-broker \
  --binding-owner control-plane-provisioner \
  --max-ttl-seconds 60
```

Every command requires `ENGRAM_OWNER_DATABASE_URL`. Existing clients receive no
new permission or grant during migration. A grant's TTL cannot be edited:
revoke it and create a new row.

```bash
engram delegation-grant list --issuer control-plane-broker

engram delegation-grant revoke \
  --issuer control-plane-broker \
  --binding-owner control-plane-provisioner \
  --reason operator_action
```

Revoked grants are never reactivated.

## Issuance and use

Enable the feature only after migration 029 is present:

```text
ENGRAM_SERVICE_PROVISIONING_ENABLED=true
ENGRAM_DELEGATION_ENABLED=true
ENGRAM_DELEGATION_DEFAULT_TTL_SECONDS=60
ENGRAM_DELEGATION_MAX_TTL_SECONDS=300
```

`POST /v1/service/delegations` requires the broker's `engsvc_` credential and a
fresh `Idempotency-Key`. A successful creation returns the `engd_` credential
once with `201`. Core stores only its indexed key ID and SHA-256 secret digest.
Every replay or exact external-reference reconciliation returns `200`,
`credential_secret_available=false`, and no token; it never extends expiry.
Responses carry `no-store`, `no-cache`, `no-referrer`, request-ID, and replay
headers.

Every ordinary HTTP request that presents an `engd_` Bearer attempt receives
the same `no-store`, `no-cache`, `no-referrer`, and `X-Request-ID` response
boundary, including authentication, validation, scope, routing, method, and
bounded internal failures. The boundary recognizes the reserved credential
prefix before authentication but does not validate, retain, authenticate, or
consume the token. It establishes one validated or generated request ID that
is shared by the response and delegation consumption or denial evidence.
Ordinary `eng_` API-key response headers remain unchanged.

The token is consumed atomically during successful authentication, before route
scope evaluation or handler execution. Therefore a write, review, export,
admin, or service-route attempt cannot execute and still consumes that token.
A route failure after authentication does not restore it. Retry a read only by
issuing a new delegated token.

Two concurrent uses yield one authenticated request and one generic `401`.
Use and explicit revocation serialize on the same token row, so use-first or
revoke-first are the only outcomes. A read authenticated before a later
revocation commit may finish.

## Lost-response recovery

If issuance may have committed but its response was lost:

1. Replay the same idempotency key to resolve the original metadata. Core
   cannot return its plaintext again.
2. Revoke the same tenant, principal, and delegation external references with
   reason `response_uncertain`.
3. Issue a replacement using a new delegation external reference and a new
   idempotency key.

The revoke endpoint truthfully returns `revoked`, `already_revoked`,
`already_used`, or `not_found` and never returns internal Core identity.

## Authority termination

An unused token fails authentication after any of these events:

- first successful authentication;
- explicit token revocation or expiry;
- issuer credential revocation or expiry;
- issuer or binding-owner disablement;
- removal of `delegation.issue`;
- delegation-grant revocation;
- binding or human-principal integrity failure.

Authority loss is terminal for every token that was active at the time. Database
triggers revoke affected tokens during explicit permission, client, credential,
grant, or subject-authority changes, so restoring the prior state does not
restore a token. If authentication itself first discovers invalid authority
(for example, a credential expires by passage of time), that same transaction
marks the token revoked before returning `401`. A fresh token is always required.

All caller responses remain generic. Append-only events contain bounded reason
codes and SHA-256 external-reference digests, never raw references, raw
idempotency keys, credentials, tokens, headers, bodies, URLs, database URLs, or
exception text.

## Purpose-bound review step-up

Review delegation is disabled by default. Enable it only after migration 030:

```text
ENGRAM_REVIEW_DELEGATION_ENABLED=true
ENGRAM_REVIEW_DELEGATION_DEFAULT_TTL_SECONDS=30
ENGRAM_REVIEW_DELEGATION_MAX_TTL_SECONDS=60
```

Use a separate review broker when possible. Grant only
`delegation.review.issue` to that client. Then create a review grant:

```bash
engram service-client set-permissions review-broker \
  --permission delegation.review.issue

engram delegation-grant create \
  --issuer review-broker \
  --binding-owner control-plane-provisioner \
  --authority-class review \
  --max-ttl-seconds 60
```

`POST /v1/service/review-delegations` accepts one fixed purpose. The
`review.queue` purpose permits only `GET /v1/review/queue` with no query
parameters. The `review.transition` purpose permits one exact request to
activate or reject one proposed item. It binds the item ID, target state, and
reason.

Core consumes a matching token before route execution. A purpose mismatch
revokes the token and returns the generic delegated `401`. A later `404` or
`409` does not restore the token.

The review event actor is always the bound human. Internal event columns record
the review token, grant, authority class, and purpose. API responses do not
expose those columns.

Read and review grants are separate. `delegation.issue` cannot issue or revoke
a review token. `delegation.review.issue` cannot issue or revoke a read token.

Browser delivery, refresh tokens, token chaining, offline introspection, and
Portal proxying are not part of this Core slice.
