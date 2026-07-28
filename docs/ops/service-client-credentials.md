# Service-client credentials

Create a control-plane client with the owner-only CLI:

```bash
engram service-client create --slug control-plane --display-name "Control plane"
```

Every `service-client` subcommand requires `ENGRAM_OWNER_DATABASE_URL`; it
never falls back to `ENGRAM_DATABASE_URL`. Unknown client or credential targets
return a non-zero status. Repeating a state transition for an existing target
is an idempotent success (`already disabled`, `already enabled`, or `already
revoked`).

The printed `engsvc_...` credential is shown once. Store it in the control
plane’s secret manager; Core stores only its key identifier and SHA-256 digest.
Rotate without downtime using `engram service-client rotate-key <slug>`, then
move callers to the new credential and revoke the old one by UUID or key ID:

```bash
engram service-client revoke-key <credential-id-or-key-id>
```

`disable` immediately invalidates every credential for a client; `enable`
re-enables the client but never restores revoked credentials. Do not pass a raw
credential on a command line, in logs, or to a browser.

New clients default to only `tenant.provision` and `principal.provision`.
Existing clients are never silently upgraded. Replace a client's complete
permission set explicitly:

```bash
engram service-client set-permissions control-plane \
  --permission tenant.provision \
  --permission principal.provision \
  --permission workspace.provision \
  --permission agent.provision \
  --permission api_key.provision
```

The command locks the client row, canonicalizes the replacement set, and
records one `service_client.permissions_changed` event only when the set
changes. It does not rotate, revoke, or print credentials.

A delegation broker is a separate client with only `delegation.issue`; do not
add that permission to the provisioning binding owner. See
`docs/ops/service-delegation.md`.
