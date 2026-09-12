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

from typing import Any

from evals.admission.schema import Record
from evals.calibration.campaign_001k import (
    DIMENSIONS_001K,
    EXECUTED_PREFIX,
    ReusedLabelSet,
)
from evals.calibration.fit import LabeledObservation
from evals.calibration.freeze import FrameRow, SplitManifest


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


def observations_from_fresh_labels(
    *,
    labels_by_sample: dict[str, dict[str, Any]],
    provider_values: dict[str, dict[str, Any]],
    split: SplitManifest,
    frame_by_id: dict[str, FrameRow],
    dimensions: tuple[str, ...] = DIMENSIONS_001K,
) -> list[LabeledObservation]:
    """Observations for fresh-202 cases from verified fresh labels + assess.3 outputs.

    ``labels_by_sample`` must come from verified consensus/human evidence
    (frozen lanes + queue) — never free-form entry.
    """
    split_by_id = {sid: "dev" for sid in split.dev_ids}
    split_by_id.update({sid: "holdout" for sid in split.holdout_ids})
    fresh_ids = sorted(labels_by_sample)
    # fresh population is 202; partial label sets are allowed only for
    # pre-fit audits and must still sit inside the frozen split
    if len(fresh_ids) != EXECUTED_PREFIX + 2 and not (
        set(fresh_ids) <= (set(split.dev_ids) | set(split.holdout_ids))
    ):
        raise ValueError("fresh_labels_outside_split")
    out: list[LabeledObservation] = []
    for sid in fresh_ids:
        row = frame_by_id[sid]
        values = provider_values.get(sid)
        if values is None:
            # Honest abstention/parse failure: no provider numeric. The fresh
            # label still counts toward review totals via the floor evaluator.
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
            split=split_by_id[sid],
            dimensions=_reused_dimensions_view(labels_by_sample[sid]),
            raw_scores=raw_scores,
            suggested_kind=suggested,
            stratum=_frame_stratum(row),
        )
        out.extend(o for o in obs if o.dimension in dimensions)
    return out


class ProviderEvidence216(Record):
    """Digest-bound assess.3 provider outputs for the 001k population."""

    run_kind: str
    prompt_version: str
    model: str
    code_git_head: str
    target_identity_digest: str
    values_by_sample: dict[str, dict[str, Any]]
    ok_count: int
    error_count: int

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ProviderEvidence216:
        if payload.get("prompt_version") != "engram.assess.3":
            raise ValueError("provider_evidence_wrong_prompt_version")
        if payload.get("run_kind") not in (
            "issue-214-protected-200-case-replay-assess3",
            "issue-216-fresh-202-assess3",
        ):
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
            values_by_sample=values,
            ok_count=ok,
            error_count=err,
        )
