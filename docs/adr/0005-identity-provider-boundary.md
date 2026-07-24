# ADR 0005 — Identity Provider Boundary

- **Status:** Accepted
- **Date:** 2026-07-24
- **Scope:** ENG-PORTAL-001A (decision; no implementation this slice)

## Context

Hosted browser users need authentication (signup, login, MFA, recovery, account
linking, password storage). Building and operating an identity store is
high-risk and outside our core competency. We also want to avoid hard-coupling to
a single vendor.

## Decision

- Prefer a **managed CIAM** (Customer Identity and Access Management) provider,
  accessed behind a **provider-neutral adapter** owned by the Control Plane.
- **Generic OIDC** remains the future **self-hosted** interface.
- **Vendor selection and real authentication are deferred to ENG-IDENTITY-001.**
  This slice implements no authentication, no password storage, no MFA, and no
  account linking.

## Security and authorization consequences

- No passwords are stored by Engram; the provider owns credential storage.
- The adapter boundary lets us swap providers without rewriting the Control Plane.

## Operational consequences

- Identity status is exposed as `not_configured` until ENG-IDENTITY-001.

## Rejected alternatives

- **Self-built password store.** Rejected: unacceptable risk and cost.
- **Hard-coupling to one vendor with no adapter.** Rejected: lock-in.

## Deferred follow-up slices

- ENG-IDENTITY-001: vendor selection, adapter implementation, real auth flows.
- Account linking and identity consolidation.
