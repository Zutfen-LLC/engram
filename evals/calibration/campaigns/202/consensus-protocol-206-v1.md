# ENG-CALIBRATION-001G (#206) — frozen model-consensus review protocol

Protocol version: `eng-calibration-consensus-206-v1`
Campaign: `eng-calibration-001f` (#202 Round-2 freeze — unchanged source of truth)
Frozen: BEFORE any of Claude Opus / GPT Astra / GLM 5.3 Max receives the 402-case packet.
Implementation: `evals/calibration/consensus.py` (+ `model_lanes.py`, `human_queue.py`).

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
captured-at timestamp, parse status (`parsed`/`malformed`), outcome status
(`judged`/`refused`/`provider_error`), reviewer confidence, parsed judgment
(five critical fields plus optional diagnostic fields), raw response digest,
error code where applicable.

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
- selection deterministic by frozen sample ID and seed
  `202-model-consensus-audit-v1` (HMAC-SHA256 rank, ascending);
- label-blind: selection uses only consensus eligibility, never judgment
  values;
- consensus cases with any `high` consequence signal are already in the
  mandatory human queue (they are not ordinary audit-only cases — audit-only
  cases come from the non-high consensus pool);
- marginal coverage of source type, kind, review status, age bucket is
  reported from the protected frame axes; the frozen selection is
  deterministic given consensus membership and is not re-run per axis.

### Frozen audit escalation threshold

Material audit disagreement = the human judgment differs from unanimous model
consensus on ANY of the five critical fields. Escalate to full human review
of ALL remaining consensus-accepted cases if EITHER:

- material audit disagreement rate > 5% of audited consensus cases; or
- any audited case reveals a consensus error the human adjudicates as
  `consequence=high` or that materially reverses a calibration outcome.

This threshold is frozen before execution and never lowered or reinterpreted
after seeing results.

## 7. Final reference-label provenance (frozen)

The final calibration ledger distinguishes:

- `cross_model_consensus` — unanimous three-model consensus accepted under
  this protocol, not human-overridden, not audit-selected (audit-selected
  consensus rows are `human_audited_consensus` by construction);
- `human_adjudicated` — resolved by a human (disagreement/uncertainty/high
  consequence/audit escalation);
- `human_audited_consensus` — audit-selected consensus the human confirmed.

Per row, protected provenance reconstructs: the three first-pass model record
digests, whether consensus was reached, human-queue entry and reasons, the
human initial judgment digest (captured before model votes were revealed),
final adjudicated dimensions, audit selection status, source packet/sampling
identity, and exact reviewer model identities.

`check_floors` and downstream fitting consume ONLY the final reference label
(`ReferenceLabel` / `ConsensusProvenanceWrapper.final_dimensions`) — never
majority votes or raw model judgments. Numeric #202 floors are unchanged.

## 8. Human adjudication workflow (scope-frozen)

`human-queue` builds the queue from three frozen lanes (escalated cases plus
audit-selected consensus; never the full 402). The workflow
(`evals/calibration/human_queue.py`) shows the original blind case evidence,
captures the human's independent initial judgment BEFORE exposing model votes
(validated against the same frozen vocabulary), preserves initial and final
results separately, is append-only/resumable per sample, and exports
mechanically ingestible protected evidence. No general Engram product UI.

## 9. Correlation report (frozen schema)

`engram-calibration-correlation-206-v1`, produced only after all three lanes
freeze: 402 expected cases and per-reviewer completion counts; unanimous
consensus / disagreement / uncertain / malformed-error-refusal counts;
mandatory high-consequence count; escalation-reason overlap; unique
human-queue count before audit; deterministic 15% audit count; total human
workload; per-dimension, pairwise, and three-way agreement; reviewer
identities and config digests; all artifact digests. Aggregate-by-axis counts
(source type / kind / review status / age bucket) are computed from the
protected frame at reporting time. No raw tenant content, private IDs,
reviewer rationales, or protected labels in public output.

## 10. Serving invariants (unchanged)

```text
assessment_selection_enabled == false
CERTIFIED_SERVING_PROFILES == {"legacy"}
```

Ordinary `/v1/recall` and MCP remain legacy-authoritative. No model review
result changes memory state, admission state, review status, promotion
status, assessment selection, or recall behavior.

## 11. Freeze-before-execution rule

This protocol (document + `consensus.py` constants) landed via PR BEFORE any
reviewer received the packet. Any post-merge change to the frozen values
requires a new protocol version string and an explicit re-freeze.
