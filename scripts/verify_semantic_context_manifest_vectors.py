"""Verify frozen semantic-context-manifest-v1 vectors with Python."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from engram.context_manifest import canonical_json_bytes, compute_manifest_hash, sha256_digest
from engram.semantic_context_manifest import (
    SemanticContextManifestV1,
    SemanticManifestDecisionContextV1,
    build_semantic_context_manifest_v1,
)

ROOT = Path(__file__).resolve().parent.parent
VECTORS = ROOT / "conformance" / "semantic-context-manifest-v1" / "vectors"


def rebuild(vector: dict[str, Any]) -> SemanticContextManifestV1:
    value = vector["input"]
    return build_semantic_context_manifest_v1(
        **value["packet"],
        query=value["query"],
        context=SemanticManifestDecisionContextV1.model_validate(value["context"]),
    )


def main() -> None:
    paths = sorted(VECTORS.glob("*.json"))
    if len(paths) < 10:
        raise SystemExit(f"expected at least 10 semantic vectors, found {len(paths)}")
    for path in paths:
        vector = json.loads(path.read_text())
        expected = vector["expected"]
        manifest = rebuild(vector)
        payload = manifest.model_dump(mode="json", by_alias=True, exclude_none=False)
        canonical = canonical_json_bytes(payload).decode("utf-8")
        assert payload == expected["manifest"], path.name
        assert canonical == expected["canonical_json"], path.name
        assert compute_manifest_hash(manifest) == expected["manifest_hash"], path.name
        assert sha256_digest(canonical.encode("utf-8")) == expected["manifest_hash"], path.name
        assert manifest.packet.hash == expected["packet_hash"], path.name
        assert manifest.request.query_digest == expected["query_digest"], path.name
        assert manifest.request.request_digest == expected["request_digest"], path.name
        assert [item.served_content_hash for item in manifest.items] == expected[
            "served_content_hashes"
        ], path.name
    print(f"Verified {len(paths)} semantic-context-manifest-v1 vectors (Python).")


if __name__ == "__main__":
    main()
