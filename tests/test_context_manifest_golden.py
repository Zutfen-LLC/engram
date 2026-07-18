"""Golden-vector tests for the Context Manifest (ENG-CONTEXT-001).

Loads every checked-in ``conformance/context-manifest-v1/vectors/*.json`` and
rebuilds its manifest from the vector's finalized-response + context inputs
using the reference Python builder, asserting the rebuilt manifest object,
``manifest_hash``, ``packet_hash``, ``request_digest``, and per-item
``served_content_hash`` all equal the frozen expected values.

This is the pytest mirror of ``scripts/verify_context_manifest_vectors.py``;
the independent cross-language proof is the JavaScript verifier
``conformance/context-manifest-v1/verify.mjs``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from engram.context_manifest import (
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

VECTORS_DIR = (
    Path(__file__).resolve().parent.parent
    / "conformance"
    / "context-manifest-v1"
    / "vectors"
)


class _Response:
    def __init__(self, **kwargs: Any) -> None:
        self.working_set = kwargs["working_set"]
        self.items = kwargs["items"]
        self.pinned_omitted_count = kwargs.get("pinned_omitted_count", 0)
        self.omitted_count = kwargs.get("omitted_count", 0)
        self.message = kwargs.get("message")


def _load_vectors() -> list[tuple[str, dict[str, Any]]]:
    if not VECTORS_DIR.exists():
        return []
    found: list[tuple[str, dict[str, Any]]] = []
    for p in sorted(VECTORS_DIR.glob("*.json")):
        found.append((p.name, json.loads(p.read_text())))
    return found


_VECTORS = _load_vectors()
_VECTOR_NAMES = [name for name, _ in _VECTORS]


def _rebuild(raw: dict[str, Any]) -> Any:
    inp = raw["input"]
    subject = ContextManifestSubjectV1(**inp["subject_context"])
    request = ContextManifestRequestInputV1(
        requested=ContextManifestRequestedV1(**inp["request_context"]["requested"]),
        effective=ContextManifestEffectiveV1(**inp["request_context"]["effective"]),
        query_digest=inp["request_context"]["query_digest"],
    )
    versions = ContextManifestVersionsV1(**inp["decision_versions"])
    response = _Response(**inp["response"])
    return build_startup_context_manifest_v1(
        response=response,
        subject_context=subject,
        request_context=request,
        decision_versions=versions,
    )


@pytest.mark.parametrize("name", _VECTOR_NAMES or ["no-vectors"])
def test_golden_vector(name: str) -> None:
    if name == "no-vectors":
        pytest.fail(f"no golden vectors found in {VECTORS_DIR}")
    raw = next(v for n, v in _VECTORS if n == name)
    inp = raw["input"]
    exp = raw["expected"]

    manifest = _rebuild(raw)
    rebuilt = manifest.model_dump(mode="json", exclude_none=False, by_alias=True)

    # Rebuilt manifest object equals the frozen manifest.
    assert rebuilt == exp["manifest"], f"{name}: manifest object drifted"

    # manifest_hash recomputed two independent ways.
    assert compute_manifest_hash(manifest) == exp["manifest_hash"]
    assert sha256_digest(exp["canonical_json"].encode("utf-8")) == exp["manifest_hash"]

    # packet_hash from response.working_set bytes.
    assert (
        sha256_digest(inp["response"]["working_set"].encode("utf-8"))
        == exp["packet_hash"]
    )

    # request_digest recomputed.
    assert manifest.request.request_digest == exp["request_digest"]

    # per-item served_content_hash from served content bytes.
    items = inp["response"]["items"]
    expected_hashes = exp["served_content_hashes"]
    assert len(expected_hashes) == len(items)
    for idx in range(len(items)):
        actual = sha256_digest(items[idx]["content"].encode("utf-8"))
        assert actual == expected_hashes[idx], (
            f"{name}: served_content_hash at index {idx} drifted"
        )

    # canonical bytes equal the frozen canonical_json.
    assert (
        canonical_json_bytes(rebuilt).decode("utf-8") == exp["canonical_json"]
    ), f"{name}: canonical bytes drifted"


def test_at_least_ten_vectors_present() -> None:
    # The contract requires the 10 scenarios listed in ENG-CONTEXT-001.
    assert len(_VECTORS) >= 10, f"expected >=10 golden vectors, found {len(_VECTORS)}"


def test_empty_packet_vector_hashes_zero_bytes() -> None:
    raw = next(v for n, v in _VECTORS if n == "001-empty-startup.json")
    assert raw["expected"]["packet_hash"] == "sha256:" + hashlib.sha256(b"").hexdigest()


def test_item_order_vectors_produce_distinct_hashes() -> None:
    # Vector 002 (mixed order) and 008 (reversed) must differ in both
    # manifest_hash and packet_hash — item order is significant.
    v002 = next(v for n, v in _VECTORS if n == "002-mixed-pinned-scored.json")
    v008 = next(v for n, v in _VECTORS if n == "008-item-order-change.json")
    assert v002["expected"]["manifest_hash"] != v008["expected"]["manifest_hash"]
    assert v002["expected"]["packet_hash"] != v008["expected"]["packet_hash"]


def test_number_boundary_vector_has_no_negative_zero_in_canonical() -> None:
    raw = next(v for n, v in _VECTORS if n == "006-number-boundaries.json")
    canon = raw["expected"]["canonical_json"]
    # The vector's first item has score=-0.0 in its INPUT. RFC 8785 section
    # 3.2.2.3 requires -0 to serialize as "0", so the canonical bytes must
    # contain '"score":0' for that item and never '"score":-0'.
    assert '"score":0,' in canon or '"score":0}' in canon
    assert '"score":-0' not in canon
