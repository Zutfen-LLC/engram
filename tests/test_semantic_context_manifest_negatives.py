"""Negative-vector coverage for semantic-context-manifest-v1."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from scripts.verify_semantic_context_manifest_negatives import NEGATIVES, ROOT, main


def test_semantic_negative_count_and_python_rejection() -> None:
    assert len(list(NEGATIVES.glob("*.json"))) >= 25
    main()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_semantic_negative_cross_language_agreement() -> None:
    result = subprocess.run(
        [
            str(ROOT / ".venv" / "bin" / "python"),
            "conformance/semantic-context-manifest-v1/run_cross_language.py",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "cross-language agreement confirmed" in result.stdout
