#162D certification completion report

## Identity

- Issue: #176 (#162D). Epic #153. Parent #162 (remains OPEN).
- Machinery PR: #177 (548be39) — certification doctrine, corpus selection, dual-review freeze, deterministic runner, 36 tests.
- Follow-up PR: #178 (0d5474e) — fresh-capture selection (content-hash identity basis).
- Operational run executed 2026-09-05 against the engram01 dogfood database.

## Gate freeze

- Doctrine artifact: `evals/admission/certification/doctrine-162d-v1.json`, digest `145d82831dc3df01ef2472923aacba94ceff8ed33a4bd4ee90a6eacd9142e7d8`, merged 19:13 UTC (PR #177), cross-snapshot fix merged 19:22 UTC (PR #178).
- Candidate under certification: P3 `candidate-kind-decoupled-v1`, declaration digest `5815772736c23b4c290f3bd3c3833389361bc08fd26015e6d136abed74dbcb01`, #162C freeze digest `0e1692ed0f06f2d5f75f25eebec8a9075efd35aea3ec70333b9f8e095101d116`.
- Controls: current + P0 `candidate-current-compat-v1`. Context only: P1/P2.
- Corpus manifest created 19:28:37 UTC — AFTER both merges: gates frozen before membership existed. `load_doctrine()` drift checks green at every stage.

## Fresh certification corpus

- Fresh live capture on engram01: 241 live proposals at 2026-09-05T19:26:18Z, snapshot digest `962be42f…` (read-only proof: `write_statement_count=0`).
- Selection: seed `eng-162d-certification-20260905-v1`, manifest digest `1b11a5d68da525adc353119c9ecc21dbb788cca7c76b27b1081d3e6900846bfe`, N=100 exactly, shortfall 0.
- Overlap: ZERO against the #162B 50-case dev corpus and the #162C 30-case holdout, proven on BOTH sample IDs and content hashes (fresh-capture basis; a re-keyed capture cannot launder a spent case).
- Strata achieved (7 dimensions): source_type (4 values), kind (3), kind_gate (2), evidence (3), governance (1), temporal_evidence (3), age (4). Composition: sync_turn/fact 54, extraction/fact 42, migration/doctrine 2, manual 2. Unavailable strata: no live conflict/dispute-recalled values in the fresh pool (governance dimension degenerate) — recorded, not hidden.

## Human labeling

- Reviewer A: all 100 cases, blind, operator-ratified (`reviewer-a-frozen.json`, digest `05ed764783f313fc…`), reviewed solely from the packet.
- Reviewer B: independent blind subset — derived queue = high-consequence ∪ substantive disagreement = cases 28, 55, 72.
- Adjudication: 3 substantive A/B disagreements (case 28 startup eligibility; case 55 consequence high→medium; case 72 five fields, truncated fragment → defer), operator-resolved with recorded reasons, no mechanical vote. Disputed-boundary cases recorded (A judged high on 55 and 72).
- Final corpus: digest `0a5cc4d6503243d4fda32b653f356e27afda2e898faa50690b7652c86e019617`, 100 complete labels, 0 unresolved, canonical high-consequence = 1, conservative high boundary = 3. Inter-rater agreement preserved per field in the corpus summary.
- Labels frozen BEFORE any policy/candidate evaluation (reveal gate enforced by the runner).

## Determinism

- The certification runner was executed twice on identical inputs; outputs byte-identical. Report digest covers the full record.

## G0–G7 results

| gate | requirement | observed | result |
| --- | --- | --- | --- |
| G0 parity | 100% storage + auto parity | 100% / 100% | PASS |
| G1 storage accuracy | P3 ≥ current + 5pp | current .02, P3 .13, +11pp; bootstrap 95% CI [+5pp, +17pp] | PASS |
| G2 held-back reduction | ≥ 15% relative | 77 → 66 = 14.29% | **FAIL** |
| G3 high-consequence safety | 0 reject→retain, 0 protected review bypass | 0 reject→retain; **1 review bypass** | **FAIL** |
| G4 review burden | ≤ 35% rate, 100% high-consequence required-review coverage | 2% rate; **1 required-review miss** | **FAIL** |
| G5 false eligibility | 0 high false governed/startup | 0 / 0 | PASS |
| G6 automatic admission | disabled, INSUFFICIENT_EVIDENCE, ppv null | 0 positives, ppv null | PASS |
| G7 signal honesty | unknown stays unknown; no leakage | preserved; labels structurally absent | PASS |

## Terminal decision

**NOT_CERTIFIED** (safety_or_required_numerical_gate_failed).
Automatic admission: **INSUFFICIENT_EVIDENCE** (zero predicted positives; PPV null — never reported as 100%).

### Root cause of the failures

The single canonical high-consequence case (case 28: honest-certification-evidence doctrine content, stored as kind `fact`) is evidence-starved at decision time (`below_taxonomy_confidence_minimum`, blockers confidence/retention_disposition/taxonomy_confidence). Human labels require review. P3 routes it to `defer` with `review=no` (reasons: `defer_insufficient_evidence`, `defer_deferral_window_open`) — P3's review routing fires only for protected KINDS and governance cases, not for high-consequence ordinary-kind cases with starved evidence. Same miss under the conservative boundary. G2's 14.29% vs 15% is a near-miss but frozen doctrine does not round.

## Secondary metrics

- Storage accuracy: current .02 / P0 .02 / P3 .13. Useful held back: 77 / 77 / 66. Review routed: 0 / 0 / 2.
- Gains concentrate in sync_turn/fact (0→11 correct of 54); extraction/fact unchanged (0/42) — P3 does not address evidence starvation for extraction sources.
- False governed/startup eligibility: 0 everywhere. No automatic positives under any policy.

## Authorization scope

A pass would have authorized ONLY accepting P3 as the evidence-backed storage/kind-decoupling design for future #158. This NOT_CERTIFIED result authorizes nothing; production policy, defaults, startup recall, the 72h gate, and #160/#161 are untouched. Parent #162 remains open.

Evidence for #158 design (not authorization): P3's kind-decoupling materially improves storage accuracy (+11pp) but its review routing is too narrow — high-consequence ordinary-kind evidence-starved cases need review routing (an evidence-consequence interaction absent from all #162C candidates). Any future candidate needs a new certification corpus and version.

## Privacy

Raw content, snapshot, reviewer records, ledgers, and per-case results live outside Git on the workstation and engram01 under `~/.local/share/engram/evals/162d/` (0700/0600). Public artifacts carry digests and aggregates only. The certification capture was provably read-only.
