# ENG-CALIBRATION-001K (#216) operator runbook — pre-review stage

Status: **AWAITING MAINTAINER RE-REVIEW** (post round-1 NO-GO correction).

Round-1 NO-GO (PR #217, reviewed head `ed0565d`) corrected at `d257db1`
(FIX-217-1..6): all execution-critical 001k changes are now committed and
clean-checkout reproducible; the reviewer campaign is the strict two-stage
protocol (Stage A = DEV-only 102 fresh cases; Stage B = artifact freeze;
Stage C = holdout-100 review, mechanically barred until freeze). The original
all-202 reviewer exports were NEVER EXECUTED and are quarantined byte-for-byte
under `protected/quarantined-202-batches-20260912/`.

Phases 0, 1, 2, and 4 remain complete and frozen (re-verified from the
clean checkout). Stage-A DEV review, Stage-B fitting, and Stage-C holdout
evaluation are pending maintainer re-review of the corrected authority.

## Phase 0 — target identity and reconciliation (2026-09-11)

| Fact | Value |
| --- | --- |
| origin/main at freeze | `468cfc0a88119943fd02520c5fdb70a25715c1c8` (= issue baseline, PR #215 merge) |
| Deployed engram01 SHA | `25256f7615c27be683e09e604df1a3bb553ff061` (lags main; assessment contract unchanged) |
| Deployed provider config | openai adapter / `deepseek-ai/DeepSeek-V4-Flash` @ `api.deepinfra.com` |
| provider_config_digest (production form) | `sha256:8488c809d9d1ace29470ad85d57cbebb01b997e1a5730a86263c0e40a0384b45` (identical to 001f freeze — verified against `assessment_config_version()` with the deployed env) |
| Default prompt at main | `engram.assess.3` (assess.1/.2 historical only) |
| assessment_selection_enabled | `false` |
| CERTIFIED_SERVING_PROFILES | `{"legacy"}` |

Campaign tooling SHA for the 001k freeze: `468cfc0a88119943fd02520c5fdb70a25715c1c8`.

**Target identity (001k)** — `a9d8df3427364d507bc986a83ee4e719e937f23ae1a10dcc66d379ba617ddb42` (re-frozen at `d257db1`; the prior `8d2b47c2…` freeze bound the uncommitted working tree and is superseded — same contract fields, corrected campaign tooling SHA),
frozen at `~/.local/share/engram/evals/216/protected/identity-frozen.json`
(0600). Dimensions: `("taxonomy", "retention")` only — the #214 semantic
boundary: `engram.assess.3` emits taxonomy/retention numerics; epistemic
state is evidence-derived and risk is policy-derived, neither is
provider-emitted, so neither is calibrated in this campaign.

Delta `25256f7..468cfc0` was reconciled commit-by-commit: #205 round-2
re-freeze docs, #206/#209/#210 consensus + subscription tooling, #211 CI
format ratchet, #214/#215 assess.3 correction. No serving-path drift.

## Phase 1 — frozen evidence-reuse boundary

Reuse manifest digest: `2a8d3e3f23dacef04fe742e14d7373abc33f3b6806d679453c64ee58d7385fd3`
(protected: `reuse-manifest.json`; public aggregate:
`evals/calibration/campaigns/216/campaign-reuse-public.json`).

Partition (all mechanically derived, never asserted):

```text
population            = the frozen 001f 402-case sample (byte-identical, digest-bound)
executed-200 (dev)    = positions 0..199 of the frozen sample order
                        proven: #213 synthesis case set == first 200 (digest-verified)
fresh-202             = positions 200..401; zero overlap with ALL 18 scanned corpora:
                        213 synthesis/reassess/retry/retry3, 214 replay/replay-3,
                        accepted lane records (both lane roots, incl. per-case .resp),
                        human-queue judgments, executed #208 batches 001-004
spanning-duplicate forced-dev = 25 fresh members of 19 groups shared with executed cases
leakage-safe pool     = 177
holdout (frozen)      = 100  (deterministic: campaign id + seed 216-holdout-v1 + sample id,
                              whole fresh-internal-duplicate-group constrained)
dev-fresh             = 77
split                 = dev 302 / holdout 100, zero cross-split duplicate groups
```

Split digest: `cb7434a5285536e540d9c8898ba446c35f25dfaaf22a1b3d8b323a40067bea37`.
Reused-labels digest: `bc2218d870703ab64a0a25cef683fd19d0c19b29b801e3a07bb6dc6df817c40a`
(derived exclusively from the digest-verified #213 synthesis; per-field
`reviewer_majority` vs `source_adjudication` provenance retained; never
upgraded to human truth).

Frozen floors: identical to the 001f set (total>=300, holdout>=100,
per-dimension>=150, per-bin>=50, per-stratum>=10, high-consequence>=20,
Brier<=0.25, ECE<=0.15, non-unknown fraction>=0.50), applied to the two
frozen dimensions.

## Phase 4 — exact assess.3 provider outputs (complete)

- Executed-200: the frozen #214 replay-3 evidence
  (`sha256:e5ad81da329b9433b22df70c43994f1a5aa52aab98478511d9575ac91c480257`,
  prompt `engram.assess.3`, 198 ok / 2 strict-parse failures preserved as
  abstentions — never fabricated).
- Fresh-202: executed 2026-09-11 under the exact frozen target
  (`216-assess3-fresh202-results.json`,
  `sha256:e7f4b7d3fcc64dee61b147dd982404c9a798d0e3dec424325a151b2c2db7493a`,
  code head `468cfc0a`): **202 ok / 0 errors**.

Labels were never provider input; no prompt/model/config change occurred
after any campaign output.

## Stage A — DEV-only review (corrected, awaiting maintainer re-review)

Three subscription-UI lanes initialized against the DEV-only authority
(`dev-sampling-manifest.json`, seed `216-dev-v1`, 102 cases, zero holdout),
three deterministic batches exported and verified:

| Batch | Cases |
| --- | --- |
| eng-calibration-001k:sub-review-001 | 50 |
| eng-calibration-001k:sub-review-002 | 50 |
| eng-calibration-001k:sub-review-003 | 2 |

Mechanical proofs (clean checkout `d257db1`): batch union == the exact 102
frozen DEV IDs; batch union ∩ holdout == 0; DEV authority binds the new 001k
target digest (`a9d8df34…`), not the 001f one; holdout-stage export fails
closed through the mechanical barrier until artifact freeze.

Paste-ready prompts: `~/.local/share/engram/evals/216/protected/handoff/
paste-001k-dev-sub-review-00{1..3}.txt`. NOT YET AUTHORIZED FOR EXECUTION —
maintainer re-review of PR #217 first.

The superseded all-202 exports live quarantined (never executed).

## Phases 5-7 — fitting, holdout, artifact (pending labels)

Tooling ready: `evals/calibration/campaign_001k_fit.py` builds observations
(reused-200 via `observations_from_reused`, fresh via
`observations_from_fresh_labels`) with the frame `unavailable` ->
stratum `unknown` vocabulary mapping, honest abstention handling, and the
frozen dimension filter. Fitting/holdout/artifact reuse the unchanged
`evals.calibration.fit` contract (exact-stratum-reliability-bins-v1,
MIN_CALIBRATION_SAMPLES=50 bin floor, undersupported strata stay
uncalibrated).

Pre-fit sanity (development side, reused-200 only): the migration/fact
stratum already carries 80 taxonomy observations in the 0.9 bin and 56
retention observations in the 0.8 bin — two claimable profiles exist before
any fresh label is observed. This is recorded for auditability only; it was
computed on DEV evidence only and before holdout labels exist.

## Serving invariants (unchanged, exact)

```text
assessment_selection_enabled == false
CERTIFIED_SERVING_PROFILES == {"legacy"}
```

No production recall/MCP/promotion state was mutated at any point.
