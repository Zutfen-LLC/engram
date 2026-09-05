# Admission evaluation v1

This package measures current promotion readiness. It does not change policy.
See the [labeling handbook](../labeling/admission-v1.md) and
[field reference](../labeling/field-reference-v1.md).

Validate the public contract set and print its aggregate baseline:

```bash
.venv/bin/python -m evals.admission evals/admission/contract-v1.json
```

Add `--results /protected/location/results.json` to record each policy result.
The target must not exist. Private results must remain outside the repository.
The aggregate output never contains sample text or raw production IDs.
The result file contains policy output only. It cannot contain label fields.

To regenerate the authored contract and machine schemas:

```bash
.venv/bin/python -m evals.admission.build_contract
.venv/bin/python -m evals.admission.reference
```

The generator does not call the policy evaluator to assign labels.
The checked-in contract uses a fixed evaluation time and policy configuration.
Its `code_sha` identifies the production policy baseline. Reports also include
a digest of the evaluator source files. Changing the evaluator changes that digest.
The final implementation commit is recorded separately in the completion report.

## Authorized dogfood snapshot

Run the capture command where authorized PostgreSQL access already exists.
Set `ENGRAM_DATABASE_URL` through the protected environment. Use an authorized
application role and explicit tenant/principal IDs. The capture sets RLS context.
For reproducible sample identities, set `ENGRAM_EVAL_SNAPSHOT_KEY` to a protected,
random hex-encoded key of at least 32 bytes. Keep it outside evaluation artifacts.

```bash
python -m evals.admission.snapshot \
  --tenant "$EVAL_TENANT_ID" \
  --principal "$EVAL_PRINCIPAL_ID" \
  --code-sha "$EVAL_POLICY_SHA" \
  --dataset-id dogfood-admission \
  --dataset-version "$EVAL_SNAPSHOT_VERSION" \
  --sampling evals/admission/sampling-v1.json \
  --output /protected/location/dogfood-snapshot.json
```

The provided sampling definition takes a census of live proposals. For a future
labeling queue, change `selection_method` to `stratified_hash` and choose a
`per_stratum` allocation. Preserve the original census artifact. Sampling uses
source, kind, review status, blocker combination, evidence, selected lane, age,
conflict, dispute, and recalled state. Unavailable recalled state remains unknown.
No risk stratum exists until a consequence judgment exists.

The capture does not invoke recall, classification, promotion jobs, or conflict
rechecks. It uses the same canonical policy evaluator and evidence/readiness
helpers as production. The database rejects writes for the entire transaction.
Jobs use the existing bounded diagnostic lookup. A very large per-item job
history can exceed that diagnostic window; this is not a complete job audit.

Preserve the private artifact to reproduce the baseline. A second live capture
has a new timestamp and can have a different population. Replaying the saved
artifact uses its fixed timestamp and configuration.

## Blind human-review packet (#173 first handoff)

`evals.admission.blind_review` creates only the pre-adjudication artifacts:
deterministic protected membership, a blind JSON/Markdown packet, and empty
resumable review state. It never invokes the promotion evaluator and the packet
serializer rejects current-policy fields.

Run it only on an authorized host. Keep every output except the generated
content-free public manifest outside the repository. The directory is created
mode `0700`; every private file is exclusive-created mode `0600`.

```bash
python -m evals.admission.blind_review select \
  --snapshot /protected/162a/dogfood-snapshot.json \
  --snapshot-key-file /protected/162a/.snapshot-key \
  --seed eng-calibration-001b-dogfood-20260905-v1 \
  --target-count 50 \
  --code-sha "$REVIEW_TOOL_SHA" \
  --private-output /protected/162b/tranche-private.json \
  --public-output /tmp/blind-tranche-public.json
```

```bash
python -m evals.admission.blind_review packet \
  --snapshot /protected/162a/dogfood-snapshot.json \
  --tranche /protected/162b/tranche-private.json \
  --snapshot-key-file /protected/162a/.snapshot-key \
  --tenant "$EVAL_TENANT_ID" \
  --principal "$EVAL_PRINCIPAL_ID" \
  --json-output /protected/162b/blind-packet.json \
  --markdown-output /protected/162b/blind-packet.md \
  --state-output /protected/162b/review-state.json \
  --proof-output /protected/162b/read-only-proof.json
```

Content recovery sets a repeatable-read `READ ONLY` transaction and RLS context,
queries only selected snapshot content hashes, then verifies each returned row
by recomputing its HMAC identity from the protected snapshot key. It also checks
the captured content hash. It never joins by text. Secret-bearing content is
replaced with a fixed redaction marker before either packet is written.

The review state intentionally has no Reviewer A/B judgments, frozen timestamp,
or policy reveal. Do not add labels or reveal/compare current policy until a
human reviewer has frozen the applicable judgment.

## Fresh dogfood baseline (2026-09-05)

Captured on `engram01` against the live dogfood database (deployed policy
`662068da`), authorized application role, tenant `Default`, census of 239 live
proposals at `2026-09-05T01:44:17Z`. The content-free aggregate report is
checked in as `dogfood-baseline-v1.json`. The private snapshot artifact
(HMAC sample IDs, no memory text) remains on the dogfood host in a
protected operator-data location outside the repository, mode `0600`.
It is an unlabeled operational baseline, not a correctness measurement.

