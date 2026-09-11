"""Operator-attested subscription-UI reviewer provenance (#209).

A narrow, explicitly weaker but HONEST provenance mode for the #208 frontier
review campaign: the three reviewer lanes are executed through the
maintainer's existing Claude.ai / ChatGPT / z.ai consumer subscriptions, with
the human operator attesting which service/model produced each response.

Trust boundary (frozen here, before any #208 review executes):

- Engram machine-verifies: the exact frozen neutral case evidence, the
  canonical request batches, the prompt/instruction digest, the exact copied
  raw response bytes, parsing, lane membership, consensus, audit selection,
  and final ledger mechanics — exactly as for the machine-verified path.
- The human operator attests which consumer service/model produced the
  response (``SubscriptionReviewAttestation``).
- Engram does NOT claim to machine-verify provider execution identity:
  ``provider_metadata_available`` is recorded ``false``, provider
  request/response IDs are structurally ``None``, and the consumer service's
  hidden system/developer layer is explicitly recorded as unobservable
  (``opaque_service_system_layer``).

This mode can never masquerade as ``provider_metadata`` (the #206
machine-verified path is unchanged and requires a digest-bound provider
metadata artifact whose bytes mechanically derive the identity), and it can
never be reached by generic ``executor_attestation``. It is campaign-scoped:
``SUBSCRIPTION_UI_OPTED_CAMPAIGNS`` is the complete opt-in list, bound to the
frozen consensus protocol version, and every boundary (lane init, record
ingestion, lane freeze/load, final ledger verification) re-checks it.

This module performs NO network access and invokes NO model. The subscription
UIs are operated by the human maintainer; this module only exports
deterministic paste-ready logical review batches and mechanically imports the
verbatim raw responses the maintainer returns.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self

from pydantic import model_validator

from evals.admission.schema import Record
from evals.calibration.consensus import (
    CONSENSUS_PROTOCOL_VERSION,
    FAMILY_BY_SLOT,
    REVIEWER_SLOTS,
    digest_of,
)

if TYPE_CHECKING:
    from evals.calibration.consensus import ExecutionReceipt as ReceiptAlias
    from evals.calibration.consensus import ModelReviewRecord, ReviewerIdentity, SlotName
    from evals.calibration.freeze import SamplingManifest
    from evals.calibration.ingestion import LaneSession as LaneSessionAlias

# --- Frozen identities --------------------------------------------------------

SUBSCRIPTION_ATTESTATION_SCHEMA: Literal[
    "engram-calibration-subscription-attestation-209-v1"
] = "engram-calibration-subscription-attestation-209-v1"

SUBSCRIPTION_REVIEW_BATCH_SCHEMA: Literal[
    "engram-calibration-subscription-review-batch-209-v1"
] = "engram-calibration-subscription-review-batch-209-v1"

SUBSCRIPTION_REVIEW_BATCH_MANIFEST_SCHEMA: Literal[
    "engram-calibration-subscription-review-batch-manifest-209-v1"
] = "engram-calibration-subscription-review-batch-manifest-209-v1"

#: The provenance mode value. Deliberately distinct from both
#: ``provider_metadata`` and ``executor_attestation``.
SUBSCRIPTION_PROVENANCE_MODE: Literal["operator_attested_subscription_ui"] = (
    "operator_attested_subscription_ui"
)

#: Frozen closed service vocabulary per reviewer slot (#208 table).
SERVICE_BY_SLOT: dict[str, str] = {
    "model_a": "claude_ai",
    "model_b": "chatgpt",
    "model_c": "z_ai",
}
SERVICE_VOCABULARY: frozenset[str] = frozenset(SERVICE_BY_SLOT.values())

#: Frozen stable model identifier per slot for subscription lanes. The exact
#: user-visible display name varies per service/UI and is recorded per
#: attestation; THIS value is what the frozen ``ReviewerIdentity`` binds so
#: record/lane identity comparison stays exact and stable. It names the
#: family, not a cryptographically proven model version — never promotable
#: to a provider-metadata claim.
SUBSCRIPTION_MODEL_BY_SLOT: dict[str, str] = {
    "model_a": "claude-opus@claude-ai-subscription-ui",
    "model_b": "gpt-astra@chatgpt-subscription-ui",
    "model_c": "glm-5-3-max@z-ai-subscription-ui",
}

#: The COMPLETE campaign opt-in for the subscription-UI provenance mode:
#: (campaign_id, consensus protocol version) pairs. Anything not listed can
#: never initialize, ingest, freeze, or verify a subscription-UI lane. #208
#: executes campaign ``eng-calibration-001f`` under protocol
#: ``eng-calibration-consensus-206-v1`` — and nothing else.
SUBSCRIPTION_UI_OPTED_CAMPAIGNS: frozenset[tuple[str, str]] = frozenset(
    {("eng-calibration-001f", "eng-calibration-consensus-206-v1")}
)

#: Deterministic logical-batch limits (#209 export requirements).
BATCH_MAX_CASES: int = 50
BATCH_MAX_SERIALIZED_BYTES: int = 120_000

#: The exact per-case result fields a subscription reviewer must return.
BATCH_RESULT_FIELDS: tuple[str, ...] = (
    "sample_id",
    "expected_kind",
    "retention_value",
    "epistemic_state",
    "consequence",
    "acceptable_abstention",
    "reviewer_confidence",
)

_BATCH_ID_PATTERN = re.compile(r"[A-Za-z0-9:_\-.]+")


def subscription_mode_permitted(campaign_id: str, protocol_version: str) -> bool:
    """The one opt-in check, re-run at every authority boundary."""
    return (campaign_id, protocol_version) in SUBSCRIPTION_UI_OPTED_CAMPAIGNS


def subscription_reviewer_identity(
    slot: SlotName,
    *,
    reviewer_config_digest: str,
    prompt_digest: str,
) -> ReviewerIdentity:
    """Build the frozen ``ReviewerIdentity`` for one subscription lane.

    The provider model identifier is the frozen family-scoped subscription
    token (stable across attestation display-name variation); the prompt
    digest is the canonical #206 labeling-instructions digest, so the lane
    binds the exact same frozen semantics as the machine-verified path.
    """
    from evals.calibration.consensus import ReviewerIdentity

    if slot not in REVIEWER_SLOTS:
        raise ValueError("unknown_reviewer_slot")
    return ReviewerIdentity(
        reviewer_slot=slot,
        reviewer_family=FAMILY_BY_SLOT[slot],
        provider_model_identifier=SUBSCRIPTION_MODEL_BY_SLOT[slot],
        reviewer_config_digest=reviewer_config_digest,
        prompt_digest=prompt_digest,
    )


# --- SubscriptionReviewAttestation ---------------------------------------------


def _attestation_digest_payload(payload: dict[str, Any]) -> str:
    return digest_of({k: v for k, v in payload.items() if k != "attestation_digest"})


class SubscriptionReviewAttestation(Record):
    """Protected operator attestation for ONE logical review batch response.

    The operator attests: this exact raw response (digest-bound) to this
    exact canonical emitted batch (digest-bound) was produced through the
    named consumer subscription service, with the named user-visible model
    selected, in this reviewer slot. Engram machine-verifies the digests and
    the frozen slot/service/family mapping; the service/model identity itself
    is operator-attested, never claimed as machine-verified.
    """

    attestation_schema: Literal[
        "engram-calibration-subscription-attestation-209-v1"
    ] = SUBSCRIPTION_ATTESTATION_SCHEMA
    campaign_id: str
    protocol_version: str
    reviewer_slot: str
    reviewer_family: str
    service: str
    user_visible_model_name: str
    operator_reference: str
    attested_at: str
    request_batch_digest: str
    raw_response_digest: str
    subscription_ui_execution: Literal[True] = True
    provider_metadata_available: Literal[False] = False
    provider_request_id: None = None
    provider_response_id: None = None
    opaque_service_system_layer: Literal[True] = True
    conversation_reference: str | None = None
    attestation_digest: str

    @model_validator(mode="after")
    def attestation_contract(self) -> Self:
        if self.protocol_version != CONSENSUS_PROTOCOL_VERSION:
            raise ValueError("protocol_version_mismatch")
        if self.reviewer_slot not in REVIEWER_SLOTS:
            raise ValueError("unknown_reviewer_slot")
        if FAMILY_BY_SLOT[self.reviewer_slot] != self.reviewer_family:
            raise ValueError("attestation_family_does_not_match_frozen_slot")
        if SERVICE_BY_SLOT[self.reviewer_slot] != self.service:
            raise ValueError("attestation_service_does_not_match_frozen_slot")
        if not self.user_visible_model_name:
            raise ValueError("attestation_requires_user_visible_model_name")
        if not self.operator_reference:
            raise ValueError("attestation_requires_operator_reference")
        if not self.attested_at:
            raise ValueError("attestation_requires_attested_timestamp")
        if len(self.request_batch_digest) != 64 or len(self.raw_response_digest) != 64:
            raise ValueError("attestation_requires_sha256_hex_digests")
        if not hmac.compare_digest(
            _attestation_digest_payload(self.model_dump(mode="json")), self.attestation_digest
        ):
            raise ValueError("attestation_digest_mismatch")
        return self

    def payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def build_attestation(
    *,
    campaign_id: str,
    reviewer_slot: str,
    service: str,
    user_visible_model_name: str,
    operator_reference: str,
    request_batch_digest: str,
    raw_response_digest: str,
    attested_at: str | None = None,
    conversation_reference: str | None = None,
) -> SubscriptionReviewAttestation:
    """Construct a digest-bound attestation (the only production path)."""
    if reviewer_slot not in REVIEWER_SLOTS:
        raise ValueError("unknown_reviewer_slot")
    payload: dict[str, Any] = {
        "attestation_schema": SUBSCRIPTION_ATTESTATION_SCHEMA,
        "campaign_id": campaign_id,
        "protocol_version": CONSENSUS_PROTOCOL_VERSION,
        "reviewer_slot": reviewer_slot,
        "reviewer_family": FAMILY_BY_SLOT[reviewer_slot],
        "service": service,
        "user_visible_model_name": user_visible_model_name,
        "operator_reference": operator_reference,
        "attested_at": attested_at or datetime.now(UTC).isoformat(),
        "request_batch_digest": request_batch_digest,
        "raw_response_digest": raw_response_digest,
        "subscription_ui_execution": True,
        "provider_metadata_available": False,
        "provider_request_id": None,
        "provider_response_id": None,
        "opaque_service_system_layer": True,
        "conversation_reference": conversation_reference,
    }
    payload["attestation_digest"] = _attestation_digest_payload(payload)
    return SubscriptionReviewAttestation.model_validate(payload)


def verify_evidence_subscription_attestation(evidence: Any) -> None:
    """Fail closed unless the evidence's embedded attestation binds it.

    Called by the ``ExecutionEvidence`` schema validator (lazily, to avoid an
    import cycle): a subscription-UI evidence must embed a digest-bound
    attestation whose slot/family/campaign match the attested actual
    identity, and the campaign opt-in must hold. The per-case request
    binding (generation + request item digest) is carried by the evidence
    itself and verified against the retained request batches exactly as on
    the machine path.
    """
    payload = evidence.subscription_attestation
    if not isinstance(payload, dict) or not payload:
        raise ValueError("subscription_ui_identity_requires_attestation")
    attestation = SubscriptionReviewAttestation.model_validate(payload)
    if attestation.reviewer_slot != evidence.actual_reviewer_slot:
        raise ValueError("subscription_attestation_slot_mismatch")
    if attestation.reviewer_family != evidence.actual_reviewer_family:
        raise ValueError("subscription_attestation_family_mismatch")
    if attestation.campaign_id != evidence.campaign_id:
        raise ValueError("subscription_attestation_campaign_mismatch")
    if not subscription_mode_permitted(
        attestation.campaign_id, attestation.protocol_version
    ):
        raise ValueError("subscription_ui_mode_not_opted_in_for_campaign")


# --- Deterministic logical review-batch export ---------------------------------


def batches_directory(protected_root: Path) -> Path:
    return protected_root / "batches"


def batch_manifest_path(protected_root: Path) -> Path:
    return batches_directory(protected_root) / "manifest.json"


def batch_prompt_path(protected_root: Path, batch_id: str) -> Path:
    return batches_directory(protected_root) / f"{batch_id}.json"


def batch_id_for_index(campaign_id: str, index: int) -> str:
    return f"{campaign_id}:sub-review-{index:03d}"


def _build_batch_prompt(
    *,
    batch_id: str,
    campaign_id: str,
    cases: list[dict[str, Any]],
    instructions: dict[str, Any],
    label_guide_version: str,
    reviewer_instructions_version: str,
) -> dict[str, Any]:
    """One neutral paste-ready prompt, identical across all three lanes.

    No slot/family/service/model identity appears anywhere in the prompt —
    reviewer identity comes from the operator attestation at import time,
    never from telling the model which lane it is.
    """
    return {
        "batch_schema": SUBSCRIPTION_REVIEW_BATCH_SCHEMA,
        "batch_id": batch_id,
        "campaign_id": campaign_id,
        "protocol_version": CONSENSUS_PROTOCOL_VERSION,
        "label_guide_version": label_guide_version,
        "reviewer_instructions_version": reviewer_instructions_version,
        "instructions": instructions,
        "cases": cases,
        "response_contract": {
            "respond_with": (
                "ONE strict JSON object and nothing else — no markdown fences,"
                " no prose before or after."
            ),
            "shape": {
                "results": (
                    "array with EXACTLY one result object per supplied case,"
                    " in the supplied order"
                )
            },
            "result_fields": {
                "sample_id": "string — echo the case's sample_id exactly",
                "expected_kind": "allowed vocabulary from the instructions",
                "retention_value": "allowed vocabulary from the instructions",
                "epistemic_state": "allowed vocabulary from the instructions",
                "consequence": "allowed vocabulary from the instructions",
                "acceptable_abstention": "allowed vocabulary from the instructions",
                "reviewer_confidence": '"low" | "medium" | "high"',
            },
            "rules": [
                "Return exactly the supplied sample_ids in the supplied order.",
                "No omitted cases, no extra cases, no duplicate cases.",
                "Each result object carries EXACTLY the seven result fields.",
                "Do NOT reproduce or quote the case source content.",
                "You never see provider scores, policy outputs, current"
                " decisions, or any other reviewer's outputs; do not guess at them.",
                "unknown is a legitimate, protected answer on every field;"
                " never guess to avoid it.",
            ],
        },
    }


def serialize_prompt(prompt: dict[str, Any]) -> bytes:
    return (json.dumps(prompt, sort_keys=True, indent=2) + "\n").encode()


def export_review_batches(
    protected_root: Path,
    *,
    campaign_id: str,
    sampling: SamplingManifest,
    neutral_packet_path: Path,
    neutral_packet_manifest: Path,
    source_packet_digest: str,
    max_cases: int = BATCH_MAX_CASES,
    max_serialized_bytes: int = BATCH_MAX_SERIALIZED_BYTES,
) -> dict[str, Any]:
    """Emit the deterministic paste-ready logical review batches.

    Batches partition the EXACT frozen packet membership in frozen order,
    bounded by ``max_cases`` and the serialized-size ceiling. Everything is
    written exclusively under ``<protected_root>/batches/``; a re-export
    must be byte-identical or it fails closed.
    """
    from evals.calibration.freeze import LABEL_GUIDE_VERSION
    from evals.calibration.ingestion import (
        LABELING_INSTRUCTIONS,
        labeling_instructions_digest,
        load_neutral_packet_verified,
    )
    from evals.calibration.review import write_protected_file
    from evals.calibration.reviewer_instructions import REVIEWER_INSTRUCTIONS_VERSION

    if max_cases < 1 or max_serialized_bytes < 1:
        raise ValueError("subscription_batch_limits_must_be_positive")
    if not subscription_mode_permitted(campaign_id, CONSENSUS_PROTOCOL_VERSION):
        raise ValueError("subscription_ui_mode_not_opted_in_for_campaign")
    packet, packet_sha = load_neutral_packet_verified(
        neutral_packet_path, manifest_path=neutral_packet_manifest
    )
    if packet.sampling_manifest_digest != sampling.manifest_digest():
        raise ValueError("subscription_batch_sampling_manifest_mismatch")
    if str(getattr(packet, "source_packet_digest", "")) != source_packet_digest:
        raise ValueError("subscription_batch_source_packet_mismatch")
    if packet.guide_version != LABEL_GUIDE_VERSION:
        raise ValueError("subscription_batch_guide_version_mismatch")
    packet_ids = tuple(str(case["sample_id"]) for case in packet.cases)
    if packet_ids != tuple(sampling.sample_ids):
        raise ValueError("subscription_batch_membership_order_mismatch")

    # Deterministic greedy split over the frozen order.
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_bytes = 0
    for case in packet.cases:
        case_bytes = len(json.dumps(case, sort_keys=True).encode())
        if current and (
            len(current) >= max_cases or current_bytes + case_bytes > max_serialized_bytes
        ):
            groups.append(current)
            current = []
            current_bytes = 0
        current.append(case)
        current_bytes += case_bytes
    if current:
        groups.append(current)

    manifest: dict[str, Any] = {
        "batch_manifest_schema": SUBSCRIPTION_REVIEW_BATCH_MANIFEST_SCHEMA,
        "campaign_id": campaign_id,
        "protocol_version": CONSENSUS_PROTOCOL_VERSION,
        "sampling_manifest_digest": sampling.manifest_digest(),
        "source_packet_digest": source_packet_digest,
        "neutral_packet_sha256": packet_sha,
        "prompt_digest": labeling_instructions_digest(),
        "reviewer_instructions_version": REVIEWER_INSTRUCTIONS_VERSION,
        "max_cases": max_cases,
        "max_serialized_bytes": max_serialized_bytes,
        "batches": [],
    }
    for index, group in enumerate(groups, start=1):
        payload = serialize_prompt(
            _build_batch_prompt(
                batch_id=batch_id_for_index(campaign_id, index),
                campaign_id=campaign_id,
                cases=group,
                instructions=LABELING_INSTRUCTIONS,
                label_guide_version=LABEL_GUIDE_VERSION,
                reviewer_instructions_version=REVIEWER_INSTRUCTIONS_VERSION,
            )
        )
        manifest["batches"].append(
            {
                "batch_id": batch_id_for_index(campaign_id, index),
                "index": index,
                "case_count": len(group),
                "sample_ids": [str(case["sample_id"]) for case in group],
                "prompt_sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    manifest_payload = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()

    # Per-lane request batches: the mechanical prerequisite for batch
    # import — every ingested record must bind to an ACTUAL emitted request
    # item (#206 FIX-R4-1, unchanged). The logical batches are lane-neutral;
    # each lane still emits its own canonical request JSONL generation, and
    # batch import binds each case to that lane's emitted request.
    existing_manifest = batch_manifest_path(protected_root)
    if existing_manifest.exists():
        # Re-export must be byte-identical (determinism proof), else fail.
        if existing_manifest.read_bytes() != manifest_payload:
            raise ValueError("subscription_batch_manifest_conflict_not_deterministic")
        for entry in manifest["batches"]:
            path = batch_prompt_path(protected_root, str(entry["batch_id"]))
            if not hmac.compare_digest(
                hashlib.sha256(path.read_bytes()).hexdigest(), str(entry["prompt_sha256"])
            ):
                raise ValueError("subscription_batch_prompt_conflict_not_deterministic")
        return load_batch_manifest_summary(manifest)

    for entry, group in zip(manifest["batches"], groups, strict=True):
        write_protected_file(
            batch_prompt_path(protected_root, str(entry["batch_id"])),
            serialize_prompt(
                _build_batch_prompt(
                    batch_id=str(entry["batch_id"]),
                    campaign_id=campaign_id,
                    cases=group,
                    instructions=LABELING_INSTRUCTIONS,
                    label_guide_version=LABEL_GUIDE_VERSION,
                    reviewer_instructions_version=REVIEWER_INSTRUCTIONS_VERSION,
                )
            ),
        )
    write_protected_file(existing_manifest, manifest_payload)
    return load_batch_manifest_summary(manifest)


def load_batch_manifest_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    """Public-safe summary (no sample IDs) of an in-memory batch manifest."""
    """Public-safe summary (no sample IDs) of an in-memory batch manifest."""
    return {
        "batch_count": len(manifest["batches"]),
        "total_cases": sum(int(entry["case_count"]) for entry in manifest["batches"]),
        "batches": [
            {
                "batch_id": str(entry["batch_id"]),
                "index": int(entry["index"]),
                "case_count": int(entry["case_count"]),
                "prompt_sha256": str(entry["prompt_sha256"]),
            }
            for entry in manifest["batches"]
        ],
    }


def verify_review_batches(
    protected_root: Path,
    *,
    sampling: SamplingManifest | None = None,
) -> dict[str, Any]:
    """Canonically verify the retained batch manifest + prompt bytes.

    Checks: manifest schema/campaign opt-in, contiguous indexes, each prompt
    file exists and hashes to its recorded digest, each prompt parses and its
    embedded batch_id/case order match the manifest, no duplicate sample
    across batches, and (when ``sampling`` is supplied) the batches partition
    EXACTLY the frozen sample membership in frozen order. Returns the
    verified manifest.
    """
    path = batch_manifest_path(protected_root)
    if not path.is_file():
        raise ValueError("subscription_batches_not_exported")
    manifest: dict[str, Any] = json.loads(path.read_text())
    if manifest.get("batch_manifest_schema") != SUBSCRIPTION_REVIEW_BATCH_MANIFEST_SCHEMA:
        raise ValueError("subscription_batch_manifest_schema_mismatch")
    if manifest.get("protocol_version") != CONSENSUS_PROTOCOL_VERSION:
        raise ValueError("subscription_batch_manifest_protocol_mismatch")
    if not subscription_mode_permitted(
        str(manifest.get("campaign_id", "")), str(manifest.get("protocol_version", ""))
    ):
        raise ValueError("subscription_ui_mode_not_opted_in_for_campaign")
    if (
        sampling is not None
        and manifest.get("sampling_manifest_digest") != sampling.manifest_digest()
    ):
        raise ValueError("subscription_batch_sampling_manifest_mismatch")
    entries = manifest.get("batches")
    if not isinstance(entries, list) or not entries:
        raise ValueError("subscription_batch_manifest_empty")
    all_ids: list[str] = []
    for position, entry in enumerate(entries, start=1):
        if int(entry["index"]) != position:
            raise ValueError("subscription_batch_index_not_contiguous")
        prompt_path = batch_prompt_path(protected_root, str(entry["batch_id"]))
        if not prompt_path.is_file():
            raise ValueError("subscription_batch_prompt_missing")
        payload = prompt_path.read_bytes()
        if not hmac.compare_digest(
            hashlib.sha256(payload).hexdigest(), str(entry["prompt_sha256"])
        ):
            raise ValueError("subscription_batch_prompt_digest_mismatch")
        prompt = json.loads(payload)
        if prompt.get("batch_schema") != SUBSCRIPTION_REVIEW_BATCH_SCHEMA:
            raise ValueError("subscription_batch_prompt_schema_mismatch")
        if prompt.get("batch_id") != entry["batch_id"]:
            raise ValueError("subscription_batch_prompt_id_mismatch")
        prompt_ids = [str(case["sample_id"]) for case in prompt.get("cases", [])]
        if prompt_ids != [str(sid) for sid in entry["sample_ids"]]:
            raise ValueError("subscription_batch_prompt_membership_mismatch")
        if len(set(prompt_ids)) != len(prompt_ids):
            raise ValueError("subscription_batch_duplicate_sample_id")
        all_ids.extend(prompt_ids)
    if len(set(all_ids)) != len(all_ids):
        raise ValueError("subscription_batch_duplicate_sample_id")
    if sampling is not None and all_ids != list(sampling.sample_ids):
        raise ValueError("subscription_batch_partition_mismatch")
    return manifest


# --- Batch response import ------------------------------------------------------


def raw_batch_path(lane_root: Path, batch_id: str) -> Path:
    return lane_root / "raw-batches" / f"{batch_id}.resp"


def attestation_path(lane_root: Path, batch_id: str) -> Path:
    return lane_root / "subscription-attestations" / f"{batch_id}.json"


def _parse_batch_response(raw: bytes, expected_ids: list[str]) -> list[dict[str, Any]]:
    """Strictly parse the copied batch response; refuse any membership drift."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("subscription_batch_response_unparseable") from exc
    if not isinstance(payload, dict):
        raise ValueError("subscription_batch_response_must_be_object")
    unknown_keys = set(payload) - {"results"}
    if unknown_keys:
        raise ValueError(
            "subscription_batch_response_extra_keys:" + ",".join(sorted(unknown_keys))
        )
    results = payload.get("results")
    if not isinstance(results, list):
        raise ValueError("subscription_batch_response_requires_results_array")
    if len(results) != len(expected_ids):
        raise ValueError("subscription_batch_response_case_count_mismatch")
    seen: set[str] = set()
    parsed: list[dict[str, Any]] = []
    for position, (result, expected) in enumerate(zip(results, expected_ids, strict=True)):
        if not isinstance(result, dict):
            raise ValueError(f"subscription_batch_result_not_object:{position}")
        sample_id = result.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id:
            raise ValueError(f"subscription_batch_result_requires_sample_id:{position}")
        if sample_id in seen:
            raise ValueError(f"subscription_batch_duplicate_result:{sample_id}")
        seen.add(sample_id)
        if sample_id != expected:
            raise ValueError(f"subscription_batch_result_out_of_order_or_unknown:{sample_id}")
        extra = set(result) - set(BATCH_RESULT_FIELDS)
        if extra:
            raise ValueError(
                f"subscription_batch_result_extra_fields:{sample_id}:"
                + ",".join(sorted(extra))
            )
        missing = set(BATCH_RESULT_FIELDS) - set(result)
        if missing:
            raise ValueError(
                f"subscription_batch_result_missing_fields:{sample_id}:"
                + ",".join(sorted(missing))
            )
        parsed.append(result)
    return parsed


