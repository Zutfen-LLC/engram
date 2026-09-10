"""Practical provider-agnostic lane execution/ingestion workflow (#206 FIX-6).

The exact model invocation stays an EXTERNAL responsibility (Hermes executes
the frontier reviewers); this module is the complete mechanical handoff:

    model-lane-init      bind one lane to one frozen ReviewerIdentity
    model-lane-request   emit ONLY that lane's neutral cases + frozen
                         labeling instructions/schema (JSONL, resumable)
    (external model execution happens here — no credentials in this module)
    model-lane-ingest    mechanically ingest structured per-case or batch
                         (JSONL) responses into validated ModelReviewRecords
    model-lane-status    completion / missing / failure counts
    freeze-model-lane    freeze only after exact full membership (402)

Guarantees:

- one lane is bound to exactly one frozen ``ReviewerIdentity``; the binding
  file is exclusive-create and immutable;
- requests contain only the lane's neutral case input plus the frozen
  labeling instructions and response schema — no other lane's evidence;
- raw model response bytes are preserved in protected storage and their
  sha256 digest is recorded on the record (when a response exists);
- refusal / malformed / provider_error stay distinct orthogonal states
  (``absent`` parse status for missing responses);
- ingestion is append-only and resume-safe: previously accepted records are
  never replaced, duplicates are refused, and requests resume from the next
  missing sample;
- every accepted record is bound to the lane authority at ingest time
  (FIX-1), so another lane's output cannot be ingested here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import model_validator

from evals.admission.schema import Record
from evals.calibration.consensus import (
    CONSENSUS_PROTOCOL_VERSION,
    ExecutionReceipt,
    LaneFreeze,
    ModelReviewRecord,
    ReviewerIdentity,
)
from evals.calibration.freeze import LABEL_GUIDE_VERSION, SamplingManifest
from evals.calibration.model_lanes import (
    NeutralModelPacket,
    append_review_record,
    freeze_lane,
    load_lane_records,
)
from evals.calibration.provider_metadata import (
    CANONICAL_MALFORMED_ERROR_CODE,
    ProviderMetadataArtifact,
)
from evals.calibration.review import write_protected_file
from evals.calibration.reviewer_instructions import (
    RESPONSE_PARSER_VERSION,
    canonical_instruction_bundle,
    parse_model_response,
)

LANE_AUTHORITY_SCHEMA: Literal["engram-calibration-model-lane-authority-206-v1"] = (
    "engram-calibration-model-lane-authority-206-v1"
)
LANE_REQUEST_SCHEMA: Literal["engram-calibration-model-lane-request-206-v1"] = (
    "engram-calibration-model-lane-request-206-v1"
)
LANE_REQUEST_BATCH_SCHEMA: Literal["engram-calibration-model-lane-request-batch-206-v2"] = (
    "engram-calibration-model-lane-request-batch-206-v2"
)

# FIX-R4-6 / FIX-R5-5: the emitted instructions are the COMPLETE canonical
# semantic bundle (actual decision semantics for all five critical fields).
# PROVENANCE: the frozen 157 guide + admission handbook supply the closed
# vocabularies, the judge-independently principle, the ``unknown`` rule,
# retention/epistemic/consequence/abstention semantics, and the honor
# rules; the detailed PER-KIND definitions (fact/observation/decision/
# procedure/summary/doctrine/invariant/preference/diary_entry/unknown) and
# the full decision-rule wording are a NEW #206 reviewer operationalization
# (``REVIEWER_INSTRUCTIONS_VERSION``), frozen before execution and layered
# on the underlying guide — NOT text inherited from the old sources. Every
# emitted request embeds the complete bundle in full, and
# ``ReviewerIdentity.prompt_digest`` binds its exact digest.
LABELING_INSTRUCTIONS: dict[str, Any] = canonical_instruction_bundle(LABEL_GUIDE_VERSION)


def labeling_instructions_digest() -> str:
    """Digest of the EXACT frozen instruction material supplied to reviewers.

    FIX-R3 (prompt provenance): ``ReviewerIdentity.prompt_digest`` must equal
    this digest. A caller-provided digest cannot claim one prompt while
    ``model-lane-request`` emits another: the lane refuses to initialize (and
    refuses to load) against any reviewer identity whose prompt digest does
    not match the canonical instructions actually embedded in every request.

    FIX-R4-6: the canonical material is the full semantic bundle — changing
    any semantic rule changes this digest, and a reviewer identity carrying a
    stale/incomplete prompt digest can no longer initialize a lane.
    """
    from evals.calibration.consensus import digest_of

    return digest_of(LABELING_INSTRUCTIONS)


def request_item_digest(request: Mapping[str, Any]) -> str:
    """FIX-R4-1: canonical digest over one emitted request item.

    Computed over the exact canonical JSON of the request object written to
    the immutable batch file, so a response can be bound to the exact bytes
    of the request that produced it.
    """
    import json

    from evals.calibration.consensus import digest_of

    return digest_of(json.loads(json.dumps(request, sort_keys=True)))


def _request_batch_manifest_path(batch_path: Path) -> Path:
    """Manifest path for one request batch (module-level canonical form)."""
    return batch_path.with_suffix(".manifest.json")


# FIX-R6-1: the exact canonical key set of an emitted request line. A
# request line carrying ANY additional (or missing) key is non-canonical —
# no extra evidence may appear and no frozen case evidence may be modified.
CANONICAL_REQUEST_KEYS: frozenset[str] = frozenset(
    {
        "lane_request_schema",
        "protocol_version",
        "campaign_id",
        "sampling_manifest_digest",
        "source_packet_digest",
        "neutral_packet_sha256",
        "reviewer_slot",
        "reviewer_family",
        "provider_model_identifier",
        "reviewer_config_digest",
        "prompt_digest",
        "label_guide_version",
        "case_index",
        "sample_id",
        "case",
        "labeling_instructions",
    }
)


# FIX-R6-1 performance guard (corrected per Round-6 NO-GO): memoize the
# verified request registry keyed on the CONTENT-AUTHENTICATING identity
# (stable path/role + SHA-256 of the current bytes) of every authority
# input — the lane authority, the retained neutral packet, and every
# batch/manifest pair. Stat identity (mtime_ns, size) is deliberately NOT
# trusted: a file owner can rewrite same-length bytes and restore the
# original mtime with os.utime(), leaving (path, mtime_ns, size) identical
# while the bytes differ. The cache key therefore hashes the actual bytes
# on every authority pass, so a changed byte makes a cache hit impossible
# and full canonical verification always re-runs. Security claim: a cache
# hit means the EXACT current bytes were canonically verified in this
# process. The expensive work being memoized is repeated parsing and
# reconstruction of the large embedded instruction bundles — not the
# cheap read+hash that authenticates the bytes.
_REGISTRY_CACHE: dict[
    tuple[tuple[str, str], ...], dict[str, dict[int, tuple[str, str, str, dict[str, Any]]]]
] = {}

# Test-only instrumentation: counts authority-pass cache hits/misses so
# tests prove the fast path is taken without relying on runtime timing.
_REGISTRY_CACHE_STATS: dict[str, int] = {"hit": 0, "miss": 0}


def _file_identity(path: Path) -> tuple[str, str]:
    """Content-authenticating file identity: stable path + SHA-256 of the
    CURRENT bytes. Never derived from (mtime, size) alone."""
    payload = path.read_bytes()
    return (str(path), hashlib.sha256(payload).hexdigest())


def _load_request_registry(
    lane_root: Path,
) -> dict[str, dict[int, tuple[str, str, str, dict[str, Any]]]]:
    """Scan VERIFIED request batches into a provenance registry.

    FIX-R5-2: every retained ``lane-requests-*.jsonl`` + manifest pair is
    passed through the canonical ``verify_request_batch`` byte verifier
    FIRST — a manifest assertion can never register a request that was not
    actually emitted, because every manifest claim (generation, reviewer
    identity, neutral packet, request SHA, pending membership, item digests,
    case indexes) is re-derived from the actual batch bytes.

    FIX-R6-1: when the lane carries its immutable authority file (the real
    ``LaneSession`` path), every batch is additionally verified against the
    AUTHORITY — each request line must be the canonical projection of the
    frozen reviewer/lane identity, the frozen campaign/sampling/source
    packet bindings, the byte-verified neutral packet cases, and the
    canonical labeling instructions. A lane with an authority but no
    retained neutral-packet bytes fails closed.

    Returns ``sample_id -> generation -> (request_item_digest,
    reviewer_identity_digest, neutral_packet_sha256, request_line)``. A
    sample pending in several generations (resume re-emission) has one
    entry per generation; responses bind to one exact generation.
    """
    authority: LaneAuthority | None = None
    packet: NeutralModelPacket | None = None
    cache_key: tuple[tuple[str, str], ...] | None = None
    authority_path = lane_root / "lane.json"
    if authority_path.is_file():
        retained = lane_root / "neutral-packet.json"
        batches = sorted(lane_root.glob("lane-requests-*.jsonl"))
        input_paths = [authority_path, retained, *batches]
        input_paths.extend(_request_batch_manifest_path(p) for p in batches)
        cache_key = tuple(_file_identity(p) for p in input_paths if p.exists())
        cached = _REGISTRY_CACHE.get(cache_key)
        if cached is not None:
            _REGISTRY_CACHE_STATS["hit"] += 1
            return cached
        _REGISTRY_CACHE_STATS["miss"] += 1
        authority = LaneAuthority.model_validate(json.loads(authority_path.read_text()))
        if not retained.is_file():
            raise ValueError("lane_retained_neutral_packet_missing")
        payload = retained.read_bytes()
        if not hmac.compare_digest(
            hashlib.sha256(payload).hexdigest(), authority.neutral_packet_sha256
        ):
            raise ValueError("neutral_packet_sha_mismatch")
        packet = NeutralModelPacket.model_validate(json.loads(payload))
    registry: dict[str, dict[int, tuple[str, str, str, dict[str, Any]]]] = {}
    for batch_path in sorted(lane_root.glob("lane-requests-*.jsonl")):
        manifest_path = _request_batch_manifest_path(batch_path)
        verify_request_batch(
            batch_path, manifest_path=manifest_path, lane_authority=authority, neutral_packet=packet
        )
        manifest = json.loads(manifest_path.read_text())
        generation = int(manifest["generation"])
        batch_lines = {
            str(json.loads(line)["sample_id"]): json.loads(line)
            for line in batch_path.read_text().splitlines()
            if line.strip()
        }
        for sample_id, item in manifest.get("request_items", {}).items():
            entry = (
                str(item["request_item_digest"]),
                str(manifest["reviewer_identity_digest"]),
                str(manifest["neutral_packet_sha256"]),
                batch_lines[sample_id],
            )
            existing = registry.setdefault(sample_id, {}).get(generation)
            if existing is not None and existing != entry:
                raise ValueError("request_batch_manifest_generation_conflict")
            registry[sample_id][generation] = entry
    if cache_key is not None:
        _REGISTRY_CACHE[cache_key] = registry
    return registry


def _verify_request_line_canonical(
    request: Mapping[str, Any],
    *,
    lane_authority: LaneAuthority,
    neutral_packet: NeutralModelPacket,
) -> None:
    """FIX-R6-1: prove one request line IS the canonical projection of the
    frozen lane authority, frozen reviewer identity, byte-verified neutral
    packet, and canonical labeling instructions.

    Internal self-consistency (a recomputed manifest after a paired rewrite)
    is NOT authority: every semantic field is compared against its frozen
    external authority, the instruction bundle must BE the canonical frozen
    bundle (with its digest equal to the reviewer's frozen ``prompt_digest``),
    and the case object must equal ``neutral_packet.cases[case_index]``
    field-exactly at the frozen packet index.
    """
    sample_id = str(request.get("sample_id", ""))
    reviewer = lane_authority.reviewer
    if request.get("campaign_id") != lane_authority.campaign_id:
        raise ValueError(f"request_line_campaign_not_frozen_authority:{sample_id}")
    if request.get("sampling_manifest_digest") != lane_authority.sampling_manifest_digest:
        raise ValueError(f"request_line_sampling_manifest_not_frozen_authority:{sample_id}")
    if request.get("source_packet_digest") != lane_authority.source_packet_digest:
        raise ValueError(f"request_line_source_packet_not_frozen_authority:{sample_id}")
    if request.get("neutral_packet_sha256") != lane_authority.neutral_packet_sha256:
        raise ValueError(f"request_line_neutral_packet_not_frozen_authority:{sample_id}")
    if request.get("reviewer_slot") != reviewer.reviewer_slot:
        raise ValueError(f"request_line_slot_not_frozen_reviewer:{sample_id}")
    if request.get("reviewer_family") != reviewer.reviewer_family:
        raise ValueError(f"request_line_family_not_frozen_reviewer:{sample_id}")
    if request.get("provider_model_identifier") != reviewer.provider_model_identifier:
        raise ValueError(f"request_line_model_not_frozen_reviewer:{sample_id}")
    if request.get("reviewer_config_digest") != reviewer.reviewer_config_digest:
        raise ValueError(f"request_line_config_not_frozen_reviewer:{sample_id}")
    if request.get("prompt_digest") != reviewer.prompt_digest:
        raise ValueError(f"request_line_prompt_not_frozen_reviewer:{sample_id}")
    if request.get("label_guide_version") != LABEL_GUIDE_VERSION:
        raise ValueError(f"request_line_label_guide_not_frozen:{sample_id}")
    # Frozen instructions: the actual bundle must BE the canonical bundle,
    # and its digest must equal the reviewer's frozen prompt digest — not a
    # digest-to-digest comparison within the mutable request/manifest pair.
    if request.get("labeling_instructions") != LABELING_INSTRUCTIONS:
        raise ValueError(f"request_line_instructions_not_canonical_bundle:{sample_id}")
    if request.get("prompt_digest") != labeling_instructions_digest():
        raise ValueError(f"request_line_prompt_not_canonical_instructions:{sample_id}")
    # Frozen case projection: field-exact equality with the byte-verified
    # neutral packet case at the frozen packet index.
    case_index = request.get("case_index")
    frozen_case_index = {
        str(case["sample_id"]): index for index, case in enumerate(neutral_packet.cases)
    }
    if not isinstance(case_index, int) or frozen_case_index.get(sample_id) != case_index:
        raise ValueError(f"request_line_case_index_not_frozen_packet_index:{sample_id}")
    frozen_case = neutral_packet.cases[case_index]
    if not isinstance(frozen_case, dict) or request.get("case") != dict(frozen_case):
        raise ValueError(f"request_line_case_not_frozen_neutral_case:{sample_id}")


def verify_request_batch(
    batch_path: Path,
    *,
    manifest_path: Path | None = None,
    lane_authority: LaneAuthority | None = None,
    neutral_packet: NeutralModelPacket | None = None,
) -> dict[str, Any]:
    """FIX-R5-2 / FIX-R6-1: canonical request-batch byte verifier.

    Re-derives EVERY manifest claim from the actual ``.jsonl`` batch bytes:

    1. filename generation matches ``manifest.generation``;
    2. the batch file exists;
    3. ``SHA256(batch bytes) == manifest.request_sha256`` (recomputed, never
       trusted);
    4. every non-empty line parses as request JSON;
    5. every line carries the frozen campaign/protocol/lane identity;
    6. every line carries the expected neutral packet SHA;
    7. every line carries the frozen prompt/config identity;
    8. sample IDs are unique inside the generation;
    9. ``pending_sample_ids`` exactly equal the emitted lines in order;
    10. ``request_items`` exactly equal the mechanically recomputed item
        digests/case indexes;
    11. no extra manifest request item exists;
    12. no emitted line is omitted from the manifest.

    FIX-R6-1 (canonical authority mode): when ``lane_authority`` and
    ``neutral_packet`` are supplied — the real lane path, used by the
    registry, freeze, load, and final-ledger verification — the verifier
    additionally proves every request line IS the canonical projection of
    the frozen authorities, not merely self-consistent with its manifest:

    - exact canonical key set (no extra evidence, nothing dropped);
    - ``campaign_id`` / ``sampling_manifest_digest`` / ``source_packet_digest``
      / ``neutral_packet_sha256`` equal the frozen ``LaneAuthority``;
    - ``reviewer_slot`` / ``reviewer_family`` / ``provider_model_identifier``
      / ``reviewer_config_digest`` / ``prompt_digest`` equal the frozen
      ``ReviewerIdentity``;
    - ``label_guide_version`` equals the frozen guide version;
    - ``labeling_instructions`` IS the canonical frozen instruction bundle
      (exact object equality) and its digest equals the reviewer's frozen
      ``prompt_digest`` — never merely a digest-to-digest comparison inside
      the mutable request/manifest pair;
    - ``case_index`` is the frozen neutral-packet index of ``sample_id`` and
      ``case`` equals ``neutral_packet.cases[case_index]`` field-exactly.

    A paired pre-execution rewrite (alter ``case`` or instructions, then
    recompute every internal digest) therefore fails here: internal
    self-consistency is not authority.

    Returns the verified manifest. Any violation fails closed.
    """
    if manifest_path is None:
        manifest_path = _request_batch_manifest_path(batch_path)
    if not batch_path.is_file():
        raise ValueError("request_batch_file_missing")
    if not manifest_path.is_file():
        raise ValueError("request_batch_manifest_missing")
    filename_generation = batch_path.stem.rsplit("-", 1)[-1]
    if not filename_generation.isdigit():
        raise ValueError("request_batch_filename_generation_unparseable")
    payload = batch_path.read_bytes()
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("lane_request_batch_schema") != LANE_REQUEST_BATCH_SCHEMA:
        raise ValueError("request_batch_manifest_schema_mismatch")
    generation = manifest.get("generation")
    if not isinstance(generation, int) or generation != int(filename_generation):
        raise ValueError("request_batch_generation_mismatch")
    if not hmac.compare_digest(
        hashlib.sha256(payload).hexdigest(), str(manifest.get("request_sha256", ""))
    ):
        raise ValueError("request_batch_sha_mismatch")
    canonical = lane_authority is not None and neutral_packet is not None
    seen_ids: set[str] = set()
    line_sample_ids: list[str] = []
    recomputed_items: dict[str, dict[str, Any]] = {}
    for line in payload.decode().splitlines():
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            raise ValueError("request_batch_line_not_valid_json") from None
        if not isinstance(request, dict):
            raise ValueError("request_batch_line_not_valid_json")
        # FIX-R6-1: exact canonical key set — no extra evidence may appear
        # and no frozen case evidence may be modified or dropped.
        if set(request) != CANONICAL_REQUEST_KEYS:
            raise ValueError("request_batch_line_noncanonical_keys")
        # (5) frozen campaign/protocol/lane identity on every line
        if request.get("lane_request_schema") != LANE_REQUEST_SCHEMA:
            raise ValueError("request_batch_line_schema_mismatch")
        if request.get("protocol_version") != CONSENSUS_PROTOCOL_VERSION:
            raise ValueError("request_batch_line_protocol_mismatch")
        # (6) expected neutral packet SHA on every line
        if not hmac.compare_digest(
            str(request.get("neutral_packet_sha256", "")),
            str(manifest.get("neutral_packet_sha256", "")),
        ):
            raise ValueError("request_batch_line_neutral_packet_mismatch")
        # (7) frozen prompt/config identity on every line; the reviewer
        # identity digest over the frozen identity fields must match the
        # manifest's reviewer binding.
        if not hmac.compare_digest(
            str(request.get("prompt_digest", "")),
            str(manifest.get("reviewer_prompt_digest", "")),
        ):
            raise ValueError("request_batch_line_prompt_digest_mismatch")
        sample_id = request.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id:
            raise ValueError("request_batch_line_requires_sample_id")
        if sample_id in seen_ids:  # (8) unique inside the generation
            raise ValueError("request_batch_duplicate_sample_id")
        seen_ids.add(sample_id)
        line_sample_ids.append(sample_id)  # (9) in emitted order
        if canonical:
            # FIX-R6-1: prove the line against the FROZEN AUTHORITIES.
            assert lane_authority is not None and neutral_packet is not None
            _verify_request_line_canonical(
                request,
                lane_authority=lane_authority,
                neutral_packet=neutral_packet,
            )
        recomputed_items[sample_id] = {
            "request_item_digest": request_item_digest(request),
            "case_index": request.get("case_index"),
        }
    # (9) pending_sample_ids exactly equal the emitted lines in order
    pending = manifest.get("pending_sample_ids")
    if pending != line_sample_ids:
        raise ValueError("request_batch_pending_membership_mismatch")
    # (10)+(11) request_items exactly equal the recomputed items, no extras
    items = manifest.get("request_items")
    if not isinstance(items, dict) or items != recomputed_items:
        raise ValueError("request_batch_items_mismatch")
    # (12) no emitted line omitted from the manifest — implied by the exact
    # dict equality above (every emitted sample must appear as an item).
    if not isinstance(manifest, dict):
        raise ValueError("request_batch_manifest_not_an_object")
    # reviewer identity digest binding used by the registry consumers
    if not str(manifest.get("reviewer_identity_digest", "")):
        raise ValueError("request_batch_requires_reviewer_identity_digest")
    return manifest


def observe_execution(
    lane_root: Path,
    *,
    campaign_id: str,
    actual_reviewer_slot: str,
    actual_reviewer_family: str,
    actual_provider_model_identifier: str,
    actual_configuration_digest: str,
    actual_prompt_digest: str,
    sample_id: str,
    request_generation: int,
    executor_identity: str,
    executor_status: Literal["completed", "provider_error"],
    identity_source: Literal["provider_metadata", "executor_attestation"],
    provider_metadata_artifact: ProviderMetadataArtifact | None = None,
    executed_at: datetime | None = None,
) -> ExecutionReceipt:
    """FIX-R5-1 / FIX-R6-2: build a truthful execution receipt from OBSERVED
    evidence.

    The executor wrapper (Hermes, or the test harness) calls this AFTER
    executing one request emitted by ``model-lane-request``. The expected
    lane ``ReviewerIdentity`` is deliberately NOT a parameter: no helper can
    manufacture an "actual" identity by copying the intended one. Only the
    emitted-request binding is looked up from the retained VERIFIED request
    batches. Ingestion then compares this observed identity EXACTLY against
    the frozen lane identity — a mismatch fails closed.

    FIX-R6-2: for ``identity_source == "provider_metadata"`` the
    ``provider_metadata_artifact`` (the digest-bound raw provider metadata
    captured at execution time) is REQUIRED — arbitrary invented
    request/response ID strings can no longer claim machine verification;
    the identity fields on the evidence are the mechanical derivation of
    the artifact bytes (and the artifact is preserved per-sample under
    ``provider-meta/``). The provider does not report our internal
    prompt/config digests, so those come from the verified request item the
    execution answers. An environment that cannot expose model identity in
    provider metadata must use ``identity_source == "executor_attestation"``
    — which can never freeze a consensus lane.
    """
    from evals.calibration.consensus import ExecutionEvidence

    registry = _load_request_registry(lane_root)
    emitted = registry.get(sample_id, {}).get(request_generation)
    if emitted is None:
        raise ValueError("response_request_not_emitted")
    _item_digest, _identity, _packet, request_line = emitted
    if identity_source == "provider_metadata":
        if provider_metadata_artifact is None:
            raise ValueError("provider_metadata_identity_requires_metadata_artifact")
        from evals.calibration.provider_metadata import (
            derive_provider_identity,
            publish_provider_metadata,
        )

        derived = derive_provider_identity(provider_metadata_artifact)
        if actual_provider_model_identifier != derived["reported_model_identifier"]:
            raise ValueError("provider_metadata_model_derivation_mismatch")
        publish_provider_metadata(lane_root, sample_id, provider_metadata_artifact)
        provider_request_id = derived["provider_request_id"]
        provider_response_id = derived["provider_response_id"]
    else:
        if provider_metadata_artifact is not None:
            raise ValueError("executor_attestation_must_not_claim_provider_metadata")
        provider_request_id = None
        provider_response_id = None
    evidence = ExecutionEvidence(
        campaign_id=campaign_id,
        actual_reviewer_slot=actual_reviewer_slot,  # type: ignore[arg-type]
        actual_reviewer_family=actual_reviewer_family,
        actual_provider_model_identifier=actual_provider_model_identifier,
        actual_configuration_digest=actual_configuration_digest,
        actual_prompt_digest=actual_prompt_digest,
        request_generation=request_generation,
        request_item_digest=_item_digest,
        executed_at=executed_at or datetime.now(UTC),
        executor_status=executor_status,
        executor_identity=executor_identity,
        identity_source=identity_source,
        provider_request_id=provider_request_id,
        provider_response_id=provider_response_id,
        provider_metadata=(
            provider_metadata_artifact.model_dump(mode="json")
            if provider_metadata_artifact is not None
            else None
        ),
    )
    return ExecutionReceipt.from_evidence(evidence)


def verify_reviewer_prompt_binding(reviewer: ReviewerIdentity) -> None:
    """Fail closed unless the reviewer identity binds the real frozen prompt."""
    expected = labeling_instructions_digest()
    if reviewer.prompt_digest != expected:
        raise ValueError("reviewer_prompt_digest_does_not_match_frozen_instructions")
    if reviewer.label_guide_version != LABEL_GUIDE_VERSION:
        raise ValueError("reviewer_label_guide_version_mismatch")


def verify_lane_request_bindings(
    lane_root: Path,
    reviewer: ReviewerIdentity,
    records: Mapping[str, ModelReviewRecord],
    *,
    campaign_id: str,
    neutral_packet_sha256: str,
) -> None:
    """FIX-R4-1 / FIX-R5-3: prove every accepted record answers an ACTUAL
    emitted request.

    Runs at lane freeze, frozen-lane load, and (through
    ``validate_lane_provenance_with_raw``) every correlation/report, queue,
    and final-ledger boundary: each record's ``(request_generation,
    request_item_digest)`` must exist in a retained VERIFIED request batch
    for this lane (FIX-R5-2: the batch bytes — not the manifest assertions
    — are the authority), bound to this exact reviewer identity and this
    exact neutral packet. A lane with accepted records but no emitted
    requests is structurally impossible and fails closed.
    """
    registry = _load_request_registry(lane_root)
    if not registry:
        raise ValueError("lane_has_no_emitted_requests")
    identity_digest = reviewer.lane_identity_digest()
    for sample_id in sorted(records):
        record = records[sample_id]
        emitted = registry.get(sample_id, {}).get(record.request_generation or -1)
        if emitted is None:
            raise ValueError(f"record_request_not_emitted:{sample_id}")
        item_digest, manifest_identity, packet_sha, _request_line = emitted
        if not hmac.compare_digest(record.request_item_digest or "", item_digest):
            raise ValueError(f"record_request_item_digest_mismatch:{sample_id}")
        if manifest_identity != identity_digest:
            raise ValueError(f"record_request_bound_to_other_reviewer:{sample_id}")
        if not hmac.compare_digest(packet_sha, neutral_packet_sha256):
            raise ValueError(f"record_request_bound_to_other_neutral_packet:{sample_id}")
        if campaign_id != record.campaign_id:
            raise ValueError(f"record_campaign_mismatch:{sample_id}")


def require_machine_verified_execution_identity(
    records: Mapping[str, ModelReviewRecord],
    *,
    lane_root: Path | None = None,
) -> None:
    """FIX-R5-1 / FIX-R6-2: a lane can only freeze as a valid consensus
    reviewer lane when every accepted record's ACTUAL executor identity was
    machine-verified (``identity_source == provider_metadata``).

    An honest executor attestation (``executor_attestation``) is preserved
    as protected evidence but can NEVER ground a frozen consensus lane:
    unverified actual identity cannot freeze.

    FIX-R6-2: machine verification is not a label. When ``lane_root`` is
    supplied (the freeze/load/ledger paths), each record's embedded
    provider metadata artifact is re-checked against the PRESERVED
    per-sample artifact bytes (``provider-meta/<sample>.json``): the
    artifact must still exist, still digest-bind its raw metadata, and
    still mechanically derive the identity the evidence claims. Mutating
    the preserved artifact (or the embedded copy) after ingestion fails
    every downstream boundary.
    """
    from evals.calibration.provider_metadata import (
        ProviderMetadataArtifact,
        load_provider_metadata,
        verify_execution_provider_metadata,
    )

    for sample_id in sorted(records):
        record = records[sample_id]
        execution = record.execution
        if execution is None or execution.identity_source != "provider_metadata":
            raise ValueError(f"lane_freeze_requires_machine_verified_executor_identity:{sample_id}")
        if lane_root is None:
            continue
        preserved = load_provider_metadata(lane_root, sample_id)
        if preserved is None:
            raise ValueError(f"machine_verified_identity_missing_preserved_artifact:{sample_id}")
        embedded = execution.provider_metadata
        if not isinstance(embedded, dict):
            raise ValueError(f"machine_verified_identity_missing_embedded_artifact:{sample_id}")
        if preserved.model_dump(mode="json") != embedded:
            raise ValueError(f"provider_metadata_artifact_disagrees_with_preserved:{sample_id}")
        artifact = ProviderMetadataArtifact.model_validate(embedded)
        if execution.evidence is not None:
            verify_execution_provider_metadata(execution.evidence, artifact=artifact)


class LaneAuthority(Record):
    """The immutable lane binding written by ``model-lane-init``."""

    authority_schema: Literal["engram-calibration-model-lane-authority-206-v1"] = (
        LANE_AUTHORITY_SCHEMA
    )
    protocol_version: str
    campaign_id: str
    reviewer: ReviewerIdentity
    sampling_manifest_digest: str
    source_packet_digest: str
    # FIX-R3-3: the exact neutral packet bytes this lane may request against.
    # Bound at init from the independently retained neutral packet manifest;
    # request emission re-reads and re-hashes the packet file and refuses any
    # byte disagreement BEFORE any reviewer request is emitted.
    neutral_packet_sha256: str

    @model_validator(mode="after")
    def authority_contract(self) -> Self:
        if self.protocol_version != CONSENSUS_PROTOCOL_VERSION:
            raise ValueError("protocol_version_mismatch")
        return self


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def load_neutral_packet_verified(
    neutral_packet_path: Path,
    *,
    manifest_path: Path,
    expected_packet_name: str | None = None,
) -> tuple[NeutralModelPacket, str]:
    """Load the neutral packet ONLY after byte-level manifest verification.

    FIX-R3-3: the packet's own embedded metadata (``sampling_manifest_digest``,
    ``source_packet_digest``) is NOT proof of its contents. Authority comes
    from the independently retained ``neutral-packet-manifest.json`` mapping
    the packet file name to its SHA-256: the exact file bytes are re-read and
    re-hashed here, and any disagreement (edited case content, reordered /
    missing / extra cases producing different bytes) fails closed.
    """
    import hashlib as _hashlib

    payload = neutral_packet_path.read_bytes()
    actual_digest = _hashlib.sha256(payload).hexdigest()
    manifest = json.loads(manifest_path.read_text())
    name = neutral_packet_path.name
    if expected_packet_name is not None:
        name = expected_packet_name
    expected_digest = manifest.get(name)
    if not isinstance(expected_digest, str) or not expected_digest:
        raise ValueError("neutral_packet_manifest_missing_entry")
    if not hmac.compare_digest(actual_digest, expected_digest):
        raise ValueError("neutral_packet_sha_mismatch")
    packet = NeutralModelPacket.model_validate(json.loads(payload))
    return packet, actual_digest


class LaneSession:
    """One reviewer lane bound to its frozen authority.

    Directory layout mirrors ``model_lanes``: the campaign protected root
    contains ``lanes/<slot>/`` record files; the authority file lives at
    ``lanes/<slot>/lane.json``. All operations derive the reviewer identity
    from the immutable authority — a record can never be persisted under a
    different identity through this session.

    FIX-R3-3: the authority binds the exact neutral packet SHA-256 (from the
    independently retained manifest). Every request emission re-reads the
    packet bytes, re-hashes them against the authority digest, verifies the
    sampling/source-packet binding, and proves EXACT frozen membership and
    order — all BEFORE any reviewer request is emitted.

    FIX-R3-6: request batches are immutable generations
    (``lane-requests-000001.jsonl``, ``lane-requests-000002.jsonl``, ...).
    Each emission writes the NEXT exclusive-create batch containing only the
    still-pending cases; previous batches are never overwritten, so the real
    resume sequence (emit -> partial ingest -> emit again) works.
    """

    def __init__(self, protected_root: Path, reviewer_slot: str):
        self.protected_root = protected_root
        self.reviewer_slot = reviewer_slot
        authority_path = self.lane_root / "lane.json"
        if not authority_path.exists():
            raise ValueError("lane_not_initialized")
        self.authority = LaneAuthority.model_validate(json.loads(authority_path.read_text()))
        if self.authority.reviewer.reviewer_slot != reviewer_slot:
            raise ValueError("lane_authority_slot_mismatch")
        verify_reviewer_prompt_binding(self.authority.reviewer)
        self.reviewer = self.authority.reviewer
        self.campaign_id = self.authority.campaign_id
        self.sampling_manifest_digest = self.authority.sampling_manifest_digest
        self.source_packet_digest = self.authority.source_packet_digest
        self.neutral_packet_sha256 = self.authority.neutral_packet_sha256

    @property
    def lane_root(self) -> Path:
        return self.protected_root / "lanes" / self.reviewer_slot

    @classmethod
    def init(
        cls,
        protected_root: Path,
        *,
        reviewer: ReviewerIdentity,
        campaign_id: str,
        sampling: SamplingManifest,
        source_packet_digest: str,
        neutral_packet_path: Path,
        neutral_packet_manifest: Path,
    ) -> LaneSession:
        """Bind one lane to one frozen reviewer identity (exclusive-create).

        FIX-R3 (prompt provenance): the reviewer identity must bind the exact
        frozen labeling instructions (``prompt_digest`` ==
        ``labeling_instructions_digest()``).

        FIX-R3-3: the exact neutral packet bytes are verified against the
        independently retained manifest and their SHA-256 is frozen into the
        lane authority before anything else happens.
        """
        verify_reviewer_prompt_binding(reviewer)
        _, packet_sha = load_neutral_packet_verified(
            neutral_packet_path, manifest_path=neutral_packet_manifest
        )
        # FIX-R6-1: retain the exact verified packet bytes inside the lane
        # root so every later canonical request-line verification (registry
        # load, freeze, frozen-lane load, final ledger) re-derives request
        # case projections from byte-verified frozen packet bytes rather
        # than re-trusting the external packet file.
        retained = protected_root / "lanes" / reviewer.reviewer_slot / "neutral-packet.json"
        if not retained.exists():
            write_protected_file(retained, neutral_packet_path.read_bytes())
        elif hashlib.sha256(retained.read_bytes()).hexdigest() != packet_sha:
            raise ValueError("lane_retained_neutral_packet_sha_mismatch")
        authority = LaneAuthority(
            protocol_version=CONSENSUS_PROTOCOL_VERSION,
            campaign_id=campaign_id,
            reviewer=reviewer,
            sampling_manifest_digest=sampling.manifest_digest(),
            source_packet_digest=source_packet_digest,
            neutral_packet_sha256=packet_sha,
        )
        payload = (json.dumps(authority.model_dump(mode="json"), sort_keys=True) + "\n").encode()
        lane_path = protected_root / "lanes" / reviewer.reviewer_slot / "lane.json"
        write_protected_file(lane_path, payload)
        return cls(protected_root, reviewer.reviewer_slot)

    # -- request emission ---------------------------------------------------

    def _check_packet(
        self, packet: NeutralModelPacket, sampling: SamplingManifest, packet_sha: str
    ) -> None:
        """FIX-R3-3: byte-level + binding + exact-membership/order checks."""
        if sampling.manifest_digest() != self.sampling_manifest_digest:
            raise ValueError("sampling_manifest_mismatch")
        # the exact packet bytes must hash to the authority-frozen digest
        if not hmac.compare_digest(packet_sha, self.neutral_packet_sha256):
            raise ValueError("neutral_packet_sha_mismatch")
        if packet.sampling_manifest_digest != self.sampling_manifest_digest:
            raise ValueError("lane_packet_sampling_manifest_mismatch")
        if str(getattr(packet, "source_packet_digest", "")) != self.source_packet_digest:
            raise ValueError("lane_packet_source_packet_mismatch")
        if packet.guide_version != LABEL_GUIDE_VERSION:
            raise ValueError("lane_packet_guide_version_mismatch")
        # EXACT frozen membership and order: the reviewer sees precisely the
        # frozen sample sequence, no more, no less, in the frozen order.
        packet_ids = tuple(str(case["sample_id"]) for case in packet.cases)
        if packet_ids != tuple(sampling.sample_ids):
            if len(packet_ids) == len(sampling.sample_ids):
                raise ValueError("neutral_packet_membership_order_mismatch")
            raise ValueError("neutral_packet_membership_mismatch")

    def _next_request_batch_path(self) -> Path:
        """Next immutable generation: lane-requests-000001.jsonl, -000002, ..."""
        existing = sorted(self.lane_root.glob("lane-requests-*.jsonl"))
        sequence = 0
        for path in existing:
            suffix = path.stem.split("-")[-1]
            if suffix.isdigit():
                sequence = max(sequence, int(suffix))
        return self.lane_root / f"lane-requests-{sequence + 1:06d}.jsonl"

    @staticmethod
    def _request_batch_manifest_path(batch_path: Path) -> Path:
        return _request_batch_manifest_path(batch_path)

    def emit_requests(
        self,
        packet_path: Path,
        *,
        sampling: SamplingManifest,
        manifest_path: Path | None = None,
    ) -> Path:
        """Emit ONLY this lane's pending neutral case requests (JSONL).

        FIX-R3-3: the neutral packet is loaded through byte-level manifest
        verification (never trusting its embedded metadata), bound against
        the lane authority digest, and proven to carry the EXACT frozen
        membership and order — BEFORE any request line is constructed.

        FIX-R3-6: writes the NEXT immutable batch generation containing only
        cases still missing an accepted record. Earlier batches are never
        touched, so partial ingestion followed by re-emission works.
        """
        if manifest_path is None:
            manifest_path = packet_path.parent / "neutral-packet-manifest.json"
        packet, packet_sha = load_neutral_packet_verified(packet_path, manifest_path=manifest_path)
        self._check_packet(packet, sampling, packet_sha)
        accepted = load_lane_records(self.protected_root, self.reviewer.reviewer_slot)
        lines: list[str] = []
        for index, case in enumerate(packet.cases):
            sample_id = case["sample_id"]
            if sample_id in accepted:
                continue  # resume: never re-request accepted evidence
            request = {
                "lane_request_schema": LANE_REQUEST_SCHEMA,
                "protocol_version": CONSENSUS_PROTOCOL_VERSION,
                "campaign_id": self.campaign_id,
                "sampling_manifest_digest": self.sampling_manifest_digest,
                "source_packet_digest": self.source_packet_digest,
                "neutral_packet_sha256": self.neutral_packet_sha256,
                "reviewer_slot": self.reviewer.reviewer_slot,
                "reviewer_family": self.reviewer.reviewer_family,
                "provider_model_identifier": self.reviewer.provider_model_identifier,
                "reviewer_config_digest": self.reviewer.reviewer_config_digest,
                "prompt_digest": self.reviewer.prompt_digest,
                "label_guide_version": LABEL_GUIDE_VERSION,
                "case_index": index,
                "sample_id": sample_id,
                "case": case,
                "labeling_instructions": LABELING_INSTRUCTIONS,
            }
            lines.append(json.dumps(request, sort_keys=True))
        out_path = self._next_request_batch_path()
        payload = ("\n".join(lines) + "\n").encode() if lines else b""
        write_protected_file(out_path, payload)
        # A request batch is immutable evidence too: bind the exact generation,
        # reviewer authority, neutral packet, accepted state, pending membership
        # and emitted bytes. This makes resume operational without permitting an
        # old batch to be silently reinterpreted after partial ingestion.
        # FIX-R4-1: each request item is individually digestible — the manifest
        # records the canonical digest of every emitted request line so every
        # accepted response can be bound to the exact request that produced it.
        generation = int(out_path.stem.rsplit("-", 1)[1])
        manifest = {
            "lane_request_batch_schema": LANE_REQUEST_BATCH_SCHEMA,
            "generation": generation,
            "reviewer_identity_digest": self.reviewer.lane_identity_digest(),
            "reviewer_prompt_digest": self.reviewer.prompt_digest,
            "neutral_packet_sha256": self.neutral_packet_sha256,
            "accepted_record_digests": {
                sample_id: accepted[sample_id].record_digest() for sample_id in sorted(accepted)
            },
            "pending_sample_ids": [json.loads(line)["sample_id"] for line in lines],
            "request_items": {
                json.loads(line)["sample_id"]: {
                    "request_item_digest": request_item_digest(json.loads(line)),
                    "case_index": json.loads(line)["case_index"],
                }
                for line in lines
            },
            "request_sha256": hashlib.sha256(payload).hexdigest(),
        }
        write_protected_file(
            self._request_batch_manifest_path(out_path),
            (json.dumps(manifest, sort_keys=True) + "\n").encode(),
        )
        return out_path

    # -- record construction (single path) -----------------------------------

    def build_record(
        self,
        response: Mapping[str, Any],
        *,
        sampling: SamplingManifest,
    ) -> ModelReviewRecord:
        """Construct a ``ModelReviewRecord`` from one structured response.

        The reviewer identity on the record comes from the LANE AUTHORITY,
        never from the response — another lane's output cannot be ingested
        through this path even if its payload names a different slot/family.

        FIX-R4-1: the response must carry an execution receipt derived from
        OBSERVED ``ExecutionEvidence`` (actual provider/model/config
        identity + the exact emitted request item it answers). The ACTUAL
        executor identity is compared EXACTLY against the frozen
        ``ReviewerIdentity`` — never derived from lane config — and the
        referenced request item must exist in one of this lane's VERIFIED
        immutable request-batch generations, bound to this reviewer identity
        and this neutral packet. A response with no matching emitted request
        fails closed.

        FIX-R5-4: for ``executor_status == completed`` the substantive
        outcome (judged / refused / malformed) is DERIVED ENTIRELY from the
        preserved raw response bytes by the frozen deterministic parser. The
        external response envelope carries only ``sample_id``,
        ``execution``, and ``raw_response`` — a caller-supplied ``outcome``
        or ``judgment`` key is REJECTED, so no wrapper can select or relabel
        the refusal-vs-malformed-vs-judged distinction. For
        ``executor_status == provider_error`` no raw response may be present
        and the outcome is derived from the execution evidence.
        """
        if sampling.manifest_digest() != self.sampling_manifest_digest:
            raise ValueError("sampling_manifest_mismatch")
        sample_id = str(response.get("sample_id", ""))
        if not sample_id:
            raise ValueError("response_requires_sample_id")
        if sample_id not in set(sampling.sample_ids):
            raise ValueError("record_sample_not_in_sampling_manifest")
        captured_at = response.get("captured_at") or datetime.now(UTC).isoformat()
        # --- FIX-R5-4: the wrapper cannot select the substantive outcome ----
        for forbidden in ("outcome", "judgment"):
            if forbidden in response:
                raise ValueError(f"response_envelope_must_not_carry_{forbidden}")
        # --- FIX-R4-1: execution receipt + request binding -------------------
        receipt_payload = response.get("execution")
        if not isinstance(receipt_payload, Mapping):
            raise ValueError("response_requires_execution_receipt")
        execution = ExecutionReceipt.model_validate(dict(receipt_payload))
        if not execution.matches_reviewer_identity(self.reviewer, self.campaign_id):
            raise ValueError("execution_receipt_identity_mismatch")
        registry = _load_request_registry(self.lane_root)
        emitted = registry.get(sample_id, {}).get(execution.request_generation)
        if emitted is None:
            raise ValueError("response_request_not_emitted")
        item_digest, identity_digest, packet_sha, _request_line = emitted
        if not hmac.compare_digest(execution.request_item_digest, item_digest):
            raise ValueError("response_request_item_digest_mismatch")
        if identity_digest != self.reviewer.lane_identity_digest():
            raise ValueError("response_request_bound_to_other_reviewer")
        if not hmac.compare_digest(packet_sha, self.neutral_packet_sha256):
            raise ValueError("response_request_bound_to_other_neutral_packet")
        common: dict[str, Any] = {
            "protocol_version": CONSENSUS_PROTOCOL_VERSION,
            "campaign_id": self.campaign_id,
            "sampling_manifest_digest": self.sampling_manifest_digest,
            "source_packet_digest": self.source_packet_digest,
            "sample_id": sample_id,
            "reviewer_slot": self.reviewer.reviewer_slot,
            "reviewer_family": self.reviewer.reviewer_family,
            "provider_model_identifier": self.reviewer.provider_model_identifier,
            "reviewer_config_digest": self.reviewer.reviewer_config_digest,
            "prompt_digest": self.reviewer.prompt_digest,
            "label_guide_version": LABEL_GUIDE_VERSION,
            "captured_at": captured_at,
            "execution": execution,
            "request_generation": execution.request_generation,
            "request_item_digest": execution.request_item_digest,
        }
        if execution.executor_status == "provider_error":
            # No response bytes exist; the outcome is DERIVED from the
            # execution evidence, and no raw response may be carried.
            raw_response = response.get("raw_response")
            if raw_response:
                raise ValueError("provider_error_without_response_must_not_carry_raw_response")
            error_code = response.get("error_code")
            if not error_code:
                raise ValueError("failed_review_requires_error_code")
            return ModelReviewRecord(
                **common,
                parse_status="absent",
                outcome_status="provider_error",
                reviewer_confidence="unknown",
                judgment=None,
                raw_response_digest=None,
                error_code=str(error_code),
            )
        # executor_status == completed: the substantive outcome is DERIVED
        # from the exact raw bytes by the frozen parser — nothing caller-
        # supplied can influence judged vs refused vs malformed.
        raw_response = response.get("raw_response")
        if raw_response is None or not isinstance(raw_response, str):
            raise ValueError("completed_execution_requires_raw_response_bytes")
        raw_bytes = raw_response.encode()
        raw_digest = _sha256_text(raw_response)
        parsed = parse_model_response(raw_bytes, expected_sample_id=sample_id)
        if parsed.classification == "judged":
            assert parsed.judgment is not None
            return ModelReviewRecord(
                **common,
                parse_status="parsed",
                outcome_status="judged",
                parser_version=RESPONSE_PARSER_VERSION,
                reviewer_confidence=parsed.judgment.reviewer_confidence,
                judgment=parsed.judgment,
                raw_response_digest=raw_digest,
                error_code=None,
            )
        if parsed.classification == "refused":
            assert parsed.error_code is not None
            return ModelReviewRecord(
                **common,
                parse_status="malformed",
                outcome_status="refused",
                reviewer_confidence="unknown",
                judgment=None,
                raw_response_digest=raw_digest,
                error_code=parsed.error_code,
            )
        return ModelReviewRecord(
            **common,
            parse_status="malformed",
            outcome_status="malformed",
            reviewer_confidence="unknown",
            judgment=None,
            raw_response_digest=raw_digest,
            # FIX-R6-3: the canonical malformed error code — never a
            # wrapper-supplied token; re-derived from the bytes at every
            # verification boundary.
            error_code=CANONICAL_MALFORMED_ERROR_CODE,
        )

    # -- ingestion ------------------------------------------------------------

    def ingest_response(
        self,
        response: Mapping[str, Any],
        *,
        sampling: SamplingManifest,
    ) -> ModelReviewRecord:
        """Validate + append ONE structured response (fail closed, FIX-1 bound).

        FIX-R2-4 interruption-safe ordering: the raw response bytes are
        written EXCLUSIVELY FIRST (never overwriting existing evidence), the
        record is constructed/validated against that exact digest, and only
        then is the accepted record published. A crash can therefore leave at
        most an orphan raw file without an accepted record — never an
        accepted record lacking its bound raw bytes. Re-ingesting the same
        sample after such a crash reuses the identical raw bytes
        (byte-identical rewrite is refused, not silently overwritten).
        """
        record = self.build_record(response, sampling=sampling)
        raw_response = response.get("raw_response")
        if raw_response:
            raw_path = self.lane_root / "raw" / f"{record.sample_id}.resp"
            payload = raw_response.encode()
            if raw_path.exists():
                # Never silently overwrite raw model evidence: the orphan
                # must hash identically to what this response claims.
                if hashlib.sha256(raw_path.read_bytes()).hexdigest() != (
                    record.raw_response_digest
                ):
                    raise ValueError("raw_response_orphan_digest_conflict")
            else:
                write_protected_file(raw_path, payload)
        append_review_record(
            record,
            self.protected_root,
            reviewer=self.reviewer,
            campaign_id=self.campaign_id,
            sampling=sampling,
            source_packet_digest=self.source_packet_digest,
        )
        return record

    def ingest_jsonl(
        self,
        jsonl_path: Path,
        *,
        sampling: SamplingManifest,
    ) -> dict[str, Any]:
        """Ingest a batch JSONL of response objects (one per line).

        Returns per-outcome accepted counts, refused duplicates, and errors
        with line numbers. Accepted lines persist exclusively; a failed line
        never rolls back previously accepted evidence (resume-safe).
        """
        if sampling.manifest_digest() != self.sampling_manifest_digest:
            raise ValueError("sampling_manifest_mismatch")
        accepted = {"judged": 0, "refused": 0, "malformed": 0, "provider_error": 0}
        duplicates = 0
        errors: list[dict[str, Any]] = []
        for line_number, line in enumerate(jsonl_path.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise ValueError("response_line_must_be_object")
                record = self.ingest_response(payload, sampling=sampling)
            except ValueError as exc:
                message = str(exc)
                if "review_record_already_accepted" in message:
                    duplicates += 1
                else:
                    errors.append({"line": line_number, "error": message})
                continue
            accepted[record.outcome_status] += 1
        return {
            "accepted": accepted,
            "accepted_total": sum(accepted.values()),
            "duplicates_refused": duplicates,
            "errors": errors,
        }

    # -- status / freeze -------------------------------------------------------

    def status(self, sampling: SamplingManifest) -> dict[str, Any]:
        """Completion / missing / failure counts for this lane."""
        if sampling.manifest_digest() != self.sampling_manifest_digest:
            raise ValueError("sampling_manifest_mismatch")
        records = load_lane_records(self.protected_root, self.reviewer.reviewer_slot)
        expected = list(sampling.sample_ids)
        missing = [sid for sid in expected if sid not in records]
        counts = {"judged": 0, "refused": 0, "malformed": 0, "provider_error": 0}
        for record in records.values():
            counts[record.outcome_status] += 1
        return {
            "reviewer_slot": self.reviewer.reviewer_slot,
            "expected": len(expected),
            "accepted": len(records),
            "missing": len(missing),
            "next_missing": missing[:50],
            "outcome_counts": counts,
            "complete": not missing,
        }

    def freeze(self, sampling: SamplingManifest) -> LaneFreeze:
        """Freeze the lane; requires exact full frozen membership."""
        return freeze_lane(
            protected_root=self.protected_root,
            reviewer=self.reviewer,
            campaign_id=self.campaign_id,
            sampling=sampling,
            source_packet_digest=self.source_packet_digest,
        )
