# ADR-160: Recall admission profiles and separated recall signals

Status: Proposed (implementation landed; candidate profiles shadow-only until
#162 certification)
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
   | `governed` | active + disputed stay kinds | yes | `semantic-signals-v1` | none | no |
   | `exploratory` | active + proposed | yes | `semantic-signals-v1` | item 20 / byte 2048 | no |

   `review`/`historical-audit` from the issue's candidate list are
   deliberately not selectable yet: they are reviewer/operator surfaces with
   their own capability requirements, not recall packets.

2. **Shadow-only rollout (the certification boundary).** Before any of this
   changes served behavior, `recall_profiles.CERTIFIED_SERVING_PROFILES`
   (currently `{"legacy"}`) is the single choke point every serving path goes
   through (`resolve_serving_profile`):

   * ordinary `POST /v1/recall` **always serves the certified packet** —
     `legacy` today. Requesting `governed`/`exploratory` is HTTP 422 (fail
     closed; a silent legacy substitute would misattribute the packet), and
     an uncertified `settings.recall_default_profile` is **refused with a
     warning**, never honored — configuration alone cannot promote a
     candidate profile into production authority;
   * #162D/#176 terminated `NOT_CERTIFIED`; no certification authorizes
     governed/exploratory serving behavior. A profile key enters
     `CERTIFIED_SERVING_PROFILES` only with an accepted #162 certification
     recorded here — a code-level change, deliberately not expressible via
     request parameters, tenant settings, or deployment flags;
   * candidate packets are computed **exclusively** by the authorized,
     read-only shadow comparison surface (`POST /v1/recall/shadow-compare`,
     `engram/recall_shadow.py`): it evaluates the legacy packet and each
     requested candidate profile with the same read-only evaluation core
     serving uses (`engram.recall.evaluate_semantic_profile`) and returns
     id-level comparisons. It writes no `recall_logs` row, bumps no
     `recall_count`/`last_recalled_at` exposure counters, enqueues nothing,
     and feeds no promotion/evidence input — so an uncertified profile can
     never affect recall telemetry as though its packet had been served.

3. **Exploratory authorization.** The shadow surface (the only place
   exploratory candidate output exists) requires **both**:

   * caller capability — `REVIEW_SCOPE` (the existing review-domain guard),
     so an ordinary `read` caller can never inspect proposals/unknown
   * evidence; and
   * tenant policy — `tenant_config.recall_profile_shadow_enabled`
     (migration 041, `BOOLEAN NOT NULL DEFAULT FALSE`). Capability is
     necessary but not sufficient: tenant denial wins even for a capable
     caller, and an absent config row fails closed.

   The MCP tool continues to expose no profile parameter at all, so neither
   exploratory nor governed can be smuggled through MCP defaults.

4. **Separated signal model** (`engram/recall_signals.py`, version
   `recall-signals-v1`, admission policy version `recall-admission-v1`).
   Items under governed/exploratory expose distinct fields:
   `relevance_score`, `utility_score`, `epistemic_state`
   (`supported|contested|insufficient_evidence|unknown`), structured
   `warning_codes` (+ free-text `warnings`), and an `admission` block
   (profile, decision, reason codes, policy version, bound assessment id /
   status / outcome). No blended `trust_score` exists on this path.
   `memory_confidence` — the historical source-policy prior, not epistemic
   confidence — produces **no** warning on this path; a generic
   `low_confidence` code would reintroduce exactly the conflation #160
   removes. Epistemic state stays honestly `unknown`/
   `insufficient_evidence` until #157 enrichment lands.

5. **Ranking contract** (governed/exploratory): admission is a gate *before*
   ranking — it reads governance state only (review status, disputed
   stay-kind doctrine, durable admission outcome), never similarity,
   importance, or exposure. Among admitted items,
   `rank = similarity * (0.5 + 0.5 * utility)` where
   `utility = 0.7 * importance + 0.3 * freshness`. Relevance dominates;
   utility orders; unknown evidence is admitted-or-withheld-and-marked, never
   converted into a numeric trust floor.

6. **Durable-assessment binding**: governed/exploratory consult the #159
   projection through the shared bulk resolver
   (`admission_assessment.resolve_bulk_admissions`, also used by the review
   queue, so the two can never disagree about what "stale" means).
   Precedence is applied centrally, before any review-status branch: an
   explicit `blocked` outcome withholds in **every** profile and **every**
   review status (including stale-and-blocked rows, and including
   exploratory); a `stale` projection withholds whenever the profile is
   strict — i.e. governed fails closed on stale state for active items,
   disputed stay-kind items, and any future governance-compatible state.
   Exploratory marks stale instead of withholding; `legacy_import`
   projections are marked, never trusted. Resolution is **not** conditioned
   on `admission_assessment_capture_enabled`: that flag governs capture of
   new assessments (#159 rollout), not reads — disabling capture (the
   documented rollback) must never hide an existing `blocked`/`stale`
   decision from recall enforcement.

7. **Eligibility before the bounded retrieval window.** The signal path
   retrieves through a dedicated neutral primitive
   (`semantic.retrieve_candidates`) that computes no trust blend, returns
   only id/distance/similarity + embedding identity, and takes the caller's
   complete mechanically-expressible corpus predicate. For governed recall
   the SQL predicate applied **before the HNSW LIMIT** is exactly
   `review_status = 'active' OR (review_status = 'disputed' AND kind IN
   governed stay kinds)`, alongside the existing tenant/visibility/
   workspace/validity/embedding-profile/RLS constraints. Ineligible disputed
   rows therefore can never occupy the bounded candidate window and starve
   eligible active memories sitting just outside it. Anything not
   expressible without examining relevance (per-item durable admission
   digests) remains the post-retrieval admission gate's job. The same
   principle binds future profile-specific corpus constraints: rows
   excludable without examining relevance must not consume the bounded
   relevance window. `semantic.search` (the trust-weighted wrapper) is
   unchanged for `/v1/search` and the legacy profile.

8. **Compatibility** (additive-first): default profile is `legacy`
   (`settings.recall_default_profile`, refused while uncertified), which
   preserves pre-#160 behavior byte-for-byte — including `trust_score`,
   relationship expansion, and `scoring_version='semantic-v3'`.
   `recall_logs.recall_profile` (migration 040, backfilled `'legacy'`)
   records the effective served profile — which can only be `legacy` or
   `startup` while certification stands; the response adds `recall_profile`,
   `signals_version`, `omitted_by_admission` (constant `None`/`{}` under
   legacy serving, kept for forward compatibility). SDK
   `RecallRequest`/`RecallResponse` gained the same additive fields (the
   request field is rejected server-side for uncertified values until
   certification); the MCP tool intentionally does not expose a profile
   parameter, so exploratory cannot be smuggled through MCP defaults.

## Feedback-loop safeguards (issue #160)

* utility excludes exposure counters (`recall_count`,
  `startup_recall_count`) — repeated serving cannot raise rank through
  utility, admission, or epistemic state;
* admission ignores similarity/importance — popularity cannot buy admission;
* semantic telemetry still increments only `recall_count`/`last_recalled_at`
  on served packets, unchanged from legacy; the shadow surface increments
  nothing.

## Known limitations / follow-ups (issue #160 remains open)

This ADR records one slice of #160. Deliberately deferred, tracked by the
issue:

* **Signal-aware graph/tunnel expansion** — expansion is legacy-only;
  admission must precede expansion and the rescorer still speaks the blended
  score. Governed/exploratory evaluate direct semantic hits only until
  expansion learns the signal model.
* **#157 bulk epistemic enrichment in the recall hot path** — per-item
  effective selection is N-queries today; epistemic state derives from
  item-level review/conflict/verification state until a bulk helper lands.
* **Evidence-root diversity / redundancy packing** — packing is purely
  rank-ordered; evidence-root grouping (via the #157 evidence manifest) is
  follow-up.
* **Conflict-pair preservation in packing** — contested items are admitted
  and marked, but packing can still drop one side of an unresolved conflict.
* **Demonstrated-usefulness feedback in utility** — utility-v1 is importance
  + freshness only; versioned, bounded feedback with actor/root provenance is
  follow-up.
* **Semantic Context Ledger receipts** — receipts remain startup-only;
  binding per-item admission evidence lands with semantic receipts.
* **Review/historical recall surfaces** — not selectable; they need their own
  capability contracts.
* **Dogfood evaluation + exposure-concentration analysis** — run on the
  shadow-comparison surface (the reason it exists).
* **Certification/default cutover** — flipping `CERTIFIED_SERVING_PROFILES`
  (and then `recall_default_profile`) is gated on accepted #162
  certification; per-tenant profile policy beyond the shadow allow is part
  of that rollout work.
* **`omitted_by_admission` is response-only** — gate-level withholding counts
  by reason code are returned to the caller and logged; `recall_logs` has no
  JSON omission column yet (note: under shadow-only rollout these counts
  surface only from the shadow comparison, never from `/v1/recall`).

## Consequences

* Ordinary semantic recall keeps meaning "legacy packet" until certification
  changes a code constant; there is no configuration path that can serve a
  candidate profile, and no request that can broaden a working set with
  proposals/unknown evidence beyond what legacy already served.
* Rollback of the #159 interaction is capture-disable, which stops new
  assessment capture but leaves persisted projections enforcing recall
  (fail-closed reads, per the #159 rollback contract).
* Dogfooding/certification evaluation happens on the shadow surface without
  any risk of changing served behavior.
* The blended `compute_semantic_trust_score` remains for `/v1/search` and the
  legacy profile; deprecating it there is a separate, consumer-gated step.
