# ENG-CALIBRATION-001F (#202) operator runbook — review stage

Status: PRE-REVIEW FREEZE INVALIDATED. Do not begin human review from the v1
packets. The v1 split was incorrectly generated over all 624 eligible rows
instead of the 402 sampled rows. No human labels were accepted before this
invalidation. Corrected v2 artifacts are being frozen against corrected code.

## Invalidated v1 audit trail — do not use

| Artifact | Invalid identity |
| --- | --- |
| Campaign code | repository SHA `3d17923e6553a5a633a819682f9ffad8423cd8ef` |
| Target identity | digest `78d45df3…f3093` |
| Corpus snapshot | 624 eligible rows; sha256 `cc172a6f…8bdb0` |
| Sampling manifest | 402 samples; digest `27de308a…c8475` |
| Split manifest | 362 dev + 262 holdout; digest `2f2b7ec5…ed73` |
| Blind packets | invalidated because their split/identity lineage used v1 |

The protected v1 directory is retained immutably for audit. The authorized raw
snapshot may be copied byte-for-byte into the corrected v2 protected directory;
all derived manifests and packets must be regenerated.

## Invalid v1 accounting

The v1 sample had 402 members, but its split had 624 members (362 dev + 262
holdout). This violates `sample_count == dev_count + holdout_count` and the
stronger exact-membership partition invariant.

## What human review must do

1. Reviewer A labels every corrected v2 case under the frozen guide (label
   schema `engram-admission-label-v1`, dimensions per the guide;
   `label_origin=human_adjudicated`).
2. Reviewer B independently labels every corrected v2 case from the separate
   full-population blind packet. This strict-superset strategy necessarily
   dual-reviews all high-consequence and policy-disagreement cases without
   selecting Reviewer B membership from provider or policy output.
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
