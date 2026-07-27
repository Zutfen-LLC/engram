# Service agent onboarding

This runbook is for a trusted service client operating inside a tenant that it
already owns through `tenant_provisioning_bindings`. It is a generic Core API;
Portal orchestration and browser delegation remain separate work.

## Permissions

Existing clients retain their prior permissions. Grant onboarding authority
explicitly with the owner-only command:

```bash
engram service-client set-permissions control-plane \
  --permission tenant.provision \
  --permission principal.provision \
  --permission workspace.provision \
  --permission agent.provision \
  --permission api_key.provision
```

The workspace-agent endpoint requires `workspace.provision` and
`agent.provision`. The credential endpoint requires `api_key.provision`.

## Create or reconcile stable resources

Call `POST /v1/service/provisioning/workspace-agent` with a service credential,
a fresh `Idempotency-Key`, and:

```json
{
  "tenant_external_ref": "opaque-tenant-reference",
  "workspace": {
    "external_ref": "opaque-workspace-reference",
    "name": "Workspace",
    "slug": "workspace"
  },
  "agent": {
    "external_ref": "opaque-agent-reference",
    "name": "Agent"
  }
}
```

The tenant UUID is never caller-supplied. Core resolves the tenant through the
authenticated service client's binding, creates only unbound resources, and
never adopts an existing workspace or principal by name or slug. A replay
returns the same workspace, agent, and membership IDs.

## Issue the ordinary agent key

Call `POST /v1/service/provisioning/agent-api-key` with a different
`Idempotency-Key`:

```json
{
  "tenant_external_ref": "opaque-tenant-reference",
  "workspace_external_ref": "opaque-workspace-reference",
  "agent_external_ref": "opaque-agent-reference",
  "api_key": {
    "external_ref": "opaque-key-reference",
    "label": "agent runtime",
    "replaces_external_ref": null
  }
}
```

Core fixes the key scopes to `read` and `write` and leaves
`memory_profile_id=null`. The `eng_...` plaintext is returned only with the
`201` that creates the key. Store it before discarding the response. A retry or
reconciliation returns the same key metadata with
`credential_secret_available=false` and `key=null`.

The external reference binds the complete immutable request, including
`label`, fixed scopes, no memory profile, and replacement lineage. Reusing an
external reference with a changed label or lineage returns
`API_KEY_EXTERNAL_REF_CONFLICT`; Core does not create an idempotency record for
the changed request.

## Recover from lost delivery

Plaintext is deliberately unrecoverable. Replace the key explicitly with a new
external reference and idempotency key:

```json
{
  "tenant_external_ref": "opaque-tenant-reference",
  "workspace_external_ref": "opaque-workspace-reference",
  "agent_external_ref": "opaque-agent-reference",
  "api_key": {
    "external_ref": "opaque-replacement-reference",
    "label": "agent runtime replacement",
    "replaces_external_ref": "opaque-key-reference"
  }
}
```

Revocation and successor creation commit atomically. Losing this response has
the same semantics: replay reports the secret unavailable, and a later
replacement may replace that active successor. Never delete the workspace,
agent, membership, or bindings to compensate for an ambiguous response.

Deferred database constraints validate the final key/binding state at commit.
They reject split revocation or binding updates, unbound provisioner-created
keys, mismatched tenant/principal identity, non-fixed key policy, fabricated
lineage, and any replacement that does not leave one active unrevoked
successor.
