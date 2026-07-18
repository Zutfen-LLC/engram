"""Cross-language negative-fixture and JavaScript-verifier tests (ENG-CONTEXT-001).

Proves the shared negative conformance fixtures are rejected by BOTH the Python
and JavaScript verifiers, and that the JavaScript verifier independently
enforces canonical UUIDs, SHA-256 hashes, the visibility vocabulary, nonnegative
counts/budgets, profile all-or-none coherence, and the startup
effective.item_budget invariant. These tests shell out to the standalone
verifier scripts so the contract runners stay the source of truth.

Node is required only to run the JavaScript half; if `node` is absent the
JavaScript-parameterized tests skip (the hosted CI `conformance-vectors` job is
the authoritative run).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
NEGATIVES_DIR = (
    REPOSITORY_ROOT / "conformance" / "context-manifest-v1" / "negative"
)
PY_RUNNER = REPOSITORY_ROOT / "scripts" / "verify_context_manifest_negatives.py"
JS_RUNNER = (
    REPOSITORY_ROOT / "conformance" / "context-manifest-v1" / "verify_negatives.mjs"
)
CROSS_RUNNER = (
    REPOSITORY_ROOT / "conformance" / "context-manifest-v1" / "run_cross_language.py"
)

_HAS_NODE = shutil.which("node") is not None


def _load_negatives() -> list[tuple[str, dict[str, Any]]]:
    if not NEGATIVES_DIR.exists():
        return []
    return sorted((p.name, json.loads(p.read_text())) for p in NEGATIVES_DIR.glob("*.json"))


_NEGATIVES = _load_negatives()
_NEG_NAMES = [name for name, _ in _NEGATIVES]


def _python_for_subprocess() -> str:
    """Resolve a Python interpreter that can import engram + jsonschema.

    ``sys.executable`` is correct in normal environments (hosted CI, a clean
    venv with the project installed). Some IDE/agent sandboxes intercept
    ``sys.executable`` and report a host application binary instead of the real
    interpreter; in that case fall back to a discovered ``python3``. The
    verifier scripts import ``engram.context_manifest`` and ``jsonschema``, so
    the chosen interpreter must resolve both AND emit a sentinel (exit code
    alone is not enough — a host application may exit 0 while ignoring its
    args). The current ``PYTHONPATH`` is inherited so a non-isolated system
    interpreter can still resolve the project deps.
    """
    sentinel = "ENGRAM_PY_OK"
    env = dict(os.environ)
    for candidate in (sys.executable, shutil.which("python3"), shutil.which("python3.14")):
        if not candidate:
            continue
        probe = subprocess.run(
            [
                candidate,
                "-c",
                "import engram.context_manifest, jsonschema; "
                f"print({sentinel!r})",
            ],
            capture_output=True,
            text=True,
            cwd=REPOSITORY_ROOT,
            env=env,
        )
        if probe.returncode == 0 and sentinel in probe.stdout:
            return candidate
    return sys.executable  # last resort; let the subprocess failure surface


def test_negative_fixture_set_covers_required_invariants() -> None:
    """The shared negative set must cover every documented rejection boundary."""
    assert _NEG_NAMES, f"no negative fixtures found in {NEGATIVES_DIR}"
    # Required rejection proofs (by fixture filename stem) the task enumerates.
    required = {
        "malformed-tenant-uuid",
        "uppercase-tenant-uuid",
        "malformed-item-uuid",
        "invalid-visibility",
        "negative-response-item-count",
        "negative-response-byte-count",
        "negative-omission-count",
        "negative-requested-budget",
        "negative-effective-budget",
        "non-null-effective-startup-item-budget",
        "profile-id-only",
        "profile-revision-only",
        "profile-version-only",
        "profile-id-revision-no-version",
        "profile-id-version-no-revision",
        "profile-revision-version-no-id",
        "malformed-sha256",
        "uppercase-sha256",
        "string-boolean",
        "mixed-type-reasons",
    }
    present = set(p.removesuffix(".json") for p in _NEG_NAMES)
    missing = required - present
    assert not missing, f"missing required negative fixtures: {sorted(missing)}"


def test_each_negative_fixture_has_documented_shape() -> None:
    """Each fixture carries name, mutation, expected_error, and input — no hashes."""
    for fname, fixture in _NEGATIVES:
        for key in ("name", "mutation", "expected_error", "input"):
            assert key in fixture, f"{fname}: missing key {key!r}"
        # A negative fixture must not carry expected valid hashes.
        assert "expected" not in fixture or not fixture["expected"], (
            f"{fname}: negative fixture must not carry expected valid hashes"
        )
        assert fixture["name"] == Path(fname).stem, (
            f"{fname}: name/filename mismatch"
        )


def test_python_negative_verifier_rejects_all() -> None:
    """The Python verifier script exits 0 (all fixtures rejected)."""
    result = subprocess.run(
        [_python_for_subprocess(), str(PY_RUNNER)],
        capture_output=True,
        text=True,
        cwd=REPOSITORY_ROOT,
        env=dict(os.environ),
    )
    assert result.returncode == 0, (
        f"Python negative verifier failed:\nstdout:{result.stdout}\nstderr:{result.stderr}"
    )
    assert "All" in result.stdout and "negative fixtures rejected" in result.stdout


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_javascript_negative_verifier_rejects_all() -> None:
    """The JavaScript verifier script exits 0 (all fixtures rejected)."""
    result = subprocess.run(
        ["node", str(JS_RUNNER)],
        capture_output=True,
        text=True,
        cwd=REPOSITORY_ROOT,
    )
    assert result.returncode == 0, (
        f"JavaScript negative verifier failed:\nstdout:{result.stdout}\nstderr:{result.stderr}"
    )
    assert "All" in result.stdout and "negative fixtures rejected" in result.stdout


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_cross_language_runner_reports_agreement() -> None:
    """The cross-language driver exits 0 and reports both halves agree."""
    result = subprocess.run(
        [_python_for_subprocess(), str(CROSS_RUNNER)],
        capture_output=True,
        text=True,
        cwd=REPOSITORY_ROOT,
        env=dict(os.environ),
    )
    assert result.returncode == 0, (
        f"cross-language runner failed:\nstdout:{result.stdout}\nstderr:{result.stderr}"
    )
    assert "Cross-language agreement" in result.stdout


# ─── JavaScript verifier independent rejection proofs ───────────────────
# Each case feeds a single mutated input to the JS verifier's reconstruction
# path (via a tiny inline driver) and asserts Node exits non-zero with a
# contract-named error. This proves the JS validators enforce the v1 contract
# boundaries the valid vectors alone do not exercise.

_JS_DRIVER_TEMPLATE = """
import {{ buildManifestFromInput }} from {libPathModule};
import {{ readFile }} from "node:fs/promises";
const [fixturePath] = process.argv.slice(2);
const fixture = JSON.parse(await readFile(fixturePath, "utf8"));
try {{
  buildManifestFromInput(fixture.name, fixture.input);
  console.error("ACCEPT " + fixture.name);
  process.exit(0);
}} catch (e) {{
  console.error("REJECT " + fixture.name + ": " + e.message);
  process.exit(1);
}}
"""


def _js_driver(tmp_path: Path, *, body: str) -> Path:
    """Write an inline Node driver that imports lib.mjs by absolute path.

    ES module imports resolve relative to the importing file's URL, not the
    process cwd, so the driver (in tmp_path) must reference the checked-in
    lib.mjs by absolute path. The body template uses ``{{ }}`` to escape the
    JS literal braces around the one ``{libPathModule}`` substitution.
    """
    lib_path = REPOSITORY_ROOT / "conformance" / "context-manifest-v1" / "lib.mjs"
    driver = tmp_path / "driver.mjs"
    driver.write_text(body.format(libPathModule=json.dumps(str(lib_path))))
    return driver


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
@pytest.mark.parametrize(
    "fixture_name,expected_token",
    [
        ("malformed-tenant-uuid", "tenant_id"),
        ("uppercase-tenant-uuid", "tenant_id"),
        ("malformed-item-uuid", "item id"),
        ("invalid-visibility", "visibility"),
        ("negative-response-item-count", "item_count"),
        ("negative-response-byte-count", "byte_count"),
        ("negative-omission-count", "omitted_count"),
        ("negative-requested-budget", "byte_budget"),
        ("negative-effective-budget", "byte_budget"),
        ("non-null-effective-startup-item-budget", "item_budget"),
        ("profile-id-only", "profile"),
        ("profile-revision-only", "profile"),
        ("profile-version-only", "profile"),
        ("profile-id-revision-no-version", "profile"),
        ("profile-id-version-no-revision", "profile"),
        ("profile-revision-version-no-id", "profile"),
        ("malformed-sha256", "query_digest"),
        ("uppercase-sha256", "query_digest"),
        ("string-boolean", "pinned"),
        ("mixed-type-reasons", "reasons"),
    ],
)
def test_javascript_verifier_rejects_fixture(
    fixture_name: str, expected_token: str, tmp_path: Path
) -> None:
    """The JavaScript verifier rejects each shared negative fixture and reports
    the rejected-invariant token (exact text need not match Python)."""
    driver = _js_driver(tmp_path, body=_JS_DRIVER_TEMPLATE)
    fixture_path = NEGATIVES_DIR / f"{fixture_name}.json"
    result = subprocess.run(
        ["node", str(driver), str(fixture_path)],
        capture_output=True,
        text=True,
        cwd=REPOSITORY_ROOT,
    )
    assert result.returncode != 0, (
        f"{fixture_name}: JavaScript ACCEPTED a negative fixture "
        f"(stdout:{result.stdout} stderr:{result.stderr})"
    )
    assert "REJECT" in result.stderr
    assert expected_token in result.stderr, (
        f"{fixture_name}: expected token {expected_token!r} in JS error; "
        f"got: {result.stderr.strip()}"
    )


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_javascript_verifier_validates_expected_manifest(tmp_path: Path) -> None:
    """The JavaScript verifier independently validates the frozen expected
    manifest of each golden vector (not just compares it to a reconstruction)."""
    driver = _js_driver(
        tmp_path,
        body=(
            "import {{ validateExpectedManifest }} from {libPathModule};\n"
            "import {{ readFile }} from 'node:fs/promises';\n"
            "const [vectorPath] = process.argv.slice(2);\n"
            "const v = JSON.parse(await readFile(vectorPath, 'utf8'));\n"
            "validateExpectedManifest(v.name, v.expected.manifest);\n"
            "console.log('OK ' + v.name);\n"
        ),
    )
    vectors_dir = REPOSITORY_ROOT / "conformance" / "context-manifest-v1" / "vectors"
    for vector_file in sorted(vectors_dir.glob("*.json")):
        result = subprocess.run(
            ["node", str(driver), str(vector_file)],
            capture_output=True,
            text=True,
            cwd=REPOSITORY_ROOT,
        )
        assert result.returncode == 0, (
            f"{vector_file.name}: JS expected-manifest validation failed: "
            f"{result.stderr}"
        )
        assert "OK" in result.stdout


# ─── authority is a signed, unconstrained integer (cross-language contract) ─
#
# `authority` mirrors the storage column `authority` (a SmallInteger with NO
# CHECK range) and the Python contract field `authority: int`. The normative
# JSON Schema types it `{"type": "integer"}` with NO minimum. So a manifest
# with `"authority": -1` is valid according to Python, the schema, and the
# documentation. The JavaScript verifier MUST agree — it must use a signed
# integer validator for authority, not a nonnegative one (which would make a
# negative authority valid in one language and invalid in another).
#
# Counts, budgets, ordinals, and the profile version stay nonnegative; only
# authority is signed.

_AUTHORITY_DRIVER_TEMPLATE = """
import {{ buildManifestFromInput, validateExpectedManifest }} from {libPathModule};
import {{ readFile, writeFile }} from 'node:fs/promises';
const [inPath, outPath, authorityLiteral] = process.argv.slice(2);
const vector = JSON.parse(await readFile(inPath, 'utf8'));
// Mutate authority on item 0 of the input, then reconstruct + validate the
// resulting expected manifest. Both must succeed for a signed integer.
vector.input.response.items[0].authority = JSON.parse(authorityLiteral);
let manifest;
try {{
  manifest = buildManifestFromInput(vector.name, vector.input);
  validateExpectedManifest(vector.name, manifest);
}} catch (e) {{
  console.error('REJECT ' + e.message);
  process.exit(1);
}}
await writeFile(outPath, JSON.stringify(manifest.items[0].authority));
console.log('ACCEPT');
"""


def _golden_vector_input() -> dict[str, Any]:
    """A coherent base input (vector 002) for authority mutation tests."""
    vector_path = (
        REPOSITORY_ROOT
        / "conformance"
        / "context-manifest-v1"
        / "vectors"
        / "002-mixed-pinned-scored.json"
    )
    return json.loads(vector_path.read_text())


def _run_authority_driver(authority_json: str, tmp_path: Path) -> subprocess.CompletedProcess:
    """Run the JS authority driver; authority is passed as a JSON literal arg."""
    vector_path = (
        REPOSITORY_ROOT
        / "conformance"
        / "context-manifest-v1"
        / "vectors"
        / "002-mixed-pinned-scored.json"
    )
    driver = _js_driver(tmp_path, body=_AUTHORITY_DRIVER_TEMPLATE)
    out_path = tmp_path / "authority.json"
    return subprocess.run(
        ["node", str(driver), str(vector_path), str(out_path), authority_json],
        capture_output=True,
        text=True,
        cwd=REPOSITORY_ROOT,
    )


@pytest.mark.parametrize("authority", [-1, -2147483648, 0, 1, 999999])
def test_authority_signed_integer_accepted_by_python_model(authority: int) -> None:
    """The Pydantic contract field accepts any signed integer for authority."""
    from engram.context_manifest import ContextManifestItemV1

    item = ContextManifestItemV1(
        ordinal=0,
        item_id="00000000-0000-0000-0000-0000000000a1",
        kind="fact",
        served_content_hash="sha256:" + "a" * 64,
        review_status="active",
        authority=authority,
        visibility="private",
        workspace_id=None,
        score=0.5,
        importance=0.5,
        source_trust=0.5,
        memory_confidence=0.5,
        reasons=[],
        warnings=[],
        pinned=False,
        human_verified=True,
        conflict_type=None,
        conflict_resolution_status=None,
    )
    assert item.authority == authority


@pytest.mark.parametrize("authority", [-1, -2147483648, 0, 1, 999999])
def test_authority_signed_integer_accepted_by_normative_schema(authority: int) -> None:
    """The checked-in normative JSON Schema has no minimum on authority."""
    import copy

    from jsonschema import Draft202012Validator

    from engram.context_manifest import normative_manifest_schema_dict

    vector = _golden_vector_input()
    manifest = copy.deepcopy(vector["expected"]["manifest"])
    manifest["items"][0]["authority"] = authority
    validator = Draft202012Validator(normative_manifest_schema_dict())
    assert not list(validator.iter_errors(manifest)), (
        f"authority={authority} should be schema-valid"
    )


@pytest.mark.parametrize("authority", [-1, -2147483648, 0, 1, 999999])
def test_authority_signed_integer_accepted_by_python_builder(authority: int) -> None:
    """The Python manifest builder accepts a negative authority without coercion."""
    import copy

    from engram.context_manifest import (
        ContextManifestEffectiveV1,
        ContextManifestRequestedV1,
        ContextManifestRequestInputV1,
        ContextManifestSubjectV1,
        ContextManifestVersionsV1,
        build_startup_context_manifest_v1,
    )

    vector = _golden_vector_input()
    inp = copy.deepcopy(vector["input"])
    inp["response"]["items"][0]["authority"] = authority

    class _Response:
        def __init__(self, **kwargs: Any) -> None:
            self.working_set = kwargs["working_set"]
            self.items = kwargs["items"]
            self.pinned_omitted_count = kwargs.get("pinned_omitted_count", 0)
            self.omitted_count = kwargs.get("omitted_count", 0)
            self.message = kwargs.get("message")
            self.item_count = kwargs["item_count"]
            self.byte_count = kwargs["byte_count"]

    manifest = build_startup_context_manifest_v1(
        response=_Response(**inp["response"]),
        subject_context=ContextManifestSubjectV1(**inp["subject_context"]),
        request_context=ContextManifestRequestInputV1(
            requested=ContextManifestRequestedV1(**inp["request_context"]["requested"]),
            effective=ContextManifestEffectiveV1(**inp["request_context"]["effective"]),
            query_digest=inp["request_context"]["query_digest"],
        ),
        decision_versions=ContextManifestVersionsV1(**inp["decision_versions"]),
    )
    # The authority is preserved verbatim, not clamped or rejected.
    assert manifest.items[0].authority == authority


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
@pytest.mark.parametrize("authority", [-1, -2147483648, 0, 1, 999999])
def test_authority_signed_integer_accepted_by_javascript(
    authority: int, tmp_path: Path
) -> None:
    """The JavaScript verifier accepts any signed integer for authority.

    This is the cross-language half: a negative authority must be valid in JS
    exactly as it is in Python and the schema. The driver reconstructs the
    manifest from the mutated input AND runs the independent expected-manifest
    validation on the result, then reports the reconstructed authority.
    """
    out_path = tmp_path / "authority.json"
    result = _run_authority_driver(json.dumps(authority), tmp_path)
    assert result.returncode == 0, (
        f"authority={authority}: JavaScript rejected a valid signed integer. "
        f"stderr: {result.stderr.strip()}"
    )
    assert "ACCEPT" in result.stdout
    # The reconstructed authority is preserved verbatim.
    assert json.loads(out_path.read_text()) == authority


@pytest.mark.parametrize(
    "bad_authority", [1.5, True, "10", None]
)
def test_authority_non_integer_rejected_by_python_model(bad_authority: Any) -> None:
    """A non-integer authority is rejected by the Pydantic contract."""
    from engram.context_manifest import ContextManifestItemV1

    with pytest.raises(Exception):  # noqa: B017
        ContextManifestItemV1(
            ordinal=0,
            item_id="00000000-0000-0000-0000-0000000000a1",
            kind="fact",
            served_content_hash="sha256:" + "a" * 64,
            review_status="active",
            authority=bad_authority,  # type: ignore[arg-type]
            visibility="private",
            workspace_id=None,
            score=0.5,
            importance=0.5,
            source_trust=0.5,
            memory_confidence=0.5,
            reasons=[],
            warnings=[],
            pinned=False,
            human_verified=True,
            conflict_type=None,
            conflict_resolution_status=None,
        )


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
@pytest.mark.parametrize("bad_authority", ["1.5", "true", '"10"', "null"])
def test_authority_non_integer_rejected_by_javascript(
    bad_authority: str, tmp_path: Path
) -> None:
    """A non-integer authority is rejected by the JavaScript verifier.

    The JS signed-integer validator still rejects floats, booleans, strings,
    and null — only the range was relaxed, not the type. `bad_authority` is a
    JSON literal the driver parses before validation.
    """
    result = _run_authority_driver(bad_authority, tmp_path)
    assert result.returncode != 0, (
        f"bad_authority={bad_authority}: JavaScript accepted a non-integer authority"
    )
    assert "REJECT" in result.stderr
    assert "authority" in result.stderr
