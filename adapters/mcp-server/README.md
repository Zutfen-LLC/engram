# engram-mcp

MCP (Model Context Protocol) server adapter that exposes Engram's memory tools to
MCP-compatible clients (Hermes, Claude Desktop, etc.). Each tool is a thin async
wrapper over the [Engram Python SDK](../../sdk/engram-client), so anything you
can do through the REST API you can do from an MCP-connected agent.

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

From the repository root:

```bash
pip install -e adapters/mcp-server
```

This pulls in the sibling Engram SDK (`sdk/engram-client`) via a path dependency,
the `mcp` package, `httpx`, and `pydantic`. It installs the `engram-mcp` console
script.

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

## Tool signatures

Each tool mirrors the corresponding SDK method. The parameters below are the
ones most callers need; see the SDK for the full surface.

### `engram_remember`

Persist a memory item. Returns `{id, status, review_status, memory_confidence}`.

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `content` | string | yes | The memory text. |
| `kind` | string | no | doctrine, decision, invariant, preference, fact, diary_entry... |
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
