# ADR 0007 — Human Roles and Capabilities

- **Status:** Accepted
- **Date:** 2026-07-24
- **Scope:** ENG-PORTAL-001A (decision; no implementation this slice)

## Context

Organizations need human roles (owner, administrator, developer, reviewer,
billing administrator, viewer). These are hosted-plane concerns and must not be
confused with Core's agent/integration API-key scopes.

## Decision

- Human roles are **organization-scoped** and map to explicit **hosted
  capabilities**.
- Roles: **Owner, Administrator, Developer, Reviewer, Billing administrator,
  Viewer**.
- They do **not** map one-to-one to Core API-key scopes; hosted capabilities are
  translated to short-lived delegated Core authority (ADR 0006), not to
  persistent admin scope.
- The **Billing administrator has no implicit memory-content access**.
- The **platform operator is not a tenant role** (ADR 0009).
- **Step-up requirements are documented but not implemented.**

## Security and authorization consequences

- Least-privilege: a billing admin cannot read memory content by default.
- Role-to-scope translation is explicit and auditable, never blanket admin.

## Operational consequences

- Roles and capabilities will be enforced by the Control Plane once membership
  tables exist.

## Rejected alternatives

- **Reusing Core API-key scopes as human roles.** Rejected: conflates
  agent/integration authority with human authority.

## Deferred follow-up slices

- Memberships, invitations, and capability enforcement (post-ENG-IDENTITY-001).
