## Summary

Adds `scripts/import_mempalace.py`, a standalone CLI that imports MemPalace ChromaDB drawers into Engram via the REST API. It reads drawers from the `mempalace_drawers` ChromaDB collection, infers Engram kind from metadata (doctrine/diary_entry/fact), detects duplicates via content_hash (mirroring `engram.canonicalize`), and writes via `POST /v1/remember` with `source_type='migration'`, `external_source='mempalace'`. KG triples and tunnels are imported conditionally — the script probes `/v1/kg` and `/v1/tunnels` and skips cleanly if those endpoints (T13/T14) aren't merged yet.

## Files changed

- `scripts/import_mempalace.py` (new): Argparse CLI with `--palace`, `--engram-url`, `--dry-run`/`--apply`, `--limit`, `--timeout`. Reads ChromaDB drawers, loads KG triples from `knowledge_graph.sqlite3`, loads tunnels from `~/.mempalace/tunnels.json`, dedup detection, kind inference, dry-run reporting, and REST import via httpx.

## Testing

- **ruff check .** — PASS (0 errors)
- **mypy --strict scripts/import_mempalace.py** — PASS (0 errors, chromadb import annotated with type: ignore)
- **mypy engram/** — PASS (22 files, 0 errors)
- **pytest -q** — PASS (61 tests, 0 failures)
- **Dry-run on real palace** (205 drawers) — Reports correct counts by wing/room, kind inference (152 fact / 36 diary_entry / 17 doctrine), 29 KG triples, 21 tunnels, 0 duplicates.
- **Dry-run with --limit 5** — Correctly limits and reports subset.

## Notes

- **chromadb dependency**: The script uses a deferred `import chromadb` inside `_load_drawers()` so it does not fail at import time if chromadb is not installed. chromadb is NOT added to pyproject.toml because the script is standalone and should be run in an environment that has chromadb (e.g. the MemPalace venv). Engram's own venv does not need chromadb.
- **KG/tunnel import is conditional**: `/v1/kg` and `/v1/tunnels` endpoints are still stubs (T13/T14 not merged). The script probes them at runtime and skips with a clear message if they return 404/501. No hard dependency.
- **Hash compatibility**: `canonicalize()` and `content_hash()` mirror `engram/canonicalize.py` exactly so dedup works correctly after import.
- **Pre-write rung**: landed on rung 7 (write the minimum that works).

Closes T11.
