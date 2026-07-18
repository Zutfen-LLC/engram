#!/usr/bin/env python3
"""Verify the ENG-CONTEXT-001 golden conformance vectors.

Rebuilds each manifest from the vector's finalized-response + context inputs
using the reference Python builder, then asserts:

- the rebuilt manifest object equals the frozen ``expected.manifest``;
- ``manifest_hash`` (recomputed) equals the frozen value;
- ``packet_hash`` (recomputed from ``response.working_set`` bytes) equals frozen;
- ``request_digest`` (recomputed) equals frozen;
- each per-item ``served_content_hash`` (recomputed from served ``content``
  bytes) equals frozen;
- the canonical RFC 8785 bytes of the frozen manifest hash to the frozen
  ``manifest_hash``.

Independent of the JavaScript verifier (``conformance/context-manifest-v1/
verify.mjs``); the two must agree on every frozen value.

Usage::

    python scripts/verify_context_manifest_vectors.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engram.context_manifest import (  # noqa: E402
    ContextManifestEffectiveV1,
    ContextManifestRequestedV1,
    ContextManifestRequestInputV1,
    ContextManifestSubjectV1,
    ContextManifestVersionsV1,
    build_startup_context_manifest_v1,
    canonical_json_bytes,
    compute_manifest_hash,
    sha256_digest,
)

VECTORS_DIR = ROOT / "conformance" / "context-manifest-v1" / "vectors"


class _Response:
    def __init__(self, **kwargs: Any) -> None:
        self.working_set = kwargs["working_set"]
        self.items = kwargs["items"]
        self.pinned_omitted_count = kwargs.get("pinned_omitted_count", 0)
        self.omitted_count = kwargs.get("omitted_count", 0)
        self.message = kwargs.get("message")


def _verify_vector(path: Path) -> None:
    vector = json.loads(path.read_text())
    name = vector["name"]
    inp = vector["input"]
    exp = vector["expected"]

    subject = ContextManifestSubjectV1(**inp["subject_context"])
    request = ContextManifestRequestInputV1(
        requested=ContextManifestRequestedV1(**inp["request_context"]["requested"]),
        effective=ContextManifestEffectiveV1(**inp["request_context"]["effective"]),
        query_digest=inp["request_context"]["query_digest"],
    )
    versions = ContextManifestVersionsV1(**inp["decision_versions"])
    response = _Response(**inp["response"])

    manifest = build_startup_context_manifest_v1(
        response=response,
        subject_context=subject,
        request_context=request,
        decision_versions=versions,
    )
    rebuilt = manifest.model_dump(mode="json", exclude_none=False, by_alias=True)

    # 1. Rebuilt manifest object equals frozen manifest.
    if rebuilt != exp["manifest"]:
        _fail(name, "manifest object mismatch", exp["manifest"], rebuilt)

    # 2. manifest_hash recomputed over the rebuilt manifest.
    recomputed_hash = compute_manifest_hash(manifest)
    if recomputed_hash != exp["manifest_hash"]:
        _fail(name, "manifest_hash", exp["manifest_hash"], recomputed_hash)

    # 3. manifest_hash is also the hash of the frozen canonical bytes.
    frozen_canon_hash = sha256_digest(exp["canonical_json"].encode("utf-8"))
    if frozen_canon_hash != exp["manifest_hash"]:
        _fail(name, "canonical_json hash", exp["manifest_hash"], frozen_canon_hash)

    # 4. packet_hash recomputed from response.working_set bytes.
    packet_hash = sha256_digest(inp["response"]["working_set"].encode("utf-8"))
    if packet_hash != exp["packet_hash"]:
        _fail(name, "packet_hash", exp["packet_hash"], packet_hash)

    # 5. request_digest recomputed.
    if manifest.request.request_digest != exp["request_digest"]:
        _fail(
            name,
            "request_digest",
            exp["request_digest"],
            manifest.request.request_digest,
        )

    # 6. per-item served_content_hash recomputed from served content bytes.
    if len(exp["served_content_hashes"]) != len(inp["response"]["items"]):
        _fail(
            name,
            "served_content_hashes length",
            str(len(exp["served_content_hashes"])),
            str(len(inp["response"]["items"])),
        )
    for i, raw_item in enumerate(inp["response"]["items"]):
        content_hash = sha256_digest(raw_item["content"].encode("utf-8"))
        if content_hash != exp["served_content_hashes"][i]:
            _fail(
                name,
                f"served_content_hash[{i}]",
                exp["served_content_hashes"][i],
                content_hash,
            )

    # 7. canonical bytes of the rebuilt manifest equal the frozen canonical.
    rebuilt_canon = canonical_json_bytes(rebuilt).decode("utf-8")
    if rebuilt_canon != exp["canonical_json"]:
        _fail(name, "canonical_json bytes", exp["canonical_json"], rebuilt_canon)

    print(f"  OK  {path.name}: manifest_hash={exp['manifest_hash'][:24]}...")


def _fail(name: str, what: str, expected: Any, got: Any) -> None:
    print(f"FAIL {name}: {what}", file=sys.stderr)
    print(f"  expected: {expected}", file=sys.stderr)
    print(f"  got:      {got}", file=sys.stderr)
    raise SystemExit(1)


def main() -> int:
    vectors = sorted(VECTORS_DIR.glob("*.json"))
    if not vectors:
        print(f"no vectors found in {VECTORS_DIR}", file=sys.stderr)
        return 1
    print(f"Verifying {len(vectors)} context-manifest-v1 vectors (Python)...")
    for path in vectors:
        _verify_vector(path)
    print(f"All {len(vectors)} vectors verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
