# Service provisioning

`POST /v1/service/provisioning/tenant-human` is the generic boundary for a
trusted external control plane to create or resolve one Core tenant and one
human (`type=user`) principal. It is not a Portal, browser, subscription, or
workspace API.

It accepts `Authorization: Bearer engsvc_<key_id>_<secret>` and a required
`Idempotency-Key`. Service credentials have only the `tenant.provision` and
`principal.provision` permissions; these are not tenant API-key scopes. An
`engsvc_` credential cannot use ordinary Core routes, and an `eng_` credential
cannot use this route.

The request contains opaque tenant and human external references plus immutable
Core display names and tenant slug. References are scoped to the authenticated
service client. Core never adopts an existing tenant by matching a name or slug:
an unbound slug produces `TENANT_SLUG_CONFLICT`. The same human reference may
therefore map to separate principals in separate tenants.

Responses are cache-disabled and return `201` for creation, `200` for
reconciliation or an idempotent replay, and `Idempotency-Replayed: true` for a
replay. Only SHA-256 digests of idempotency keys and external references are
persisted. New tenants receive the same builtin memory kinds and active
configuration as `POST /v1/admin/tenants`.
