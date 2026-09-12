"""Deterministic operator path for #216's DEV-only machine review campaign."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

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
from evals.calibration.machine_reviewer_216 import (
    CommandRunner,
    MachineReviewerAuthority,
    MachineReviewerRunner,
)
from evals.calibration.model_lanes import freeze_lane

MACHINE_IDENTITIES_216: dict[str, tuple[str, str, str]] = {
    "model_a": (
        "openrouter",
        "anthropic/claude-sonnet-5",
        "environment_bound_provider_credentials",
    ),
    "model_b": ("openai-codex", "gpt-5.6-sol", "local_authenticated_profile"),
    "model_c": ("zai", "glm-5.3", "environment_bound_provider_credentials"),
}


@dataclass(frozen=True)
class RuntimeIdentity216:
    hermes_version: str
    provider: str
    model: str
    endpoint_routing: str


RuntimePreflight = Callable[[str, str], RuntimeIdentity216]


def default_preflight(provider: str, model: str) -> RuntimeIdentity216:
    """Resolve the forced route with the installed Hermes runtime before output.

    The Hermes executable's adjacent interpreter imports the same installed
    resolver that ``hermes chat --provider/-m`` uses. The tiny child prints
    only non-secret routing fields; a provider whose credentials are absent
    fails before a lane authority or review process can exist.
    """
    executable = shutil.which("hermes")
    if executable is None:
        raise ValueError("machine_reviewer_runtime_preflight_failed")
    hermes_path = Path(executable).resolve()
    python = hermes_path.parent / "python3"
    probe = (
        "import json,sys; from hermes_cli.runtime_provider import resolve_runtime_provider; "
        "r=resolve_runtime_provider(requested=sys.argv[1],target_model=sys.argv[2]); "
        "print(json.dumps({'provider':r.get('provider'),'base_url':r.get('base_url'),"
        "'api_mode':r.get('api_mode')}))"
    )
    version = subprocess.run(("hermes", "--version"), check=False, capture_output=True, text=True)
    route = subprocess.run(
        (str(python), "-c", probe, provider, model), check=False, capture_output=True, text=True
    )
    if version.returncode != 0 or route.returncode != 0:
        raise ValueError("machine_reviewer_runtime_preflight_failed")
    try:
        resolved = json.loads(route.stdout)
        resolved_provider = resolved["provider"]
        endpoint = resolved["base_url"]
        api_mode = resolved["api_mode"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("machine_reviewer_runtime_preflight_failed") from exc
    if not all(
        isinstance(value, str) and value for value in (resolved_provider, endpoint, api_mode)
    ):
        raise ValueError("machine_reviewer_runtime_preflight_failed")
    return RuntimeIdentity216(
        version.stdout.strip(), resolved_provider, model, f"{endpoint}|{api_mode}"
    )


def _authority(
    session: LaneSession, runtime: RuntimeIdentity216, target_digest: str, membership: str
) -> MachineReviewerAuthority:
    expected_provider, expected_model, auth = MACHINE_IDENTITIES_216[session.reviewer.reviewer_slot]
    if runtime.provider != expected_provider or runtime.model != expected_model:
        raise ValueError("machine_reviewer_runtime_identity_mismatch")
    return MachineReviewerAuthority(
        campaign_id=CAMPAIGN_ID_001K,
        reviewer=session.reviewer,
        hermes_version=runtime.hermes_version,
        resolved_provider_identifier=runtime.provider,
        resolved_model_identifier=runtime.model,
        auth_mechanism_class=auth,  # type: ignore[arg-type]
        endpoint_routing=runtime.endpoint_routing,
        config_digest=session.reviewer.reviewer_config_digest,
        prompt_digest=session.reviewer.prompt_digest,
        source_packet_digest=session.source_packet_digest,
        target_identity_digest=target_digest,
        membership_digest=membership,
        generation_params={"temperature": 0, "max_tokens": 1024},
        mode="machine_orchestrated",
        reviewer_version="machine-reviewer-216-v1",
    )


def _reviewer(slot: str, runtime: RuntimeIdentity216) -> ReviewerIdentity:
    return ReviewerIdentity(
        reviewer_slot=slot,  # type: ignore[arg-type]
        reviewer_family=CAMPAIGN_216_FAMILY_BY_SLOT[slot],
        campaign_id=CAMPAIGN_ID_001K,
        provider_model_identifier=runtime.model,
        reviewer_config_digest=digest_of(
            {
                "provider": runtime.provider,
                "model": runtime.model,
                "endpoint": runtime.endpoint_routing,
            }
        ),
        prompt_digest=labeling_instructions_digest(),
    )


def run_machine_dev_review(
    protected_root: Path,
    *,
    preflight: RuntimePreflight = default_preflight,
    command_runner: CommandRunner | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    """Run exactly the frozen DEV-102 population, never consensus or holdout.

    The three runtime identities are captured and checked before creating any
    lane. A non-dry run emits lane-local requests, runs one full 102-case batch
    per lane concurrently, retains failed whole-batch attempts, ingests the
    first accepted output per case, and freezes all three lanes.
    """
    sampling = SamplingManifest.model_validate(
        json.loads((protected_root / "dev-sampling-manifest.json").read_text())
    )
    source_packet = protected_root / f"{CAMPAIGN_ID_001K}-dev-v1.blind.json"
    source_digest = hashlib.sha256(source_packet.read_bytes()).hexdigest()
    stage = verify_stage_authority(
        protected_root=protected_root, sampling=sampling, source_packet_digest=source_digest
    )
    stage.require_capability()
    if stage.stage != "dev" or len(sampling.sample_ids) != 102:
        raise ValueError("machine_reviewer_requires_exact_dev_102")
    reuse = json.loads((protected_root / "reuse-manifest.json").read_text())
    if set(sampling.sample_ids) & set(reuse["holdout_ids"]):
        raise ValueError("machine_reviewer_holdout_leakage")

    runtimes = {
        slot: preflight(provider, model)
        for slot, (provider, model, _auth) in MACHINE_IDENTITIES_216.items()
    }
    for slot, runtime in runtimes.items():
        provider, model, _auth = MACHINE_IDENTITIES_216[slot]
        if runtime.provider != provider or runtime.model != model:
            raise ValueError("machine_reviewer_runtime_identity_mismatch")
    report: dict[str, object] = {
        "campaign_id": CAMPAIGN_ID_001K,
        "stage": "dev",
        "logical_cases": len(sampling.sample_ids),
        "holdout": 0,
        "identities": {
            slot: {
                "family": CAMPAIGN_216_FAMILY_BY_SLOT[slot],
                "provider": runtime.provider,
                "model": runtime.model,
            }
            for slot, runtime in runtimes.items()
        },
        "lanes": {},
    }
    if dry_run:
        return report

    neutral_packet = protected_root / f"{CAMPAIGN_ID_001K}-dev-v1.neutral.json"
    neutral_manifest = protected_root / "neutral-packet-manifest.json"
    sessions: dict[str, LaneSession] = {}
    runners: dict[str, MachineReviewerRunner] = {}
    batches: dict[str, Path] = {}
    for slot in REVIEWER_SLOTS:
        session = LaneSession.init(
            protected_root,
            reviewer=_reviewer(slot, runtimes[slot]),
            campaign_id=CAMPAIGN_ID_001K,
            sampling=sampling,
            source_packet_digest=source_digest,
            neutral_packet_path=neutral_packet,
            neutral_packet_manifest=neutral_manifest,
            provenance_mode="machine_executor_provenance",
        )
        sessions[slot] = session
        runners[slot] = MachineReviewerRunner(
            session,
            _authority(
                session, runtimes[slot], stage.target_identity_digest, stage.membership_digest
            ),
            command_runner=command_runner,
        )
        batches[slot] = session.emit_requests(
            neutral_packet, sampling=sampling, manifest_path=neutral_manifest
        )

    def execute(slot: str) -> tuple[str, int]:
        attempts = runners[slot].review_emitted_batch(
            batches[slot], chunk_size=102, max_format_attempts=2
        )
        if len(attempts) != 102:
            raise ValueError("machine_reviewer_batch_count_mismatch")
        for line in batches[slot].read_text().splitlines():
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
        results = list(pool.map(execute, REVIEWER_SLOTS))
    report["lanes"] = {slot: {"accepted": count, "frozen": True} for slot, count in results}
    return report
