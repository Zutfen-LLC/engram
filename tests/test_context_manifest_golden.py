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
        # Declared counts from the vector input (the builder verifies them
        # against len(items)/sum(content bytes)).
        self.item_count = kwargs["item_count"]
        self.byte_count = kwargs["byte_count"]


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


# Frozen expected (manifest_hash, packet_hash) per vector at the ENG-CONTEXT-001
# final-correction-start head (c9e5918), keyed by the stable numeric prefix
# (rename-invariant). The final conformance correction must NOT change any valid
# manifest preimage: vector 010 was renamed (lf-crlf-trailing-newline →
# embedded-newline-content) with name/description only, and its expected hashes
# are unchanged. This map is the programmatic proof of that guarantee.
_CORRECTION_START_HASHES: dict[str, tuple[str, str]] = {
    "001": (
        "sha256:07def9ea36cb165c5a22e5ded1d389e7d167fae67db2790cea01ab661e60bbc4",
        "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    ),
    "002": (
        "sha256:d59169b4312ad3499f196049a1661b12b7ad9c5165a1cdf5ce85f7622b2155d1",
        "sha256:8b921c077d6fa6867673d6e29ce64f416b7a94eb25d387359b841fab64741aa1",
    ),
    "003": (
        "sha256:2daab65cb44b622dc437993b25452c77f90c35115bbfe18c79e3997ef8a51784",
        "sha256:e924d31b36e7dafc9faa5a77cd395c82bea05d48e293cb6f819b9c8cdb8bc1b5",
    ),
    "004": (
        "sha256:d11e6405ecbc6f5c6a5e1e03e4b7b07a0829f03fbc86cad6de0237f978db4aaa",
        "sha256:43be6c94e282a27fa0e07d9d60608b4bac41c53950f821c26977f37103dbc14e",
    ),
    "005": (
        "sha256:d365150a559fdf93bb8027f90346d3cbaa598e5bf9195d1bad10f02edbcfe77c",
        "sha256:8a1f73ffc44a49bbd5957ccd0f449b5b7eef298894e3886c8f076666e7c9fb99",
    ),
    "006": (
        "sha256:fa2d348298dc068396fb27332a58ee4facfb639f0a047dcc68c701f00c0a1018",
        "sha256:b23c14edc584a215e611066d0a2b24b9b88bac53acac2c6f150ccf91c708728b",
    ),
    "007": (
        "sha256:d68536139f07124443f3a6025ab7ca52e3ebecb17b84455a1db91361d4766c81",
        "sha256:2f82eaf9055b2a11e3fda8b06e209869d539fee33938c5eebdef4397abd7eb53",
    ),
    "008": (
        "sha256:0625a7d45ededdadbe29ca0934378deb275a662f660048550538464945940e29",
        "sha256:4735b44bb748ef41c0ecb7c22a19beba259854e53563dcd39dddb33f32413222",
    ),
    "009": (
        "sha256:50f1075140d96c41bd19e59fb56fcc65ecbdb08f690cd775db285cad4a3c6033",
        "sha256:e00b0203b8c661f2820054ce7d930b6ef35394feac2e1f34231e4cbba0b64bb6",
    ),
    "010": (
        "sha256:172e07c939267dfcd76c09b28ec5852f0a9cc4421b29d32e2e1a6c7a13b71157",
        "sha256:7d7b8647c58e15b246fab6bd1eb6bf3d0bbd77f881fd89af2a8dcd29c670cd14",
    ),
}


def test_valid_expected_hashes_unchanged_from_correction_start() -> None:
    """Every valid vector's expected (manifest_hash, packet_hash) must equal the
    frozen correction-start values. The vector-010 rename changed only its
    filename/name/description — no valid manifest preimage may change."""
    import re

    seen_prefixes: set[str] = set()
    for fname, raw in _VECTORS:
        match = re.match(r"(\d+)-", fname)
        assert match is not None, f"vector filename {fname!r} lacks a numeric prefix"
        prefix = match.group(1)
        seen_prefixes.add(prefix)
        expected_hashes = _CORRECTION_START_HASHES.get(prefix)
        assert expected_hashes is not None, f"unknown vector prefix {prefix!r}"
        expected_mh, expected_ph = expected_hashes
        actual_mh = raw["expected"]["manifest_hash"]
        actual_ph = raw["expected"]["packet_hash"]
        assert actual_mh == expected_mh, (
            f"{fname}: manifest_hash changed from correction-start value "
            f"({expected_mh} → {actual_mh}). A valid manifest preimage changed; "
            f"this correction must not alter any frozen expected hash."
        )
        assert actual_ph == expected_ph, (
            f"{fname}: packet_hash changed from correction-start value "
            f"({expected_ph} → {actual_ph})."
        )
    # Every documented correction-start vector must still be present.
    assert seen_prefixes == set(_CORRECTION_START_HASHES), (
        f"vector set drifted: missing={set(_CORRECTION_START_HASHES) - seen_prefixes}, "
        f"extra={seen_prefixes - set(_CORRECTION_START_HASHES)}"
    )


def test_vector_010_renamed_to_embedded_newline_content() -> None:
    """Vector 010 must be the renamed embedded-newline vector, not the stale
    trailing-newline name. Its content demonstrates LF inside an item's content
    in a coherent working-set-v1 packet (no trailing packet newline)."""
    filenames = {fname for fname, _ in _VECTORS}
    assert "010-embedded-newline-content.json" in filenames
    assert "010-lf-crlf-trailing-newline.json" not in filenames
    raw = next(v for n, v in _VECTORS if n == "010-embedded-newline-content.json")
    assert raw["name"] == "010-embedded-newline-content"
    # The name and description must describe embedded-newline behavior, not a
    # trailing-newline packet. The description MAY reference that a trailing
    # packet newline is a rejected negative case — it must not claim the packet
    # HAS one.
    assert "embedded" in raw["description"].lower()
    assert "trailing newline" not in raw["description"].lower() or "reject" in raw[
        "description"
    ].lower()
    # The packet is coherent: working_set is the working-set-v1 render of items
    # with an embedded LF in the first item's content, and NO trailing newline.
    working_set = raw["input"]["response"]["working_set"]
    assert not working_set.endswith("\n"), "vector 010 packet must not end with a newline"
    assert "\n" in working_set, "vector 010 must contain an embedded newline"
