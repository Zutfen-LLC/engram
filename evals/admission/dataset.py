"""Validate, hash, and sample isolated evaluation datasets."""

from __future__ import annotations

from collections import Counter, defaultdict

from pydantic import AwareDatetime, model_validator

from engram.safety import has_secrets
from evals.admission.policy import ConfigSnapshot, PolicyInput, evaluate
from evals.admission.schema import LabelRecord, Manifest, Record, Sampling, Token, digest


class Sample(Record):
    sample_id: Token
    content: str | None
    policy_input: PolicyInput
    label: LabelRecord | None

    @model_validator(mode="after")
    def identity(self) -> Sample:
        if self.sample_id != self.policy_input.sample_id:
            raise ValueError("sample_identity_mismatch")
        if self.content is not None and digest(self.content) != self.policy_input.content_hash:
            raise ValueError("content_hash_mismatch")
        if self.label and (
            self.label.sample_id != self.sample_id
            or self.label.content_hash != self.policy_input.content_hash
        ):
            raise ValueError("label_identity_mismatch")
        return self


class Dataset(Record):
    manifest: Manifest
    config: ConfigSnapshot | None
    evaluation_at: AwareDatetime
    samples: tuple[Sample, ...]

    @model_validator(mode="after")
    def integrity(self) -> Dataset:
        m = self.manifest
        if tuple(s.sample_id for s in self.samples) != m.sample_ids:
            raise ValueError("sample_order_mismatch")
        if tuple(s.policy_input.content_hash for s in self.samples) != m.sample_content_hashes:
            raise ValueError("content_membership_mismatch")
        if data_digest(self.samples, self.config, self.evaluation_at) != m.data_digest:
            raise ValueError("dataset_digest_mismatch")
        for s in self.samples:
            if s.label and (
                s.label.dataset_id != m.dataset_id
                or s.label.dataset_version != m.dataset_version
                or s.label.label_schema_version != m.label_schema_version
            ):
                raise ValueError("dataset_version_mismatch")
            if m.privacy_class in ("public_synthetic", "sanitized_fixture"):
                if s.content is None or s.label is None:
                    raise ValueError("public_label_and_content_required")
                if has_secrets(s.model_dump_json()):
                    raise ValueError("public_secret_rejected")
        return self


def data_digest(
    samples: tuple[Sample, ...], config: ConfigSnapshot | None, at: AwareDatetime
) -> str:
    return digest(
        {
            "samples": [s.model_dump(mode="json") for s in samples],
            "config": config.model_dump(mode="json") if config else None,
            "evaluation_at": at.isoformat(),
        }
    )


def build_dataset(
    samples: tuple[Sample, ...],
    *,
    config: ConfigSnapshot | None,
    at: AwareDatetime,
    code_sha: str,
    dataset_id: str,
    dataset_version: str,
    privacy: str,
    sampling: Sampling,
    population_count: int,
    counts: tuple[tuple[str, int], ...],
) -> Dataset:
    manifest = Manifest.model_validate(
        {
            "manifest_schema_version": "engram-eval-dataset-manifest-v1",
            "dataset_id": dataset_id,
            "dataset_version": dataset_version,
            "label_schema_version": "engram-admission-label-v1",
            "created_at": at,
            "snapshot_as_of": at,
            "source_class": {
                "public_synthetic": "synthetic",
                "sanitized_fixture": "sanitized",
                "private_dogfood": "dogfood",
                "private_incident": "incident",
            }[privacy],
            "code_sha": code_sha,
            "sample_count": len(samples),
            "eligible_population_count": population_count,
            "allowed_use": "evaluation_only",
            "privacy_class": privacy,
            "sampling": sampling,
            "sample_ids": tuple(s.sample_id for s in samples),
            "sample_content_hashes": tuple(s.policy_input.content_hash for s in samples),
            "data_digest": data_digest(samples, config, at),
            "stratum_counts": counts,
        }
    )
    return Dataset(manifest=manifest, config=config, evaluation_at=at, samples=samples)


