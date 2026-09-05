#162C final holdout completion

Freeze

- Private raw B/C/D Markdown, normalized records, A freeze, final adjudication, final corpus, snapshot, and per-case results are outside Git under `~/.local/share/engram/evals/162c/reviewer-provenance/`, protected `0700`/`0600`.
- B source hash: `53d1c251a261faf7f1e7f8c7d3644c2794ea4ffe3683ee13ed6d5d14d54828fb`; normalized digest: `bb6e12f5122f1aec8d6a554a714c7e62dcaa183548350d17063f669e4c63ff82`.
- C source hash: `257f5bd9a30ca25a95761c15f9e1a6124d8b0219348659a6e4e71a57524bd07b`; normalized digest: `f5c420ef7e35b96ff0bc0ee87216b3574bf6cdd31cb7f441f41e318870cb81d0`.
- D source hash: `a6e011265c7d0cb0fc9ec77098684742408216eaa13432d50f9aa1a4b237fc47`; normalized digest: `e93b3c970bea1464cfdd4e0942d0b03f2452bf2e66e6c46f162acf28360af272`.
- A/B/C/D membership: 30/8/30/30. B maps exactly to frozen-A high cases 2, 6, 7, 8, 12, 21, 22, 24. Development/holdout overlap remains zero.
- Final adjudication digest: `8bb09759d94aa4796d6d5b6266e314257077803a56410b26b70d0cc2afee8e55`.
- Final corpus digest: `0d59fb92387c05b2a9b67118402ade183c021299ba8512d32f8ebe39f2d6a8c4`.
- Regeneration was deterministic. Reviewer normalization rejects policy/candidate contamination, membership drift, and unrepresentable judgments. Candidate outputs were not consulted before sealing.

Results

| policy | dev accuracy / held / review | holdout accuracy / held / review | false automatic admissions | holdout cost |
| --- | --- | --- | --- | --- |
| current | .22 / 29 / 0 | .367 / 13 / 0 | 0 | 75 |
| P0 compatibility | .22 / 29 / 0 | .367 / 13 / 0 | 0 | 75 |
| P1 tier-separated | .22 / 29 / 17 | .367 / 13 / 8 | 0 | 0 |
| P2 evidence-recovery | .22 / 29 / 38 | .367 / 13 / 27 | 0 | 32 |
| P3 kind-decoupled | .34 / 23 / 17 | .467 / 10 / 8 | 0 | 0 |

P0 matches the current policy's storage and automatic-admission surfaces for all 30 holdout cases. Every policy had zero automatic positives, so PPV is null rather than 100%. The canonical high-consequence slice is 8 cases; conservative disputed-boundary sensitivity is 10. Both have zero false automatic positives for every policy. No candidate uses unavailable future signals; unknown/abstention is zero on this captured replay.

H1 — P3’s development gain generalizes: accuracy rises .12 on both development and holdout; useful held-back memories fall 29→23 in development and 13→10 in holdout.

H2 — Evidence recovery does not generalize as storage recovery: P2 leaves held-back count unchanged while creating 27/30 review routing (38/50 on development).

H3 — Taxonomy/kind decoupling generalizes: P3 is the only candidate with a holdout storage/recovery improvement.

H4 — No unsafe automatic admission was observed. This is necessary, not certification: zero predicted automatic positives leaves PPV undefined.

H5 — Review burden: P1 and P3 route 8/30; P2 routes 27/30. P2’s burden is not justified by a holdout recovery gain.

H6 — No measured gain depends on unavailable future signals; candidates consume only captured policy inputs. Their automatic-admission claims remain unproven because this holdout has no automatic positives.

Pareto and shortlist

- P0: `interesting_but_blocked` — parity baseline; no material recovery.
- P1: `interesting_but_blocked` — cost reduction but no recovery; adds review.
- P2: `interesting_but_blocked` — no recovery; disproportionate review burden.
- P3: `eligible_for_162D_certification` — holdout recovery, no observed safety regression, and modest review burden.

On the stated lower-is-better objectives, P0, P1, and P3 are non-dominated; P2 is dominated by P3. #162D owns numerical certification gates and rollout doctrine.
