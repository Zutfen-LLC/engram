# Service provisioning

Core exposes three generic external-control-plane operations:

- `POST /v1/service/provisioning/tenant-human` creates or resolves a tenant and
  human (`type=user`) principal.
- `POST /v1/service/provisioning/workspace-agent` creates or resolves one
  workspace, agent principal, and `member` workspace membership inside a
  service-owned tenant.
- `POST /v1/service/provisioning/agent-api-key` issues or atomically replaces
  the fixed `read` + `write` ordinary API key for that workspace-agent pair.

All accept `Authorization: Bearer engsvc_<key_id>_<secret>` and a required
`Idempotency-Key`. Service permissions are `tenant.provision`,
`principal.provision`, `workspace.provision`, `agent.provision`, and
`api_key.provision`; these are not tenant API-key scopes. An `engsvc_`
credential cannot use ordinary Core routes, and an `eng_` credential cannot
use service routes.

Requests contain opaque external references and Core names/slugs, never caller
selected Core resource UUIDs. References are scoped to the authenticated
service client. Core never adopts an existing resource by matching a name or
slug. Unknown and cross-service tenant bindings are indistinguishable.

Responses are cache-disabled and return `201` for creation, `200` for
reconciliation or an idempotent replay, and `Idempotency-Replayed: true` for a
replay. Every response has `Cache-Control: no-store`, `Pragma: no-cache`,
`Referrer-Policy: no-referrer`, and an `X-Request-ID`. Validation failures use
`INVALID_REQUEST` without echoing input.

Binding rows retain raw external references because reconciliation requires
them. Idempotency records store only a key digest, and provisioning audit
events store only SHA-256 external-reference digests. Events are append-only;
a deterministic conflict rolls back resource mutations in a savepoint, then
commits one bounded conflict event and the locked credential's
`last_used_at` update together in the enclosing authenticated transaction.

Stable workspace/agent resources are intentionally separate from credential
issuance. Workspace-agent replays always return the same resource identifiers.
An API-key plaintext secret exists only in the successful `201` creation
response. Replays and reconciliation return
`credential_secret_available=false` and `key=null`; Core cannot reconstruct or
read the secret.

If delivery is lost, call the API-key operation with a new API-key external
reference and `replaces_external_ref` naming the active binding. Core revokes
the prior key, marks its binding replaced, and creates its successor in one
transaction. At most one service-provisioned key is active for a
workspace-agent pair. Database revocation is authoritative; other replicas
observe it within the configured API-key cache TTL.

The database enforces this relationship with deferred constraint triggers, not
application ordering alone. Provisioner-created key rows must have one matching
binding at commit; tenant, agent principal, fixed scopes, absent memory profile,
revocation state, and replacement lineage must agree across both tables.
Partial revocation/replacement and a second unbound active key are rejected.

These routes do not grant browser authority, create memory or agent activity,
bind a memory profile, or implement Portal orchestration. See
`docs/service-agent-onboarding.md` for request contracts and the lost-delivery
runbook.
