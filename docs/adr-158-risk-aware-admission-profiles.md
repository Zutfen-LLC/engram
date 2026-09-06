# ADR: Risk-aware admission profiles

Status: implemented as a shadow-only policy.
Issue: #158. Baseline: `88fca303374fca865e28d8ec6c4a68ea7a402ec2`.

## Decision

Keep `path_a_compat` as the only authoritative admission profile.

Add `risk_aware_shadow_v1` as a checked-in declarative policy. The profile
uses `engram.admission-policy.v1`. Its JSON artifact is under
`policies/admission/`. The loader validates the JSON Schema and a RFC 8785
digest of the artifact without its `artifact_digest` field. A mismatch fails
closed.

The evaluator accepts only explicit item, effective-assessment, policy, and
evaluation-time snapshots. It does not read settings, call a provider, parse
item content, access a database, mutate state, or enqueue work.

The profile consumes only the effective #157 `combined` assessment selected
under the configured selection contract. The profile artifact pins the accepted
contract hash and selection-policy version. Disabled, missing, mismatched,
failed, stale, or uncalibrated assessment state remains explicit. The evaluator
does not infer low risk from `kind` or source type.

## V2 decision contract

Migration `039_risk_aware_admission_shadow.sql` adds nullable V2 fields to
`admission_assessments`. It does not rewrite V1 rows. V2 rows use
`engram.admission-assessment.v2`, `mode='shadow'`, and
`policy_profile_key='risk_aware_shadow_v1'`.

V2 stores risk, epistemic, retention, exact effective-assessment references,
highest tier, per-surface decisions, observation window, and deterministic
reason, blocker, and next-action values. The V2 hash includes these fields and
the exact effective-assessment identities and hashes.

The migration requires V2 shadow rows to have no resulting state or linked
mutation event. Existing projection guards reject shadow rows. Therefore a V2
row cannot become current or authorize `proposed -> active`.

## Rule matrix

| Condition | Exploratory | Governed | Startup |
| --- | --- | --- | --- |
| Not live or conflict/dispute | blocked | blocked | blocked |
| Existing governance review | allow | review_required | review_required |
| High or unknown consequence | allow | review_required | review_required |
| Contested evidence | allow | review_required | review_required |
| Missing or insufficient evidence, low risk | allow | withhold | withhold |
| Missing or insufficient evidence, medium risk | allow | review_required | review_required |
| Qualified low risk | allow | allow at 0 hours | withhold |
| Qualified medium risk | allow | allow after 72 hours | withhold |
| Qualified, existing human verified authority | allow | allow | allow |

Elapsed time changes only the low or medium observation-window result. It does
not change risk, epistemic state, retention state, calibration, provenance, or
review authority.

## Operator interface and rollback

`POST /v1/items/{item_id}/admission-assessments/simulate` compares one eligible
item. `POST /v1/admission-assessments/simulate` returns one bounded keyset page.
`engram admission-assessments simulate` provides the same page result for an
API key's tenant, principal, and memory-profile boundary.

All simulation is read-only by default. An admin can explicitly request V2
shadow persistence. This only appends immutable history. It does not project
current state, change a review status, enqueue `promotion.evaluate`, call a
provider, or affect recall ranking.

Rollback means stop running the shadow profile. V1 Path A behavior continues.
V2 shadow history remains immutable and attributable. Automatic admission
remains uncertified.