def _result_envelope(result: dict[str, Any]) -> dict[str, Any]:
    """Deterministically project one flat batch result into the frozen #206
    per-case response envelope (the shape the frozen parser consumes)."""
    return {
        "sample_id": str(result["sample_id"]),
        "outcome": "judged",
        "judgment": {
            "fields": {
                "expected_kind": result["expected_kind"],
                "retention_value": result["retention_value"],
                "epistemic_state": result["epistemic_state"],
                "consequence": result["consequence"],
                "acceptable_abstention": result["acceptable_abstention"],
            },
            "reviewer_confidence": result["reviewer_confidence"],
        },
    }


def _lane_records_bound_to_batch(session: LaneSessionAlias, batch_id: str) -> set[str]:
    """Sample IDs whose accepted records attest to this exact batch."""
    from evals.calibration.model_lanes import load_lane_records

    manifest = json.loads(batch_manifest_path(session.protected_root).read_text())
    entry = next(
        (e for e in manifest["batches"] if str(e["batch_id"]) == batch_id), None
    )
    if entry is None:
        raise ValueError("subscription_batch_not_in_canonical_manifest")
    batch_digest = str(entry["prompt_sha256"])
    bound: set[str] = set()
    for sample_id, record in load_lane_records(
        session.protected_root, session.reviewer.reviewer_slot
    ).items():
        execution = record.execution
        payload = getattr(execution, "subscription_attestation", None) if execution else None
        if isinstance(payload, dict) and hmac.compare_digest(
            str(payload.get("request_batch_digest", "")), batch_digest
        ):
            bound.add(sample_id)
    return bound


