"""Deterministic operator path for #216's DEV-only machine review campaign."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from evals.calibration.campaign_001k import CAMPAIGN_ID_001K
from evals.calibration.consensus import (
    CAMPAIGN_216_FAMILY_BY_SLOT,
    ReviewerIdentity,
    digest_of,
)
from evals.calibration.ingestion import LaneSession, labeling_instructions_digest
from evals.calibration.machine_reviewer_216 import (
    CommandRunner,
    MachineReviewerAuthority,
)

#: FIX7 (#217): the stable, explicit superseded-mode error for the Hermes
#: machine executor reviewer path.  ``216-machine-dev-review`` fails closed
#: with this error before any lane creation or model execution, and
#: ``run_machine_dev_review`` raises it on every call — direct HTTPS
#: (``216-api-dev-review``, ``direct_api_provenance``) is the ONE active
#: reviewer authority for ``eng-calibration-001k``.
MACHINE_DEV_REVIEW_SUPERSEDED_ERROR = "campaign_001k_machine_reviewer_superseded"

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
    """SUPERSEDED (FIX7, #217): can never create active 001k evidence.

    Direct HTTPS (``216-api-dev-review`` / ``direct_api_provenance``) is the
    ONE active reviewer authority for ``eng-calibration-001k``.  This Hermes
    machine-executor path is superseded and fails closed immediately —
    before any lane creation or model execution, wet or dry — with the
    stable explicit error ``campaign_001k_machine_reviewer_superseded``.
    """
    del protected_root, preflight, command_runner, dry_run
    raise ValueError(MACHINE_DEV_REVIEW_SUPERSEDED_ERROR)
