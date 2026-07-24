# ADR 0009 — Support Access Boundary

- **Status:** Accepted
- **Date:** 2026-07-24
- **Scope:** ENG-PORTAL-001A (decision; no implementation this slice)

## Context

Platform operators will need to help customers. Unconstrained "support" access is
a recurring source of breaches and trust erosion. We must define the model before
any support tooling exists.

## Decision

- **Platform operators are not tenant administrators.** There is no global
  tenant-admin credential.
- Future support access is **deny-by-default**: a support action requires an
  explicit, **reason-bound**, **time-limited**, **step-up-authenticated**, and
  **audited** grant scoped to a specific tenant/surface.
- Support access is separate from human organization roles (ADR 0007) and from
  delegated browser authorization (ADR 0006).

## Security and authorization consequences

- No standing access to customer memory content.
- Every support access is individually attributable and revocable.

## Operational consequences

- Support flows will be built as explicit workflows, never via a backdoor role.

## Rejected alternatives

- **A global operator role that impersonates tenants.** Rejected: unacceptable
  blast radius and weak attribution.

## Deferred follow-up slices

- Support-access workflows and audit (future).
