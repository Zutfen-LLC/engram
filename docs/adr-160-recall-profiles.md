# ADR-160: Recall admission profiles and separated recall signals

Status: Proposed (implementation landed; rollout gated on evaluation)
Date: 2026-09-06
Issue: #160 (ENG-RECALL-003), parent #153
Depends on: #157 (versioned memory assessments), #158 (risk-aware admission
policy), #159 (durable admission assessments)

## Context

Until now, semantic recall labeled a blend of semantic relevance,
importance/popularity, source priors, and review status as a single
`trust_score` (`engram.semantic.compute_semantic_trust_score`), ranked by
`similarity * trust_score`, and silently treated "semantic recall" as "active
plus proposed items". Consequences (issue #160 baseline):

* positive feedback raises `importance`, so usefulness raised a number
  presented as trust;
* proposed items entered ordinary packets through a 0.85 multiplier rather
  than an admission decision;
* startup and semantic recall used two different, implicit admission
  boundaries;
* nothing bound a served item to the assessment that authorized it.

The #157–#159 substrate (versioned assessments, risk-aware admission policy
artifacts, durable `admission_assessments` + digest-verified
`admission_assessment_current` projections) exists but was never consulted by
recall.

## Decision

1. **Recall profiles** (`engram/recall_profiles.py`, contract
   `recall-profiles-v1`). `POST /v1/recall` gains `recall_profile`
   (semantic mode only; `startup` mode is its own profile). Three semantic
   profiles ship:

   | Profile | Corpus window | Admission gate | Ranking | Budget caps | Expansion |
   |---|---|---|---|---|---|
   | `legacy` (default) | active + proposed | none | `semantic-v3` blend (unchanged) | none | yes |
   | `governed` | active + disputed | yes | `semantic-signals-v1` | none | no |
   | `exploratory` | active + proposed | yes | `semantic-signals-v1` | item 20 / byte 2048 | no |

   `review`/`historical-audit` from the issue's candidate list are
   deliberately not selectable yet: they are reviewer/operator surfaces with
   their own capability requirements, not recall packets.

2. **Separated signal model** (`engram/recall_signals.py`, version
   `recall-signals-v1`, admission policy version `recall-admission-v1`).
   Served items under governed/exploratory expose distinct fields:
   `relevance_score`, `utility_score`, `epistemic_state`
   (`supported|contested|insufficient_evidence|unknown`), structured
   `warning_codes` (+ free-text `warnings`), and an `admission` block
   (profile, decision, reason codes, policy version, bound assessment id /
   status / outcome). No blended `trust_score` exists on this path.

3. **Ranking contract** (governed/exploratory): admission is a gate *before*
   ranking — it reads governance state only (review status, disputed
   stay-kind doctrine, durable admission outcome), never similarity,
   importance, or exposure. Among admitted items,
   `rank = similarity * (0.5 + 0.5 * utility)` where
   `utility = 0.7 * importance + 0.3 * freshness`. Relevance dominates;
   utility orders; unknown evidence is admitted-or-withheld-and-marked, never
   converted into a numeric trust floor.

4. **Durable-assessment binding**: governed/exploratory consult the #159
   projection through a new shared bulk resolver
   (`admission_assessment.resolve_bulk_admissions`, also now used by the
   review queue, so the two can never disagree about what "stale" means).
   Governed withholds on stale or blocked; exploratory marks stale but still
   honors blocked. While `admission_assessment_capture_enabled` is false
   (default) the loader short-circuits and the rule-based gate applies.

5. **Compatibility** (additive-first): default profile is `legacy`
   (`settings.recall_default_profile`), which preserves pre-#160 behavior
   byte-for-byte — including `trust_score`, relationship expansion, and
   `scoring_version='semantic-v3'`. `recall_logs.recall_profile` (migration
   040, backfilled `'legacy'`) records the effective profile; the response
   adds `recall_profile`, `signals_version`, `omitted_by_admission`. SDK
   `RecallRequest`/`RecallResponse` gained the same additive fields; the MCP
   tool intentionally does not expose a profile parameter, so exploratory
   cannot be smuggled through MCP defaults.

## Feedback-loop safeguards (issue #160)

* utility excludes exposure counters (`recall_count`,
  `startup_recall_count`) — repeated serving cannot raise rank through
  utility, admission, or epistemic state;
* admission ignores similarity/importance — popularity cannot buy admission;
* semantic telemetry still increments only `recall_count`/`last_recalled_at`
  on signal profiles, unchanged from legacy.

## Known limitations / follow-ups

* **Relationship expansion is legacy-only.** Admission must precede
  graph/tunnel expansion (issue #160 security section) and the expansion
  rescorer still speaks the blended score; governed/exploratory serve direct
  semantic hits only until expansion learns the signal model.
* **#157 epistemic enrichment not yet in the recall hot path.** Per-item
  `effective_assessment_selection` is N-queries; a bulk selection helper is
  follow-up. Epistemic state currently derives from item-level review/
  conflict/verification state.
* **Diversity/redundancy controls not yet implemented.** The issue requires
  that "repeated paraphrases from one evidence root do not crowd out
  independent useful context"; packing is still purely rank-ordered by
  budget. Needs evidence-root grouping (via the #157 evidence manifest) and
  is follow-up work.
* **Conflict/tension preservation in packing not yet implemented.** Contested
  items are admitted and marked (`epistemic_state='contested'`,
  `conflict_unresolved`), but budget packing can still drop one side of an
  unresolved conflict; the issue requires packing that preserves conflict
  representation. Follow-up.
* **Utility has no demonstrated-usefulness term yet.** The issue lists
  feedback with actor/root provenance as a utility input; utility-v1 is
  importance + freshness only (adding feedback needs the versioned, bounded
  policy the issue demands, plus self-feedback/author-feedback
  distinguishability). Follow-up.
* **Startup recall is unchanged** (its pipeline already had an explicit
  eligibility boundary); it records `recall_profile='startup'`.
* **Context-manifest binding:** receipts remain startup-only; binding
  per-item admission evidence into receipts lands with semantic receipts.
* **`omitted_by_admission` is response-only:** gate-level withholding counts
  by reason code are returned to the caller and emitted as a structured log
  line (`semantic_recall_admission`), but `recall_logs` has no JSON omission
  column yet; persisting them there is follow-up.
* **Shadow comparison on dogfood data, exposure-concentration analysis, and
  per-tenant profile policy** (tenant_config allow-lists/defaults) are
  rollout work, tracked by the issue's evaluation checklist.

## Consequences

* Ordinary semantic recall no longer *has* to mean "active plus proposed":
  callers opt into `governed` for that guarantee today; flipping
  `recall_default_profile` after evaluation makes it the default without code
  changes.
* Rollback is selecting `legacy` (per-request, per-tenant default, or
  deployment setting); recall logs keep the evidence of what each profile
  served.
* The blended `compute_semantic_trust_score` remains for `/v1/search` and the
  legacy profile; deprecating it there is a separate, consumer-gated step.
