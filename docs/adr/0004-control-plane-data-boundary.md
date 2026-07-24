# ADR 0004 — Control Plane Data Boundary

- **Status:** Accepted
- **Date:** 2026-07-24
- **Scope:** ENG-PORTAL-001A

## Context

The Control Plane will own hosted business state (portal users, memberships,
sessions, subscriptions, entitlements, meters). For this to be a real boundary —
not just a logical one — it must be enforced at the database layer, and it must
not be able to read or write Core memory tables.

## Decision

- A **dedicated `engram_control` database** in the existing PostgreSQL cluster
  for the initial hosted deployment. A separate database creates an enforceable
  boundary while letting the first deployment share the cluster.
- Roles:
  - Existing **Core owner/migration** role (unchanged).
  - Existing **`engram_app`** Core runtime role (unchanged).
  - New **`engram_control_app`** non-owner Control Plane runtime role.
- Rules:
  - `engram_control_app` can connect **only** to the Control Plane database.
  - `engram_app` can connect **only** to the Core database.
  - We `REVOKE CONNECT ON DATABASE ... FROM PUBLIC` for both databases and grant
    each runtime role CONNECT only to its own database, so no role connects to
    the other DB merely by existing.
  - The Control Plane runtime role has **no Core table privileges**, and the
    Core runtime role has **no Control Plane table privileges**. The Control
    Plane runtime role is granted only CONNECT, schema USAGE, and the minimum
    SELECT needed for readiness — including `alembic_version`. It has **no
    schema CREATE and no migration authority**; migration execution is
    **owner-only**.
  - **No cross-database foreign keys or direct joins.**

## Security and authorization consequences

- The data boundary is enforced by PostgreSQL privileges, not just convention.
- Even a compromised Control Plane runtime credential cannot read Core memory.

## Operational consequences

- A same-volume regression proof verifies Core API and worker still start after
  the hosted bootstrap has changed database privileges.
- The dedicated DB can be split to a separate cluster later with no app change.

## Rejected alternatives

- **Separate schemas in one database with role grants.** Rejected: weaker
  boundary; a migration or a `GRANT` mistake leaks across schemas.
- **Separate clusters on day one.** Rejected: unnecessary operational cost for
  the initial deployment; a dedicated DB in the shared cluster is enforceable.

## Deferred follow-up slices

- Moving `engram_control` to its own cluster.
- Control Plane business tables (portal_users, memberships, sessions, etc.).
