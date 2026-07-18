#!/usr/bin/env python3
"""Generate the ENG-CONTEXT-001 golden conformance vectors.

This script is the **single source of truth** for the frozen expected hashes in
``conformance/context-manifest-v1/vectors/*.json``. Run it to (re)generate the
vectors; the expected ``manifest_hash``, ``packet_hash``, per-item
``served_content_hash``, ``request_digest``, and ``canonical_bytes`` are frozen
from the reference Python builder.

Usage::

    python scripts/generate_context_manifest_vectors.py

The generated vectors are immutable once released (see
docs/context-manifest-v1.md §Versioning). Corrections require a new vector
version or schema version, not rewriting published expected hashes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Allow running from repo root without an installed package.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engram.context_manifest import (  # noqa: E402
    MANIFEST_CONTRACT_VERSION,
    MEMORY_CONTEXT_VERSION,
    PACKET_RENDER_VERSION,
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

# Fixed UUIDs so vectors are byte-stable across regenerations (the manifest
# stores UUIDs as lowercase canonical strings; fixing them removes a source of
# hash nondeterminism between regenerations).
TENANT = "00000000-0000-0000-0000-000000000001"
PRINCIPAL = "00000000-0000-0000-0000-000000000002"
WORKSPACE = "00000000-0000-0000-0000-000000000003"
ITEM_1 = "00000000-0000-0000-0000-0000000000a1"
ITEM_2 = "00000000-0000-0000-0000-0000000000a2"
ITEM_3 = "00000000-0000-0000-0000-0000000000a3"
PROFILE = "00000000-0000-0000-0000-00000000009a"
PROFILE_REV = "00000000-0000-0000-0000-00000000009b"


def _versions() -> ContextManifestVersionsV1:
    return ContextManifestVersionsV1(
        scoring_version="v1",
        config_version="v1",
        candidate_strategy_version="startup-candidates-v1",
        manifest_contract_version=MANIFEST_CONTRACT_VERSION,
        packet_render_version=PACKET_RENDER_VERSION,
    )


def _item(
    *,
    id_: str,
    content: str,
    kind: str = "fact",
    score: float | None = 0.8123,
    reasons: list[str] | None = None,
    warnings: list[str] | None = None,
    pinned: bool = False,
    importance: float = 0.9,
    source_trust: float = 0.8,
    memory_confidence: float = 0.75,
    human_verified: bool = True,
    review_status: str = "active",
    authority: int = 10,
    visibility: str = "private",
    workspace_id: str | None = None,
    conflict_type: str | None = None,
    conflict_resolution_status: str | None = None,
) -> dict[str, Any]:
    return {
        "id": id_,
        "kind": kind,
        "content": content,
        "review_status": review_status,
        "score": score,
        "reasons": list(reasons) if reasons is not None else ["importance=0.90"],
        "warnings": list(warnings) if warnings is not None else [],
        "pinned": pinned,
        "importance": importance,
        "source_trust": source_trust,
        "memory_confidence": memory_confidence,
        "human_verified": human_verified,
        "authority": authority,
        "visibility": visibility,
        "workspace_id": workspace_id,
        "conflict_type": conflict_type,
        "conflict_resolution_status": conflict_resolution_status,
    }


def _render(items: list[dict[str, Any]]) -> str:
    return "\n".join(f"[{i['kind']}] {i['content']}" for i in items)


def _build_vector(
    *,
    name: str,
    description: str,
    items: list[dict[str, Any]],
    pinned_omitted_count: int = 0,
    omitted_count: int = 0,
    message: str | None = None,
    subject: ContextManifestSubjectV1 | None = None,
    request: ContextManifestRequestInputV1 | None = None,
    reorder_inputs: bool = False,
) -> dict[str, Any]:
    if subject is None:
        subject = ContextManifestSubjectV1(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            workspace_id=None,
            memory_context_version=MEMORY_CONTEXT_VERSION,
            memory_profile_id=None,
            memory_profile_revision_id=None,
            memory_profile_version=None,
        )
    if request is None:
        request = ContextManifestRequestInputV1(
            requested=ContextManifestRequestedV1(
                workspace_supplied=False,
                byte_budget=None,
                token_budget=None,
                item_budget=None,
            ),
            effective=ContextManifestEffectiveV1(
                workspace_id=None,
                byte_budget=4096,
                token_budget=None,
                item_budget=None,
            ),
            query_digest=None,
        )

    working_set = _render(items)
    response = _Response(
        working_set=working_set,
        items=items,
        pinned_omitted_count=pinned_omitted_count,
        omitted_count=omitted_count,
        message=message,
    )
    manifest = build_startup_context_manifest_v1(
        response=response,
        subject_context=subject,
        request_context=request,
        decision_versions=_versions(),
    )
    manifest_obj = manifest.model_dump(mode="json", exclude_none=False, by_alias=True)
    canonical = canonical_json_bytes(manifest_obj).decode("utf-8")

    return {
        "name": name,
        "description": description,
        "schema": "engram.context-manifest-vector",
        "schema_version": "1.0",
        "input": {
            # The finalized recall response (the only served-data source the
            # builder consumes). Items carry their raw served `content` so the
            # verifiers can independently hash exact UTF-8 bytes.
            "response": {
                "working_set": working_set,
                "item_count": len(items),
                "byte_count": sum(len(i["content"].encode("utf-8")) for i in items),
                "pinned_omitted_count": pinned_omitted_count,
                "omitted_count": omitted_count,
                "message": message,
                "items": items,
            },
            "subject_context": subject.model_dump(mode="json"),
            "request_context": request.model_dump(mode="json"),
            "decision_versions": _versions().model_dump(mode="json"),
            # Whether the builder was fed inputs in a different key order (for
            # the key-order-equivalence vector). Not consumed by the verifier;
            # documented for humans.
            "reorder_inputs": reorder_inputs,
        },
        "expected": {
            "manifest": manifest_obj,
            "canonical_json": canonical,
            "manifest_hash": compute_manifest_hash(manifest),
            "packet_hash": manifest.packet.hash,
            "request_digest": manifest.request.request_digest,
            "served_content_hashes": [
                i.served_content_hash for i in manifest.items
            ],
        },
    }


class _Response:
    def __init__(self, **kwargs: Any) -> None:
        self.working_set = kwargs["working_set"]
        self.items = kwargs["items"]
        self.pinned_omitted_count = kwargs.get("pinned_omitted_count", 0)
        self.omitted_count = kwargs.get("omitted_count", 0)
        self.message = kwargs.get("message")
        # Declared counts are derived here (the builder verifies they match
        # len(items)/sum(content bytes) before trusting them). Carried in the
        # vector input so the verifiers exercise the coherence checks.
        self.item_count = kwargs.get("item_count", len(self.items))
        self.byte_count = kwargs.get(
            "byte_count",
            sum(len(i["content"].encode("utf-8")) for i in self.items),
        )


def _build_all() -> list[dict[str, Any]]:
    vectors: list[dict[str, Any]] = []

    # 1. Empty startup recall.
    vectors.append(
        _build_vector(
            name="001-empty-startup",
            description="Empty startup recall: no items, empty packet.",
            items=[],
            omitted_count=0,
        )
    )

    # 2. Mixed pinned and scored items.
    vectors.append(
        _build_vector(
            name="002-mixed-pinned-scored",
            description="Mixed pinned (score=null) and scored items.",
            items=[
                _item(id_=ITEM_1, content="alpha", pinned=True, score=None, reasons=[]),
                _item(id_=ITEM_2, content="beta", score=0.55, kind="preference"),
            ],
            omitted_count=4,
        )
    )

    # 3. Profile-bound workspace recall.
    vectors.append(
        _build_vector(
            name="003-profile-bound-workspace",
            description="Profile-bound workspace recall: non-null profile and workspace.",
            items=[
                _item(
                    id_=ITEM_1,
                    content="workspace secret",
                    visibility="workspace",
                    workspace_id=WORKSPACE,
                ),
            ],
            subject=ContextManifestSubjectV1(
                tenant_id=TENANT,
                principal_id=PRINCIPAL,
                workspace_id=WORKSPACE,
                memory_context_version=MEMORY_CONTEXT_VERSION,
                memory_profile_id=PROFILE,
                memory_profile_revision_id=PROFILE_REV,
                memory_profile_version=3,
            ),
            request=ContextManifestRequestInputV1(
                requested=ContextManifestRequestedV1(
                    workspace_supplied=True,
                    byte_budget=None,
                    token_budget=None,
                    item_budget=None,
                ),
                effective=ContextManifestEffectiveV1(
                    workspace_id=WORKSPACE,
                    byte_budget=4096,
                    token_budget=None,
                    item_budget=None,
                ),
                query_digest=None,
            ),
        )
    )

    # 4. Unicode content and escaping (emoji, non-ASCII, quotes, backslash).
    vectors.append(
        _build_vector(
            name="004-unicode-and-escaping",
            description=(
                "Unicode content and JSON escaping: emoji, non-ASCII, quotes, "
                "backslash, newline. Proves exact Unicode scalar preservation."
            ),
            items=[
                _item(
                    id_=ITEM_1,
                    content='héllo "wörld" \\ \n 🚀 𝔸',
                    reasons=['unicode="héllo"'],
                ),
                _item(id_=ITEM_2, content="日本語のテキスト", kind="preference"),
            ],
        )
    )

    # 5. Null fields (pinned score null, all optional nulls).
    vectors.append(
        _build_vector(
            name="005-null-fields",
            description="Null optional fields: pinned score=null, all profile/conflict nulls.",
            items=[
                _item(id_=ITEM_1, content="only", pinned=True, score=None, reasons=[]),
            ],
        )
    )

    # 6. -0, integer, and representative finite floating-point values.
    vectors.append(
        _build_vector(
            name="006-number-boundaries",
            description=(
                "Number boundaries: -0.0 (canonicalized to 0), integer-valued "
                "floats, small and large finite floats. Proves ECMAScript "
                "number serialization."
            ),
            items=[
                _item(
                    id_=ITEM_1,
                    content="nums",
                    score=-0.0,
                    importance=1.0,
                    source_trust=0.0,
                    memory_confidence=0.000001,
                ),
                _item(
                    id_=ITEM_2,
                    content="nums2",
                    score=0.5,
                    importance=100.0,
                    source_trust=1e21,
                    memory_confidence=1.5e-7,
                ),
            ],
        )
    )

    # 7. Object inputs in different key order produce the same canonical bytes.
    #    This vector's manifest is built normally; the verifier proves that
    #    re-serializing the expected.manifest with a different key order yields
    #    the same manifest_hash (verified by canonicalizing the manifest object,
    #    not the input).
    vectors.append(
        _build_vector(
            name="007-key-order-equivalence",
            description=(
                "Key-order equivalence: the expected manifest canonicalizes "
                "identically regardless of object key insertion order. The "
                "verifier independently re-canonicalizes the manifest object."
            ),
            items=[
                _item(id_=ITEM_1, content="order-a"),
                _item(id_=ITEM_2, content="order-b", kind="preference"),
            ],
            reorder_inputs=True,
        )
    )

    # 8. Item-order change producing a different manifest and packet hash.
    #    (Same content set as 002 but reversed; the verifier confirms a
    #    different manifest_hash/packet_hash than 002 — documented, not
    #    cross-asserted in the verifier to keep vectors independent.)
    vectors.append(
        _build_vector(
            name="008-item-order-change",
            description=(
                "Item-order change: reversed order vs 002. Produces a distinct "
                "manifest_hash and packet_hash (item order is significant)."
            ),
            items=[
                _item(id_=ITEM_2, content="beta", score=0.55, kind="preference"),
                _item(
                    id_=ITEM_1, content="alpha", pinned=True, score=None, reasons=[]
                ),
            ],
            omitted_count=4,
        )
    )

    # 9. Whitespace/case-only content change proving the exact served-content
    #    hash changes (and would change packet + manifest hashes).
    vectors.append(
        _build_vector(
            name="009-whitespace-case-content",
            description=(
                "Whitespace/case content: trailing-space + mixed-case content. "
                "The served_content_hash reflects exact bytes (no "
                "case/whitespace normalization)."
            ),
            items=[
                _item(id_=ITEM_1, content="Alpha  "),  # trailing spaces
            ],
        )
    )

    # 10. Exact-byte LF-joined multi-line packet (working-set-v1 render).
    #     The packet is COHERENT: its working_set equals the working-set-v1
    #     render of its items (multi-line content + LF join, no trailing
    #     newline). It proves exact-byte packet hashing: the LF separators
    #     and an embedded newline in content are preserved verbatim. The
    #     incoherent variants (CRLF, trailing newline) are negative fixtures
    #     the builder rejects — see test_context_manifest_unit coherence tests.
    items_lf = [
        _item(id_=ITEM_1, content="line one\nwith embedded newline"),
        _item(id_=ITEM_2, content="line two", kind="preference"),
    ]
    manifest_obj = _build_vector_raw(
        items=items_lf, working_set=_render(items_lf)
    )
    vectors.append(manifest_obj)

    return vectors


def _build_vector_raw(
    *, items: list[dict[str, Any]], working_set: str
) -> dict[str, Any]:
    """Build a vector with an author-controlled working_set (for vector 10)."""
    response = _Response(working_set=working_set, items=items, omitted_count=0)
    manifest = build_startup_context_manifest_v1(
        response=response,
        subject_context=ContextManifestSubjectV1(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            workspace_id=None,
            memory_context_version=MEMORY_CONTEXT_VERSION,
            memory_profile_id=None,
            memory_profile_revision_id=None,
            memory_profile_version=None,
        ),
        request_context=ContextManifestRequestInputV1(
            requested=ContextManifestRequestedV1(
                workspace_supplied=False,
                byte_budget=None,
                token_budget=None,
                item_budget=None,
            ),
            effective=ContextManifestEffectiveV1(
                workspace_id=None,
                byte_budget=4096,
                token_budget=None,
                item_budget=None,
            ),
            query_digest=None,
        ),
        decision_versions=_versions(),
    )
    manifest_obj = manifest.model_dump(mode="json", exclude_none=False, by_alias=True)
    canonical = canonical_json_bytes(manifest_obj).decode("utf-8")
    return {
        "name": "010-embedded-newline-content",
        "description": (
            "Embedded-newline content: an item's content contains an LF, which "
            "the working-set-v1 render preserves verbatim between the [kind] "
            "lines. The packet is COHERENT (no trailing packet newline; LF "
            "separates rendered items). Proves embedded LF is preserved exactly "
            "in served_content_hash and packet_hash. Trailing-packet-newline "
            "and CRLF-separator variants are rejected by the builder — they are "
            "negative cases, not valid vectors."
        ),
        "schema": "engram.context-manifest-vector",
        "schema_version": "1.0",
        "input": {
            "response": {
                "working_set": working_set,
                "item_count": len(items),
                "byte_count": sum(len(i["content"].encode("utf-8")) for i in items),
                "pinned_omitted_count": 0,
                "omitted_count": 0,
                "message": None,
                "items": items,
            },
            "subject_context": ContextManifestSubjectV1(
                tenant_id=TENANT,
                principal_id=PRINCIPAL,
                workspace_id=None,
                memory_context_version=MEMORY_CONTEXT_VERSION,
                memory_profile_id=None,
                memory_profile_revision_id=None,
                memory_profile_version=None,
            ).model_dump(mode="json"),
            "request_context": ContextManifestRequestInputV1(
                requested=ContextManifestRequestedV1(
                    workspace_supplied=False,
                    byte_budget=None,
                    token_budget=None,
                    item_budget=None,
                ),
                effective=ContextManifestEffectiveV1(
                    workspace_id=None,
                    byte_budget=4096,
                    token_budget=None,
                    item_budget=None,
                ),
                query_digest=None,
            ).model_dump(mode="json"),
            "decision_versions": _versions().model_dump(mode="json"),
            "reorder_inputs": False,
        },
        "expected": {
            "manifest": manifest_obj,
            "canonical_json": canonical,
            "manifest_hash": compute_manifest_hash(manifest),
            "packet_hash": manifest.packet.hash,
            "request_digest": manifest.request.request_digest,
            "served_content_hashes": [i.served_content_hash for i in manifest.items],
        },
    }


def main() -> int:
    VECTORS_DIR.mkdir(parents=True, exist_ok=True)
    vectors = _build_all()
    for v in vectors:
        path = VECTORS_DIR / f"{v['name']}.json"
        path.write_text(json.dumps(v, indent=2, ensure_ascii=False) + "\n")
        short_hash = v["expected"]["manifest_hash"][:24]
        print(f"wrote {path.relative_to(ROOT)} (manifest_hash={short_hash}...)")
    # Also verify round-trip: re-load and re-hash each vector.
    print("\n--- self-check: re-load and re-hash ---")
    for v in vectors:
        path = VECTORS_DIR / f"{v['name']}.json"
        loaded = json.loads(path.read_text())
        recomputed = sha256_digest(
            canonical_json_bytes(loaded["expected"]["manifest"]).decode("utf-8").encode("utf-8")
        )
        assert recomputed == loaded["expected"]["manifest_hash"], v["name"]
    print(f"OK: {len(vectors)} vectors generated and self-consistent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