def import_batch_response(
    protected_root: Path,
    *,
    reviewer_slot: str,
    batch_id: str,
    raw_response: str,
    service: str,
    user_visible_model_name: str,
    operator_reference: str,
    sampling: SamplingManifest,
    attested_at: str | None = None,
    conversation_reference: str | None = None,
    retry_mechanical_failure: bool = False,
) -> dict[str, Any]:
    """Ingest one verbatim subscription-UI batch response into one lane.

    Mechanical, fail-closed, resume-safe. The maintainer supplies the exact
    raw response copied from the service plus the attestation fields; this
    function verifies the batch is a real canonical emitted batch, binds the
    attestation to the exact request batch and response bytes, preserves both
    under protected lane evidence, deterministically derives per-case
    records through the unchanged #206 ingestion path, and refuses
    missing/extra/duplicate/out-of-order results and cross-lane/cross-batch
    replay. No manual per-case JSON construction anywhere.
    """
    from evals.calibration.ingestion import LaneSession, lane_provenance_mode
    from evals.calibration.review import write_protected_file

    if not isinstance(raw_response, str) or not raw_response:
        raise ValueError("subscription_import_requires_raw_response_text")
    if batch_id != batch_id.strip() or not _BATCH_ID_PATTERN.fullmatch(batch_id):
        raise ValueError("subscription_batch_id_not_canonical")
    manifest = verify_review_batches(protected_root, sampling=sampling)
    entry = next((e for e in manifest["batches"] if str(e["batch_id"]) == batch_id), None)
    if entry is None:
        raise ValueError("subscription_batch_not_in_canonical_manifest")
    expected_ids = [str(sid) for sid in entry["sample_ids"]]
    raw_bytes = raw_response.encode()
    raw_digest = hashlib.sha256(raw_bytes).hexdigest()
    batch_digest = str(entry["prompt_sha256"])

    session = LaneSession(protected_root, reviewer_slot)
    if lane_provenance_mode(session.lane_root) != SUBSCRIPTION_PROVENANCE_MODE:
        raise ValueError("lane_not_in_subscription_mode")
    campaign_id = session.campaign_id

    # Cross-lane / cross-batch replay (#209): the same response bytes can
    # never be attested into two different lanes or two different logical
    # batches. Independently-produced frontier responses echoing 402 frozen
    # sample IDs are never byte-identical; identical bytes mean replay (or
    # cross-service contamination), and both are refused. Failed-attempt
    # evidence (".failed-N") is retained, not live, and excluded.
    for other_slot in REVIEWER_SLOTS:
        other_raw_dir = protected_root / "lanes" / other_slot / "raw-batches"
        if not other_raw_dir.is_dir():
            continue
        for existing in sorted(other_raw_dir.glob("*.resp")):
            if ".failed-" in existing.name:
                continue
            same_live_slot = other_slot == reviewer_slot
            if same_live_slot and existing.stem == batch_id:
                continue  # idempotent resupply of this exact batch/lane
            if hmac.compare_digest(
                hashlib.sha256(existing.read_bytes()).hexdigest(), raw_digest
            ):
                raise ValueError(
                    "subscription_batch_response_replay_refused:"
                    f"{other_slot}:{existing.stem}"
                )

    # Strict membership/order/shape validation BEFORE any evidence is
    # preserved: an unparseable or drifting response leaves no artifacts.
    results = _parse_batch_response(raw_bytes, expected_ids)

    # Preserve the exact copied bytes FIRST (interruption-safe, mirroring the
    # per-case raw path): a crash can leave an orphan batch file, never an
    # unbacked record. A conflicting re-supply is permitted only as an
    # explicit mechanical-failure retry with NO accepted record bound to the
    # failed attempt (the failed bytes are retained as evidence).
    raw_path = raw_batch_path(session.lane_root, batch_id)
    if raw_path.exists():
        existing_digest = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        if existing_digest != raw_digest:
            if not retry_mechanical_failure:
                raise ValueError("subscription_batch_raw_conflict_retry_required")
            if _lane_records_bound_to_batch(session, batch_id):
                raise ValueError("subscription_batch_retry_has_accepted_records")
            counter = 1
            failed = session.lane_root / "raw-batches" / f"{batch_id}.failed-{counter}.resp"
            while failed.exists():
                counter += 1
                failed = session.lane_root / "raw-batches" / f"{batch_id}.failed-{counter}.resp"
            raw_path.rename(failed)
    if not raw_path.exists():
        write_protected_file(raw_path, raw_bytes)

    attestation = None
    att_path = attestation_path(session.lane_root, batch_id)
    if att_path.exists():
        # Same batch, same raw bytes: the preserved attestation IS the
        # binding for this evidence — reuse it verbatim (a re-attested
        # copy would only differ by timestamp, which is not new evidence).
        preserved_attestation = SubscriptionReviewAttestation.model_validate(
            json.loads(att_path.read_text())
        )
        if preserved_attestation.raw_response_digest != raw_digest or (
            preserved_attestation.request_batch_digest != batch_digest
        ):
            raise ValueError("subscription_attestation_conflict")
        attestation = preserved_attestation
    else:
        attestation = build_attestation(
            campaign_id=campaign_id,
            reviewer_slot=reviewer_slot,
            service=service,
            user_visible_model_name=user_visible_model_name,
            operator_reference=operator_reference,
            request_batch_digest=batch_digest,
            raw_response_digest=raw_digest,
            attested_at=attested_at,
            conversation_reference=conversation_reference,
        )
        write_protected_file(
            att_path,
            (json.dumps(attestation.payload(), sort_keys=True) + "\n").encode(),
        )

    accepted = 0
    resumed = 0
    errors: list[dict[str, Any]] = []
    for result in results:
        sample_id = str(result["sample_id"])
        try:
            receipt = observe_subscription_execution(
                session.lane_root,
                campaign_id=campaign_id,
                sample_id=sample_id,
                reviewer_slot=reviewer_slot,
                subscription_attestation=attestation,
                executor_identity=f"operator:{operator_reference}",
            )
            envelope = _result_envelope(result)
            session.ingest_response(
                {
                    "sample_id": sample_id,
                    "execution": receipt.model_dump(mode="json"),
                    "raw_response": json.dumps(envelope, sort_keys=True),
                },
                sampling=sampling,
            )
        except ValueError as exc:
            message = str(exc)
            if "review_record_already_accepted" in message:
                resumed += 1
                continue
            errors.append({"sample_id": sample_id, "error": message})
            continue
        accepted += 1
    return {
        "batch_id": batch_id,
        "reviewer_slot": reviewer_slot,
        "cases": len(results),
        "accepted": accepted,
        "resumed_duplicates": resumed,
        "errors": errors,
        "raw_response_digest": raw_digest,
        "request_batch_digest": batch_digest,
        "attestation_digest": attestation.attestation_digest,
    }


