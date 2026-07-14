"""Run the canonical trust-proof selection with the active Python environment."""

from __future__ import annotations

import subprocess
import sys

from trust_proof_files import TRUST_PROOF_FILES


def main() -> int:
    """Execute pytest without relying on host-side shell expansion."""
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *TRUST_PROOF_FILES], check=False
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
