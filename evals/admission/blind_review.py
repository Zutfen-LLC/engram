"""Private, blind pre-adjudication review artifacts for protected snapshots.

This module intentionally does not call the promotion evaluator. It only reads a
frozen snapshot's capture-time metadata and selected live content in a PostgreSQL
READ ONLY transaction. No packet serializer accepts policy evaluation objects.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import re
import stat
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from engram.models import MemoryItem
from engram.safety import has_secrets
from evals.admission.dataset import Dataset, Sample
from evals.admission.schema import digest

FORBIDDEN_PACKET_FIELDS = frozenset(
    {
        "current_policy_version",
        "current_selected_lane",
        "would_promote",
        "blocker_codes",
        "readiness_state",
        "terminal_under_current_policy",
        "current_job_state",
        "promotion_score",
        "promotion_threshold",
        "candidate_policy",
    }
)
SELECTION_VERSION = "eng-calibration-001b-blind-tranche-v1"
UUID_PATTERN = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")


@dataclass(frozen=True)
class SelectionDefinition:
    selection_seed: str
    target_count: int = 50
    version: str = SELECTION_VERSION

    def __post_init__(self) -> None:
        if not self.selection_seed or self.target_count < 1:
            raise ValueError("invalid_selection_definition")


@dataclass(frozen=True)
class Tranche:
    snapshot_identity: str
    source_dataset_id: str
    source_dataset_version: str
    selection_seed: str
    selection_version: str
    code_sha: str
    sample_ids: tuple[str, ...]
    review_case_ids: tuple[str, ...]
    coverage: dict[str, dict[str, int]]
    population_coverage: dict[str, dict[str, int]]
    selection_digest: str

    def private_dict(self) -> dict[str, Any]:
        return {
            "artifact_schema_version": "engram-blind-tranche-private-v1",
            "snapshot_identity": self.snapshot_identity,
            "source_dataset_id": self.source_dataset_id,
            "source_dataset_version": self.source_dataset_version,
            "selection_seed": self.selection_seed,
            "selection_version": self.selection_version,
            "code_sha": self.code_sha,
            "sample_ids": list(self.sample_ids),
            "review_case_ids": list(self.review_case_ids),
            "coverage": self.coverage,
            "population_coverage": self.population_coverage,
            "selection_digest": self.selection_digest,
        }

    def public_dict(self) -> dict[str, Any]:
        return {
            "artifact_schema_version": "engram-blind-tranche-public-v1",
            "snapshot_identity": self.snapshot_identity,
            "source_dataset_id": self.source_dataset_id,
            "source_dataset_version": self.source_dataset_version,
            "selection_seed": self.selection_seed,
            "selection_version": self.selection_version,
            "code_sha": self.code_sha,
            "selected_count": len(self.sample_ids),
            "coverage": self.coverage,
            "population_coverage": self.population_coverage,
            "selection_digest": self.selection_digest,
        }


def hmac_sample_id(key: bytes, item_id: uuid.UUID) -> str:
    if len(key) < 32:
        raise ValueError("snapshot_key_too_short")
    return hmac.new(key, str(item_id).encode(), hashlib.sha256).hexdigest()


def _review_case_id(key: bytes, sample_id: str) -> str:
    if len(key) < 32:
        raise ValueError("snapshot_key_too_short")
    return (
        "rvw_"
        + hmac.new(key, f"{SELECTION_VERSION}:{sample_id}".encode(), hashlib.sha256).hexdigest()[
            :24
        ]
    )


def _age_bucket(sample: Sample, at: datetime) -> str:
    age_hours = (at - sample.policy_input.created_at).total_seconds() / 3600
    if age_hours < 24:
        return "lt24h"
    if age_hours < 72:
        return "24to72h"
    if age_hours < 168:
        return "72hto7d"
    if age_hours < 720:
        return "7dto30d"
    return "ge30d"


def _evidence_context(sample: Sample) -> str:
    policy = sample.policy_input
    if policy.receipt is not None and policy.retention_evidence_at is not None:
        return "qualified_recorded"
    if policy.retention_evidence_at is not None or policy.retention_confidence is not None:
        return "partial_recorded"
    if policy.source_confidence_prior is not None and policy.source_confidence_prior < 0.7:
        return "below_reference_confidence"
    return "none_recorded"


def _governance_context(sample: Sample) -> str:
    policy = sample.policy_input
    if policy.external_dispute:
        return "dispute"
    if policy.conflict_resolution_status:
        return "conflict"
    if policy.superseded:
        return "superseded"
    return "none_recorded"


def _temporal_evidence(sample: Sample, at: datetime) -> str:
    evidence_at = sample.policy_input.retention_evidence_at
    if evidence_at is None:
        return "no_evidence_timestamp"
    if at - evidence_at < timedelta(hours=72):
        return "cooling_window"
    return "evidence_matured"


def _strata(sample: Sample, at: datetime) -> dict[str, str]:
    policy = sample.policy_input
    return {
        "source_type": policy.source_type,
        "kind": policy.kind,
        "evidence": _evidence_context(sample),
        "age": _age_bucket(sample, at),
        "governance": _governance_context(sample),
        "temporal_evidence": _temporal_evidence(sample, at),
        "kind_gate": "review_gated" if not policy.kind_auto_promote else "standard",
    }


def _coverage(samples: Iterable[Sample], at: datetime) -> dict[str, dict[str, int]]:
    values = list(samples)
    if not values:
        return {}
    counters: dict[str, Counter[str]] = {field: Counter() for field in _strata(values[0], at)}
    for sample in values:
        for field, value in _strata(sample, at).items():
            counters[field][value] += 1
    return {field: dict(sorted(counter.items())) for field, counter in sorted(counters.items())}


def _rank(seed: str, sample_id: str) -> str:
    return digest([seed, sample_id])


def select_tranche(
    snapshot: Dataset,
    definition: SelectionDefinition,
    *,
    code_sha: str,
    review_key: bytes = b"\x01" * 32,
) -> Tranche:
    """Select a deterministic rare-strata-first tranche without evaluation output."""
    if snapshot.manifest.privacy_class != "private_dogfood":
        raise ValueError("private_dogfood_snapshot_required")
    if any(sample.content is not None or sample.label is not None for sample in snapshot.samples):
        raise ValueError("unlabeled_content_free_snapshot_required")
    if len(code_sha) != 40 or any(char not in "0123456789abcdef" for char in code_sha):
        raise ValueError("invalid_code_sha")
    population = tuple(sorted(snapshot.samples, key=lambda sample: sample.sample_id))
    if len({sample.sample_id for sample in population}) != len(population):
        raise ValueError("duplicate_sample")
    target = min(definition.target_count, len(population))
    counts = _coverage(population, snapshot.evaluation_at)
    selected: list[Sample] = []
    remaining = set(sample.sample_id for sample in population)
    by_id = {sample.sample_id: sample for sample in population}

    # First cover every available safe categorical value, rarest values first.
    categories = sorted(
        (
            (count, field, value)
            for field, values in counts.items()
            for value, count in values.items()
        ),
        key=lambda entry: (entry[0], entry[1], entry[2]),
    )
    for _, field, value in categories:
        if len(selected) >= target:
            break
        candidates = [
            sample
            for sample in population
            if sample.sample_id in remaining
            and _strata(sample, snapshot.evaluation_at)[field] == value
        ]
        if candidates:
            choice = min(
                candidates,
                key=lambda sample: (
                    _rank(definition.selection_seed, sample.sample_id),
                    sample.sample_id,
                ),
            )
            selected.append(choice)
            remaining.remove(choice.sample_id)

    # Then greedily favor underrepresented values. A deterministic HMAC-like
    # hash tie-breaker prevents source ordering from affecting membership.
    while len(selected) < target:
        selected_coverage = _coverage(selected, snapshot.evaluation_at)

        def score(
            sample: Sample, coverage: dict[str, dict[str, int]] = selected_coverage
        ) -> tuple[float, str, str]:
            rarity = 0.0
            for field, value in _strata(sample, snapshot.evaluation_at).items():
                population_count = counts[field][value]
                selected_count = coverage.get(field, {}).get(value, 0)
                rarity += 1.0 / (population_count * (selected_count + 1))
            return (-rarity, _rank(definition.selection_seed, sample.sample_id), sample.sample_id)

        choice = min((by_id[sample_id] for sample_id in remaining), key=score)
        selected.append(choice)
        remaining.remove(choice.sample_id)

    selected = sorted(selected, key=lambda sample: sample.sample_id)
    sample_ids = tuple(sample.sample_id for sample in selected)
    review_case_ids = tuple(_review_case_id(review_key, sample_id) for sample_id in sample_ids)
    if len(set(review_case_ids)) != len(review_case_ids):
        raise ValueError("review_case_id_collision")
    snapshot_identity = snapshot.manifest.data_digest
    selection_digest = digest(
        {
            "snapshot_identity": snapshot_identity,
            "definition": {
                "seed": definition.selection_seed,
                "version": definition.version,
                "target": target,
            },
            "code_sha": code_sha,
            "sample_ids": list(sample_ids),
        }
    )
    return Tranche(
        snapshot_identity=snapshot_identity,
        source_dataset_id=snapshot.manifest.dataset_id,
        source_dataset_version=snapshot.manifest.dataset_version,
        selection_seed=definition.selection_seed,
        selection_version=definition.version,
        code_sha=code_sha,
        sample_ids=sample_ids,
        review_case_ids=review_case_ids,
        coverage=_coverage(selected, snapshot.evaluation_at),
        population_coverage=counts,
        selection_digest=selection_digest,
    )


def resolve_content_rows(
    rows: Iterable[Mapping[Any, Any]],
    key: bytes,
    selected_sample_ids: set[str],
    *,
    expected_content_hashes: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Accept rows only when protected HMAC and captured-hash identities match."""
    resolved: dict[str, str] = {}
    for row in rows:
        item_id = row["id"]
        if not isinstance(item_id, uuid.UUID):
            item_id = uuid.UUID(str(item_id))
        sample_id = hmac_sample_id(key, item_id)
        if sample_id in selected_sample_ids:
            if (
                expected_content_hashes
                and row["content_hash"] != expected_content_hashes[sample_id]
            ):
                raise ValueError("snapshot_content_hash_mismatch")
            content = row["content"]
            if not isinstance(content, str):
                raise ValueError("invalid_content")
            if sample_id in resolved:
                raise ValueError("duplicate_hmac_identity")
            resolved[sample_id] = content
    if set(resolved) != selected_sample_ids:
        raise ValueError("content_membership_mismatch")
    return resolved


