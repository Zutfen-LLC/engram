"""#209 subscription-UI reviewer-lane provenance tests (round 2, synthetic only).

Covers the four NO-GO correction areas plus the original round's required
proofs. Every fixture is synthetic; no real Claude/ChatGPT/z.ai/OpenRouter
review executes anywhere in this file.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from evals.calibration import subscription_ui
from evals.calibration.consensus import (
    CONSENSUS_PROTOCOL_VERSION,
    REVIEWER_SLOTS,
    ExecutionEvidence,
    SlotName,
)
from evals.calibration.freeze import SamplingManifest, protected_frame_digest
from evals.calibration.ingestion import (
    LaneSession,
    init_subscription_lane,
    labeling_instructions_digest,
    lane_provenance_mode,
)
from evals.calibration.model_lanes import (
    NeutralModelPacket,
    freeze_lane,
    load_lane_records,
)
from evals.calibration.review import _packet_file_payload, write_protected_file
from evals.calibration.subscription_ui import (
    BATCH_MAX_CASES,
    BATCH_MAX_SERIALIZED_BYTES,
    BATCH_RESULT_FIELDS,
    SERVICE_BY_SLOT,
    SUBSCRIPTION_MODEL_BY_SLOT,
    SubscriptionLaneAuthority,
    SubscriptionReviewAttestation,
    attempt_attestation_path,
    attempt_raw_path,
    batch_prompt_path,
    build_attestation,
    build_subscription_lane_authority,
    import_batch_response,
    load_attempt,
    load_subscription_lane_authority,
    next_outstanding_batch,
    prepare_subscription_campaign,
    require_lane_visible_model_authority,
    subscription_mode_permitted,
    subscription_reviewer_identity,
    verify_review_batches,
)
from tests.test_calibration_206_helpers import build_frame_rows

NOW = datetime(2026, 9, 11, tzinfo=UTC)
CAMPAIGN = "eng-calibration-001f"
SOURCE_DIGEST = "f" * 64
CONFIG_DIGEST = "a" * 64
IDS = tuple(f"s{i:03d}" for i in range(1, 11))  # 10 synthetic cases
VISIBLE_MODEL_BY_SLOT = {
    "model_a": "Claude Opus 4.8",
    "model_b": "GPT-Astra 2.4",
    "model_c": "GLM-5.3-Max",
}
OPERATOR = "test-operator"
REPO_ROOT = Path(subscription_ui.__file__).resolve().parents[2]
REAL_PROTECTED = Path.home() / ".local/share/engram/evals/202/protected"

CONF_BY_SLOT = {"model_a": "high", "model_b": "medium", "model_c": "low"}
EPI_BY_SLOT = {
    "model_a": "weakly_supported",
    "model_b": "adequately_supported",
    "model_c": "unverifiable",
}


def _sampling(ids: tuple[str, ...] = IDS) -> SamplingManifest:
    frame_rows = build_frame_rows(ids)
    return SamplingManifest(
        campaign_id=CAMPAIGN,
        target_identity_digest="1" * 64,
        frame_digest=protected_frame_digest(frame_rows),
        snapshot_sha256="3" * 64,
        snapshot_as_of=NOW,
        sampling_seed="seed",
        inclusion_rules=("rule",),
        exclusion_rules=(),
        source_row_counts={"eligible_frame": len(ids)},
        stratum_counts={"all": len(ids)},
        coverage_dimensions={},
        sample_ids=ids,
        sample_hashes=tuple(hashlib.sha256(sid.encode()).hexdigest() for sid in ids),
    )


def _packet(ids: tuple[str, ...] = IDS) -> NeutralModelPacket:
    cases = [
        {
            "sample_id": sid,
            "content": f"content-{sid}",
            "governed_kind": "fact",
            "source_type": "manual",
            "review_status": "active",
            "assertion_mode": "unknown",
            "origin": "unknown",
            "risk": "unknown",
            "evidence_state": "unknown",
            "age_days": 5,
            "age_bucket": "lt_7d",
            "input_size_bucket": "small",
        }
        for sid in ids
    ]
    return NeutralModelPacket(
        packet_id="campaign-blind-v2",
        sampling_manifest_digest=_sampling(ids).manifest_digest(),
        guide_version="engram-calibration-guide-157-v1",
        reviewer_hint="neutral_model_review",
        cases=cases,
        source_packet_digest=SOURCE_DIGEST,
    )


def _write_packet(tmp_path: Path, packet: NeutralModelPacket) -> tuple[Path, Path]:
    packet_dir = tmp_path / "packet"
    packet_dir.mkdir(parents=True, exist_ok=True)
    payload = _packet_file_payload(packet)
    packet_path = packet_dir / f"{packet.packet_id}.neutral.json"
    write_protected_file(packet_path, payload)
    manifest = packet_dir / "neutral-packet-manifest.json"
    write_protected_file(
        manifest,
        (
            json.dumps(
                {packet_path.name: hashlib.sha256(payload).hexdigest()},
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode(),
    )
    return packet_path, manifest


@pytest.fixture()
def campaign(tmp_path: Path):
    """A fully prepared three-lane subscription campaign (NO manual
    emit_requests call — preparation does it, proving FIX-4)."""
    sampling = _sampling()
    packet = _packet()
    packet_path, packet_manifest = _write_packet(tmp_path, packet)
    prompt_digest = labeling_instructions_digest()
    reviewers = {}
    for slot in REVIEWER_SLOTS:
        reviewers[slot] = subscription_reviewer_identity(
            slot, reviewer_config_digest=CONFIG_DIGEST, prompt_digest=prompt_digest
        )
        init_subscription_lane(
            tmp_path,
            reviewer=reviewers[slot],
            campaign_id=CAMPAIGN,
            sampling=sampling,
            source_packet_digest=SOURCE_DIGEST,
            neutral_packet_path=packet_path,
            neutral_packet_manifest=packet_manifest,
            user_visible_model_name=VISIBLE_MODEL_BY_SLOT[slot],
            operator_reference=OPERATOR,
        )
    prepared = prepare_subscription_campaign(
        tmp_path,
        campaign_id=CAMPAIGN,
        sampling=sampling,
        source_packet_digest=SOURCE_DIGEST,
        neutral_packet_path=packet_path,
        neutral_packet_manifest=packet_manifest,
        max_cases=4,
    )
    return {
        "root": tmp_path,
        "sampling": sampling,
        "packet": packet,
        "packet_path": packet_path,
        "packet_manifest": packet_manifest,
        "reviewers": reviewers,
        "prepared": prepared,
    }


def _manifest(campaign: dict) -> list[dict]:
    return verify_review_batches(
        campaign["root"], sampling=campaign["sampling"], source_packet_digest=SOURCE_DIGEST
    )["batches"]


def _batch_response(entry: dict, slot: str, *, mutate=None) -> str:
    results = []
    for sid in entry["sample_ids"]:
        result = {
            "sample_id": str(sid),
            "expected_kind": "fact",
            "retention_value": "retain",
            "epistemic_state": EPI_BY_SLOT[slot],
            "consequence": "low",
            "acceptable_abstention": "no",
            "reviewer_confidence": CONF_BY_SLOT[slot],
        }
        results.append(result)
    payload = {"results": results}
    if mutate is not None:
        mutate(payload)
    return json.dumps(payload, indent=1)


def _import(campaign: dict, slot: str, entry: dict, raw: str, **kwargs):
    return import_batch_response(
        campaign["root"],
        reviewer_slot=slot,
        batch_id=str(entry["batch_id"]),
        raw_response=raw,
        sampling=campaign["sampling"],
        source_packet_digest=SOURCE_DIGEST,
        **kwargs,
    )


# =============================================================================
# FIX-1 — frozen visible-model lane authority
# =============================================================================


def test_lane_init_requires_visible_model_and_operator(tmp_path: Path):
    sampling = _sampling()
    packet = _packet()
    packet_path, packet_manifest = _write_packet(tmp_path, packet)
    reviewer = subscription_reviewer_identity(
        "model_a",
        reviewer_config_digest=CONFIG_DIGEST,
        prompt_digest=labeling_instructions_digest(),
    )
    with pytest.raises(TypeError):
        init_subscription_lane(  # type: ignore[call-arg]
            tmp_path,
            reviewer=reviewer,
            campaign_id=CAMPAIGN,
            sampling=sampling,
            source_packet_digest=SOURCE_DIGEST,
            neutral_packet_path=packet_path,
            neutral_packet_manifest=packet_manifest,
        )
    with pytest.raises(ValueError, match="subscription_lane_requires_user_visible_model_name"):
        init_subscription_lane(
            tmp_path,
            reviewer=reviewer,
            campaign_id=CAMPAIGN,
            sampling=sampling,
            source_packet_digest=SOURCE_DIGEST,
            neutral_packet_path=packet_path,
            neutral_packet_manifest=packet_manifest,
            user_visible_model_name="",
            operator_reference=OPERATOR,
        )
    with pytest.raises(ValueError, match="subscription_lane_requires_operator_reference"):
        init_subscription_lane(
            tmp_path,
            reviewer=reviewer,
            campaign_id=CAMPAIGN,
            sampling=sampling,
            source_packet_digest=SOURCE_DIGEST,
            neutral_packet_path=packet_path,
            neutral_packet_manifest=packet_manifest,
            user_visible_model_name="Claude Opus 4.8",
            operator_reference="",
        )


def test_visible_model_frozen_at_init_before_any_output(campaign):
    for slot in REVIEWER_SLOTS:
        authority = load_subscription_lane_authority(campaign["root"] / "lanes" / slot)
        assert authority.reviewer_slot == slot
        assert authority.reviewer_family == subscription_ui.FAMILY_BY_SLOT[slot]
        assert authority.service == SERVICE_BY_SLOT[slot]
        assert authority.user_visible_model_name == VISIBLE_MODEL_BY_SLOT[slot]
        assert authority.operator_reference == OPERATOR
        # Frozen identity binding: slot->service->family mapping is not caller choice.
        forged = dict(authority.payload())
        forged["service"] = "chatgpt"
        forged["authority_digest"] = "0" * 64
        with pytest.raises(ValueError):
            SubscriptionLaneAuthority.model_validate(forged)


def test_import_takes_no_service_or_model_claims():
    """The import boundary cannot even be handed a substituted visible model:
    the API has no service/model/operator parameters at all (FIX-1)."""
    import inspect

    signature = inspect.signature(import_batch_response)
    for forbidden in ("service", "user_visible_model_name", "model_name", "operator_reference"):
        assert forbidden not in signature.parameters


# --- FIX-1 (round 3): wrong-family visible model must fail at INITIALIZATION ---

_WRONG_FAMILY_LABELS_BY_SLOT = {
    "model_a": [
        "Claude Sonnet 4.5",
        "Claude Haiku 4.5",
        "Claude 4 Bash",
        "GPT-Astra 2.4",
    ],
    "model_b": [
        "GPT-4o",
        "GPT-4.1",
        "GPT-5",
        "o4-mini",
        "ChatGPT 5.2 Standard",
    ],
    "model_c": [
        "GLM-5.3-Air",
        "GLM-5.3-Flash",
        "GLM-4.7",
        "GLM-5 Max",  # missing the 5.3 version identity
    ],
}


def _init_label(tmp_path: Path, slot: str, label: str):
    """Drive the REAL init path (not a later attestation) with one label."""
    sampling = _sampling()
    packet = _packet()
    packet_path, packet_manifest = _write_packet(tmp_path / f"init-{abs(hash(label))}", packet)
    return init_subscription_lane(
        tmp_path / f"init-{abs(hash(label))}",
        reviewer=subscription_reviewer_identity(
            cast("SlotName", slot),
            reviewer_config_digest=CONFIG_DIGEST,
            prompt_digest=labeling_instructions_digest(),
        ),
        campaign_id=CAMPAIGN,
        sampling=sampling,
        source_packet_digest=SOURCE_DIGEST,
        neutral_packet_path=packet_path,
        neutral_packet_manifest=packet_manifest,
        user_visible_model_name=label,
        operator_reference=OPERATOR,
    )


@pytest.mark.parametrize(
    ("slot", "label"),
    [
        ("model_a", "Claude Opus 4.8"),
        ("model_a", "claude opus 4.6"),
        ("model_b", "GPT-Astra 2.4"),
        ("model_b", "gpt astra (preview)"),
        ("model_c", "GLM-5.3-Max"),
        ("model_c", "glm 5.3 max"),
    ],
)
def test_correct_family_label_initializes(tmp_path: Path, slot: str, label: str):
    """Direct initialization pass matrix: correct visible labels pass the
    real init path and freeze the EXACT original label."""
    session = _init_label(tmp_path, slot, label)
    authority = load_subscription_lane_authority(session.lane_root)
    assert authority.user_visible_model_name == label  # exact original kept


@pytest.mark.parametrize(
    ("slot", "label"),
    [(slot, label) for slot, labels in _WRONG_FAMILY_LABELS_BY_SLOT.items() for label in labels],
)
def test_wrong_family_visible_label_cannot_initialize(tmp_path: Path, slot: str, label: str):
    """Direct initialization fail matrix: a wrong-family visible label STOPs
    the REAL init path with an explicit error — before any lane state or
    output exists (never silently inferred from the slot)."""
    root = tmp_path / f"init-{abs(hash(label))}"
    with pytest.raises(
        ValueError,
        match="subscription_visible_model_family_conflict|subscription_visible_model_family_not_established",
    ):
        _init_label(tmp_path, slot, label)
    # Fail-closed: no lane directory was created at all.
    assert not (root / "lanes" / slot).exists()


def test_unmappable_visible_label_fails_closed(tmp_path: Path):
    """A label that cannot mechanically establish any family (no family
    token at all) fails closed rather than being inferred from the slot."""
    with pytest.raises(ValueError, match="subscription_visible_model_family_not_established"):
        _init_label(tmp_path, "model_a", "Claude 4.5 Preview Plus")
    with pytest.raises(ValueError, match="subscription_visible_model_family_not_established"):
        _init_label(tmp_path, "model_b", "ChatGPT Plus")
    with pytest.raises(ValueError, match="subscription_visible_model_family_not_established"):
        _init_label(tmp_path, "model_c", "Z.ai Assistant")


def test_wrong_family_visible_label_rejected_at_cli_sub_lane_init(tmp_path: Path):
    """The CLI initialization path fails closed on a wrong-family label."""
    sampling = _sampling()
    packet = _packet()
    work = tmp_path / "cli"
    work.mkdir(parents=True)
    packet_path, packet_manifest = _write_packet(work, packet)
    sampling_file = work / "sampling-manifest.json"
    sampling_file.write_text(json.dumps(sampling.model_dump(mode="json")))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "evals.calibration",
            "sub-lane-init",
            "--sampling-manifest",
            str(sampling_file),
            "--reviewer-slot",
            "model_a",
            "--reviewer-config-digest",
            CONFIG_DIGEST,
            "--source-packet-digest",
            SOURCE_DIGEST,
            "--neutral-packet",
            str(packet_path),
            "--neutral-packet-manifest",
            str(packet_manifest),
            "--visible-model-name",
            "Claude Sonnet 4.5",
            "--operator",
            OPERATOR,
            "--protected-dir",
            str(work / "protected"),
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode != 0
    assert "subscription_visible_model_family" in (result.stderr + result.stdout)


def test_sonnet_display_cannot_label_opus_frozen_lane(campaign):
    """model_a + Claude Sonnet display name cannot be accepted for an
    Opus-frozen lane: an attestation carrying a different visible model than
    the frozen lane authority fails closed at the authority gate."""
    slot = "model_a"
    lane_root = campaign["root"] / "lanes" / slot
    # Round 3: a Sonnet lane authority can no longer even be BUILT or loaded
    # — forge the digest-bound ATTESTATION directly (exactly what a later
    # substituted attestation would look like) and prove the frozen-lane
    # authority gate still rejects it.
    correct = load_subscription_lane_authority(lane_root)
    base = build_attestation(
        campaign_id=CAMPAIGN,
        reviewer_slot=slot,
        lane_authority=correct,
        request_batch_digest="b" * 64,
        raw_response_digest="c" * 64,
        attested_at=NOW.isoformat(),
    )
    sonnet_payload = dict(base.payload())
    sonnet_payload["user_visible_model_name"] = "Claude Sonnet 4.5"
    sonnet_payload["attestation_digest"] = subscription_ui._attestation_digest_payload(
        sonnet_payload
    )
    forged = SubscriptionReviewAttestation.model_validate(sonnet_payload)
    assert forged.user_visible_model_name == "Claude Sonnet 4.5"
    with pytest.raises(ValueError, match="subscription_attestation_visible_model_mismatch"):
        require_lane_visible_model_authority(lane_root, forged)


def test_changed_visible_model_after_batch1_fails(campaign):
    entries = _manifest(campaign)
    _import(campaign, "model_a", entries[0], _batch_response(entries[0], "model_a"))
    # Maintainer changes the selected model after output exists: rewrite the
    # lane's frozen authority to a different (family-correct, e.g. newer
    # version) visible model — a wrong-family label would now be rejected by
    # the schema itself, so the digest-binding proof uses a valid-family
    # different label, which is the realistic mid-campaign change.
    lane_root = campaign["root"] / "lanes" / "model_a"
    authority = load_subscription_lane_authority(lane_root)
    changed = build_subscription_lane_authority(
        campaign_id=CAMPAIGN,
        reviewer_slot="model_a",
        user_visible_model_name="Claude Opus 5.1",
        operator_reference=OPERATOR,
        lane_identity_digest=authority.lane_identity_digest,
        frozen_at=authority.frozen_at,
    )
    path = subscription_ui.subscription_lane_authority_path(lane_root)
    path.unlink()
    path.write_text(json.dumps(changed.payload(), sort_keys=True, indent=2) + "\n")
    # Batch 2 import fails closed against the frozen preparation authority.
    with pytest.raises(ValueError, match="subscription_lane_authority_digest_mismatch"):
        _import(campaign, "model_a", entries[1], _batch_response(entries[1], "model_a"))


def test_wrong_service_and_wrong_family_authority_fail():
    # Round 3: build from a family-CORRECT authority, then tamper service /
    # family (with a fresh digest so the slot-mapping checks are what fire).
    base = build_subscription_lane_authority(
        campaign_id=CAMPAIGN,
        reviewer_slot="model_a",
        user_visible_model_name="Claude Opus 4.8",
        operator_reference="o",
        lane_identity_digest="1" * 64,
    ).payload()
    forged_service = dict(base)
    forged_service["service"] = "chatgpt"
    forged_service["authority_digest"] = subscription_ui._authority_digest_payload(forged_service)
    with pytest.raises(ValueError, match="service_does_not_match_slot"):
        SubscriptionLaneAuthority.model_validate(forged_service)
    forged_family = dict(base)
    forged_family["reviewer_family"] = "gpt-astra"
    forged_family["authority_digest"] = subscription_ui._authority_digest_payload(forged_family)
    with pytest.raises(ValueError, match="family_does_not_match_slot"):
        SubscriptionLaneAuthority.model_validate(forged_family)


def test_wrong_service_attestation_fails(campaign):
    attestation = build_attestation(
        campaign_id=CAMPAIGN,
        reviewer_slot="model_a",
        lane_authority=load_subscription_lane_authority(campaign["root"] / "lanes" / "model_a"),
        request_batch_digest="b" * 64,
        raw_response_digest="c" * 64,
        attested_at=NOW.isoformat(),
    )
    forged = dict(attestation.payload())
    forged["service"] = "chatgpt"
    with pytest.raises(ValueError, match="attestation_service_does_not_match_frozen_slot"):
        SubscriptionReviewAttestation.model_validate(forged)
    forged = dict(attestation.payload())
    forged["reviewer_family"] = "gpt-astra"
    with pytest.raises(ValueError, match="attestation_family_does_not_match_frozen_slot"):
        SubscriptionReviewAttestation.model_validate(forged)


def test_correct_frozen_model_passes_all_batches_and_lane_freeze(campaign):
    for slot in REVIEWER_SLOTS:
        for entry in _manifest(campaign):
            result = _import(campaign, slot, entry, _batch_response(entry, slot))
            assert result["outcome"] == "completed_structured"
            assert result["accepted"] == entry["case_count"]
            assert result["frozen_user_visible_model"] == VISIBLE_MODEL_BY_SLOT[slot]
        lane = freeze_lane(
            protected_root=campaign["root"],
            reviewer=campaign["reviewers"][slot],
            campaign_id=CAMPAIGN,
            sampling=campaign["sampling"],
            source_packet_digest=SOURCE_DIGEST,
        )
        assert len(lane.sample_ids) == len(IDS)


def test_correlation_report_emits_frozen_visible_model(campaign):
    from evals.calibration.consensus import build_correlation_report
    from evals.calibration.model_lanes import load_frozen_lanes, records_by_lane_from_files

    for slot in REVIEWER_SLOTS:
        for entry in _manifest(campaign):
            _import(campaign, slot, entry, _batch_response(entry, slot))
        freeze_lane(
            protected_root=campaign["root"],
            reviewer=campaign["reviewers"][slot],
            campaign_id=CAMPAIGN,
            sampling=campaign["sampling"],
            source_packet_digest=SOURCE_DIGEST,
        )
    lanes = load_frozen_lanes(
        campaign["root"],
        campaign_id=CAMPAIGN,
        sampling=campaign["sampling"],
        source_packet_digest=SOURCE_DIGEST,
        reviewers=campaign["reviewers"],
    )
    report = build_correlation_report(
        campaign_id=CAMPAIGN,
        lanes=lanes,
        records_by_lane=records_by_lane_from_files(campaign["root"]),
        sampling=campaign["sampling"],
        source_packet_digest=SOURCE_DIGEST,
    )
    for slot in REVIEWER_SLOTS:
        entry = report.execution_provenance["by_lane"][slot]
        assert entry["identity_source"] == "operator_attested_subscription_ui"
        assert entry["frozen_user_visible_model_name"] == [VISIBLE_MODEL_BY_SLOT[slot]]
        assert entry["provider_metadata_available"] is False
    assert "operator-attested" in report.execution_provenance["note"]


# =============================================================================
# FIX-2 — canonical logical-prompt authority + paired-rewrite adversarial suite
# =============================================================================


def _paired_rewrite(campaign: dict, mutate_prompt, *, also_prepare: bool = False) -> None:
    """Rewrite batches/<batch>.json + batches/manifest.json (and, optionally,
    subscription-prepare.json) as an internally self-consistent pair,
    recomputing every mutable internal digest."""
    root = campaign["root"]
    mpath = subscription_ui.batch_manifest_path(root)
    manifest = json.loads(mpath.read_text())
    entry = manifest["batches"][0]
    ppath = batch_prompt_path(root, str(entry["batch_id"]))
    prompt = json.loads(ppath.read_text())
    mutate_prompt(prompt)
    payload = subscription_ui.serialize_prompt(prompt)
    ppath.unlink()
    ppath.write_bytes(payload)
    entry["prompt_sha256"] = hashlib.sha256(payload).hexdigest()
    entry["serialized_bytes"] = len(payload)
    manifest["logical_manifest_digest"] = subscription_ui.logical_manifest_digest(manifest)
    mpath.unlink()
    mpath.write_bytes(subscription_ui._canonical_json_bytes(manifest))
    if also_prepare:
        prep = subscription_ui.prepare_record_path(root)
        record = json.loads(prep.read_text())
        record["logical_manifest_digest"] = manifest["logical_manifest_digest"]
        prep.unlink()
        prep.write_bytes(subscription_ui._canonical_json_bytes(record))


@pytest.mark.parametrize("also_prepare", [False, True])
def test_rewrite_alter_case_content_preserving_id(campaign, also_prepare):
    def mutate(prompt):
        prompt["cases"][0]["content"] = "forged-content"

    _paired_rewrite(campaign, mutate, also_prepare=also_prepare)
    with pytest.raises(
        ValueError, match="not_canonical_projection|logical_manifest_digest_mismatch"
    ):
        verify_review_batches(
            campaign["root"], sampling=campaign["sampling"], source_packet_digest=SOURCE_DIGEST
        )


@pytest.mark.parametrize("also_prepare", [False, True])
def test_rewrite_swap_two_cases(campaign, also_prepare):
    def mutate(prompt):
        prompt["cases"][0], prompt["cases"][1] = prompt["cases"][1], prompt["cases"][0]

    _paired_rewrite(campaign, mutate, also_prepare=also_prepare)
    with pytest.raises(
        ValueError, match="not_canonical_projection|logical_manifest_digest_mismatch"
    ):
        verify_review_batches(
            campaign["root"], sampling=campaign["sampling"], source_packet_digest=SOURCE_DIGEST
        )


def test_rewrite_drop_non_id_case_field(campaign):
    def mutate(prompt):
        del prompt["cases"][0]["risk"]

    _paired_rewrite(campaign, mutate, also_prepare=True)
    with pytest.raises(
        ValueError, match="not_canonical_projection|logical_manifest_digest_mismatch"
    ):
        verify_review_batches(
            campaign["root"], sampling=campaign["sampling"], source_packet_digest=SOURCE_DIGEST
        )


def test_rewrite_add_extra_case_evidence(campaign):
    def mutate(prompt):
        prompt["cases"][0]["extra_evidence"] = "forged"

    _paired_rewrite(campaign, mutate, also_prepare=True)
    with pytest.raises(
        ValueError, match="not_canonical_projection|logical_manifest_digest_mismatch"
    ):
        verify_review_batches(
            campaign["root"], sampling=campaign["sampling"], source_packet_digest=SOURCE_DIGEST
        )


def test_rewrite_change_retention_semantics(campaign):
    def mutate(prompt):
        sem = prompt["instructions"]["semantics"]
        if isinstance(sem, dict):
            hit = False
            for key in list(sem):
                if "retention" in str(key).lower():
                    sem[key] = "FORGED-RETENTION-SEMANTICS"
                    hit = True
            if not hit:
                sem["retention_value"] = "FORGED-RETENTION-SEMANTICS"
        else:
            prompt["instructions"]["semantics"] = "FORGED"

    _paired_rewrite(campaign, mutate, also_prepare=True)
    with pytest.raises(
        ValueError, match="not_canonical_projection|logical_manifest_digest_mismatch"
    ):
        verify_review_batches(
            campaign["root"], sampling=campaign["sampling"], source_packet_digest=SOURCE_DIGEST
        )


def test_rewrite_change_consequence_semantics(campaign):
    def mutate(prompt):
        sem = prompt["instructions"]["semantics"]
        if isinstance(sem, dict):
            hit = False
            for key in list(sem):
                if "consequence" in str(key).lower():
                    sem[key] = "FORGED-CONSEQUENCE-SEMANTICS"
                    hit = True
            if not hit:
                sem["consequence"] = "FORGED-CONSEQUENCE-SEMANTICS"
        else:
            prompt["instructions"]["semantics"] = "FORGED"

    _paired_rewrite(campaign, mutate, also_prepare=True)
    with pytest.raises(
        ValueError, match="not_canonical_projection|logical_manifest_digest_mismatch"
    ):
        verify_review_batches(
            campaign["root"], sampling=campaign["sampling"], source_packet_digest=SOURCE_DIGEST
        )


def test_rewrite_change_response_contract(campaign):
    def mutate(prompt):
        prompt["response_contract"]["rules"][0] = "FORGED RULE"

    _paired_rewrite(campaign, mutate, also_prepare=True)
    with pytest.raises(
        ValueError, match="not_canonical_projection|logical_manifest_digest_mismatch"
    ):
        verify_review_batches(
            campaign["root"], sampling=campaign["sampling"], source_packet_digest=SOURCE_DIGEST
        )


def test_rewrite_change_guide_version(campaign):
    def mutate(prompt):
        prompt["label_guide_version"] = "forged-guide-v999"

    _paired_rewrite(campaign, mutate, also_prepare=True)
    with pytest.raises(
        ValueError, match="not_canonical_projection|logical_manifest_digest_mismatch"
    ):
        verify_review_batches(
            campaign["root"], sampling=campaign["sampling"], source_packet_digest=SOURCE_DIGEST
        )


def test_rewrite_change_reviewer_instruction_version(campaign):
    def mutate(prompt):
        prompt["reviewer_instructions_version"] = "forged-instructions-v999"

    _paired_rewrite(campaign, mutate, also_prepare=True)
    with pytest.raises(
        ValueError, match="not_canonical_projection|logical_manifest_digest_mismatch"
    ):
        verify_review_batches(
            campaign["root"], sampling=campaign["sampling"], source_packet_digest=SOURCE_DIGEST
        )


def test_logical_prompt_carries_canonical_206_instructions(campaign):
    """The paste-ready prompt's instruction object IS the canonical #206
    bundle and its response contract is the frozen contract."""
    from evals.calibration.ingestion import LABELING_INSTRUCTIONS

    for entry in _manifest(campaign):
        prompt = json.loads(batch_prompt_path(campaign["root"], str(entry["batch_id"])).read_text())
        assert prompt["instructions"] == LABELING_INSTRUCTIONS
        assert prompt["response_contract"] == subscription_ui.CANONICAL_RESPONSE_CONTRACT
        assert prompt["protocol_version"] == CONSENSUS_PROTOCOL_VERSION


def test_logical_batches_share_one_frozen_manifest_digest(campaign):
    """The logical-batch manifest digest is shared campaign authority: all
    three lanes' imports verify against the same frozen digest."""
    prepare = json.loads(subscription_ui.prepare_record_path(campaign["root"]).read_text())
    manifest = verify_review_batches(
        campaign["root"], sampling=campaign["sampling"], source_packet_digest=SOURCE_DIGEST
    )
    assert manifest["logical_manifest_digest"] == prepare["logical_manifest_digest"]
    assert len(prepare["lane_authority_digests"]) == 3


