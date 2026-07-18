#!/usr/bin/env python3
"""Verify the ENG-CONTEXT-001 golden conformance vectors.

Each vector passes through every stage:

  1. Build the manifest from the frozen finalized-response inputs.
  2. Compare it to ``expected.manifest``.
  3. Validate ``expected.manifest`` against the normative JSON Schema.
  4. Parse ``expected.manifest`` through ``ContextManifestV1.model_validate``.
  5. Parse ``expected.canonical_json`` through ``model_validate_json``.
  6. Reserialize both parsed representations.
  7. Prove canonical bytes and manifest hashes are unchanged.
  8. Verify response coherence (item count, byte count, working-set-v1 render).
  9. Verify packet, request, and item hashes.

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
    ContextManifestV1,
    ContextManifestVersionsV1,
    build_startup_context_manifest_v1,
    canonical_json_bytes,
    compute_manifest_hash,
    normative_manifest_schema_dict,
    reconstruct_working_set_v1,
    sha256_digest,
)

# jsonschema is a dev dependency; the verifier requires it for stage 3.
try:
    from jsonschema import Draft202012Validator
except ImportError as exc:  # pragma: no cover - environment guard
    print(
        "jsonschema is required to run the conformance verifier. "
        "Install dev dependencies: pip install -e '.[dev]'",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc

VECTORS_DIR = ROOT / "conformance" / "context-manifest-v1" / "vectors"

# Exact top-level input keys a context-manifest-v1 vector may carry. Extra
# additive fields on individual response item dicts are allowed (served recall
# items are loose dict[str, Any]); the top-level envelope and its named
# sub-objects are fixed by the vector contract.
_INPUT_TOP_KEYS = frozenset(
    {"response", "subject_context", "request_context", "decision_versions", "reorder_inputs"}
)
_RESPONSE_KEYS = frozenset(
    {
        "working_set",
        "item_count",
        "byte_count",
        "pinned_omitted_count",
        "omitted_count",
        "message",
        "items",
    }
)
_SUBJECT_KEYS = frozenset(
    {
        "tenant_id",
        "principal_id",
        "workspace_id",
        "memory_context_version",
        "memory_profile_id",
        "memory_profile_revision_id",
        "memory_profile_version",
    }
)


class _Response:
    def __init__(self, **kwargs: Any) -> None:
        self.working_set = kwargs["working_set"]
        self.items = kwargs["items"]
        self.pinned_omitted_count = kwargs.get("pinned_omitted_count", 0)
        self.omitted_count = kwargs.get("omitted_count", 0)
        self.message = kwargs.get("message")
        self.item_count = kwargs["item_count"]
        self.byte_count = kwargs["byte_count"]


def _fail(name: str, what: str, expected: Any, got: Any) -> None:
    print(f"FAIL {name}: {what}", file=sys.stderr)
    print(f"  expected: {expected}", file=sys.stderr)
    print(f"  got:      {got}", file=sys.stderr)
    raise SystemExit(1)


def _check_response_coherence(name: str, response: _Response) -> None:
    """Stage 8: response coherence the builder itself enforces.

    Also asserts the declared counts are nonnegative integers — a valid golden
    vector must never carry a negative declared count or budget (those are
    negative-fixture territory, not valid vectors).
    """
    for field in ("item_count", "byte_count", "pinned_omitted_count", "omitted_count"):
        value = getattr(response, field)
        if isinstance(value, bool) or not isinstance(value, int):
            _fail(name, f"coherence {field} type", "int (not bool)", type(value).__name__)
        if value < 0:
            _fail(name, f"coherence {field} nonnegative", ">= 0", value)
    if response.item_count != len(response.items):
        _fail(name, "coherence item_count", len(response.items), response.item_count)
    derived_bytes = sum(len(i["content"].encode("utf-8")) for i in response.items)
    if response.byte_count != derived_bytes:
        _fail(name, "coherence byte_count", derived_bytes, response.byte_count)
    reconstructed = reconstruct_working_set_v1(response.items)
    if response.working_set != reconstructed:
        _fail(name, "coherence working_set render", reconstructed, response.working_set)


def _verify_vector(path: Path, schema_validator: Draft202012Validator) -> None:
    vector = json.loads(path.read_text())
    name = vector["name"]
    inp = vector["input"]
    exp = vector["expected"]

    # Stage 0: filename/name consistency. The checked-in filename (minus .json)
    # must equal the vector's declared "name", so the two cannot drift.
    if path.stem != name:
        _fail(
            name,
            "filename/name consistency",
            f"filename stem {path.stem!r}",
            f"name {name!r}",
        )

    # Stage 0b: input envelope carries no unknown top-level keys, and the named
    # sub-objects carry no unknown keys where the vector contract forbids them.
    # (Individual response item dicts MAY carry additive served-decision fields;
    # only the manifest's selected fields are validated, so items are unchecked.)
    input_extra = set(inp) - _INPUT_TOP_KEYS
    if input_extra:
        _fail(name, "input unknown top-level keys", sorted(_INPUT_TOP_KEYS), sorted(input_extra))
    response_extra = set(inp["response"]) - _RESPONSE_KEYS
    if response_extra:
        _fail(
            name,
            "input.response unknown keys",
            sorted(_RESPONSE_KEYS),
            sorted(response_extra),
        )
    subject_extra = set(inp["subject_context"]) - _SUBJECT_KEYS
    if subject_extra:
        _fail(
            name,
            "input.subject_context unknown keys",
            sorted(_SUBJECT_KEYS),
            sorted(subject_extra),
        )

    subject = ContextManifestSubjectV1(**inp["subject_context"])
    request = ContextManifestRequestInputV1(
        requested=ContextManifestRequestedV1(**inp["request_context"]["requested"]),
        effective=ContextManifestEffectiveV1(**inp["request_context"]["effective"]),
        query_digest=inp["request_context"]["query_digest"],
    )
    versions = ContextManifestVersionsV1(**inp["decision_versions"])
    response = _Response(**inp["response"])

    # Stage 8: response coherence (before building). Also asserts the declared
    # counts (item_count, byte_count, omission counts) are nonnegative integers.
    _check_response_coherence(name, response)

    # Stages 1-2: build from inputs; rebuilt manifest object equals frozen.
    manifest = build_startup_context_manifest_v1(
        response=response,
        subject_context=subject,
        request_context=request,
        decision_versions=versions,
    )
    rebuilt = manifest.model_dump(mode="json", exclude_none=False, by_alias=True)
    if rebuilt != exp["manifest"]:
        _fail(name, "manifest object mismatch", exp["manifest"], rebuilt)

    # Stage 3: validate frozen manifest against the normative JSON Schema.
    schema_validator.validate(exp["manifest"])

    # Stage 4: parse frozen wire dict through the strict model.
    m_from_dict = ContextManifestV1.model_validate(exp["manifest"])

    # Stage 5: parse frozen canonical JSON through model_validate_json.
    m_from_json = ContextManifestV1.model_validate_json(exp["canonical_json"])

    # Stages 6-7: reserialize both parsed representations; bytes + hash unchanged.
    canon_from_dict = canonical_json_bytes(
        m_from_dict.model_dump(mode="json", exclude_none=False, by_alias=True)
    ).decode("utf-8")
    canon_from_json = canonical_json_bytes(
        m_from_json.model_dump(mode="json", exclude_none=False, by_alias=True)
    ).decode("utf-8")
    if canon_from_dict != exp["canonical_json"]:
        _fail(name, "model_validate round-trip canonical", exp["canonical_json"], canon_from_dict)
    if canon_from_json != exp["canonical_json"]:
        _fail(
            name,
            "model_validate_json round-trip canonical",
            exp["canonical_json"],
            canon_from_json,
        )
    if compute_manifest_hash(m_from_dict) != exp["manifest_hash"]:
        _fail(
            name,
            "model_validate manifest_hash",
            exp["manifest_hash"],
            compute_manifest_hash(m_from_dict),
        )
    if compute_manifest_hash(m_from_json) != exp["manifest_hash"]:
        _fail(
            name,
            "model_validate_json manifest_hash",
            exp["manifest_hash"],
            compute_manifest_hash(m_from_json),
        )

    # manifest_hash recomputed over the rebuilt manifest.
    if compute_manifest_hash(manifest) != exp["manifest_hash"]:
        _fail(name, "manifest_hash", exp["manifest_hash"], compute_manifest_hash(manifest))

    # The frozen canonical bytes must hash to the frozen manifest_hash.
    if sha256_digest(exp["canonical_json"].encode("utf-8")) != exp["manifest_hash"]:
        _fail(
            name,
            "canonical_json byte hash",
            exp["manifest_hash"],
            sha256_digest(exp["canonical_json"].encode("utf-8")),
        )

    # Stage 9: packet_hash recomputed from response.working_set bytes.
    packet_hash = sha256_digest(inp["response"]["working_set"].encode("utf-8"))
    if packet_hash != exp["packet_hash"]:
        _fail(name, "packet_hash", exp["packet_hash"], packet_hash)

    # request_digest recomputed.
    if manifest.request.request_digest != exp["request_digest"]:
        _fail(name, "request_digest", exp["request_digest"], manifest.request.request_digest)

    # per-item served_content_hash recomputed from served content bytes.
    items = inp["response"]["items"]
    if len(exp["served_content_hashes"]) != len(items):
        _fail(
            name,
            "served_content_hashes length",
            str(len(exp["served_content_hashes"])),
            str(len(items)),
        )
    for i, raw_item in enumerate(items):
        content_hash = sha256_digest(raw_item["content"].encode("utf-8"))
        if content_hash != exp["served_content_hashes"][i]:
            _fail(name, f"served_content_hash[{i}]", exp["served_content_hashes"][i], content_hash)

    print(f"  OK  {path.name}: manifest_hash={exp['manifest_hash'][:24]}...")


def _check_schema_validity() -> None:
    """Stage -1: the generated normative schema is itself valid Draft 2020-12."""
    schema = normative_manifest_schema_dict()
    Draft202012Validator.check_schema(schema)
    # The profile all-or-none coherence oneOf must be present on the subject
    # definition (augmented after model_json_schema, which cannot emit the
    # semantic model_validator).
    subject_def = schema["$defs"]["ContextManifestSubjectV1"]
    if "oneOf" not in subject_def:
        raise SystemExit(
            "DRIFT: normative schema is missing the ContextManifestSubjectV1 "
            "profile-coherence oneOf augmentation."
        )
    branches = subject_def["oneOf"]
    if len(branches) != 2:
        raise SystemExit(
            f"DRIFT: profile-coherence oneOf has {len(branches)} branches (expected 2)."
        )


def main() -> int:
    vectors = sorted(VECTORS_DIR.glob("*.json"))
    if not vectors:
        print(f"no vectors found in {VECTORS_DIR}", file=sys.stderr)
        return 1
    # Stage -1: the generated schema is valid Draft 2020-12 and carries the
    # profile-coherence oneOf augmentation.
    _check_schema_validity()
    print(f"Verifying {len(vectors)} context-manifest-v1 vectors (Python)...")
    schema_validator = Draft202012Validator(normative_manifest_schema_dict())
    for path in vectors:
        _verify_vector(path, schema_validator)
    print(f"All {len(vectors)} vectors verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
