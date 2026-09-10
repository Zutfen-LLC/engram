"""Shared builders for #206 tests: frame rows, receipts, split, identity."""

from __future__ import annotations

from datetime import UTC, datetime

from engram.assessment_schema import AssessmentContract
from evals.admission.schema import digest
from evals.calibration.fit import AssessmentExecutionReceipt
from evals.calibration.freeze import FrameRow, SplitManifest, TargetIdentity

NOW = datetime(2026, 9, 10, tzinfo=UTC)


def build_identity() -> TargetIdentity:
    return TargetIdentity(
        campaign_id="campaign",
        campaign_tooling_repo_sha="a" * 40,
        assessment_schema_version="engram.assessment.v1",
        assessment_code_version="assessment-engine-v1",
        prompt_version="engram.assess.1",
        provider_adapter="openai",
        provider_model="model",
        provider_config_digest="sha256:" + "9" * 64,
        provider_params={"temperature": 0},
        assessment_policy_version="assessment-selection-v1",
        calibration_artifact_schema_version="engram.calibration-profiles-v1",
        calibration_dataset_version="dataset-v2",
        label_guide_version="engram-calibration-guide-157-v1",
        canonicalization_version="assessment-evidence-manifest-v1",
        dimensions=("taxonomy", "retention", "epistemic"),
    )


def build_frame_rows(ids: tuple[str, ...]) -> list[FrameRow]:
    return [
        FrameRow(
            item_uuid=f"00000000-0000-0000-0000-{index:012d}",
            sample_id=sample_id,
            content_hash=digest(sample_id),
            content_norm_hash=digest(["norm", sample_id]),
            kind="fact",
            source_type="manual",
            review_status="active",
            assertion_mode="unknown",
            origin="unknown",
            risk="unknown",
            age_bucket="week",
            evidence_state="unknown",
            content_bytes=10,
            input_size_bucket="small",
        )
        for index, sample_id in enumerate(ids)
    ]


def build_split(
    ids: tuple[str, ...], *, dev: tuple[str, ...], holdout: tuple[str, ...]
) -> SplitManifest:
    return SplitManifest(
        campaign_id="campaign",
        sampling_manifest_digest="e" * 64,
        sampling_membership_digest=digest(sorted(ids)),
        split_seed="split",
        dev_fraction=0.6,
        grouping=("content_hash",),
        dev_ids=dev,
        holdout_ids=holdout,
        leakage_checks={},
    )


def build_receipts(
    ids: tuple[str, ...],
    frame: list[FrameRow],
    identity: TargetIdentity,
    contract: AssessmentContract,
) -> list[AssessmentExecutionReceipt]:
    by_id = {row.sample_id: row for row in frame}
    receipts: list[AssessmentExecutionReceipt] = []
    for sample_id in ids:
        base = {
            "sample_id": sample_id,
            "input_content_hash": by_id[sample_id].content_hash,
            "execution_id": f"execution-{sample_id}",
            "captured_at": NOW.isoformat(),
            "assessment": {
                "taxonomy": {"raw_value": 0.55},
                "retention": {"raw_value": 0.55},
                "epistemic": {"raw_value": 0.55},
                "suggested_kind": "fact",
            },
        }
        base["provider_request_digest"] = digest(
            {
                "sample_id": sample_id,
                "input_content_hash": by_id[sample_id].content_hash,
                "target_identity_digest": identity.identity_digest(),
                "assessment_contract_digest": digest(contract.model_dump(mode="json")),
            }
        )
        base["provider_response_digest"] = digest(["response", sample_id])
        placeholder = AssessmentExecutionReceipt.model_validate(
            {**base, "receipt_digest": "0" * 64}
        )
        receipts.append(
            placeholder.model_copy(update={"receipt_digest": placeholder.verified_payload_digest()})
        )
    return receipts