# =============================================================================
# FIX-3 — returned-response preservation, refusal/malformed/retry semantics
# =============================================================================


def _attempt_paths(campaign, slot, batch_id):
    lane_root = campaign["root"] / "lanes" / slot
    return lane_root, subscription_ui.batch_attempts_dir(lane_root, str(batch_id))


def test_prose_refusal_is_preserved_and_escalates(campaign):
    entries = _manifest(campaign)
    raw = "I cannot help with judging these memory items."
    result = _import(campaign, "model_a", entries[0], raw)
    assert result["outcome"] == "substantive_malformed"
    assert result["accepted"] == 0
    assert result["escalated"] == entries[0]["case_count"]
    lane_root, attempts = _attempt_paths(campaign, "model_a", entries[0]["batch_id"])
    assert (attempts / "attempt-01" / "raw.resp").read_bytes() == raw.encode()
    record, attestation, preserved = load_attempt(lane_root, str(entries[0]["batch_id"]), 1)
    assert record.outcome == "substantive_malformed"
    assert preserved == raw.encode()
    assert attestation.raw_response_digest == hashlib.sha256(raw.encode()).hexdigest()
    # Per-case records honestly mark refusal/malformed — never judged fields.
    records = load_lane_records(campaign["root"], "model_a")
    for sid in entries[0]["sample_ids"]:
        rec = records[str(sid)]
        assert rec.outcome_status == "malformed"
        assert rec.judgment is None


