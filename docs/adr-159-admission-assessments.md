# ADR: Durable admission assessments

Status: implemented behind a disabled capture flag.
Issue: #159. Baseline: `1ab6110d392dead10a0a5793bc59b4787f15972d`.

## Decision

Persist every promotion/admission decision current Path A policy makes as an
append-only `admission_assessments` row, with a separate mutable one-row
`admission_assessment_current` projection. No pointer column is added to
`memory_items`.

This issue persists the **decision** produced by current promotion policy. It
does not replace that policy. `assess_promotion_candidate()` and
`auto_promote_proposed_memories()` remain the sole production authority, and
no threshold, weight, cooling period, lane rule or blocker code changes.

#157 `memory_assessments` are evidence assessments, not promotion decisions.
They are recorded here as diagnostic references only.

## Policy identity

There is exactly one production policy profile in this issue:

| Field | Value |
| --- | --- |
| `policy_profile_key` | `path_a_compat` |
| `policy_contract_version` | `path-a-compat-v1` |

`path-a-compat-v1` names the current two-lane Path A behavior. It introduces
no #158 candidate-policy semantics; #158 will add its own profile rather than
redefine this one.

`policy_config_digest` covers everything decision-affecting that is needed to
reproduce the decision, and nothing volatile:

- profile key and contract version;
- legacy confidence threshold and `promotion-legacy-v1`;
- minimum age hours;
- evidence lane enabled/disabled, evidence threshold, `promotion-evidence-v1`;
- evidence score ceiling, source-prior weight, retention weight, taxonomy
  minimum;
- the accepted classification/retention receipt versions
  (`classification-v2` / `retention-v1`);
- the kind auto-promotion eligibility fact for the evaluated item.

It deliberately excludes timestamps, job IDs, evaluation IDs, request IDs and
every other invocation-scoped value, so the same policy over the same item
digests identically months apart.

`input_digest` binds the item/governance/evidence state actually evaluated:
content hash, governed kind, review status, validity/supersession, source type
and prior, memory confidence, retention fields, conflict state,
authority/sensitivity/verification, visibility, creation time, and the bound
classification receipt's identity, values and versions.

#157 assessment IDs and hashes are recorded separately in
`available_memory_assessment_refs` and move neither digest. Letting them move
a digest would make an evidence assessment retroactively look like a promotion
input, which is exactly the collapse this issue must avoid.

## Canonical decision hash

`decision_hash` is `sha256:<hex>` over the RFC 8785 (JCS) canonical bytes of
the decision envelope, produced by the same pinned `rfc8785` library the
context manifest and extraction receipts use.

Included: schema version, tenant/item identity, mode, item content hash, input
digest, policy profile/contract/config digest, selected basis, outcome,
blocker codes in canonical sorted order, reason codes in canonical sorted
order, normalized decision inputs, conflict-recheck status, and the
cooling/eligibility/next-action values that are deterministic outputs of the
evaluated state.

Excluded: assessment ID, `created_at` / `evaluated_at`, job / evaluation /
request IDs, actor ID, and all mutable projection state.

Blocker codes are sorted before hashing. The evaluator emits them in discovery
order, which depends on which lane it examined first; without sorting, the same
decision would hash two different ways.

`mode` is part of the envelope. A shadow preview, an authoritative evaluation
and a legacy import are different claims about the same state, and the hash
says which one it is.

## Outcome vocabulary

`admitted`, `would_admit`, `cooling`, `review_required`, `blocked`,
`insufficient_evidence`, `unknown`, `stale`, `not_applicable`.

Precedence when several apply:

```
stale > blocked > review_required > cooling > insufficient_evidence > unknown
```

except that a successful mutation is always `admitted` and its non-mutating
shadow equivalent is always `would_admit`.

Blocker-to-category mapping:

| Category | Blocker codes |
| --- | --- |
| `blocked` | `conflict`, `conflict_recheck` |
| `review_required` | `kind_policy`, `review_policy`, `external_dispute` |
| `cooling` | `age`, **and** a lane that would otherwise qualify |
| `insufficient_evidence` | `confidence`, `evidence_disabled`, `no_retention_evidence`, `missing_source_prior`, `retention_disposition`, `taxonomy_confidence`, `evidence_score`, `evidence_version`, `evidence_inconsistent` |
| `unknown` | anything uninterpretable, and any state no category explains |

