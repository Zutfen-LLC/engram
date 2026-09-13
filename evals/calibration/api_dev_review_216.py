"""Canonical DEV-only direct-provider reviewer runner for ENG-CALIBRATION-001K."""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from evals.calibration.api_reviewer_216 import (
    TRANSPORT_ENVELOPE,
    DirectReviewerAuthority216,
    DirectReviewerRunner216,
    reviewer_routes_216,
    verify_direct_reviewer_authority_216,
)
from evals.calibration.campaign_001k import CAMPAIGN_ID_001K
from evals.calibration.campaign_001k_stage_authority import verify_stage_authority
from evals.calibration.consensus import (
    CAMPAIGN_216_FAMILY_BY_SLOT,
    REVIEWER_SLOTS,
    ReviewerIdentity,
    digest_of,
)
from evals.calibration.freeze import SamplingManifest
from evals.calibration.ingestion import LaneSession, labeling_instructions_digest
from evals.calibration.model_lanes import freeze_lane
from evals.calibration.reviewer_instructions import RESPONSE_PARSER_VERSION


def _reviewer(slot: str) -> ReviewerIdentity:
    route = reviewer_routes_216()[slot]
    return ReviewerIdentity(
        reviewer_slot=slot,  # type: ignore[arg-type]
        reviewer_family=CAMPAIGN_216_FAMILY_BY_SLOT[slot],
        campaign_id=CAMPAIGN_ID_001K,
        provider_model_identifier=route.model,
        reviewer_config_digest=digest_of(
            {
                "transport": route.transport,
                "endpoint": route.endpoint,
                "model": route.model,
                "routing": route.provider_preferences,
            }
        ),
        prompt_digest=labeling_instructions_digest(),
    )


def _authority(
    session: LaneSession, *, target_digest: str, membership_digest: str
) -> DirectReviewerAuthority216:
    route = reviewer_routes_216()[session.reviewer.reviewer_slot]
    return DirectReviewerAuthority216(
        reviewer=session.reviewer,
        transport=route.transport,
        endpoint=route.endpoint,
        requested_model=route.model,
        routing_constraints=route.provider_preferences,
        credential_name=route.credential_name,
        target_identity_digest=target_digest,
        sampling_manifest_digest=session.sampling_manifest_digest,
        membership_digest=membership_digest,
        source_packet_digest=session.source_packet_digest,
        prompt_digest=session.reviewer.prompt_digest,
        transport_envelope_digest=digest_of(TRANSPORT_ENVELOPE),
        generation_params={},
        extractor_version=route.extractor_version,
        parser_version=RESPONSE_PARSER_VERSION,
    )


def _preflight(protected_root: Path) -> tuple[SamplingManifest, Path, str, Any]:
    sampling = SamplingManifest.model_validate(
        json.loads((protected_root / "dev-sampling-manifest.json").read_text())
    )
    source_packet = protected_root / f"{CAMPAIGN_ID_001K}-dev-v1.blind.json"
    source_digest = hashlib.sha256(source_packet.read_bytes()).hexdigest()
    stage = verify_stage_authority(
        protected_root=protected_root, sampling=sampling, source_packet_digest=source_digest
    )
    stage.require_capability()
    reuse = json.loads((protected_root / "reuse-manifest.json").read_text())
    if (
        stage.stage != "dev"
        or len(sampling.sample_ids) != 102
        or set(sampling.sample_ids) & set(reuse["holdout_ids"])
    ):
        raise ValueError("direct_api_reviewer_requires_exact_dev_102_zero_holdout")
    return sampling, source_packet, source_digest, stage


