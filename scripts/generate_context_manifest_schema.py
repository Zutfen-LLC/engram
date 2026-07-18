#!/usr/bin/env python3
"""Generate / verify the normative Context Manifest v1 JSON Schema.

The strict wire model (``engram.context_manifest.ContextManifestV1``) is the
single source of truth. This script materializes its JSON Schema to
``schemas/context-manifest-v1.schema.json`` so the checked-in schema cannot
drift from the model.

Usage::

    # Regenerate the checked-in schema.
    python scripts/generate_context_manifest_schema.py

    # Drift check: fail (exit 1) if the checked-in schema differs from the
    # model-generated schema. Used in CI and the schema-drift test.
    python scripts/generate_context_manifest_schema.py --check

The generated schema is the normative v1 contract. Do not hand-edit the
checked-in file — change the model, then regenerate.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engram.context_manifest import normative_manifest_schema_dict  # noqa: E402

SCHEMA_PATH = ROOT / "schemas" / "context-manifest-v1.schema.json"


def _generated_schema_text() -> str:
    schema = normative_manifest_schema_dict()
    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the checked-in schema differs from the generated one",
    )
    args = parser.parse_args()

    generated = _generated_schema_text()

    if args.check:
        if not SCHEMA_PATH.exists():
            print(
                f"DRIFT: {SCHEMA_PATH.relative_to(ROOT)} does not exist; "
                "run without --check to generate it.",
                file=sys.stderr,
            )
            return 1
        checked_in = SCHEMA_PATH.read_text()
        if checked_in != generated:
            print(
                f"DRIFT: {SCHEMA_PATH.relative_to(ROOT)} does not match the "
                "model-generated schema. Regenerate with:\n"
                f"  python {Path(__file__).name}\n"
                "(change the model, not the checked-in schema.)",
                file=sys.stderr,
            )
            return 1
        print(f"OK: {SCHEMA_PATH.relative_to(ROOT)} matches the model.")
        return 0

    SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA_PATH.write_text(generated)
    print(f"wrote {SCHEMA_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
