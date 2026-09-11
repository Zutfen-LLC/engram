"""Operator-attested subscription-UI reviewer provenance (#209, round 2).

A narrow, explicitly weaker but HONEST provenance mode for the #208 frontier
review campaign: the three reviewer lanes are executed through the
maintainer's existing Claude.ai / ChatGPT / z.ai consumer subscriptions, with
the human operator attesting which service/model produced each response.

Trust boundary (frozen here, before any #208 review executes):

- Engram machine-verifies: the exact frozen neutral case evidence, the
  canonical per-lane request generations, the canonical logical paste batches
  (externally re-derived from frozen authority, never self-consistent
  manifest trust), the prompt/instruction digest, the exact copied raw
  response bytes for EVERY returned attempt (structured, refusal, malformed,
  or mechanically incomplete), parsing, lane membership, consensus, audit
  selection, and final ledger mechanics — exactly as for the machine path.
- The human operator attests, at subscription-lane INITIALIZATION and before
  any output exists, the exact user-visible selected model name/version for
  the lane (FIX-1). That frozen visible model is immutable lane authority:
  every later batch attestation must match it exactly on slot, service,
  family, and visible model name, and the frozen authority digest is bound
  into the campaign preparation record so a mid-campaign change fails closed.
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
frozen consensus protocol version, and every boundary (lane init, preparation,
batch export/verify/import, record ingestion, lane freeze/load, final ledger
verification) re-checks it.

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
import secrets
from collections.abc import Mapping, Sequence
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

SUBSCRIPTION_ATTESTATION_SCHEMA: Literal["engram-calibration-subscription-attestation-209-v2"] = (
    "engram-calibration-subscription-attestation-209-v2"
)

SUBSCRIPTION_REVIEW_BATCH_SCHEMA: Literal["engram-calibration-subscription-review-batch-209-v1"] = (
    "engram-calibration-subscription-review-batch-209-v1"
)

SUBSCRIPTION_REVIEW_BATCH_MANIFEST_SCHEMA: Literal[
    "engram-calibration-subscription-review-batch-manifest-209-v2"
] = "engram-calibration-subscription-review-batch-manifest-209-v2"

SUBSCRIPTION_LANE_AUTHORITY_SCHEMA: Literal[
    "engram-calibration-subscription-lane-authority-209-v2"
] = "engram-calibration-subscription-lane-authority-209-v2"

SUBSCRIPTION_PREPARE_SCHEMA: Literal["engram-calibration-subscription-prepare-209-v1"] = (
    "engram-calibration-subscription-prepare-209-v1"
)

SUBSCRIPTION_BATCH_ATTEMPT_SCHEMA: Literal[
    "engram-calibration-subscription-batch-attempt-209-v2"
] = "engram-calibration-subscription-batch-attempt-209-v2"

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
#: user-visible display name is frozen SEPARATELY per lane at initialization
#: (``SubscriptionLaneAuthority.user_visible_model_name``, FIX-1); THIS value
#: is what the frozen ``ReviewerIdentity`` binds so record/lane identity
#: comparison stays exact and stable. It names the family, not a
#: cryptographically proven model version — never promotable to a
#: provider-metadata claim.
SUBSCRIPTION_MODEL_BY_SLOT: dict[str, str] = {
    "model_a": "claude-opus@claude-ai-subscription-ui",
    "model_b": "gpt-astra@chatgpt-subscription-ui",
    "model_c": "glm-5-3-max@z-ai-subscription-ui",
}

#: FIX-1 (round 3): frozen visible-model-family identification rules per
#: asserted reviewer family. A user-visible model label may only freeze a
#: lane when it POSITIVELY identifies the requested family: every required
#: token must be present (word-separated) and/or every required substring in
#: the punctuation-stripped join, and no visibly different family marker for
#: the same provider may appear. Anything else fails CLOSED — the family is
#: never inferred from the reviewer slot alone.
#:
#: Rule tuple: (required word tokens, required substrings in the
#: punctuation-stripped ordered join, forbidden word tokens, forbidden
#: substrings). For this frozen campaign the three requested families are
#: explicitly enumerated; an unmapped family is itself a fail-closed error.
_FamilyRule = tuple[frozenset[str], tuple[str, ...], frozenset[str], tuple[str, ...]]

VISIBLE_MODEL_FAMILY_RULES: dict[str, _FamilyRule] = {
    "claude-opus": (
        frozenset({"opus"}),
        (),
        frozenset({"sonnet", "haiku", "instant"}),
        (),
    ),
    "gpt-astra": (
        frozenset({"astra"}),
        (),
        frozenset({"4o", "o1", "o3", "o4"}),
        ("gpt3", "gpt4", "gpt5"),
    ),
    "glm-5-3-max": (
        frozenset({"max"}),
        ("glm", "53"),
        frozenset({"air", "flash", "lite"}),
        (),
    ),
}


def _visible_label_tokens(name: str) -> tuple[frozenset[str], str]:
    """Normalize a visible label: lowercase, punctuation -> separators.

    Returns (word tokens, punctuation-stripped ordered join). ``Claude Opus
    4.8`` -> (``{claude, opus, 4, 8}``, ``"claudeopus48"``); ``GLM-5.3-Max``
    -> (``{glm, 5, 3, max}``, ``"glm53max"``).
    """
    lowered = name.lower()
    parts = [part for part in re.split(r"[^a-z0-9]+", lowered) if part]
    return frozenset(parts), "".join(parts)


def validate_visible_model_family(reviewer_family: str, user_visible_model_name: str) -> None:
    """FIX-1 (round 3): fail closed unless the user-visible model label
    POSITIVELY identifies the frozen slot/family.

    Wrong-family labels (``model_a`` + ``Claude Sonnet ...``), visibly
    different variants of the same provider (Haiku, GPT-4o, GLM-5-Air), and
    labels that cannot mechanically establish any family all STOP
    initialization with an explicit error — before any output exists. The
    exact original visible string is preserved unchanged in the frozen
    authority after validation.
    """
    rule = VISIBLE_MODEL_FAMILY_RULES.get(reviewer_family)
    if rule is None:
        raise ValueError(f"subscription_visible_model_family_unknown:{reviewer_family}")
    required_tokens, required_substrings, forbidden_tokens, forbidden_substrings = rule
    tokens, joined = _visible_label_tokens(user_visible_model_name)
    if any(token in tokens for token in forbidden_tokens) or any(
        fragment in joined for fragment in forbidden_substrings
    ):
        raise ValueError(f"subscription_visible_model_family_conflict:{reviewer_family}")
    missing_tokens = [token for token in sorted(required_tokens) if token not in tokens]
    missing_substrings = [frag for frag in required_substrings if frag not in joined]
    if missing_tokens or missing_substrings:
        raise ValueError(f"subscription_visible_model_family_not_established:{reviewer_family}")


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

#: Frozen canonical response contract for the logical paste batches. This is
#: the ONLY contract a paste-ready prompt may carry: it is compared by exact
#: object equality against every reconstructed prompt at every verification
#: boundary, so a rewritten prompt with different response semantics can
#: never become its own authority (FIX-2).
CANONICAL_RESPONSE_CONTRACT: dict[str, Any] = {
    "respond_with": (
        "ONE strict JSON object and nothing else — no markdown fences, no prose before or after."
    ),
    "shape": {
        "results": ("array with EXACTLY one result object per supplied case, in the supplied order")
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
        "unknown is a legitimate, protected answer on every field; never guess to avoid it.",
    ],
}

AttemptOutcome = Literal[
    "completed_structured",
    "substantive_refusal",
    "substantive_malformed",
    "mechanically_incomplete",
]

_BATCH_ID_PATTERN = re.compile(r"[A-Za-z0-9:_\-.]+")
_ERROR_CODE_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


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


# --- SubscriptionLaneAuthority (FIX-1) -----------------------------------------


def subscription_lane_authority_path(lane_root: Path) -> Path:
    return lane_root / "subscription-authority.json"


def _authority_digest_payload(payload: Mapping[str, Any]) -> str:
    return digest_of({k: v for k, v in payload.items() if k != "authority_digest"})


class SubscriptionLaneAuthority(Record):
    """FIX-1: the frozen visible-model lane authority.

    Required and frozen at subscription-lane initialization — BEFORE any
    output exists: reviewer slot, closed-vocabulary service, asserted
    reviewer family, the exact user-visible selected model name/version, the
    operator reference, and the frozen lane identity digest. The exact
    visible model label chosen by the operator is immutable lane authority
    for the duration of the campaign; the self-digest is bound into the
    campaign preparation record, so changing the selected model after
    preparation (i.e. after any output could exist) fails closed at every
    verification boundary rather than silently accepting a new display name.
    """

    lane_authority_schema: Literal["engram-calibration-subscription-lane-authority-209-v2"] = (
        SUBSCRIPTION_LANE_AUTHORITY_SCHEMA
    )
    campaign_id: str
    protocol_version: str
    reviewer_slot: str
    reviewer_family: str
    service: str
    user_visible_model_name: str
    operator_reference: str
    frozen_at: str
    lane_identity_digest: str
    authority_digest: str

    @model_validator(mode="after")
    def authority_contract(self) -> Self:
        if self.protocol_version != CONSENSUS_PROTOCOL_VERSION:
            raise ValueError("protocol_version_mismatch")
        if self.reviewer_slot not in REVIEWER_SLOTS:
            raise ValueError("unknown_reviewer_slot")
        # The service/family/slot mapping is frozen, not caller choice.
        if FAMILY_BY_SLOT[self.reviewer_slot] != self.reviewer_family:
            raise ValueError("subscription_authority_family_does_not_match_slot")
        if SERVICE_BY_SLOT[self.reviewer_slot] != self.service:
            raise ValueError("subscription_authority_service_does_not_match_slot")
        if not self.user_visible_model_name:
            raise ValueError("subscription_authority_requires_user_visible_model_name")
        # FIX-1 (round 3): the visible label must POSITIVELY identify the
        # frozen family — a non-empty wrong-family label (Sonnet/Haiku for
        # an Opus lane, another GLM variant for a Max lane) fails closed
        # here, at initialization, before any output exists.
        validate_visible_model_family(self.reviewer_family, self.user_visible_model_name)
        if not self.operator_reference:
            raise ValueError("subscription_authority_requires_operator_reference")
        if not self.frozen_at:
            raise ValueError("subscription_authority_requires_frozen_at")
        if len(self.lane_identity_digest) != 64:
            raise ValueError("subscription_authority_requires_lane_identity_digest")
        if not hmac.compare_digest(
            _authority_digest_payload(self.model_dump(mode="json")), self.authority_digest
        ):
            raise ValueError("subscription_authority_digest_mismatch")
        if not subscription_mode_permitted(self.campaign_id, self.protocol_version):
            raise ValueError("subscription_ui_mode_not_opted_in_for_campaign")
        return self

    def payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def build_subscription_lane_authority(
    *,
    campaign_id: str,
    reviewer_slot: str,
    user_visible_model_name: str,
    operator_reference: str,
    lane_identity_digest: str,
    frozen_at: str | None = None,
) -> SubscriptionLaneAuthority:
    """Construct the digest-bound frozen visible-model lane authority."""
    if reviewer_slot not in REVIEWER_SLOTS:
        raise ValueError("unknown_reviewer_slot")
    # FIX-1 (round 3): positive family identification at freeze time —
    # validate BEFORE constructing/freeze any authority bytes.
    validate_visible_model_family(FAMILY_BY_SLOT[reviewer_slot], user_visible_model_name)
    payload: dict[str, Any] = {
        "lane_authority_schema": SUBSCRIPTION_LANE_AUTHORITY_SCHEMA,
        "campaign_id": campaign_id,
        "protocol_version": CONSENSUS_PROTOCOL_VERSION,
        "reviewer_slot": reviewer_slot,
        "reviewer_family": FAMILY_BY_SLOT[reviewer_slot],
        "service": SERVICE_BY_SLOT[reviewer_slot],
        "user_visible_model_name": user_visible_model_name,
        "operator_reference": operator_reference,
        "frozen_at": frozen_at or datetime.now(UTC).isoformat(),
        "lane_identity_digest": lane_identity_digest,
    }
    payload["authority_digest"] = _authority_digest_payload(payload)
    return SubscriptionLaneAuthority.model_validate(payload)


def load_subscription_lane_authority(lane_root: Path) -> SubscriptionLaneAuthority:
    """Load + fully validate the frozen visible-model lane authority."""
    path = subscription_lane_authority_path(lane_root)
    if not path.is_file():
        raise ValueError("subscription_lane_authority_missing")
    return SubscriptionLaneAuthority.model_validate(json.loads(path.read_text()))


def write_subscription_lane_authority(
    lane_root: Path, authority: SubscriptionLaneAuthority
) -> None:
    from evals.calibration.review import write_protected_file

    write_protected_file(
        subscription_lane_authority_path(lane_root),
        (json.dumps(authority.payload(), sort_keys=True, indent=2) + "\n").encode(),
    )


def require_lane_visible_model_authority(
    lane_root: Path, attestation: SubscriptionReviewAttestation
) -> SubscriptionLaneAuthority:
    """FIX-1 enforcement point: an attestation may only ever carry the lane's
    FROZEN visible-model authority. Slot, service, reviewer family, and the
    exact user-visible model name must equal the frozen lane authority —
    never merely the slot-derived defaults."""
    authority = load_subscription_lane_authority(lane_root)
    if attestation.reviewer_slot != authority.reviewer_slot:
        raise ValueError("subscription_attestation_slot_mismatch")
    if attestation.service != authority.service:
        raise ValueError("subscription_attestation_service_mismatch")
    if attestation.reviewer_family != authority.reviewer_family:
        raise ValueError("subscription_attestation_family_mismatch")
    if attestation.user_visible_model_name != authority.user_visible_model_name:
        raise ValueError("subscription_attestation_visible_model_mismatch")
    if attestation.operator_reference != authority.operator_reference:
        raise ValueError("subscription_attestation_operator_mismatch")
    return authority


# --- SubscriptionReviewAttestation ---------------------------------------------


def _attestation_digest_payload(payload: Mapping[str, Any]) -> str:
    return digest_of({k: v for k, v in payload.items() if k != "attestation_digest"})


class SubscriptionReviewAttestation(Record):
    """Protected operator attestation for ONE logical batch attempt response.

    The operator attests: this exact raw response (digest-bound) to this
    exact canonical emitted batch (digest-bound) was produced through the
    named consumer subscription service, with the FROZEN lane-visible model
    selected, in this reviewer slot. Engram machine-verifies the digests and
    the frozen slot/service/family mapping; the service/model identity itself
    is operator-attested, never claimed as machine-verified. The
    service/family/model/operator fields can only enter through the frozen
    lane authority (see ``build_attestation``), so a logically contradictory
    attestation (slot model_a, service claude_ai, family claude-opus, visible
    model "Claude Sonnet ...") cannot be constructed through any production
    path and fails validation against the frozen lane at every gate.
    """

    attestation_schema: Literal["engram-calibration-subscription-attestation-209-v2"] = (
        SUBSCRIPTION_ATTESTATION_SCHEMA
    )
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
    lane_authority: SubscriptionLaneAuthority,
    request_batch_digest: str,
    raw_response_digest: str,
    attested_at: str | None = None,
    conversation_reference: str | None = None,
) -> SubscriptionReviewAttestation:
    """Construct a digest-bound attestation (the only production path).

    FIX-1: the service, reviewer family, user-visible model name, and
    operator reference are sourced EXCLUSIVELY from the frozen lane
    authority — never from import-time caller claims and never inferred from
    the slot alone.
    """
    if reviewer_slot not in REVIEWER_SLOTS:
        raise ValueError("unknown_reviewer_slot")
    if lane_authority.reviewer_slot != reviewer_slot:
        raise ValueError("subscription_attestation_slot_mismatch")
    if lane_authority.campaign_id != campaign_id:
        raise ValueError("subscription_attestation_campaign_mismatch")
    payload: dict[str, Any] = {
        "attestation_schema": SUBSCRIPTION_ATTESTATION_SCHEMA,
        "campaign_id": campaign_id,
        "protocol_version": CONSENSUS_PROTOCOL_VERSION,
        "reviewer_slot": reviewer_slot,
        "reviewer_family": lane_authority.reviewer_family,
        "service": lane_authority.service,
        "user_visible_model_name": lane_authority.user_visible_model_name,
        "operator_reference": lane_authority.operator_reference,
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
    if not subscription_mode_permitted(attestation.campaign_id, attestation.protocol_version):
        raise ValueError("subscription_ui_mode_not_opted_in_for_campaign")


# --- Deterministic logical review-batch export (FIX-2) --------------------------


def batches_directory(protected_root: Path) -> Path:
    return protected_root / "batches"


def batch_manifest_path(protected_root: Path) -> Path:
    return batches_directory(protected_root) / "manifest.json"


def batch_prompt_path(protected_root: Path, batch_id: str) -> Path:
    return batches_directory(protected_root) / f"{batch_id}.json"


def prepare_record_path(protected_root: Path) -> Path:
    return protected_root / "subscription-prepare.json"


def batch_id_for_index(campaign_id: str, index: int) -> str:
    return f"{campaign_id}:sub-review-{index:03d}"


def _build_batch_prompt(
    *,
    batch_id: str,
    campaign_id: str,
    cases: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """One neutral paste-ready prompt, identical across all three lanes.

    A pure DETERMINISTIC PROJECTION of frozen external authority (FIX-2):
    the canonical #206 labeling instruction bundle, the frozen guide and
    reviewer-instruction versions, the frozen campaign/protocol identity,
    and the exact frozen neutral-packet case objects. No slot/family/
    service/model identity appears anywhere in the prompt — reviewer
    identity comes from the operator attestation at import time, never from
    telling the model which lane it is.
    """
    from evals.calibration.freeze import LABEL_GUIDE_VERSION
    from evals.calibration.ingestion import LABELING_INSTRUCTIONS
    from evals.calibration.reviewer_instructions import REVIEWER_INSTRUCTIONS_VERSION

    return {
        "batch_schema": SUBSCRIPTION_REVIEW_BATCH_SCHEMA,
        "batch_id": batch_id,
        "campaign_id": campaign_id,
        "protocol_version": CONSENSUS_PROTOCOL_VERSION,
        "label_guide_version": LABEL_GUIDE_VERSION,
        "reviewer_instructions_version": REVIEWER_INSTRUCTIONS_VERSION,
        "instructions": LABELING_INSTRUCTIONS,
        "cases": [dict(case) for case in cases],
        "response_contract": CANONICAL_RESPONSE_CONTRACT,
    }


def serialize_prompt(prompt: Mapping[str, Any]) -> bytes:
    return (json.dumps(prompt, sort_keys=True, indent=2) + "\n").encode()


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()


def logical_manifest_digest(manifest: Mapping[str, Any]) -> str:
    """Canonical digest of the logical batch manifest, excluding the digest
    itself — the shared external authority digest for all three lanes."""
    return _sha256(
        _canonical_json_bytes({k: v for k, v in manifest.items() if k != "logical_manifest_digest"})
    )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def build_logical_batches(
    *,
    campaign_id: str,
    packet: Any,
    max_cases: int = BATCH_MAX_CASES,
    max_serialized_bytes: int = BATCH_MAX_SERIALIZED_BYTES,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Deterministically partition the frozen packet into logical batches.

    FIX-4 Defect B: candidate batches are built with the ACTUAL serialized
    prompt (full instruction bundle + response contract + metadata + cases,
    serialized exactly as pasted); every emitted batch satisfies
    ``len(serialize_prompt(prompt)) <= max_serialized_bytes``. ``max_cases``
    is an independent cap. A single frozen case whose mandatory prompt
    exceeds the ceiling fails closed with an explicit single-case-oversize
    error rather than emitting an oversized prompt.

    The partition follows the packet's own frozen case order (the packet is
    byte-verified against the lane authority digest; equality with the
    sampling manifest is enforced separately wherever the sampling is
    available, so verification also works at boundaries that legitimately
    carry no sampling manifest).
    """
    if max_cases < 1 or max_serialized_bytes < 1:
        raise ValueError("subscription_batch_limits_must_be_positive")
    probe_id = batch_id_for_index(campaign_id, 0)
    groups: list[list[Mapping[str, Any]]] = []
    current: list[Mapping[str, Any]] = []
    for case in packet.cases:
        single = serialize_prompt(
            _build_batch_prompt(batch_id=probe_id, campaign_id=campaign_id, cases=[case])
        )
        if len(single) > max_serialized_bytes:
            raise ValueError(
                f"subscription_batch_single_case_exceeds_serialized_ceiling:{case['sample_id']}"
            )
        if current:
            candidate = [*current, case]
            candidate_bytes = len(
                serialize_prompt(
                    _build_batch_prompt(batch_id=probe_id, campaign_id=campaign_id, cases=candidate)
                )
            )
            if len(current) >= max_cases or candidate_bytes > max_serialized_bytes:
                groups.append(current)
                current = []
        current.append(case)
    if current:
        groups.append(current)

    prompts: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    for index, group in enumerate(groups, start=1):
        batch_id = batch_id_for_index(campaign_id, index)
        prompt = _build_batch_prompt(batch_id=batch_id, campaign_id=campaign_id, cases=group)
        payload = serialize_prompt(prompt)
        if len(payload) > max_serialized_bytes:  # pragma: no cover - probe ids same width
            raise ValueError("subscription_batch_exceeds_serialized_ceiling")
        prompts.append(prompt)
        entries.append(
            {
                "batch_id": batch_id,
                "index": index,
                "case_count": len(group),
                "sample_ids": [str(case["sample_id"]) for case in group],
                "serialized_bytes": len(payload),
                "prompt_sha256": _sha256(payload),
            }
        )
    manifest: dict[str, Any] = {
        "batch_manifest_schema": SUBSCRIPTION_REVIEW_BATCH_MANIFEST_SCHEMA,
        "campaign_id": campaign_id,
        "protocol_version": CONSENSUS_PROTOCOL_VERSION,
        "sampling_manifest_digest": str(getattr(packet, "sampling_manifest_digest", "")),
        "neutral_packet_sha256": "",
        "source_packet_digest": "",
        "prompt_digest": "",
        "label_guide_version": "",
        "reviewer_instructions_version": "",
        "max_cases": max_cases,
        "max_serialized_bytes": max_serialized_bytes,
        "max_serialized_bytes_observed": max(int(entry["serialized_bytes"]) for entry in entries),
        "batches": entries,
    }
    manifest["logical_manifest_digest"] = logical_manifest_digest(manifest)
    return manifest, prompts