async def resolve_selected_content(
    url: str,
    tenant: uuid.UUID,
    principal: uuid.UUID,
    key: bytes,
    snapshot: Dataset,
    tranche: Tranche,
) -> tuple[dict[str, str], dict[str, int | bool]]:
    """Fetch only selected content hashes under repeatable-read READ ONLY RLS context."""
    sample_map = {sample.sample_id: sample for sample in snapshot.samples}
    if set(tranche.sample_ids) - set(sample_map):
        raise ValueError("tranche_not_in_snapshot")
    content_hashes = {
        sample_map[sample_id].policy_input.content_hash for sample_id in tranche.sample_ids
    }
    engine = create_async_engine(url, echo=False, hide_parameters=True)
    statements: list[str] = []
    try:
        async with engine.connect() as connection, connection.begin():
            await connection.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            statements.append("read_only_transaction")
            await connection.execute(
                text(
                    "SELECT set_config('app.tenant_id', :tenant, true), "
                    "set_config('app.principal_id', :principal, true)"
                ),
                {"tenant": str(tenant), "principal": str(principal)},
            )
            statements.append("rls_context")
            async with AsyncSession(bind=connection, autoflush=False) as session:
                rows = (
                    (
                        await session.execute(
                            select(
                                MemoryItem.id, MemoryItem.content_hash, MemoryItem.content
                            ).where(
                                MemoryItem.tenant_id == tenant,
                                MemoryItem.valid_to.is_(None),
                                MemoryItem.content_hash.in_(content_hashes),
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                statements.append("selected_content_hash_lookup")
                expected_content_hashes = {
                    sample_id: sample_map[sample_id].policy_input.content_hash
                    for sample_id in tranche.sample_ids
                }
                resolved = resolve_content_rows(
                    rows,
                    key,
                    set(tranche.sample_ids),
                    expected_content_hashes=expected_content_hashes,
                )
                return resolved, {
                    "transaction_read_only": True,
                    "queried_content_hash_count": len(content_hashes),
                    "returned_row_count": len(rows),
                    "identity_verified_count": len(resolved),
                    "write_statement_count": 0,
                    "job_or_event_write_count": 0,
                }
    finally:
        await engine.dispose()


def _case(sample: Sample, review_case_id: str, content: str, at: datetime) -> dict[str, Any]:
    policy = sample.policy_input
    if has_secrets(content):
        shown_content = "[REDACTED: secret detector]"
        content_redacted = True
    else:
        shown_content, uuid_count = UUID_PATTERN.subn("[REDACTED: raw UUID]", content)
        content_redacted = uuid_count > 0
    evidence: dict[str, Any] = {
        "recorded": _evidence_context(sample),
        "assertion_origin": "unknown_not_recorded",
        "evidence_root_independence": "unknown_not_recorded",
    }
    if policy.retention_evidence_at is not None:
        evidence["captured_evidence_at"] = policy.retention_evidence_at.isoformat()
    governance = {
        "conflict": policy.conflict_resolution_status or "none_recorded",
        "external_dispute": "recorded" if policy.external_dispute else "none_recorded",
        "supersession": "recorded" if policy.superseded else "none_recorded",
        "temporal_validity": "unknown_not_recorded",
        "scope": "unknown_not_recorded",
    }
    return {
        "review_case_id": review_case_id,
        "content": shown_content,
        "content_redacted": content_redacted,
        "stored_kind": policy.kind,
        "source_assertion_mode": {
            "source_type": policy.source_type,
            "safe_provenance": "unknown_not_recorded",
        },
        "captured": {"at": policy.created_at.isoformat(), "age_bucket": _age_bucket(sample, at)},
        "decision_time_evidence_context": evidence,
        "governance_context": governance,
    }


def _assert_blind(packet: Mapping[str, Any]) -> None:
    rendered = json.dumps(packet, sort_keys=True)
    if any(field in rendered for field in FORBIDDEN_PACKET_FIELDS):
        raise ValueError("blindness_contract_violation")
    if "-" in rendered and any(str(item_id) in rendered for item_id in _uuid_strings(rendered)):
        raise ValueError("raw_uuid_contract_violation")


def _uuid_strings(value: str) -> tuple[str, ...]:
    import re

    return tuple(
        re.findall(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", value)
    )


def build_packet(snapshot: Dataset, tranche: Tranche, content: Mapping[str, str]) -> dict[str, Any]:
    """Build the only human-facing artifact; it deliberately excludes policy output."""
    if set(content) != set(tranche.sample_ids):
        raise ValueError("content_membership_mismatch")
    by_id = {sample.sample_id: sample for sample in snapshot.samples}
    cases = [
        _case(by_id[sample_id], review_case_id, content[sample_id], snapshot.evaluation_at)
        for sample_id, review_case_id in zip(
            tranche.sample_ids, tranche.review_case_ids, strict=True
        )
    ]
    packet = {
        "packet_schema_version": "engram-blind-review-packet-v1",
        "selection_digest": tranche.selection_digest,
        "case_count": len(cases),
        "cases": cases,
    }
    _assert_blind(packet)
    return packet


def build_review_state(tranche: Tranche) -> dict[str, Any]:
    return {
        "review_state_schema_version": "engram-blind-review-state-v1",
        "selection_digest": tranche.selection_digest,
        "cases": [
            {
                "review_case_id": review_case_id,
                "reviewer_a": None,
                "reviewer_a_frozen_at": None,
                "reviewer_b_required": None,
                "reviewer_b": None,
                "disagreement": None,
                "resolution": None,
                "policy_reveal": None,
            }
            for review_case_id in tranche.review_case_ids
        ],
    }


def markdown_packet(packet: Mapping[str, Any]) -> str:
    lines = ["# Engram blind review packet", "", f"Cases: {packet['case_count']}", ""]
    for number, case in enumerate(packet["cases"], start=1):
        source = case["source_assertion_mode"]
        captured = case["captured"]
        evidence = case["decision_time_evidence_context"]
        governance = case["governance_context"]
        source_line = f"Source/assertion mode: {source['source_type']}; assertion origin "
        source_line += str(evidence["assertion_origin"])
        evidence_line = f"Decision-time evidence context: {evidence['recorded']}; "
        evidence_line += "evidence-root independence " + str(evidence["evidence_root_independence"])
        governance_line = "Governance context: " + "; ".join(
            f"{key}={value}" for key, value in governance.items()
        )
        lines.extend(
            [
                f"## Case {number} / {case['review_case_id']}",
                "",
                "Content:",
                str(case["content"]),
                "",
                f"Stored kind: {case['stored_kind']}",
                source_line,
                f"Captured: {captured['at']} ({captured['age_bucket']})",
                evidence_line,
                governance_line,
                "",
            ]
        )
    return "\n".join(lines)


def _secure_write(path: Path, content: str) -> None:
    target = path.resolve()
    repository = Path(__file__).resolve().parents[2]
    if target.is_relative_to(repository):
        raise ValueError("private_output_must_be_outside_repository")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(content)
    if stat.S_IMODE(target.stat().st_mode) != 0o600:
        raise ValueError("private_file_permissions_invalid")


def _load_snapshot(path: Path) -> Dataset:
    return Dataset.model_validate_json(path.read_bytes())


def _load_tranche(path: Path) -> Tranche:
    data = json.loads(path.read_text())
    return Tranche(
        snapshot_identity=data["snapshot_identity"],
        source_dataset_id=data["source_dataset_id"],
        source_dataset_version=data["source_dataset_version"],
        selection_seed=data["selection_seed"],
        selection_version=data["selection_version"],
        code_sha=data["code_sha"],
        sample_ids=tuple(data["sample_ids"]),
        review_case_ids=tuple(data["review_case_ids"]),
        coverage=data["coverage"],
        population_coverage=data["population_coverage"],
        selection_digest=data["selection_digest"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    select_parser = subparsers.add_parser("select")
    select_parser.add_argument("--snapshot", type=Path, required=True)
    select_parser.add_argument("--snapshot-key-file", type=Path, required=True)
    select_parser.add_argument("--seed", required=True)
    select_parser.add_argument("--target-count", type=int, default=50)
    select_parser.add_argument("--code-sha", required=True)
    select_parser.add_argument("--private-output", type=Path, required=True)
    select_parser.add_argument("--public-output", type=Path, required=True)
    packet_parser = subparsers.add_parser("packet")
    packet_parser.add_argument("--snapshot", type=Path, required=True)
    packet_parser.add_argument("--tranche", type=Path, required=True)
    packet_parser.add_argument("--snapshot-key-file", type=Path, required=True)
    packet_parser.add_argument("--database-url-env", default="ENGRAM_DATABASE_URL")
    packet_parser.add_argument("--tenant", type=uuid.UUID, required=True)
    packet_parser.add_argument("--principal", type=uuid.UUID, required=True)
    packet_parser.add_argument("--json-output", type=Path, required=True)
    packet_parser.add_argument("--markdown-output", type=Path, required=True)
    packet_parser.add_argument("--state-output", type=Path, required=True)
    packet_parser.add_argument("--proof-output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "select":
            snapshot = _load_snapshot(args.snapshot)
            key = bytes.fromhex(args.snapshot_key_file.read_text().strip())
            tranche = select_tranche(
                snapshot,
                SelectionDefinition(selection_seed=args.seed, target_count=args.target_count),
                code_sha=args.code_sha,
                review_key=key,
            )
            _secure_write(
                args.private_output,
                json.dumps(tranche.private_dict(), sort_keys=True, indent=2) + "\n",
            )
            args.public_output.parent.mkdir(parents=True, exist_ok=True)
            args.public_output.write_text(
                json.dumps(tranche.public_dict(), sort_keys=True, indent=2) + "\n"
            )
        else:
            snapshot = _load_snapshot(args.snapshot)
            tranche = _load_tranche(args.tranche)
            key = bytes.fromhex(args.snapshot_key_file.read_text().strip())
            content, proof = asyncio.run(
                resolve_selected_content(
                    os.environ[args.database_url_env],
                    args.tenant,
                    args.principal,
                    key,
                    snapshot,
                    tranche,
                )
            )
            packet = build_packet(snapshot, tranche, content)
            _secure_write(args.json_output, json.dumps(packet, sort_keys=True, indent=2) + "\n")
            _secure_write(args.markdown_output, markdown_packet(packet))
            _secure_write(
                args.state_output,
                json.dumps(build_review_state(tranche), sort_keys=True, indent=2) + "\n",
            )
            _secure_write(args.proof_output, json.dumps(proof, sort_keys=True, indent=2) + "\n")
    except Exception:
        print('{"error":"blind_review_failed_no_private_details"}')
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
