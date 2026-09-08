"""Run both semantic negative verifiers and require agreement."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    commands = [
        [sys.executable, "scripts/verify_semantic_context_manifest_negatives.py"],
        ["node", "conformance/semantic-context-manifest-v1/verify_negatives.mjs"],
    ]
    for command in commands:
        subprocess.run(command, cwd=ROOT, check=True)
    print("Semantic negative-vector cross-language agreement confirmed.")


if __name__ == "__main__":
    main()
