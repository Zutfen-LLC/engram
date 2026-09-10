"""Independently retained frozen campaign digests (#202 Round-2 re-freeze).

FIX-R4-4: the expected dev/holdout split digest must be sourced from the
already-frozen campaign authority — never computed from the split object
currently being verified (that comparison can never detect substitution).

The canonical public-safe campaign manifest
(``evals/calibration/campaigns/202/campaign-manifest-public.json``) is the
committed record of the Round-2 re-freeze; these constants mirror it exactly
and are themselves test-bound to the JSON so the two cannot drift. Authority
boundaries (final ledger verification, fitting, floor evaluation) compare
against THESE values.
"""

from __future__ import annotations

import json
from pathlib import Path

EXPECTED_CAMPAIGN_ID = "eng-calibration-001f"
EXPECTED_TARGET_IDENTITY_DIGEST = "57fc03918d5335c2925e2e6402fcadc4a29f5ef138fba6e6d839e9d8ec292ef9"
EXPECTED_SAMPLING_MANIFEST_DIGEST = (
    "ed2e0c80bfe0c30c39d5ad5bc5656b007617320484efae14c87fa51027d66b3d"
)
EXPECTED_SPLIT_MANIFEST_DIGEST = "a2a27ed4c0152bf2d9b6c318bbfcfd6e5e20944184a0cc9df18cb2b4e3fbb72b"
EXPECTED_FRAME_DIGEST = "e5c0c60b5d80a3a715a73cd4596e4cc07db604008dc8452cfde671e97045eb08"

_PUBLIC_MANIFEST = Path(__file__).with_name("202") / "campaign-manifest-public.json"


def public_manifest_values() -> dict[str, str]:
    """Read the committed public-safe campaign manifest (the authority copy)."""
    payload = json.loads(_PUBLIC_MANIFEST.read_text())
    return {
        "campaign_id": str(payload["campaign_id"]),
        "target_identity_digest": str(payload["identity_digest"]),
        "sampling_manifest_digest": str(payload["sampling"]["sampling_manifest_digest"]),
        "split_manifest_digest": str(payload["sampling"]["split_manifest_digest"]),
        "frame_digest": str(payload["sampling"]["frame_digest"]),
    }
