# ADR: Versioned memory assessments

Status: implemented behind disabled rollout flags.
Issue: #157. Baseline: `352b4f51ee1e18e1da6b1b70f12eab75df6daa42`.

## Decision

Use `memory_assessments` for append-only assessment history. Use
`assessment_requests` for durable request identity and execution authority.
Keep `classification_runs` and the existing promotion evaluator for compatibility.
Reassessment does not update memory content, governed kind, source prior, or
legacy classification receipts.

The contract is `engram.assessment.v1`. Its prompt is `engram.assess.1`.
Its code contract is `assessment-engine-v1`. A request pins the provider,
model, prompt, schema, inference configuration, and calibration artifact.
An unsupported target is rejected. A worker with a different deployed contract
retries the job without substituting another contract.

## Dimensions

| Dimension | Meaning | Version 1 behavior |
| --- | --- | --- |
| Taxonomy | Confidence in placement | A bounded model suggestion. It cannot change governed kind. |
| Retention | Expected durable usefulness | A bounded raw model estimate. It does not establish truth or admission authority. |
| Epistemic | Support from recorded evidence | Unknown without provenance. Recorded origin alone means insufficient evidence. |
| Risk | Consequence of silent admission or stale recall | High for sensitive content or high-authority sources. Otherwise unknown. |
| Provenance | Recorded assertion mode and origin | Derived from #156 receipts. Conflicting modes remain unknown. |
| Reliability | Calibration status of an exact contract | Uncalibrated unless an exact, sufficiently sampled profile matches. |

Unresolved conflict or disputed review means `contested`. A governed diary entry
uses `not_applicable`. A recorded human verification provides a binary evidence
feature. It does not provide a probability. Only an exact calibration profile can
turn that feature into a calibrated value and band with state `supported`.
These rules do not inspect the open internet or count batch hashes as independent
corroboration. Missing risk remains unknown. Policies that require known risk must
reject unknown risk. This issue does not introduce such an admission policy.

## Input and evidence identity

The input digest includes canonical content identity, actual content identity,
governed kind, source type, visibility, workspace, authority, sensitivity,
verification, review state, conflict state, and linked extraction evidence.
The request also records its actor and pinned profile execution authority.
It contains no transcript or duplicate memory content.

`assessment_evidence_manifest` returns only hashes, IDs, assertion mode, and
origin for evidence linked to an eligible item. It does not return transcript
text, source excerpts, or unrelated receipts. Its fixed-search-path definer
function checks tenant and principal item eligibility before returning metadata.
The API and worker also apply the memory profile before calling it. This permits
all eligible readers to use the same normalized assessment without granting
access to another writer's extraction transcript. At most 64 evidence links are
accepted. Larger inputs fail explicitly.

Workers keep the item unlocked during inference. They lock and reload it before
saving the result. A changed input digest produces an immutable `stale` row.
Stale rows cannot become effective. A new request uses the new digest.
The provider receives at most 16,000 input bytes. It has a 30-second client
timeout, a 35-second outer timeout, and a 1,024-token output limit.
Only validated, bounded taxonomy and numeric output fields are stored.
Provider text and exception messages are discarded. Secret checks run before
submission and output storage.

## Request lifecycle

A durable unique key covers tenant, item, purpose, target contract, and input
digest. Transaction advisory locks serialize identical requests and duplicate
worker execution. Each queue attempt can append one assessment. A partial unique
index permits one completed assessment per request. Old workers continue to use
`classification_runs`. They cannot replace new assessment history.

Provider failure or disablement commits an explicit failed or disabled assessment
before the existing queue schedules a retry. Scores remain null. The queue exposes
attempt counts and dead-letter state. A reviewer can retry a dead request when its
input and contract still match. Retry preserves attempt numbers and receipts.
Retry lookup uses current item write eligibility and review scope, regardless
of the original request principal. The worker continues under the request's
immutable pinned execution authority and original request actor. The retry
action itself records the current reviewer as actor, with its reason, request
ID, and target hash in an item event.
After an input or target change, the reviewer must create a new request.