`cooling` is never inferred from an `age` blocker alone. The assessment
persists both lanes' trust and age qualification, and `cooling` requires a lane
whose trust test passed and whose observation boundary is the single remaining
gate. A `wait_until` next action always carries a `next_evaluation_at`.

`unknown` is never coerced into `insufficient_evidence` or a zero score.

## Next-action vocabulary

`wait_until`, `classification_required`, `human_review_required`,
`conflict_resolution_required`, `new_evidence_required`,
`policy_reconciliation_required`, `none`.

| Condition | Action |
| --- | --- |
| `admitted` / `would_admit` / `not_applicable` | `none` |
| `cooling` | `wait_until` |
| `no_retention_evidence`, `evidence_version`, `evidence_inconsistent` | `classification_required` |
| `kind_policy`, `review_policy`, `external_dispute` | `human_review_required` |
| `conflict`, `conflict_recheck` | `conflict_resolution_required` |
| `evidence_score`, `retention_disposition`, `taxonomy_confidence`, `missing_source_prior`, `confidence` | `new_evidence_required` |
| `stale`, `evidence_disabled` | `policy_reconciliation_required` |

Multiple actions are returned when independently required, always in a fixed
order so the hash is stable.

## Current projection

`admission_assessment_current` is keyed by
`(tenant_id, memory_item_id, policy_profile_key)` and stores identity plus only
the operational metadata needed to resolve precedence. The pointed assessment
remains the source of truth for the decision itself.

Precedence is `(mode_rank, evaluated_at, mutation_rank, assessment_id)`:

- `mode_rank`: authoritative (1) always supersedes `legacy_import` (0);
- `evaluated_at`: within a mode, the later evaluation wins;
- `mutation_rank`: a same-instant tie resolves toward the evaluation that
  actually mutated item state, which is exactly what a lost promotion race
  produces;
- `assessment_id`: final deterministic tiebreak.

Shadow rows can never become current — refused in `project_current()` and
again by the projection table's own CHECK constraint.

Freshness is resolved by comparing the pointed assessment's `input_digest` and
`policy_config_digest` against digests recomputed from current state. Historical
rows are never mutated to mark them stale. `missing` (nothing recorded) stays
distinct from a recorded decision whose outcome is `unknown`.

## Transaction and lock order

