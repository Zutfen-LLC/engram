"""Campaign 001k (#216): evidence-reuse boundary, split freeze, reused labels.

ENG-CALIBRATION-001K — the reviewed, versioned, held-out-validated calibration
campaign for the corrected ``engram.assess.3`` provider contract (#157/#216).

Doctrine frozen by issue #216 BEFORE any fresh label or provider result:

- the population is the SAME frozen #202 402-case sample (frame, snapshot,
  membership, ordering — byte-identical, digest-bound to the committed 001f
  campaign authority in ``evals.calibration.campaigns``);
- the first 200 executed cases (#208 checkpoint / #213 synthesis / #214
  replays) are development-contaminated: they can NEVER enter the holdout and
  enter the development side only through digest-verified preserved protected
  evidence (#213 reference synthesis), never free-form re-entry;
- fresh cases (positions 201-402 of the frozen sample order) must be
  mechanically proven unexecuted/unreviewed/unexposed before holdout use;
- the fresh members of duplicate groups spanning the executed/fresh boundary
  are conservatively forced to the development side (no #161 evidence-root
  inference; grouping uses only already-recorded #202 grouping facts);
- the holdout is >= 100 cases drawn deterministically (whole duplicate-group
  constrained, label/outcome-blind) from the leakage-safe fresh pool, frozen
  before any fresh label or provider result is observed;
- ``engram.assess.3`` emits only taxonomy/retention numerics (the #214
  semantic boundary: epistemic/evidence state is evidence-derived, risk is
  policy-derived — neither is provider-emitted), so the calibrated dimensions
  are exactly ``("taxonomy", "retention")``.

Everything here is deterministic for fixed inputs. Exact membership manifests
are protected artifacts; public summaries expose aggregates and digests only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import model_validator

from evals.admission.schema import Digest, Record, Token, digest
from evals.calibration.campaigns import (
    EXPECTED_FRAME_DIGEST,
    EXPECTED_SAMPLING_MANIFEST_DIGEST,
)
from evals.calibration.freeze import (
    FrameRow,
    SamplingManifest,
    SplitManifest,
    TargetIdentity,
    protected_frame_digest,
)

#: The #216 campaign identifier (distinct identity from 001f).
CAMPAIGN_ID_001K = "eng-calibration-001k"
#: The frozen 001f campaign whose evidence this campaign reuses.
PRIOR_CAMPAIGN_ID = "eng-calibration-001f"

#: FIX7 (#217): the ONE provenance mode that may carry ACTIVE #216 reviewer
#: executor authority for campaign 001k.  Every lane freeze / frozen-lane
#: reload / final-ledger provenance boundary for 001k dispatches through
#: ``require_active_001k_provenance_mode``, which enforces this — so a
#: future provenance-mode addition cannot silently create another active
#: 001k reviewer route (a new mode is rejected there, fail-closed, until
#: this frozen set is deliberately revised).
ACTIVE_001K_PROVENANCE_MODES: frozenset[str] = frozenset({"direct_api_provenance"})

#: FIX7 (#217): the #216 reviewer-executor modes SUPERSEDED by the sealed
#: direct HTTPS path.  They can never again create, ingest, freeze, reload,
#: or satisfy active DEV reviewer evidence for 001k.
SUPERSEDED_001K_REVIEWER_MODES: frozenset[str] = frozenset(
    {"operator_attested_subscription_ui", "machine_executor_provenance"}
)


def require_active_001k_provenance_mode(mode: str) -> None:
    """FIX7 (#217) campaign-level invariant for active 001k reviewer evidence.

    ``direct_api_provenance`` (the sealed direct HTTPS ``216-api-dev-review``
    path) is the ONLY reviewer-executor authority for
    ``eng-calibration-001k``.  The superseded modes fail closed with stable
    explicit errors:

    - ``operator_attested_subscription_ui`` ->
      ``campaign_001k_subscription_ui_superseded`` (quarantine/historical
      parse only; can never satisfy current active lane, consensus,
      ledger, fitting, candidate-freeze, or HOLDOUT authority);
    - ``machine_executor_provenance`` ->
      ``campaign_001k_machine_reviewer_superseded`` (same restriction).

    Any OTHER mode outside ``ACTIVE_001K_PROVENANCE_MODES`` — including
    ``provider_metadata`` and every provenance mode added in the future —
    fails closed with ``campaign_001k_provenance_mode_not_active``, so no
    generic or future route can silently become active 001k reviewer authority.
    """
    if mode == "operator_attested_subscription_ui":
        raise ValueError("campaign_001k_subscription_ui_superseded")
    if mode == "machine_executor_provenance":
        raise ValueError("campaign_001k_machine_reviewer_superseded")
    if mode not in ACTIVE_001K_PROVENANCE_MODES:
        raise ValueError("campaign_001k_provenance_mode_not_active")


#: Cases executed under #208's intentional 200-case checkpoint = positions
#: 0..199 of the frozen 001f sample order (mechanically proven: synthesis
#: case IDs == first 200 of ``sampling.sample_ids``).
EXECUTED_PREFIX = 200
#: Frozen holdout floor (issue #216 Phase 2; equal to the 001f floor).
HOLDOUT_MIN_216 = 100
#: Deterministic holdout selection seed (frozen with this module).
HOLDOUT_SEED_216 = "216-holdout-v1"
#: 001k split seed.
SPLIT_SEED_216 = "216-split-v1"

#: Calibrated dimensions under the #214 semantic boundary.
DIMENSIONS_001K: tuple[str, ...] = ("taxonomy", "retention")

#: Preserved protected evidence digests (independently retained; the reuse
#: boundary fails closed unless the bytes still hash exactly to these).
SYNTHESIS_RELPATH = "213-reference-synthesis/engram-200-case-reference-synthesis.json"
SYNTHESIS_SHA256 = "dba2828c28309539f04dc387fc2d0645354579bcccf5f6191e62ce6ab45e88c8"
REPLAY3_RELPATH = "214-replay-3/replay-results.json"
REPLAY3_SHA256 = "e5ad81da329b9433b22df70c43994f1a5aa52aab98478511d9575ac91c480257"
PRIOR_SAMPLING_FILE_SHA256 = "f7ecfb222210e4e0bd023c4a3ff468a00040690024d82279cef9b7fe5f0b3c32"
PRIOR_SPLIT_FILE_SHA256 = "fd336b087364db691c02c2656f2d4136bc5af2834802a1bf4c1c87315da22e72"
PRIOR_FRAME_FILE_SHA256 = "fbbbdaf5733f758f68f5cd370fa69b0330e4cfa7f1faf87e149bd1c6fe9a1fa0"
PRIOR_DUPLICATES_FILE_SHA256 = "2e7d8de55d286081ba46c984c3151519d3e8c46afd2acd32b68d1b9aeab748be"

REUSE_MANIFEST_SCHEMA: Literal["engram-calibration-reuse-001k-v1"] = (
    "engram-calibration-reuse-001k-v1"
)
CAMPAIGN_001K_SPLIT_SCHEMA = "engram-calibration-split-001k-v1"

#: Batches actually executed under #208 (pasted AND returned).
EXECUTED_BATCH_IDS: tuple[str, ...] = (
    "eng-calibration-001f:sub-review-001",
    "eng-calibration-001f:sub-review-002",
    "eng-calibration-001f:sub-review-003",
    "eng-calibration-001f:sub-review-004",
)
#: Batches exported but never executed (no reviewer return exists).
UNEXECUTED_BATCH_IDS: tuple[str, ...] = (
    "eng-calibration-001f:sub-review-005",
    "eng-calibration-001f:sub-review-006",
    "eng-calibration-001f:sub-review-007",
    "eng-calibration-001f:sub-review-008",
    "eng-calibration-001f:sub-review-009",
)

REUSE_RULES: tuple[str, ...] = (
    "first-200 executed cases are development-only and can never enter the "
    "001k holdout (development contamination via #213/#214)",
    "reused labels are derived exclusively from the digest-verified #213 "
    "reference synthesis preserved bytes; no free-form re-entry",
    "reused label provenance distinguishes reviewer_majority vs "
    "source_adjudication per field; no label is upgraded to human truth",
    "fresh cases require mechanical freshness proof: zero overlap with every "
    "executed/reviewed/replayed evidence corpus",
    "fresh members of duplicate groups spanning the executed/fresh boundary "
    "are forced to the development side before holdout selection",
    "holdout selection is deterministic, duplicate-group-constrained, and "
    "label/outcome-blind (campaign id + frozen seed + sample id only)",
    "no sample moves between dev/holdout after fresh labels or provider results are observed",
)

# ---------------------------------------------------------------------------
# Protected-evidence loading (digest-verified, fail closed)
# ---------------------------------------------------------------------------


def _read_verified(root: Path, relpath: str, expected_sha256: str) -> bytes:
    payload = (root / relpath).read_bytes()
    actual = hashlib.sha256(payload).hexdigest()
    if not hmac.compare_digest(actual, expected_sha256):
        raise ValueError(f"protected_evidence_digest_mismatch:{relpath}")
    return payload


def load_prior_evidence(prior_root: Path) -> dict[str, Any]:
    """Load and digest-verify the preserved 001f protected campaign evidence."""
    sampling = SamplingManifest.model_validate(
        json.loads(_read_verified(prior_root, "sampling-manifest.json", PRIOR_SAMPLING_FILE_SHA256))
    )
    if not hmac.compare_digest(sampling.manifest_digest(), EXPECTED_SAMPLING_MANIFEST_DIGEST):
        raise ValueError("prior_sampling_manifest_digest_mismatch")
    frame_rows = [
        FrameRow.model_validate(row)
        for row in json.loads(_read_verified(prior_root, "frame.json", PRIOR_FRAME_FILE_SHA256))
    ]
    if protected_frame_digest(frame_rows) != EXPECTED_FRAME_DIGEST:
        raise ValueError("prior_frame_digest_mismatch")
    duplicates = json.loads(
        _read_verified(prior_root, "duplicate-groups.json", PRIOR_DUPLICATES_FILE_SHA256)
    )
    synthesis = json.loads(_read_verified(prior_root, SYNTHESIS_RELPATH, SYNTHESIS_SHA256))
    replay3 = json.loads(_read_verified(prior_root, REPLAY3_RELPATH, REPLAY3_SHA256))
    return {
        "sampling": sampling,
        "frame": frame_rows,
        "duplicates": duplicates,
        "synthesis": synthesis,
        "replay3": replay3,
    }


# ---------------------------------------------------------------------------
# Membership classes
# ---------------------------------------------------------------------------


def executed_membership(sampling: SamplingManifest) -> tuple[Token, ...]:
    """Executed first-200 = positions 0..199 of the frozen sample order."""
    return tuple(sampling.sample_ids[:EXECUTED_PREFIX])


def fresh_membership(sampling: SamplingManifest) -> tuple[Token, ...]:
    """Fresh 202 = positions 200..401 of the frozen sample order."""
    return tuple(sampling.sample_ids[EXECUTED_PREFIX:])


def _verify_executed_prefix(synthesis: dict[str, Any], sampling: SamplingManifest) -> None:
    """Prove the #213 synthesis population IS the frozen first-200."""
    meta = synthesis.get("metadata", {})
    if int(meta.get("executed_cases", -1)) != EXECUTED_PREFIX:
        raise ValueError("synthesis_executed_count_mismatch")
    syn_ids = {case["sample_id"] for case in synthesis["cases"]}
    prefix = set(executed_membership(sampling))
    if syn_ids != prefix:
        raise ValueError("synthesis_membership_not_executed_prefix")


def spanning_duplicate_members(
    duplicates: dict[str, list[str]], executed: tuple[Token, ...]
) -> tuple[Token, ...]:
    """Fresh members of any duplicate group containing an executed case."""
    executed_set = set(executed)
    forced: set[str] = set()
    for members in duplicates.values():
        if any(member in executed_set for member in members):
            forced |= {member for member in members if member not in executed_set}
    return tuple(sorted(forced))


def fresh_internal_groups(
    duplicates: dict[str, list[str]], fresh: tuple[Token, ...]
) -> list[list[str]]:
    """Duplicate groups entirely inside the fresh population."""
    fresh_set = set(fresh)
    return [
        sorted(members)
        for members in duplicates.values()
        if members and all(member in fresh_set for member in members)
    ]


def _group_rank(group: list[str], campaign_id: str, seed: str) -> str:
    return digest([campaign_id, seed, *group])


def deterministic_holdout(
    fresh_pool: tuple[Token, ...],
    internal_groups: list[list[str]],
    *,
    campaign_id: str = CAMPAIGN_ID_001K,
    seed: str = HOLDOUT_SEED_216,
    holdout_min: int = HOLDOUT_MIN_216,
) -> tuple[Token, ...]:
    """Deterministically select >= ``holdout_min`` holdout IDs from the pool.

    Whole duplicate-group constrained: a fresh-internal group never straddles
    the holdout/dev boundary. Label-blind and outcome-blind: ranking uses only
    the frozen campaign ID, the frozen seed, and sample IDs.
    """
    pool_set = set(fresh_pool)
    grouped: set[str] = set()
    groups_in_pool = []
    for group in internal_groups:
        members = [m for m in group if m in pool_set]
        if members:
            groups_in_pool.append(members)
            grouped |= set(members)
    singles = sorted(pool_set - grouped)
    units = [(m,) for m in singles] + [tuple(members) for members in groups_in_pool]
    units.sort(key=lambda unit: (_group_rank(list(unit), campaign_id, seed), unit))
    selected: set[str] = set()
    for unit in units:
        if len(selected) >= holdout_min:
            break
        selected |= set(unit)
    if len(selected) < holdout_min:
        raise ValueError("fresh_pool_too_small_for_holdout")
    return tuple(sorted(selected))


# ---------------------------------------------------------------------------
# Prior-evidence corpora scan (freshness proof input)
# ---------------------------------------------------------------------------

#: Result-file corpora whose case IDs constitute OBSERVED evidence: any fresh
#: candidate appearing in one is contamination and fails the freeze.
_OBSERVED_CORPORA: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("213-reference-synthesis", SYNTHESIS_RELPATH, ("cases",)),
    ("213-reassessment", "213-reassessment/reassess-results.json", ("cases",)),
    ("213-reassessment-retry", "213-reassessment/retry-results.json", ("cases",)),
    ("213-reassessment-retry3", "213-reassessment/retry3-results.json", ("cases",)),
    ("214-replay", "214-replay/replay-results.json", ("cases",)),
    ("214-replay-3", REPLAY3_RELPATH, ("cases",)),
)


