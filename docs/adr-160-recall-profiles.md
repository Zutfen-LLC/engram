# ADR-160: Recall admission profiles and separated recall signals

Status: Proposed (implementation landed; candidate profiles shadow-only until
#162 certification; candidate admission is V2-bound since issue #186, served
evidence presentation since issue #188)
Date: 2026-09-06
Issue: #160 (ENG-RECALL-003), parent #153; supplements: #186
(ENG-RECALL-003B), #188 (ENG-RECALL-003C)
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
     id-level comparisons. It runs whenever *any* requested packet has an
     eligible corpus — legacy may legitimately evaluate to an empty packet
     while a candidate is non-empty (e.g. the only eligible item is a
     disputed governed stay kind, invisible to legacy's active+proposed
     window). It writes no `recall_logs` row, bumps no
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
   `recall_logs.recall_profile` (migration 040) records the effective served
   profile — which can only be `legacy` or `startup` while certification
   stands. Historical rows are backfilled truthfully from the mode they
   already record (startup rows → `startup`, matching what new startup logs
   write; semantic rows → `legacy`); the response adds `recall_profile`,
   `signals_version`, `omitted_by_admission` (constant `None`/`{}` under
   legacy serving, kept for forward compatibility). SDK
   `RecallRequest`/`RecallResponse` gained the same additive fields (the
   request field is rejected server-side for uncertified values until
   certification); the MCP tool intentionally does not expose a profile
   parameter, so exploratory cannot be smuggled through MCP defaults.

## Supplement (issue #186, ENG-RECALL-003B): V2-bound candidate admission

Issue #186 changed the canonical admission authority for the **shadow**
governed/exploratory packets. Decision 6 above (review-status + #159
projection as the admission policy) is superseded for candidate evaluation by
the exact #158 per-surface decisions; the text above is retained as the
historical record of the first slice.

1. **Canonical admission authority.** For `governed` and `exploratory`,
   admission is the exact `risk_aware_shadow_v1` per-surface decision
   (`RecallProfileSpec.v2_surface`: governed → `semantic_governed`,
   exploratory → `semantic_exploratory` — never `startup`, never
   `highest_admission_tier`). `review_status` is no longer a positive
   admission source: the #158 policy's domain is **live proposals**
   (`not_live` → blocked on every surface), so active items are not
   admissible into candidate packets no matter how similar or important.
   Both candidate corpus windows are therefore the mechanically-expressible
   live-proposal predicate (`review_status='proposed' AND valid_to IS NULL
   AND superseded_by IS NULL AND conflict_resolution_status IS DISTINCT FROM
   'unresolved'` — NULL-safe) applied before the bounded HNSW window — rows
   the V2 gate would inevitably withhold (active, conflicted, closed) can
   never starve eligible proposals.

2. **Shared bulk resolver** (`engram/admission_shadow.py`
   `resolve_bulk_v2_decisions`). One bounded resolution for the whole
   candidate window (support + bulk #157 selection + one latest-row lookup;
   query count constant in window size), owned by the #158 module so the
   simulator, the operator simulate surfaces, and #160 recall cannot drift
   on "what is the effective V2 decision?". For each item it re-evaluates
   the exact decision from current state (the same evaluation
   `simulate_item` produces — mechanical parity, pinned by tests) and
   resolves the latest persisted V2 shadow row against it:

   | Resolution | Meaning |
   |---|---|
   | `current` | the row's `decision_hash` equals the fresh evaluation — the row IS the decision |
   | `missing` | no V2 row persisted for the item |
   | `stale` | the row's input no longer matches: state, effective #157 selection, content identity, or a time-dependent outcome changed since it was recorded (input-digest mismatch, in #159 vocabulary) |
   | `mismatched` | row recorded under a different policy artifact digest |
   | `unsupported` | latest row under the profile key is not a V2 row |

   Only `current` carries positive authority; every other status withholds
   with its own reason code (`v2_decision_missing` / `_stale` /
   `_mismatched` / `_unsupported`). The resolver reads only — it never
   projects a shadow row current, mutates review state, enqueues work, or
   calls a provider.

3. **Fail-closed surface gate** (`engram/recall_signals.py`, admission
   policy version `recall-admission-v2`). An item enters a candidate packet
   only when the exact surface decision is `allow` under a `current`
   resolution. `withhold` / `review_required` / `blocked` / `unknown` are
   consumed as-is (distinct outcomes never flattened); governed and
   exploratory may differ only per their own exact surface decisions.
   Recall-local rules survive as **withhold-only** defense in depth: the
   #159 `blocked` outcome (every profile) and strict-stale binding
   (governed), plus the lifecycle facts the window already enforces. A
   local/V2 disagreement always withholds, keeps the local reason code as
   the primary reason, and is itemized in the packet's bounded,
   content-free `admission_diagnostics` — each entry carrying the reason
   codes, the V2 resolution status and exact surface decision,
   `gates_disagree`, and the same full `v2` binding block admitted items
   carry (see below), so an operator can tell exactly which V2 decision a
   local boundary overrode.

4. **Payload contract** (additive; `recall-shadow-compare-v2`). Admitted
   candidate items carry the full V2 binding block (`admission.v2`),
   and — since the #186 review correction — withheld candidates expose the
   same block through `admission_diagnostics`. The binding keeps the
   persisted row's identity and the fresh evaluation's identity separate
   and never collapses them:

   * `persisted` — what the durable row says about itself (assessment id,
     schema version, policy contract version, artifact digest, decision
     hash), read from the row's own columns; `null` when no row exists;
   * `fresh` — what re-evaluating now under the current policy produces
     (schema version, policy version + artifact digest, decision hash, the
     exact surface decision, risk/epistemic/retention state, effective
     #157 assessment refs, observation-window/eligible/next-evaluation,
     bounded code sets).

   For `current` the two agree; for `stale` both decision hashes stay
   visible, for `mismatched` both artifact digests, for `unsupported` the
   row's non-V2 schema next to the fresh V2 one, for `missing` there is no
   persisted identity while `fresh` still describes the current
   evaluation. Each candidate packet also carries a `v2_resolution`
   summary (policy identity, per-status counts, the number of queries the
   resolver actually executed — constant in window size, never a ceiling).
   The pre-existing `admission.assessment_*` fields keep their #159
   Path-A meaning.

5. **Boundary unchanged.** This remains a shadow-only integration:
   `CERTIFIED_SERVING_PROFILES` is still `{"legacy"}`, `POST /v1/recall`
   still serves only the certified legacy packet, shadow comparison still
   writes nothing (no recall logs, no exposure counters, no promotion or
   evidence inputs), and an accepted #162 certification is still required
   before any serving-default change. Nothing here authorizes #161/#162
   production enablement.

Operational consequence: a candidate packet contains only items whose V2
rows were persisted (via the #158 simulate + persist surfaces) and are still
current — an unqualified or never-simulated corpus evaluates to explicit
`v2_decision_missing` withholdings, which is the certification-honest
behavior #186 requires.

## Supplement (issue #188, ENG-RECALL-003C): canonical served evidence state

PR #187 fixed the admission *authority*; issue #188 fixes the served item
*presentation* so an admitted candidate packet can never describe a different
evidence state than the one that admitted it. It is a shadow-only
presentation/contract correction: no ranking, packing, production serving,
promotion, review state, or certification authority changes.

1. **Canonical evidence source.** For the V2-bound profiles (`governed`,
   `exploratory`), the served evidence state of an admitted item is the
   already-resolved `admission.v2.fresh` evaluation — the exact state the
   admission policy consumed. No second #157 selection pass, no per-item
   `memory_assessments` query, no second V2 policy evaluation, and no
   positive inference from `review_status`, `memory_confidence`,
   `source_trust`, importance, age, recall counts, or human verification on
   these profiles. The item-local review/conflict/verification heuristic
   (`derive_epistemic_state`) remains only for non-V2 local profiles (none
   are registered today) and for its pre-existing unit contracts.

2. **`evidence` payload block (additive).** Every admitted V2-bound item
   carries a structured block that is a pure projection of the binding —
   `source: "v2_fresh_evaluation"`, `profile_key`, `policy_version`,
   `policy_artifact_digest`, `decision_hash`, `v2_resolution_status`,
   `epistemic_state`, `risk_state`, `retention_state`,
   `effective_assessment_refs` — and the pre-existing top-level
   `epistemic_state` field now mirrors `evidence.epistemic_state` exactly.
   The identity invariant (`evidence.* == admission.v2.fresh.*`) is
   structural: the block is built by one pure function
   (`recall_signals.build_v2_evidence_fields`) from the `RecallAdmissionDecision`
   the gate already returned. An impossible admitted combination
   (non-`current` resolution, non-`allow` surface decision, or an
   unpresentable epistemic state such as `not_applicable`) raises
   `V2EvidenceContractError` instead of being reinterpreted. The block is
   deliberately **receipt-ready**: the #160 Context Ledger receipt slice can
   copy/bind it as-is; no receipt storage or contract lands here. Raw #157
   `selection_status` is intentionally not surfaced in this slice (it would
   revise the #158 decision/hash contract and stale persisted V2 rows);
   unavailability remains visible through the bound blocker/reason/
   next-action codes.

3. **Warning contract.** Warnings on admitted V2-bound items derive from the
   canonical V2 evidence state plus independently true lifecycle/governance
   marks (`unreviewed`, dispute/conflict, #159 stale/legacy-import) and can
   never contradict `evidence.*`: `unknown` → `evidence_unknown`,
   `contested` → `evidence_contested`, `insufficient_evidence` →
   `evidence_insufficient`, `supported` → no evidence-quality code;
   `risk_state` `high`/`unknown` → `risk_high`/`risk_unknown`, so an
   exploratory item the exact surface allowed still carries its risk
   unmistakably. Non-`current` V2 resolutions never produce served evidence
   fields — they remain fail-closed withholds represented through the
   withheld `admission_diagnostics.v2` blocks.

4. **Boundaries unchanged.** Ranking (`compute_signal_rank_score`, utility
   weights), relevance retrieval, ordering, budgets, live-proposal corpus
   eligibility, #159 local-withhold precedence, and graph/tunnel expansion
   are untouched; the evidence/risk fields do not feed ranking.
   `CERTIFIED_SERVING_PROFILES` remains `{"legacy"}`, ordinary recall stays
   legacy-only, the shadow comparison stays reviewer + tenant-policy gated
   and writes nothing, MCP exposes no profile selection, and the legacy
   packet shape is byte-for-byte unchanged (no `evidence` /
   `warning_codes` keys on legacy items).

## Supplement (issue #190, ENG-RECALL-003D): admission-first relationship expansion

Issue #190 extends the governed/exploratory shadow profiles through the
existing bounded graph/tunnel relationship expansion, without reintroducing
the legacy blended trust/ranking model. Relationship expansion becomes an
**admission-first relevance mechanism**: a relationship can make a memory
*relevant*, it can never make that memory trusted, epistemically supported,
review-approved, or admissible. This remains shadow-only — it authorizes no
governed/exploratory production serving and no #162 certification/cutover.

1. **Admission precedes expansion.** The candidate-profile pipeline order is
   normative: direct semantic candidates → exact V2 admission on the direct
   candidates → seeds chosen **only** from admitted direct candidates →
   bounded graph/tunnel neighbor discovery → exact V2 admission on **every**
   expanded neighbor → relationship-aware relevance → separated utility
   ranking → budget packing. A withheld direct hit can never seed expansion
   (zero admitted seeds means no expansion run at all), and seed admission
   never transfers to a neighbor: each neighbor is admitted through the same
   `decide_recall_admission` gate over its own resolved `risk_aware_shadow_v1`
   decision, so only `current + exact-surface allow` enters the packet and
   `missing | stale | mismatched | unsupported` and
   `review_required | withhold | not_applicable` stay fail-closed.

2. **Discovery under the same hard boundaries.** Neighbor discovery reuses
   the shared bounded mechanics (depth-1 only, per-seed and total graph caps,
   total tunnel cap, deterministic ordering/tie-breaking) and adds the same
   live-proposal corpus window the profile's own direct retrieval applies
   (`recall_signals.live_proposal_expression`) as the discovery prefilter —
   the one prefilter the #190 contract permits precisely because it can
   neither admit, widen, nor hide policy-relevant candidate state: it is the
   identical window, and active/closed/conflicted rows are inevitably
   withheld by the V2 gate anyway. Tenant/read-eligibility/workspace
   boundaries are enforced inside discovery itself; the candidate tunnel
   fetch orders `created_at desc, id asc` (importance-free) because utility
   signals may order only already-admitted items, never decide which
   neighbors a bounded window discovers.

3. **Relationship relevance is versioned and utility-free.** One pure
   contract (`relationship_recall.compute_relationship_relevance`, version
   `relationship-relevance-v1`) computes relevance from exactly four inputs:
   the item's direct semantic score (when it was a direct hit), the
   source-seed relevance that justified expansion, the strongest graph edge
   (bounded to `[0, 1]`, per-edge weights clamped), and tunnel membership.
   Importance, source trust, memory confidence, human verification, review
   state, exposure counters, and epistemic/risk state are not inputs — they
   cannot move relevance by construction. The combination is
   `clamp01(max(direct_score, w_semantic·semantic + w_graph·edge +
   w_tunnel·tunnel))` over the existing relationship weights minus the
   importance term: an unlinked direct item's relevance is exactly its
   similarity (pre-#190 values), links never demote a direct hit, and a
   relationship can only derive relevance from the bounded source-seed/
   relationship contract. Admitted items reached through expansion carry a
   structured `relationship` block (origin decomposition, direct/seed
   scores, edge types, tunnel labels, per-component contributions,
   `relevance_score`); the final rank feeds that value into the unchanged
   `compute_signal_rank_score` with utility — never a new blended scalar.

4. **Evidence identity is untouched by relationships.** An admitted expanded
   item presents the same `admission.v2` / `evidence` identity as any direct
   item (the #188 invariant): `admission.v2` is the binding that admitted
   *that neighbor*, `evidence.*` is its pure projection, top-level
   `epistemic_state` mirrors it, and relationship metadata never rewrites
   evidence. A `supports` edge cannot produce `supported`; a `contradicts`
   edge is relevance, never conflict resolution — full conflict-preserving/
   diversity packing remains follow-up work.

5. **Diagnostics and boundedness.** Withheld expanded neighbors remain
   auditable and content-safe: `admission_diagnostics` entries carry the
   expansion `origin` (`direct`, `graph`, `tunnel`, `graph+tunnel`) alongside
   the V2 resolution status and exact surface decision, and each packet
   carries a bounded `expansion` summary (contract version, seed/neighbor/
   admitted/withheld counts). Expansion performs no provider call and one
   bounded bulk V2 resolution for the newly discovered neighbor set per
   packet (query count constant in neighbor count, on top of the
   already-bounded discovery queries); the packet-level `v2_resolution`
   summary totals both windows under the same policy identity.

6. **Legacy stays compatibility-only.** `expand_recall_candidates` (the
   legacy blend, importance included, `semantic-v3`) remains byte-for-byte
   unchanged and remains the only expansion `POST /v1/recall` runs.
   `CERTIFIED_SERVING_PROFILES` stays `{"legacy"}`, the shadow surface stays
   read-only and reviewer + tenant-policy gated, no MCP profile selection is
   enabled, and #161 corroboration, semantic Context Ledger receipts,
   dogfood ranking tuning, and #162 certification/cutover all remain
   follow-up.

## Feedback-loop safeguards (issue #160)

* utility excludes exposure counters (`recall_count`,
  `startup_recall_count`) — repeated serving cannot raise rank through
  utility, admission, or epistemic state;
* admission ignores similarity/importance — popularity cannot buy admission;
* semantic telemetry still increments only `recall_count`/`last_recalled_at`
  on served packets, unchanged from legacy; the shadow surface increments
  nothing.

## Known limitations / follow-ups (issue #160 remains open)

This ADR records the #160 slice plus the #186 V2-binding, #188
evidence-presentation, and #190 admission-first relationship-expansion
supplements.
Deliberately deferred, tracked by the issue:

* **Conflict-preserving/diversity packing over expanded candidates** — since
  #190 the candidate profiles expand through graph/tunnel relationships
  (admission-first, `relationship-relevance-v1`), but packing is still
  purely rank-ordered: evidence-root grouping and conflict-pair preservation
  over direct + expanded candidates are follow-up.
* **#157 enrichment on non-V2 paths** — the V2 resolver supplies
  risk/epistemic/retention state for candidate admission in bulk
  (`effective_assessment_selection_bulk`), and since #188 the served items of
  the V2-bound candidate profiles present exactly that state. Startup and any
  future non-V2 profile still use their own item-level derivation; enriching
  *those* served fields from #157 is follow-up.
* **Evidence-root diversity / redundancy packing** — packing is purely
  rank-ordered; evidence-root grouping (via the #157 evidence manifest) is
  follow-up.
* **Conflict-pair preservation in packing** — contested items are admitted
  and marked, but packing can still drop one side of an unresolved conflict.
* **Demonstrated-usefulness feedback in utility** — utility-v1 is importance
  + freshness only; versioned, bounded feedback with actor/root provenance is
  follow-up.
* **Semantic Context Ledger receipts** — receipts remain startup-only;
  since #188 the candidate packet's `evidence` block is receipt-ready (the
  receipt slice can copy/bind it without reconstructing it), and binding
  per-item admission evidence into receipts lands with semantic receipts.
* **Review/historical recall surfaces** — not selectable; they need their own
  capability contracts.
* **Dogfood evaluation + exposure-concentration analysis** — run on the
  shadow-comparison surface (the reason it exists). Since #186 this includes
  keeping the V2 row corpus fresh: candidate packets bind only to *current*
  persisted decisions, so dogfooding pairs the #158 simulate+persist pass
  with the #160 comparison. Since #190 the compared packets include
  relationship expansion, so dogfood measures its contribution too.
* **Certification/default cutover** — flipping `CERTIFIED_SERVING_PROFILES`
  (and then `recall_default_profile`) is gated on a *fresh* accepted #162
  certification of the now-integrated policy (V2-bound admission +
  admission-first expansion); #162D/#176 terminated NOT_CERTIFIED against the
  pre-#186 integration and nothing here authorizes #161/#162 production
  enablement.
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
