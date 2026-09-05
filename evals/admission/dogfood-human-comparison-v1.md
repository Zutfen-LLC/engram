# #173 human-adjudicated current-policy baseline

This report is aggregate-only. It was replayed from the frozen private 50-case corpus and captured policy inputs; it is not production evidence and it does not change memory state, policy, classification, or recall.

## Identity and human review

- Source snapshot digest: `1d1de9f799c2ff203ff0554fee2273d98ed7e697555215c9b7036b2815023117`
- Deterministic tranche selection digest: `0e4167a3fab5591e7a40a10a8fa2782bc3ea31fd7cab776fdc20f99f20ba2c8b`
- Final corpus digest: `4876d3bb1857fcf761f1bb3566d9d7cf76bb21cd2b5acbc8365123d8de527f8a`
- Reviewer A frozen digest: `618a75139bc35681b4efcb3535d9d56d7bfa4ba6ff90606d39bf8ea28e6e86d9`
- Reviewer B digest: `93ea2f3d4eb88928faf77dd9caf0df7c99ba7446b55a02405bd5ea958e4fde5f`
- Adjudication digest: `2d417e6dd74529ca3a023575c8ad5feb9c3e36fb3ff2084952237a51cdc29996`

Reviewer A completed and froze all 50 labels before policy reveal. Reviewer B independently reviewed all 9 high-consequence labels. All 9 A/B disagreements were resolved; there are no unresolved disagreements and no high-consequence labels without Reviewer B.

Raw disagreement counts over the 9 dual-reviewed cases are: expected kind 3, retention value 2, epistemic state 4, consequence 1, storage disposition 5, startup eligibility 3, governed-semantic eligibility 4, and human-review requirement 1. At N=9 these are reported as raw counts, not a false-precision inter-rater percentage.

Final human labels: retention retain 40 / do-not-retain 10; storage retain 31 / defer 9 / reject 10; consequence low 22 / medium 19 / high 9; startup eligible 15; governed-semantic eligible 23; human review required 13. Decision-time epistemic support is adequately supported 21 / weakly supported 23 / unknown 6. Temporal-validity issues are 42, expected supersession 2, and expected conflict 6. Extraction quality is atomic yes 13 / no 28 / unknown 9, with proposition count one 13 / multiple 23 / zero 5 / unknown 9. Thus `retention_value=retain` does not imply a good atomic/propositional extraction.

## Current-policy comparison

The captured current policy automatically admitted 0 of 50 cases; false automatic admissions were 0, including 0 of 9 high-consequence cases. Automatic-admission precision is undefined because current policy predicted zero automatic positives. This is not a precision victory.

The comparison's `agreement_count=38` is deliberately narrow: it means equality between (a) current `would_promote` and (b) the strict human automatic-permission predicate: expected storage disposition=retain, startup eligible=yes, governed-semantic eligible=yes, and human-review-required=no. It does not mean overall correctness, retention agreement, startup agreement, governed-recall agreement, or review agreement. Mismatch is 12 under that exact predicate.

Held-back categories remain separate: human retention retain 40; human expected storage retain 31; human startup eligible 15; human governed-semantic eligible 23. Consequence-stratified strict-predicate agreement/mismatch is low 15/7, medium 14/5, high 9/0. No high-consequence automatic admission occurred.

Stored-kind versus human-expected-kind disagreement is 28. This is a comparison of captured stored kind and human expected kind, not classifier accuracy; classification was not executed. Current policy blocker occurrences are no-retention-evidence 22, retention-disposition 27, confidence 39, kind-policy 16, taxonomy-confidence 16, evidence-score 5, missing-source-prior 2, age 2, and conflict 3. Readiness states are missing-evidence 14, below-taxonomy-confidence-minimum 11, blocked-by-kind-policy 16, below-evidence-threshold 4, cooling 2, and conflict/dispute 3.

## Where current admission behavior disagrees with intended doctrine

A. Evidence/source-prior starvation is the dominant observed under-admission pattern: 37 held human-retained cases had an evidence/confidence/source blocker. This is not proof that every such case should be automatically admitted; it is evidence that the current policy cannot represent useful-but-reviewed, retained, or governed-only states well.

B. Kind/taxonomy gating is also material: 27 cases encountered a kind or taxonomy gate and 28 stored-kind/human-expected-kind disagreements were observed. These are gating/representation mismatches, not a classifier-accuracy claim.

C. Cooling is a small measured contributor: 2 held cases were in `cooling`, so the universal 72-hour delay does not explain the dominant under-admission in this tranche.

D. The human doctrine differentiates retention, automatic admission, startup exposure, governed-semantic exposure, and review: 9 retain judgments were defer rather than storage-retain; 6 governed-eligible judgments required review; 25 retained judgments were not startup eligible. Current binary automatic promotion has no corresponding differentiated tiers.

E. Temporal governance is prominent: 42 labels identify a temporal-validity issue and 2 expect supersession. One later factual outcome was recorded as became-outdated; it remains separate from decision-time epistemic support. Historical retention is therefore not a blanket claim of current-state validity.

F. Extraction quality is a separate problem: 28 labels were non-atomic and 5 had zero propositions. A retained proposition can still require repair or non-admission because the extraction is not a usable memory unit.

G. Safety posture: there were zero high-consequence false automatic admissions, but also zero high-consequence automatic positives. The result demonstrates conservatism in this tranche, not positive-admission precision.

## Incident seed

The private incident seed is `engram-admission-incident-seed-v1`, digest `917d0ff3440e6ee4da97bb77dc934993678668fdf275c4910b42f4bc3748e4c6`, mode 0600 outside Git. It has 4 genuine, distinct examples: held-back-despite-likely-value, stale-or-superseded, conflict-involved, and poor-extraction-or-non-propositional. Every private record keeps decision-time evidence/state and later outcome/context in separate fields.

No qualifying case was observed in this tranche/history for: correctly useful with verified later outcome; incorrectly retained with verified later outcome; wrongly promoted/admitted; or delayed/orphaned by orchestration. The absence is recorded rather than synthesized.

## Bounded implications for later tickets

- #158: low/medium/high labels and review requirements support a risk-based admission investigation; this tranche does not select a threshold or policy.
- #160: 15 startup-eligible, 23 governed-semantic-eligible, and 25 retained-but-not-startup labels support separate recall surfaces; temporal/supersession context should not enter startup as current fact by default.
- #161: evidence-root independence is unknown for all 50 labels; weak/missing source evidence means this tranche cannot certify corroboration behavior.
