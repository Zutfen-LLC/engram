# ENG-CALIBRATION-001G (#206) — frozen model-consensus review protocol

Protocol version: `eng-calibration-consensus-206-v1`
Campaign: `eng-calibration-001f` (#202 Round-2 freeze — unchanged source of truth)
Frozen: BEFORE any of Claude Opus / GPT Astra / GLM 5.3 Max receives the 402-case packet.
Implementation: `evals/calibration/consensus.py` (+ `lane_binding.py`, `model_lanes.py`,
`ingestion.py`, `human_queue.py`, `ledger.py`).

This is a review-methodology correction only. It does not change the frozen
dogfood population (624 eligible / 402 sampled / 241 dev / 161 holdout), the
sampling manifest (`ed2e0c80…`), the split manifest (`a2a27ed4…`), the target
identity (`57fc0391…`), the evidence floors, the calibration methodology, or
serving behavior. No mechanically demonstrated contract dependency requires a
re-freeze: the protocol consumes the already-frozen blind packet evidence and
adds only campaign-specific provenance vocabulary.

## 1. Reviewer panel (frozen)

| Slot | Family | Intended reviewer |
| --- | --- | --- |
| `model_a` | `claude-opus` | Claude Opus |
| `model_b` | `gpt-astra` | GPT Astra |
| `model_c` | `glm-5-3-max` | GLM 5.3 Max |

At execution time the exact provider/model identifier/version actually used is
recorded per lane (`ReviewerIdentity.provider_model_identifier` plus
config/prompt digests). If any named reviewer is unavailable or materially
changed, STOP and update/freeze the protocol before substitution; never
silently replace a reviewer after seeing results.

Each reviewer runs independently: separate context/session, no access to
another reviewer's output, provider raw scores/suggestions, current
admission/policy decisions, downstream calibration results, or human
audit/adjudication results. Lane isolation is enforced structurally: each
lane's records live under `lanes/<slot>/` in protected storage, lane loading
validates distinct slots and distinct reviewer identity digests, and the
harness never cross-mounts lane evidence.

### 1a. Exact record-to-lane identity binding (FIX-1)

One canonical validator (`evals/calibration/lane_binding.py`,
`validate_record_lane_binding`) is used at BOTH acceptance time (before a
record is persisted — `append_review_record` with lane-authority arguments,
and every `LaneSession.ingest_*` path) and freeze/load time
(`freeze_lane`/`load_frozen_lanes` via `validate_lane_provenance`). Every
accepted `ModelReviewRecord` must match the lane authority by exact equality
on ALL of:

```text
protocol_version, campaign_id, sampling_manifest_digest,
source_packet_digest, reviewer_slot, reviewer_family,
provider_model_identifier, reviewer_config_digest, prompt_digest,
label_guide_version
```

plus: the sample ID belongs to the frozen sampling manifest; each
`(slot, sample_id)` appears at most once; the digests in a `LaneFreeze`
equal the actual current record objects (recomputed at load — a record
mutated after freeze fails `lane_record_digest_mismatch`); and the lane
itself cannot be substituted across campaigns or source packets. A lane can
never attest "these 402 records were Claude Opus with config X" when the
records identify anything else.

## 2. Model-review input (frozen)

`model-packet` projects the ALREADY-FROZEN blind packet (Reviewer A packet,
sha256 `07f9fdbf…`) into a neutral model packet:

- exactly the same 402 cases in the same frozen order;
- the same decision-time case fields (content, governed kind, source type,
  review status, assertion mode, origin, risk, evidence state, age/size
  buckets);
- no reviewer hints, no provider scores, no model suggestions, no policy
  outputs, no prior labels;
- digest-bound to the sampling manifest (`ed2e0c80…`) and the source packet
  (payload sha256 recorded in the packet);
- protected outside public Git; only digests appear in public artifacts.

## 3. Model-review record schema (frozen)

Schema `engram-calibration-model-review-206-v1`, one record per (slot, case):
campaign ID, sampling manifest digest, source/neutral packet digest, sample
ID, reviewer slot, reviewer family, exact provider/model identifier, reviewer
config digest, prompt digest, labeling-guide version (`engram-calibration-guide-157-v1`),
captured-at timestamp, parse status, outcome status, reviewer confidence,
parsed judgment (five critical fields plus optional diagnostic fields), raw
response digest, error code where applicable.

### 3a. Orthogonal failure semantics (FIX-6)

Refusal, malformed output, and provider error are NEVER conflated:

```text
provider_error -> no model response / provider execution failed
                 (parse_status="absent"; raw response digest must be null)
refused        -> response received, explicit refusal / no judgment
malformed      -> response received but judgment-schema parsing failed
judged         -> valid parsed judgment
```

`parse_status` (`parsed`/`malformed`/`absent`) says whether parseable
response bytes exist; `outcome_status`
(`judged`/`refused`/`malformed`/`provider_error`) is the truthful terminal
state. Where a response exists, its bytes are preserved in protected storage
(`lanes/<slot>/raw/<sample>.resp`) and the record carries their sha256.

This schema is distinct from `LabelRecord`; the frozen #202 ledger validators
reject these rows outright, and the #206 provenance vocabulary is disjoint
from `label_origin` values. Model judgments can never masquerade as
`human_adjudicated`. Raw model outputs remain in protected evidence; public
Git receives only aggregate counts and digests.

## 4. Consensus-critical vs diagnostic fields (frozen)

Critical (exact agreement across all three reviewers required):

- `expected_kind`
- `retention_value`
- `epistemic_state`
- `consequence`
- `acceptable_abstention`

Semantic definitions are the existing frozen ones from
`engram-calibration-guide-157-v1` — unchanged because the reviewer is a model.

Diagnostic-only (may be returned; disagreement NEVER creates a human case):
every other `Dimensions` field (atomicity, spans, expected storage
disposition, blockers, next action, etc. — see `DIAGNOSTIC_FIELDS`).

## 5. Escalation rules (frozen)

A case fails consensus (enters the mandatory human queue) if ANY reviewer:

- disagrees on any critical field;
- returns `expected_kind=unknown`, `retention_value=uncertain`,
  `epistemic_state∈{unknown, ambiguous}`, `consequence=unknown`, or
  `acceptable_abstention=unknown`;
- produces malformed/unparseable output;
- refuses or errors (provider failures recorded separately from substantive
  `unknown` judgments);
- omits a required field (schema-rejected);
- marks its own confidence below `medium` (the frozen usable-confidence
  floor: ALL THREE reviewers must be at least `medium`).

Additionally every case where ANY model assigns `consequence=high` enters the
human queue regardless of agreement. NO MAJORITY VOTING: 2-of-3 never
qualifies; any 2–1 split is a human case; disagreements are never resolved by
confidence weighting, reputation weighting, or a fourth model.

## 6. Deterministic human audit of unanimous consensus (frozen)

- 15% of otherwise consensus-accepted cases;
- exact frozen size: `ceil(0.15 * consensus_eligible_count)`, bounded by the
  consensus population;
- selection deterministic by frozen sample ID and seed
  `202-model-consensus-audit-v1` (HMAC-SHA256 rank, ascending);
- label-blind: selection uses only consensus eligibility plus pre-existing
  frozen frame metadata — never judgment values;
- consensus cases with any `high` consequence signal are already in the
  mandatory human queue (they are not ordinary audit-only cases — audit-only
  cases come from the non-high consensus pool).

### 6a. Marginal-coverage selection algorithm (FIX-2)

Algorithm `marginal-coverage-hmac-rank-v1`
(`select_audit_sample_with_coverage`), deterministic and mechanically
reproducible — no optimization solver:

1. Required marginal cells are derived from the consensus population per
   frozen axis — `source_type`, `kind`, `review_status`, `age_bucket` —
   using only pre-existing frozen frame metadata.
2. The frozen HMAC seed/rank is the ONLY ranking and tie-break primitive.
3. Phase A walks cases in ascending HMAC rank and selects each case covering
   at least one not-yet-covered marginal cell, until every cell is covered
   or the target count is reached.
4. Phase B fills remaining audit slots by global HMAC rank.
5. The selection never exceeds the frozen target count.

If the target is smaller than the number of coverable cells, Phase A's
rank-greedy order IS the deterministic prioritization (the covered cells are
exactly those owned by the globally highest-ranked cases) and the uncovered
cells are reported in the `AuditSelection.uncovered_cells` evidence —
coverage is never silently claimed. Reporting populates the privacy-safe
`aggregate_by_axis` (consensus and audit-selected counts per cell; no sample
IDs, no tenant content) in the correlation report.

### 6b. Frozen audit escalation threshold (FIX-3)

Material audit disagreement = the human FINAL resolution differs from
unanimous model consensus on ANY of the five critical fields. Escalate to
full human review of ALL remaining consensus-accepted cases if EITHER:

- material audit disagreement rate strictly greater than 5% of audited
  consensus cases (1/20 does not escalate; 1/19 does); or
- any audited case reveals a consensus error the human adjudicates as
  `consequence=high`; or
- any material calibration reversal (below).

This threshold is frozen before execution and never lowered or reinterpreted
after seeing results.

### 6c. Material calibration reversal — precise rule (FIX-3)

A material disagreement is a **material calibration reversal** when it flips
the frozen calibration-outcome polarity of at least one dimension
(`REVERSAL_POLARITY` in `consensus.py`):

- `retention`: `retain` (positive) vs `do_not_retain` (negative);
  `uncertain` is unknown-polarity;
- `epistemic`: supported (`adequately_supported`/`weakly_supported`,
  positive) vs not-supported (`contradicted`/`contested`/`unverifiable`,
  negative); `ambiguous`/`unknown` are unknown-polarity;
- `taxonomy`: depends on the provider's later `suggested_kind`
  (positive ⇔ final `expected_kind` == suggestion). When the suggestion is
  not yet available and the human/consensus `expected_kind` values differ,
  the rule FAILS CLOSED (treated as a reversal); callers holding the later
  assessment evidence pass `suggested_kind` for exact evaluation
  (`evaluate_audit_outcome(..., suggested_kind_by_sample=…)`).

Unknown-polarity values never by themselves constitute a reversal.
`derive_final_human_population` mechanically expands the required human
population to every previously unaudited consensus case when escalation
fires; `verify_consensus_ledger` refuses to construct a final ledger while
any required human case is unresolved, and under escalation no automatically
accepted `cross_model_consensus` row may survive.

## 7. Final reference-label provenance — verified ledger authority (FIX-4)

The final calibration ledger distinguishes:

- `cross_model_consensus` — unanimous three-model consensus accepted under
  this protocol, not human-overridden, not audit-selected (audit-selected
  consensus rows are `human_audited_consensus` by construction);
- `human_adjudicated` — resolved by a human (disagreement/uncertainty/high
  consequence/audit escalation);
- `human_audited_consensus` — audit-selected consensus the human confirmed
  (this origin survives even under full escalation: the row WAS audited).

**The verified consensus ledger is the sole normal authority into floors and
fitting.** `verify_consensus_ledger` (`evals/calibration/ledger.py`)
re-derives every row from protected evidence — the three frozen lanes (each
record FIX-1-bound to its lane identity), the initial classifications, the
deterministic audit selection, the completed human queue evidence, and the
frozen audit outcome — and fails closed on: fabricated unanimous labels that
differ from the actual three-model records; consensus provenance on a 2–1
case; consensus provenance on an audit-selected case; human provenance
without initial/reveal/final evidence; final dimensions differing from the
stored final resolution; missing, duplicate, or extra samples; unresolved
required human cases; and escalation with an automatic-consensus row left
behind. `consensus_reference_observations` / `consensus_reference_completion`
consume this verified ledger; there is no normal calibration path accepting
hand-constructed `ReferenceLabel` lists.

Per row, protected provenance reconstructs: the three first-pass model record
digests, whether consensus was reached, human-queue entry and reasons, the
human initial judgment digest (captured before model votes were revealed),
final adjudicated dimensions, audit selection status, source packet/sampling
identity, and exact reviewer model identities. The ledger additionally binds
campaign ID, protocol version, sampling manifest, source packet, all three
lane digests, the human-queue evidence digest, the frozen audit outcome, and
the exact final sample membership (exactly all 402 samples once each).
Numeric #202 floors are unchanged.

## 8. Human adjudication workflow (scope-frozen)

`human-queue` builds the queue from three frozen lanes (escalated cases plus
audit-selected consensus; never the full 402 unless full audit escalation
fires). The workflow (`evals/calibration/human_queue.py`) shows the original
blind case evidence, captures the human's independent initial judgment
BEFORE exposing model votes (validated against the same frozen vocabulary),
preserves initial and final results separately, is append-only/resumable per
sample, and exports mechanically ingestible protected evidence. No general
Engram product UI.

### 8a. Vote-reveal binding (FIX-5)

`reveal_model_votes` persists a `VoteRevealEvent` binding: the sample ID;
campaign/protocol/sampling/source-packet identity; the EXACT three
first-pass model record digests in frozen slot order; the three frozen lane
digests; and the reveal timestamp. Reveal before the initial judgment fails;
`record_final_resolution` verifies the reveal event matches the CURRENT
frozen lane evidence (a model record mutated after the reveal fails
resolution) and never mutates the immutable initial judgment.

### 8b. Queue export (FIX-5)

`export_queue_evidence` emits ONE coherent protected case state per queued
sample with distinct `initial_judgment`, `reveal_event`, and
`final_resolution` fields — never duplicating initial/revealed files as
separate human cases. Counts distinguish queue size, initial judgments
complete, votes revealed, final resolutions complete, and unresolved cases.
Final-ledger construction consumes the authoritative FINAL resolution, never
the initial judgment.

## 9. Correlation report (frozen schema)

`engram-calibration-correlation-206-v1`, produced only after all three lanes
freeze: 402 expected cases and per-reviewer completion counts; unanimous
consensus / disagreement / uncertain counts; malformed-error-refusal counts
(UNIQUE affected cases — a case with multiple failure reasons counts once;
reason overlap is reported separately in `queue_reason_overlap`);
mandatory high-consequence count; unique human-queue count before audit;
deterministic 15% audit count; total human workload; per-dimension,
pairwise, and three-way agreement; reviewer identities and config digests;
all artifact digests; and populated `aggregate_by_axis` marginal counts
(source type / kind / review status / age bucket) computed from the
protected frame at reporting time. No raw tenant content, private IDs,
reviewer rationales, or protected labels in public output.

## 10. Lane execution/ingestion workflow (FIX-6)

The exact model invocation is an external Hermes responsibility; the
mechanical handoff is frozen in `evals/calibration/ingestion.py` and the CLI:

```text
python -m evals.calibration model-lane-init \
  --sampling-manifest <protected sampling-manifest.json> \
  --reviewer-identity <model_a identity.json> --reviewer-slot model_a \
  --source-packet-digest <digest> --protected-dir <campaign root>
python -m evals.calibration model-lane-request \
  --sampling-manifest … --reviewer-slot model_a \
  --neutral-packet <neutral packet json> --source-packet-digest … \
  --protected-dir …        # emits lanes/model_a/lane-requests-model_a.jsonl
#   (external executor runs the reviewer over the JSONL; no credentials here)
python -m evals.calibration model-lane-ingest \
  --sampling-manifest … --reviewer-slot model_a \
  --responses <responses.jsonl> --source-packet-digest … --protected-dir …
python -m evals.calibration model-lane-status \
  --sampling-manifest … --reviewer-slot model_a \
  --source-packet-digest … --protected-dir …
python -m evals.calibration freeze-model-lane …   # exact 402 required
```

Guarantees: one lane binds one frozen `ReviewerIdentity` (exclusive-create);
requests carry only that lane's neutral case input plus the frozen labeling
instructions/schema; raw response bytes are preserved under
`lanes/<slot>/raw/` with their digest recorded; ingestion is append-only and
resume-safe (accepted evidence is never replaced, duplicates refused,
requests resume from the next missing sample); status reports
completion/missing/failure counts (never protected sample IDs); freeze
requires exact full frozen membership. A response envelope never supplies
reviewer identity — it comes from the lane authority — so another lane's
output cannot be ingested.

## 11. Serving invariants (unchanged)

```text
assessment_selection_enabled == false
CERTIFIED_SERVING_PROFILES == {"legacy"}
```

Ordinary `/v1/recall` and MCP remain legacy-authoritative. No model review
result changes memory state, admission state, review status, promotion
status, assessment selection, or recall behavior.

## 12. Freeze-before-execution rule

This protocol (document + `consensus.py` constants) landed via PR BEFORE any
reviewer received the packet. Any post-merge change to the frozen values
requires a new protocol version string and an explicit re-freeze.