def scan_prior_corpora(prior_root: Path) -> dict[str, set[Token]]:
    """Scan preserved protected evidence and classify per-corpus case IDs.

    Contamination corpora (observed evidence):

    - accepted #206/#208 lane case records (``lanes/<slot>/s*.json``);
    - the #208 human-queue judgments (initial/reveal/final per case);
    - #213 synthesis/reassessment and #214 replay case sets;
    - cases inside EXECUTED #208 batches (pasted AND returned).

    Export-only corpora (request bytes no reviewer ever answered):

    - #208 batches exported but never executed (005-009, no returns exist);
    - #206 lane request batches (emitted, never consumed — the campaign
      switched to #209 subscription mode).
    """
    corpora: dict[str, set[Token]] = {}
    for name, relpath, _ in _OBSERVED_CORPORA:
        path = prior_root / relpath
        if not path.is_file():
            raise ValueError(f"prior_evidence_missing:{relpath}")
        payload = json.loads(path.read_bytes())
        corpora[name] = {case["sample_id"] for case in payload["cases"]}
    # accepted lane records (both lane roots: #206 direct + #208 subscription)
    lane_ids: set[Token] = set()
    subscription_lanes = prior_root.parent / "protected-subscription-208" / "lanes"
    for lane_root in (prior_root / "lanes", subscription_lanes):
        if not lane_root.is_dir():
            continue
        for slot in ("model_a", "model_b", "model_c"):
            lane_dir = lane_root / slot
            if not lane_dir.is_dir():
                continue
            for path in lane_dir.glob("s*.json"):
                if path.name == "subscription-authority.json":
                    continue
                record = json.loads(path.read_text())
                lane_ids.add(record["sample_id"])
            # authoritative executed set: per-case raw response bytes
            raw_dir = lane_dir / "raw"
            if raw_dir.is_dir():
                for path in raw_dir.glob("*.resp"):
                    lane_ids.add(json.loads(path.read_text())["sample_id"])
    corpora["accepted-lane-records"] = lane_ids
    # human-queue judgments (protected-subscription-208 sibling root)
    queue_dir = prior_root.parent / "protected-subscription-208" / "human-queue" / "judgments"
    if not queue_dir.is_dir():
        raise ValueError("prior_evidence_missing:human-queue/judgments")
    queue_ids: set[Token] = set()
    for path in queue_dir.glob("s*.json"):
        queue_ids.add(_sample_id_from_filename(path.name))
    corpora["human-queue-judgments"] = queue_ids
    # executed vs unexecuted #208 batches
    batches_dir = prior_root.parent / "protected-subscription-208" / "batches"
    if not batches_dir.is_dir():
        raise ValueError("prior_evidence_missing:batches")
    for path in sorted(batches_dir.glob("eng-calibration-001f:sub-review-*.json")):
        batch_id = path.name[: -len(".json")]
        payload = json.loads(path.read_bytes())
        ids = {case["sample_id"] for case in payload["cases"]}
        if batch_id in EXECUTED_BATCH_IDS:
            corpora[f"batch-executed:{batch_id}"] = ids
        elif batch_id in UNEXECUTED_BATCH_IDS:
            corpora[f"unexecuted-export:{batch_id}"] = ids
        else:
            raise ValueError(f"unknown_prior_batch:{batch_id}")
    # lane request batches (export-only)
    request_ids: set[Token] = set()
    for slot in ("model_a", "model_b", "model_c"):
        for path in (prior_root / "lanes" / slot).glob("lane-requests-*.jsonl"):
            for line in path.read_text().splitlines():
                if line.strip():
                    request_ids.add(json.loads(line)["sample_id"])
    corpora["unexecuted-export:lane-requests"] = request_ids
    return corpora


