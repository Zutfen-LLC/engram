# ENG-CALIBRATION-001F (#202) operator runbook — review stage

Status: STOPPED FOR HUMAN ADJUDICATION. Everything before human review is
frozen and digest-bound; everything after it is blocked on the reviewed
labels passing the frozen floors and gates.

## Frozen so far (do not regenerate)

| Artifact | Where | Identity |
| --- | --- | --- |
| Campaign code | `evals/calibration/` @ PR #203 | branch `202-dogfood-157-calibration` |
| Target identity | protected `identity-frozen.json` + public manifest | digest `78d45df3…f3093` |
| Corpus snapshot | protected `raw-items-snapshot.json` (624 eligible rows) | sha256 `cc172a6f…8bdb0` |
| Sampling manifest | protected; digest in public manifest | digest `27de308a…c8475` |
| Split manifest | protected; digest in public manifest | digest `2f2b7ec5…ed73` |
| Blind packets (A/B) | protected `eng-calibration-001f-blind-v1.*.json` | sha256 in `packets-manifest.json` |
| Label guide | `evals/labeling/calibration-157-v1.md` | `engram-calibration-guide-157-v1` |

Protected root: `~/.local/share/engram/evals/202/` (0700, files 0600).

## Sample achieved

402 samples / 624 eligible; dev 362 / holdout 262; 30 duplicate groups all
contained within one split; per-stratum counts in the public manifest.

## What human review must do

1. Reviewer A labels all 402 cases from the reviewer_a packet under the
   frozen guide (label schema `engram-admission-label-v1`, dimensions per
   the guide; `label_origin=human_adjudicated`).
2. Reviewer B independently labels (at minimum) every case Reviewer A marks
   `consequence=high`, plus the policy-disagreement set — derived AFTER A's
   labels are frozen, never chosen by the reviewer.
3. Operator adjudicates every substantive disagreement with a recorded
   reason; unresolved disagreement keeps `disagreement=unresolved` and the
   sample contributes no calibration support.
4. Labels are ingested via `ingest_reviewer_labels` (fail-closed) and the
   adjudicated ledger is frozen via `freeze_ledger`.

Reviewers must not see: provider scores, model suggestions, this runbook's
outcome statistics, or each other's labels before adjudication.

## After review (blocked until labels exist)

1. Capture provider scores for the 402 samples under the frozen contract
   (frozen-clone discipline; never bulk-enqueue on production).
2. Join labels+scores into `LabeledObservation`s via the frozen outcome
   mapping; `check_floors` must pass or the campaign terminates
   `CALIBRATION_EVIDENCE_INSUFFICIENT`.
3. `fit_profiles` on DEV only; `evaluate_holdout`; `build_artifact`.
4. `gate_checks` decides the selection-enable recommendation. Any failure
   keeps `assessment_selection_enabled=false`.

## Invariants (verified at every step)

- `CERTIFIED_SERVING_PROFILES == {"legacy"}` — unchanged, asserted.
- `assessment_selection_enabled` — false on engram01 throughout.
- No serving/default/candidate-ranking change in this campaign.
