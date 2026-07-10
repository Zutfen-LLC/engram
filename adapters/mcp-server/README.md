# engram-mcp

MCP (Model Context Protocol) server adapter that exposes Engram's memory tools to
MCP-compatible clients (Hermes, Claude Desktop, etc.). Each tool is a thin async
wrapper over the [Engram Python SDK](../../sdk/engram-client), so anything you
can do through the REST API you can do from an MCP-connected agent.

> **Status & dogfooding:** This adapter is **implemented and verified** — it
> ships unit + integration tests and was smoke-tested end-to-end
> (`engram_remember`/`engram_recall`/`engram_search`) against the running dogfood
> deployment (record: [`docs/ops/dogfood-verification.md`](../../docs/ops/dogfood-verification.md)).
> It provides **explicit, tool-driven** memory capture: an agent (or its harness)
> decides when to call `engram_remember`. **Automatic** lifecycle capture
> (extracting facts on `pre_compress` / `sync_turn` / `session_end` without an
> explicit tool call) is the job of the separate
> [`engram-hooks`](../engram-hooks) companion library, which is written but not
> yet verified end-to-end (post-MVP). Point a client at the dogfood instance by
> setting `ENGRAM_BASE_URL` to the deployment URL and `ENGRAM_API_KEY` to an
> issued key.

## Tools

All tool names are prefixed with `engram_`.

| Tool | Description |
| --- | --- |
| `engram_remember` | Persist a memory item with dedup, trust defaults, and supersession. |
| `engram_recall` | Fetch a bounded working set of active memories (startup or semantic mode). |
| `engram_search` | Keyword (FTS), semantic (vector), or hybrid search over active memories. |
| `engram_classify` | Suggest kind, wing, room, and visibility for raw text. |
| `engram_kg_query` | Query knowledge-graph triples for an entity (subject or object). |
| `engram_kg_add` | Add a knowledge-graph triple, backed by a memory item. |
| `engram_diary_write` | Write a private diary entry for a principal. |

## Install

For a raw local repo checkout, bootstrap the repo venv so both sibling packages
(the SDK and the MCP adapter) are installed and importable without `PYTHONPATH`:

```bash
bash scripts/setup-python-dev.sh
# or: make setup-python-dev
```

This runs `uv sync --extra dev`, then installs both editable local packages into
`./.venv`:

- `sdk/engram-client`
- `adapters/mcp-server`

After that, both of these work from the repo venv:

```bash
.venv/bin/python -m engram_mcp
.venv/bin/engram-mcp
```

If you only want the adapter install commands explicitly, run:

```bash
uv pip install --python .venv/bin/python \
  -e sdk/engram-client \
  -e adapters/mcp-server
```

The order matters: the local SDK must be installed into the venv before the
adapter so the adapter's `engram-client>=0.1.0` runtime dependency is already
satisfied from the repo checkout.

## Configuration

The server reads connection details from environment variables at startup.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `ENGRAM_BASE_URL` | yes | — | Engram REST API base URL (e.g. `http://localhost:8000`). |
| `ENGRAM_API_KEY` | no | — | Bearer token. Omit for local no-auth setups (`ENGRAM_AUTH_ENABLED=false`). |
| `ENGRAM_TIMEOUT` | no | `30` | Per-request timeout in seconds. |

> Note: when launched as a subprocess by an MCP client, the server inherits only
> a minimal environment. **Pass `ENGRAM_*` variables explicitly via the
> `env:` block** of the client's MCP config (see the Hermes example below).

## Running

```bash
engram-mcp                    # stdio transport (default; what MCP clients launch)
ENGRAM_BASE_URL=http://localhost:8000 engram-mcp
```

For development, you can also run the module directly:

```bash
python -m engram_mcp
```

This package also exposes the underlying server module entrypoint, so both forms
below are valid when a client wants an explicit module target:

```bash
python -m engram_mcp
python -m engram_mcp.server
```

## Hermes configuration

Hermes registers MCP servers in `~/.hermes/config.yaml` under the top-level
`mcp_servers:` key. Add Engram as a stdio server, passing the connection
variables through `env:`:

```yaml
mcp_servers:
  engram:
    command: "engram-mcp"
    env:
      ENGRAM_BASE_URL: "http://localhost:8000"
      ENGRAM_API_KEY: "eng_your_api_key_here"
    timeout: 60
```

If `engram-mcp` isn't on the PATH Hermes launches with, invoke the module
explicitly instead:

```yaml
mcp_servers:
  engram:
    command: "python"
    args: ["-m", "engram_mcp"]
    env:
      ENGRAM_BASE_URL: "http://localhost:8000"
      ENGRAM_API_KEY: "eng_your_api_key_here"
    timeout: 60
```

For a remote deployment, point `ENGRAM_BASE_URL` at the public URL and use an
API key issued by the Engram admin endpoints.

## Smoke testing & verification

The adapter ships with a layered test suite under `tests/`:

* **Unit tests** (`test_registration.py`, `test_config.py`, `test_forwarding.py`)
  verify tool registration, JSON-schema enums (e.g. `sensitivity='restricted'`,
  recall `mode='semantic'`), config/startup failures, and that each tool
  forwards the expected request shape to the SDK. These run with **no network
  and no database**.
