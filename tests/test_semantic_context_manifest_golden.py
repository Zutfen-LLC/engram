"""Cross-language golden coverage for semantic-context-manifest-v1."""

from __future__ import annotations

import shutil
import subprocess
import sys

import pytest

from scripts.verify_semantic_context_manifest_vectors import ROOT, VECTORS, main


def test_python_semantic_vectors() -> None:
    main()


def test_semantic_vector_count() -> None:
    assert len(list(VECTORS.glob("*.json"))) >= 10


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_javascript_semantic_vectors() -> None:
    verifier = ROOT / "conformance" / "semantic-context-manifest-v1" / "verify.mjs"
    result = subprocess.run(["node", str(verifier)], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Verified 10 semantic-context-manifest-v1 vectors" in result.stdout


def test_startup_vector_tree_is_unchanged_by_semantic_generator() -> None:
    startup = ROOT / "conformance" / "context-manifest-v1" / "vectors"
    before = {path.name: path.read_bytes() for path in startup.glob("*.json")}
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "generate_semantic_context_manifest_vectors.py"),
        ],
        cwd=ROOT,
        check=True,
    )
    after = {path.name: path.read_bytes() for path in startup.glob("*.json")}
    assert after == before
