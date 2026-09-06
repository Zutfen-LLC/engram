# Issue #157 completion report

## Schema and compatibility

See [the ADR](adr-157-versioned-assessments.md) and migration 037.
`classification_runs` remains the compatibility receipt. `memory_assessments`
supports multiple immutable historical runs. Effective selection requires a
pinned contract and matching current input. It never selects the newest model
solely because it is newer.

| Existing field | Compatibility mapping |
| --- | --- |
| `confidence` | Deprecated taxonomy-only alias |
| `taxonomy_confidence` | Raw taxonomy score |
| `retention_confidence` | Raw usefulness score; rules-only zero becomes null in the additive API field |
| `retention_disposition` | Storage disposition; no truth or admission claim |
| `memory_confidence` | Historical source-policy prior; unchanged |
| Bound classification receipt | Immutable legacy assessment with unknown epistemic state and risk |

## Reassessment trigger matrix

| Trigger | Interface | Result |
| --- | --- | --- |
| Manual request | `POST /v1/items/{id}/reassess` | Idempotent request for a purpose and deployed target contract |
| Bounded tenant query | `POST /v1/assessments/reassess` | At most 100 eligible items; item-ID pagination |
| Provider recovery | New request or dead-request `/retry` | Preserves failure history; appends a later attempt |
| Model/prompt/schema upgrade | Deploy the supported contract, then request `model_upgrade` | New identity; old effective contract remains pinned |
| Provenance added | Request `provenance_added` | New evidence digest; stale results cannot bind |
| Human correction | Request `human_correction` | Rechecks verification, review, and conflict state |
| Policy rollout | Request `policy_rollout` | Audits the target; does not change promotion thresholds |
| Explicit-kind capture | Capture flag | Queues assessment without changing governed kind |
| Structured extraction | Capture flag | Queues after extraction links exist in the same transaction |
| Assessment completed | Durable `classification_reassessed` job | Reuses the existing promotion evaluator |

Read history at `GET /v1/items/{id}/assessments`. Read request state at
`GET /v1/items/{id}/reassessments/{request_id}`. A review-scoped debug endpoint
returns bounded provider output and evidence references. Item detail and
promotion readiness expose safe effective assessment fields. The SDK provides
`assessments`, `reassess`, and `reassessment_status`. MCP provides
`engram_assessments` and `engram_reassess`.

## Calibration dataset and results

The dataset is `evals/assessments/golden-v1.json`. Results are in
`evals/assessments/results-v1.json`. Reproduce them with:

```bash
.venv/bin/python -m evals.assessments.evaluate \
  evals/assessments/golden-v1.json \
  --output evals/assessments/results-v1.json
```

The 12 synthetic samples verify metric calculations and unknown handling.
They do not establish production calibration, admission precision, or factual
accuracy. Every stratum remains uncalibrated. The synthetic Brier score is 0.22925. Expected calibration error is 0.365.
Abstention is 2 of 12 samples. These are fixture results, not provider quality
measurements. The results include reliability bins, confusion, and sample counts.
No production profile is installed.

## Verification

The assessment tests exercise the HTTP API, worker boundary, calibration
interface, and PostgreSQL application role. They cover provider disablement,
provider failure and recovery, immutable historical rows, duplicate requests,
two workers, model upgrades, simultaneous review, RLS, profile restrictions,
migration reapplication, explicit-kind capture, and tenant queue fairness.
The calibration tests reject mismatched contracts and undersampled bins.

`make check` passed with a real PostgreSQL database: lint and strict type
checks passed; 3,596 tests passed and 2 tests were skipped.
Database skip enforcement was enabled.

`make compose-ci` passed against a fresh PostgreSQL and pgvector database
with the non-owner application role. The root suite had 3,564 passed tests
and 34 skipped tests. The SDK had 56 passed tests. The hooks adapter had
190 passed tests. The MCP adapter had 36 passed tests.

The calibration evaluation reproduced the checked-in results without changes.

## Rollout and naming debt

Reassessment, automatic capture, and effective selection default to disabled.
Selection also requires an exact contract hash. Rollback disables the flags and
preserves history. The existing promotion evaluator remains authoritative.
A future admission policy must explicitly consume epistemic and risk dimensions.

Legacy naming remains in `confidence`, `memory_confidence`,
`retention_confidence`, `retention_evidence_at`, `retention_policy_version`,
`evidence_score`, and `classification_runs`. These compatibility names do not
imply factual confidence or calibrated probability.

## Standards

No remaining findings. The review identified missing enum constraints and
duplicate response serialization. Both were corrected.

## Spec

No remaining findings. The review identified retention availability tied to
taxonomy mode and calibration artifact replacement during inference. Regression
tests reproduced both defects. The corrected implementation passes them.

Standards: 0 remaining findings. Spec: 0 remaining findings.