def run_api_dev_review(
    protected_root: Path,
    *,
    dry_run: bool = False,
    credentials: dict[str, str] | None = None,
) -> dict[str, object]:
    """Execute only canonical pending DEV requests; dry run transmits zero bytes."""
    sampling, _source_packet, source_digest, stage = _preflight(protected_root)
    supplied = (
        credentials
        if credentials is not None
        else {name: os.environ.get(name, "") for name in {"OPENROUTER_API_KEY", "ZAI_API_KEY"}}
    )
    if not all(supplied.get(route.credential_name) for route in reviewer_routes_216().values()):
        raise ValueError("direct_api_reviewer_required_credential_missing")
    report: dict[str, object] = {
        "campaign_id": CAMPAIGN_ID_001K,
        "stage": "dev",
        "logical_cases": 102,
        "reviewer_lanes": 3,
        "planned_logical_calls": 306,
        "holdout": 0,
        "transmitted_requests": 0,
        "identities": {
            slot: {
                "transport": route.transport,
                "endpoint": route.endpoint,
                "model": route.model,
                "routing": route.provider_preferences,
            }
            for slot, route in reviewer_routes_216().items()
        },
    }
    if dry_run:
        return report
    neutral = protected_root / f"{CAMPAIGN_ID_001K}-dev-v1.neutral.json"
    manifest = protected_root / "neutral-packet-manifest.json"
    sessions: dict[str, LaneSession] = {}
    runners: dict[str, DirectReviewerRunner216] = {}
    batches: dict[str, Path] = {}
    for slot in REVIEWER_SLOTS:
        reviewer = _reviewer(slot)
        session = LaneSession.init(
            protected_root,
            reviewer=reviewer,
            campaign_id=CAMPAIGN_ID_001K,
            sampling=sampling,
            source_packet_digest=source_digest,
            neutral_packet_path=neutral,
            neutral_packet_manifest=manifest,
            provenance_mode="direct_api_provenance",
        )
        sessions[slot] = session
        authority = _authority(
            session,
            target_digest=stage.target_identity_digest,
            membership_digest=stage.membership_digest,
        )
        verify_direct_reviewer_authority_216(
            authority,
            sampling=sampling,
            stage=stage,
            source_packet_digest=source_digest,
            reviewer=reviewer,
        )
        runners[slot] = DirectReviewerRunner216(session, authority, credentials=supplied)
        batches[slot] = session.emit_requests(neutral, sampling=sampling, manifest_path=manifest)

    def execute(slot: str) -> tuple[str, int]:
        for line in batches[slot].read_text().splitlines():
            runners[slot].review_request_line(line)
            runners[slot].ingest_accepted(line, sampling=sampling)
        frozen = freeze_lane(
            protected_root=protected_root,
            reviewer=sessions[slot].reviewer,
            campaign_id=CAMPAIGN_ID_001K,
            sampling=sampling,
            source_packet_digest=source_digest,
        )
        return slot, len(frozen.sample_ids)

    with ThreadPoolExecutor(max_workers=3) as pool:
        result = dict(pool.map(execute, REVIEWER_SLOTS))
    attempts_root = protected_root / "lanes"
    receipts = list(attempts_root.glob("*/api-reviewer/attempts/*/attempt-*/attempt.json"))
    receipt_values = [json.loads(path.read_text()) for path in receipts]
    report["transmitted_requests"] = len(receipt_values)
    report["pre_response_transport_failures"] = sum(
        receipt.get("failure_class") == "transport_pre_response" for receipt in receipt_values
    )
    report["http_non_2xx_attempts"] = sum(
        receipt.get("http_response_received") and not 200 <= int(receipt["http_status"]) < 300
        for receipt in receipt_values
    )
    report["retryable_http_attempts"] = sum(
        receipt.get("failure_class") == "retryable_http" for receipt in receipt_values
    )
    report["non_retryable_http_attempts"] = sum(
        receipt.get("failure_class") == "non_retryable_http" for receipt in receipt_values
    )
    report["mechanical_retry_attempts"] = report["pre_response_transport_failures"]
    report["structural_retry_attempts"] = sum(
        receipt.get("failure_class") == "structural_format" for receipt in receipt_values
    )
    accepted_judged = sum(
        receipt.get("accepted") and receipt["attempt"]["outcome_status"] == "judged"
        for receipt in receipt_values
    )
    accepted_refused = sum(
        receipt.get("accepted") and receipt["attempt"]["outcome_status"] == "refused"
        for receipt in receipt_values
    )
    report["accepted_judged"] = accepted_judged
    report["accepted_refused"] = accepted_refused
    report["unresolved_samples"] = 306 - accepted_judged - accepted_refused

    report["completed_logical_reviews"] = sum(result.values())
    report["lanes"] = {slot: {"accepted": result[slot], "frozen": True} for slot in REVIEWER_SLOTS}
    return report
