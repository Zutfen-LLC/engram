# Hosted-Platform Local Development

- **Scope:** ENG-PORTAL-001A (scaffold)
- **Status:** Implemented scaffold + target contracts. Identity, billing,
  delegation, organizations, and management behavior are **not** implemented.

This guide covers local development for the hosted surfaces (Control Plane,
Portal, portal-sdk, ui) and the additive hosted Compose stack.

## Implemented vs. target

| Area | Status |
| --- | --- |
| Control Plane scaffold (health, ready, meta, logging, sanitized errors, OpenAPI) | **Implemented** |
| Control Plane dedicated DB + least-privilege role + isolation | **Implemented** (compose bootstrap) |
| Control Plane Alembic (owner-only, no-op base revision) | **Implemented** |
| Portal scaffold (status page, server-side Control Plane access, a11y) | **Implemented** |
| portal-sdk (typed client + generated types + drift check) | **Implemented** |
| ui (tokens, accessible StatusCard, page container) | **Implemented** |
| Hosted Compose overlay + standard-stack non-regression | **Implemented** |
| Identity / CIAM | **Not implemented** (ADR 0005, ENG-IDENTITY-001) |
| Browser sessions + Core delegation | **Not implemented** (ADR 0006, ENG-DELEGATION-001) |
| Organizations / memberships / invitations | **Not implemented** (ADR 0007) |
| Billing / entitlements / metering | **Not implemented** (ADR 0008) |
| Support access | **Not implemented** (ADR 0009) |
| Transfers | **Not implemented** (ADR 0010) |

## Prerequisites

- Python ≥ 3.11 and [`uv`](https://docs.astral.sh/uv/) (pinned to `0.11.29` in CI).
- Node.js 22 and `pnpm` (pinned to `9.12.3` via `packageManager`; provision it
  explicitly — do **not** assume Corepack is available):
  ```bash
  npm install -g pnpm@9.12.3
  ```
- Docker + Docker Compose v2 for the hosted stack.

## Control Plane (Python)

The Control Plane is a `uv` workspace member (`services/control-plane`).

```bash
# Install the workspace member (dev extras for testing).
uv sync --package engram-control-plane --extra dev

# Checks.
uv run --package engram-control-plane ruff check services/control-plane
uv run --package engram-control-plane mypy services/control-plane/engram_control_plane
uv run --package engram-control-plane pytest services/control-plane/tests

# Regenerate the committed OpenAPI contract.
uv run --package engram-control-plane engram-control-plane \
  export-openapi -o packages/portal-sdk/openapi.json
```

### Environment variables

All prefixed `ENGRAM_CONTROL_`. See
`services/control-plane/.env.control.example`. In `production`/`staging`,
`DATABASE_URL` and `OWNER_DATABASE_URL` are required and validated at startup
(not at import — app import and `export-openapi` stay database-free).

| Variable | Purpose |
| --- | --- |
| `ENGRAM_CONTROL_ENVIRONMENT` | `development` \| `staging` \| `production` |
| `ENGRAM_CONTROL_HOST` / `ENGRAM_CONTROL_PORT` | bind address (default `0.0.0.0:8100`) |
| `ENGRAM_CONTROL_DATABASE_URL` | runtime (non-owner) asyncpg URL to `engram_control` |
| `ENGRAM_CONTROL_OWNER_DATABASE_URL` | owner/migration URL (migrate / check-migrations only) |
| `ENGRAM_CONTROL_LOG_LEVEL` | `debug` \| `info` \| `warning` \| `error` |
| `ENGRAM_CONTROL_BUILD_SHA` | build provenance |

## Portal / portal-sdk / ui (TypeScript)

pnpm workspace (`apps/*`, `packages/*`):

```bash
pnpm install
pnpm lint         # tsc --noEmit (sdk, ui) + next lint (portal)
pnpm typecheck    # tsc --noEmit across the workspace
pnpm test         # vitest across the workspace
pnpm build        # next build (portal) + tsup (sdk)
pnpm contracts:check   # OpenAPI -> TS drift check
```

Regenerate types after changing the Control Plane OpenAPI:

```bash
pnpm --filter engram-portal-sdk contracts:generate
```

### Control Plane connectivity (Portal)

The Portal reads `CONTROL_PLANE_URL` (server-side env). There is deliberately
**no** `NEXT_PUBLIC_` equivalent — the internal URL never reaches the browser.

## Database bootstrap and migrations

The hosted Compose overlay bootstraps the dedicated database and role
automatically. Manual steps (for a non-Compose PostgreSQL):

1. Create the `engram_control` database and the `engram_control_app` runtime
   role, and enforce CONNECT isolation, via
   `services/control-plane/scripts/bootstrap-control-db.sh` (run as the owner
   role).
2. Apply migrations (owner-only):
   ```bash
   ENGRAM_CONTROL_OWNER_DATABASE_URL=postgresql+asyncpg://owner:...@host/engram_control \
     engram-control-plane migrate
   ```
3. Verify head:
   ```bash
   engram-control-plane check-migrations   # 0 at head, 1 behind, 2 error
   ```

## Hosted Compose

```bash
docker compose -f docker-compose.yml -f docker-compose.hosted.yml up --build
```

- Portal: http://localhost:3000 — Control Plane: http://localhost:8100
- Dependency order: postgres → control-db-bootstrap → control-plane-migrate →
  control-plane → portal.

Validate the overlay without booting:

```bash
python scripts/validate_hosted_compose.py
```

### Database boundary proofs (CI)

The `hosted-smoke` job proves:
- `engram_control_app` **cannot** connect to the Core database.
- `engram_app` **cannot** connect to the Control Plane database.
- Core API and worker still start after the hosted bootstrap changed privileges.

## CI architecture

- `.github/workflows/ci.yml` — existing Core jobs (unchanged).
- `.github/workflows/ci-hosted.yml` — four parallel hosted jobs:
  `control-plane-quality`, `portal-quality`, `hosted-compose-validate`,
  `hosted-smoke`. New jobs never enter the Core real-DB critical path.

### Contract generation

The Control Plane OpenAPI (`packages/portal-sdk/openapi.json`) is the source of
truth; TypeScript types are generated from it with `openapi-typescript`. CI
regenerates and fails on drift (no `git diff`).
