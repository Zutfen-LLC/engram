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
