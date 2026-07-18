"""JSON Schema tests for the Context Manifest v1 contract (ENG-CONTEXT-001).

Proves the checked-in normative schema:
  - cannot drift from the strict wire model (drift test);
  - accepts every checked-in golden manifest (positive validation);
  - rejects malformed manifests (negative validation).

Uses ``jsonschema`` (development-only dependency). The production package must
not gain a runtime dependency on jsonschema; schema validation here is a
contract-integrity gate, not a runtime path.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

jsonschema = pytest.importorskip("jsonschema")
from jsonschema import Draft202012Validator  # noqa: E402

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPOSITORY_ROOT / "schemas" / "context-manifest-v1.schema.json"
VECTORS_DIR = (
    REPOSITORY_ROOT / "conformance" / "context-manifest-v1" / "vectors"
)

from engram.context_manifest import normative_manifest_schema_dict  # noqa: E402


def _load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


def _load_vectors() -> list[tuple[str, dict[str, Any]]]:
    if not VECTORS_DIR.exists():
        return []
    return sorted((p.name, json.loads(p.read_text())) for p in VECTORS_DIR.glob("*.json"))


_VECTORS = _load_vectors()


# ─── Drift: checked-in schema == model-generated schema ────────────────


def test_checked_in_schema_matches_model() -> None:
    """The checked-in schema must equal the model-generated schema."""
    checked_in = SCHEMA_PATH.read_text()
    generated = json.dumps(
        normative_manifest_schema_dict(), indent=2, ensure_ascii=False
    ) + "\n"
    assert checked_in == generated, (
        "schemas/context-manifest-v1.schema.json drifted from the model. "
        "Regenerate: python scripts/generate_context_manifest_schema.py"
    )


def test_schema_generator_check_passes() -> None:
    """The generator's own --check drift guard passes (belt-and-suspenders)."""
    result = subprocess.run(
        [sys.executable, "scripts/generate_context_manifest_schema.py", "--check"],
        capture_output=True,
        text=True,
        cwd=REPOSITORY_ROOT,
    )
    assert result.returncode == 0, (
        f"--check failed:\nstdout:{result.stdout}\nstderr:{result.stderr}"
    )


# ─── Positive: every golden manifest validates ─────────────────────────


@pytest.mark.parametrize("name", [n for n, _ in _VECTORS] or ["none"])
def test_golden_manifest_validates_against_schema(name: str) -> None:
    if name == "none":
        pytest.fail(f"no golden vectors found in {VECTORS_DIR}")
    raw = next(v for n_, v in _VECTORS if n_ == name)
    schema = _load_schema()
    Draft202012Validator(schema).validate(raw["expected"]["manifest"])


# ─── Negative: malformed manifests are rejected ────────────────────────


def _valid_manifest() -> dict[str, Any]:
    """A known-valid manifest (copy of a golden vector) to mutate."""
    raw = next(v for n_, v in _VECTORS if n_ == "002-mixed-pinned-scored.json")
    return copy.deepcopy(raw["expected"]["manifest"])


def _assert_rejected(malformed: dict[str, Any], *, label: str) -> None:
    schema = _load_schema()
    with pytest.raises(jsonschema.exceptions.ValidationError, match=r".*"):
        Draft202012Validator(schema).validate(malformed)
    # (the pytest.raises above is the assertion; label is for diagnostics)
    assert label


def test_missing_schema_rejected() -> None:
    m = _valid_manifest()
    del m["schema"]
    _assert_rejected(m, label="missing schema")


def test_wrong_schema_rejected() -> None:
    m = _valid_manifest()
    m["schema"] = "not.engram"
    _assert_rejected(m, label="wrong schema")


def test_missing_schema_version_rejected() -> None:
    m = _valid_manifest()
    del m["schema_version"]
    _assert_rejected(m, label="missing schema_version")


def test_wrong_canonicalization_rejected() -> None:
    m = _valid_manifest()
    m["canonicalization"] = "json-sort-keys"
    _assert_rejected(m, label="wrong canonicalization")


def test_semantic_mode_in_v1_rejected() -> None:
    m = _valid_manifest()
    m["mode"] = "semantic"
    _assert_rejected(m, label="semantic mode")


def test_malformed_uuid_rejected() -> None:
    m = _valid_manifest()
    m["subject"]["tenant_id"] = "not-a-uuid"
    _assert_rejected(m, label="malformed tenant UUID")


def test_uppercase_noncanonical_uuid_rejected() -> None:
    m = _valid_manifest()
    # Use a UUID with hex letters; uppercase is noncanonical (the regex requires
    # lowercase [0-9a-f]).
    m["subject"]["tenant_id"] = "ABCDEF12-0000-0000-0000-000000000001"
    _assert_rejected(m, label="uppercase UUID")


def test_malformed_hash_rejected() -> None:
    m = _valid_manifest()
    m["packet"]["hash"] = "not-a-hash"
    _assert_rejected(m, label="malformed packet hash")


def test_uppercase_hash_rejected() -> None:
    m = _valid_manifest()
    m["packet"]["hash"] = "sha256:" + "A" * 64
    _assert_rejected(m, label="uppercase hash")


def test_negative_item_count_rejected() -> None:
    m = _valid_manifest()
    m["result"]["item_count"] = -1
    _assert_rejected(m, label="negative item_count")


def test_negative_byte_count_rejected() -> None:
    m = _valid_manifest()
    m["result"]["served_content_byte_count"] = -5
    _assert_rejected(m, label="negative byte count")


