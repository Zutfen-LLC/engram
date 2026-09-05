"""#162D certification corpus selection: fresh, N=100, zero spent overlap.

Extends the #162C holdout selection contract to two spent corpora. Both the
#162B development tranche and the #162C holdout manifest are excluded from
the eligible pool BEFORE selection (fail-closed lesson from #162C: selecting
then checking lets rare strata pull dev cases). The doctrine must be loadable
(gates frozen) before any certification membership can exist.

The manifest is content-free (HMAC sample IDs only) and digest-pinned.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.admission.blind_review import (
    SelectionDefinition,
    select_tranche,
)
from evals.admission.certification.doctrine import CORPUS_SIZE, load_doctrine
from evals.admission.dataset import Dataset
from evals.admission.schema import digest

CERTIFICATION_SCHEMA_VERSION = "engram-162d-certification-manifest-v1"


def _load_json(path: Path) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(path.read_text())
    return value


def check_disjoint_all(
    certification_sample_ids: tuple[str, ...],
    spent: dict[str, tuple[str, ...]],
) -> dict[str, Any]:
    """Prove zero overlap against every spent corpus; fail closed otherwise."""
    cert_set = set(certification_sample_ids)
    if len(cert_set) != len(certification_sample_ids):
        raise ValueError("duplicate_certification_sample")
    proofs: dict[str, Any] = {}
    for name, ids in spent.items():
        overlap = sorted(cert_set & set(ids))
        if overlap:
            raise ValueError(f"certification_overlaps_{name}")
        proofs[name] = {
            "spent_n": len(ids),
            "overlap_count": 0,
            "disjoint": True,
            "proof": "derived set intersection is empty",
        }
    return {
        "spent_corpora": {name: p["spent_n"] for name, p in proofs.items()},
        "certification_n": len(cert_set),
        "overlap_count": 0,
        "all_disjoint": True,
        "per_corpus_proof": proofs,
    }


def _spent_ids(
    *, development_tranche: dict[str, Any], holdout_manifest: dict[str, Any]
) -> dict[str, tuple[str, ...]]:
    return {
        "162b_development": tuple(development_tranche.get("sample_ids", ())),
        "162c_holdout": tuple(holdout_manifest.get("sample_ids", ())),
    }


def select_certification_corpus(
    snapshot: Dataset,
    development_tranche_path: Path,
    holdout_manifest_path: Path,
    *,
    seed: str,
    code_sha: str,
    snapshot_key: bytes,
    frozen_at: datetime | None = None,
) -> dict[str, Any]:
    """Select the fresh N=100 certification corpus from a live snapshot.

    Hard requirements enforced here, in order:
    1. the doctrine artifact is loadable (gates frozen before membership);
    2. both prior corpora are REMOVED from the pool before selection;
   3. selection is the deterministic rare-strata-first pass (no hand curation);
    4. zero overlap is derived, not asserted;
    5. exactly CORPUS_SIZE cases unless the eligible population is smaller —
       in which case the run must be declared INCONCLUSIVE before evaluation
       (``final_n`` and ``shortfall`` record this; the runner refuses).
    """
    doctrine = load_doctrine()
    development = _load_json(development_tranche_path)
    holdout = _load_json(holdout_manifest_path)
    for label, artifact in (
        ("development_tranche", development),
        ("holdout_manifest", holdout),
    ):
        if artifact.get("snapshot_identity") != snapshot.manifest.data_digest:
            raise ValueError(f"{label}_snapshot_mismatch")
    spent = _spent_ids(development_tranche=development, holdout_manifest=holdout)
    excluded = set().union(*spent.values()) if spent else set()
    # Remove spent samples BEFORE selection: the pool must be genuinely fresh.
    filtered = snapshot.model_copy(
        update={
            "samples": tuple(
                s for s in snapshot.samples if s.sample_id not in excluded
            )
        }
    )
    definition = SelectionDefinition(selection_seed=seed, target_count=CORPUS_SIZE)
    tranche = select_tranche(filtered, definition, code_sha=code_sha, review_key=snapshot_key)
    overlap = check_disjoint_all(tranche.sample_ids, spent)
    shortfall = max(0, CORPUS_SIZE - len(tranche.sample_ids))
    manifest = {
        "manifest_schema_version": CERTIFICATION_SCHEMA_VERSION,
        "created_at": (frozen_at or datetime.now(tz=UTC)).isoformat(),
        "snapshot_identity": snapshot.manifest.data_digest,
        "source_dataset_id": snapshot.manifest.dataset_id,
        "source_dataset_version": snapshot.manifest.dataset_version,
        "code_sha": code_sha,
        "selection_seed": seed,
        "selection_version": definition.version,
        "selection_digest": tranche.selection_digest,
        "doctrine_digest": doctrine["doctrine_digest"],
        "freeze_digest": doctrine["candidate_under_certification"]["source_freeze_digest"],
        "sample_ids": list(tranche.sample_ids),
        "review_case_ids": list(tranche.review_case_ids),
        "coverage": tranche.coverage,
        "population_coverage": tranche.population_coverage,
        "overlap_proof": overlap,
        "excluded_samples": {
            name: list(ids) for name, ids in spent.items()
        },
        "target_count": CORPUS_SIZE,
        "final_n": len(tranche.sample_ids),
        "shortfall": shortfall,
        "unavailable_strata": [],
        "privacy_class": "private_dogfood",
        "allowed_use": "162d_certification_only",
    }
    if shortfall:
        manifest["run_status"] = "inconclusive_population_shortfall"
        manifest["run_status_reason"] = (
            "eligible fresh population smaller than required N=100; run must "
            "be declared INCONCLUSIVE before evaluation per doctrine"
        )
    manifest["certification_manifest_digest"] = digest(
        {k: v for k, v in manifest.items() if k != "certification_manifest_digest"}
    )
    return manifest


def verify_certification_freeze_gate(manifest: dict[str, Any]) -> None:
    """A certification run may proceed only against frozen doctrine + fresh corpus."""
    claimed = manifest.get("certification_manifest_digest")
    unsigned = {k: v for k, v in manifest.items() if k != "certification_manifest_digest"}
    if claimed != digest(unsigned):
        raise ValueError("certification_manifest_digest_mismatch")
    doctrine = load_doctrine()
    if manifest.get("doctrine_digest") != doctrine["doctrine_digest"]:
        raise ValueError("certification_doctrine_generation_mismatch")
    if manifest.get("final_n") != CORPUS_SIZE:
        raise ValueError("certification_corpus_size_invalid")
    if not manifest.get("overlap_proof", {}).get("all_disjoint"):
        raise ValueError("certification_overlap_unproven")