Batches contain at most 100 eligible items. Continue with the last returned item
ID. An evidence validation error fails the batch with HTTP 422 and rolls back
all requests from that batch. The queue orders reassessment tenants by their
last attempt only when selecting the reassessment lane. It retains the existing
global due-time order for mixed and other job-type claims. Successful assessment and the
`classification_reassessed` promotion trigger commit together. The durable
follow-up runs the existing promotion evaluator. No threshold, cooling window,
or admission evidence weight changes in this issue.

## Effective selection

Selection is a deterministic read policy. It does not mutate assessment rows.
The operator must enable selection and pin an exact contract hash. For each
purpose, choose the earliest completed row with that contract and the current
input digest. Creation time and assessment ID break ties. A different model,
newer receipt, failed attempt, stale result, or legacy backfill cannot replace it.
When no row matches, effective selection is empty. History remains queryable.
A policy version identifies the selection configuration. Change that version
when changing its pinned contract. Item detail and promotion readiness expose
the policy version and effective IDs, schema versions, and normalized values.

## Calibration

Calibration artifacts are operator-installed JSON files. They must match provider,
model, prompt, schema, code, inference configuration, source type, assertion mode,
governed kind, risk, and a labeled dataset version. The request pins the artifact
digest and version. A returned model that differs from the configured model cannot
use its calibration profile. There is no implicit pooling or model transfer.
Each applicable bin needs at least 50 samples. Smaller or mismatched bins remain
uncalibrated. Calibrated bands use the Wilson 95% interval for the labeled outcome
rate. Raw values remain separate.

`evals/assessments/evaluate.py` reports reliability bins, Brier score, expected
calibration error, abstention rate, kind confusion, and sample counts. It does not
fit or install a profile. The checked-in dataset contains synthetic metric
regression fixtures. It is not a production calibration study. No production
calibration profile ships with this change.

## Authorization and compatibility

Assessment tables have FORCE RLS and item eligibility policies. The application
role can insert and read history. It cannot update or delete history. Triggers
also reject updates and invalid links. Reassessment requires review scope and
existing-item write eligibility. Review scope does not expand item visibility.
Provider details require review scope plus item read eligibility. Ordinary reads
return only normalized values, bounded reasons, identities, and hashes.

The canonical hash uses SHA-256 over the PostgreSQL 16 JSONB serialization of
the receipt and its identity envelope. The encoding version is `pg-jsonb-v1`.
It includes the creation time in UTC. This is a database-attested hash. It is
not a digital signature or proof that the proposition is true.

Migration 037 copies existing bound classification receipts into legacy assessment
rows. It preserves taxonomy, retention, disposition, content/context hashes, and
known provider versions. Unknown prompt/configuration versions remain null.
Epistemic state and risk remain unknown. Migration reapplication does not change
existing rows. Unbound receipts are not backfilled.

`/v1/classify` keeps its existing fields. `confidence` is deprecated and means
taxonomy confidence only. New `assessment_dimensions` fields preserve unknown
as null. A legacy rules-only retention zero maps to unknown in the new field.
The SDK and MCP preserve this distinction. `memory_confidence` remains the
historical source-policy prior. It is not a generic factual-confidence measure.

## Rollout and rollback

All three flags default to false:

- `ENGRAM_ASSESSMENT_REASSESSMENT_ENABLED` enables API requests and workers.
- `ENGRAM_ASSESSMENT_CAPTURE_ENABLED` adds requests to explicit-kind and automatic capture.
- `ENGRAM_ASSESSMENT_SELECTION_ENABLED` exposes effective selection.

Before selection, set `ENGRAM_ASSESSMENT_EFFECTIVE_CONTRACT_HASH` to the reviewed
contract hash and set `ENGRAM_ASSESSMENT_POLICY_VERSION`. Calibration defaults to
`uncalibrated`. An optional `ENGRAM_ASSESSMENT_CALIBRATION_PROFILES_PATH` provides
reviewed profiles. Its version must match `ENGRAM_ASSESSMENT_CALIBRATION_VERSION`.
These flags do not select a new promotion policy.

To roll back, disable capture, selection, and reassessment. Keep the tables.
The downgrade file preserves immutable history. Disabled workers leave queued
work visible through retry/dead-letter state. Legacy classification and promotion
continue to use their existing tables and policy.
