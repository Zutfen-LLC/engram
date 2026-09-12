"""Mechanical holdout-access barrier for campaign 001k (#216 FIX2-217-7).

The two-stage review doctrine (DEV before fit, HOLDOUT only after artifact
freeze) must not depend on operator discipline. This module is the narrow
001k campaign gate every holdout export/show/import path must pass.

FIX2-217-7 correction: arbitrary well-formed 64-hex strings are NO LONGER
proof of anything. The barrier accepts exactly ONE canonical DEV
artifact-freeze record — produced by :func:`record_dev_artifact_freeze`
from the actual Phase-5 fitting/artifact operation — and every holdout
unlock is derived from that verified freeze authority:

- ``record_dev_artifact_freeze`` mechanically binds campaign ID, the exact
  verified 001k target identity digest, the exact split digest, the DEV
  membership/evidence digest, the exact frozen candidate-calibration
  artifact bytes digest, the fitting methodology/version, the frozen fitting
  inputs digest, and the frozen holdout membership digest. It verifies the
  target identity against the frozen #216 contract
  (``verify_target_identity_001k``) and the artifact bytes digest against
  the actual candidate artifact file before writing anything.
- ``unlock_holdout`` is callable ONLY with the verified freeze record plus
  the exact artifact bytes; it re-verifies every binding and writes the
  protected unlock record.
- ``require_holdout_export_allowed`` requires and verifies the protected
  unlock record derived from that freeze authority. There is no
  explicit-digest alternative path: supplying ``"0" * 64`` digests fails
  exactly like supplying nothing.

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

from evals.calibration.freeze import TargetIdentity

#: The ONLY campaign this barrier governs.
_BARRIER_CAMPAIGN = "eng-calibration-001k"
_FREEZE_FILENAME = "dev-artifact-freeze-001k.json"
_UNLOCK_FILENAME = "holdout-unlock-001k.json"
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")

#: Frozen fitting methodology identity for the 001k campaign (reused from
#: the unchanged ``evals.calibration.fit`` contract).
FITTING_METHODOLOGY = "deterministic-exact-stratum-reliability-bins-v1"


def _well_formed(value: Any) -> bool:
    return isinstance(value, str) and bool(_DIGEST_PATTERN.fullmatch(value))


def _freeze_path(protected_root: Path) -> Path:
    return protected_root / _FREEZE_FILENAME


def _unlock_path(protected_root: Path) -> Path:
    return protected_root / _UNLOCK_FILENAME


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    record = json.loads(path.read_text())
    if not isinstance(record, dict):
        raise ValueError("holdout_barrier_record_malformed")
    return record


def _valid_freeze(record: dict[str, Any]) -> bool:
    return (
        record.get("freeze_schema") == "engram-calibration-dev-artifact-freeze-001k-v1"
        and record.get("campaign_id") == _BARRIER_CAMPAIGN
        and _well_formed(record.get("target_identity_digest"))
        and _well_formed(record.get("split_digest"))
        and _well_formed(record.get("dev_membership_digest"))
        and _well_formed(record.get("dev_fitting_evidence_digest"))
        and _well_formed(record.get("frozen_artifact_digest"))
        and record.get("fitting_methodology") == FITTING_METHODOLOGY
        and _well_formed(record.get("frozen_fitting_inputs_digest"))
        and _well_formed(record.get("holdout_membership_digest"))
    )


def record_dev_artifact_freeze(
    *,
    protected_root: Path,
    target_identity: TargetIdentity,
    split_digest: str,
    dev_membership_digest: str,
    dev_fitting_evidence_digest: str,
    candidate_artifact_path: Path,
    frozen_fitting_inputs_digest: str,
    holdout_membership_digest: str,
) -> Path:
    """Write the canonical DEV artifact-freeze record (Phase 5 output).

    Called only by the actual fitting/artifact-freeze operation. Binds
    mechanically: campaign, the VERIFIED 001k target identity digest, split
    digest, DEV membership/evidence digests, the exact candidate artifact
    bytes digest (hashed from the real file), fitting methodology/version,
    frozen fitting inputs, and the frozen holdout membership digest.
    """
    from evals.calibration.campaign_001k_fit import verify_target_identity_001k

    verify_target_identity_001k(target_identity)
    frozen_artifact_digest = _sha256_file(candidate_artifact_path)
    payload = {
        "freeze_schema": "engram-calibration-dev-artifact-freeze-001k-v1",
        "campaign_id": _BARRIER_CAMPAIGN,
        "target_identity_digest": target_identity.identity_digest(),
        "split_digest": split_digest,
        "dev_membership_digest": dev_membership_digest,
        "dev_fitting_evidence_digest": dev_fitting_evidence_digest,
        "frozen_artifact_digest": frozen_artifact_digest,
        "fitting_methodology": FITTING_METHODOLOGY,
        "frozen_fitting_inputs_digest": frozen_fitting_inputs_digest,
        "holdout_membership_digest": holdout_membership_digest,
    }
    body = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
    path = _freeze_path(protected_root)
    if path.exists():
        if hmac.compare_digest(hashlib.sha256(body).hexdigest(), _sha256_file(path)):
            return path
        raise ValueError("dev_artifact_freeze_conflict_not_deterministic")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    path.chmod(0o600)
    return path


def load_verified_freeze(protected_root: Path) -> dict[str, Any]:
    """Load and fully validate the canonical DEV artifact-freeze record."""
    record = _load_json(_freeze_path(protected_root))
    if record is None or not _valid_freeze(record):
        raise ValueError("holdout_locked_artifact_not_frozen")
    return record


def require_holdout_export_allowed(
    *,
    campaign_id: str,
    protected_root: Path,
    frozen_artifact_digest: Any = None,
    dev_fitting_evidence_digest: Any = None,
) -> None:
    """Fail closed unless the holdout is unlocked for THIS campaign.

    FIX2-217-7: the ONLY accepted proof is a valid protected unlock record
    derived from the canonical DEV artifact-freeze authority. The legacy
    explicit-digest parameters are accepted for call-site compatibility and
    IGNORED as authority: supplying arbitrary well-formed digests (e.g.
    ``"0" * 64``) fails exactly like supplying nothing.
    """
    if campaign_id != _BARRIER_CAMPAIGN:
        raise ValueError(f"holdout_locked_unknown_campaign:{campaign_id}")
    del frozen_artifact_digest, dev_fitting_evidence_digest  # never authority
    record = _load_json(_unlock_path(protected_root))
    if record is None or not _valid_unlock(record, protected_root):
        raise ValueError("holdout_locked_artifact_not_frozen")


def unlock_holdout(
    *,
    protected_root: Path,
    campaign_id: str,
    holdout_split_digest: str,
    holdout_membership_digest: str,
    frozen_artifact_digest: str,
    target_identity_digest: str,
) -> Path:
    """Write the protected unlock record (callable only at artifact freeze).

    FIX2-217-7: the unlock is created ONLY from the verified canonical DEV
    artifact-freeze authority — every binding must match the freeze record
    field-exactly, and the freeze record must itself be valid. There is no
    parameter path around the freeze record.
    """
    if campaign_id != _BARRIER_CAMPAIGN:
        raise ValueError(f"holdout_unlock_unknown_campaign:{campaign_id}")
    freeze = load_verified_freeze(protected_root)
    bindings = (
        ("target_identity_digest", target_identity_digest, freeze["target_identity_digest"]),
        ("holdout_split_digest", holdout_split_digest, freeze["split_digest"]),
        (
            "holdout_membership_digest",
            holdout_membership_digest,
            freeze["holdout_membership_digest"],
        ),
        ("frozen_artifact_digest", frozen_artifact_digest, freeze["frozen_artifact_digest"]),
    )
    for name, supplied, expected in bindings:
        if not _well_formed(supplied):
            raise ValueError(f"holdout_unlock_digest_malformed:{name}")
        if not hmac.compare_digest(supplied, expected):
            raise ValueError(f"holdout_unlock_binding_mismatch:{name}")
    payload = {
        "barrier_schema": "engram-calibration-holdout-barrier-001k-v2",
        "campaign_id": campaign_id,
        "holdout_split_digest": holdout_split_digest,
        "holdout_membership_digest": holdout_membership_digest,
        "frozen_artifact_digest": frozen_artifact_digest,
        "target_identity_digest": target_identity_digest,
        "dev_fitting_evidence_digest": freeze["dev_fitting_evidence_digest"],
        "derived_from_freeze": _FREEZE_FILENAME,
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
    record = _load_json(_unlock_path(protected_root))
    if record is None or not _valid_unlock(record, protected_root):
        raise ValueError("holdout_locked_artifact_not_frozen")
    if campaign_id != _BARRIER_CAMPAIGN or record["campaign_id"] != campaign_id:
        raise ValueError("holdout_binding_campaign_mismatch")
    if not hmac.compare_digest(str(record["holdout_split_digest"]), holdout_split_digest):
        raise ValueError("holdout_binding_split_mismatch")
    if not hmac.compare_digest(str(record["frozen_artifact_digest"]), frozen_artifact_digest):
        raise ValueError("holdout_binding_artifact_mismatch")
    if not hmac.compare_digest(str(record["target_identity_digest"]), target_identity_digest):
        raise ValueError("holdout_binding_target_identity_mismatch")


def _valid_unlock(record: dict[str, Any], protected_root: Path) -> bool:
    """A valid unlock must derive field-exactly from a valid canonical freeze."""
    freeze = _load_json(_freeze_path(protected_root))
    if freeze is None or not _valid_freeze(freeze):
        return False
    return (
        record.get("barrier_schema") == "engram-calibration-holdout-barrier-001k-v2"
        and record.get("campaign_id") == _BARRIER_CAMPAIGN
        and record.get("derived_from_freeze") == _FREEZE_FILENAME
        and hmac.compare_digest(
            str(record["target_identity_digest"]), str(freeze["target_identity_digest"])
        )
        and hmac.compare_digest(str(record["holdout_split_digest"]), str(freeze["split_digest"]))
        and hmac.compare_digest(
            str(record["holdout_membership_digest"]), str(freeze["holdout_membership_digest"])
        )
        and hmac.compare_digest(
            str(record["frozen_artifact_digest"]), str(freeze["frozen_artifact_digest"])
        )
        and hmac.compare_digest(
            str(record["dev_fitting_evidence_digest"]),
            str(freeze["dev_fitting_evidence_digest"]),
        )
    )
