# ADR 0002 — Hosted Repository and CI Layout

- **Status:** Accepted
- **Date:** 2026-07-24
- **Scope:** ENG-PORTAL-001A

## Context

We must add hosted surfaces (Portal, Control Plane, portal-sdk, ui) without
destabilizing the existing self-hosted Core, its SDK/adapters, or the real-DB
CI critical path. The repository is currently a single Python project with
independent sibling packages; there is no JavaScript/TypeScript tooling at all.

## Decision

- **One repository** with **independent workspaces**: a `uv` workspace for the
  Control Plane Python member (`[tool.uv.workspace] members =
  ["services/control-plane"]`, no cross-package sources — the Control Plane does
  not depend on Core) and a `pnpm` workspace (`apps/*`, `packages/*`) for the
  TypeScript surfaces. Core and its SDK/adapters remain independent.
- **Additive hosted Compose**: `docker-compose.hosted.yml` adds services only; it
  does not modify the existing `postgres`/`engram-service`/`engram-worker`.
  `docker compose up` (no overlay) is unchanged.
- **Parallel hosted CI**: a new `.github/workflows/ci-hosted.yml` runs the four
  hosted jobs (`control-plane-quality`, `portal-quality`,
  `hosted-compose-validate`, `hosted-smoke`) in parallel. The existing
  `ci.yml` jobs (compose-real-db-ci, compose-validate, conformance-vectors,
  lock-drift) are preserved verbatim and are never inserted into the hosted
  critical path.

## Security and authorization consequences

- The Control Plane workspace member cannot pull in Core internals accidentally
  (no `[tool.uv.sources]` mapping exists).
- New CI jobs cannot widen the Core real-DB gate; they run independently.

## Operational consequences

- Local dev uses `uv` for the Control Plane and `pnpm` for TS surfaces.
- The standard self-hosted stack remains independently bootable and unchanged.

## Rejected alternatives

- **Separate repositories per surface.** Rejected: cross-repo contract drift and
  release coordination cost outweigh the separation; a monorepo with hard
  boundaries is simpler here.
- **A monorepo task runner (Turborepo).** Rejected for this slice: unnecessary
  complexity; `pnpm -r` and `uv --package` suffice.

## Deferred follow-up slices

- Consider a monorepo task runner only if cross-surface orchestration cost grows.
- Shared CI caching tuning as the hosted surface count grows.
