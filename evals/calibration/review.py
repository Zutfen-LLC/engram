"""Blind reviewer packets, label ingestion, and the protected label ledger (#202).

Packets carry item content and recorded state ONLY — never provider scores,
model suggestions, policy outputs, or the other reviewer's labels. Label
ingestion fails closed on any count/order/identity mismatch. The frozen
ledger is canonicalized, digested, and stored once outside the repository.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from evals.admission.schema import LabelRecord, Record, digest
from evals.calibration.freeze import LABEL_GUIDE_VERSION, SamplingManifest

PACKET_SCHEMA = "engram-calibration-blind-packet-v1"


class BlindPacket(Record):
    packet_schema: str = PACKET_SCHEMA
    packet_id: str
    guide_version: str
    reviewer_hint: str  # opaque: "reviewer_a" | "reviewer_b"
    cases: list[dict[str, Any]]

    def packet_digest(self) -> str:
        return digest(self.model_dump(mode="json"))


def build_packets(
    *,
    sampling: SamplingManifest,
    samples: list[dict[str, Any]],
    packet_id: str,
) -> list[BlindPacket]:
    """Build Reviewer-A and Reviewer-B packets from the frozen sample set.

    Cases are ordered by frozen sample order (NOT by any reviewer-visible
    property) and numbered by packet order. Reviewer B sees the identical case
    view; its queue is derived later from frozen high-consequence labels.
    """
    by_id = {s["sample_id"]: s for s in samples}
    missing = [sid for sid in sampling.sample_ids if sid not in by_id]
    if missing:
        raise ValueError("packet_samples_missing_from_manifest")
    case_view: list[dict[str, Any]] = []
    for sid in sampling.sample_ids:
        s = by_id[sid]
        # Decision-time evidence only. No scores, no suggestions, no policy.
        case_view.append(
            {
                "sample_id": sid,
                "content": s["content"],
                "governed_kind": s["kind"],
                "source_type": s["source_type"],
                "review_status": s["review_status"],
                "assertion_mode": s["assertion_mode"],
                "origin": s["origin"],
                "age_days": s["age_days"],
            }
        )
    packets = [
        BlindPacket(
            packet_id=packet_id,
            guide_version=LABEL_GUIDE_VERSION,
            reviewer_hint=hint,
            cases=case_view,
        )
        for hint in ("reviewer_a", "reviewer_b")
    ]
    return packets


def write_packets(packets: list[BlindPacket], protected_dir: Path) -> dict[str, str]:
    """Persist packets + manifest to a 0700/0600 protected directory."""
    import hashlib

    protected_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(protected_dir, 0o700)
    manifest: dict[str, str] = {}
    for packet in packets:
        name = f"{packet.packet_id}.{packet.reviewer_hint}.json"
        path = protected_dir / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(json.loads(packet.model_dump_json()), indent=2, sort_keys=True))
        manifest[name] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    (protected_dir / "packets-manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2)
    )
    os.chmod(protected_dir / "packets-manifest.json", 0o600)
    return manifest


def ingest_reviewer_labels(
    *,
    packet: BlindPacket,
    labels: list[LabelRecord],
    dataset_id: str,
    dataset_version: str,
) -> list[LabelRecord]:
    """Validate one reviewer's labels against its blind packet. Fail closed.

    Rules (per #202 review discipline and the frozen label contract):
    - exact case count, unique sample ids, packet order equality;
    - every label's dataset identity matches;
    - label fields outside the packet are never borrowed;
    - no synthetic_authored rows in a human campaign.
    """
    packet_ids = [c["sample_id"] for c in packet.cases]
    label_ids = [label.sample_id for label in labels]
    if len(labels) != len(packet_ids):
        raise ValueError("label_count_mismatch")
    if len(set(label_ids)) != len(label_ids):
        raise ValueError("duplicate_label_sample_id")
    if label_ids != packet_ids:
        raise ValueError("label_order_mismatch")
    for label in labels:
        if label.dataset_id != dataset_id or label.dataset_version != dataset_version:
            raise ValueError("label_dataset_identity_mismatch")
        if label.label_origin != "human_adjudicated":
            raise ValueError("human_campaign_requires_human_labels")
    return labels


def reviewer_agreement(
    labels_a: list[LabelRecord], labels_b: list[LabelRecord] | None
) -> dict[str, Any]:
    """Agreement by calibrated dimension, pre-adjudication (protected output)."""
    if labels_b is None:
        return {"reviewer_b_present": False}
    by_b = {label.sample_id: label for label in labels_b}
    dims = ("expected_kind", "retention_value", "epistemic_state", "consequence")
    match: dict[str, int] = {d: 0 for d in dims}
    total: dict[str, int] = {d: 0 for d in dims}
    for label in labels_a:
        other = by_b.get(label.sample_id)
        if other is None:
            continue
        for dim in dims:
            a_val = getattr(label.reviewer_a.dimensions, dim)
            b_val = getattr(other.reviewer_a.dimensions, dim)
            total[dim] += 1
            if a_val == b_val:
                match[dim] += 1
    return {
        "reviewer_b_present": True,
        "pair_count": min(len(labels_a), len(labels_b)),
        "per_dimension": {
            dim: {
                "agree": match[dim],
                "total": total[dim],
                "rate": round(match[dim] / total[dim], 4) if total[dim] else None,
            }
            for dim in dims
        },
    }


def freeze_ledger(
    *,
    campaign_id: str,
    records: list[LabelRecord],
    protected_dir: Path,
) -> dict[str, str | int]:
    """Canonicalize and freeze the adjudicated label ledger outside Git."""
    protected_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(protected_dir, 0o700)
    payload = json.dumps(
        [json.loads(r.model_dump_json()) for r in sorted(records, key=lambda r: r.sample_id)],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    ledger_path = protected_dir / f"{campaign_id}-label-ledger.json"
    fd = os.open(ledger_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
    return {
        "path": str(ledger_path),
        "sha256": digest(payload),
        "records": len(records),
        "high_consequence": sum(
            1
            for r in records
            if (r.final_dimensions() or r.reviewer_a.dimensions).consequence == "high"
        ),
        "unresolved_disagreements": sum(1 for r in records if r.disagreement == "unresolved"),
    }


def verify_ledger(path: Path, expected_sha256: str) -> list[LabelRecord]:
    """Verify digest before consuming; never mutate a reviewer record."""
    data = Path(path).read_bytes()
    if digest(data) != expected_sha256:
        raise ValueError("label_ledger_digest_mismatch")
    return [LabelRecord.model_validate(row) for row in json.loads(data)]
