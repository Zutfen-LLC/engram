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
- **Design doc:** `docs/design.md` (source of truth for architecture and trust model)
- **Backlog:** `docs/backlog.json` (task definitions, dependency graph, acceptance criteria)

## Before you start

1. Read the task definition in `docs/backlog.json` — it has acceptance criteria, file scope, and context notes.
2. Read `docs/design.md` sections referenced by the task.
3. Check `docs/backlog.json` `dependency_graph` — your task's dependencies must be merged first.

## Code conventions

### Models and types
- All Pydantic models use v2 syntax (`BaseModel`, `Field(default_factory=...)`).
- Enum-like string fields use `Literal` types in Pydantic models and CHECK constraints in the DB.
- `dict` fields must be typed as `dict[str, Any]`, not bare `dict` (mypy --strict).

### Database
- ORM models live in `engram/models.py`. Migration DDL lives in `migrations/`.
- `engram.db:get_session` yields an `AsyncSession`. The dependency must set `app.tenant_id` and `app.principal_id` via `SET LOCAL` for RLS (not yet implemented — see T01).
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
ruff check .
mypy engram/
pytest -q
```

All three must pass with zero errors. If you add a new dependency, add it to `pyproject.toml` `[project.dependencies]` (or `[project.optional-dependencies] dev` for test-only).

## PR conventions

- One branch, one PR per task. Branch prefix: `feat/` (see backlog task's `branch` field).
- Squash-merge. Conventional commit title.
- Do not bundle unrelated changes.
- Do not modify `docs/design.md` or `docs/backlog.json` unless the task explicitly says to — those are planning artifacts, not code.
