# ADR 0001 — Hosted Surface Boundaries

- **Status:** Accepted
- **Date:** 2026-07-24
- **Scope:** ENG-PORTAL-001A

## Context

Engram Core is a tenant-scoped memory data plane and an independently usable
self-hosted product. To offer a hosted SaaS, we need browser-facing surfaces
(the Portal), a hosted control/business plane (the Control Plane), and the
existing memory data plane (Core). Without hard boundaries, the natural drift
is for the Portal to reach into Core tables or for the Control Plane to share
Core's database — both of which collapse trust isolation and make independent
evolution impossible.

## Decision

We lock three hard boundaries:

1. **Engram Core** owns tenant-scoped memory: Core tenants/workspaces/principals,
   agent and integration API keys, memories and provenance, trust/authority/
   review/verification/conflict state, recall/search/graph/jobs/RLS, and Context
   Ledger receipts. Core remains independently self-hostable.
2. **Engram Control Plane** is the hosted SaaS identity, organization, business,
   entitlement, security, and orchestration plane. It has a dedicated database
   and a dedicated runtime role. It **must not import Core ORM models or Core
   database sessions**; future Core interaction is HTTP service-API-only.
3. **Engram Portal** is the browser application and backend-for-frontend. It
   **never accesses Core or Control Plane database tables directly**, **never
   receives or stores a long-lived agent API key**, **never retrieves an
   already-issued API-key secret**, and uses **server-side** calls to the
   Control Plane. It uses **short-lived delegated Core authorization** in a
   future slice. Customer and platform-operator experiences remain separate.

## Security and authorization consequences

- Portal-to-database access is prohibited by construction (no DB credential).
- Control-Plane-to-Core-table access is prohibited by construction (separate DB
  and role; see ADR 0004).
- Browser users are never represented by stored agent API keys (see ADR 0006).
- No Core API key is issued to or stored by the Control Plane in this slice.

## Operational consequences

- Each surface can be deployed, scaled, and rolled back independently.
- The Control Plane's health does not gate Core's health (and vice versa); the
  Portal renders an honest unavailable state when the Control Plane is absent.

## Rejected alternatives

- **Single monolithic service / shared database.** Rejected: collapses trust
  isolation and makes the hosted plane unable to evolve without risking Core.
- **Portal with a direct Core connection.** Rejected: a browser-user path that
  can read memory bypasses agent-scope authority.

## Deferred follow-up slices

- ENG-IDENTITY-001: managed CIAM behind the adapter (ADR 0005).
- ENG-DELEGATION-001: short-lived delegated Core authorization (ADR 0006).
- Core service-authenticated provisioning APIs for tenants/principals.
