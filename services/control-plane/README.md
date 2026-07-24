# Engram Control Plane

The Engram Control Plane is the hosted SaaS identity, organization, business,
entitlement, security, and orchestration boundary. It is a **hard service
boundary** separate from Engram Core: a dedicated PostgreSQL database, a
dedicated non-owner runtime role, an independent Alembic migration history, and
**no import of the `engram` Core package**.

> **Status (ENG-PORTAL-001A):** scaffolding only. No production identity,
> billing, delegation, organization, or management behavior is implemented. See
> `docs/adr/` and `docs/plans/hosted-signup-to-first-agent.md` for the target
> contract.

## Commands

```bash
engram-control-plane serve            # start the API server
engram-control-plane migrate          # apply Alembic migrations (owner role only)
engram-control-plane check-migrations # exit 0 at head, 1 if behind, 2 on error
engram-control-plane export-openapi   # write deterministic OpenAPI JSON (DB-free)
```

`export-openapi` is database-free and secret-free.

## Environment

See `.env.control.example`. Key variables (all prefixed `ENGRAM_CONTROL_`):
`ENVIRONMENT`, `HOST`, `PORT`, `DATABASE_URL`, `OWNER_DATABASE_URL`,
`LOG_LEVEL`, `BUILD_SHA`.

In a `production`/`staging` environment, `DATABASE_URL` and
`OWNER_DATABASE_URL` are required and validated at startup.

## Endpoints

| Method | Path               | Behavior                                                       |
| ------ | ------------------ | ------------------------------------------------------------- |
| GET    | `/health`          | Database-free liveness.                                       |
| GET    | `/ready`           | 200 only when DB reachable and Alembic at head; else 503.     |
| GET    | `/control/v1/meta` | Safe scaffold metadata and boundary contract.                 |

## Database boundary

- Dedicated `engram_control` database in the shared PostgreSQL cluster.
- `engram_control_app` runtime role: `NOBYPASSRLS`, `NOCREATEDB`,
  `NOCREATEROLE`, `NOSUPERUSER`; granted CONNECT on `engram_control` only,
  schema USAGE, and SELECT on readiness tables (including `alembic_version`).
- Migration execution is owner-only.