def _load_frozen_packet_from_lanes(
    protected_root: Path,
    *,
    campaign_id: str,
    prepare: Mapping[str, Any],
    sampling: SamplingManifest | None,
    source_packet_digest: str | None,
    lane_slots: Sequence[str] | None,
) -> tuple[Any, str]:
    """Re-derive the frozen neutral packet from the LANES' independently
    retained bytes (FIX-2): each initialized subscription lane retains the
    byte-verified packet inside its lane root at init time; every lane must
    still agree with the prepare record's packet identity."""
    from evals.calibration.ingestion import LaneAuthority, lane_provenance_mode
    from evals.calibration.model_lanes import NeutralModelPacket

    initialized = tuple(
        slot for slot in REVIEWER_SLOTS if (protected_root / "lanes" / slot / "lane.json").is_file()
    )
    slots = tuple(lane_slots) if lane_slots is not None else initialized
    if not slots:
        raise ValueError("subscription_batches_require_initialized_subscription_lanes")
    packet: Any = None
    packet_sha = ""
    for slot in slots:
        lane_root = protected_root / "lanes" / slot
        if lane_provenance_mode(lane_root) != SUBSCRIPTION_PROVENANCE_MODE:
            raise ValueError(f"subscription_batches_lane_not_subscription_mode:{slot}")
        authority = LaneAuthority.model_validate(json.loads((lane_root / "lane.json").read_text()))
        if authority.campaign_id != campaign_id:
            raise ValueError(f"subscription_batches_lane_campaign_mismatch:{slot}")
        if authority.sampling_manifest_digest != prepare.get("sampling_manifest_digest"):
            raise ValueError(f"subscription_batches_lane_sampling_mismatch:{slot}")
        if authority.source_packet_digest != prepare.get("source_packet_digest"):
            raise ValueError(f"subscription_batches_lane_source_packet_mismatch:{slot}")
        if authority.neutral_packet_sha256 != prepare.get("neutral_packet_sha256"):
            raise ValueError(f"subscription_batches_lane_packet_mismatch:{slot}")
        retained = lane_root / "neutral-packet.json"
        if not retained.is_file():
            raise ValueError(f"lane_retained_neutral_packet_missing:{slot}")
        payload = retained.read_bytes()
        sha = _sha256(payload)
        if not hmac.compare_digest(sha, authority.neutral_packet_sha256):
            raise ValueError(f"neutral_packet_sha_mismatch:{slot}")
        if packet is None:
            packet = NeutralModelPacket.model_validate(json.loads(payload))
            packet_sha = sha
        elif not hmac.compare_digest(packet_sha, sha):
            raise ValueError(f"subscription_batches_lane_packet_disagreement:{slot}")
    if packet is None:  # pragma: no cover - defensive
        raise ValueError("subscription_batches_require_initialized_subscription_lanes")
    if packet.sampling_manifest_digest != prepare.get("sampling_manifest_digest"):
        raise ValueError("subscription_batch_sampling_manifest_mismatch")
    if sampling is not None:
        if packet.sampling_manifest_digest != sampling.manifest_digest():
            raise ValueError("subscription_batch_sampling_manifest_mismatch")
        expected_source = (
            source_packet_digest
            if source_packet_digest is not None
            else prepare.get("source_packet_digest")
        )
        if str(getattr(packet, "source_packet_digest", "")) != expected_source:
            raise ValueError("subscription_batch_source_packet_mismatch")
    return packet, packet_sha


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
    bounded by ``max_cases`` and the ACTUAL serialized-prompt ceiling. The
    manifest, prompts, and the shared logical-manifest digest are frozen in
    the campaign preparation record (FIX-2); everything is written
    exclusively under ``<protected_root>/batches/`` plus
    ``subscription-prepare.json``; a re-export must be byte-identical or it
    fails closed.
    """
    from evals.calibration.freeze import LABEL_GUIDE_VERSION
    from evals.calibration.ingestion import (
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

    manifest, prompts = build_logical_batches(
        campaign_id=campaign_id,
        packet=packet,
        max_cases=max_cases,
        max_serialized_bytes=max_serialized_bytes,
    )
    manifest["neutral_packet_sha256"] = packet_sha
    manifest["source_packet_digest"] = source_packet_digest
    manifest["prompt_digest"] = labeling_instructions_digest()
    manifest["label_guide_version"] = LABEL_GUIDE_VERSION
    manifest["reviewer_instructions_version"] = REVIEWER_INSTRUCTIONS_VERSION
    manifest["logical_manifest_digest"] = logical_manifest_digest(manifest)
    manifest_payload = _canonical_json_bytes(manifest)

    # The shared frozen logical-batch authority, bound to the lanes' frozen
    # visible-model authorities (FIX-1) and written as durable campaign
    # authority next to the batches (FIX-2).
    lane_authority_digests: dict[str, str] = {}
    for slot in REVIEWER_SLOTS:
        lane_root = protected_root / "lanes" / slot
        if not (lane_root / "lane.json").is_file():
            raise ValueError(f"subscription_prepare_lane_not_initialized:{slot}")
        authority = load_subscription_lane_authority(lane_root)
        lane_authority_digests[slot] = authority.authority_digest
    prepare_record: dict[str, Any] = {
        "prepare_schema": SUBSCRIPTION_PREPARE_SCHEMA,
        "campaign_id": campaign_id,
        "protocol_version": CONSENSUS_PROTOCOL_VERSION,
        "sampling_manifest_digest": sampling.manifest_digest(),
        "source_packet_digest": source_packet_digest,
        "neutral_packet_sha256": packet_sha,
        "prompt_digest": labeling_instructions_digest(),
        "label_guide_version": LABEL_GUIDE_VERSION,
        "reviewer_instructions_version": REVIEWER_INSTRUCTIONS_VERSION,
        "max_cases": max_cases,
        "max_serialized_bytes": max_serialized_bytes,
        "batch_count": len(prompts),
        "total_cases": sum(int(entry["case_count"]) for entry in manifest["batches"]),
        "max_serialized_bytes_observed": int(manifest["max_serialized_bytes_observed"]),
        "logical_manifest_digest": manifest["logical_manifest_digest"],
        "lane_authority_digests": lane_authority_digests,
    }
    prepare_payload = _canonical_json_bytes(prepare_record)

    existing_prepare = prepare_record_path(protected_root)
    if existing_prepare.exists():
        # Re-export must be byte-identical (determinism proof), else fail.
        if existing_prepare.read_bytes() != prepare_payload:
            raise ValueError("subscription_prepare_conflict_not_deterministic")
        existing_manifest = batch_manifest_path(protected_root)
        if existing_manifest.read_bytes() != manifest_payload:
            raise ValueError("subscription_batch_manifest_conflict_not_deterministic")
        for entry in manifest["batches"]:
            path = batch_prompt_path(protected_root, str(entry["batch_id"]))
            if not path.is_file() or not hmac.compare_digest(
                _sha256(path.read_bytes()), str(entry["prompt_sha256"])
            ):
                raise ValueError("subscription_batch_prompt_conflict_not_deterministic")
        return load_batch_manifest_summary(manifest)

    for entry, prompt in zip(manifest["batches"], prompts, strict=True):
        write_protected_file(
            batch_prompt_path(protected_root, str(entry["batch_id"])),
            serialize_prompt(prompt),
        )
    write_protected_file(batch_manifest_path(protected_root), manifest_payload)
    write_protected_file(existing_prepare, prepare_payload)
    return load_batch_manifest_summary(manifest)


def load_batch_manifest_summary(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Public-safe summary (no sample IDs) of an in-memory batch manifest."""
    return {
        "batch_count": len(manifest["batches"]),
        "total_cases": sum(int(entry["case_count"]) for entry in manifest["batches"]),
        "max_serialized_bytes_observed": int(manifest["max_serialized_bytes_observed"]),
        "max_serialized_bytes": int(manifest["max_serialized_bytes"]),
        "max_cases": int(manifest["max_cases"]),
        "logical_manifest_digest": str(manifest["logical_manifest_digest"]),
        "batches": [
            {
                "batch_id": str(entry["batch_id"]),
                "index": int(entry["index"]),
                "case_count": int(entry["case_count"]),
                "serialized_bytes": int(entry["serialized_bytes"]),
                "prompt_sha256": str(entry["prompt_sha256"]),
            }
            for entry in manifest["batches"]
        ],
    }