def _sample_id_from_filename(name: str) -> str:
    stem = name.split(".")[0]
    if not re.fullmatch(r"s[0-9a-f]{24}", stem):
        raise ValueError(f"unexpected_queue_artifact:{name}")
    return stem


# ---------------------------------------------------------------------------
# Phase 1 — reuse manifest
# ---------------------------------------------------------------------------


class FreshnessProof(Record):
    """Mechanical freshness proof for the fresh 202 candidates."""

    proof_schema: Literal["engram-calibration-freshness-001k-v1"] = (
        "engram-calibration-freshness-001k-v1"
    )
    candidate_count: int
    #: evidence corpora scanned (paths relative to the protected root)
    corpora_scanned: tuple[str, ...]
    #: fresh IDs appearing in any executed/reviewed/replayed corpus. Must be
    #: empty: any hit fails the freeze.
    executed_overlap: tuple[Token, ...] = ()
    #: fresh IDs inside exported-but-never-executed batch payloads. NOT
    #: contamination (no reviewer observed them; no returns exist) but
    #: recorded for audit.
    unexecuted_export_overlap: tuple[Token, ...] = ()
    duplicate_groups_total: int
    spanning_group_forced_dev: tuple[Token, ...]
    fresh_internal_group_count: int
    leakage_safe_pool: int


