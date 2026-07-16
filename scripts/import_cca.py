#!/usr/bin/env python3
"""Import a CCA lite ledger JSON file into Engram via the REST API.

Reads a cca_lite_memory_packet@v1 file, maps CCA kinds to Engram kinds,
detects duplicates via content_hash, and imports via POST /v1/remember with
source_type='migration' and external_source='cca'.

Usage::
    python scripts/import_cca.py --cca-file path/to/hermes-memory.json --dry-run
    python scripts/import_cca.py --cca-file path/to/hermes-memory.json \
        --engram-url http://localhost:8000 --apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

# CCA kind -> Engram kind
KIND_MAP: dict[str, str] = {
    "decision": "decision",
    "invariant": "invariant",
    "operational_fact": "fact",
    "preference": "preference",
    "doctrine": "doctrine",
}


def canonicalize(content: str) -> str:
    """Strip, collapse whitespace, lowercase — mirrors engram.canonicalize."""
    return " ".join(content.split()).lower()


def content_hash(canonical: str) -> str:
    """sha256: + hex digest — mirrors engram.canonicalize.content_hash."""
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return "sha256:" + digest


def load_cca(path: Path) -> list[dict[str, Any]]:
    """Load entries from a CCA lite packet (dict with 'entries') or bare list."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return data.get("entries", [])
    if isinstance(data, list):
        return data
    return []


def fetch_existing_hashes(engram_url: str) -> set[str]:
    """Fetch content_hashes of existing CCA-eligible items from Engram."""
    try:
        resp = httpx.get(f"{engram_url}/v1/export/cca", timeout=30.0)
        resp.raise_for_status()
        return {e["content_hash"] for e in resp.json().get("entries", [])}
    except Exception:
        return set()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import CCA ledger JSON into Engram via the REST API."
    )
    parser.add_argument("--cca-file", required=True, help="Path to CCA lite JSON file")
    parser.add_argument(
        "--engram-url", default="http://localhost:8000", help="Engram API base URL"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Import via POST /v1/remember (default: dry-run)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Report counts and duplicates without importing (default)",
    )
    parser.add_argument(
        "--visibility",
        choices=["private", "workspace", "tenant", "public"],
        default="private",
        help=(
            "Visibility to import with (ENG-SCOPE-001). Default: private — an "
            "import never silently reproduces the old accidental tenant-wide "
            "behavior. Use 'workspace' with --workspace for workspace-shared "
            "imports."
        ),
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="Workspace slug to import into. Required when --visibility=workspace.",
    )
    args = parser.parse_args()

    if args.visibility == "workspace" and not args.workspace:
        print(
            "ERROR: --visibility workspace requires --workspace (no request sent).",
            file=sys.stderr,
        )
        return 1

    cca_path = Path(args.cca_file)
    if not cca_path.exists():
        print(f"ERROR: file not found: {cca_path}", file=sys.stderr)
        return 1

    entries = load_cca(cca_path)
    existing_hashes = fetch_existing_hashes(args.engram_url) if args.apply else set()
    seen_hashes: set[str] = set()

    by_kind: Counter[str] = Counter()
    duplicates = 0
    skipped = 0
    to_import: list[dict[str, Any]] = []

    for entry in entries:
        cca_kind = entry.get("kind", "")
        engram_kind = KIND_MAP.get(cca_kind)
        if engram_kind is None:
            skipped += 1
            continue

        content = entry.get("text", "")
        chash = content_hash(canonicalize(content))

        if chash in seen_hashes or chash in existing_hashes:
            duplicates += 1
            continue

        seen_hashes.add(chash)
        by_kind[engram_kind] += 1
        item: dict[str, Any] = {
            "content": content,
            "kind": engram_kind,
            "source_type": "migration",
            "source_session": entry.get("session_id") or "",
            "external_source": "cca",
            "external_id": entry.get("id"),
            "metadata": {"captured_at": entry.get("captured_at", "")},
            "visibility": args.visibility,
        }
        if args.workspace is not None:
            item["workspace"] = args.workspace
        to_import.append(item)

    # Report
    scope = (
        f"{args.visibility} (workspace={args.workspace})"
        if args.workspace
        else f"{args.visibility} (no workspace)"
    )
    print(f"CCA file: {cca_path}")
    print(f"Import scope: {scope}")
    print(f"Total entries: {len(entries)}")
    print(f"To import: {len(to_import)}")
    print(f"Duplicates: {duplicates}")
    print(f"Skipped (unmapped kind): {skipped}")
    print("By kind:")
    for kind, count in by_kind.most_common():
        print(f"  {kind}: {count}")

    if not args.apply:
        print("\n(dry-run — no changes made. Use --apply to import.)")
        return 0

    imported = 0
    errors = 0
    with httpx.Client(base_url=args.engram_url, timeout=30.0) as client:
        for item in to_import:
            try:
                resp = client.post("/v1/remember", json=item)
                resp.raise_for_status()
                imported += 1
            except Exception as exc:
                print(f"ERROR importing {item['external_id']}: {exc}", file=sys.stderr)
                errors += 1

    print(f"\nImported: {imported}")
    print(f"Errors: {errors}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
