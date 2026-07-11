# AGENTS.md

Guidelines for AI coding agents working on Engram.

## Quick reference

- **Language:** Python ≥ 3.11
- **Framework:** FastAPI + Pydantic v2 + SQLAlchemy 2.0 async (asyncpg)
- **Database:** PostgreSQL 16 + pgvector ≥ 0.8
- **Tests:** pytest + pytest-asyncio (asyncio_mode=auto)
- **Linting:** ruff (E, F, I, UP, B, SIM; line-length=100)
- **Type checking:** mypy --strict
- **Commit style:** conventional commits (feat:, fix:, test:, docs:, refactor:)
- **Design doc:** `docs/design.md` (source of truth for architecture and trust model, with implementation-status annotations)
- **Backlog:** `docs/plans/engram-mvp-backlog.md` (execution backlog; MVP items BL-001–BL-010 are complete, BL-011+ is post-MVP). `docs/backlog.json` is retired to a pointer — do not use it as an active task source.

## Before you start

1. Read the task definition in `docs/plans/engram-mvp-backlog.md` — it has acceptance criteria, file scope, and context notes.
2. Read `docs/design.md` sections referenced by the task.
3. Check the task's **Dependencies** line — its dependencies must be merged first.

## Docker

Use Docker Compose v2 (`docker compose`) and the repository Compose files. Do
not assume a particular username, permission mechanism, host port, daemon
state, running container, local image cache, or installed package version.

- `docker-compose.yml` runs the PostgreSQL and production service stack.
- `docker-compose.ci.yml` runs migrations and the complete test suite against
  a real PostgreSQL + pgvector database using the non-owner application role.
- Run the CI path with
  `docker compose -f docker-compose.ci.yml up --build --abort-on-container-error`.
- Tear it down with `docker compose -f docker-compose.ci.yml down -v`.
- Consult `docs/deployment.md` for supported deployment commands and environment
  configuration.

## Local Python development

The project uses `uv` for dependency management.

### Initial setup

```bash
uv sync --extra dev
bash scripts/setup-python-dev.sh
```

### Running checks locally

```bash
make check        # lint + strict type checking + root tests
make lint
make typecheck
make test
```

The Makefile targets use executables from `.venv/bin/` after the environment is
bootstrapped.

### Running the service locally

Configure `ENGRAM_DATABASE_URL` for the RLS-enforced application role and
`ENGRAM_OWNER_DATABASE_URL` for migrations and administration. Do not assume a
specific host or port.

```bash
.venv/bin/engram init-db
.venv/bin/engram serve
```

## Code conventions

### Models and types

- All Pydantic models use v2 syntax (`BaseModel`, `Field(default_factory=...)`).
- Enum-like string fields use `Literal` types in Pydantic models and CHECK constraints in the DB.
- `dict` fields must be typed as `dict[str, Any]`, not bare `dict` (mypy --strict).

### Database

- ORM models live in `engram/models.py`. Migration DDL lives in `migrations/`.
- `engram.db:get_session` yields an `AsyncSession`. The dependency sets `app.tenant_id` and `app.principal_id` via `SET LOCAL` for RLS based on the authenticated principal (with auth disabled, it falls back to the seeded default tenant/admin).
- Content is append-first: never `UPDATE` memory item content. Metadata changes go through PATCH which writes to `item_events` first, then updates the column.
- Dedup is enforced by unique index `idx_memitems_dedup` on `(tenant_id, workspace_id, principal_id, content_hash) WHERE valid_to IS NULL AND review_status != 'rejected'` with `NULLS NOT DISTINCT`.

### API routes

- Each route file under `engram/api/routes/` owns one resource area.
- `engram/api/app.py` is the factory — register new routers there.
- FastAPI route stubs that raise `NotImplementedError` use `response_model=None` + `-> NoReturn`. When implementing, replace with the real return type and `response_model=YourResponse`.

### Trust model

- Source trust and review_status defaults depend on BOTH `source_type` and `principal.type` — see design.md §4 (Source trust defaults table).
- Defaults are read from `tenant_config`, not hardcoded. The lookup encodes the table from §4.
- Only `review_status='active'` items enter startup recall. Proposed items enter semantic recall only.

### Safety

- `engram/safety.py` has a secret-pattern denylist. The remember endpoint must call `has_secrets()` before storing — block with HTTP 422 if matched.
- Never store secrets, API keys, or credentials in memory items.

## Verification checklist (run before claiming done)

```bash
make check
```

This runs lint, strict type checking, and the root test suite. CI additionally
runs the Docker Compose real-PostgreSQL path in `docker-compose.ci.yml`, which
verifies migrations, app-role FORCE RLS, and the root, SDK, MCP, and hooks
adapter suites.

If you add a new dependency, add it to `pyproject.toml` `[project.dependencies]` (or `[project.optional-dependencies] dev` for test-only).

### Local pre-commit hook

```bash
bash scripts/setup-hooks.sh
```

The hook runs `make check` before each commit.

## PR conventions

- One branch, one PR per task. Use the branch prefix specified by the backlog task.
- Squash-merge. Conventional commit title.
- Do not bundle unrelated changes.
- Do not modify `docs/design.md` or `docs/backlog.json` unless the task explicitly says to — those are planning artifacts, not code.