class ExecutedEvidenceFacts(Record):
    """Mechanically derived facts about the executed first-200."""

    executed_case_count: int
    executed_ids_digest: Digest
    executed_batch_ids: tuple[str, ...]
    unexecuted_batch_ids: tuple[str, ...]
    synthesis_sha256: str
    synthesis_case_count: int
    replay3_sha256: str
    replay3_prompt_version: str
    replay3_model: str


class ReuseManifest(Record):
    """The frozen Phase-1 evidence-reuse boundary (#216).

    Frozen BEFORE any fresh label or 001k provider result exists.
    """

    manifest_schema: Literal["engram-calibration-reuse-001k-v1"]
    campaign_id: Token
    prior_campaign_id: Token
    prior_sampling_manifest_digest: Digest
    prior_frame_digest: Digest
    executed: ExecutedEvidenceFacts
    freshness: FreshnessProof
    reuse_rules: tuple[str, ...]
    holdout_ids: tuple[Token, ...]
    forced_dev_fresh_ids: tuple[Token, ...]
    dev_fresh_ids: tuple[Token, ...]

    @model_validator(mode="after")
    def partition_invariants(self) -> Self:
        if self.campaign_id != CAMPAIGN_ID_001K:
            raise ValueError("reuse_manifest_campaign_mismatch")
        if self.prior_campaign_id != PRIOR_CAMPAIGN_ID:
            raise ValueError("reuse_manifest_prior_campaign_mismatch")
        if self.prior_sampling_manifest_digest != EXPECTED_SAMPLING_MANIFEST_DIGEST:
            raise ValueError("reuse_manifest_prior_sampling_authority_mismatch")
        if self.prior_frame_digest != EXPECTED_FRAME_DIGEST:
            raise ValueError("reuse_manifest_prior_frame_authority_mismatch")
        if len(self.holdout_ids) < HOLDOUT_MIN_216:
            raise ValueError("reuse_manifest_holdout_below_floor")
        if set(self.holdout_ids) & set(self.forced_dev_fresh_ids):
            raise ValueError("reuse_manifest_holdout_leakage_forced_dev")
        if set(self.holdout_ids) & set(self.dev_fresh_ids):
            raise ValueError("reuse_manifest_holdout_dev_overlap")
        if set(self.forced_dev_fresh_ids) & set(self.dev_fresh_ids):
            raise ValueError("reuse_manifest_dev_fresh_overlap")
        if self.executed.executed_case_count != EXECUTED_PREFIX:
            raise ValueError("reuse_manifest_executed_count_mismatch")
        return self

    def manifest_digest(self) -> Digest:
        return digest(self.model_dump(mode="json"))

    def dev_ids(self, executed_ids: tuple[Token, ...]) -> tuple[Token, ...]:
        """Full development membership: executed-200 + forced + dev-fresh."""
        merged = tuple(executed_ids) + tuple(self.forced_dev_fresh_ids) + tuple(self.dev_fresh_ids)
        if len(set(merged)) != len(merged):
            raise ValueError("reuse_manifest_dev_membership_overlap")
        return merged


