#!/usr/bin/env python3
"""Cross-language driver for the ENG-CONTEXT-001 negative conformance fixtures.

Runs BOTH the Python and the JavaScript negative-fixture verifiers against the
SAME checked-in fixtures and confirms they agree: every negative must be
rejected by both, and neither may accept a fixture the other rejects. The
hosted conformance job fails if the two languages disagree.

This driver shells out to the two independent runners:
  - scripts/verify_context_manifest_negatives.py   (Python)
  - conformance/context-manifest-v1/verify_negatives.mjs   (JavaScript)

Both must exit 0. The driver itself is a thin agreement/exit-code wrapper; it
deliberately does not re-implement any contract logic so the two runners stay
the source of truth.

Usage::

    python conformance/context-manifest-v1/run_cross_language.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

NEGATIVES_DIR = (
    ROOT / "conformance" / "context-manifest-v1" / "negative"
)

PYTHON_RUNNER = ROOT / "scripts" / "verify_context_manifest_negatives.py"
JS_RUNNER = ROOT / "conformance" / "context-manifest-v1" / "verify_negatives.mjs"


def _run(cmd: list[str], *, label: str) -> int:
    print(f"\n=== {label} ===")
    proc = subprocess.run(cmd, cwd=str(ROOT))  # noqa: S603, S607
    return proc.returncode


def _fixture_names() -> list[str]:
    return sorted(p.name for p in NEGATIVES_DIR.glob("*.json"))


def main() -> int:
    fixtures = _fixture_names()
    if not fixtures:
        print(f"no negative fixtures found in {NEGATIVES_DIR}", file=sys.stderr)
        return 1

    # 1) Confirm the shared fixture set is non-empty and each fixture carries
    #    the documented shape (name, mutation, expected_error, input).
    for path in sorted(NEGATIVES_DIR.glob("*.json")):
        fixture = json.loads(path.read_text())
        for key in ("name", "mutation", "expected_error", "input"):
            if key not in fixture:
                print(
                    f"{path.name}: missing required key '{key}'",
                    file=sys.stderr,
                )
                return 1

    print(
        f"Cross-language negative-fixture agreement over {len(fixtures)} fixtures."
    )

    # 2) Run both independent verifiers. Each must reject every fixture.
    py_rc = _run(
        [sys.executable, str(PYTHON_RUNNER)], label="Python negative verifier"
    )
    js_rc = _run(["node", str(JS_RUNNER)], label="JavaScript negative verifier")

    if py_rc != 0:
        print(
            "Python negative-fixture verifier FAILED — one or more fixtures "
            "were accepted (or the verifier errored).",
            file=sys.stderr,
        )
    if js_rc != 0:
        print(
            "JavaScript negative-fixture verifier FAILED — one or more fixtures "
            "were accepted (or the verifier errored).",
            file=sys.stderr,
        )

    if py_rc == 0 and js_rc == 0:
        print(
            f"\nCross-language agreement: both verifiers rejected all "
            f"{len(fixtures)} negative fixtures."
        )
        return 0

    print(
        "\nCross-language DISAGREEMENT or failure: see verifier output above.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
