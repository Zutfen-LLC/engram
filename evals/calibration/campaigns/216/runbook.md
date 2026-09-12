# ENG-CALIBRATION-001K (#216) operator runbook — pre-review stage

Status: **AWAITING MAINTAINER RE-REVIEW** (post round-2 NO-GO correction,
FIX2-217-1..7).

Round-1 head `9e019df` was corrected by the FIX2-217 round: provider
evidence and fresh labels are now verified capabilities (never naked
dicts at the fitting boundary), the 001k target authority is mechanically
unique, and the holdout barrier binds a canonical DEV artifact-freeze
record. Round-1's superseded DEV batches/quarantines are preserved under
`protected/quarantined-*-20260912/` (never executed: no reviewer response
was ever collected for ANY 001k batch).

## Phase 0 — target identity (re-frozen at functional head 10f3f57)

| Fact | Value |
| --- | --- |
| origin/main (accepted base) | `468cfc0a88119943fd02520c5fdb70a25715c1c8` |
| Functional tooling SHA for the freeze | `10f3f579e1893ee69480598511b5feb76ff3896d` (FIX2-217-1..4 + case-schema followup; `a79fb0a` was the intermediate functional head — its freeze `8726815d…` is superseded because the case-schema fix is execution-semantic) |
| Target identity digest | `ca2736335917d768458457acb0db34594075a54fae295ebb7d99c857e705c5c6` |
| provider adapter / model | `openai` / `deepseek-ai/DeepSeek-V4-Flash` @ `api.deepinfra.com` |
| provider_config_digest (production form) | `sha256:8488c809d9d1ace29470ad85d57cbebb01b997e1a5730a86263c0e40a0384b45` (re-derived live from the deployed container via `assessment_config_version(resolve_classification_provider())`) |
| prompt | `engram.assess.3`; dimensions exactly `("taxonomy", "retention")` |
| schema / code contract | `engram.assessment.v1` / `assessment-engine-v1` |
| dataset identity | `calibration-157-dogfood-v3-216` |
| Deployed engram01 SHA | `25256f7615c27be683e09e604df1a3bb553ff061` (assessment contract files byte-identical to the branch: the only `engram/` delta vs deployed is the already-live #214 assess.3 correction; verified by sha256 of `assessment_provider.py`/`assessment_schema.py` in the container vs git blobs) |
| assessment_selection_enabled | `false` (verified in-container this round) |
| CERTIFIED_SERVING_PROFILES | `{"legacy"}` (verified in-container this round) |

The protected identity artifact is
`~/.local/share/engram/evals/216/protected/identity-frozen.json` (0600),
generated from the clean checkout `/tmp/engram-216-final` at exactly
`10f3f57`. `verify_target_identity_001k` enforces the full contract at
load; a hash-valid artifact of any other shape is rejected.

## Phase 1 — frozen evidence-reuse boundary (unchanged, re-validated)

Reuse manifest digest: `2a8d3e3f23dacef04fe742e14d7373abc33f3b6806d679453c64ee58d7385fd3`;
split digest: `cb7434a5285536e540d9c8898ba446c35f25dfaaf22a1b3d8b323a40067bea37`;
reused-labels digest: `bc2218d870703ab64a0a25cef683fd19d0c19b29b801e3a07bb6dc6df817c40a`.

Partition: executed-200 dev-only / fresh-202 with 25 spanning-duplicate
forced-dev / leakage-safe pool 177 / holdout 100 / dev-fresh 77; zero
cross-split duplicate groups. Re-validated this round against the retained
artifacts (holdout 100, forced 25, dev_fresh 77).

## Phase 4 — provider evidence

- Reused-200: the frozen #214 replay-3 evidence
  (`sha256:e5ad81da…`, prompt `engram.assess.3`, execution identity
  `1dc42fc…`, 198 ok / 2 preserved strict-parse abstentions). Bound
  through the explicit reviewed historical-authority contract
  (`REPLAY3_HISTORICAL_AUTHORITY`); it cannot and does not claim a 001k
  target digest. Mechanically verified this round:
  `replay3.all_case_ids == frozen executed-200`.
- Fresh DEV-102: re-executed 2026-09-12 under the corrected target
  (`216-assess3-dev102-results.json`,
  `sha256:c9c4d68b7ffd11157ab2196ad9b3b08d8573c517322ec08252f1ff9a92efc23c`,
  code head `10f3f57`, provider-config digest recorded):
  **102 ok / 0 errors**; `all_case_ids ==` the exact frozen 102-DEV
  population, mechanically proven via `stage_provider_values`.
- The holdout 100 were NOT executed: provider execution does not
  authorize holdout review.
- Superseded provider evidence (retained, quarantined): the original
  fresh-202 artifact (202 ok / 0 errors, superseded target `8d2b47c2…`,
  code head `468cfc0`, no config digest recorded) and one intermediate
  a79fb0a-scoped DEV-102 run (102 ok / 0 errors; recorded by digest
  `aea65ba3…` — its bytes were not retained through the re-freeze,
  documented in the quarantine record).

## Stage A — DEV-only review authority (regenerated at 10f3f57)

DEV sampling authority: `dev-sampling-manifest.json`
(sha256 `a7fd5c4f59120ebd0673c7960bb4718d99b91f775f03cace77610a48ec628d90`,
seed `216-dev-v1`, 102 cases = `forced_dev_fresh ∪ dev_fresh`, binds
target `ca273633…`).
Blind packet: `32638601fe9799562222bc6f74e64546b798d59a193a4173d45389c39aeb21b2`.
Neutral packet: `9264c8e6d9fdb88be08f86fe2294a342a4d53c0881e2307d563c9ea388f35249`
(manifest `a5cf7b118c11013939088646e12e8208d8fbeb350817bad8e276397fcfdc3fba`).
Subscription prepare: `6ea55dc326ce9e4d3fbec6307fa21c5a6ff6f2a866dedf8279d866f419bad3cb`.

Three subscription lanes re-initialized (claude-opus / gpt-astra /
glm-5-3-max, same frozen visible-model names and operator reference as
round 1). Deterministic batches:

| Batch | Cases | prompt sha256 |
| --- | --- | --- |
| eng-calibration-001k:sub-review-001 | 50 | `1cb02d97…` |
| eng-calibration-001k:sub-review-002 | 50 | `fd864a29…` |
| eng-calibration-001k:sub-review-003 | 2 | `c212cdbf…` |

Mechanical proofs (clean checkout `10f3f57`): batch union == the exact 102
frozen DEV IDs; batch union ∩ holdout == 0; zero reviewer response files
exist; the DEV authority binds `ca273633…`.

Paste-ready prompts: `protected/handoff/paste-001k-dev-sub-review-00{1..3}.txt`
— NOT YET AUTHORIZED FOR EXECUTION. Maintainer re-review of PR #217 first.

## Phases 5-7 — fitting, holdout, artifact (pending labels)

Fitting boundary (FIX2-217-2/3): `dev_fit_observations` /
`holdout_evaluate_observations` accept ONLY a verified
`FreshLabelAuthority216` (derived from a real `VerifiedConsensusLedger`
over the frozen subscription lanes + human queue) plus a verified
`ProviderEvidence216`; naked dicts are structurally rejected.

Holdout barrier (FIX2-217-7): `record_dev_artifact_freeze` writes the
canonical freeze record at Phase 5 (binding verified target identity,
split digest, DEV membership/evidence digests, exact artifact bytes
digest, fitting methodology, frozen fitting inputs, holdout membership
digest); `unlock_holdout` derives only from it; every 001k holdout
expose/ingest path (sub-prepare/batches/show/import/lane-init) requires
the verified unlock; direct model-lane commands reject 001k outright.

## Serving invariants (unchanged, exact)

```text
assessment_selection_enabled == false
CERTIFIED_SERVING_PROFILES == {"legacy"}
```

No production recall/MCP/promotion state was mutated at any point.