def build_freshness_proof(
    *,
    fresh: tuple[Token, ...],
    corpora: dict[str, set[Token]],
    spanning: tuple[Token, ...],
    duplicate_groups_total: int,
    fresh_internal_group_count: int,
) -> FreshnessProof:
    """Fail closed if any fresh candidate appears in executed evidence."""
    fresh_set = set(fresh)
    executed_overlap: set[Token] = set()
    export_overlap: set[Token] = set()
    for name, ids in corpora.items():
        overlap = fresh_set & ids
        if not overlap:
            continue
        if name.startswith("unexecuted-export:"):
            export_overlap |= overlap
        else:
            executed_overlap |= overlap
    if executed_overlap:
        raise ValueError(
            f"fresh_candidates_appear_in_executed_evidence:{sorted(executed_overlap)[:5]}"
        )
    pool = tuple(sorted(fresh_set - set(spanning)))
    return FreshnessProof(
        candidate_count=len(fresh),
        corpora_scanned=tuple(sorted(corpora)),
        executed_overlap=(),
        unexecuted_export_overlap=tuple(sorted(export_overlap)),
        duplicate_groups_total=duplicate_groups_total,
        spanning_group_forced_dev=tuple(sorted(spanning)),
        fresh_internal_group_count=fresh_internal_group_count,
        leakage_safe_pool=len(pool),
    )


