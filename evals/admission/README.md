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

Public baseline reports contain aggregate observations and content-addressed
manifest evidence. They do not publish private sample membership or per-item
content hashes. Never use the historical 239-item count as a new measurement.

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
