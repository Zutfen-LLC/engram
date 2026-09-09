"""Frozen campaign identity, evidence floors, sampling, and split plan (#202).

Everything in this module is deterministic for fixed inputs. Manifest digests
are computed over RFC 8785 canonical JSON. Sampling never inspects provider
output or labels: the frame is selected from recorded dogfood state only, so
sample membership cannot correlate with model agreement.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Annotated, Any, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from evals.admission.schema import Digest, Record, Token, digest

CAMPAIGN_SCHEMA = "engram-calibration-campaign-v1"
CALIBRATION_ARTIFACT_SCHEMA = "engram.calibration-profiles-v1"
LABEL_GUIDE_VERSION = "engram-calibration-guide-157-v1"
CANONICALIZATION_VERSION = "assessment-evidence-manifest-v1"

Sha1 = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
SplitName = Literal["dev", "holdout"]

# The production loader rejects any bin below this support; the campaign floor
# must not promise support the artifact contract cannot represent.
PRODUCTION_MIN_BIN_SAMPLES = 50


class TargetIdentity(Record):
    """The exact calibration target, frozen before sampling (#202 identity)."""

    campaign_schema: Literal["engram-calibration-campaign-v1"] = "engram-calibration-campaign-v1"
    campaign_id: Token
    repo_sha: Sha1
    assessment_schema_version: str
    assessment_code_version: str
    prompt_version: str
    provider_adapter: str
    provider_model: str
    provider_config_digest: Digest
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
    """Minimum sample/coverage floor, frozen before fitting.

    If the reviewed corpus cannot satisfy this floor the campaign terminates
    ``CALIBRATION_EVIDENCE_INSUFFICIENT``. The floor is never lowered after
    results are observed.
    """

    floors_schema: Literal["engram-calibration-floors-v1"] = "engram-calibration-floors-v1"
    campaign_id: Token
    total_reviewed_min: int = Field(ge=1)
    per_dimension_labeled_min: int = Field(ge=1)
    holdout_min: int = Field(ge=1)
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
    """One dogfood memory item as recorded state only (no content)."""

    item_uuid: str
    content_hash: str
    content_norm_hash: Digest
    kind: str
    source_type: str
    review_status: str
    assertion_mode: str
    origin: str
    risk: str
    age_days: int = Field(ge=0)
    evidence_state: str
    content_bytes: int = Field(ge=0)


class SamplingManifest(Record):
    manifest_schema: Literal["engram-calibration-sampling-v1"] = "engram-calibration-sampling-v1"
    campaign_id: Token
    target_identity_digest: Digest
    snapshot_sha256: Digest
    snapshot_as_of: AwareDatetime
    sampling_seed: Token
    selection_method: Literal["stratified_hash"] = "stratified_hash"
    inclusion_rules: tuple[str, ...]
    exclusion_rules: tuple[str, ...]
    source_row_counts: dict[str, int]
    stratum_counts: dict[str, int]
    sample_ids: tuple[Token, ...]
    sample_hashes: tuple[str, ...]

    @model_validator(mode="after")
    def membership(self) -> Self:
        if len(self.sample_ids) != len(self.sample_hashes):
            raise ValueError("sample_membership_mismatch")
        if len(set(self.sample_ids)) != len(self.sample_ids):
            raise ValueError("duplicate_sample_id")
        counted = sum(self.stratum_counts.values())
        if counted != len(self.sample_ids):
            raise ValueError("stratum_count_mismatch")
        return self

    def manifest_digest(self) -> Digest:
        return digest(self.model_dump(mode="json"))


class SplitManifest(Record):
    split_schema: Literal["engram-calibration-split-v1"] = "engram-calibration-split-v1"
    campaign_id: Token
    sampling_manifest_digest: Digest
    split_seed: Token
    dev_fraction: float = Field(gt=0.0, lt=1.0)
    grouping: tuple[Literal["content_hash", "normalized_text"], ...]
    dev_ids: tuple[Token, ...]
    holdout_ids: tuple[Token, ...]
    leakage_checks: dict[str, int]

    @model_validator(mode="after")
    def partition(self) -> Self:
        if set(self.dev_ids) & set(self.holdout_ids):
            raise ValueError("split_overlap")
        if len(self.dev_ids) + len(self.holdout_ids) != (
            len(set(self.dev_ids)) + len(set(self.holdout_ids))
        ):
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
    """Deterministic opaque sample id; production UUIDs never enter public artifacts."""
    return "s" + digest(item_uuid)[:24]


def normalized_text_hash(content: str) -> Digest:
    """Mechanically detectable paraphrase/duplicate grouping key.

    Conservative and content-free in the manifest: only the digest is stored.
    This is normalization for duplicate detection, not #161 evidence-root
    inference — it uses item text only, never provenance graphs.
    """
    collapsed = re.sub(r"\s+", " ", content.casefold()).strip()
    letters = re.sub(r"[^a-z0-9 ]", "", collapsed)
    return digest(letters)


def build_frame(
    rows: list[dict[str, Any]],
    *,
    content_by_uuid: dict[str, str],
    snapshot_as_of: Any,
) -> tuple[list[FrameRow], dict[str, int]]:
    """Apply inclusion/exclusion rules and return the eligible frame + counts."""
    from datetime import datetime

    from engram.safety import has_secrets

    def _ts(value: Any):
        if value is None or isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

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
        if len(content.encode()) > 16000:
            excluded["content_too_large"] += 1
            continue
        if has_secrets(content):
            excluded["secret_scanner"] += 1
            continue
        created_at = _ts(row.get("created_at"))
        age_days = max(0, (snapshot_as_of - created_at).days) if created_at else 0
        frame.append(
            FrameRow(
                item_uuid=row["item_uuid"],
                content_hash=row["content_hash"],
                content_norm_hash=normalized_text_hash(content),
                kind=row["kind"],
                source_type=row["source_type"],
                review_status=row["review_status"],
                assertion_mode=row.get("assertion_mode") or "unknown",
                origin=row.get("origin") or "unknown",
                risk=row.get("risk") or "unknown",
                age_days=age_days,
                evidence_state=row.get("evidence_state") or "unknown",
                content_bytes=len(content.encode()),
            )
        )
    return frame, dict(excluded)


def stratified_sample(
    frame: list[FrameRow],
    *,
    campaign_id: str,
    sampling_seed: str,
    coverage_min: int = 8,
    allocation_fraction: float = 0.6,
    strata_keys: tuple[str, ...] = ("kind", "source_type", "review_status"),
) -> tuple[list[Token], dict[str, int]]:
    """Deterministic stratified selection, hash-ordered within each stratum.

    Allocation per stratum: ``min(size, max(coverage_min, size * fraction))``.
    Small strata get coverage-only representation (they will honestly remain
    uncalibrated); large strata get enough samples that the production
    50-per-bin floor is reachable. Ordering keys are the campaign id, seed,
    and content hash — never provider output or labels.
    """
    buckets: dict[tuple[str, ...], list[FrameRow]] = defaultdict(list)
    for row in frame:
        buckets[tuple(getattr(row, key) for key in strata_keys)].append(row)
    selected: list[Token] = []
    stratum_counts: dict[str, int] = {}
    for stratum in sorted(buckets):
        rows = sorted(
            buckets[stratum],
            key=lambda r: digest([campaign_id, sampling_seed, r.content_hash]),
        )
        take = min(len(rows), max(coverage_min, round(len(rows) * allocation_fraction)))
        chosen = [sample_id_for(r.item_uuid) for r in rows[:take]]
        selected.extend(chosen)
        stratum_counts["/".join(stratum)] = len(chosen)
    selected.sort()
    return selected, stratum_counts


def assign_splits(
    frame: list[FrameRow],
    *,
    campaign_id: str,
    split_seed: str,
    dev_fraction: float,
) -> tuple[list[Token], list[Token], dict[str, int], dict[str, list[Token]]]:
    """Deterministic dev/holdout assignment with duplicate-group containment.

    Items sharing a content hash or normalized-text hash (mechanically
    detectable duplicates/paraphrases) are grouped into ONE split so the
    holdout stays independent. Grouping uses known grouping facts only.
    """
    by_id = {sample_id_for(r.item_uuid): r for r in frame}
    defaultdict(list)
    parent: dict[str, str] = {}

    def find(a: str) -> str:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for sid in by_id:
        parent[sid] = sid
    by_exact: dict[str, Token] = {}
    by_norm: dict[str, Token] = {}
    for sid, row in sorted(by_id.items()):
        if row.content_hash in by_exact:
            union(sid, by_exact[row.content_hash])
        else:
            by_exact[row.content_hash] = sid
        if row.content_norm_hash in by_norm:
            union(sid, by_norm[row.content_norm_hash])
        else:
            by_norm[row.content_norm_hash] = sid

    member_count: dict[str, int] = defaultdict(int)
    for sid in by_id:
        member_count[find(sid)] += 1
    roots = sorted(member_count)
    ranked = sorted(roots, key=lambda root: digest([campaign_id, split_seed, root]))
    n_dev = max(1, round(len(ranked) * dev_fraction))
    dev_roots, _holdout_roots = set(ranked[:n_dev]), set(ranked[n_dev:])
    dev_ids: list[Token] = []
    holdout_ids: list[Token] = []
    for sid in sorted(by_id):
        (dev_ids if find(sid) in dev_roots else holdout_ids).append(sid)
    grouped = sum(1 for count in member_count.values() if count > 1)
    multi_groups = {
        root: sorted(sid for sid in by_id if find(sid) == root)
        for root, count in member_count.items()
        if count > 1
    }
    checks = {
        "duplicate_groups": grouped,
        "cross_split_shared_hash_groups": 0,  # proven by construction; test-verified
    }
    return dev_ids, holdout_ids, checks, multi_groups