def test_invalid_visibility_rejected() -> None:
    m = _valid_manifest()
    m["items"][0]["visibility"] = "secret"
    _assert_rejected(m, label="invalid visibility")


def test_unknown_top_level_field_rejected() -> None:
    m = _valid_manifest()
    m["unknown_field"] = "inject"
    _assert_rejected(m, label="unknown top-level field")


def test_unknown_item_field_rejected() -> None:
    m = _valid_manifest()
    m["items"][0]["unknown_item_field"] = "inject"
    _assert_rejected(m, label="unknown item field")


def test_omitted_required_null_valued_field_rejected() -> None:
    # Omitting a required field that is normally null (e.g. result.message)
    # must be rejected — the manifest requires explicit nulls.
    m = _valid_manifest()
    del m["result"]["message"]
    _assert_rejected(m, label="omitted required null field")


# ─── Schema is itself valid Draft 2020-12 ──────────────────────────────


def test_generated_schema_passes_draft2020_check_schema() -> None:
    """The generated normative schema must be a valid Draft 2020-12 schema."""
    Draft202012Validator.check_schema(_load_schema())


# ─── Profile all-or-none coherence (schema-encoded) ────────────────────

# Canonical UUIDs / version used to build a profiled subject.
_PROFILE_ID = "00000000-0000-0000-0000-00000000009a"
_PROFILE_REV = "00000000-0000-0000-0000-00000000009b"
_PROFILE_VERSION = 3

# Every partial-profile combination: set some of the three profile fields, leave
# the others null. All six must be rejected by both Pydantic and the schema.
_PARTIAL_PROFILE_COMBOS: list[tuple[str, dict[str, Any]]] = [
    (
        "id-only",
        {"memory_profile_id": _PROFILE_ID},
    ),
    (
        "revision-only",
        {"memory_profile_revision_id": _PROFILE_REV},
    ),
    (
        "version-only",
        {"memory_profile_version": _PROFILE_VERSION},
    ),
    (
        "id+revision",
        {"memory_profile_id": _PROFILE_ID, "memory_profile_revision_id": _PROFILE_REV},
    ),
    (
        "id+version",
        {"memory_profile_id": _PROFILE_ID, "memory_profile_version": _PROFILE_VERSION},
    ),
    (
        "revision+version",
        {"memory_profile_revision_id": _PROFILE_REV, "memory_profile_version": _PROFILE_VERSION},
    ),
]


def _subject_with_profile(
    base_vector: str = "003-profile-bound-workspace.json",
) -> dict[str, Any]:
    """A fully-profiled (all three set) subject from a golden vector."""
    raw = next(v for n_, v in _VECTORS if n_ == base_vector)
    return copy.deepcopy(raw["expected"]["manifest"]["subject"])


@pytest.mark.parametrize("label,overrides", _PARTIAL_PROFILE_COMBOS)
def test_schema_rejects_partial_profile(label: str, overrides: dict[str, Any]) -> None:
    """The normative schema rejects every partial-profile combination."""
    # Start from an unprofiled subject and apply only the override fields.
    m = _valid_manifest()
    for key, value in overrides.items():
        m["subject"][key] = value
    _assert_rejected(m, label=f"partial profile: {label}")


def test_schema_accepts_all_null_profile() -> None:
    """The normative schema accepts an unprofiled (all-null) subject."""
    m = _valid_manifest()
    # _valid_manifest() is already unprofiled; assert it validates cleanly.
    schema = _load_schema()
    Draft202012Validator(schema).validate(m)


def test_schema_accepts_all_valid_profile() -> None:
    """The normative schema accepts a fully-profiled subject."""
    m = _valid_manifest()
    m["subject"] = _subject_with_profile()
    schema = _load_schema()
    Draft202012Validator(schema).validate(m)


@pytest.mark.parametrize("label,overrides", _PARTIAL_PROFILE_COMBOS)
def test_pydantic_model_rejects_partial_profile(label: str, overrides: dict[str, Any]) -> None:
    """The strict wire model rejects every partial-profile combination."""
    from engram.context_manifest import ContextManifestSubjectV1

    fields = {
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "principal_id": "00000000-0000-0000-0000-000000000002",
        "workspace_id": None,
        "memory_context_version": "memory-context-v2",
        "memory_profile_id": None,
        "memory_profile_revision_id": None,
        "memory_profile_version": None,
    }
    fields.update(overrides)
    with pytest.raises(Exception):  # noqa: B017
        ContextManifestSubjectV1(**fields)


def test_pydantic_model_accepts_all_null_profile() -> None:
    from engram.context_manifest import ContextManifestSubjectV1

    ContextManifestSubjectV1(
        tenant_id="00000000-0000-0000-0000-000000000001",
        principal_id="00000000-0000-0000-0000-000000000002",
        workspace_id=None,
        memory_context_version="memory-context-v2",
        memory_profile_id=None,
        memory_profile_revision_id=None,
        memory_profile_version=None,
    )


def test_pydantic_model_accepts_all_valid_profile() -> None:
    from engram.context_manifest import ContextManifestSubjectV1

    ContextManifestSubjectV1(
        tenant_id="00000000-0000-0000-0000-000000000001",
        principal_id="00000000-0000-0000-0000-000000000002",
        workspace_id=None,
        memory_context_version="memory-context-v2",
        memory_profile_id=_PROFILE_ID,
        memory_profile_revision_id=_PROFILE_REV,
        memory_profile_version=_PROFILE_VERSION,
    )