* **Integration tests** (`test_integration.py`) start a real Engram uvicorn
  service against a live PostgreSQL and drive full
  `MCP → SDK → HTTP → FastAPI → DB` round trips (`engram_remember →
  engram_recall → engram_search` and `engram_kg_add → engram_kg_query`) plus
  failure paths (unreachable service, validation 422). These **skip
  automatically** when no database is reachable.

### Run the full suite

From the repository root (with the repo venv active):

```bash
pytest -q adapters/mcp-server/tests
```

### Integration round trips against local Compose

The integration tests spin up their own in-process Engram server, so they only
need a reachable database:

```bash
docker compose up -d                                   # start PostgreSQL + Engram
pytest -q adapters/mcp-server/tests/test_integration.py
```

Auth must be disabled (`ENGRAM_AUTH_ENABLED=false`, the Compose default). The
tests bind an ephemeral loopback port, so there is no conflict with the Compose
service on `:8000`.

> In CI these run inside the Compose stack with `ENGRAM_FAIL_ON_DB_SKIP=1`, so a
> DB-backed skip (e.g. a broken round trip) fails the build instead of passing
> silently.

### Manual smoke against a running Engram

The most direct manual check is a programmatic one-off against any running
Engram instance — this is exactly what the integration tests do internally:

```python
import asyncio

from engram_mcp import build_server
from mcp.shared.memory import create_connected_server_and_client_session


async def main() -> None:
    # Reads ENGRAM_BASE_URL / ENGRAM_API_KEY from the environment.
    server = build_server()
    async with create_connected_server_and_client_session(server) as session:
        await session.call_tool("engram_remember", {"content": "smoke check"})
        print(await session.call_tool("engram_recall", {"mode": "startup"}))
        print(await session.call_tool("engram_search", {"query": "smoke"}))


asyncio.run(main())
```

```bash
docker compose up -d                       # start Engram (auth disabled)
ENGRAM_BASE_URL=http://localhost:8000 python smoke.py
```

For an auth-enabled deployment, also export `ENGRAM_API_KEY`.

## Tool signatures

Each tool mirrors the corresponding SDK method. The parameters below are the
ones most callers need; see the SDK for the full surface.

### `engram_remember`

Persist a memory item. Returns `{id, status, review_status, memory_confidence}`.

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `content` | string | yes | The memory text. |
| `kind` | string | no | Built-in: fact, preference, doctrine, decision, invariant, observation, diary_entry, procedure, summary. Tenants may also register custom kinds — see `docs/design.md` § Memory kinds. Omitted `kind` triggers auto-classification. |
| `wing` | string | no | Top-level taxonomy bucket. |
| `room` | string | no | Sub-bucket within a wing. |
| `workspace` | string | no | Workspace name or id. |
| `visibility` | `private` \| `workspace` \| `tenant` | no | `workspace` |
| `source_type` | `manual` \| `import` \| `migration` \| `extraction` \| `sync_turn` \| `pre_compress` | no | `manual` |
| `importance` | float | no | `0.5` |
| `sensitivity` | `normal` \| `sensitive` \| `restricted` | no | `normal` |
| `subject_type`, `subject_id`, `subject_name` | string | no | Optional provenance subject. |
| `external_id`, `external_source` | string | no | For imports. |

### `engram_recall`

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `mode` | `startup` \| `semantic` | no | `startup` |
| `query` | string | no | Required for semantic mode. |
| `workspace` | string | no | Scope to a workspace. |
| `token_budget` | int | no | Soft cap on returned content. |

### `engram_search`

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `query` | string | yes | Search text. |
| `mode` | `keyword` \| `semantic` \| `hybrid` | no | `hybrid` |
| `limit` | int | no | `10` |
| `wing`, `room`, `kind` | string | no | Filters. |

### `engram_classify`

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `content` | string | yes | Text to classify. |
| `context` | string | no | Surrounding context. |
| `workspace` | string | no | Workspace vocabulary scope. |

### `engram_kg_query`

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `entity` | string | yes | Entity name (matched as subject or object). |
| `direction` | `outgoing` \| `incoming` \| `both` | no | `both` |
| `predicate` | string | no | Filter by predicate. |
| `as_of` | string | no | ISO timestamp for point-in-time query. |

### `engram_kg_add`

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `subject` | string | yes | Triple subject. |
| `predicate` | string | yes | Triple predicate. |
| `object` | string | yes | Triple object. |
| `workspace` | string | no | Workspace scope. |
| `source_item_id` | string (UUID) | no | Backing memory item id. |
| `confidence` | float | no | `0.5` |

### `engram_diary_write`

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `entry` | string | yes | Diary text. |
| `principal` | string | yes | Principal name (not id). |
| `topic` | string | no | Optional topic tag. |

## See also

- [Engram Python SDK](../../sdk/engram-client) — the underlying async client.
- [Engram REST API](../../engram/api) — server-side route definitions.
- [Design doc](../../docs/design.md) — trust model and scoring formula.
