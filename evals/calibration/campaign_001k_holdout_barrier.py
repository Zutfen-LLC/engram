"""Mechanical holdout-access barrier for campaign 001k (#216 FIX-217-6).

The two-stage review doctrine (DEV before fit, HOLDOUT only after artifact
freeze) must not depend on operator discipline. This module is the narrow
001k campaign gate every holdout export/show/import path must pass.

State:

- Before the candidate artifact is frozen, holdout reviewer export is
  LOCKED: ``require_holdout_export_allowed`` fails closed unless supplied
  BOTH the exact frozen artifact digest and the DEV-fitting evidence digest
  recorded by the artifact freeze (and those must be well formed and bound
  to ``eng-calibration-001k``).
- The freeze step (Phases 5-6, after DEV fitting) calls ``unlock_holdout``
  to write the protected unlock record; from then on exports bind campaign
  ID + holdout split digest + holdout membership + artifact digest + target
  identity digest.

There is deliberately no generic "any campaign" bypass: the gate hardcodes
the 001k campaign ID and rejects everything else.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from pathlib import Path
from typing import Any

#: The ONLY campaign this barrier governs.
_BARRIER_CAMPAIGN = "eng-calibration-001k"
_UNLOCK_FILENAME = "holdout-unlock-001k.json"
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _well_formed(value: Any) -> bool:
    return isinstance(value, str) and bool(_DIGEST_PATTERN.fullmatch(value))


def _unlock_path(protected_root: Path) -> Path:
    return protected_root / _UNLOCK_FILENAME


def require_holdout_export_allowed(
    *,
    campaign_id: str,
    frozen_artifact_digest: Any,
    dev_fitting_evidence_digest: Any,
    protected_root: Path | None = None,
) -> None:
    """Fail closed unless the holdout is unlocked for THIS campaign.

    Either the explicit digests must be supplied and well formed, or (when
    ``protected_root`` is given) a valid unlock record must exist on disk.
    Anything else — wrong campaign, missing digest, malformed digest, no
    freeze — raises ``holdout_locked_*`` and the caller must refuse to
    export/show/import any holdout reviewer material.
    """
    if campaign_id != _BARRIER_CAMPAIGN:
        raise ValueError(f"holdout_locked_unknown_campaign:{campaign_id}")
    if frozen_artifact_digest is None and dev_fitting_evidence_digest is None:
        if protected_root is not None:
            record = _load_unlock_record(protected_root)
            if record is not None and _valid_unlock(record):
                return
        raise ValueError("holdout_locked_artifact_not_frozen")
    if not _well_formed(frozen_artifact_digest):
        raise ValueError("holdout_locked_artifact_digest_malformed")
    if not _well_formed(dev_fitting_evidence_digest):
        raise ValueError("holdout_locked_dev_evidence_digest_malformed")


def unlock_holdout(
    *,
    protected_root: Path,
    campaign_id: str,
    holdout_split_digest: str,
    holdout_membership_digest: str,
    frozen_artifact_digest: str,
    target_identity_digest: str,
    dev_fitting_evidence_digest: str,
) -> Path:
    """Write the protected unlock record (callable only at artifact freeze).

    The record mechanically binds campaign ID, holdout split digest, frozen
    holdout membership, artifact digest, target identity digest, and the
    DEV-fitting evidence digest. It is the ONLY way the barrier opens.
    """
    if campaign_id != _BARRIER_CAMPAIGN:
        raise ValueError(f"holdout_unlock_unknown_campaign:{campaign_id}")
    for name, value in (
        ("holdout_split_digest", holdout_split_digest),
        ("holdout_membership_digest", holdout_membership_digest),
        ("frozen_artifact_digest", frozen_artifact_digest),
        ("target_identity_digest", target_identity_digest),
        ("dev_fitting_evidence_digest", dev_fitting_evidence_digest),
    ):
        if not _well_formed(value):
            raise ValueError(f"holdout_unlock_digest_malformed:{name}")
    payload = {
        "barrier_schema": "engram-calibration-holdout-barrier-001k-v1",
        "campaign_id": campaign_id,
        "holdout_split_digest": holdout_split_digest,
        "holdout_membership_digest": holdout_membership_digest,
        "frozen_artifact_digest": frozen_artifact_digest,
        "target_identity_digest": target_identity_digest,
        "dev_fitting_evidence_digest": dev_fitting_evidence_digest,
    }
    body = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    path = _unlock_path(protected_root)
    if path.exists():
        # Idempotent only for byte-identical records; anything else fails.
        if hmac.compare_digest(
            hashlib.sha256(body.encode()).hexdigest(),
            hashlib.sha256(path.read_bytes()).hexdigest(),
        ):
            return path
        raise ValueError("holdout_unlock_conflict_not_deterministic")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body.encode())
    path.chmod(0o600)
    return path


def verify_holdout_binding(
    *,
    protected_root: Path,
    campaign_id: str,
    holdout_split_digest: str,
    frozen_artifact_digest: str,
    target_identity_digest: str,
) -> None:
    """Verify an export path binds the exact frozen holdout identities."""
    record = _load_unlock_record(protected_root)
    if record is None or not _valid_unlock(record):
        raise ValueError("holdout_locked_artifact_not_frozen")
    if campaign_id != _BARRIER_CAMPAIGN or record["campaign_id"] != campaign_id:
        raise ValueError("holdout_binding_campaign_mismatch")
    if not hmac.compare_digest(str(record["holdout_split_digest"]), holdout_split_digest):
        raise ValueError("holdout_binding_split_mismatch")
    if not hmac.compare_digest(str(record["frozen_artifact_digest"]), frozen_artifact_digest):
        raise ValueError("holdout_binding_artifact_mismatch")
    if not hmac.compare_digest(str(record["target_identity_digest"]), target_identity_digest):
        raise ValueError("holdout_binding_target_identity_mismatch")


def _load_unlock_record(protected_root: Path) -> dict[str, Any] | None:
    path = _unlock_path(protected_root)
    if not path.is_file():
        return None
    record = json.loads(path.read_text())
    if not isinstance(record, dict):
        raise ValueError("holdout_unlock_record_malformed")
    return record


def _valid_unlock(record: dict[str, Any]) -> bool:
    return (
        record.get("barrier_schema") == "engram-calibration-holdout-barrier-001k-v1"
        and record.get("campaign_id") == _BARRIER_CAMPAIGN
        and _well_formed(record.get("holdout_split_digest"))
        and _well_formed(record.get("holdout_membership_digest"))
        and _well_formed(record.get("frozen_artifact_digest"))
        and _well_formed(record.get("target_identity_digest"))
        and _well_formed(record.get("dev_fitting_evidence_digest"))
    )
