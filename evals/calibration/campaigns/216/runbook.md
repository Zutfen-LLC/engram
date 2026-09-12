# ENG-CALIBRATION-001K (#216) operator runbook — pre-review stage

Status: **AWAITING MAINTAINER FRONTIER REVIEW** (Phase 3 fresh-202 labeling).

Phases 0, 1, 2, and 4 are complete and frozen. Phase 3 (three subscription
reviewer lanes over the fresh 202) and Phases 5-7 (fitting, holdout, artifact)
are pending the maintainer-mediated frontier review and the human queue.

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

**Target identity (001k)** — `8d2b47c273b1d082cf84c2cafeeb47bf740eb3f46e8b91cab8c636f484e6cf7f`,
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

## Phase 3 — fresh review (IN FLIGHT — maintainer handoff)

Three subscription-UI lanes initialized (frozen visible-model authorities,
operator `zutfen-maintainer:216-fresh-review`), 5 deterministic batches
exported (`~/.local/share/engram/evals/216/protected/batches/`), paste-ready
prompts in `.../protected/handoff/paste-001k-sub-review-00{1..5}.txt`:

| Batch | Cases |
| --- | --- |
| eng-calibration-001k:sub-review-001..004 | 50 each |
| eng-calibration-001k:sub-review-005 | 2 |

Import each lane response with `sub-import --campaign-id eng-calibration-001k`
(the CLI subcommands accept `--campaign-id`; 001f remains the default).
After three complete lanes: `model-report`, `human-queue`, then the #208-style
checkpoint adjudication over disagreement/unknown/high-consequence cases.

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
