#!/usr/bin/env python3
"""Import MemPalace ChromaDB drawers into Engram via the REST API.

Reads drawers from a MemPalace ChromaDB store, infers kind from source_file /
metadata, detects duplicates via content_hash, and imports via POST /v1/remember.
KG triples and tunnels are imported conditionally (only if /v1/kg and /v1/tunnels
endpoints exist and are functional — they come from T13/T14).

Usage:
    python scripts/import_mempalace.py --dry-run
    python scripts/import_mempalace.py --apply --engram-url http://localhost:8000
    python scripts/import_mempalace.py --dry-run --limit 20
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_PALACE = str(Path.home() / ".mempalace" / "palace")
DRAWERS_COLLECTION = "mempalace_drawers"
KG_DB_NAME = "knowledge_graph.sqlite3"
TUNNELS_FILE_NAME = "tunnels.json"

MANUAL_REVIEW_KEYWORDS = ["TODO", "FIXME", "HACK", "XXX", "TBD", "PLACEHOLDER"]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class DrawerRecord:
    """A single MemPalace drawer normalized for import."""

    drawer_id: str
    content: str
    wing: str
    room: str
    source_file: str
    added_by: str
    kind: str
    content_hash: str
    needs_review: bool
    raw_metadata: dict[str, Any]


@dataclass
class ImportReport:
    """Accumulated dry-run / apply statistics."""

    total: int = 0
    by_wing: Counter[str] = field(default_factory=Counter)
    by_wing_room: Counter[str] = field(default_factory=Counter)
    by_kind: Counter[str] = field(default_factory=Counter)
    duplicates: int = 0
    needs_review: int = 0
    imported: int = 0
    errors: int = 0
    kg_triples: int = 0
    tunnels: int = 0
    kg_skipped: bool = False
    tunnel_skipped: bool = False


# ---------------------------------------------------------------------------
# Hashing (must match engram.canonicalize for dedup to work post-import)
# ---------------------------------------------------------------------------


def canonicalize(content: str) -> str:
    """Strip, collapse whitespace, lowercase — mirrors engram.canonicalize."""
    return " ".join(content.split()).lower()


def content_hash(canonical: str) -> str:
    """sha256: + hex digest — mirrors engram.canonicalize.content_hash."""
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


# ---------------------------------------------------------------------------
# Kind inference
# ---------------------------------------------------------------------------

_DOCTRINE_KEYWORDS = ("doctrine", "decision", "architecture", "design.md")
_DIARY_KEYWORDS = ("diary",)


def infer_kind(source_file: str, metadata: dict[str, Any]) -> str:
    """Infer Engram kind from MemPalace metadata.

    Rules (from task spec):
      - 'doctrine'/'decision' in source_file  -> 'doctrine'
      - diary (source_file or metadata type)  -> 'diary_entry'
      - otherwise                             -> 'fact'
    """
    sf_lower = (source_file or "").lower()
    combined = f"{sf_lower} {(metadata.get('type') or '').lower()}"

    if any(kw in combined for kw in _DOCTRINE_KEYWORDS):
        return "doctrine"
    if any(kw in combined for kw in _DIARY_KEYWORDS):
        return "diary_entry"
    return "fact"


# ---------------------------------------------------------------------------
# ChromaDB reading
# ---------------------------------------------------------------------------


def _load_drawers(palace_path: str, limit: int | None) -> list[DrawerRecord]:
    """Read all drawers from the ChromaDB store, returning normalized records."""
    import chromadb  # type: ignore[import-not-found]  # noqa: PLC0415 — deferred so the script can run without chromadb

    client = chromadb.PersistentClient(path=palace_path)
    col = client.get_collection(DRAWERS_COLLECTION)

    include = ["metadatas", "documents"]
    raw = col.get(include=include)
    ids: list[str] = list(raw["ids"])
    metas: list[dict[str, Any]] = list(raw["metadatas"])
    docs: list[str | None] = list(raw["documents"])

    records: list[DrawerRecord] = []
    for idx, drawer_id in enumerate(ids):
        meta = metas[idx] if idx < len(metas) else {}
        doc = docs[idx] if idx < len(docs) else None
        content = doc or ""

        wing = str(meta.get("wing", "unknown"))
        room = str(meta.get("room", "general"))
        source_file = str(meta.get("source_file", ""))
        added_by = str(meta.get("added_by", "mempalace"))

        kind = infer_kind(source_file, meta)
        chash = content_hash(canonicalize(content))

        needs_review = any(kw in content.upper() for kw in MANUAL_REVIEW_KEYWORDS)

        records.append(
            DrawerRecord(
                drawer_id=drawer_id,
                content=content,
                wing=wing,
                room=room,
                source_file=source_file,
                added_by=added_by,
                kind=kind,
                content_hash=chash,
                needs_review=needs_review,
                raw_metadata=meta,
            )
        )

    if limit is not None:
        records = records[:limit]
    return records


def _load_kg_triples(palace_path: str) -> list[dict[str, Any]]:
    """Load KG triples from the SQLite knowledge graph database."""
    db_path = Path(palace_path) / KG_DB_NAME
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM triples WHERE valid_to IS NULL").fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _load_tunnels() -> list[dict[str, Any]]:
    """Load explicit tunnels from ~/.mempalace/tunnels.json."""
    tunnel_file = Path.home() / ".mempalace" / TUNNELS_FILE_NAME
    if not tunnel_file.exists():
        return []
    result: list[dict[str, Any]] = json.loads(tunnel_file.read_text())
    return result


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


def detect_duplicates(records: list[DrawerRecord]) -> tuple[list[DrawerRecord], int]:
    """Return (unique_records, duplicate_count) based on content_hash."""
    seen: set[str] = set()
    unique: list[DrawerRecord] = []
    dupes = 0
    for rec in records:
        if rec.content_hash in seen:
            dupes += 1
        else:
            seen.add(rec.content_hash)
            unique.append(rec)
    return unique, dupes


# ---------------------------------------------------------------------------
# Engram REST API interaction
# ---------------------------------------------------------------------------


def _check_endpoint(client: httpx.Client, base_url: str, path: str) -> bool:
    """Probe whether an endpoint exists (non-404) and is not a stub."""
    try:
        resp = client.get(f"{base_url}{path}", params={"entity": "_probe"})
        # 404 = route doesn't exist; 501 = NotImplementedError stub
        return resp.status_code not in (404, 501)
    except httpx.HTTPError:
        return False


def _import_drawer(
    client: httpx.Client,
    base_url: str,
    rec: DrawerRecord,
    timeout: float,
) -> bool:
    """POST a single drawer to /v1/remember. Returns True on success."""
    payload: dict[str, Any] = {
        "content": rec.content,
        "kind": rec.kind,
        "wing": rec.wing,
        "room": rec.room,
        "source_type": "migration",
        "external_source": "mempalace",
        "external_id": rec.drawer_id,
    }
    resp = client.post(f"{base_url}/v1/remember", json=payload, timeout=timeout)
    # 201 = created, 200 = deduped (already exists, not an error)
    return resp.status_code in (200, 201)


def _import_kg_triple(
    client: httpx.Client,
    base_url: str,
    triple: dict[str, Any],
    timeout: float,
) -> bool:
    """POST a KG triple to /v1/kg. Returns True on success."""
    payload = {
        "subject": triple.get("subject", ""),
        "predicate": triple.get("predicate", ""),
        "object": triple.get("object", ""),
    }
    if triple.get("valid_from"):
        payload["valid_from"] = triple["valid_from"]
    resp = client.post(f"{base_url}/v1/kg", json=payload, timeout=timeout)
    return resp.status_code in (200, 201)


def _import_tunnel(
    client: httpx.Client,
    base_url: str,
    tunnel: dict[str, Any],
    timeout: float,
) -> bool:
    """POST a tunnel to /v1/tunnels. Returns True on success."""
    source = tunnel.get("source", {})
    target = tunnel.get("target", {})
    payload = {
        "source_wing": source.get("wing", ""),
        "source_room": source.get("room", ""),
        "target_wing": target.get("wing", ""),
        "target_room": target.get("room", ""),
        "label": tunnel.get("label"),
    }
    resp = client.post(f"{base_url}/v1/tunnels", json=payload, timeout=timeout)
    return resp.status_code in (200, 201)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _print_dry_run(
    records: list[DrawerRecord],
    duplicates: int,
    kg_triples: list[dict[str, Any]],
    tunnels: list[dict[str, Any]],
) -> None:
    """Print the dry-run analysis report."""
    report = ImportReport()
    report.total = len(records) + duplicates
    report.duplicates = duplicates
    report.kg_triples = len(kg_triples)
    report.tunnels = len(tunnels)

    for rec in records:
        report.by_wing[rec.wing] += 1
        report.by_wing_room[f"{rec.wing}/{rec.room}"] += 1
        report.by_kind[rec.kind] += 1
        if rec.needs_review:
            report.needs_review += 1

    print("=" * 60)
    print("MemPalace → Engram Import (DRY RUN)")
    print("=" * 60)
    print(f"Total drawers in palace : {report.total}")
    print(f"Unique (after dedup)    : {len(records)}")
    print(f"Duplicates detected     : {report.duplicates}")
    print(f"Needs manual review     : {report.needs_review}")
    print()

    print("--- By Wing ---")
    for wing, count in report.by_wing.most_common():
        print(f"  {wing:40s} {count:>4d}")
    print()

    print("--- By Wing/Room (top 20) ---")
    for wr, count in report.by_wing_room.most_common(20):
        print(f"  {wr:50s} {count:>4d}")
    print()

    print("--- By Kind ---")
    for kind, count in report.by_kind.most_common():
        print(f"  {kind:20s} {count:>4d}")
    print()

    print(f"KG triples to import    : {report.kg_triples}")
    print(f"Tunnels to import       : {report.tunnels}")
    print()

    if report.needs_review > 0:
        print("--- Items Flagged for Review ---")
        shown = 0
        for rec in records:
            if rec.needs_review and shown < 10:
                preview = rec.content[:80].replace("\n", " ")
                print(f"  [{rec.wing}/{rec.room}] {preview}...")
                shown += 1
        if report.needs_review > 10:
            print(f"  ... and {report.needs_review - 10} more")
    print("=" * 60)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import MemPalace ChromaDB drawers into Engram.",
    )
    parser.add_argument(
        "--palace",
        default=DEFAULT_PALACE,
        help=f"Path to MemPalace ChromaDB store (default: {DEFAULT_PALACE})",
    )
    parser.add_argument(
        "--engram-url",
        default="http://localhost:8000",
        help="Engram REST API base URL (default: http://localhost:8000)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Analyze only — report counts, duplicates, review items (default).",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Import drawers via POST /v1/remember.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of drawers to process (for testing).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP request timeout in seconds (default: 30).",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Engram API key (Bearer token). Required for auth-enabled deployments.",
    )
    args = parser.parse_args()

    # Resolve API key from env if not given on CLI
    if args.api_key is None:
        args.api_key = os.environ.get("ENGRAM_API_KEY")

    palace_path = str(Path(args.palace).expanduser())
    if not Path(palace_path).exists():
        print(f"ERROR: palace path does not exist: {palace_path}", file=sys.stderr)
        return 1

    # --- Load data ---
    try:
        records = _load_drawers(palace_path, args.limit)
    except Exception as exc:  # noqa: BLE001 — top-level script, want all errors
        print(f"ERROR: failed to read ChromaDB store: {exc}", file=sys.stderr)
        return 1

    kg_triples = _load_kg_triples(palace_path)
    tunnels = _load_tunnels()

    unique_records, duplicates = detect_duplicates(records)

    # --- Dry run ---
    if not args.apply:
        _print_dry_run(unique_records, duplicates, kg_triples, tunnels)
        return 0

    # --- Apply ---
    base_url = args.engram_url.rstrip("/")
    report = ImportReport()
    report.total = len(unique_records)
    report.duplicates = duplicates

    # Build client with optional auth headers
    headers: dict[str, str] = {}
    if args.api_key:
        headers["Authorization"] = f"Bearer {args.api_key}"

    with httpx.Client(headers=headers) as client:
        for rec in unique_records:
            report.by_wing[rec.wing] += 1
            report.by_kind[rec.kind] += 1
            if rec.needs_review:
                report.needs_review += 1
            try:
                if _import_drawer(client, base_url, rec, args.timeout):
                    report.imported += 1
                else:
                    report.errors += 1
            except httpx.HTTPError as exc:
                report.errors += 1
                print(f"  ERROR importing {rec.drawer_id}: {exc}", file=sys.stderr)

        # --- KG triples (conditional on endpoint existing) ---
        if kg_triples:
            if _check_endpoint(client, base_url, "/v1/kg/query"):
                for triple in kg_triples:
                    try:
                        if _import_kg_triple(client, base_url, triple, args.timeout):
                            report.kg_triples += 1
                    except httpx.HTTPError:
                        pass
            else:
                report.kg_skipped = True
                print("SKIP: /v1/kg endpoint not available (T13 not merged)")

        # --- Tunnels (conditional on endpoint existing) ---
        if tunnels:
            if _check_endpoint(client, base_url, "/v1/tunnels"):
                for tunnel in tunnels:
                    try:
                        if _import_tunnel(client, base_url, tunnel, args.timeout):
                            report.tunnels += 1
                    except httpx.HTTPError:
                        pass
            else:
                report.tunnel_skipped = True
                print("SKIP: /v1/tunnels endpoint not available (T14 not merged)")

    print("=" * 60)
    print("MemPalace → Engram Import (APPLY)")
    print("=" * 60)
    print(f"Drawers imported  : {report.imported}")
    print(f"Drawers errored   : {report.errors}")
    print(f"Duplicates skipped: {report.duplicates}")
    if kg_triples:
        if report.kg_skipped:
            print("KG triples       : SKIPPED (endpoint not available)")
        else:
            print(f"KG triples       : {report.kg_triples}")
    if tunnels:
        if report.tunnel_skipped:
            print("Tunnels          : SKIPPED (endpoint not available)")
        else:
            print(f"Tunnels          : {report.tunnels}")
    print("=" * 60)

    return 0 if report.errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