def _reconstruct_canonical_manifest(
    *,
    campaign_id: str,
    prepare: Mapping[str, Any],
    packet: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Re-derive the ENTIRE canonical logical manifest AND every prompt as a
    deterministic projection of the prepare record's frozen identity plus
    the byte-verified retained packet (FIX-2). The reconstruction never
    reads the on-disk batches — the on-disk artifacts must prove themselves
    byte-equal to it. Case content, instructions, response contract,
    membership, and order therefore cannot change through any rewrite of
    the mutable batches/ files."""
    from evals.calibration.freeze import LABEL_GUIDE_VERSION
    from evals.calibration.ingestion import labeling_instructions_digest
    from evals.calibration.reviewer_instructions import REVIEWER_INSTRUCTIONS_VERSION

    manifest, prompts = build_logical_batches(
        campaign_id=campaign_id,
        packet=packet,
        max_cases=int(prepare["max_cases"]),
        max_serialized_bytes=int(prepare["max_serialized_bytes"]),
    )
    manifest["neutral_packet_sha256"] = str(prepare["neutral_packet_sha256"])
    manifest["source_packet_digest"] = str(prepare["source_packet_digest"])
    manifest["prompt_digest"] = labeling_instructions_digest()
    manifest["label_guide_version"] = LABEL_GUIDE_VERSION
    manifest["reviewer_instructions_version"] = REVIEWER_INSTRUCTIONS_VERSION
    manifest["logical_manifest_digest"] = logical_manifest_digest(manifest)
    for field in (
        "prompt_digest",
        "label_guide_version",
        "reviewer_instructions_version",
    ):
        if manifest[field] != prepare.get(field):
            raise ValueError(f"subscription_prepare_{field}_not_canonical_frozen_authority")
    return manifest, prompts


def verify_review_batches(
    protected_root: Path,
    *,
    sampling: SamplingManifest | None = None,
    lane_slots: Sequence[str] | None = None,
    source_packet_digest: str | None = None,
) -> dict[str, Any]:
    """Canonically verify the retained batch manifest + prompt bytes.

    FIX-2: internal self-consistency is NOT authority. The verification
    EXTERNALLY reconstructs the canonical logical manifest and every prompt
    from frozen authority — the byte-verified retained neutral packet
    (re-read from the subscription lanes' own retained bytes), the canonical
    #206 labeling instruction bundle, the frozen guide/reviewer-instruction
    versions, and the campaign/protocol/sampling/source identity recorded in
    the frozen preparation record — and then requires:

    - the retained ``manifest.json`` bytes equal the canonical manifest;
    - its self-digest equals the prepare record's frozen
      ``logical_manifest_digest`` (a rewritten manifest cannot become its
      own authority);
    - every retained prompt file's bytes equal the canonical projection
      (exact case objects, exact instruction object, exact response
      contract) — field equality, not sample-ID matching;
    - batch membership/order is the deterministic manifest partition of the
      frozen packet; no duplicates; the full partition covers the sampling;
    - every prompt's ACTUAL serialized size respects the frozen ceiling;
    - each lane's frozen visible-model authority still hashes to the digest
      bound at preparation (FIX-1 fail-closed on model changes).

    Returns the verified canonical manifest.
    """
    prepare_file = prepare_record_path(protected_root)
    if not prepare_file.is_file():
        raise ValueError("subscription_batches_not_prepared")
    prepare = json.loads(prepare_file.read_text())
    if prepare.get("prepare_schema") != SUBSCRIPTION_PREPARE_SCHEMA:
        raise ValueError("subscription_prepare_schema_mismatch")
    campaign_id = str(prepare.get("campaign_id", ""))
    if prepare.get("protocol_version") != CONSENSUS_PROTOCOL_VERSION:
        raise ValueError("subscription_prepare_protocol_mismatch")
    if not subscription_mode_permitted(campaign_id, str(prepare.get("protocol_version", ""))):
        raise ValueError("subscription_ui_mode_not_opted_in_for_campaign")
    if sampling is not None and (
        prepare.get("sampling_manifest_digest") != sampling.manifest_digest()
    ):
        raise ValueError("subscription_batch_sampling_manifest_mismatch")

    # FIX-1: the frozen visible-model lane authorities must still hash to
    # their preparation-time digests (mid-campaign model change fails here).
    for slot, digest in sorted(dict(prepare.get("lane_authority_digests", {})).items()):
        lane_root = protected_root / "lanes" / str(slot)
        if not subscription_lane_authority_path(lane_root).is_file():
            raise ValueError(f"subscription_prepare_lane_authority_missing:{slot}")
        authority = load_subscription_lane_authority(lane_root)
        if not hmac.compare_digest(authority.authority_digest, str(digest)):
            raise ValueError(f"subscription_lane_authority_digest_mismatch:{slot}")

    # FIX-2: re-derive everything from the lanes' retained frozen packet.
    packet, _packet_sha = _load_frozen_packet_from_lanes(
        protected_root,
        campaign_id=campaign_id,
        prepare=prepare,
        sampling=sampling,
        source_packet_digest=source_packet_digest,
        lane_slots=lane_slots,
    )
    canonical, canonical_prompts = _reconstruct_canonical_manifest(
        campaign_id=campaign_id,
        prepare=prepare,
        packet=packet,
    )
    if not hmac.compare_digest(
        str(canonical["logical_manifest_digest"]), str(prepare.get("logical_manifest_digest", ""))
    ):
        raise ValueError("subscription_prepare_logical_manifest_digest_mismatch")

    path = batch_manifest_path(protected_root)
    if not path.is_file():
        raise ValueError("subscription_batches_not_exported")
    retained_manifest_bytes = path.read_bytes()
    if retained_manifest_bytes != _canonical_json_bytes(canonical):
        raise ValueError("subscription_batch_manifest_not_canonical_projection")
    manifest: dict[str, Any] = json.loads(retained_manifest_bytes)
    entries = manifest.get("batches")
    if not isinstance(entries, list) or not entries:
        raise ValueError("subscription_batch_manifest_empty")

    all_ids: list[str] = []
    ceiling = int(manifest["max_serialized_bytes"])
    for position, (entry, canonical_prompt) in enumerate(
        zip(entries, canonical_prompts, strict=True), start=1
    ):
        if int(entry["index"]) != position:
            raise ValueError("subscription_batch_index_not_contiguous")
        prompt_path = batch_prompt_path(protected_root, str(entry["batch_id"]))
        if not prompt_path.is_file():
            raise ValueError("subscription_batch_prompt_missing")
        payload = prompt_path.read_bytes()
        canonical_bytes = serialize_prompt(canonical_prompt)
        if payload != canonical_bytes:
            raise ValueError("subscription_batch_prompt_not_canonical_projection")
        if not hmac.compare_digest(_sha256(payload), str(entry["prompt_sha256"])):
            raise ValueError("subscription_batch_prompt_digest_mismatch")
        if len(payload) > ceiling:
            raise ValueError("subscription_batch_prompt_exceeds_serialized_ceiling")
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


def prepare_subscription_campaign(
    protected_root: Path,
    *,
    campaign_id: str,
    sampling: SamplingManifest,
    source_packet_digest: str,
    neutral_packet_path: Path,
    neutral_packet_manifest: Path,
    max_cases: int = BATCH_MAX_CASES,
    max_serialized_bytes: int = BATCH_MAX_SERIALIZED_BYTES,
) -> dict[str, Any]:
    """The one campaign preparation operation (``sub-prepare``).

    Self-contained #208 workflow step. Verifies all three subscription
    lanes are initialized with their frozen service/family/visible-model
    authority and the exact shared neutral packet; emits the canonical #206
    per-lane request generations (only when a lane has none yet, so
    re-running preparation is idempotent and never generates semantically
    different request authority after outputs exist); exports the shared
    deterministic logical paste batches and freezes their manifest digest
    as durable campaign authority; binds both sides together; and returns
    the first outstanding logical batch.
    """
    from evals.calibration.ingestion import LaneSession, lane_provenance_mode

    if not subscription_mode_permitted(campaign_id, CONSENSUS_PROTOCOL_VERSION):
        raise ValueError("subscription_ui_mode_not_opted_in_for_campaign")
    lane_status: dict[str, dict[str, Any]] = {}
    for slot in REVIEWER_SLOTS:
        session = LaneSession(protected_root, slot)
        if lane_provenance_mode(session.lane_root) != SUBSCRIPTION_PROVENANCE_MODE:
            raise ValueError(f"subscription_prepare_lane_not_subscription_mode:{slot}")
        if session.sampling_manifest_digest != sampling.manifest_digest():
            raise ValueError(f"subscription_prepare_lane_sampling_mismatch:{slot}")
        if session.source_packet_digest != source_packet_digest:
            raise ValueError(f"subscription_prepare_lane_source_packet_mismatch:{slot}")
        authority = load_subscription_lane_authority(session.lane_root)
        generations = sorted(session.lane_root.glob("lane-requests-*.jsonl"))
        if generations:
            # Idempotent: existing canonical request authority is never
            # re-emitted (a second generation could only differ semantically
            # after outputs exist, which is exactly what must never happen).
            emitted = "already_emitted"
        else:
            session.emit_requests(
                neutral_packet_path, sampling=sampling, manifest_path=neutral_packet_manifest
            )
            emitted = "emitted"
        lane_status[slot] = {
            "reviewer_slot": slot,
            "service": authority.service,
            "reviewer_family": authority.reviewer_family,
            "user_visible_model_name": authority.user_visible_model_name,
            "canonical_requests": emitted,
            "lane_identity_digest": authority.lane_identity_digest,
        }
    summary = export_review_batches(
        protected_root,
        campaign_id=campaign_id,
        sampling=sampling,
        neutral_packet_path=neutral_packet_path,
        neutral_packet_manifest=neutral_packet_manifest,
        source_packet_digest=source_packet_digest,
        max_cases=max_cases,
        max_serialized_bytes=max_serialized_bytes,
    )
    nxt = next_outstanding_batch(protected_root, sampling=sampling)
    return {"lanes": lane_status, "batches": summary, "next_outstanding_batch": nxt}


# --- Batch response import (FIX-3) ----------------------------------------------


def batch_attempts_dir(lane_root: Path, batch_id: str) -> Path:
    return lane_root / "raw-batches" / f"{batch_id}.attempts"


def attempt_dir(lane_root: Path, batch_id: str, attempt: int) -> Path:
    return batch_attempts_dir(lane_root, batch_id) / f"attempt-{attempt:02d}"


def attempt_raw_path(lane_root: Path, batch_id: str, attempt: int) -> Path:
    return attempt_dir(lane_root, batch_id, attempt) / "raw.resp"


def attempt_attestation_path(lane_root: Path, batch_id: str, attempt: int) -> Path:
    return attempt_dir(lane_root, batch_id, attempt) / "attestation.json"


def attempt_record_path(lane_root: Path, batch_id: str, attempt: int) -> Path:
    return attempt_dir(lane_root, batch_id, attempt) / "attempt.json"


def latest_attempt(lane_root: Path, batch_id: str) -> int:
    """Highest existing attempt number for this batch (0 when none)."""
    root = batch_attempts_dir(lane_root, batch_id)
    if not root.is_dir():
        return 0
    latest = 0
    for path in root.glob("attempt-*"):
        suffix = path.name.rsplit("-", 1)[-1]
        if suffix.isdigit():
            latest = max(latest, int(suffix))
    return latest


def latest_committed_attempt(lane_root: Path, batch_id: str) -> int:
    """Highest attempt number whose full evidence triple exists (raw.resp +
    attestation.json + attempt.json) — the committed-attempt watermark.

    FIX-4 (round 3): an interruption can leave a PARTIAL ``attempt-N/``
    directory (crash between the raw write and the attempt-record write).
    Only a complete triple is a committed attempt; partial directories are
    reconciled deterministically by ``_reconcile_partial_attempts`` instead
    of bricking the batch.
    """
    root = batch_attempts_dir(lane_root, batch_id)
    if not root.is_dir():
        return 0
    latest = 0
    for path in root.glob("attempt-*"):
        suffix = path.name.rsplit("-", 1)[-1]
        if not suffix.isdigit():
            continue  # quarantined partial attempts are never committed
        number = int(suffix)
        if number <= latest:
            continue
        if (
            (path / "raw.resp").is_file()
            and (path / "attestation.json").is_file()
            and (path / "attempt.json").is_file()
        ):
            latest = number
    return latest


def _quarantine_partial_attempt(lane_root: Path, batch_id: str, attempt: int) -> None:
    """Retain — never delete — a partial attempt that cannot be completed
    deterministically, by renaming its directory out of the numeric attempt
    sequence. The bytes stay on disk for audit; nothing is overwritten."""
    directory = attempt_dir(lane_root, batch_id, attempt)
    target = directory.parent / f"{directory.name}.quarantined-{secrets.token_hex(6)}"
    directory.rename(target)


def _reconcile_partial_attempts(
    lane_root: Path,
    batch_id: str,
    *,
    raw_digest: str,
    batch_digest: str,
    retry_permitted: bool,
) -> tuple[int, SubscriptionReviewAttestation | None]:
    """FIX-4 (round 3): deterministic crash recovery for partial attempts.

    A crash between ``raw.resp`` and ``attempt.json`` leaves a partial
    directory above the committed watermark. Recovery rules (never deleting
    evidence, never overwriting mismatching bytes):

    - raw only, and the resupplied bytes match exactly: finish the SAME
      attempt deterministically (the caller re-derives classification and
      writes the missing attestation/attempt record);
    - raw + attestation, both digest-consistent with the resupplied bytes
      and this batch: reuse the preserved attestation and complete only the
      attempt record;
    - anything else (mismatching bytes, inconsistent attestation): quarantine
      the partial directory and require an explicit mechanical retry before
      a new attempt number is used.
    """
    from pydantic import ValidationError

    attempt = latest_committed_attempt(lane_root, batch_id) + 1
    while True:
        directory = attempt_dir(lane_root, batch_id, attempt)
        if not directory.is_dir():
            return attempt, None
        raw_path = attempt_raw_path(lane_root, batch_id, attempt)
        attestation_path = attempt_attestation_path(lane_root, batch_id, attempt)
        record_path = attempt_record_path(lane_root, batch_id, attempt)
        if record_path.is_file():  # pragma: no cover - defensive
            raise ValueError(f"subscription_attempt_unexpected_committed:{attempt}")
        if raw_path.is_file() and not attestation_path.is_file():
            if hmac.compare_digest(_sha256(raw_path.read_bytes()), raw_digest):
                return attempt, None
        elif raw_path.is_file() and attestation_path.is_file():
            existing: SubscriptionReviewAttestation | None = None
            try:
                existing = SubscriptionReviewAttestation.model_validate(
                    json.loads(attestation_path.read_text())
                )
                preserved_digest = _sha256(raw_path.read_bytes())
                consistent = (
                    existing is not None
                    and hmac.compare_digest(existing.raw_response_digest, preserved_digest)
                    and hmac.compare_digest(existing.raw_response_digest, raw_digest)
                    and hmac.compare_digest(existing.request_batch_digest, batch_digest)
                )
            except (ValueError, ValidationError, OSError):
                consistent = False
            if consistent:
                return attempt, existing
        if not retry_permitted:
            raise ValueError(f"subscription_partial_attempt_retry_required:{attempt}")
        _quarantine_partial_attempt(lane_root, batch_id, attempt)
        attempt += 1


class SubscriptionBatchAttempt(Record):
    """FIX-3: protected per-attempt evidence for one logical batch import.

    Every returned browser response is preserved BEFORE substantive
    parsing/classification changes campaign state. Each attempt is an
    independent bound pair (exact raw bytes + matching attestation) plus
    this record, distinguishing:

    - ``completed_structured`` — strict per-case structured judgments;
    - ``substantive_refusal`` — a substantive completed refusal of the
      whole batch (parseable refusal object): every case enters escalation;
    - ``substantive_malformed`` — any other substantive completed response
      that cannot yield per-case structured judgments (prose, fences,
      truncated or wrong-shape JSON): every case enters escalation;
    - ``mechanically_incomplete`` — capture/transport-shaped failure only
      (empty capture, or structured-but-drifting membership such as
      missing/extra/duplicate/out-of-order results), retryable exclusively
      through an explicit mechanical retry that leaves this attempt's raw
      bytes + attestation + record fully intact.

    A substantive completed response can never disappear and be rerun until
    it becomes convenient; an accepted (``completed_structured``) attempt
    can never be replaced.
    """

    attempt_schema: Literal["engram-calibration-subscription-batch-attempt-209-v2"] = (
        SUBSCRIPTION_BATCH_ATTEMPT_SCHEMA
    )
    campaign_id: str
    protocol_version: str
    reviewer_slot: str
    batch_id: str
    attempt: int
    outcome: Literal[
        "completed_structured",
        "substantive_refusal",
        "substantive_malformed",
        "mechanically_incomplete",
    ]
    request_batch_digest: str
    raw_response_digest: str
    attestation_digest: str
    case_count: int
    retry_of_attempt: int | None = None
    mechanical_failure_reason: str | None = None
    completed_at: str

    @model_validator(mode="after")
    def attempt_contract(self) -> Self:
        if self.protocol_version != CONSENSUS_PROTOCOL_VERSION:
            raise ValueError("protocol_version_mismatch")
        if self.reviewer_slot not in REVIEWER_SLOTS:
            raise ValueError("unknown_reviewer_slot")
        if self.attempt < 1:
            raise ValueError("subscription_attempt_must_be_positive")
        if len(self.request_batch_digest) != 64 or len(self.raw_response_digest) != 64:
            raise ValueError("subscription_attempt_requires_sha256_hex_digests")
        if len(self.attestation_digest) != 64:
            raise ValueError("subscription_attempt_requires_attestation_digest")
        if self.outcome == "mechanically_incomplete" and not self.mechanical_failure_reason:
            raise ValueError("subscription_attempt_requires_mechanical_failure_reason")
        if self.outcome != "mechanically_incomplete" and self.mechanical_failure_reason:
            raise ValueError("subscription_attempt_must_not_carry_failure_reason")
        if self.retry_of_attempt is not None and self.retry_of_attempt >= self.attempt:
            raise ValueError("subscription_attempt_retry_of_not_earlier")
        if not subscription_mode_permitted(self.campaign_id, self.protocol_version):
            raise ValueError("subscription_ui_mode_not_opted_in_for_campaign")
        return self

    def payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def load_attempt(
    lane_root: Path,
    batch_id: str,
    attempt: int,
) -> tuple[SubscriptionBatchAttempt, SubscriptionReviewAttestation, bytes]:
    """Load one preserved attempt: (record, attestation, exact raw bytes)."""
    record = SubscriptionBatchAttempt.model_validate(
        json.loads(attempt_record_path(lane_root, batch_id, attempt).read_text())
    )
    attestation = SubscriptionReviewAttestation.model_validate(
        json.loads(attempt_attestation_path(lane_root, batch_id, attempt).read_text())
    )
    raw = attempt_raw_path(lane_root, batch_id, attempt).read_bytes()
    if not hmac.compare_digest(_sha256(raw), record.raw_response_digest):
        raise ValueError("subscription_attempt_raw_digest_mismatch")
    if record.attestation_digest != attestation.attestation_digest:
        raise ValueError("subscription_attempt_attestation_digest_mismatch")
    if attestation.raw_response_digest != record.raw_response_digest:
        raise ValueError("subscription_attempt_attestation_raw_mismatch")
    return record, attestation, raw


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
        raise ValueError("subscription_batch_response_extra_keys:" + ",".join(sorted(unknown_keys)))
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
                f"subscription_batch_result_extra_fields:{sample_id}:" + ",".join(sorted(extra))
            )
        missing = set(BATCH_RESULT_FIELDS) - set(result)
        if missing:
            raise ValueError(
                f"subscription_batch_result_missing_fields:{sample_id}:" + ",".join(sorted(missing))
            )
        parsed.append(result)
    return parsed


def _strip_fences(text: str) -> str:
    """Strip ONE surrounding markdown code fence pair, if present."""
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    return stripped.strip()


def _classify_substantive(raw: bytes) -> tuple[AttemptOutcome, str | None]:
    """Classify a substantive non-structured response (deterministic).

    Returns ``(outcome, refusal_error_code)``:

    - ``substantive_refusal`` — the bytes (after stripping one surrounding
      markdown fence) parse as a JSON object whose ``outcome`` is
      ``"refused"`` with a non-empty ``error_code`` and no ``judgment``:
      the reviewer explicitly refused the whole batch.
    - ``substantive_malformed`` — everything else that is not strict
      structured output and not mechanically incomplete (prose, truncated
      JSON, wrong-shape JSON, etc.).
    """
    try:
        payload = json.loads(_strip_fences(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "substantive_malformed", None
    if not isinstance(payload, dict):
        return "substantive_malformed", None
    if payload.get("outcome") != "refused":
        return "substantive_malformed", None
    error_code = payload.get("error_code")
    if not isinstance(error_code, str) or not error_code or payload.get("judgment") is not None:
        return "substantive_malformed", None
    if not _ERROR_CODE_PATTERN.fullmatch(error_code):
        return "substantive_malformed", None
    return "substantive_refusal", error_code


def _mechanical_failure_reason(exc: ValueError, raw: bytes) -> str | None:
    """A mechanical (retryable) failure ONLY for capture/transport-shaped
    incompleteness, never for substantive model noncompliance (round-3
    FIX-3):

    - ``empty_capture`` — empty or whitespace-only capture (nothing was
      actually returned);
    - ``truncated_json`` — the text opens a JSON object/array (``{``/``[``)
      but the bytes end mid-structure: the structured contract was being
      followed and the capture is completion-shaped truncation.

    Everything else — a syntactically complete returned object with missing
    cases, extra cases, duplicate IDs, out-of-order IDs, missing/extra
    result fields, or a wrong complete ``results`` shape — is substantive
    completed evidence of response-contract noncompliance and is NOT
    retryable here. Consumer-UI contract noncompliance alone is never
    treated as proof of transport failure.
    """
    message = str(exc)
    if message == "subscription_batch_response_unparseable":
        if not raw.strip():
            return "empty_capture"
        stripped = _strip_fences(raw.decode("utf-8", errors="replace")).lstrip()
        if stripped[:1] in ("{", "["):
            return "truncated_json"
        return None
    return None


def _result_envelope(result: Mapping[str, Any]) -> dict[str, Any]:
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


def _escalation_envelope(sample_id: str, outcome: str, error_code: str) -> dict[str, Any]:
    """Deterministic per-case envelope for a batch-level substantive
    refusal/malformed outcome (FIX-3). The per-case record honestly marks
    the case as refused/malformed AT THE BATCH LEVEL — it never claims the
    model individually judged fields it never produced. The model's exact
    bytes remain preserved in the attempt raw response.

    ``malformed`` is encoded as an invalid judgment (``outcome == judged``
    with an empty judgment object), which the frozen parser degrades to
    ``malformed`` — the caller cannot select or relabel the outcome.
    """
    if outcome == "substantive_refusal":
        return {"sample_id": sample_id, "outcome": "refused", "error_code": error_code}
    return {"sample_id": sample_id, "outcome": "judged", "judgment": {}}


def _lane_records_bound_to_batch(session: LaneSessionAlias, batch_id: str) -> set[str]:
    """Sample IDs whose accepted records attest to this exact batch."""
    from evals.calibration.model_lanes import load_lane_records

    manifest = json.loads(batch_manifest_path(session.protected_root).read_text())
    entry = next(
        (e for e in manifest["batches"] if str(e["batch_id"]) == batch_id),
        None,
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
    sampling: SamplingManifest,
    source_packet_digest: str | None = None,
    attested_at: str | None = None,
    conversation_reference: str | None = None,
    retry_mechanical_failure: bool = False,
) -> dict[str, Any]:
    """Ingest one verbatim subscription-UI batch response into one lane.

    FIX-3: the exact returned browser bytes and a digest-bound attestation
    are preserved for EVERY attempt BEFORE any substantive parsing or
    classification changes campaign state. Substantive refusal/malformed
    responses are preserved and mechanically expand to batch-level
    escalation records for every case in the batch (they can never
    disappear and be rerun until convenient). A genuinely mechanical
    failure may be retried explicitly; the failed attempt's raw bytes,
    attestation, number, and reason remain fully intact, and the retry is a
    new independent bound pair. An accepted (``completed_structured``)
    attempt can never be replaced.

    FIX-1: no service/model/operator claims are accepted here at all — the
    attestation is built exclusively from the lane's frozen visible-model
    authority, so a mid-campaign visible-model change fails closed inside
    ``verify_review_batches`` before any evidence is touched.
    """
    from evals.calibration.ingestion import LaneSession, lane_provenance_mode
    from evals.calibration.review import write_protected_file

    # FIX-2 (round 3): the import boundary requires only a STRING. The exact
    # empty string is valid attempt evidence (empty_capture): it must reach
    # the preserved mechanical-attempt path — raw.resp (zero bytes),
    # attestation, attempt.json, mechanically_incomplete/empty_capture —
    # exactly like every other returned attempt.
    if not isinstance(raw_response, str):
        raise ValueError("subscription_import_requires_raw_response_text")
    if batch_id != batch_id.strip() or not _BATCH_ID_PATTERN.fullmatch(batch_id):
        raise ValueError("subscription_batch_id_not_canonical")
    manifest = verify_review_batches(
        protected_root, sampling=sampling, source_packet_digest=source_packet_digest
    )
    entry = next((e for e in manifest["batches"] if str(e["batch_id"]) == batch_id), None)
    if entry is None:
        raise ValueError("subscription_batch_not_in_canonical_manifest")
    expected_ids = [str(sid) for sid in entry["sample_ids"]]
    raw_bytes = raw_response.encode()
    raw_digest = _sha256(raw_bytes)
    batch_digest = str(entry["prompt_sha256"])

    session = LaneSession(protected_root, reviewer_slot)
    if lane_provenance_mode(session.lane_root) != SUBSCRIPTION_PROVENANCE_MODE:
        raise ValueError("lane_not_in_subscription_mode")
    campaign_id = session.campaign_id
    lane_authority = load_subscription_lane_authority(session.lane_root)

    # Cross-lane / cross-batch replay (#209): the same response bytes can
    # never be attested into two different lanes or two different logical
    # batches. Independently-produced frontier responses echoing 402 frozen
    # sample IDs are never byte-identical; identical bytes mean replay (or
    # cross-service contamination), and both are refused. The idempotent
    # resupply exception is ONLY the same lane+batch attempt being re-supplied
    # with its own exact bytes. FIX-2 (round 3): an empty/whitespace-only
    # capture carries no content identity — byte-identity replay detection is
    # meaningless for it and is skipped (each lane's empty capture is its own
    # preserved mechanical attempt).
    if raw_bytes.strip():
        for other_slot in REVIEWER_SLOTS:
            other_attempts_root = protected_root / "lanes" / other_slot / "raw-batches"
            if not other_attempts_root.is_dir():
                continue
            for existing in sorted(other_attempts_root.glob("*.attempts/attempt-*/raw.resp")):
                same_batch = existing.parent.parent.name == f"{batch_id}.attempts"
                if other_slot == reviewer_slot and same_batch:
                    continue  # handled by attempt conflict semantics below
                if hmac.compare_digest(_sha256(existing.read_bytes()), raw_digest):
                    raise ValueError(
                        "subscription_batch_response_replay_refused:"
                        f"{other_slot}:{existing.parent.parent.name}"
                    )

    # FIX-4 (round 3): only a COMPLETE evidence triple (raw + attestation +
    # attempt record) is a committed attempt. A crash between writes leaves
    # a partial directory that is reconciled deterministically below —
    # an interruption can never permanently brick the batch.
    current = latest_committed_attempt(session.lane_root, batch_id)
    attempt = 0
    retry_of: int | None = None
    mechanical_reason: str | None = None
    record_outcome: AttemptOutcome | None = None
    refusal_code: str | None = None
    results: list[dict[str, Any]] | None = None
    attestation: SubscriptionReviewAttestation | None = None
    if current:
        record, preserved_attestation, preserved_raw = load_attempt(
            session.lane_root, batch_id, current
        )
        if _sha256(preserved_raw) == raw_digest:
            # Idempotent resupply of the exact preserved attempt bytes:
            # re-derive the same classification and resume ingestion.
            attempt = current
            record_outcome = record.outcome
            if record_outcome == "completed_structured":
                results = _parse_batch_response(preserved_raw, expected_ids)
            attestation = preserved_attestation
        else:
            if record.outcome == "completed_structured":
                raise ValueError("subscription_attempt_accepted_cannot_be_replaced")
            if record.outcome in ("substantive_refusal", "substantive_malformed"):
                raise ValueError("subscription_attempt_substantive_cannot_be_retried")
            # mechanically incomplete: explicit retry, never overwriting
            if not retry_mechanical_failure:
                raise ValueError("subscription_batch_raw_conflict_retry_required")
            if _lane_records_bound_to_batch(session, batch_id):
                raise ValueError("subscription_batch_retry_has_accepted_records")
            retry_of = current

    if attestation is None or record_outcome is None:
        # FIX-4 (round 3): the next attempt slot may hold a PARTIAL directory
        # from an interrupted import. Reconcile it deterministically: matching
        # bytes finish the SAME attempt; raw+attestation reuse the preserved
        # attestation; anything else is quarantined (never deleted) and needs
        # an explicit mechanical retry.
        attempt, recovered_attestation = _reconcile_partial_attempts(
            session.lane_root,
            batch_id,
            raw_digest=raw_digest,
            batch_digest=batch_digest,
            retry_permitted=retry_mechanical_failure,
        )
        if recovered_attestation is not None:
            attestation = recovered_attestation

    if attestation is None or record_outcome is None:
        # FIX-3 preserve-first ordering: exact raw bytes land on disk BEFORE
        # any substantive parse/classification changes campaign state.
        raw_path = attempt_raw_path(session.lane_root, batch_id, attempt)
        if not raw_path.exists():
            write_protected_file(raw_path, raw_bytes)
        elif _sha256(raw_path.read_bytes()) != raw_digest:  # pragma: no cover - race guard
            raise ValueError("subscription_attempt_raw_conflict")
        # Attempt classification (total: never raises past this point).
        try:
            results = _parse_batch_response(raw_bytes, expected_ids)
            record_outcome = "completed_structured"
        except ValueError as exc:
            mechanical_reason = _mechanical_failure_reason(exc, raw_bytes)
            if mechanical_reason is not None:
                record_outcome = "mechanically_incomplete"
            else:
                classified, refusal_code = _classify_substantive(raw_bytes)
                record_outcome = classified
                if classified == "substantive_malformed":
                    refusal_code = None
        if attestation is None:
            attestation = build_attestation(
                campaign_id=campaign_id,
                reviewer_slot=reviewer_slot,
                lane_authority=lane_authority,
                request_batch_digest=batch_digest,
                raw_response_digest=raw_digest,
                attested_at=attested_at,
                conversation_reference=conversation_reference,
            )
            write_protected_file(
                attempt_attestation_path(session.lane_root, batch_id, attempt),
                (json.dumps(attestation.payload(), sort_keys=True) + "\n").encode(),
            )
        assert record_outcome is not None
        attempt_record = SubscriptionBatchAttempt(
            campaign_id=campaign_id,
            protocol_version=CONSENSUS_PROTOCOL_VERSION,
            reviewer_slot=reviewer_slot,
            batch_id=batch_id,
            attempt=attempt,
            outcome=record_outcome,
            request_batch_digest=batch_digest,
            raw_response_digest=raw_digest,
            attestation_digest=attestation.attestation_digest,
            case_count=len(expected_ids),
            retry_of_attempt=retry_of,
            mechanical_failure_reason=(
                mechanical_reason if record_outcome == "mechanically_incomplete" else None
            ),
            completed_at=datetime.now(UTC).isoformat(),
        )
        write_protected_file(
            attempt_record_path(session.lane_root, batch_id, attempt),
            _canonical_json_bytes(attempt_record.payload()),
        )
    assert attestation is not None

    accepted = 0
    resumed = 0
    escalated = 0
    errors: list[dict[str, Any]] = []
    if record_outcome == "completed_structured":
        assert results is not None
        for result in results:
            sample_id = str(result["sample_id"])
            try:
                receipt = observe_subscription_execution(
                    session.lane_root,
                    campaign_id=campaign_id,
                    sample_id=sample_id,
                    reviewer_slot=reviewer_slot,
                    subscription_attestation=attestation,
                    executor_identity=f"operator:{lane_authority.operator_reference}",
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
    elif record_outcome in ("substantive_refusal", "substantive_malformed"):
        # Batch-level escalation: every case in the batch enters the
        # protocol's human-escalation path as an honestly-marked refused or
        # malformed record bound to this preserved attempt.
        error_code = refusal_code or "subscription_batch_malformed"
        for sample_id in expected_ids:
            try:
                receipt = observe_subscription_execution(
                    session.lane_root,
                    campaign_id=campaign_id,
                    sample_id=sample_id,
                    reviewer_slot=reviewer_slot,
                    subscription_attestation=attestation,
                    executor_identity=f"operator:{lane_authority.operator_reference}",
                )
                envelope = _escalation_envelope(sample_id, record_outcome, error_code)
                session.ingest_response(
                    {
                        "sample_id": sample_id,
                        "execution": receipt.model_dump(mode="json"),
                        "raw_response": json.dumps(envelope, sort_keys=True),
                    },
                    sampling=sampling,
                )
                escalated += 1
            except ValueError as exc:
                message = str(exc)
                if "review_record_already_accepted" in message:
                    resumed += 1
                    continue
                errors.append({"sample_id": sample_id, "error": message})
    return {
        "batch_id": batch_id,
        "reviewer_slot": reviewer_slot,
        "attempt": attempt,
        "outcome": record_outcome,
        "cases": len(expected_ids),
        "accepted": accepted,
        "resumed_duplicates": resumed,
        "escalated": escalated,
        "errors": errors,
        "raw_response_digest": raw_digest,
        "request_batch_digest": batch_digest,
        "attestation_digest": attestation.attestation_digest,
        "frozen_user_visible_model": lane_authority.user_visible_model_name,
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
    ATTESTED actual identity is the operator's attestation (slot/family/model
    from the frozen lane authority; config/prompt digests from the frozen
    request line the lane emitted). Ingestion compares this observed identity
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
    for EVERY case in the batch (per-lane gaps are visible in
    ``lane_gaps``).

    FIX-4: fails closed when any subscription lane is missing its canonical
    per-lane #206 request evidence — the documented workflow must never
    reach handoff without the request generations import requires.
    """
    from evals.calibration.ingestion import _load_request_registry
    from evals.calibration.model_lanes import load_lane_records

    slots = tuple(reviewer_slots or REVIEWER_SLOTS)
    manifest = verify_review_batches(protected_root, sampling=sampling)
    for slot in slots:
        registry = _load_request_registry(protected_root / "lanes" / slot)
        if not registry:
            raise ValueError(f"subscription_lane_missing_canonical_requests:{slot}")
    records = {slot: load_lane_records(protected_root, slot) for slot in slots}
    for entry in sorted(manifest["batches"], key=lambda e: int(e["index"])):
        ids = [str(sid) for sid in entry["sample_ids"]]
        lane_gaps = {slot: sum(1 for sid in ids if sid not in records[slot]) for slot in slots}
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
    still agrees with the PRESERVED per-attempt evidence: the attempt record
    and attestation files must exist and equal the embedded payloads, the
    preserved raw attempt bytes must still hash to the attestation's
    raw-response digest, the attempt must be ``completed_structured`` (an
    escalated case never falsely backs a judged record), the batch must
    still be a canonically verified emitted batch, and — FIX-1 — the
    attestation must still match the lane's frozen visible-model authority
    exactly on slot, service, family, and user-visible model name. Mixing
    provenance modes inside one lane is refused, and a generic
    ``executor_attestation`` record can NEVER satisfy this gate.
    """
    manifest = verify_review_batches(protected_root, sampling=sampling)
    entries_by_digest = {
        str(entry["prompt_sha256"]): str(entry["batch_id"]) for entry in manifest["batches"]
    }
    # FIX-1: load once (validates the frozen visible-model authority).
    load_subscription_lane_authority(lane_root)
    for sample_id in sorted(records):
        record = records[sample_id]
        execution = record.execution
        if execution is None:
            raise ValueError(f"subscription_lane_requires_execution_receipt:{sample_id}")
        source = execution.identity_source
        if source == "executor_attestation":
            raise ValueError(f"subscription_lane_rejects_generic_executor_attestation:{sample_id}")
        if source == "provider_metadata":
            raise ValueError(f"subscription_lane_refuses_mixed_provenance:{sample_id}")
        if source != SUBSCRIPTION_PROVENANCE_MODE:
            raise ValueError(f"subscription_lane_unknown_provenance_source:{sample_id}")
        payload = getattr(execution, "subscription_attestation", None)
        if not isinstance(payload, dict) or not payload:
            raise ValueError(f"subscription_lane_missing_embedded_attestation:{sample_id}")
        attestation = SubscriptionReviewAttestation.model_validate(payload)
        # FIX-1: exact frozen visible-model lane authority.
        require_lane_visible_model_authority(lane_root, attestation)
        batch_id = entries_by_digest.get(attestation.request_batch_digest)
        if batch_id is None:
            raise ValueError(f"subscription_attestation_batch_not_canonical:{sample_id}")
        attempts_root = batch_attempts_dir(lane_root, batch_id)
        if not attempts_root.is_dir():
            raise ValueError(f"subscription_attestation_missing_preserved_attempt:{sample_id}")
        preserved_dir: Path | None = None
        for candidate in sorted(attempts_root.glob("attempt-*")):
            att_p = candidate / "attestation.json"
            if att_p.is_file() and json.loads(att_p.read_text()) == payload:
                preserved_dir = candidate
                break
        if preserved_dir is None:
            raise ValueError(f"subscription_attestation_disagrees_with_preserved:{sample_id}")
        raw = preserved_dir / "raw.resp"
        if not raw.is_file():
            raise ValueError(f"subscription_batch_raw_evidence_missing:{sample_id}")
        if not hmac.compare_digest(_sha256(raw.read_bytes()), attestation.raw_response_digest):
            raise ValueError(f"subscription_batch_raw_evidence_digest_mismatch:{sample_id}")
        record_path = preserved_dir / "attempt.json"
        if not record_path.is_file():
            # FIX-4 (round 3): a partial (uncommitted) attempt directory can
            # never back a frozen record.
            raise ValueError(f"subscription_attestation_missing_preserved_attempt:{sample_id}")
        attempt_record = SubscriptionBatchAttempt.model_validate(
            json.loads(record_path.read_text())
        )
        # The preserved attempt's outcome class must honestly match the
        # record it backs: judged records only ever back completed_structured
        # attempts; refused/malformed records back substantive attempts.
        attempt_outcome = attempt_record.outcome
        if record.outcome_status == "judged":
            if attempt_outcome != "completed_structured":
                raise ValueError(f"subscription_record_backs_non_completed_attempt:{sample_id}")
        elif record.outcome_status == "refused":
            if attempt_outcome != "substantive_refusal":
                raise ValueError(f"subscription_record_backs_non_refusal_attempt:{sample_id}")
        elif record.outcome_status == "malformed":
            if attempt_outcome != "substantive_malformed":
                raise ValueError(f"subscription_record_backs_non_malformed_attempt:{sample_id}")
        else:  # pragma: no cover - provider_error cannot reach this gate
            raise ValueError(f"subscription_record_backs_non_completed_attempt:{sample_id}")
        if not hmac.compare_digest(
            attempt_record.attestation_digest, attestation.attestation_digest
        ):
            raise ValueError(f"subscription_attempt_attestation_digest_mismatch:{sample_id}")
        if not subscription_mode_permitted(attestation.campaign_id, attestation.protocol_version):
            raise ValueError(f"subscription_ui_mode_not_opted_in_for_campaign:{sample_id}")
