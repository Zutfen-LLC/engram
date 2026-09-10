"""Provider-backed machine-verifiable execution identity (#206 FIX-R6-2).

Round-5's ``identity_source == "provider_metadata"`` accepted any executor
string as a "provider request/response ID" — provider-metadata-LABELED
evidence, not provider-metadata-VERIFIED evidence. This module closes that
gap with a provider-agnostic artifact design:

``ProviderMetadataArtifact``
    The raw provider metadata the executor captured at execution time,
    preserved verbatim (``raw_metadata``), digest-bound
    (``raw_metadata_sha256``), and stamped with the frozen adapter identity
    that knows how to read it.

``derive_provider_identity``
    The ONE mechanical derivation from the artifact bytes to the identity
    fields used in ``ExecutionEvidence``. The model identifier in the
    evidence is DERIVED FROM the metadata — never independently supplied.
    Re-run at observation, ingestion, freeze, load, and final ledger
    verification.

``verify_evidence_against_artifact``
    Fail closed unless an ``ExecutionEvidence``'s identity fields ARE the
    derivation of its own embedded, digest-bound provider metadata.

``attest_execution``
    The honest fallback (``identity_source == "executor_attestation"``):
    the environment cannot machine-verify identity. Preserved, but can
    never ground a frozen consensus lane, and can never masquerade as
    provider metadata — that path requires a digest-bound artifact whose
    bytes mechanically derive every identity field it claims.

Trust boundary (FIX-R6-2):

    provider metadata artifact  -> actual provider/model identity
    verified request item       -> actual campaign / prompt / reviewer
                                   config / case identity

    Both are then compared against the frozen ``ReviewerIdentity``. The
    provider is NOT trusted to report our internal prompt/config digests —
    those come from the byte-verified emitted request item.

This module deliberately has NO module-level import of
``evals.calibration.consensus`` (which imports this module): consensus
types are imported lazily inside functions so the artifact schema can live
beside the evidence schema without an import cycle.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self

import rfc8785
from pydantic import model_validator

from evals.admission.schema import Record

if TYPE_CHECKING:
    from evals.calibration.consensus import ExecutionEvidence, SlotName

# Frozen parser/adapter identity. Any change to how provider metadata is
# interpreted MUST change this string.
PROVIDER_METADATA_ADAPTER_VERSION: Literal["provider-metadata-adapter-206-v1"] = (
    "provider-metadata-adapter-206-v1"
)
PROVIDER_METADATA_ARTIFACT_SCHEMA: Literal["engram-calibration-provider-metadata-206-v1"] = (
    "engram-calibration-provider-metadata-206-v1"
)

# Identity fields that MUST be present in the raw provider metadata for the
# provider_metadata identity source to apply. The provider must expose the
# model that actually produced the response plus its request/response IDs.
_REQUIRED_METADATA_FIELDS: tuple[str, ...] = ("model", "request_id", "response_id")

# Canonical error code recorded for a genuinely malformed (unparseable)
# model response. FIX-R6-3: malformed is never an unchecked catch-all —
# every malformed record must carry exactly this code, re-derived from the
# raw bytes at every verification boundary.
CANONICAL_MALFORMED_ERROR_CODE: Literal["unparseable_response"] = "unparseable_response"


def _metadata_digest(raw_metadata: dict[str, Any]) -> str:
    return hashlib.sha256(rfc8785.dumps(raw_metadata)).hexdigest()


class ProviderMetadataArtifact(Record):
    """FIX-R6-2: the machine-verifiable provider metadata evidence.

    ``raw_metadata`` is the exact normalized metadata object the executor
    captured from the provider at execution time (API response envelope /
    header fields), preserved verbatim and digest-bound. The identity
    fields on ``ExecutionEvidence`` are derived FROM these bytes by the
    frozen adapter — never supplied alongside them.
    """

    metadata_schema: Literal["engram-calibration-provider-metadata-206-v1"] = (
        PROVIDER_METADATA_ARTIFACT_SCHEMA
    )
    adapter_version: str
    provider: str
    raw_metadata: dict[str, Any]
    raw_metadata_sha256: str

    @model_validator(mode="after")
    def artifact_contract(self) -> Self:
        if self.adapter_version != PROVIDER_METADATA_ADAPTER_VERSION:
            raise ValueError("provider_metadata_adapter_version_mismatch")
        if not self.provider:
            raise ValueError("provider_metadata_requires_provider")
        if not hmac.compare_digest(_metadata_digest(self.raw_metadata), self.raw_metadata_sha256):
            raise ValueError("provider_metadata_digest_mismatch")
        return self

    @classmethod
    def capture(
        cls,
        *,
        provider: str,
        raw_metadata: dict[str, Any],
    ) -> ProviderMetadataArtifact:
        """Capture raw provider metadata at execution time (digest-bound).

        Fails closed when the metadata cannot mechanically establish model
        identity (missing/empty ``model`` / ``request_id`` / ``response_id``)
        — the caller must then use ``attest_execution`` instead of claiming
        machine verification.
        """
        missing = [field for field in _REQUIRED_METADATA_FIELDS if field not in raw_metadata]
        if missing:
            raise ValueError("provider_metadata_missing_required_fields:" + ",".join(missing))
        for field in _REQUIRED_METADATA_FIELDS:
            value = raw_metadata[field]
            if not isinstance(value, str) or not value:
                raise ValueError(f"provider_metadata_field_must_be_nonempty_string:{field}")
        return cls(
            adapter_version=PROVIDER_METADATA_ADAPTER_VERSION,
            provider=provider,
            raw_metadata=raw_metadata,
            raw_metadata_sha256=_metadata_digest(raw_metadata),
        )

    def metadata_digest(self) -> str:
        return self.raw_metadata_sha256


def derive_provider_identity(artifact: ProviderMetadataArtifact) -> dict[str, str]:
    """Mechanically derive the provider identity from the artifact bytes.

    The frozen adapter (``PROVIDER_METADATA_ADAPTER_VERSION``) reads exactly
    ``model`` / ``request_id`` / ``response_id`` from the digest-bound raw
    metadata. The artifact validator re-runs on every derivation, so
    mutating the stored metadata fails its digest; mutating a derived field
    without mutating the metadata fails the derivation equality checks.
    """
    artifact.model_validate(artifact.model_dump(mode="json"))  # digest re-binding
    raw = artifact.raw_metadata
    return {
        "reported_model_identifier": str(raw["model"]),
        "provider_request_id": str(raw["request_id"]),
        "provider_response_id": str(raw["response_id"]),
    }


def verify_execution_provider_metadata(
    evidence: ExecutionEvidence,
    *,
    artifact: ProviderMetadataArtifact,
) -> None:
    """Fail closed unless the evidence identity IS the metadata derivation."""
    if artifact.adapter_version != PROVIDER_METADATA_ADAPTER_VERSION:
        raise ValueError("provider_metadata_adapter_version_mismatch")
    if evidence.identity_source != "provider_metadata":
        raise ValueError("provider_metadata_verification_requires_provider_metadata_source")
    derived = derive_provider_identity(artifact)
    if not hmac.compare_digest(
        evidence.actual_provider_model_identifier, derived["reported_model_identifier"]
    ):
        raise ValueError("provider_metadata_model_derivation_mismatch")
    if evidence.provider_request_id != derived["provider_request_id"]:
        raise ValueError("provider_metadata_request_id_derivation_mismatch")
    if evidence.provider_response_id != derived["provider_response_id"]:
        raise ValueError("provider_metadata_response_id_derivation_mismatch")


def verify_evidence_against_artifact(evidence: ExecutionEvidence) -> None:
    """Re-run the derivation against the evidence's EMBEDDED artifact.

    Used by the ``ExecutionEvidence`` schema validator: the embedded
    ``provider_metadata`` payload must validate as a digest-bound artifact
    and must mechanically derive the model/request/response identity the
    evidence claims. Arbitrary invented IDs can never satisfy this.
    """
    payload = evidence.provider_metadata
    if not isinstance(payload, dict) or not payload:
        raise ValueError("provider_metadata_identity_requires_metadata_artifact")
    artifact = ProviderMetadataArtifact.model_validate(payload)
    verify_execution_provider_metadata(evidence, artifact=artifact)


def provider_metadata_path(lane_root: Path, sample_id: str) -> Path:
    return lane_root / "provider-meta" / f"{sample_id}.json"


def publish_provider_metadata(
    lane_root: Path, sample_id: str, artifact: ProviderMetadataArtifact
) -> Path:
    """Preserve the artifact as protected per-sample evidence (exclusive)."""
    path = provider_metadata_path(lane_root, sample_id)
    payload = (json.dumps(artifact.model_dump(mode="json"), sort_keys=True) + "\n").encode()
    if path.exists():
        existing = json.loads(path.read_text())
        if existing != artifact.model_dump(mode="json"):
            raise ValueError("provider_metadata_artifact_conflict")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    from evals.calibration.review import write_protected_file

    write_protected_file(path, payload)
    return path


def load_provider_metadata(lane_root: Path, sample_id: str) -> ProviderMetadataArtifact | None:
    """Load the preserved artifact for one sample, if present."""
    path = provider_metadata_path(lane_root, sample_id)
    if not path.is_file():
        return None
    return ProviderMetadataArtifact.model_validate(json.loads(path.read_text()))


def build_execution_evidence(
    *,
    campaign_id: str,
    actual_reviewer_slot: SlotName,
    request_generation: int,
    executor_identity: str,
    request_item: dict[str, Any],
    provider_metadata: ProviderMetadataArtifact,
    executed_at: datetime | None = None,
    executor_status: Literal["completed", "provider_error"] = "completed",
) -> ExecutionEvidence:
    """FIX-R6-2: construct identity-bearing evidence ONLY from authorities.

    The actual model identity is DERIVED from the digest-bound provider
    metadata artifact; campaign / prompt / reviewer-config identity are
    taken from the byte-verified emitted request item (whose canonical
    request-line authority is proven by FIX-R6-1) — none of the ``actual_*``
    identity fields are free parameters. The provider does not necessarily
    report our internal digests, so those come from the verified request
    the execution answers.
    """
    from evals.calibration.consensus import FAMILY_BY_SLOT, ExecutionEvidence
    from evals.calibration.ingestion import request_item_digest

    request_campaign_id = str(request_item.get("campaign_id", ""))
    request_prompt_digest = str(request_item.get("prompt_digest", ""))
    request_config_digest = str(request_item.get("reviewer_config_digest", ""))
    if not request_campaign_id or not request_prompt_digest or not request_config_digest:
        raise ValueError("execution_evidence_requires_request_item_identity")
    derived = derive_provider_identity(provider_metadata)
    return ExecutionEvidence(
        campaign_id=request_campaign_id,
        actual_reviewer_slot=actual_reviewer_slot,
        actual_reviewer_family=FAMILY_BY_SLOT[actual_reviewer_slot],
        actual_provider_model_identifier=derived["reported_model_identifier"],
        actual_configuration_digest=request_config_digest,
        actual_prompt_digest=request_prompt_digest,
        request_generation=request_generation,
        request_item_digest=request_item_digest(request_item),
        executed_at=executed_at or datetime.now(UTC),
        executor_status=executor_status,
        executor_identity=executor_identity,
        identity_source="provider_metadata",
        provider_request_id=derived["provider_request_id"],
        provider_response_id=derived["provider_response_id"],
        provider_metadata=provider_metadata.model_dump(mode="json"),
    )


def attest_execution(
    *,
    campaign_id: str,
    actual_reviewer_slot: SlotName,
    actual_reviewer_family: str,
    actual_provider_model_identifier: str,
    actual_configuration_digest: str,
    actual_prompt_digest: str,
    request_generation: int,
    request_item_digest: str,
    executor_identity: str,
    executor_status: Literal["completed", "provider_error"],
    executed_at: datetime | None = None,
) -> ExecutionEvidence:
    """Honest executor attestation: identity the environment cannot
    machine-verify. Preserved as protected evidence but can NEVER ground a
    frozen consensus lane (``require_machine_verified_execution_identity``
    rejects it), and there is deliberately no way to turn an attestation
    into a ``provider_metadata`` claim: that path requires a digest-bound
    provider metadata artifact whose bytes derive every identity field.
    """
    from evals.calibration.consensus import FAMILY_BY_SLOT, ExecutionEvidence

    if FAMILY_BY_SLOT[actual_reviewer_slot] != actual_reviewer_family:
        raise ValueError("attested_family_does_not_match_slot")
    return ExecutionEvidence(
        campaign_id=campaign_id,
        actual_reviewer_slot=actual_reviewer_slot,
        actual_reviewer_family=actual_reviewer_family,
        actual_provider_model_identifier=actual_provider_model_identifier,
        actual_configuration_digest=actual_configuration_digest,
        actual_prompt_digest=actual_prompt_digest,
        request_generation=request_generation,
        request_item_digest=request_item_digest,
        executed_at=executed_at or datetime.now(UTC),
        executor_status=executor_status,
        executor_identity=executor_identity,
        identity_source="executor_attestation",
        provider_request_id=None,
        provider_response_id=None,
        provider_metadata=None,
    )