def test_structured_json_refusal_object_escalates_as_refusal(campaign):
    entries = _manifest(campaign)
    raw = json.dumps({"outcome": "refused", "error_code": "policy_refusal"})
    result = _import(campaign, "model_b", entries[0], raw)
    assert result["outcome"] == "substantive_refusal"
    assert result["escalated"] == entries[0]["case_count"]
    records = load_lane_records(campaign["root"], "model_b")
    for sid in entries[0]["sample_ids"]:
        rec = records[str(sid)]
        assert rec.outcome_status == "refused"
        assert rec.error_code == "policy_refusal"


def test_malformed_output_is_preserved(campaign):
    entries = _manifest(campaign)
    raw = json.dumps({"foo": "bar"})
    result = _import(campaign, "model_c", entries[0], raw)
    assert result["outcome"] == "substantive_malformed"
    lane_root, attempts = _attempt_paths(campaign, "model_c", entries[0]["batch_id"])
    assert (attempts / "attempt-01" / "raw.resp").read_bytes() == raw.encode()


def test_truncated_output_preserved_as_failed_attempt(campaign):
    entries = _manifest(campaign)
    good = _batch_response(entries[0], "model_a")
    truncated = good[: len(good) // 2]
    result = _import(campaign, "model_a", entries[0], truncated)
    assert result["outcome"] == "mechanically_incomplete"
    assert result["accepted"] == 0
    # A different response without the retry flag is refused...
    with pytest.raises(ValueError, match="subscription_batch_raw_conflict_retry_required"):
        _import(campaign, "model_a", entries[0], good)
    # ...and the substantive retry preserves attempt 1 entirely.
    result2 = _import(campaign, "model_a", entries[0], good, retry_mechanical_failure=True)
    assert result2["outcome"] == "completed_structured"
    assert result2["attempt"] == 2
    assert result2["accepted"] == entries[0]["case_count"]
    lane_root, attempts = _attempt_paths(campaign, "model_a", entries[0]["batch_id"])
    # Attempt 1 raw bytes + attestation retained.
    record1, att1, raw1 = load_attempt(lane_root, str(entries[0]["batch_id"]), 1)
    assert raw1 == truncated.encode()
    assert record1.outcome == "mechanically_incomplete"
    assert record1.mechanical_failure_reason == "truncated_json"
    assert record1.retry_of_attempt is None
    # Attempt 2 is a new independent bound pair.
    record2, att2, raw2 = load_attempt(lane_root, str(entries[0]["batch_id"]), 2)
    assert raw2 == good.encode()
    assert record2.retry_of_attempt == 1
    assert att2.attestation_digest != att1.attestation_digest
    assert att2.raw_response_digest == hashlib.sha256(good.encode()).hexdigest()


def test_no_accepted_substantive_attempt_can_be_replaced(campaign):
    entries = _manifest(campaign)
    _import(campaign, "model_a", entries[0], _batch_response(entries[0], "model_a"))
    different = _batch_response(entries[0], "model_a").replace("fact", "doctrine")
    with pytest.raises(ValueError, match="subscription_attempt_accepted_cannot_be_replaced"):
        _import(campaign, "model_a", entries[0], different)
    # Even with the retry flag: accepted records prevent retry.
    with pytest.raises(ValueError, match="subscription_attempt_accepted_cannot_be_replaced"):
        _import(campaign, "model_a", entries[0], different, retry_mechanical_failure=True)


def test_retry_after_accepted_records_fails(campaign):
    entries = _manifest(campaign)
    truncated = _batch_response(entries[0], "model_a")[:10]
    _import(campaign, "model_a", entries[0], truncated)
    # Bound accepted records appear (structured import for a SECOND batch
    # creates records); a mechanical retry of batch 1 whose lane carries
    # records bound to batch 1's digest is impossible here because attempt 1
    # is incomplete — instead prove the guard via the empty-capture path.
    good = _batch_response(entries[0], "model_a")
    result = _import(campaign, "model_a", entries[0], good, retry_mechanical_failure=True)
    assert result["outcome"] == "completed_structured"
    # Now the accepted attempt cannot be re-supplied with different bytes.
    with pytest.raises(ValueError, match="subscription_attempt_accepted_cannot_be_replaced"):
        _import(
            campaign,
            "model_a",
            entries[0],
            _batch_response(entries[0], "model_a").replace("fact", "doctrine"),
            retry_mechanical_failure=True,
        )


def test_substantive_refusal_cannot_be_retried(campaign):
    entries = _manifest(campaign)
    _import(campaign, "model_a", entries[0], json.dumps({"outcome": "refused", "error_code": "no"}))
    with pytest.raises(ValueError, match="subscription_attempt_substantive_cannot_be_retried"):
        _import(
            campaign,
            "model_a",
            entries[0],
            _batch_response(entries[0], "model_a"),
            retry_mechanical_failure=True,
        )


def test_batch_level_refusal_reaches_human_escalation(campaign):
    """A whole-batch refusal expands to escalation records for every case and
    surfaces in the #206 human queue via the correlation report."""
    from evals.calibration.consensus import build_correlation_report
    from evals.calibration.model_lanes import load_frozen_lanes, records_by_lane_from_files

    entries = _manifest(campaign)
    for slot in ("model_a", "model_b"):
        for entry in entries:
            _import(campaign, slot, entry, _batch_response(entry, slot))
    # Lane model_c refuses the whole first batch, completes the rest.
    _import(
        campaign,
        "model_c",
        entries[0],
        json.dumps({"outcome": "refused", "error_code": "policy_refusal"}),
    )
    for entry in entries[1:]:
        _import(campaign, "model_c", entry, _batch_response(entry, "model_c"))
    for slot in REVIEWER_SLOTS:
        freeze_lane(
            protected_root=campaign["root"],
            reviewer=campaign["reviewers"][slot],
            campaign_id=CAMPAIGN,
            sampling=campaign["sampling"],
            source_packet_digest=SOURCE_DIGEST,
        )
    lanes = load_frozen_lanes(
        campaign["root"],
        campaign_id=CAMPAIGN,
        sampling=campaign["sampling"],
        source_packet_digest=SOURCE_DIGEST,
        reviewers=campaign["reviewers"],
    )
    report = build_correlation_report(
        campaign_id=CAMPAIGN,
        lanes=lanes,
        records_by_lane=records_by_lane_from_files(campaign["root"]),
        sampling=campaign["sampling"],
        source_packet_digest=SOURCE_DIGEST,
    )
    refused_ids = set(entries[0]["sample_ids"])
    assert report.malformed_error_refusal_count >= len(refused_ids)
    assert report.human_queue_count_before_audit >= len(refused_ids)


def test_membership_drift_is_substantive_malformed_not_retryable(campaign):
    """FIX-3 (round 3): complete valid JSON whose membership drifts from the
    batch contract is substantive evidence of noncompliance — preserved,
    escalated, and NON-retryable (never a mechanical transport failure)."""
    entries = _manifest(campaign)

    def mutate(payload):
        payload["results"].pop()  # missing case

    raw = _batch_response(entries[0], "model_a", mutate=mutate)
    result = _import(campaign, "model_a", entries[0], raw)
    assert result["outcome"] == "substantive_malformed"
    assert result["escalated"] == entries[0]["case_count"]
    lane_root, attempts = _attempt_paths(campaign, "model_a", entries[0]["batch_id"])
    assert (attempts / "attempt-01" / "raw.resp").read_bytes() == raw.encode()
    record, _att, preserved = load_attempt(lane_root, str(entries[0]["batch_id"]), 1)
    assert record.outcome == "substantive_malformed"
    assert record.mechanical_failure_reason is None
    assert preserved == raw.encode()
    # Every case entered escalation as malformed.
    records = load_lane_records(campaign["root"], "model_a")
    for sid in entries[0]["sample_ids"]:
        assert records[str(sid)].outcome_status == "malformed"
    # A second different response is refused even WITH the retry flag.
    with pytest.raises(ValueError, match="subscription_attempt_substantive_cannot_be_retried"):
        _import(
            campaign,
            "model_a",
            entries[0],
            _batch_response(entries[0], "model_a"),
            retry_mechanical_failure=True,
        )
    # Attempt 1 remains byte-identical.
    assert load_attempt(lane_root, str(entries[0]["batch_id"]), 1)[2] == raw.encode()


_DRIFT_MUTATIONS = {
    "missing_case": lambda r: r.pop(),
    "extra_case": lambda r: r.append(dict(r[0])),
    "duplicate_case_id": lambda r: r.append(dict(r[-1])),
    "reversed_order": lambda r: r.reverse(),
    "missing_field": lambda r: r[0].pop("consequence"),
    "extra_field": lambda r: r[0].update({"forged": "x"}),
    "wrong_results_shape": None,  # handled inline: results is an object, not array
}


@pytest.mark.parametrize("drift", sorted(_DRIFT_MUTATIONS))
def test_complete_schema_drift_is_non_retryable_substantive_malformed(campaign, drift):
    """FIX-3 (round 3): each complete-response contract violation — missing
    case, extra case, duplicate ID, reversed order, missing/extra result
    field, wrong complete results shape — is substantive malformed evidence:
    preserved exactly, escalated for every case, non-retryable."""
    entries = _manifest(campaign)
    mutate_payload = _DRIFT_MUTATIONS[drift]
    if drift == "wrong_results_shape":
        raw = json.dumps({"results": {"s001": "forged"}})
    else:
        raw = _batch_response(entries[0], "model_a", mutate=lambda p: mutate_payload(p["results"]))
    result = _import(campaign, "model_a", entries[0], raw)
    assert result["outcome"] == "substantive_malformed", drift
    assert result["escalated"] == entries[0]["case_count"]
    lane_root, attempts = _attempt_paths(campaign, "model_a", entries[0]["batch_id"])
    assert (attempts / "attempt-01" / "raw.resp").read_bytes() == raw.encode()
    record, _att, _preserved = load_attempt(lane_root, str(entries[0]["batch_id"]), 1)
    assert record.outcome == "substantive_malformed"
    assert record.mechanical_failure_reason is None
    records = load_lane_records(campaign["root"], "model_a")
    for sid in entries[0]["sample_ids"]:
        assert records[str(sid)].outcome_status == "malformed"
    with pytest.raises(ValueError, match="subscription_attempt_substantive_cannot_be_retried"):
        _import(
            campaign,
            "model_a",
            entries[0],
            _batch_response(entries[0], "model_a"),
            retry_mechanical_failure=True,
        )


def test_empty_capture_is_preserved_mechanical_attempt_evidence(campaign):
    """FIX-2 (round 3): the exact empty string is valid attempt evidence —
    preserved as zero-byte raw.resp, attested, recorded, classified
    mechanically_incomplete/empty_capture, and explicitly retryable."""
    entries = _manifest(campaign)
    result = _import(campaign, "model_a", entries[0], "")
    assert result["outcome"] == "mechanically_incomplete"
    assert result["accepted"] == 0
    lane_root, attempts = _attempt_paths(campaign, "model_a", entries[0]["batch_id"])
    raw_path = attempts / "attempt-01" / "raw.resp"
    assert raw_path.read_bytes() == b""  # zero bytes preserved
    empty_sha = hashlib.sha256(b"").hexdigest()
    assert result["raw_response_digest"] == empty_sha
    record, attestation, preserved = load_attempt(lane_root, str(entries[0]["batch_id"]), 1)
    assert len(preserved) == 0
    assert record.outcome == "mechanically_incomplete"
    assert record.mechanical_failure_reason == "empty_capture"
    assert attestation.raw_response_digest == empty_sha
    assert raw_path.stat().st_size == 0
    # No review records were accepted from an empty capture.
    assert not load_lane_records(campaign["root"], "model_a")
    # Retry without the explicit flag fails...
    with pytest.raises(ValueError, match="subscription_batch_raw_conflict_retry_required"):
        _import(campaign, "model_a", entries[0], _batch_response(entries[0], "model_a"))
    # ...with the flag, attempt 2 completes the batch...
    result2 = _import(
        campaign,
        "model_a",
        entries[0],
        _batch_response(entries[0], "model_a"),
        retry_mechanical_failure=True,
    )
    assert result2["outcome"] == "completed_structured"
    assert result2["attempt"] == 2
    # ...and attempt 1 remains byte-identical zero bytes.
    record1_again, _att, raw1_again = load_attempt(lane_root, str(entries[0]["batch_id"]), 1)
    assert raw1_again == b""
    assert record1_again.outcome == "mechanically_incomplete"
    assert record1_again.mechanical_failure_reason == "empty_capture"


def test_whitespace_capture_still_mechanical_empty(campaign):
    """Whitespace-only capture continues to classify as empty_capture."""
    entries = _manifest(campaign)
    result = _import(campaign, "model_b", entries[0], "  \n\t ")
    assert result["outcome"] == "mechanically_incomplete"
    lane_root, _attempts = _attempt_paths(campaign, "model_b", entries[0]["batch_id"])
    record, _att, preserved = load_attempt(lane_root, str(entries[0]["batch_id"]), 1)
    assert record.mechanical_failure_reason == "empty_capture"
    assert preserved == b"  \n\t "


def test_truncated_json_remains_retryable(campaign):
    """FIX-3 retains retryability for demonstrably truncated JSON."""
    entries = _manifest(campaign)
    good = _batch_response(entries[0], "model_c")
    truncated = good[: len(good) // 2]
    result = _import(campaign, "model_c", entries[0], truncated)
    assert result["outcome"] == "mechanically_incomplete"
    lane_root, _attempts = _attempt_paths(campaign, "model_c", entries[0]["batch_id"])
    record, _att, _preserved = load_attempt(lane_root, str(entries[0]["batch_id"]), 1)
    assert record.mechanical_failure_reason == "truncated_json"
    result2 = _import(campaign, "model_c", entries[0], good, retry_mechanical_failure=True)
    assert result2["outcome"] == "completed_structured"


# =============================================================================
# FIX-4 — self-contained CLI workflow, real serialized-size ceiling
# =============================================================================


def test_serialized_prompt_respects_actual_byte_ceiling(campaign):
    manifest = verify_review_batches(
        campaign["root"], sampling=campaign["sampling"], source_packet_digest=SOURCE_DIGEST
    )
    ceiling = manifest["max_serialized_bytes"]
    for entry in manifest["batches"]:
        payload = batch_prompt_path(campaign["root"], str(entry["batch_id"])).read_bytes()
        # ACTUAL serialized bytes (instructions + contract + metadata + cases).
        assert len(payload) <= ceiling
        assert entry["serialized_bytes"] == len(payload)
        assert entry["case_count"] <= BATCH_MAX_CASES


def test_single_case_oversize_fails_closed(tmp_path: Path):
    sampling = _sampling()
    packet = _packet()
    packet_path, packet_manifest = _write_packet(tmp_path, packet)
    for slot in REVIEWER_SLOTS:
        init_subscription_lane(
            tmp_path,
            reviewer=subscription_reviewer_identity(
                slot,
                reviewer_config_digest=CONFIG_DIGEST,
                prompt_digest=labeling_instructions_digest(),
            ),
            campaign_id=CAMPAIGN,
            sampling=sampling,
            source_packet_digest=SOURCE_DIGEST,
            neutral_packet_path=packet_path,
            neutral_packet_manifest=packet_manifest,
            user_visible_model_name=VISIBLE_MODEL_BY_SLOT[slot],
            operator_reference=OPERATOR,
        )
    with pytest.raises(
        ValueError, match="subscription_batch_single_case_exceeds_serialized_ceiling"
    ):
        prepare_subscription_campaign(
            tmp_path,
            campaign_id=CAMPAIGN,
            sampling=sampling,
            source_packet_digest=SOURCE_DIGEST,
            neutral_packet_path=packet_path,
            neutral_packet_manifest=packet_manifest,
            max_serialized_bytes=100,  # instructions alone exceed this
        )


def test_deterministic_batch_boundaries_and_prepare_idempotency(campaign):
    first = verify_review_batches(
        campaign["root"], sampling=campaign["sampling"], source_packet_digest=SOURCE_DIGEST
    )
    generations_before = {
        slot: sorted((campaign["root"] / "lanes" / slot).glob("lane-requests-*.jsonl"))
        for slot in REVIEWER_SLOTS
    }
    prepared_again = prepare_subscription_campaign(
        campaign["root"],
        campaign_id=CAMPAIGN,
        sampling=campaign["sampling"],
        source_packet_digest=SOURCE_DIGEST,
        neutral_packet_path=campaign["packet_path"],
        neutral_packet_manifest=campaign["packet_manifest"],
        max_cases=4,
    )
    second = verify_review_batches(
        campaign["root"], sampling=campaign["sampling"], source_packet_digest=SOURCE_DIGEST
    )
    assert first == second
    assert prepared_again["batches"] == campaign["prepared"]["batches"]
    for slot in REVIEWER_SLOTS:
        generations_after = sorted(
            (campaign["root"] / "lanes" / slot).glob("lane-requests-*.jsonl")
        )
        assert generations_after == generations_before[slot]  # no new authority


def test_next_outstanding_fails_closed_without_canonical_requests(campaign):
    for path in (campaign["root"] / "lanes" / "model_a").glob("lane-requests-*.jsonl"):
        path.unlink()
    with pytest.raises(ValueError, match="subscription_lane_missing_canonical_requests"):
        next_outstanding_batch(campaign["root"], sampling=campaign["sampling"])


def _manifest_from_files(protected: Path) -> list[dict]:
    manifest = json.loads((protected / "batches" / "manifest.json").read_text())
    return manifest["batches"]


def test_cli_workflow_end_to_end(tmp_path: Path):
    """The documented #208 workflow, driven ONLY through the real CLI:
    sub-lane-init x3, sub-prepare, sub-batch-show, sub-import x3 per batch,
    next batch, freeze x3. No hidden manual ``emit_requests`` step."""
    sampling = _sampling()
    packet = _packet()
    packet_path, packet_manifest = _write_packet(tmp_path, packet)
    work = tmp_path / "cli"
    work.mkdir()
    sampling_file = work / "sampling-manifest.json"
    sampling_file.write_text(json.dumps(sampling.model_dump(mode="json")))
    protected = work / "protected"

    def run(*args: str) -> dict[str, Any]:
        result = subprocess.run(
            [sys.executable, "-m", "evals.calibration", *args],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=True,
        )
        return json.loads(result.stdout)

    identity_files: dict[str, Path] = {}
    for slot in REVIEWER_SLOTS:
        reviewer = subscription_reviewer_identity(
            slot, reviewer_config_digest=CONFIG_DIGEST, prompt_digest=labeling_instructions_digest()
        )
        identity_files[slot] = work / f"reviewer-{slot}.json"
        identity_files[slot].write_text(json.dumps(reviewer.model_dump(mode="json")))
        out = run(
            "sub-lane-init",
            "--sampling-manifest",
            str(sampling_file),
            "--reviewer-slot",
            slot,
            "--reviewer-config-digest",
            CONFIG_DIGEST,
            "--source-packet-digest",
            SOURCE_DIGEST,
            "--neutral-packet",
            str(packet_path),
            "--neutral-packet-manifest",
            str(packet_manifest),
            "--visible-model-name",
            VISIBLE_MODEL_BY_SLOT[slot],
            "--operator",
            OPERATOR,
            "--protected-dir",
            str(protected),
        )
        assert out["frozen_user_visible_model_name"] == VISIBLE_MODEL_BY_SLOT[slot]

    # No lane has canonical request evidence before preparation.
    for slot in REVIEWER_SLOTS:
        assert not list((protected / "lanes" / slot).glob("lane-requests-*.jsonl"))

    prepared = run(
        "sub-prepare",
        "--sampling-manifest",
        str(sampling_file),
        "--neutral-packet",
        str(packet_path),
        "--neutral-packet-manifest",
        str(packet_manifest),
        "--source-packet-digest",
        SOURCE_DIGEST,
        "--protected-dir",
        str(protected),
    )
    assert prepared["batches"]["total_cases"] == len(IDS)
    assert all(lane["canonical_requests"] == "emitted" for lane in prepared["lanes"].values())
    # Preparation itself created the canonical per-lane request evidence.
    for slot in REVIEWER_SLOTS:
        assert list((protected / "lanes" / slot).glob("lane-requests-*.jsonl"))

    batch_ids = [b["batch_id"] for b in prepared["batches"]["batches"]]
    entries_by_id = {e["batch_id"]: e for e in _manifest_from_files(protected)}
    shown = subprocess.run(
        [
            sys.executable,
            "-m",
            "evals.calibration",
            "sub-batch-show",
            "--sampling-manifest",
            str(sampling_file),
            "--source-packet-digest",
            SOURCE_DIGEST,
            "--protected-dir",
            str(protected),
            "--batch-id",
            batch_ids[0],
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=True,
    )
    prompt = json.loads(shown.stdout)
    assert prompt["batch_id"] == batch_ids[0]

    raw_dir = work / "raw"
    raw_dir.mkdir()
    for batch_id in batch_ids:
        entry = entries_by_id[batch_id]
        for slot in REVIEWER_SLOTS:
            raw_file = raw_dir / f"{batch_id}.{slot}.resp"
            raw_file.write_text(_batch_response(entry, slot))
            out = run(
                "sub-import",
                "--sampling-manifest",
                str(sampling_file),
                "--reviewer-slot",
                slot,
                "--batch-id",
                batch_id,
                "--raw-response",
                str(raw_file),
                "--source-packet-digest",
                SOURCE_DIGEST,
                "--protected-dir",
                str(protected),
            )
            assert out["outcome"] == "completed_structured"
            assert out["accepted"] == entry["case_count"]

    for slot in REVIEWER_SLOTS:
        out = run(
            "freeze-model-lane",
            "--sampling-manifest",
            str(sampling_file),
            "--reviewer-identity",
            str(identity_files[slot]),
            "--source-packet-digest",
            SOURCE_DIGEST,
            "--protected-dir",
            str(protected),
        )
        assert out["cases"] == len(IDS)


@pytest.mark.skipif(
    not (REAL_PROTECTED / "sampling-manifest.json").is_file(),
    reason="real #202 round-2 protected corpus not present on this host",
)
def test_real_corpus_batch_count_and_sizes(tmp_path: Path):
    """Recompute the deterministic batch count for the real 402-case corpus
    under the ACTUAL serialized-prompt ceiling."""
    sampling = SamplingManifest.model_validate(
        json.loads((REAL_PROTECTED / "sampling-manifest.json").read_text())
    )
    assert len(sampling.sample_ids) == 402
    packet_path = REAL_PROTECTED / "eng-calibration-001f-blind-v2.neutral.json"
    packet_manifest = REAL_PROTECTED / "neutral-packet-manifest.json"
    source_digest = json.loads(packet_path.read_text())["source_packet_digest"]
    for slot in REVIEWER_SLOTS:
        init_subscription_lane(
            tmp_path,
            reviewer=subscription_reviewer_identity(
                slot,
                reviewer_config_digest=CONFIG_DIGEST,
                prompt_digest=labeling_instructions_digest(),
            ),
            campaign_id=CAMPAIGN,
            sampling=sampling,
            source_packet_digest=source_digest,
            neutral_packet_path=packet_path,
            neutral_packet_manifest=packet_manifest,
            user_visible_model_name=VISIBLE_MODEL_BY_SLOT[slot],
            operator_reference=OPERATOR,
        )
    prepared = prepare_subscription_campaign(
        tmp_path,
        campaign_id=CAMPAIGN,
        sampling=sampling,
        source_packet_digest=source_digest,
        neutral_packet_path=packet_path,
        neutral_packet_manifest=packet_manifest,
    )
    summary = prepared["batches"]
    assert summary["total_cases"] == 402
    assert summary["max_serialized_bytes_observed"] <= BATCH_MAX_SERIALIZED_BYTES
    for batch in summary["batches"]:
        payload = batch_prompt_path(tmp_path, str(batch["batch_id"])).read_bytes()
        assert len(payload) == batch["serialized_bytes"]
        assert len(payload) <= BATCH_MAX_SERIALIZED_BYTES


# =============================================================================
# FIX-4 (round 3) — partial-attempt crash recovery
# =============================================================================


def _crash_after_raw(campaign, slot, batch_id, raw):
    """Simulate a crash after raw.resp but before attestation/attempt.json."""
    from evals.calibration.review import write_protected_file

    lane_root = campaign["root"] / "lanes" / slot
    write_protected_file(attempt_raw_path(lane_root, str(batch_id), 1), raw.encode())
    return lane_root


def _crash_after_attestation(campaign, slot, batch_id, raw):
    """Simulate a crash after raw.resp + attestation.json, before attempt.json."""
    lane_root = campaign["root"] / "lanes" / slot
    authority = load_subscription_lane_authority(lane_root)
    manifest = json.loads(subscription_ui.batch_manifest_path(campaign["root"]).read_text())
    entry = next(e for e in manifest["batches"] if str(e["batch_id"]) == str(batch_id))
    attestation = build_attestation(
        campaign_id=CAMPAIGN,
        reviewer_slot=slot,
        lane_authority=authority,
        request_batch_digest=str(entry["prompt_sha256"]),
        raw_response_digest=hashlib.sha256(raw.encode()).hexdigest(),
        attested_at=NOW.isoformat(),
    )
    from evals.calibration.review import write_protected_file

    write_protected_file(attempt_raw_path(lane_root, str(batch_id), 1), raw.encode())
    write_protected_file(
        attempt_attestation_path(lane_root, str(batch_id), 1),
        (json.dumps(attestation.payload(), sort_keys=True) + "\n").encode(),
    )
    return lane_root


def test_crash_after_raw_only_same_bytes_completes_same_attempt(campaign):
    """raw-only partial + exact resupply finishes the SAME attempt
    deterministically; no data loss, no evidence replacement."""
    entries = _manifest(campaign)
    raw = _batch_response(entries[0], "model_a")
    _crash_after_raw(campaign, "model_a", entries[0]["batch_id"], raw)
    result = _import(campaign, "model_a", entries[0], raw)
    assert result["attempt"] == 1  # same attempt completed, not a new one
    assert result["outcome"] == "completed_structured"
    assert result["accepted"] == entries[0]["case_count"]
    lane_root, _attempts = _attempt_paths(campaign, "model_a", entries[0]["batch_id"])
    record, att, preserved = load_attempt(lane_root, str(entries[0]["batch_id"]), 1)
    assert preserved == raw.encode()
    assert att.raw_response_digest == hashlib.sha256(raw.encode()).hexdigest()
    assert record.attempt == 1


def test_crash_after_raw_only_different_bytes_requires_explicit_retry(campaign):
    """raw-only partial + DIFFERENT bytes: without the flag it STOPs; with
    the flag the partial attempt is quarantined (never deleted) and a new
    attempt is created."""
    entries = _manifest(campaign)
    crashed_bytes = _batch_response(entries[0], "model_a").replace("fact", "doctrine")
    lane_root = _crash_after_raw(campaign, "model_a", entries[0]["batch_id"], crashed_bytes)
    good = _batch_response(entries[0], "model_a")
    with pytest.raises(ValueError, match="subscription_partial_attempt_retry_required"):
        _import(campaign, "model_a", entries[0], good)
    result = _import(campaign, "model_a", entries[0], good, retry_mechanical_failure=True)
    assert result["attempt"] == 2
    assert result["outcome"] == "completed_structured"
    _, attempts = _attempt_paths(campaign, "model_a", entries[0]["batch_id"])
    quarantined = list(attempts.glob("attempt-01.quarantined-*"))
    assert len(quarantined) == 1
    # The quarantined partial bytes are retained, never deleted.
    assert (quarantined[0] / "raw.resp").read_bytes() == crashed_bytes.encode()
    # The committed attempt 2 is complete and the only numeric attempt-01 is gone.
    assert not (attempts / "attempt-01").exists()
    record2, _att, raw2 = load_attempt(lane_root, str(entries[0]["batch_id"]), 2)
    assert raw2 == good.encode()
    assert record2.retry_of_attempt is None


def test_crash_after_raw_and_attestation_completes_record(campaign):
    """raw + attestation partial: the preserved attestation is verified
    against the resupplied bytes and the SAME attempt record is completed."""
    entries = _manifest(campaign)
    raw = _batch_response(entries[0], "model_b")
    _crash_after_attestation(campaign, "model_b", entries[0]["batch_id"], raw)
    result = _import(campaign, "model_b", entries[0], raw)
    assert result["attempt"] == 1
    assert result["outcome"] == "completed_structured"
    lane_root, _attempts = _attempt_paths(campaign, "model_b", entries[0]["batch_id"])
    record, att, preserved = load_attempt(lane_root, str(entries[0]["batch_id"]), 1)
    assert preserved == raw.encode()
    assert record.outcome == "completed_structured"
    # The completed record embeds the PRESERVED attestation, not a new one.
    assert result["attestation_digest"] == att.attestation_digest


def test_crash_after_raw_and_attestation_mismatch_quarantines(campaign):
    """raw + attestation partial whose attestation binds DIFFERENT bytes:
    quarantined, explicit retry required, partial bytes retained."""
    entries = _manifest(campaign)
    crashed_bytes = _batch_response(entries[0], "model_c")
    _crash_after_attestation(campaign, "model_c", entries[0]["batch_id"], crashed_bytes)
    good = _batch_response(entries[0], "model_c").replace("fact", "doctrine")
    with pytest.raises(ValueError, match="subscription_partial_attempt_retry_required"):
        _import(campaign, "model_c", entries[0], good)
    result = _import(campaign, "model_c", entries[0], good, retry_mechanical_failure=True)
    assert result["attempt"] == 2
    _, attempts = _attempt_paths(campaign, "model_c", entries[0]["batch_id"])
    quarantined = list(attempts.glob("attempt-01.quarantined-*"))
    assert len(quarantined) == 1
    assert (quarantined[0] / "raw.resp").read_bytes() == crashed_bytes.encode()


def test_committed_attempt_unaffected_by_recovery_logic(campaign):
    """A fully committed attempt is immutable under the recovery machinery:
    exact resupply is idempotent, different bytes are refused."""
    entries = _manifest(campaign)
    raw = _batch_response(entries[0], "model_a")
    _import(campaign, "model_a", entries[0], raw)
    # Exact resupply is the idempotent resume path.
    again = _import(campaign, "model_a", entries[0], raw)
    assert again["attempt"] == 1
    assert again["resumed_duplicates"] == entries[0]["case_count"]
    # Different bytes cannot replace a committed attempt.
    with pytest.raises(ValueError, match="subscription_attempt_accepted_cannot_be_replaced"):
        _import(campaign, "model_a", entries[0], raw.replace("fact", "doctrine"))


# =============================================================================
# Preserved round-1 proofs (provider path, consensus, serving, replay)
# =============================================================================


def test_subscription_attestation_cannot_validate_as_provider_metadata():
    with pytest.raises(ValueError, match="provider_metadata_identity_requires_metadata_artifact"):
        ExecutionEvidence(
            campaign_id=CAMPAIGN,
            actual_reviewer_slot="model_a",
            actual_reviewer_family="claude-opus",
            actual_provider_model_identifier="claude-opus@claude-ai-subscription-ui",
            actual_configuration_digest=CONFIG_DIGEST,
            actual_prompt_digest=labeling_instructions_digest(),
            request_generation=1,
            request_item_digest="d" * 64,
            executed_at=NOW,
            executor_status="completed",
            executor_identity="operator:operator-1",
            identity_source="provider_metadata",
            provider_request_id="req-1",
            provider_response_id="resp-1",
            provider_metadata=None,
        )


def test_attestation_provider_ids_structurally_none(campaign):
    attestation = build_attestation(
        campaign_id=CAMPAIGN,
        reviewer_slot="model_b",
        lane_authority=load_subscription_lane_authority(campaign["root"] / "lanes" / "model_b"),
        request_batch_digest="b" * 64,
        raw_response_digest="c" * 64,
        attested_at=NOW.isoformat(),
    )
    assert attestation.provider_request_id is None
    assert attestation.provider_response_id is None
    assert attestation.provider_metadata_available is False
    assert attestation.opaque_service_system_layer is True
    assert attestation.subscription_ui_execution is True


def test_subscription_mode_requires_campaign_opt_in():
    assert subscription_mode_permitted(CAMPAIGN, CONSENSUS_PROTOCOL_VERSION)
    assert not subscription_mode_permitted("other-campaign", CONSENSUS_PROTOCOL_VERSION)


def test_attestation_digest_binds_all_fields(campaign):
    attestation = build_attestation(
        campaign_id=CAMPAIGN,
        reviewer_slot="model_a",
        lane_authority=load_subscription_lane_authority(campaign["root"] / "lanes" / "model_a"),
        request_batch_digest="b" * 64,
        raw_response_digest="c" * 64,
        attested_at=NOW.isoformat(),
    )
    for field in ("request_batch_digest", "user_visible_model_name", "attested_at"):
        forged = dict(attestation.payload())
        forged[field] = "t" * 64 if field == "request_batch_digest" else "tampered"
        with pytest.raises(ValueError, match="attestation_digest_mismatch"):
            SubscriptionReviewAttestation.model_validate(forged)


def test_cross_lane_replay_refused(campaign):
    entries = _manifest(campaign)
    raw_a = _batch_response(entries[0], "model_a")
    result = _import(campaign, "model_a", entries[0], raw_a)
    assert result["accepted"] == entries[0]["case_count"]
    with pytest.raises(ValueError, match="subscription_batch_response_replay_refused"):
        _import(campaign, "model_b", entries[0], raw_a)


def test_cross_batch_replay_refused(campaign):
    entries = _manifest(campaign)
    raw_b1 = _batch_response(entries[0], "model_b")
    _import(campaign, "model_b", entries[0], raw_b1)
    with pytest.raises(ValueError, match="subscription_batch_response_replay_refused"):
        _import(campaign, "model_b", entries[1], raw_b1)


def test_unknown_batch_id_refused(campaign):
    entries = _manifest(campaign)
    with pytest.raises(ValueError, match="subscription_batch_not_in_canonical_manifest"):
        _import(
            campaign,
            "model_a",
            {"batch_id": "eng-calibration-001f:sub-review-999"},
            _batch_response(entries[0], "model_a"),
        )
    with pytest.raises(ValueError, match="subscription_batch_id_not_canonical"):
        _import(
            campaign,
            "model_a",
            {"batch_id": "../etc/passwd"},
            _batch_response(entries[0], "model_a"),
        )


def test_batches_lane_neutral(campaign):
    manifest = verify_review_batches(
        campaign["root"], sampling=campaign["sampling"], source_packet_digest=SOURCE_DIGEST
    )
    for entry in manifest["batches"]:
        prompt = json.loads(batch_prompt_path(campaign["root"], str(entry["batch_id"])).read_text())
        blob = json.dumps(prompt)
        for slot in REVIEWER_SLOTS:
            assert slot not in blob
            assert SUBSCRIPTION_MODEL_BY_SLOT[slot] not in blob
        for service in SERVICE_BY_SLOT.values():
            assert service not in blob


def test_generic_executor_attestation_cannot_freeze(campaign):
    from evals.calibration.subscription_ui import require_subscription_attested_identity

    entries = _manifest(campaign)
    _import(campaign, "model_b", entries[0], _batch_response(entries[0], "model_b"))
    records = load_lane_records(campaign["root"], "model_b")
    sid = entries[0]["sample_ids"][0]
    original = records[sid]
    execution = original.execution
    assert execution is not None
    forged = execution.model_copy(
        update={"identity_source": "executor_attestation", "subscription_attestation": None}
    )
    evidence = execution.evidence.model_copy(
        update={"identity_source": "executor_attestation", "subscription_attestation": None}
    )
    forged_receipt = forged.model_copy(update={"evidence": evidence})
    record = original.model_copy(update={"execution": forged_receipt})
    with pytest.raises(ValueError, match="subscription_lane_rejects_generic_executor_attestation"):
        require_subscription_attested_identity(
            {sid: record},
            lane_root=campaign["root"] / "lanes" / "model_b",
            protected_root=campaign["root"],
            sampling=campaign["sampling"],
        )


def test_machine_provider_path_unchanged(campaign):
    """The machine-verified provider path still works and still freezes."""
    from evals.calibration.consensus import ReviewerIdentity
    from tests.test_calibration_206_helpers import execution_receipt_for

    slot = "model_b"
    machine_root = campaign["root"].parent / "machine-campaign"
    machine_root.mkdir(parents=True, exist_ok=True)
    sampling = campaign["sampling"]
    packet_path, packet_manifest = _write_packet(machine_root, campaign["packet"])
    reviewer = ReviewerIdentity(
        reviewer_slot=slot,
        reviewer_family="gpt-astra",
        provider_model_identifier="gpt-astra-exact-2026-09",
        reviewer_config_digest=CONFIG_DIGEST,
        prompt_digest=labeling_instructions_digest(),
    )
    LaneSession.init(
        machine_root,
        reviewer=reviewer,
        campaign_id=CAMPAIGN,
        sampling=sampling,
        source_packet_digest=SOURCE_DIGEST,
        neutral_packet_path=packet_path,
        neutral_packet_manifest=packet_manifest,
    )
    assert lane_provenance_mode(machine_root / "lanes" / slot) == "provider_metadata"
    session = LaneSession(machine_root, slot)
    session.emit_requests(packet_path, sampling=sampling, manifest_path=packet_manifest)
    for sid in IDS:
        raw = json.dumps(
            {
                "sample_id": sid,
                "outcome": "judged",
                "judgment": {
                    "fields": {
                        "expected_kind": "fact",
                        "retention_value": "retain",
                        "epistemic_state": "adequately_supported",
                        "consequence": "low",
                        "acceptable_abstention": "no",
                    },
                    "reviewer_confidence": "medium",
                },
            }
        )
        receipt = execution_receipt_for(
            machine_root / "lanes" / slot,
            reviewer,
            sid,
            request_generation=1,
            campaign_id=CAMPAIGN,
            executor_status="completed",
        )
        session.ingest_response(
            {
                "sample_id": sid,
                "execution": receipt.model_dump(mode="json"),
                "raw_response": raw,
            },
            sampling=sampling,
        )
    lane = freeze_lane(
        protected_root=machine_root,
        reviewer=reviewer,
        campaign_id=CAMPAIGN,
        sampling=sampling,
        source_packet_digest=SOURCE_DIGEST,
    )
    assert lane.reviewer.provider_model_identifier == "gpt-astra-exact-2026-09"


def test_resume_after_completed_batches(campaign):
    entries = _manifest(campaign)
    for entry in entries:
        _import(campaign, "model_a", entry, _batch_response(entry, "model_a"))
    for entry in entries:
        result = _import(campaign, "model_a", entry, _batch_response(entry, "model_a"))
        assert result["accepted"] == 0
        assert result["resumed_duplicates"] == entry["case_count"]
    nxt = next_outstanding_batch(campaign["root"], sampling=campaign["sampling"])
    assert nxt is not None and nxt["lane_gaps"]["model_a"] == 0
    assert nxt["lane_gaps"]["model_b"] == entries[0]["case_count"]


def test_raw_attempt_bytes_digest_bound_at_freeze(campaign):
    entries = _manifest(campaign)
    raw = _batch_response(entries[0], "model_c")
    _import(campaign, "model_c", entries[0], raw)
    lane_root = campaign["root"] / "lanes" / "model_c"
    raw_path = attempt_raw_path(lane_root, str(entries[0]["batch_id"]), 1)
    raw_path.write_bytes(b'{"results": []}')
    from evals.calibration.subscription_ui import require_subscription_attested_identity

    records = load_lane_records(campaign["root"], "model_c")
    with pytest.raises(ValueError):
        require_subscription_attested_identity(
            records,
            lane_root=lane_root,
            protected_root=campaign["root"],
            sampling=campaign["sampling"],
        )


def test_serving_invariants_unchanged():
    from engram.config import settings
    from engram.recall_profiles import CERTIFIED_SERVING_PROFILES

    assert settings.assessment_selection_enabled is False
    assert {"legacy"} == set(CERTIFIED_SERVING_PROFILES)


def test_no_real_frontier_review_executed():
    import inspect

    source = inspect.getsource(subscription_ui)
    for forbidden in ("openrouter", "api.openai", "api.anthropic", "z.ai/api"):
        assert forbidden not in source.lower().replace("_", ""), forbidden
    for module in ("requests", "httpx", "urllib", "socket"):
        assert module not in sys.modules or module not in source, module


def test_batch_result_fields_frozen():
    assert BATCH_RESULT_FIELDS == (
        "sample_id",
        "expected_kind",
        "retention_value",
        "epistemic_state",
        "consequence",
        "acceptable_abstention",
        "reviewer_confidence",
    )
