#!/usr/bin/env python3
"""Verify the ENG-CONTEXT-001 shared negative conformance fixtures (Python).

Each fixture in ``conformance/context-manifest-v1/negative/*.json`` carries a
valid base ``input`` with exactly one field mutated to violate the v1 contract.
The Python verifier must **reject** every fixture before a manifest is built.

This is the Python half of the cross-language rejection proof. The JavaScript
verifier (``conformance/context-manifest-v1/verify_negatives.mjs``) must reject
the same fixtures. Exact error text need not match across languages, but each
verifier reports the fixture name and the rejected invariant.

Usage::

    python scripts/verify_context_manifest_negatives.py
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
    ContextManifestVersionsV1,
    build_startup_context_manifest_v1,
)

NEGATIVES_DIR = ROOT / "conformance" / "context-manifest-v1" / "negative"


class _Response:
    """Minimal finalized-response stand-in (satisfies RecallResponseLike)."""

    def __init__(self, **kwargs: Any) -> None:
        self.working_set = kwargs["working_set"]
        self.items = kwargs["items"]
        self.pinned_omitted_count = kwargs.get("pinned_omitted_count", 0)
        self.omitted_count = kwargs.get("omitted_count", 0)
        self.message = kwargs.get("message")
        self.item_count = kwargs["item_count"]
        self.byte_count = kwargs["byte_count"]


def _attempt_build(inp: dict[str, Any]) -> None:
    """Reconstruct the manifest from a (mutated) input. Must raise on a negative."""
    subject = ContextManifestSubjectV1(**inp["subject_context"])
    request = ContextManifestRequestInputV1(
        requested=ContextManifestRequestedV1(**inp["request_context"]["requested"]),
        effective=ContextManifestEffectiveV1(**inp["request_context"]["effective"]),
        query_digest=inp["request_context"]["query_digest"],
    )
    versions = ContextManifestVersionsV1(**inp["decision_versions"])
    response = _Response(**inp["response"])
    build_startup_context_manifest_v1(
        response=response,
        subject_context=subject,
        request_context=request,
        decision_versions=versions,
    )


def main() -> int:
    fixtures = sorted(NEGATIVES_DIR.glob("*.json"))
    if not fixtures:
        print(f"no negative fixtures found in {NEGATIVES_DIR}", file=sys.stderr)
        return 1
    print(f"Verifying {len(fixtures)} negative fixtures (Python)...")
    failures = 0
    for path in fixtures:
        fixture = json.loads(path.read_text())
        expected_error = fixture.get("expected_error", "")
        try:
            _attempt_build(fixture["input"])
        except Exception as exc:  # noqa: BLE001 - any rejection is acceptable
            # Accepted: a negative fixture must be rejected. Report the
            # invariant token so cross-language disagreement is visible.
            detail = f"{type(exc).__name__}: {str(exc).splitlines()[0][:90]}"
            print(f"  OK  {path.name}: rejected ({detail})")
            continue
        # The fixture was NOT rejected — that is a failure.
        print(
            f"FAIL {path.name}: input was accepted (expected rejection on "
            f"'{expected_error}')",
            file=sys.stderr,
        )
        failures += 1
    if failures > 0:
        print(f"\n{failures} negative fixture(s) were NOT rejected.", file=sys.stderr)
        return 1
    print(f"All {len(fixtures)} negative fixtures rejected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
