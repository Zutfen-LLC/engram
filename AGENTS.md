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

## Docker on this system

Docker is installed, the daemon runs under systemd, and Docker Compose v2 is
available as the `docker compose` subcommand. The information below is specific
to this host — do not guess or discover it by trial and error.

### Installed versions

- **Docker Engine:** 29.6.1 (Arch package `docker`)
- **Docker Compose:** 5.1.4 as a CLI plugin (`docker compose`, NOT the
  standalone `docker-compose` binary — always use the `docker compose`
  subcommand form)
- **Daemon:** systemd-managed, `active` and `enabled` at boot
- **Socket:** `/var/run/docker.sock` (mode `srw-rw----`, owned by `root:docker`)
- **Storage driver:** overlayfs; root dir `/var/lib/docker`
- **Context:** `default` (local socket)

### Permissions — critical

User `zutfen` is a member of the `docker` group (GID 956), but that group is
**not necessarily active in the current shell session**. A bare `docker ps`
may fail with `permission denied while trying to connect to the docker API at
unix:///var/run/docker.sock`.

Two reliable ways to run docker commands:

```bash
# Preferred — activates the docker group for the command without sudo:
sg docker -c 'docker ps'

# Alternative — sudo works via wheel group membership:
sudo docker ps
```

When running docker compose, wrap the entire invocation:

```bash
sg docker -c 'docker compose up -d --build'
```

### Current container state

A standalone pgvector container is running for local development:

| Container           | Image                    | Port mapping      | Purpose                        |
|---------------------|--------------------------|--------------------|--------------------------------|
| `engram-pg-debug`   | `pgvector/pgvector:pg16` | `5433 → 5432/tcp`  | Local dev DB (not Compose-managed) |

This container is NOT started by `docker compose` — it was created with `docker
run`. It has no restart policy. It uses credentials `engram`/`engram` (owner)
and `engram_app`/`engram_app` (app role), matching `.env`.

There is no `engram-service` container running. The full Compose stack (postgres
+ engram-service) is not currently deployed locally — see "Running the stack"
below.

### Available images

- `pgvector/pgvector:pg16` — Postgres 16 + pgvector (used by Compose and the
  debug container)
- `engram-engram-test:latest` — CI image (Dockerfile `ci` target)
- `python:3.12-slim` — base image for the Engram service

### Dockerfile — multi-stage

The Dockerfile has three stages:

| Stage      | Purpose                                              | CMD                                        |
|------------|------------------------------------------------------|--------------------------------------------|
| `base`     | Shared foundation (deps, source copy)               | —                                          |
| `runtime`  | Production service image                            | `uvicorn engram.api.app:app` on port 8000  |
| `ci`       | Test image — installs all dev deps + SDK + adapters | `python scripts/run_ci.py`                 |

The final stage in the Dockerfile is `ci` (runs tests, not the server). Always
specify the correct `target:` — `docker-compose.yml` uses `target: runtime`,
`docker-compose.ci.yml` uses `target: ci`. If you build without a target, you
get the CI image.

### Docker Compose files

| File                     | Purpose                          | Services                                |
|--------------------------|----------------------------------|-----------------------------------------|
| `docker-compose.yml`     | Self-host / production deployment| `postgres`, `engram-service`            |
| `docker-compose.ci.yml`  | CI verification (real DB)        | `postgres`, `engram-test`               |

Both use `pgvector/pgvector:pg16` for Postgres. The deployment compose file
exposes Postgres on `5432:5432`; the CI compose file does not expose ports
(tests run inside the network).

### Running the stack (docker-compose.yml)

```bash
# Start both services (builds the engram-service image):
sg docker -c 'docker compose up -d --build'

# Check health:
sg docker -c 'docker compose ps'
curl http://localhost:8000/ready

# View logs:
sg docker -c 'docker compose logs -f engram-service'

# Stop:
sg docker -c 'docker compose down'

# Stop and destroy the DB volume (DESTRUCTIVE — loses all data):
sg docker -c 'docker compose down -v'
```

The service is reachable at `http://localhost:8000`. On first boot (empty data
volume) Postgres runs all migrations in `migrations/` via
`docker-entrypoint-initdb.d`.

### Running CI tests via Docker (docker-compose.ci.yml)

