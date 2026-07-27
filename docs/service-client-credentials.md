# Service-client credentials

Service clients authenticate with one-time `engsvc_...` credentials and a
canonical permission set. Create, rotate, revoke, disable, and explicitly
replace permissions only through the owner database CLI documented in
`docs/ops/service-client-credentials.md`.

New clients retain the compatibility default of `tenant.provision` and
`principal.provision`. Grant workspace/agent onboarding only with
`engram service-client set-permissions`; no migration or enable operation
silently expands an existing client's authority.