def observe_subscription_execution(
    lane_root: Path,
    *,
    campaign_id: str,
    sample_id: str,
    reviewer_slot: str,
    subscription_attestation: SubscriptionReviewAttestation,
    executor_identity: str,
    executed_at: datetime | None = None,
) -> ReceiptAlias:
    """Build a truthful subscription-UI execution receipt for one case.

    Mirrors ``observe_execution`` (FIX-R5-1 shape): only the emitted-request
    binding is looked up from the retained VERIFIED request batches; the
    ATTESTED actual identity is the operator's attestation (slot/family from
    the attestation; model/config/prompt digests from the frozen request
    line the lane emitted). Ingestion compares this observed identity
    EXACTLY against the frozen lane identity — a mismatch fails closed,
    exactly as on the machine path.
    """
    from evals.calibration.consensus import ExecutionEvidence, ExecutionReceipt
    from evals.calibration.ingestion import _load_request_registry

    if reviewer_slot not in REVIEWER_SLOTS:
        raise ValueError("unknown_reviewer_slot")
    if subscription_attestation.reviewer_slot != reviewer_slot:
        raise ValueError("subscription_attestation_slot_mismatch")
    registry = _load_request_registry(lane_root)
    generations = registry.get(sample_id)
    if not generations:
        raise ValueError("response_request_not_emitted")
    request_generation = max(generations)
    _item_digest, _identity, _packet, request_line = generations[request_generation]
    line = request_line if isinstance(request_line, dict) else {}
    evidence = ExecutionEvidence(
        campaign_id=campaign_id,
        actual_reviewer_slot=reviewer_slot,  # type: ignore[arg-type]
        actual_reviewer_family=FAMILY_BY_SLOT[reviewer_slot],
        actual_provider_model_identifier=str(line.get("provider_model_identifier", "")),
        actual_configuration_digest=str(line.get("reviewer_config_digest", "")),
        actual_prompt_digest=str(line.get("prompt_digest", "")),
        request_generation=request_generation,
        request_item_digest=_item_digest,
        executed_at=executed_at or datetime.now(UTC),
        executor_status="completed",
        executor_identity=executor_identity,
        identity_source=SUBSCRIPTION_PROVENANCE_MODE,
        provider_request_id=None,
        provider_response_id=None,
        provider_metadata=None,
        subscription_attestation=subscription_attestation.payload(),
    )
    return ExecutionReceipt.from_evidence(evidence)