```bash
# Build and run the full CI suite against a real pgvector database:
sg docker -c 'docker compose -f docker-compose.ci.yml up --build --abort-on-container-error'

# Tear down after:
sg docker -c 'docker compose -f docker-compose.ci.yml down -v'
```

This verifies migrations, RLS enforcement, and runs the root test suites plus
SDK and MCP adapter coverage inside containers.

### Executing commands inside the service container

```bash
# Run a CLI command (e.g., init-db, bootstrap-key, worker):
sg docker -c 'docker compose exec engram-service engram init-db'
sg docker -c 'docker compose exec engram-service engram bootstrap-key'
sg docker -c 'docker compose exec engram-service engram worker --poll-interval 2'

# Open a psql session in the Postgres container:
sg docker -c 'docker compose exec postgres psql -U engram -d engram'
```

### Connecting to the dev database directly

The `engram-pg-debug` container maps Postgres to **port 5433** on the host.
There are no `psql` / `pg_isready` / `pg_dump` client tools installed on this
host — use `docker exec`:

```bash
# psql against the debug container:
sg docker -c 'docker exec -it engram-pg-debug psql -U engram -d engram'

# Against the compose-managed Postgres (when the stack is up):
sg docker -c 'docker compose exec postgres psql -U engram -d engram'
```

For local Python dev against the debug container, set the database URL to port
5433 (these are commented out in `.env` — uncomment or pass inline):

```bash
export ENGRAM_DATABASE_URL='postgresql+asyncpg://engram_app:engram_app@localhost:5433/engram'
export ENGRAM_OWNER_DATABASE_URL='postgresql+asyncpg://engram:engram@localhost:5433/engram'
```

## Local Python development

A `.venv` already exists in the repo root (Python 3.14.6). The project uses
`uv` for dependency management (install it if missing: `pacman -S uv` or
`curl -LsSf https://astral.sh/uv/install.sh | sh`).

### Initial setup

```bash
# Install dependencies (creates .venv if missing, installs dev deps):
uv sync --extra dev

# Install sibling packages (SDK + adapters) in editable mode:
bash scripts/setup-python-dev.sh
```

### Running checks locally

The Makefile wraps all three checks:

```bash
make check        # runs: lint (ruff) + typecheck (mypy) + test (pytest)
make lint         # ruff only
make typecheck    # mypy only
make test         # pytest only
```

All Makefile targets use `.venv/bin/` executables — they do not need `uv` at
runtime once the venv is bootstrapped.

### Running the service locally (without Docker)

```bash
# Ensure the DB env vars point at a running Postgres (see .env):
#   ENGRAM_DATABASE_URL       -> app role (RLS-enforced)
#   ENGRAM_OWNER_DATABASE_URL -> owner role (migrations/admin)
# For the debug container on port 5433, uncomment the URLs in .env or set them
# inline as shown in the Docker section above.

# Apply migrations (uses owner role):
.venv/bin/engram init-db

# Start the API server:
.venv/bin/engram serve
# Or directly:
.venv/bin/uvicorn engram.api.app:app --reload
```

### Local PostgreSQL client tools

There are no `psql`, `pg_isready`, or `pg_dump` binaries on this host. For any
Postgres CLI work, use `docker exec` against a running Postgres container (see
"Connecting to the dev database directly" above).

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

```
make check
```

This runs all three checks via the Makefile targets: `lint` (ruff), `typecheck` (mypy), `test` (pytest). All three must pass with zero errors.

CI additionally runs the Docker Compose real-DB path in `docker-compose.ci.yml`, which verifies migrations against `pgvector/pgvector:pg16` and runs root service tests plus explicit SDK and MCP adapter coverage inside containers.

If you add a new dependency, add it to `pyproject.toml` `[project.dependencies]` (or `[project.optional-dependencies] dev` for test-only).

### Local pre-commit hook

A pre-commit hook is set up to run `make check` automatically before each commit. To install it after cloning:

```bash
bash scripts/setup-hooks.sh
```

The hook blocks commits when any check fails, keeping `main` clean.

## PR conventions

- One branch, one PR per task. Branch prefix: `feat/` (see backlog task's `branch` field).
- Squash-merge. Conventional commit title.
- Do not bundle unrelated changes.
- Do not modify `docs/design.md` or `docs/backlog.json` unless the task explicitly says to — those are planning artifacts, not code.
