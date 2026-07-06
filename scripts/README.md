# Migration importers

These scripts migrate data from existing memory systems into Engram.
All importers default to **dry-run** mode — pass `--apply` to write.

## import_mempalace.py (TODO)

Imports drawers from a MemPalace ChromaDB store.

```bash
python scripts/import_mempalace.py --palace ~/.mempalace/palace --engram-url http://localhost:8000
```

## import_cca.py (TODO)

Imports entries from a Zutfen CCA JSON ledger file.

```bash
python scripts/import_cca.py --cca-file /path/to/hermes-memory.json --engram-url http://localhost:8000
```
