"""Fresh blind holdout selection for #162C (#162B corpus excluded).

Selection is mechanically gated on the committed candidate freeze: a holdout
tranche cannot be created from a snapshot unless the freeze artifact on disk
matches the current profile declarations, proving candidate definitions were
frozen before holdout labels could exist. Membership must not overlap the
#162B development tranche; the overlap check is derived, not asserted.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.admission.blind_review import (
    SelectionDefinition,
    Tranche,
    select_tranche,
)
from evals.admission.candidates.freeze import load_freeze
from evals.admission.dataset import Dataset
from evals.admission.schema import digest

HOLDOUT_SCHEMA_VERSION = "engram-162c-holdout-manifest-v1"


def _load_json(path: Path) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(path.read_text())
    return value


def check_disjoint(
    holdout_sample_ids: tuple[str, ...], development_sample_ids: tuple[str, ...]
) -> dict[str, Any]:
    overlap = sorted(set(holdout_sample_ids) & set(development_sample_ids))
    if overlap:
        raise ValueError("holdout_overlaps_development_corpus")
    return {
        "development_n": len(development_sample_ids),
        "holdout_n": len(holdout_sample_ids),
        "overlap_count": 0,
        "disjoint": True,
        "proof": "derived set intersection is empty",
    }


def select_holdout(
    snapshot: Dataset,
    development_tranche_path: Path,
    *,
    seed: str,
    target_count: int,
    code_sha: str,
    snapshot_key: bytes,
    frozen_at: datetime | None = None,
) -> dict[str, Any]:
    """Select and manifest a fresh holdout tranche from the same snapshot.

    Requires the candidate freeze; refuses development-corpus overlap.
    Returns the private manifest record (sample IDs are HMAC identities;
    no content). The blind review packet itself is produced by the existing
    #173 ``blind_review`` CLI against this tranche, unchanged.
    """
    freeze = load_freeze()
    development = _load_json(development_tranche_path)
    if development.get("snapshot_identity") != snapshot.manifest.data_digest:
        raise ValueError("development_tranche_snapshot_mismatch")
    definition = SelectionDefinition(selection_seed=seed, target_count=target_count)
    tranche = select_tranche(snapshot, definition, code_sha=code_sha, review_key=snapshot_key)
    overlap = check_disjoint(
        tranche.sample_ids, tuple(development.get("sample_ids", ()))
    )
    manifest = {
        "manifest_schema_version": HOLDOUT_SCHEMA_VERSION,
        "created_at": (frozen_at or datetime.now(tz=UTC)).isoformat(),
        "snapshot_identity": snapshot.manifest.data_digest,
        "source_dataset_id": snapshot.manifest.dataset_id,
        "source_dataset_version": snapshot.manifest.dataset_version,
        "code_sha": code_sha,
        "selection_seed": seed,
        "selection_version": definition.version,
        "selection_digest": tranche.selection_digest,
        "freeze_digest": freeze.get("freeze_digest"),
        "sample_ids": list(tranche.sample_ids),
        "review_case_ids": list(tranche.review_case_ids),
        "coverage": tranche.coverage,
        "population_coverage": tranche.population_coverage,
        "overlap_proof": overlap,
        "excluded_samples": list(development.get("sample_ids", ())),
        "target_count": target_count,
        "final_n": len(tranche.sample_ids),
        "unavailable_strata": [],
        "privacy_class": "private_dogfood",
        "allowed_use": "evaluation_only",
    }
    manifest["holdout_manifest_digest"] = digest(
        {k: v for k, v in manifest.items() if k != "holdout_manifest_digest"}
    )
    return manifest


def tranche_from_manifest(manifest: dict[str, Any]) -> Tranche:
    """Rebuild the #173 Tranche so the blind packet CLI can consume it."""
    return Tranche(
        snapshot_identity=manifest["snapshot_identity"],
        source_dataset_id=manifest["source_dataset_id"],
        source_dataset_version=manifest["source_dataset_version"],
        selection_seed=manifest["selection_seed"],
        selection_version=manifest["selection_version"],
        code_sha=manifest["code_sha"],
        sample_ids=tuple(manifest["sample_ids"]),
        review_case_ids=tuple(manifest["review_case_ids"]),
        coverage=manifest["coverage"],
        population_coverage=manifest["population_coverage"],
        selection_digest=manifest["selection_digest"],
    )


def verify_holdout_freeze_gate(manifest: dict[str, Any]) -> None:
    """A holdout comparison may run only against the frozen candidate set."""
    claimed = manifest.get("holdout_manifest_digest")
    unsigned = {k: v for k, v in manifest.items() if k != "holdout_manifest_digest"}
    if claimed != digest(unsigned):
        raise ValueError("holdout_manifest_digest_mismatch")
    freeze = load_freeze()
    if manifest.get("freeze_digest") != freeze.get("freeze_digest"):
        raise ValueError("holdout_freeze_generation_mismatch")
    if not manifest.get("overlap_proof", {}).get("disjoint"):
        raise ValueError("holdout_overlap_unproven")