# --- Maintainer handoff: next outstanding logical batch ------------------------


def next_outstanding_batch(
    protected_root: Path,
    *,
    sampling: SamplingManifest,
    reviewer_slots: tuple[str, ...] | None = None,
) -> dict[str, Any] | None:
    """The next logical batch not yet fully ingested across every lane.

    #208 handoff behavior: Hermes presents this batch's ID, case count, and
    protected digest, prints the exact paste-ready prompt, and stops. A
    batch is outstanding until EVERY reviewer lane has an accepted record
    for EVERY case in the batch (per-lane gaps are visible in ``lane_gaps``).
    """
    from evals.calibration.model_lanes import load_lane_records

    slots = tuple(reviewer_slots or REVIEWER_SLOTS)
    manifest = verify_review_batches(protected_root, sampling=sampling)
    records = {slot: load_lane_records(protected_root, slot) for slot in slots}
    for entry in sorted(manifest["batches"], key=lambda e: int(e["index"])):
        ids = [str(sid) for sid in entry["sample_ids"]]
        lane_gaps = {
            slot: sum(1 for sid in ids if sid not in records[slot]) for slot in slots
        }
        if any(lane_gaps.values()):
            return {
                "batch_id": str(entry["batch_id"]),
                "case_count": int(entry["case_count"]),
                "prompt_sha256": str(entry["prompt_sha256"]),
                "lane_gaps": lane_gaps,
            }
    return None