def stratum(
    sample: Sample, config: ConfigSnapshot | None, at: AwareDatetime, sampling: Sampling
) -> str:
    p = sample.policy_input
    r = evaluate(p, config, at)
    age = (at - p.created_at).total_seconds() / 3600
    dimensions = sample.label.final_dimensions() if sample.label else None
    values = {
        "source_type": p.source_type,
        "kind": p.kind,
        "review_status": p.review_status,
        "blocker": ",".join(sorted(r.blocker_codes)),
        "evidence_state": r.evidence_state,
        "selected_lane": r.current_selected_lane,
        "age_bucket": "lt24h"
        if age < 24
        else "24to72h"
        if age < 72
        else "72hto7d"
        if age < 168
        else "7dto30d"
        if age < 720
        else "ge30d",
        "conflict": p.conflict_resolution_status,
        "dispute": str(p.external_dispute),
        "recalled": p.recalled,
        "labeled_consequence": dimensions.consequence if dimensions else "unknown",
    }
    if "labeled_consequence" in sampling.strata and dimensions is None:
        raise ValueError("consequence_label_required")
    # The opaque key prevents private or unrecognized category text entering reports.
    return digest([values[field] for field in sampling.strata])


def select_samples(
    samples: tuple[Sample, ...],
    config: ConfigSnapshot | None,
    at: AwareDatetime,
    sampling: Sampling,
) -> tuple[tuple[Sample, ...], tuple[tuple[str, int], ...]]:
    if len({s.sample_id for s in samples}) != len(samples):
        raise ValueError("duplicate_sample")
    groups: dict[str, list[Sample]] = defaultdict(list)
    for sample in samples:
        groups[stratum(sample, config, at, sampling)].append(sample)
    selected = []
    for key in sorted(groups):
        if key in sampling.excluded_strata:
            continue
        ordered = sorted(
            groups[key], key=lambda s: (digest([sampling.selection_seed, s.sample_id]), s.sample_id)
        )
        selected.extend(
            ordered if sampling.selection_method == "census" else ordered[: sampling.per_stratum]
        )
    return tuple(sorted(selected, key=lambda s: s.sample_id)), tuple(
        sorted((key, len(group)) for key, group in groups.items())
    )


def operational_counts(dataset: Dataset) -> dict[str, object]:
    results = [
        evaluate(s.policy_input, dataset.config, dataset.evaluation_at) for s in dataset.samples
    ]
    # Only known categorical values can enter public output. Custom values become unknown.
    allowed = {
        "kind": {
            "preference",
            "fact",
            "observation",
            "decision",
            "procedure",
            "summary",
            "doctrine",
            "invariant",
            "diary_entry",
        },
        "source_type": {
            "manual",
            "import",
            "migration",
            "extraction",
            "sync_turn",
            "pre_compress",
            "session_end",
        },
    }
    counts: dict[str, object] = {}
    for field, vocabulary in allowed.items():
        counts[field] = dict(
            sorted(
                Counter(
                    value if (value := getattr(s.policy_input, field)) in vocabulary else "unknown"
                    for s in dataset.samples
                ).items()
            )
        )
    for field in (
        "evidence_state",
        "current_selected_lane",
        "current_policy_version",
        "current_job_state",
        "readiness_state",
        "would_promote",
        "terminal_under_current_policy",
    ):
        counts[field] = dict(sorted(Counter(str(getattr(r, field)) for r in results).items()))
    counts["blockers"] = dict(sorted(Counter(b for r in results for b in r.blocker_codes).items()))
    counts["population_count"] = dataset.manifest.eligible_population_count
    counts["selected_count"] = len(results)
    # Only genuinely unknown policy evaluations count here: rows where the
    # evaluator could not state a policy result at all (missing configuration).
    # A "none" policy version (known policy, no selected promotion basis) is a
    # completed evaluation and must not contribute.
    counts["unknown_policy_count"] = sum(r.current_policy_version == "unknown" for r in results)
    counts["malformed_evidence_count"] = sum(r.evidence_state == "malformed/stale" for r in results)
    return counts