def build_reuse_manifest(prior_root: Path, corpora: dict[str, set[Token]]) -> ReuseManifest:
    """Derive the complete Phase-1 reuse boundary from protected evidence.

    ``corpora`` maps corpus name -> set of sample IDs with preserved evidence
    in it. Names starting with ``unexecuted-export:`` are request-bytes-only
    exports no reviewer observed; every other corpus is contamination.
    """
    evidence = load_prior_evidence(prior_root)
    sampling: SamplingManifest = evidence["sampling"]
    _verify_executed_prefix(evidence["synthesis"], sampling)
    executed = executed_membership(sampling)
    fresh = fresh_membership(sampling)
    spanning = spanning_duplicate_members(evidence["duplicates"], executed)
    internal = fresh_internal_groups(evidence["duplicates"], fresh)
    pool = tuple(sorted(set(fresh) - set(spanning)))
    freshness = build_freshness_proof(
        fresh=fresh,
        corpora=corpora,
        spanning=spanning,
        duplicate_groups_total=len(evidence["duplicates"]),
        fresh_internal_group_count=len(internal),
    )
    holdout = deterministic_holdout(pool, internal)
    dev_fresh = tuple(sorted(set(pool) - set(holdout)))
    replay3: dict[str, Any] = evidence["replay3"]
    return ReuseManifest(
        manifest_schema=REUSE_MANIFEST_SCHEMA,
        campaign_id=CAMPAIGN_ID_001K,
        prior_campaign_id=PRIOR_CAMPAIGN_ID,
        prior_sampling_manifest_digest=EXPECTED_SAMPLING_MANIFEST_DIGEST,
        prior_frame_digest=EXPECTED_FRAME_DIGEST,
        executed=ExecutedEvidenceFacts(
            executed_case_count=len(executed),
            executed_ids_digest=digest(sorted(executed)),
            executed_batch_ids=EXECUTED_BATCH_IDS,
            unexecuted_batch_ids=UNEXECUTED_BATCH_IDS,
            synthesis_sha256=SYNTHESIS_SHA256,
            synthesis_case_count=len(evidence["synthesis"]["cases"]),
            replay3_sha256=REPLAY3_SHA256,
            replay3_prompt_version=str(replay3.get("prompt_version", "")),
            replay3_model=str(replay3.get("model", "")),
        ),
        freshness=freshness,
        reuse_rules=REUSE_RULES,
        holdout_ids=holdout,
        forced_dev_fresh_ids=tuple(sorted(spanning)),
        dev_fresh_ids=dev_fresh,
    )


# ---------------------------------------------------------------------------
# Phase 2 — the 001k split over the full 402 population
# ---------------------------------------------------------------------------


