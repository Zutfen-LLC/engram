"""Blind reviewer packets, label ingestion, and the protected label ledger (#202).

Packets carry item content and recorded state ONLY — never provider scores,
model suggestions, policy outputs, or the other reviewer's labels. Label
ingestion fails closed on any count/order/identity mismatch. The frozen
ledger is canonicalized, digested, and stored once outside the repository.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal

from engram.canonicalize import canonicalize, content_hash
from evals.admission.schema import LabelRecord, Record, digest
from evals.calibration.freeze import LABEL_GUIDE_VERSION, SamplingManifest

PACKET_SCHEMA = "engram-calibration-blind-packet-v1"


class BlindPacket(Record):
    packet_schema: str = PACKET_SCHEMA
    packet_id: str
    sampling_manifest_digest: str
    guide_version: str
    reviewer_hint: str  # opaque: "reviewer_a" | "reviewer_b"
    cases: list[dict[str, Any]]

    def packet_digest(self) -> str:
        return digest(self.model_dump(mode="json"))


class VerifiedLedger(Record):
    ledger_sha256: str
    campaign_id: str
    sampling_manifest_digest: str
    reviewer_a_packet_sha256: str
    reviewer_b_packet_sha256: str
    full_population_dual_review: Literal[True] = True
    records: tuple[LabelRecord, ...]


def _packet_file_payload(packet: BlindPacket) -> bytes:
    return (
        json.dumps(json.loads(packet.model_dump_json()), indent=2, sort_keys=True) + "\n"
    ).encode()


def packet_file_digest(packet: BlindPacket) -> str:
    return hashlib.sha256(_packet_file_payload(packet)).hexdigest()


def build_packets(
    *,
    sampling: SamplingManifest,
    samples: list[dict[str, Any]],
    packet_id: str,
) -> list[BlindPacket]:
    """Build Reviewer-A and Reviewer-B packets from the frozen sample set.

    Cases are ordered by frozen sample order (NOT by any reviewer-visible
    property) and numbered by packet order. Reviewer B sees and must label the
    identical full case view independently. This deliberately exceeds the
    issue's minimum dual-review set without selecting from model/policy output.
    """
    expected_hashes = dict(zip(sampling.sample_ids, sampling.sample_hashes, strict=True))
    by_id = {s["sample_id"]: s for s in samples}
    if len(by_id) != len(samples):
        raise ValueError("duplicate_packet_sample_id")
    expected = set(sampling.sample_ids)
    if set(by_id) != expected:
        raise ValueError("packet_sample_membership_mismatch")
    for sample_id, sample in by_id.items():
        claimed_hash = sample.get("content_hash")
        actual_hash = content_hash(canonicalize(str(sample.get("content", ""))))
        if claimed_hash != expected_hashes[sample_id] or actual_hash != claimed_hash:
            raise ValueError("packet_content_hash_mismatch")
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
                "risk": s.get("risk", "unavailable"),
                "evidence_state": s.get("evidence_state", "unavailable"),
                "age_days": s["age_days"],
                "age_bucket": s.get("age_bucket", "unavailable"),
                "input_size_bucket": s.get("input_size_bucket", "unavailable"),
            }
        )
    packets = [
        BlindPacket(
            packet_id=packet_id,
            sampling_manifest_digest=sampling.manifest_digest(),
            guide_version=LABEL_GUIDE_VERSION,
            reviewer_hint=hint,
            cases=case_view,
        )
        for hint in ("reviewer_a", "reviewer_b")
    ]
    return packets


def _secure_parent_fd(path: Path) -> int:
    """Open a parent directory without following symlink components."""
    absolute = path.absolute()
    parts = absolute.parent.parts
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(parts[0], flags)
    secure_chain_started = False
    try:
        for part in parts[1:]:
            created = False
            try:
                next_fd = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(part, 0o700, dir_fd=fd)
                created = True
                next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            secure_chain_started = secure_chain_started or created
            if secure_chain_started:
                os.fchmod(fd, 0o700)
        os.fchmod(fd, 0o700)
        return fd
    except BaseException:
        os.close(fd)
        raise


def write_protected_file(path: Path, payload: bytes) -> None:
    """Atomically publish a new 0600 file through a no-symlink directory walk."""
    parent_fd = _secure_parent_fd(path)
    temporary_name = f".{path.name}.tmp.{secrets.token_hex(12)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd: int | None = None
    published = False
    try:
        fd = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
        os.fchmod(fd, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short protected artifact write")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.link(
            temporary_name,
            path.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
        published = True
        os.fsync(parent_fd)
    finally:
        if fd is not None:
            os.close(fd)
        with suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=parent_fd)
        if published:
            os.fsync(parent_fd)
        os.close(parent_fd)


def write_packets(packets: list[BlindPacket], protected_dir: Path) -> dict[str, str]:
    """Persist packets + manifest to a 0700/0600 protected directory."""
    manifest: dict[str, str] = {}
    for packet in packets:
        name = f"{packet.packet_id}.{packet.reviewer_hint}.json"
        path = protected_dir / name
        payload = _packet_file_payload(packet)
        write_protected_file(path, payload)
        manifest[name] = hashlib.sha256(payload).hexdigest()
    write_protected_file(
        protected_dir / "packets-manifest.json",
        (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode(),
    )
    return manifest


def ingest_reviewer_labels(
    *,
    packet: BlindPacket,
    labels: list[LabelRecord],
    dataset_id: str,
    dataset_version: str,
    expected_packet_digest: str,
    expected_reviewer_hint: str,
    expected_guide_version: str = LABEL_GUIDE_VERSION,
) -> list[LabelRecord]:
    """Validate one reviewer's labels against its blind packet. Fail closed.

    Rules (per #202 review discipline and the frozen label contract):
    - exact case count, unique sample ids, packet order equality;
    - every label's dataset identity matches;
    - label fields outside the packet are never borrowed;
    - no synthetic_authored rows in a human campaign.
    """
    if not hmac.compare_digest(packet_file_digest(packet), expected_packet_digest):
        raise ValueError("packet_digest_mismatch")
    if packet.reviewer_hint != expected_reviewer_hint:
        raise ValueError("packet_reviewer_role_mismatch")
    if packet.guide_version != expected_guide_version:
        raise ValueError("packet_guide_version_mismatch")
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


def _read_bound_packet(
    path: Path,
    expected_sha256: str,
    *,
    reviewer_hint: str,
    sampling: SamplingManifest,
) -> BlindPacket:
    data = path.read_bytes()
    if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected_sha256):
        raise ValueError("packet_file_digest_mismatch")
    packet = BlindPacket.model_validate_json(data)
    if data != _packet_file_payload(packet):
        raise ValueError("packet_file_not_canonical")
    if packet.reviewer_hint != reviewer_hint:
        raise ValueError("packet_reviewer_role_mismatch")
    if packet.sampling_manifest_digest != sampling.manifest_digest():
        raise ValueError("packet_sampling_manifest_mismatch")
    if packet.guide_version != LABEL_GUIDE_VERSION:
        raise ValueError("packet_guide_version_mismatch")
    packet_ids = tuple(str(case.get("sample_id", "")) for case in packet.cases)
    if packet_ids != sampling.sample_ids or len(set(packet_ids)) != len(packet_ids):
        raise ValueError("packet_sample_membership_mismatch")
    return packet


def _validate_ledger_records(
    records: list[LabelRecord],
    *,
    sampling: SamplingManifest,
    expected_dataset_id: str,
    expected_dataset_version: str,
) -> dict[str, LabelRecord]:
    by_id = {record.sample_id: record for record in records}
    if len(by_id) != len(records):
        raise ValueError("duplicate_ledger_sample_id")
    if set(by_id) != set(sampling.sample_ids):
        raise ValueError("ledger_sample_membership_mismatch")
    expected_hashes = dict(zip(sampling.sample_ids, sampling.sample_hashes, strict=True))
    for record in records:
        if record.content_hash != expected_hashes[record.sample_id]:
            raise ValueError("ledger_content_hash_mismatch")
        if (
            record.dataset_id != expected_dataset_id
            or record.dataset_version != expected_dataset_version
        ):
            raise ValueError("label_dataset_identity_mismatch")
        if record.label_origin != "human_adjudicated":
            raise ValueError("human_campaign_requires_human_labels")
        if (
            record.reviewer_b is None
            or record.reviewer_a.adjudicator_ref == record.reviewer_b.adjudicator_ref
        ):
            raise ValueError("full_dual_review_required")
        if record.review_stage != "complete" or record.disagreement == "unresolved":
            raise ValueError("ledger_contains_incomplete_review")
        if record.final_dimensions() is None:
            raise ValueError("ledger_missing_final_dimensions")
    return by_id


def freeze_ledger(
    *,
    campaign_id: str,
    records: list[LabelRecord],
    sampling: SamplingManifest,
    protected_dir: Path,
    reviewer_a_packet_path: Path,
    reviewer_a_packet_sha256: str,
    reviewer_b_packet_path: Path,
    reviewer_b_packet_sha256: str,
    expected_dataset_id: str,
    expected_dataset_version: str,
) -> dict[str, str | int]:
    """Validate packet/label provenance and freeze a complete dual-review ledger."""
    if campaign_id != sampling.campaign_id:
        raise ValueError("ledger_campaign_mismatch")
    packet_a = _read_bound_packet(
        reviewer_a_packet_path,
        reviewer_a_packet_sha256,
        reviewer_hint="reviewer_a",
        sampling=sampling,
    )
    packet_b = _read_bound_packet(
        reviewer_b_packet_path,
        reviewer_b_packet_sha256,
        reviewer_hint="reviewer_b",
        sampling=sampling,
    )
    if packet_a.packet_id != packet_b.packet_id or packet_a.cases != packet_b.cases:
        raise ValueError("reviewer_packet_views_mismatch")
    by_id = _validate_ledger_records(
        records,
        sampling=sampling,
        expected_dataset_id=expected_dataset_id,
        expected_dataset_version=expected_dataset_version,
    )
    envelope = {
        "ledger_schema": "engram-calibration-label-ledger-v2",
        "campaign_id": campaign_id,
        "sampling_manifest_digest": sampling.manifest_digest(),
        "reviewer_a_packet_sha256": reviewer_a_packet_sha256,
        "reviewer_b_packet_sha256": reviewer_b_packet_sha256,
        "full_population_dual_review": True,
        "records": [
            json.loads(by_id[sample_id].model_dump_json()) for sample_id in sampling.sample_ids
        ],
    }
    payload = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    ledger_path = protected_dir / f"{campaign_id}-label-ledger.json"
    write_protected_file(ledger_path, payload)
    return {
        "path": str(ledger_path),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "records": len(records),
        "high_consequence": sum(
            1
            for r in records
            if (r.final_dimensions() or r.reviewer_a.dimensions).consequence == "high"
        ),
        "unresolved_disagreements": 0,
    }


def verify_ledger(
    path: Path,
    expected_sha256: str,
    *,
    sampling: SamplingManifest,
    reviewer_a_packet_sha256: str,
    reviewer_b_packet_sha256: str,
    expected_dataset_id: str,
    expected_dataset_version: str,
) -> VerifiedLedger:
    """Verify every frozen provenance binding before exposing review records."""
    data = path.read_bytes()
    actual_sha256 = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(actual_sha256, expected_sha256):
        raise ValueError("label_ledger_digest_mismatch")
    envelope = json.loads(data)
    if envelope.get("ledger_schema") != "engram-calibration-label-ledger-v2":
        raise ValueError("label_ledger_schema_mismatch")
    if (
        envelope.get("campaign_id") != sampling.campaign_id
        or envelope.get("sampling_manifest_digest") != sampling.manifest_digest()
        or envelope.get("reviewer_a_packet_sha256") != reviewer_a_packet_sha256
        or envelope.get("reviewer_b_packet_sha256") != reviewer_b_packet_sha256
        or envelope.get("full_population_dual_review") is not True
    ):
        raise ValueError("label_ledger_provenance_mismatch")
    records = [LabelRecord.model_validate(row) for row in envelope.get("records", [])]
    _validate_ledger_records(
        records,
        sampling=sampling,
        expected_dataset_id=expected_dataset_id,
        expected_dataset_version=expected_dataset_version,
    )
    return VerifiedLedger(
        ledger_sha256=actual_sha256,
        campaign_id=sampling.campaign_id,
        sampling_manifest_digest=sampling.manifest_digest(),
        reviewer_a_packet_sha256=reviewer_a_packet_sha256,
        reviewer_b_packet_sha256=reviewer_b_packet_sha256,
        records=tuple(records),
    )
