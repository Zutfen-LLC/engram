# Migration importers

These scripts migrate data from existing memory systems into Engram over the
REST API (they never touch the DB directly). All importers default to
**dry-run** mode — pass `--apply` to write. Dry-run reports counts, kind
mapping, and duplicate detection without sending anything.

Both importers are implemented and used against the CCA and MemPalace stores.
The production `--apply` runs against the live instance are operational work
tracked as post-MVP (see `docs/plans/engram-mvp-backlog.md`, BL-011).

## import_mempalace.py

Imports drawers from a MemPalace ChromaDB store into Engram, preserving
wing/room, inferring kind, and carrying over KG triples and tunnels.

```bash
# Dry-run (default): report counts by wing/room, duplicates, manual-review items
python scripts/import_mempalace.py --palace ~/.mempalace/palace \
    --engram-url http://localhost:8000

# Apply: import via POST /v1/remember with source_type='migration'
python scripts/import_mempalace.py --palace ~/.mempalace/palace \
    --engram-url http://localhost:8000 --apply
```

MemPalace import writes with `source_type='migration'`,
`external_source='mempalace'`, so imports land as trusted `active` items and are
dedup-protected by `external_id`/`content_hash`.

## import_cca.py

Imports entries from a Zutfen CCA (`cca_lite_memory_packet@v1`) JSON ledger
into Engram, mapping CCA kinds to Engram kinds and detecting duplicates via
`content_hash`.

```bash
# Dry-run (default): report counts by kind and duplicates detected
python scripts/import_cca.py --cca-file /path/to/hermes-memory.json \
    --engram-url http://localhost:8000

# Apply: import via POST /v1/remember with source_type='migration'
python scripts/import_cca.py --cca-file /path/to/hermes-memory.json \
    --engram-url http://localhost:8000 --apply
```

Kind mapping: `decision`→`decision`, `invariant`→`invariant`,
`operational_fact`→`fact`, `preference`→`preference`, `doctrine`→`doctrine`.
Imported items use `source_type='migration'`, `external_source='cca'`, with the
original `captured_at` preserved as `valid_from`.

## setup-python-dev.sh

Bootstraps the repo's `./.venv` for local Python development, then installs the
editable sibling SDK and MCP adapter packages so they are importable without
setting `PYTHONPATH`.

```bash
bash scripts/setup-python-dev.sh
# or: make setup-python-dev
```