def build_split_001k(
    reuse: ReuseManifest, sampling: SamplingManifest, duplicates: dict[str, list[str]]
) -> SplitManifest:
    """Freeze the 001k dev/holdout split over all 402 sampled cases.

    dev = executed-200 + forced-dev fresh + remaining fresh minus holdout;
    holdout = the reuse manifest's frozen holdout. Duplicate groups are
    union-found across the full population and cross-split groups must be
    exactly zero.
    """
    executed = executed_membership(sampling)
    dev = reuse.dev_ids(executed)
    holdout = reuse.holdout_ids
    if set(dev) | set(holdout) != set(sampling.sample_ids):
        raise ValueError("split_001k_membership_mismatch")
    dev_set, holdout_set = set(dev), set(holdout)
    cross = 0
    for members in duplicates.values():
        sides = {
            ("dev" if m in dev_set else "holdout" if m in holdout_set else "none") for m in members
        }
        if "dev" in sides and "holdout" in sides:
            cross += 1
    if cross != 0:
        raise ValueError(f"split_001k_cross_split_duplicate_groups:{cross}")
    return SplitManifest(
        campaign_id=CAMPAIGN_ID_001K,
        sampling_manifest_digest=sampling.manifest_digest(),
        sampling_membership_digest=digest(sorted(sampling.sample_ids)),
        split_seed=SPLIT_SEED_216,
        dev_fraction=len(dev) / (len(dev) + len(holdout)),
        grouping=("content_hash", "normalized_text", "source_ref", "root_ref", "session_ref"),
        dev_ids=tuple(sorted(dev)),
        holdout_ids=tuple(sorted(holdout)),
        leakage_checks={
            "cross_split_shared_hash_groups": cross,
            "duplicate_groups": len(duplicates),
            "sample_membership_count": len(sampling.sample_ids),
        },
    )


# ---------------------------------------------------------------------------
# Fresh-only sampling projection (drives the #209 reviewer lanes)
# ---------------------------------------------------------------------------


def load_001k_target_identity(protected_root: Path) -> TargetIdentity:
    """Load and verify the EXACT frozen 001k target identity (FIX2-217-4).

    Digest self-consistency proves nothing: the loaded identity must both
    hash to the recorded digest AND satisfy the frozen #216 contract
    field-exactly (``verify_target_identity_001k``). Returns the verified
    ``TargetIdentity`` for downstream use, not just a digest.
    """
    from evals.calibration.campaign_001k_fit import verify_target_identity_001k

    payload = json.loads((protected_root / "identity-frozen.json").read_text())
    digest_value = str(payload["target_identity_digest"])
    if not re.fullmatch(r"[0-9a-f]{64}", digest_value):
        raise ValueError("identity_artifact_malformed_digest")
    identity = TargetIdentity.model_validate(payload["target_identity"])
    if not hmac.compare_digest(identity.identity_digest(), digest_value):
        raise ValueError("identity_artifact_digest_mismatch")
    return verify_target_identity_001k(identity)


def load_001k_target_identity_digest(protected_root: Path) -> str:
    """The EXACT frozen 001k target identity digest (verified contract)."""
    return load_001k_target_identity(protected_root).identity_digest()


def _stage_sampling_manifest(
    sampling: SamplingManifest,
    *,
    stage: Literal["dev", "holdout"],
    member_ids: tuple[str, ...],
    expected_population: set[str],
    stratum_counts: dict[str, int],
    target_identity_digest: str,
) -> SamplingManifest:
    """One stage-scoped reviewer sampling authority (FIX-217-2/FIX-217-3).

    Membership is EXACTLY the frozen stage membership; the target identity is
    the NEW 001k digest (never the historical 001f one); prior frame/snapshot
    provenance stays separately recorded via the unchanged frame/snapshot
    digests of the reused population.
    """
    members = tuple(sorted(set(member_ids)))
    if set(members) != expected_population:
        raise ValueError(f"stage_{stage}_membership_mismatch")
    if not members:
        raise ValueError(f"stage_{stage}_empty")
    if sum(stratum_counts.values()) != len(members):
        raise ValueError(f"stage_{stage}_stratum_count_mismatch")
    if target_identity_digest == sampling.target_identity_digest:
        # The 001f population manifest carries the OLD target digest; a stage
        # authority must never inherit it (FIX-217-3).
        raise ValueError("stage_authority_must_bind_new_target_identity")
    if not re.fullmatch(r"[0-9a-f]{64}", target_identity_digest):
        raise ValueError("stage_authority_target_identity_malformed")
    by_id = dict(zip(sampling.sample_ids, sampling.sample_hashes, strict=True))
    return SamplingManifest(
        campaign_id=CAMPAIGN_ID_001K,
        target_identity_digest=target_identity_digest,
        frame_digest=sampling.frame_digest,
        snapshot_sha256=sampling.snapshot_sha256,
        snapshot_as_of=sampling.snapshot_as_of,
        sampling_seed=f"216-{stage}-v1",
        inclusion_rules=sampling.inclusion_rules
        + (
            f"216 {stage} reviewer authority: positions 201-402 of the frozen "
            "001f sample order restricted to the frozen 001k "
            f"{stage} membership",
        ),
        exclusion_rules=sampling.exclusion_rules,
        source_row_counts={
            **sampling.source_row_counts,
            f"stage_{stage}": len(members),
        },
        stratum_counts=stratum_counts,
        coverage_dimensions=sampling.coverage_dimensions,
        sample_ids=members,
        sample_hashes=tuple(by_id[sid] for sid in members),
    )


