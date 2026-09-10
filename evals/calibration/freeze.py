"""Frozen campaign identity, evidence floors, sampling, and split plan (#202).

Everything in this module is deterministic for fixed inputs. Exact membership
manifests are protected artifacts; public summaries expose aggregates only.
Sampling uses recorded decision-time state and never provider output or labels.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from engram.canonicalize import canonicalize, content_hash
from evals.admission.schema import Digest, Record, Token, digest

CAMPAIGN_SCHEMA = "engram-calibration-campaign-v1"
CALIBRATION_ARTIFACT_SCHEMA = "engram.calibration-profiles-v1"
LABEL_GUIDE_VERSION = "engram-calibration-guide-157-v1"
CANONICALIZATION_VERSION = "assessment-evidence-manifest-v1"

Sha1 = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
# The exact representation emitted by engram.assessments.assessment_config_version
# (and therefore by production AssessmentContract.config_version). Campaign
# identity stores it verbatim so comparisons stay exact string equality.
ContractDigest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
SplitName = Literal["dev", "holdout"]
PRODUCTION_MIN_BIN_SAMPLES = 50
UNAVAILABLE = "unavailable"


class TargetIdentity(Record):
    campaign_schema: Literal["engram-calibration-campaign-v1"] = "engram-calibration-campaign-v1"
    campaign_id: Token
    # Provenance of the tooling revision that generated and froze this campaign.
    # This is NOT a constraint on the revision later deployed at serving time;
    # runtime compatibility is gated on the deployed assessment contract.
    campaign_tooling_repo_sha: Sha1
    assessment_schema_version: str
    assessment_code_version: str
    prompt_version: str
    provider_adapter: str
    provider_model: str
    provider_config_digest: ContractDigest
    provider_params: dict[str, Any]
    assessment_policy_version: str
    calibration_artifact_schema_version: str
    calibration_dataset_version: str
    label_guide_version: str
    canonicalization_version: str
    dimensions: tuple[str, ...]

    @model_validator(mode="after")
    def dimensions_vocabulary(self) -> Self:
        allowed = {"taxonomy", "retention", "epistemic"}
        if not self.dimensions or not set(self.dimensions) <= allowed:
            raise ValueError("target_dimensions_out_of_vocabulary")
        return self

    def identity_digest(self) -> Digest:
        return digest(self.model_dump(mode="json"))


class EvidenceFloors(Record):
    floors_schema: Literal["engram-calibration-floors-v2"] = "engram-calibration-floors-v2"
    campaign_id: Token
    total_reviewed_min: int = Field(ge=1)
    per_dimension_labeled_min: int = Field(ge=1)
    per_dimension_non_unknown_fraction_min: float = Field(gt=0.0, le=1.0)
    holdout_min: int = Field(ge=1)
    holdout_per_profile_min: int = Field(ge=1)
    holdout_calibrated_brier_max: float = Field(ge=0.0, le=1.0)
    holdout_calibrated_ece_max: float = Field(ge=0.0, le=1.0)
    high_consequence_reviewed_min: int = Field(ge=0)
    per_bin_support_min: int = Field(ge=1)
    per_stratum_min: int = Field(ge=1)
    dual_review_high_consequence: bool = True

    @model_validator(mode="after")
    def bin_floor_matches_production(self) -> Self:
        if self.per_bin_support_min != PRODUCTION_MIN_BIN_SAMPLES:
            raise ValueError("per_bin_support_min must equal the production loader floor")
        if self.holdout_min >= self.total_reviewed_min:
            raise ValueError("holdout_min must be smaller than total_reviewed_min")
        return self


class FrameRow(Record):
    """One protected dogfood row containing recorded or derived pre-provider state."""

    item_uuid: str
    sample_id: Token | None = None
    content_hash: str
    content_norm_hash: Digest
    kind: str
    source_type: str
    review_status: str
    assertion_mode: str
    origin: str
    risk: str
    age_days: int | None = Field(default=None, ge=0)
    age_bucket: str
    evidence_state: str
    source_ref: str | None = None
    root_ref: str | None = None
    session_ref: str | None = None
    content_bytes: int = Field(ge=0)
    input_size_bucket: str


def protected_frame_digest(frame: list[FrameRow]) -> Digest:
    """Bind the exact protected frame, including strata and grouping references."""
    return digest(
        sorted(
            (row.model_dump(mode="json") for row in frame),
            key=lambda row: str(row["sample_id"] or sample_id_for(str(row["item_uuid"]))),
        )
    )


class SamplingManifest(Record):
    manifest_schema: Literal["engram-calibration-sampling-v2"] = "engram-calibration-sampling-v2"
    campaign_id: Token
    target_identity_digest: Digest
    frame_digest: Digest
    snapshot_sha256: Digest
    snapshot_as_of: AwareDatetime
    sampling_seed: Token
    selection_method: Literal["stratified_hash"] = "stratified_hash"
    inclusion_rules: tuple[str, ...]
    exclusion_rules: tuple[str, ...]
    source_row_counts: dict[str, int]
    stratum_counts: dict[str, int]
    coverage_dimensions: dict[str, dict[str, Any]]
    sample_ids: tuple[Token, ...]
    sample_hashes: tuple[str, ...]

    @model_validator(mode="after")
    def membership(self) -> Self:
        if len(self.sample_ids) != len(self.sample_hashes):
            raise ValueError("sample_membership_mismatch")
        if len(set(self.sample_ids)) != len(self.sample_ids):
            raise ValueError("duplicate_sample_id")
        if sum(self.stratum_counts.values()) != len(self.sample_ids):
            raise ValueError("stratum_count_mismatch")
        return self

    def manifest_digest(self) -> Digest:
        return digest(self.model_dump(mode="json"))


class SplitManifest(Record):
    split_schema: Literal["engram-calibration-split-v2"] = "engram-calibration-split-v2"
    campaign_id: Token
    sampling_manifest_digest: Digest
    sampling_membership_digest: Digest
    split_seed: Token
    dev_fraction: float = Field(gt=0.0, lt=1.0)
    grouping: tuple[
        Literal["content_hash", "normalized_text", "source_ref", "root_ref", "session_ref"], ...
    ]
    dev_ids: tuple[Token, ...]
    holdout_ids: tuple[Token, ...]
    leakage_checks: dict[str, int]

    @model_validator(mode="after")
    def partition(self) -> Self:
        if set(self.dev_ids) & set(self.holdout_ids):
            raise ValueError("split_overlap")
        all_ids = self.dev_ids + self.holdout_ids
        if len(all_ids) != len(set(all_ids)):
            raise ValueError("split_duplicate_membership")
        return self

    def split_digest(self) -> Digest:
        return digest(self.model_dump(mode="json"))


INCLUSION_RULES = (
    "live item: valid_to IS NULL and review_status NOT IN (rejected, archived)",
    "tenant dogfood corpus only",
    "content present and passes the production secret denylist",
)
EXCLUSION_RULES = (
    "kind=diary_entry: governed not_applicable epistemic, no calibration signal",
    "content > 16000 bytes: exceeds the provider input bound",
    "secret-scanner match on content",
)


def sample_id_for(item_uuid: str) -> Token:
    return "s" + digest(item_uuid)[:24]


def normalized_text_hash(content: str) -> Digest:
    collapsed = re.sub(r"\s+", " ", content.casefold()).strip()
    letters = re.sub(r"[^a-z0-9 ]", "", collapsed)
    return digest(letters)


def age_bucket(age_days: int | None) -> str:
    if age_days is None:
        return UNAVAILABLE
    if age_days < 7:
        return "lt_7d"
    if age_days < 30:
        return "7_29d"
    if age_days < 90:
        return "30_89d"
    return "ge_90d"


def input_size_bucket(content_bytes: int) -> str:
    if content_bytes <= 256:
        return "le_256b"
    if content_bytes <= 1024:
        return "257_1024b"
    if content_bytes <= 4096:
        return "1025_4096b"
    return "gt_4096b"


def _timestamp(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _recorded(row: dict[str, Any], key: str) -> str:
    """Keep explicit ``unknown`` distinct from an absent/unrecorded field."""
    if key not in row or row[key] is None or row[key] == "":
        return UNAVAILABLE
    return str(row[key])


def _group_ref(row: dict[str, Any], key: str) -> str | None:
    value = row.get(key)
    if value is None or str(value).strip().lower() in {"", "missing", "unknown", "unavailable"}:
        return None
    return str(value)


def build_frame(
    rows: list[dict[str, Any]],
    *,
    content_by_uuid: dict[str, str],
    snapshot_as_of: Any,
) -> tuple[list[FrameRow], dict[str, int]]:
    from engram.safety import has_secrets

    excluded: dict[str, int] = defaultdict(int)
    frame: list[FrameRow] = []
    for row in rows:
        content = content_by_uuid.get(row["item_uuid"], "")
        if not content:
            excluded["no_content"] += 1
            continue
        if row.get("valid_to") is not None:
            excluded["superseded"] += 1
            continue
        if row["review_status"] in ("rejected", "archived"):
            excluded["review_status"] += 1
            continue
        if row["kind"] == "diary_entry":
            excluded["kind_diary_entry"] += 1
            continue
        size = len(content.encode())
        if size > 16000:
            excluded["content_too_large"] += 1
            continue
        if has_secrets(content):
            excluded["secret_scanner"] += 1
            continue
        created_at = _timestamp(row.get("created_at"))
        days = max(0, (snapshot_as_of - created_at).days) if created_at else None
        frame.append(
            FrameRow(
                item_uuid=row["item_uuid"],
                sample_id=sample_id_for(row["item_uuid"]),
                content_hash=content_hash(canonicalize(content)),
                content_norm_hash=normalized_text_hash(content),
                kind=row["kind"],
                source_type=row["source_type"],
                review_status=row["review_status"],
                assertion_mode=_recorded(row, "assertion_mode"),
                origin=_recorded(row, "origin"),
                risk=_recorded(row, "risk"),
                age_days=days,
                age_bucket=age_bucket(days),
                evidence_state=_recorded(row, "evidence_state"),
                source_ref=_group_ref(row, "source_ref"),
                root_ref=_group_ref(row, "root_ref"),
                session_ref=_group_ref(row, "session_ref"),
                content_bytes=size,
                input_size_bucket=input_size_bucket(size),
            )
        )
    return frame, dict(excluded)


def _rank(row: FrameRow, campaign_id: str, seed: str) -> tuple[str, str]:
    sid = sample_id_for(row.item_uuid)
    return digest([campaign_id, seed, row.content_hash, sid]), sid


def _coverage_entry(
    buckets: dict[str, list[FrameRow]], selected: set[Token], coverage_min: int
) -> dict[str, Any]:
    cells = {
        value: {
            "population": len(rows),
            "selected": sum(sample_id_for(row.item_uuid) in selected for row in rows),
            "coverage_min": min(len(rows), coverage_min),
        }
        for value, rows in sorted(buckets.items())
    }
    return {"status": "sampled", "cells": cells}


def stratified_sample(
    frame: list[FrameRow],
    *,
    campaign_id: str,
    sampling_seed: str,
    coverage_min: int = 8,
    allocation_fraction: float = 0.6,
    strata_keys: tuple[str, ...] = ("kind", "source_type", "review_status"),
) -> tuple[list[Token], dict[str, int], dict[str, dict[str, Any]]]:
    """Select proportionally, then deterministically fill available marginal coverage."""
    primary: dict[tuple[str, ...], list[FrameRow]] = defaultdict(list)
    for row in frame:
        primary[tuple(str(getattr(row, key)) for key in strata_keys)].append(row)
    selected: set[Token] = set()
    for stratum in sorted(primary):
        ordered = sorted(primary[stratum], key=lambda row: _rank(row, campaign_id, sampling_seed))
        take = min(len(ordered), max(coverage_min, round(len(ordered) * allocation_fraction)))
        selected.update(sample_id_for(row.item_uuid) for row in ordered[:take])

    sampled_axes = (
        "source_type",
        "kind",
        "review_status",
        "age_bucket",
        "input_size_bucket",
        "assertion_mode",
        "origin",
        "risk",
        "evidence_state",
    )
    axis_buckets: dict[str, dict[str, list[FrameRow]]] = {}
    for axis in sampled_axes:
        buckets: dict[str, list[FrameRow]] = defaultdict(list)
        for row in frame:
            buckets[str(getattr(row, axis))].append(row)
        axis_buckets[axis] = buckets
        if set(buckets) == {UNAVAILABLE}:
            continue
        for value in sorted(buckets):
            ordered = sorted(buckets[value], key=lambda row: _rank(row, campaign_id, sampling_seed))
            selected.update(sample_id_for(row.item_uuid) for row in ordered[:coverage_min])

    selected_ids = sorted(selected)
    selected_rows = {
        sample_id_for(row.item_uuid): row
        for row in frame
        if sample_id_for(row.item_uuid) in selected
    }
    stratum_counts: dict[str, int] = defaultdict(int)
    for sid in selected_ids:
        row = selected_rows[sid]
        stratum_counts["/".join(str(getattr(row, key)) for key in strata_keys)] += 1

    coverage: dict[str, dict[str, Any]] = {}
    for axis in sampled_axes:
        buckets = axis_buckets[axis]
        if set(buckets) == {UNAVAILABLE}:
            coverage[axis] = {
                "status": "unavailable",
                "reason": "field_absent_from_frozen_snapshot",
            }
        else:
            coverage[axis] = _coverage_entry(buckets, selected, coverage_min)
    coverage.update(
        {
            "directness": {
                "status": "unavailable",
                "reason": "principal/assertion provenance absent; source_type is not a proxy",
            },
            "consequence": {"status": "post_review_only"},
            "retention_disposition": {"status": "post_review_only"},
            "ambiguity_contested": {
                "status": "unavailable",
                "reason": "no disputed/contested decision-time field in frozen snapshot",
            },
            "difficulty": {
                "status": "unavailable",
                "reason": (
                    "no objective pre-provider difficulty field; input size is reported separately"
                ),
            },
            "provider_condition": {
                "status": "post_selection_only",
                "reason": "provider success/failure/abstention cannot influence membership",
            },
        }
    )
    return selected_ids, dict(sorted(stratum_counts.items())), coverage


def assign_splits(
    frame: list[FrameRow],
    *,
    sample_ids: Sequence[Token],
    campaign_id: str,
    split_seed: str,
    dev_fraction: float,
) -> tuple[list[Token], list[Token], dict[str, int], dict[str, list[Token]]]:
    """Partition exactly the frozen sample while containing duplicate groups."""
    if len(sample_ids) != len(set(sample_ids)):
        raise ValueError("duplicate_sample_id")
    full_by_id = {sample_id_for(row.item_uuid): row for row in frame}
    missing = sorted(set(sample_ids) - set(full_by_id))
    if missing:
        raise ValueError("sample_id_missing_from_frame")
    by_id = {sid: full_by_id[sid] for sid in sample_ids}
    if not by_id:
        raise ValueError("empty_sample")

    parent = {sid: sid for sid in by_id}

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[max(a, b)] = min(a, b)

    seen_by_field: dict[str, dict[str, Token]] = {
        "content_hash": {},
        "normalized_text": {},
        "source_ref": {},
        "root_ref": {},
        "session_ref": {},
    }
    for sid, row in sorted(by_id.items()):
        values = {
            "content_hash": row.content_hash,
            "normalized_text": row.content_norm_hash,
            "source_ref": row.source_ref,
            "root_ref": row.root_ref,
            "session_ref": row.session_ref,
        }
        for field, value in values.items():
            if value is None:
                continue
            seen = seen_by_field[field]
            if value in seen:
                union(sid, seen[value])
            else:
                seen[value] = sid

    members: dict[str, list[Token]] = defaultdict(list)
    for sid in by_id:
        members[find(sid)].append(sid)
    ranked = sorted(members, key=lambda root: digest([campaign_id, split_seed, root]))
    if len(ranked) < 2:
        raise ValueError("split_requires_two_independent_groups")
    target = round(len(by_id) * dev_fraction)
    running = 0
    best_cut = 1
    best_distance = len(by_id)
    for cut in range(1, len(ranked)):
        running += len(members[ranked[cut - 1]])
        distance = abs(running - target)
        if distance < best_distance:
            best_cut, best_distance = cut, distance
    dev_roots = set(ranked[:best_cut])
    dev_ids = sorted(sid for sid in by_id if find(sid) in dev_roots)
    holdout_ids = sorted(set(by_id) - set(dev_ids))
    multi_groups = {root: sorted(ids) for root, ids in members.items() if len(ids) > 1}
    checks = {
        "duplicate_groups": len(multi_groups),
        "cross_split_shared_hash_groups": 0,
        "sample_membership_count": len(by_id),
    }
    return dev_ids, holdout_ids, checks, multi_groups


def validate_split_membership(sampling: SamplingManifest, split: SplitManifest) -> None:
    expected = set(sampling.sample_ids)
    actual = set(split.dev_ids) | set(split.holdout_ids)
    if split.campaign_id != sampling.campaign_id:
        raise ValueError("split_campaign_mismatch")
    if split.sampling_manifest_digest != sampling.manifest_digest():
        raise ValueError("split_sampling_manifest_digest_mismatch")
    if split.sampling_membership_digest != digest(sorted(sampling.sample_ids)):
        raise ValueError("split_sampling_membership_digest_mismatch")
    if actual != expected or len(split.dev_ids) + len(split.holdout_ids) != len(expected):
        raise ValueError("split_sample_membership_mismatch")


def public_sampling_summary(sampling: SamplingManifest, split: SplitManifest) -> dict[str, Any]:
    """Return only public-safe counts, rules, coverage and digests."""
    validate_split_membership(sampling, split)
    return {
        "sample_count": len(sampling.sample_ids),
        "dev_count": len(split.dev_ids),
        "holdout_count": len(split.holdout_ids),
        "source_row_counts": sampling.source_row_counts,
        "stratum_counts": sampling.stratum_counts,
        "coverage_dimensions": sampling.coverage_dimensions,
        "inclusion_rules": sampling.inclusion_rules,
        "exclusion_rules": sampling.exclusion_rules,
        "sampling_seed": sampling.sampling_seed,
        "split_seed": split.split_seed,
        "dev_fraction": split.dev_fraction,
        "grouping": split.grouping,
        "leakage_checks": split.leakage_checks,
        "sampling_manifest_digest": sampling.manifest_digest(),
        "frame_digest": sampling.frame_digest,
        "split_manifest_digest": split.split_digest(),
        "snapshot_sha256": sampling.snapshot_sha256,
        "snapshot_as_of": sampling.snapshot_as_of.isoformat().replace("+00:00", "Z"),
    }