# --- Lane-freeze provenance requirement -----------------------------------------


def require_subscription_attested_identity(
    records: Mapping[str, ModelReviewRecord],
    *,
    lane_root: Path,
    protected_root: Path,
    sampling: SamplingManifest | None = None,
) -> None:
    """The subscription-mode replacement for the machine-verified freeze gate.

    Every accepted record must carry ``identity_source ==
    operator_attested_subscription_ui`` with a digest-bound attestation that
    still agrees with the PRESERVED per-batch evidence: the attestation file
    must exist and equal the embedded payload, the preserved raw batch bytes
    must still hash to the attestation's raw-response digest, the batch must
    still be a canonically verified emitted batch, and the campaign opt-in
    must still hold. Mixing provenance modes inside one lane is refused, and
    a generic ``executor_attestation`` record can NEVER satisfy this gate.
    """
    manifest = verify_review_batches(protected_root, sampling=sampling)
    entries_by_digest = {
        str(entry["prompt_sha256"]): str(entry["batch_id"]) for entry in manifest["batches"]
    }
    for sample_id in sorted(records):
        record = records[sample_id]
        execution = record.execution
        if execution is None:
            raise ValueError(f"subscription_lane_requires_execution_receipt:{sample_id}")
        source = execution.identity_source
        if source == "executor_attestation":
            raise ValueError(
                f"subscription_lane_rejects_generic_executor_attestation:{sample_id}"
            )
        if source == "provider_metadata":
            raise ValueError(f"subscription_lane_refuses_mixed_provenance:{sample_id}")
        if source != SUBSCRIPTION_PROVENANCE_MODE:
            raise ValueError(f"subscription_lane_unknown_provenance_source:{sample_id}")
        payload = getattr(execution, "subscription_attestation", None)
        if not isinstance(payload, dict) or not payload:
            raise ValueError(f"subscription_lane_missing_embedded_attestation:{sample_id}")
        attestation = SubscriptionReviewAttestation.model_validate(payload)
        batch_id = entries_by_digest.get(attestation.request_batch_digest)
        if batch_id is None:
            raise ValueError(f"subscription_attestation_batch_not_canonical:{sample_id}")
        preserved = attestation_path(lane_root, batch_id)
        if not preserved.is_file():
            raise ValueError(
                f"subscription_attestation_missing_preserved_artifact:{sample_id}"
            )
        if json.loads(preserved.read_text()) != payload:
            raise ValueError(
                f"subscription_attestation_disagrees_with_preserved:{sample_id}"
            )
        raw = raw_batch_path(lane_root, batch_id)
        if not raw.is_file():
            raise ValueError(f"subscription_batch_raw_evidence_missing:{sample_id}")
        if not hmac.compare_digest(
            hashlib.sha256(raw.read_bytes()).hexdigest(), attestation.raw_response_digest
        ):
            raise ValueError(
                f"subscription_batch_raw_evidence_digest_mismatch:{sample_id}"
            )
        if not subscription_mode_permitted(
            attestation.campaign_id, attestation.protocol_version
        ):
            raise ValueError(
                f"subscription_ui_mode_not_opted_in_for_campaign:{sample_id}"
            )
