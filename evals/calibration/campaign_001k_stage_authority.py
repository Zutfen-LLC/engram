"""Canonical stage authority for campaign 001k (#216 FIX3).

A sampling seed is deterministic metadata, never authorization.  The only
stage authority is an opaque verification result derived from the immutable
001k campaign artifacts and exact stage membership.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any, Literal

from evals.calibration import campaign_001k as c216
from evals.calibration.freeze import SamplingManifest, SplitManifest, TargetIdentity

Stage216 = Literal["dev", "holdout"]

DEV_SEED_216 = "216-dev-v1"
HOLDOUT_SEED_216 = "216-holdout-v1"
_STAGE_SEED: dict[Stage216, str] = {"dev": DEV_SEED_216, "holdout": HOLDOUT_SEED_216}


class _StageCapability:
    __slots__ = ("binding_digest",)

    def __init__(self, binding_digest: str) -> None:
        self.binding_digest = binding_digest


def membership_digest(membership: frozenset[str]) -> str:
    return hashlib.sha256(
        json.dumps(sorted(membership), separators=(",", ":")).encode()
    ).hexdigest()


def _canonical_json_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _load_reuse(protected_root: Path) -> c216.ReuseManifest:
    path = protected_root / "reuse-manifest.json"
    if not path.is_file():
        raise ValueError("stage_authority_reuse_manifest_missing")
    return c216.ReuseManifest.model_validate(json.loads(path.read_text()))


def _load_split(protected_root: Path) -> SplitManifest:
    path = protected_root / "split-manifest-001k.json"
    if not path.is_file():
        raise ValueError("stage_authority_split_manifest_missing")
    return SplitManifest.model_validate(json.loads(path.read_text()))


def _canonical_stage_manifest_path(protected_root: Path, stage: Stage216) -> Path:
    return protected_root / (
        "dev-sampling-manifest.json" if stage == "dev" else "holdout-sampling-manifest.json"
    )


def _canonical_source_packet_path(protected_root: Path, stage: Stage216) -> Path:
    return protected_root / f"{c216.CAMPAIGN_ID_001K}-{stage}-v1.blind.json"


def _expected_membership(reuse: c216.ReuseManifest, stage: Stage216) -> frozenset[str]:
    if stage == "dev":
        members = frozenset(reuse.forced_dev_fresh_ids) | frozenset(reuse.dev_fresh_ids)
        if len(members) != 102:
            raise ValueError("stage_authority_dev_count_mismatch")
        return members
    members = frozenset(reuse.holdout_ids)
    if len(members) != 100:
        raise ValueError("stage_authority_holdout_count_mismatch")
    return members


class StageAuthority216:
    """Verified, canonical 001k DEV/HOLDOUT authorization capability.

    This is intentionally not a Pydantic record: no parsed or caller-created
    object can become authority.  :func:`verify_stage_authority` is the only
    issuer and seals the complete derived binding.
    """

    __slots__ = (
        "campaign_id",
        "stage",
        "target_identity_digest",
        "sampling_manifest_digest",
        "membership_digest",
        "reuse_manifest_digest",
        "split_manifest_digest",
        "source_packet_digest",
        "_capability",
    )

    def __init__(
        self,
        *,
        campaign_id: str,
        stage: Stage216,
        target_identity_digest: str,
        sampling_manifest_digest: str,
        membership_digest: str,
        reuse_manifest_digest: str,
        split_manifest_digest: str,
        source_packet_digest: str | None,
        capability: _StageCapability | None = None,
    ) -> None:
        self.campaign_id = campaign_id
        self.stage = stage
        self.target_identity_digest = target_identity_digest
        self.sampling_manifest_digest = sampling_manifest_digest
        self.membership_digest = membership_digest
        self.reuse_manifest_digest = reuse_manifest_digest
        self.split_manifest_digest = split_manifest_digest
        self.source_packet_digest = source_packet_digest
        self._capability = capability

    def _binding_digest(self) -> str:
        return _canonical_json_digest(
            {
                "campaign_id": self.campaign_id,
                "stage": self.stage,
                "target_identity_digest": self.target_identity_digest,
                "sampling_manifest_digest": self.sampling_manifest_digest,
                "membership_digest": self.membership_digest,
                "reuse_manifest_digest": self.reuse_manifest_digest,
                "split_manifest_digest": self.split_manifest_digest,
                "source_packet_digest": self.source_packet_digest,
            }
        )

    def require_capability(self) -> None:
        cap = self._capability
        if not isinstance(cap, _StageCapability) or not hmac.compare_digest(
            cap.binding_digest, self._binding_digest()
        ):
            raise ValueError("stage_authority_capability_invalid")


def verify_stage_authority(
    *,
    protected_root: Path,
    sampling: SamplingManifest,
    source_packet_digest: str | None = None,
) -> StageAuthority216:
    """Derive exact stage authority from canonical 001k state, never seed.

    The seed is deliberately ignored.  It may be forged, stale, or arbitrary;
    exact target/membership/reuse/split authority decides the stage.
    """
    if sampling.campaign_id != c216.CAMPAIGN_ID_001K:
        raise ValueError("stage_authority_campaign_mismatch")
    identity: TargetIdentity = c216.load_001k_target_identity(protected_root)
    reuse = _load_reuse(protected_root)
    split = _load_split(protected_root)
    if reuse.campaign_id != c216.CAMPAIGN_ID_001K or split.campaign_id != c216.CAMPAIGN_ID_001K:
        raise ValueError("stage_authority_canonical_campaign_mismatch")
    if not hmac.compare_digest(sampling.target_identity_digest, identity.identity_digest()):
        raise ValueError("stage_authority_target_identity_mismatch")
    split_membership = sorted(set(split.dev_ids) | set(split.holdout_ids))
    split_membership_digest = hashlib.sha256(
        json.dumps(split_membership, separators=(",", ":")).encode()
    ).hexdigest()
    if not hmac.compare_digest(split.sampling_membership_digest, split_membership_digest):
        raise ValueError("stage_authority_split_membership_digest_mismatch")

    membership = frozenset(sampling.sample_ids)
    dev = _expected_membership(reuse, "dev")
    holdout = _expected_membership(reuse, "holdout")
    if membership == dev:
        stage: Stage216 = "dev"
    elif membership == holdout:
        stage = "holdout"
    else:
        if len(membership) in (102, 100):
            raise ValueError("stage_authority_exact_count_wrong_membership")
        raise ValueError("stage_authority_membership_mismatch")
    if membership & (holdout if stage == "dev" else dev):
        raise ValueError("stage_authority_cross_stage_membership")
    expected_split = set(split.dev_ids) if stage == "dev" else set(split.holdout_ids)
    # DEV's split side includes the reused-200 as well as fresh DEV-102;
    # stage membership is therefore exact fresh membership AND a subset of the
    # canonical split side. HOLDOUT is exact equality to its split side.
    if not membership <= expected_split:
        raise ValueError("stage_authority_split_reuse_mismatch")
    if stage == "holdout" and membership != expected_split:
        raise ValueError("stage_authority_split_reuse_mismatch")

    canonical_path = _canonical_stage_manifest_path(protected_root, stage)
    if not canonical_path.is_file():
        raise ValueError("stage_authority_canonical_sampling_manifest_missing")
    canonical_sampling = SamplingManifest.model_validate(json.loads(canonical_path.read_text()))
    if not hmac.compare_digest(sampling.manifest_digest(), canonical_sampling.manifest_digest()):
        raise ValueError("stage_authority_sampling_manifest_digest_mismatch")
    if source_packet_digest is not None:
        packet_path = _canonical_source_packet_path(protected_root, stage)
        if not packet_path.is_file():
            raise ValueError("stage_authority_source_packet_missing")
        actual_source_digest = hashlib.sha256(packet_path.read_bytes()).hexdigest()
        if not hmac.compare_digest(source_packet_digest, actual_source_digest):
            raise ValueError("stage_authority_source_packet_mismatch")

    authority = StageAuthority216(
        campaign_id=c216.CAMPAIGN_ID_001K,
        stage=stage,
        target_identity_digest=identity.identity_digest(),
        sampling_manifest_digest=sampling.manifest_digest(),
        membership_digest=membership_digest(membership),
        reuse_manifest_digest=reuse.manifest_digest(),
        split_manifest_digest=split.split_digest(),
        source_packet_digest=source_packet_digest,
    )
    authority._capability = _StageCapability(authority._binding_digest())
    return authority
