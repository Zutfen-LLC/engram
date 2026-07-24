# Architecture Decision Records (Hosted Platform)

This directory holds ADRs for the Engram hosted platform (ENG-PORTAL-001A and
follow-up slices). Each ADR follows a fixed format:

- **Title**
- **Status:** Accepted
- **Date**
- **Context**
- **Decision**
- **Security and authorization consequences**
- **Operational consequences**
- **Rejected alternatives**
- **Deferred follow-up slices**

ADRs separate **current implementation** from **future target state**. Where a
decision describes behavior that is not yet implemented, that is stated
explicitly.

| ADR | Decision |
| --- | --- |
| [0001](0001-hosted-surface-boundaries.md) | Lock Core, Control Plane, and Portal ownership boundaries |
| [0002](0002-hosted-repository-and-ci-layout.md) | One repository, independent Python + pnpm workspaces, additive Compose, parallel hosted CI |
| [0003](0003-portal-and-control-plane-stack.md) | Next.js/TypeScript Portal; FastAPI/Python Control Plane |
| [0004](0004-control-plane-data-boundary.md) | Dedicated Control Plane PostgreSQL database + role, independent migrations, no cross-DB access |
| [0005](0005-identity-provider-boundary.md) | Managed CIAM behind a provider-neutral adapter; vendor selection deferred |
| [0006](0006-browser-session-and-core-delegation.md) | Secure HTTP-only sessions + short-lived delegated Core authorization |
| [0007](0007-human-roles-and-capabilities.md) | Org-scoped human roles map to hosted capabilities, separate from Core scopes |
| [0008](0008-billing-entitlements-and-metering.md) | Stripe owns payments; Engram owns entitlements; billing never silently alters memory trust |
| [0009](0009-support-access-boundary.md) | Platform operators are not tenant admins; support access is deny-by-default |
| [0010](0010-transfers-and-context-ledger-boundaries.md) | Transfers are future async Control Plane workflows; Context Ledger stays Core-owned |