The canonical `promotion.evaluate` path (issue #155) is unchanged in shape:

1. parse and reuse the existing job/evaluation/trigger contract;
2. preliminary work without the item lock, as current code already does;
3. `SELECT ... FOR UPDATE` on the item and reload decision-affecting state
   (`SKIP LOCKED` semantics for the untargeted sweep are unchanged);
4. revalidate policy/config under the lock. A `tenant_config` change committed
   between the pre-lock read and the lock makes the pre-lock result `stale`: it
   is recorded as immutable non-current history and the pass re-evaluates on
   the reloaded policy before any mutation;
5. recompute the candidate from locked state through the production evaluator;
6. run the promotion-time conflict recheck bound to that locked state;
7. build the canonical decision and hash;
8. for a non-mutating result: insert the assessment and update the projection
   in the same transaction;
9. for `proposed -> active`: preallocate the audit event ID, run the guarded
   mutation, insert the event, insert the assessment naming that event, set the
   event's `admission_assessment_id`, update the projection — all committed
   atomically;
10. if the guarded mutation loses the race, no `admitted` assessment or event
    is written. The pass reloads and appends a truthful non-mutating result
    (`not_applicable`, reason `mutation_race_lost`), and projection precedence
    keeps it from displacing the winner.

The assessment insert is ordered **after** the guarded mutation rather than
before it. Everything lands in one transaction either way, so atomicity is
unaffected; ordering it after is what makes a false `admitted` row
unrepresentable rather than merely unlikely.

Dry-run/preview decisions are built in memory during the pass and written only
after that pass has rolled its own transaction back, so a `shadow` row provably
cannot carry a lifecycle mutation with it. Preview rows always record
`conflict_recheck_status = not_run_preview`.

## Idempotency and concurrency

- `evaluation_id` is unique per tenant. A retry of the same canonical
  evaluation reuses the bound assessment rather than appending a second
  mutation decision. A multi-item sweep claims the evaluation identity for at
  most one item and records the rest without one.
- Queue claim semantics remain `FOR UPDATE SKIP LOCKED`; item lock order is
  unchanged.
- Two workers racing one newly eligible proposal yield exactly one
  `proposed -> active` mutation, one `admitted` assessment and one linked
  `review_change` event.

## Authorization, RLS, privacy

Tenant RLS with FORCE from the first migration. Reads follow item read
eligibility through the #157 `assessment_item_eligible` predicate; writes are
checked against tenant scope alone, because capture runs under the tenant's
admin principal, which is not the author of every private item it must record
a decision for. Cross-tenant reads and writes fail either way.

The app role has `SELECT, INSERT` on `admission_assessments` with `UPDATE` and
`DELETE` revoked, plus an immutability trigger so a future privilege drift
cannot silently make decision history rewritable. Only the projection table is
mutable.

Full normalized decision inputs and evidence references require review
authority (`GET /v1/items/{id}/admission-assessments/{assessment_id}`). The
basic current/history views and the item summary carry no provider internals,
no transcript, no extraction spans and no conflict candidate identity.

`decision_inputs` stores only safe normalized values current Path A actually
read. It never duplicates memory content, transcript text, extraction
spans/content, provider output text, credentials, unrestricted conflict
candidates, or human-evaluation labels.

## Legacy import

`engram admission-assessments backfill --tenant <id> --limit <n> --after <item-id> [--dry-run]`

Bounded, restartable, idempotent, and deliberately unable to fabricate history:

- scans only current `proposed`/`active` items in deterministic ID order;
- writes `mode=legacy_import` rows snapshotting currently observable state;
- an already-`active` item gets `not_applicable` with no `selected_basis`. It
  is not a live proposal, and this import has no evidence about which lane, if
  any, admitted it;
- a live proposal current policy would otherwise admit gets `unknown`, because
  the promotion-time conflict recheck was never run for it;
- `conflict_recheck_status` is always `unavailable_legacy`;
- no `linked_item_event_id` is ever written: historical `review_change`
  reasons do not carry enough stable policy-input identity to bind one safely,
  so linkage stays unavailable rather than guessed;
- #157 references come from real rows or are absent; none is manufactured;
- reapplication is idempotent (same decision hash for the item is skipped);
- a later authoritative evaluation supersedes the imported projection by
  ordinary precedence, without rewriting the import row.

The schema migration performs no row-per-item historical reconstruction.

## Rollback and mixed-version behavior

1. set `ENGRAM_ADMISSION_ASSESSMENT_CAPTURE_ENABLED=false`;
2. current Path A mutation and audit behavior continues, byte-identical;
3. `admission_assessments` and the projection are preserved for inspection;
4. no historical row is deleted or rewritten.

The downgrade script refuses to run while pending/running `promotion.evaluate`
jobs may still commit an assessment.

Enabling capture requires the #159-capable worker and API set to be rolled out
together: with the flag on, a promotion mutation fails closed if its
assessment, event and projection cannot commit atomically. A mixed-version
worker that does not know #159 may keep performing legacy behavior only while
the flag is disabled.

## Non-goals

No new promotion formula, threshold, weight, cooling period or admission
profile. No use of #157 epistemic/risk values as promotion authority. No #158
candidate policy. No change to semantic or startup recall eligibility or
ranking. No Context Ledger diff project. No corroboration/Path B. No automatic
deletion or archive. No new provider calls. No second copy of memory content.

A decision hash proves *what was decided from which inputs under which policy*.
It proves nothing about factual truth or external endorsement.

## Deferred

- #158: candidate policy and any profile beyond `path_a_compat`.
- #160: per-served-item Context Manifest/receipt binding. #159 provides the
  deterministic identity (`decision_hash`, assessment ID) and the resolver
  #160 binds to.
- #161: corroboration / Path B.
