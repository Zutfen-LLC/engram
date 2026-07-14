# Promotion Path A v2

This focused contract supplements the canonical trust model in `docs/design.md`. Path A v2 changes
only automated `proposed -> active` admission. It does not change recall ranking, source defaults,
authority, human verification, review authorization, or Path B.

## Independent lanes

The legacy lane qualifies when `memory_confidence` reaches the tenant's legacy threshold and the item
has aged from `created_at` for the configured minimum age.

The retention-evidence lane qualifies only when enabled and when a currently bound, consistent
`classification-v2` / `retention-v1` receipt attests `retain`, taxonomy confidence is at least `0.70`,
and the fixed score reaches the tenant's evidence threshold:

```text
evidence_score = min(0.85,
                     0.20 * source_confidence_prior
                   + 0.80 * retention_confidence)
```

Its cooling start is `max(item.created_at, item.retention_evidence_at, run.created_at)`. The legacy
clock remains independent, so new evidence cannot postpone legacy eligibility. If both lanes pass,
retention evidence wins and is the only selected basis.

## Common admission and mutation authority

Both lanes require global promotion enabled, a live unsuperseded proposal, an enabled governed kind
with `auto_promote_from_inferred=true`, no unresolved conflict, no external dispute or current external
noise verdict, trusted promotion review-policy admission, and a clear semantic/heuristic conflict
recheck. The final update repeats status, liveness, supersession, tenant, and kind-policy guards.

Built-in `fact`, `decision`, `procedure`, `summary`, and `observation` kinds are allowed by default.
`preference`, `doctrine`, `invariant`, `diary_entry`, missing/disabled kinds, and custom kinds are
blocked by default. Admins may change the governed kind flag.

Migration 016 leaves existing tenants' evidence lane disabled. Active configs inserted after the
migration default it to enabled, and the tenant-creation path writes that explicit enabled config.
The upgraded kind-seeding trigger gives future tenants the same built-in kind policy as a fresh
bootstrap.

## Execution, jobs, and previews

Startup recall, `engram promote-proposed`, `POST /v1/admin/promote`, and targeted workers use the same
assessment. Candidate kinds, receipts, external disputes, and current external noise feedback are
bulk-loaded; only otherwise-qualified candidates pay for a conflict check. The review queue uses the
same pure evaluator but never performs the conflict check and explicitly reports `not_run`.

A qualifying receipt-bound create, dedup binding, or worker classification transaction enqueues one
deduplicated `promotion.path_a` job at the evidence eligibility time. Classification refinement
reloads the item after guarded field updates and schedules from the persisted final kind, visibility,
and current receipt. Evidence binding, audit events, and job insertion are atomic.

Dry-run applies identical lane, review-policy, and conflict admission. It does not resolve internal
actors or write items, events, markers, receipts, feedback, configuration, kinds, or jobs, and every
return path ends with rollback. CLI output summarizes lane totals and stable blocker codes with
bounded candidate detail. The admin response exposes all counters and typed UUID/datetime candidates.

## Audit contract

Promotion events include operation and invocation source, selected basis and policy version,
basis-correct cooling/eligibility times, kind and source type, and relevant score/threshold inputs.
Conflict-block events are written only after the guarded marker update succeeds and include the
counterpart ID, detector verdict/reason, embedding versus heuristic mode, source item, basis, version,
and available lane diagnostics. Stable JSON formatting is used.

Promotion mutates only `review_status`. Source trust/prior, memory and retention confidence,
authority, verification fields, receipt identity, and retention evidence remain unchanged.

## Deferred to PR 3

Calibration, dogfood tenant enablement, golden-set evaluation, and live cooling-period proof are not
part of this change. Path B quorum, receipt reassessment, scheduler daemons, auto-rejection of noise,
and tenant-configurable formula weights also remain out of scope.
