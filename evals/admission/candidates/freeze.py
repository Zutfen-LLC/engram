"""Freeze mechanics for candidate definitions and evaluation configurations.

The freeze artifact is committed (content-free: version strings, parameter
values, digests). Holdout selection refuses to run against a snapshot whose
freeze generation does not match, which mechanically proves candidate
definitions were frozen before holdout labels could exist.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from evals.admission.candidates.profiles import build_profiles
from evals.admission.schema import digest

FREEZE_SCHEMA_VERSION = "engram-candidate-freeze-v1"
FREEZE_PATH = Path(__file__).parent / "candidate-freeze-162c-v1.json"


def freeze_record(now: datetime) -> dict[str, Any]:
    """Build the content-free freeze artifact from the declared profiles."""
    declarations = []
    for profile in build_profiles():
        record = profile.declaration.model_dump(mode="json")
        declarations.append(record)
    return {
        "freeze_schema_version": FREEZE_SCHEMA_VERSION,
        "frozen_at": now.isoformat(),
        "issue": "162C",
        "dataset_roles": {
            "synthetic_contract": "policy-boundary and adversarial regression",
            "dogfood-development-v1": (
                "50-case #162B human corpus; development/hypothesis-generating "
                "only; informed candidate design; never certification"
            ),
            "holdout": (
                "fresh blind dogfood sample; selection only after this freeze; "
                "labels adjudicated blind; generalization evidence"
            ),
        },
        "candidate_declarations": declarations,
        "oracle_analyses": [
            {
                "name": "oracle-risk-upper-bound-v1",
                "question": (
                    "If Engram had a reliable human consequence classifier, how "
                    "much additional automatic admission would become possible "
                    "without unsafe outcomes?"
                ),
                "excluded_from_shortlist": True,
            }
        ],
    }


def write_freeze(path: Path = FREEZE_PATH, *, now: datetime) -> str:
    record = freeze_record(now)
    digest_value = freeze_digest(record)
    record["freeze_digest"] = digest_value
    path.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n")
    return digest_value


def freeze_digest(record: dict[str, Any]) -> str:
    unsigned = {k: v for k, v in record.items() if k != "freeze_digest"}
    return digest(unsigned)


def load_freeze(path: Path = FREEZE_PATH) -> dict[str, Any]:
    record: dict[str, Any] = json.loads(path.read_text())
    claimed = record.get("freeze_digest")
    if claimed != freeze_digest(record):
        raise ValueError("freeze_digest_mismatch")
    if record.get("freeze_schema_version") != FREEZE_SCHEMA_VERSION:
        raise ValueError("invalid_freeze_schema")
    frozen_versions = tuple(
        d["policy_version"] for d in record["candidate_declarations"]
    )
    current_versions = tuple(
        p.declaration.policy_version for p in build_profiles()
    )
    if frozen_versions != current_versions:
        raise ValueError("freeze_profile_drift")
    for declaration in record["candidate_declarations"]:
        if digest(declaration) not in {
            digest(p.declaration.model_dump(mode="json")) for p in build_profiles()
        }:
            raise ValueError("freeze_declaration_drift")
    return record