Public baseline reports contain aggregate observations and content-addressed
manifest evidence. They do not publish private sample membership or per-item
content hashes. Never use the historical 239-item count as a new measurement.

## Terminology: policy version states

`current_policy_version` reports four distinct states:

- `promotion-legacy-v1` — the canonical evaluator selected the legacy
  confidence promotion basis.
- `promotion-evidence-v1` — the canonical evaluator selected the
  retention-evidence promotion basis.
- `none` — evaluation completed under known policy/configuration, but no
  promotion basis was selected (insufficient confidence or evidence score,
  taxonomy confidence, retention disposition, kind policy, conflicts or
  disputes, missing evidence, or another known policy blocker). `none` does
  not mean evaluation failed, and it does not contribute to
  `unknown_policy_count`.
- `unknown` — required policy/configuration/state was unavailable such that
  the evaluator cannot responsibly state the applicable policy result (for
  example, a missing configuration snapshot). Only these rows count in
  `unknown_policy_count`.

Blocked rows are not rows without a policy: they carry `current_policy_version="none"`.

## Verification and limits

`make check` and the Compose CI path include the contract tests. Both also run
strict type checking for this package. The PostgreSQL test records every capture
statement and rejects an injected accidental write.

The public set has 28 distinct cases. It includes explicit stale, incorrect,
conflicting, adversarial, and non-propositional roles. The credential case uses
a redacted marker. It does not send a credential to production. The evaluator
observes promotion of a hypothetical stored item; it does not test initial
secret rejection through the remember endpoint.

The 27 known-kind matches compare the captured kind to authored taxonomy.
They are not classifier accuracy measurements. Retention comparisons measure
static promotion readiness. They are not storage precision. Three protected
synthetic cases meet static promotion policy despite an authored review
requirement. No representative dogfood precision, factual accuracy, false-
promotion rate, cost estimate, calibration claim, or rollout threshold follows
from these observations.

Existing classification and recall corpora remain unchanged. The admission
loader rejects them because they do not carry the new contract. Existing recall
golden entries never become factual labels through this package.

## Human corpus freeze and comparison (#173)

`human_corpus.finalize_human_corpus()` is the private, policy-blind finalization
step. It rejects policy fields in the Reviewer B and adjudication artifacts,
preserves both reviewers, validates each final `engram-admission-label-v1`
record, and fails closed unless every high-consequence label has Reviewer B and
all disagreements are resolved. `human_comparison.compare_frozen_corpus()`
first verifies that frozen digest/gate, then evaluates only the captured
snapshot policy inputs; it never reads or mutates live memories.

The checked-in `dogfood-human-corpus-v1.json`, `dogfood-human-comparison-v1.json`,
`dogfood-human-comparison-v1.md`, and `dogfood-human-incident-seed-v1.json` are aggregate-only
reports. `incident_seed.build_incident_seed()` creates the private `0600` incident artifact from
the final frozen corpus and comparison, keeping review-case IDs and decision-time/later-outcome
records out of Git. Private labels, private membership, source snapshot, and per-case policy output
remain outside Git under restricted permissions. A zero automatic-admission count is reported with PPV
`null`/undefined, never as 100% precision.

## #162D certification (P3 kind-decoupled candidate)

`evals.admission.certification` extends the merged #162C machinery — it does
not duplicate it. The committed `certification/doctrine-162d-v1.json` is the
versioned, digest-pinned ADR/config required by the ticket's pre-run gate
freeze: candidate/baseline digests, all numerical gates G0–G7, the paired
bootstrap uncertainty method (10,000 resamples, fixed seed), corpus doctrine
(N=100, zero overlap with the spent #162B/#162C corpora, exclusions applied
BEFORE selection), review workflow, unknown-signal rules, terminal statuses,
and non-authorization semantics. `load_doctrine()` fails closed on any drift.

Workflow (private artifacts outside Git, 0700/0600, via
`python -m evals.admission.certification`):

1. `select-corpus` — fresh N=100 manifest from a live snapshot; requires the
   doctrine on disk (gates frozen before membership exists); records derived
   zero-overlap proofs against both spent corpora; flags population
   shortfall as a pre-declared INCONCLUSIVE run.
2. blind packet + Reviewer A over all 100 (existing `blind_review` CLI),
   then `expand-reviewer-a` to freeze the A ledger.
3. Reviewer B independently reviews the derived queue (every
   high-consequence case plus every substantive A/B disagreement);
   `finalize-corpus` seals the dual-reviewed corpus — it refuses unresolved
   substantive disagreement, missing B coverage, or any policy field, and
   must run BEFORE any reveal.
4. `run` — deterministic certification evaluation (replayed twice,
   byte-compared): P0/current exact parity gate, paired +5pp storage
   accuracy with bootstrap interval, 15% relative held-back reduction,
   zero-violation high-consequence safety gates, 35% review-burden gate,
   false governed/startup eligibility gates, and automatic admission locked
   to INSUFFICIENT_EVIDENCE with ppv=null at zero positives (an unexpected
   positive fails closed). Terminal decision is exactly one of
   CERTIFIED_STORAGE_POLICY / NOT_CERTIFIED / INCONCLUSIVE; a passing point
   estimate whose interval cannot support a defensible conclusion is
   INCONCLUSIVE, never a silent pass.

A pass authorizes only accepting P3 as the evidence-backed design for the
storage/kind-decoupling portion of future #158 implementation. It changes no
production behavior.
