# ADR 0006 — Browser Session and Core Delegation

- **Status:** Accepted
- **Date:** 2026-07-24
- **Scope:** ENG-PORTAL-001A (decision; no implementation this slice)

## Context

A browser user (a human in the Portal) must be able to act against their own
tenant's memory through Core, but Core's authorization model is agent/integration
API-key based. We must not bridge these by handing the browser a long-lived agent
API key.

## Decision

- Use **secure, HTTP-only browser sessions** for Portal users (no long-lived
  agent API key reaches the browser or the Portal's persistent store).
- Portal-to-Core access uses **short-lived delegated Core authorization** minted
  by the Control Plane. Browser users are **never** represented by stored agent
  API keys.
- The future delegated token's minimum claims: `iss`, `aud`, `sub`, `tenant_id`,
  `principal_id`, `capabilities`, `session_id`, `mfa`, `auth_time`, `exp`,
  `jti`. Rules: short lifetime; audience restricted to Engram Core; Core
  revalidates tenant and principal state before setting RLS context; hosted
  capabilities are not blindly translated to Core admin scope; revoked/locked
  browser sessions cannot mint new delegated credentials.
- **No delegated-token implementation is part of ENG-PORTAL-001A.** Delegation
  status is exposed as `not_implemented`.

## Security and authorization consequences

- A compromised browser session cannot exfiltrate a reusable Core credential.
- Delegation is step-up and MFA-aware (future), and Core remains authoritative
  over RLS context.

## Operational consequences

- The Portal cannot be used to manage memory until ENG-DELEGATION-001 lands.

## Rejected alternatives

- **Issue the browser a long-lived agent API key.** Rejected: a browser compromise
  yields a persistent Core credential — unacceptable.
- **Portal direct DB access.** Rejected (ADR 0001).

## Deferred follow-up slices

- ENG-DELEGATION-001: delegated token issuance, Core verification, session
  revocation enforcement.
