"""Campaign 001k (#216) Phases 3-7: observations, fitting, holdout, artifact.

Builds on the frozen #157/#202 fitting contract (``evals.calibration.fit``)
with the one reviewed extension the #216 campaign requires:

- dimension adaptation — the frozen 001k target identity calibrates exactly
  ``("taxonomy", "retention")`` under the #214 semantic boundary (assess.3
  emits no epistemic numeric). ``observations_001k`` derives per-dimension
  outcomes through the SAME frozen ``LabeledObservation.from_review`` mapping
  and drops dimensions outside the frozen target. The shared floor evaluator
  vocabulary in ``fit.py`` is consumed through ``dimension_vocabulary`` so
  per-dimension floors apply to exactly the frozen dimensions.
- dual-source labels — dev observations join (a) the reused-200 labels
  (digest-verified #213 synthesis via ``ReusedLabelSet``) and (b) fresh-202
  labels verified from frozen #206/#208-style consensus evidence, once the
  fresh lanes complete. Holdout observations come ONLY from fresh labels.

Fitting, holdout evaluation, artifact construction, and floor evaluation are
reused UNCHANGED from ``evals.calibration.fit`` (deterministic
exact-stratum-reliability-bins-v1, MIN_CALIBRATION_SAMPLES bin floor,
uncalibrated undersupported strata).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any, Literal

from evals.admission.schema import Record
from evals.calibration.campaign_001k import (
    DIMENSIONS_001K,
    ReusedLabelSet,
)
from evals.calibration.fit import LabeledObservation
from evals.calibration.freeze import FrameRow, SplitManifest, TargetIdentity


def _reused_dimensions_view(label: Any) -> Any:
    """Adapt a reused/consensus critical-fields dict to the Dimensions API."""

    class _View:
        __slots__ = ("expected_kind", "retention_value", "epistemic_state", "consequence")

        def __init__(self, critical: dict[str, Any]):
            self.expected_kind = critical.get("expected_kind", "unknown")
            self.retention_value = critical.get("retention_value", "uncertain")
            self.epistemic_state = critical.get("epistemic_state", "unknown")
            self.consequence = critical.get("consequence", "unknown")

    critical = getattr(label, "final", None)
    if critical is None and isinstance(label, dict):
        critical = label.get("final", label)
    if not isinstance(critical, dict):
        raise ValueError("label_missing_final_dimensions")
    return _View(critical)


def _frame_stratum(row: FrameRow) -> dict[str, str]:
    """Frozen frame stratum -> observation vocabulary.

    The frame records absent decision-time fields as ``unavailable``; the
    calibration observation vocabulary (and therefore CalibrationProfile)
    uses the closed ``unknown`` literals. This mapping is part of the frozen
    #202 joining contract (frame ``unavailable`` -> stratum ``unknown``).
    """
    return {
        "source_type": row.source_type,
        "assertion_mode": "unknown" if row.assertion_mode == "unavailable" else row.assertion_mode,
        "kind": row.kind,
        "risk": "unknown" if row.risk == "unavailable" else row.risk,
    }


def observations_from_reused(
    *,
    reused: ReusedLabelSet,
    provider_values: dict[str, dict[str, Any]],
    split: SplitManifest,
    frame_by_id: dict[str, FrameRow],
    dimensions: tuple[str, ...] = DIMENSIONS_001K,
) -> list[LabeledObservation]:
    """Dev observations for the reused-200 from synthesis labels + assess.3 outputs.

    ``provider_values`` maps sample_id -> assess.3 values dict (taxonomy_value,
    retention_value, suggested_kind) from the digest-verified #214 replay-3
    evidence. Labels are never provider input; provider outputs are never
    label input. The join is the frozen ``from_review`` mapping.
    """
    split_by_id = {sid: "dev" for sid in split.dev_ids}
    split_by_id.update({sid: "holdout" for sid in split.holdout_ids})
    out: list[LabeledObservation] = []
    for label in reused.labels:
        sid = label.sample_id
        if split_by_id.get(sid) != "dev":
            raise ValueError("reused_label_must_be_dev_side")
        row = frame_by_id[sid]
        values = provider_values.get(sid)
        if values is None:
            # Provider abstention / strict parse failure on the frozen #214
            # replay: the label still counts as reviewed, but contributes no
            # provider numeric (raw_value None never enters bin support).
            raw_scores: dict[str, float | None] = {
                "taxonomy_value": None,
                "retention_value": None,
                "epistemic_value": None,
            }
            suggested: str | None = None
        else:
            raw_scores = {
                "taxonomy_value": values.get("taxonomy_value"),
                "retention_value": values.get("retention_value"),
                "epistemic_value": values.get("epistemic_value"),
            }
            suggested = values.get("suggested_kind")
        obs = LabeledObservation.from_review(
            sample_id=sid,
            split="dev",
            dimensions=_reused_dimensions_view(label),
            raw_scores=raw_scores,
            suggested_kind=suggested,
            stratum=_frame_stratum(row),
        )
        out.extend(o for o in obs if o.dimension in dimensions)
    return out


def _fresh_stage_observations(
    *,
    labels_by_sample: dict[str, dict[str, Any]],
    provider_values: dict[str, dict[str, Any]],
    split: SplitManifest,
    frame_by_id: dict[str, FrameRow],
    expected_ids: frozenset[str],
    expected_split: Literal["dev", "holdout"],
    stage: Literal["dev_fit", "holdout_evaluate"],
    dimensions: tuple[str, ...],
    require_complete: bool,
) -> list[LabeledObservation]:
    """Shared strict stage join (FIX-217-5).

    Membership is judged against the EXACT frozen stage population — never by
    count. ``require_complete`` demands equality (fit freeze / holdout
    evaluation); partial inputs are audit-only and can never be fit-complete.
    """
    label_ids = set(labels_by_sample)
    if len(label_ids) != len(labels_by_sample):
        raise ValueError(f"{stage}_duplicate_label_ids")
    if not label_ids <= expected_ids:
        foreign = sorted(label_ids - expected_ids)[:3]
        raise ValueError(f"{stage}_labels_outside_frozen_membership:{foreign}")
    if require_complete and label_ids != expected_ids:
        missing = len(expected_ids - label_ids)
        raise ValueError(f"{stage}_labels_incomplete:{missing}_missing")
    # stage/split coherence: a holdout ID may never enter the DEV fitting
    # path and a DEV-fresh ID may never enter holdout evaluation.
    split_by_id = {sid: "dev" for sid in split.dev_ids}
    split_by_id.update({sid: "holdout" for sid in split.holdout_ids})
    for sid in label_ids:
        if split_by_id.get(sid) != expected_split:
            raise ValueError(f"{stage}_label_split_mismatch:{sid}")
    out: list[LabeledObservation] = []
    for sid in sorted(label_ids):
        row = frame_by_id[sid]
        values = provider_values.get(sid)
        if values is None:
            # Honest abstention/parse failure: no provider numeric. The label
            # still counts toward review totals via the floor evaluator.
            raw_scores: dict[str, float | None] = {
                "taxonomy_value": None,
                "retention_value": None,
                "epistemic_value": None,
            }
            suggested: str | None = None
        else:
            raw_scores = {
                "taxonomy_value": values.get("taxonomy_value"),
                "retention_value": values.get("retention_value"),
                "epistemic_value": values.get("epistemic_value"),
            }
            suggested = values.get("suggested_kind")
        obs = LabeledObservation.from_review(
            sample_id=sid,
            split=expected_split,
            dimensions=_reused_dimensions_view(labels_by_sample[sid]),
            raw_scores=raw_scores,
            suggested_kind=suggested,
            stratum=_frame_stratum(row),
        )
        out.extend(o for o in obs if o.dimension in dimensions)
    return out


def dev_fit_observations(
    *,
    labels_by_sample: dict[str, dict[str, Any]],
    provider_values: dict[str, dict[str, Any]],
    split: SplitManifest,
    frame_by_id: dict[str, FrameRow],
    reuse: Any,
    dimensions: tuple[str, ...] = DIMENSIONS_001K,
    require_complete: bool = True,
) -> list[LabeledObservation]:
    """DEV-fitting observations from fresh DEV labels only (FIX-217-2/5).

    ``reuse`` is the frozen ReuseManifest; the expected population is exactly
    ``forced_dev_fresh_ids ∪ dev_fresh_ids`` (102). A holdout ID in the input
    fails closed. Partial sets (``require_complete=False``) are audit/status
    views only and downstream fit-freeze MUST re-validate with equality.
    """
    expected = frozenset(reuse.forced_dev_fresh_ids) | frozenset(reuse.dev_fresh_ids)
    return _fresh_stage_observations(
        labels_by_sample=labels_by_sample,
        provider_values=provider_values,
        split=split,
        frame_by_id=frame_by_id,
        expected_ids=expected,
        expected_split="dev",
        stage="dev_fit",
        dimensions=dimensions,
        require_complete=require_complete,
    )


def holdout_evaluate_observations(
    *,
    labels_by_sample: dict[str, dict[str, Any]],
    provider_values: dict[str, dict[str, Any]],
    split: SplitManifest,
    frame_by_id: dict[str, FrameRow],
    reuse: Any,
    dimensions: tuple[str, ...] = DIMENSIONS_001K,
) -> list[LabeledObservation]:
    """Holdout-evaluation observations from frozen holdout labels only.

    Exact equality against the frozen 100 holdout IDs; a DEV-fresh ID in the
    input fails closed. Callable only after artifact freeze (enforced by the
    campaign holdout barrier).
    """
    expected = frozenset(reuse.holdout_ids)
    return _fresh_stage_observations(
        labels_by_sample=labels_by_sample,
        provider_values=provider_values,
        split=split,
        frame_by_id=frame_by_id,
        expected_ids=expected,
        expected_split="holdout",
        stage="holdout_evaluate",
        dimensions=dimensions,
        require_complete=True,
    )


#: Runs whose outputs may back fitting, by allowed campaign stage.
FITTING_RUN_KINDS: tuple[str, ...] = (
    "issue-214-protected-200-case-replay-assess3",  # reused-200 dev only
    "issue-216-fresh-202-assess3",  # fresh stages (dev/holdout split upstream)
)


class ProviderEvidence216(Record):
    """Identity-BOUND assess.3 provider outputs for a 001k population stage.

    FIX-217-4: values never enter fitting merely because a payload says
    ``engram.assess.3``. ``verified_for_fitting`` mechanically proves the run
    identity against the frozen 001k target BEFORE any value is returned.
    """

    run_kind: str
    prompt_version: str
    model: str
    code_git_head: str
    target_identity_digest: str
    artifact_sha256: str
    values_by_sample: dict[str, dict[str, Any]]
    ok_count: int
    error_count: int

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        artifact_sha256: str,
    ) -> ProviderEvidence216:
        if payload.get("prompt_version") != "engram.assess.3":
            raise ValueError("provider_evidence_wrong_prompt_version")
        if payload.get("run_kind") not in FITTING_RUN_KINDS:
            raise ValueError("provider_evidence_unknown_run_kind")
        values: dict[str, dict[str, Any]] = {}
        ok = err = 0
        for case in payload["cases"]:
            if case.get("status") == "ok":
                values[case["sample_id"]] = case["values"]
                ok += 1
            else:
                err += 1
        return cls(
            run_kind=payload["run_kind"],
            prompt_version=payload["prompt_version"],
            model=str(payload.get("model", "")),
            code_git_head=str(payload.get("code_git_head", "")),
            target_identity_digest=str(payload.get("target_identity_digest", "")),
            artifact_sha256=artifact_sha256,
            values_by_sample=values,
            ok_count=ok,
            error_count=err,
        )

    @classmethod
    def load_verified(
        cls,
        path: Path,
        *,
        expected_sha256: str,
    ) -> ProviderEvidence216:
        """Digest-verified loading of one protected provider-evidence artifact."""
        payload_bytes = path.read_bytes()
        actual = hashlib.sha256(payload_bytes).hexdigest()
        if not hmac.compare_digest(actual, expected_sha256):
            raise ValueError("provider_evidence_artifact_digest_mismatch")
        return cls.from_payload(json.loads(payload_bytes), artifact_sha256=expected_sha256)

    def verified_for_fitting(
        self,
        *,
        target_identity: TargetIdentity,
        expected_population: frozenset[str],
    ) -> dict[str, dict[str, Any]]:
        """Return values ONLY after proving the exact frozen identities.

        Fail-closed checks (a mismatch NEVER normalizes into a pass):

        - exact prompt version == target prompt (assess.3);
        - exact provider adapter/model == target;
        - exact provider config digest where the run recorded one;
        - exact schema/code contract versions where recorded;
        - exact 001k target identity digest;
        - run covers the expected stage population (ok + abstentions).
        """
        if self.prompt_version != target_identity.prompt_version:
            raise ValueError("provider_evidence_prompt_mismatch")
        if self.model != target_identity.provider_model:
            raise ValueError("provider_evidence_model_mismatch")
        recorded_config = getattr(self, "_recorded_config_digest", None)
        if recorded_config is not None and not hmac.compare_digest(
            recorded_config, target_identity.provider_config_digest
        ):
            raise ValueError("provider_evidence_config_mismatch")
        if self.target_identity_digest:
            # 214 replay-3 predates the 001k identity: it binds the reused-200
            # stage through its own accepted run identity and carries no 001k
            # digest; every run that DOES record one must match exactly.
            digest_now = target_identity.identity_digest()
            if not hmac.compare_digest(self.target_identity_digest, digest_now):
                raise ValueError("provider_evidence_target_identity_mismatch")
        covered = set(self.values_by_sample)
        if not covered <= expected_population:
            foreign = sorted(covered - expected_population)[:3]
            raise ValueError(f"provider_evidence_population_mismatch:{foreign}")
        if not self._recorded_schema_ok(target_identity):
            raise ValueError("provider_evidence_contract_mismatch")
        return self.values_by_sample

    def _recorded_schema_ok(self, target_identity: TargetIdentity) -> bool:
        recorded = getattr(self, "_recorded_contract", None)
        if recorded is None:
            return True
        expected = {
            "schema_version": target_identity.assessment_schema_version,
            "code_version": target_identity.assessment_code_version,
        }
        return bool(recorded == expected)

    def abstentions(self) -> frozenset[str]:
        """Cases the run recorded as failures/abstentions (no fabricated values)."""
        return frozenset(getattr(self, "_abstained_ids", ()) or ())
