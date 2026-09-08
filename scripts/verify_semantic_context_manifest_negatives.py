"""Verify that Python rejects all semantic negative fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from engram.context_manifest import ContextManifestV1, canonical_json_bytes
from engram.context_receipts import parse_context_receipt_manifest
from engram.semantic_context_manifest import (
    SemanticContextManifestV1,
    SemanticManifestDecisionContextV1,
    build_semantic_context_manifest_v1,
)

ROOT = Path(__file__).resolve().parent.parent
NEGATIVES = ROOT / "conformance" / "semantic-context-manifest-v1" / "negative"


def rebuild(value: dict[str, Any]) -> SemanticContextManifestV1:
    return build_semantic_context_manifest_v1(
        **value["packet"],
        query=value["query"],
        context=SemanticManifestDecisionContextV1.model_validate(value["context"]),
    )


def verify_one(fixture: dict[str, Any]) -> None:
    parse_as = fixture["parse_as"]
    manifest = fixture["manifest"]
    if parse_as == "startup":
        ContextManifestV1.model_validate(manifest)
        raise AssertionError("semantic manifest parsed as startup")
    if parse_as == "dispatch":
        parse_context_receipt_manifest(manifest)
    else:
        parsed = SemanticContextManifestV1.model_validate(manifest)
        expected = rebuild(fixture["input"])
        if canonical_json_bytes(
            parsed.model_dump(mode="json", by_alias=True, exclude_none=False)
        ) != canonical_json_bytes(
            expected.model_dump(mode="json", by_alias=True, exclude_none=False)
        ):
            raise ValueError("manifest does not match independent finalized-input reconstruction")
        raise AssertionError("negative semantic fixture was accepted")


def main() -> None:
    paths = sorted(NEGATIVES.glob("*.json"))
    if len(paths) < 48:
        raise SystemExit(f"expected at least 48 semantic negatives, found {len(paths)}")
    for path in paths:
        fixture = json.loads(path.read_text())
        try:
            verify_one(fixture)
        except AssertionError:
            raise
        except Exception:  # noqa: BLE001 - rejection is the expected result
            continue
        raise AssertionError(f"{path.name} was accepted")
    print(f"All {len(paths)} semantic negative fixtures rejected (Python).")


if __name__ == "__main__":
    main()