def fresh_dev_ids(reuse: ReuseManifest) -> tuple[str, ...]:
    """The 102 fresh development reviewer population (FIX-217-2 Stage A)."""
    return reuse.dev_ids(())


def build_dev_sampling_manifest(
    sampling: SamplingManifest,
    reuse: ReuseManifest,
    *,
    stratum_counts: dict[str, int],
    target_identity_digest: str,
) -> SamplingManifest:
    """Stage-A authority: exactly forced_dev_fresh + dev_fresh (102 cases),
    zero holdout IDs."""
    members = fresh_dev_ids(reuse)
    expected = set(reuse.forced_dev_fresh_ids) | set(reuse.dev_fresh_ids)
    manifest = _stage_sampling_manifest(
        sampling,
        stage="dev",
        member_ids=members,
        expected_population=expected,
        stratum_counts=stratum_counts,
        target_identity_digest=target_identity_digest,
    )
    if set(manifest.sample_ids) & set(reuse.holdout_ids):
        raise ValueError("dev_authority_holdout_leakage")
    return manifest


def build_holdout_sampling_manifest(
    sampling: SamplingManifest,
    reuse: ReuseManifest,
    *,
    stratum_counts: dict[str, int],
    target_identity_digest: str,
) -> SamplingManifest:
    """Stage-C authority: exactly the frozen 100 holdout cases."""
    return _stage_sampling_manifest(
        sampling,
        stage="holdout",
        member_ids=reuse.holdout_ids,
        expected_population=set(reuse.holdout_ids),
        stratum_counts=stratum_counts,
        target_identity_digest=target_identity_digest,
    )


# ---------------------------------------------------------------------------
# Phase 1A/3 — reused-200 labels, provenance-derived
# ---------------------------------------------------------------------------


class ReusedLabel(Record):
    """One reused development label derived from the #213 synthesis."""

    label_schema: Literal["engram-calibration-reused-label-001k-v1"] = (
        "engram-calibration-reused-label-001k-v1"
    )
    sample_id: Token
    final: dict[str, Any]
    #: per-field provenance basis (reviewer_majority | source_adjudication)
    field_bases: dict[str, str]
    #: human provenance note: model-consensus-derived, never human truth
    origin: Literal["prior_campaign_synthesis_213"] = "prior_campaign_synthesis_213"


class ReusedLabelSet(Record):
    """The complete reused-200 label set with digest binding."""

    set_schema: Literal["engram-calibration-reused-labels-001k-v1"] = (
        "engram-calibration-reused-labels-001k-v1"
    )
    campaign_id: Token
    synthesis_sha256: str
    labels: tuple[ReusedLabel, ...]

    @model_validator(mode="after")
    def binding(self) -> Self:
        if self.campaign_id != CAMPAIGN_ID_001K:
            raise ValueError("reused_labels_campaign_mismatch")
        if self.synthesis_sha256 != SYNTHESIS_SHA256:
            raise ValueError("reused_labels_synthesis_digest_mismatch")
        if len(self.labels) != EXECUTED_PREFIX:
            raise ValueError("reused_labels_count_mismatch")
        return self

    def set_digest(self) -> Digest:
        return digest(self.model_dump(mode="json"))


def derive_reused_labels(prior_root: Path) -> ReusedLabelSet:
    """Derive the reused-200 labels from verified #213 synthesis bytes."""
    synthesis = json.loads(_read_verified(prior_root, SYNTHESIS_RELPATH, SYNTHESIS_SHA256))
    labels = []
    for case in synthesis["cases"]:
        field_bases = {
            name: str(resolution.get("basis", ""))
            for name, resolution in case.get("field_resolution", {}).items()
        }
        labels.append(
            ReusedLabel(
                sample_id=case["sample_id"],
                final=case["final"],
                field_bases=field_bases,
            )
        )
    return ReusedLabelSet(
        campaign_id=CAMPAIGN_ID_001K,
        synthesis_sha256=SYNTHESIS_SHA256,
        labels=tuple(sorted(labels, key=lambda label: label.sample_id)),
    )
