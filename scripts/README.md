# Engram scripts

## Hermes installation and onboarding

`install-hermes.sh` configures an existing Hermes installation with an
already-provisioned Engram agent key. It prompts securely through `/dev/tty`
and never creates a principal or mints a key:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/Zutfen-LLC/engram/main/scripts/install-hermes.sh \
  | bash
```

`onboard-profile.sh` is a separate self-service workflow. It starts with a
user-level key, calls `/v1/agents`, and creates a new agent principal and scoped
key for the selected profile. Do not use it when the agent key is already
provisioned. See [`adapters/engram-hooks/README.md`](../adapters/engram-hooks/README.md)
for installer options, update behavior, release pinning, and restart guidance.

## Migration importers

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
`external_source='mempalace'`, so drawer imports land as trusted `active` items
and are dedup-protected by `external_id`/`content_hash`. Select the memory scope
with `--visibility` and optional `--workspace`; the importer forwards that same
scope to both drawers and auto-backed KG triples. Its dry-run and apply summary
labels this shared memory-write scope explicitly. Tunnels do not create backing
memories and are unaffected.

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
editable sibling SDK plus both adapter packages so they are importable without
setting `PYTHONPATH`.

```bash
bash scripts/setup-python-dev.sh
# or: make setup-python-dev
```
