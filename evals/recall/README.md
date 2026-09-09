# Recall shadow evaluation

This runner evaluates `legacy`, `governed`, and `exploratory` packets through
the shared production shadow comparison. It does not call an authoritative
recall endpoint. It does not write recall logs, exposure counters, receipts,
feedback, admission state, or promotion state.

Use a protected manifest outside the repository. The manifest has frozen query
digests, request identity, tenant configuration version, embedding profile,
repository SHA, snapshot digest, and evaluation time. It cannot contain a
terminal recommendation.

`snapshot_digest` is a digest of a protected, repeatable-read capture of the
live recall-relevant state. It includes the effective tenant configuration,
embedding profiles, visible memory identities and content hashes, embeddings,
feedback/exposure bindings, assessment/admission state, graph/tunnel state,
principals, workspaces, workspace membership, and the tenant memory-kind
registry. Those rows cover the shared legacy/governed/exploratory path for
visibility, explicit workspace resolution, feedback-actor qualification, and
disputed-kind selection. The evaluator clears only its tenant memory-kind
cache entry before capture and replay. This prevents a pre-existing process
cache value from changing replay behavior. It does not change production cache
semantics.

The digest does not reconstruct a historical PostgreSQL state. It does not
prove external provider responses are reproducible. It does not include
deployment settings that PostgreSQL does not store. The runner recomputes the
digest and fails if the connected state differs.

Run the evaluator with a database URL for the application role.

```bash
ENGRAM_DATABASE_URL=... .venv/bin/python -m evals.recall \
  /protected/recall-evaluation.json \
  --private-output /protected/recall-evaluation-private.json \
  --public-json evals/recall/report.json \
  --public-markdown evals/recall/report.md
```

Operational handoff for an authorized dogfood host:

1. Check out the exact approved PR head. Set `ENGRAM_REPOSITORY_SHA` to that
   SHA when the checkout metadata is unavailable.
2. Use the application role. Do not use an owner or migration role. Confirm
   the role can start `REPEATABLE READ READ ONLY` transactions.
3. Create the private manifest with its final cases, labels, evaluation time,
   and a placeholder `snapshot_digest`. Labels must use neutral item states,
   include the case ID and placeholder digest, and be sealed with their
   `label_set_digest`.
4. Run `python -m evals.recall MANIFEST --capture-state`. Replace the
   placeholder snapshot digest everywhere in the manifest and reseal labels.
   Run capture again. Stop if the digest changes before evaluation.
5. Create protected output directories (`0700`). Run the evaluator with a
   private output path outside the repository. It creates private files with
   mode `0600` and public files with exclusive creation.
6. Check the read-only proof, DB statement counts, provider-call counts, and
   the before/after mutation counters. Provider counts come from observers at
   the shared embedding, classification, and assessment gateways. Stop on any
   mismatch or provider call other than the expected semantic query embedding.
7. Re-run with the same manifest and state. Verify equal input and public
   report digests. Collect the public JSON, Markdown, protected private
   artifact, and command output as completion evidence.

The framework status is `FRAMEWORK_COMPLETE — AUTHORIZED_DOGFOOD_RUN_PENDING`
until an authorized evaluation is reviewed. A run reports only
`EVALUATION_EVIDENCE_COLLECTED`. A later human adjudication must be a separate,
provenance-bound artifact.

The runner opens a `REPEATABLE READ READ ONLY` transaction. It records before
and after tenant-visible mutation counters. It fails if the counters differ.
The public JSON and Markdown reports contain aggregates only. They do not
contain query text or memory content. The private output uses mode `0600` and
cannot be written inside the repository.

The report digest excludes measured latency. Fixed input, state, profile
identity, configuration, and evaluation time produce the same packet report
digest. Latency remains in the protected report because it is run-dependent.

The report does not certify a profile or change `CERTIFIED_SERVING_PROFILES`.
