# ADR 0003 — Portal and Control Plane Stack

- **Status:** Accepted
- **Date:** 2026-07-24
- **Scope:** ENG-PORTAL-001A

## Context

The Portal is a browser application + backend-for-frontend; the Control Plane is
a hosted API service. We need stack choices that fit each surface's constraints
without introducing coupling to Core's Python data-plane internals.

## Decision

- **Portal:** Next.js (App Router) with React and **strict TypeScript**, on
  **Node.js 22**, managed by a **pnpm workspace** with an **exactly pinned pnpm
  version** (provisioned explicitly; we do **not** assume Corepack is available).
  Portal uses server components or server-side route handlers for Control Plane
  access; no internal Control Plane URL is exposed via `NEXT_PUBLIC_*`. No large
  UI framework or component vendor is selected yet.
- **Control Plane:** **FastAPI**, **Pydantic v2**, **SQLAlchemy asyncio +
  asyncpg**, and **httpx**, as an independent Python package
  (`engram_control_plane`) under `services/control-plane`. It is a `uv` workspace
  member. It **does not import Core ORM models or Core database sessions**; any
  future Core interaction is HTTP service-API-only. It uses an **independent
  Alembic** migration history (owner-only execution).

## Security and authorization consequences

- Server-side-only Control Plane access keeps the internal URL off the browser.
- Strict TypeScript and Python `mypy --strict` enforce boundary typing.

## Operational consequences

- Two toolchains (uv, pnpm) coexist in one repo with independent CI jobs.
- The Portal is server-rendered for status; no client-side data fetching to the
  Control Plane.

## Rejected alternatives

- **Portal in Python.** Rejected: the rich browser BFF surface favors a JS/TS
  ecosystem and React.
- **Control Plane importing Core sessions/ORM.** Rejected: breaks the data
  boundary (ADR 0001/0004).

## Deferred follow-up slices

- UI component/library selection (only if the management console grows).
- Core HTTP service APIs for Control-Plane-to-Core interaction.
