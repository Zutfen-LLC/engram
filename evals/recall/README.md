# Recall shadow evaluation

This runner evaluates `legacy`, `governed`, and `exploratory` packets through
the shared production shadow comparison. It does not call an authoritative
recall endpoint. It does not write recall logs, exposure counters, receipts,
feedback, admission state, or promotion state.

Use a protected manifest outside the repository. The manifest has frozen query
digests, per-case frozen query embeddings (after capture), request identity,
tenant configuration version, embedding profile, repository SHA, snapshot
digest, and evaluation time. It cannot contain a terminal recommendation.

`snapshot_digest` is a digest of a protected, repeatable-read capture of the
live recall-relevant state. It includes the effective tenant configuration,
embedding profiles, visible memory identities and content hashes, embeddings,
feedback/exposure bindings, assessment/admission state, graph/tunnel state,
principals, workspaces, workspace membership, and the tenant memory-kind
registry. It also includes the versioned `runtime_config_identity`: the closed,
explicit projection of every deployment setting the comparison call graph
reads (recall budget defaults, relationship-expansion toggles/limits/weights,
the merged-candidate ceiling, #157 assessment-selection configuration, and the
query-embedding provider gate). Secrets are never part of the projection; the
provider base URL is bound by digest only. Those rows and settings cover the
shared legacy/governed/exploratory path for visibility, explicit workspace
resolution, feedback-actor qualification, disputed-kind selection, admission,
ranking, and packing. The evaluator clears only its tenant memory-kind cache
entry before capture and replay. This prevents a pre-existing process cache
value from changing replay behavior. It does not change production cache
semantics.

The digest does not reconstruct a historical PostgreSQL state. The runner
recomputes the digest and fails if the connected state or any material runtime
setting differs.

## Deterministic query embeddings

External embedding providers do not promise byte-identical vectors forever, so
deterministic replay is two-phase:

1. **Capture.** A case without a frozen embedding generates its query
   embedding once through the real shared semantic-query gateway. The
   protected private artifact records the exact vector (plus its digest) so an
   operator can freeze it into the manifest. Capture legitimately performs one
   provider call per non-empty-corpus case.
2. **Replay.** A case whose manifest carries `query_embedding` replays through
   the already-supported `query_embedding_override`. The runner makes no
   provider call for that case, and later provider-output changes cannot
   change the replay packet. The public report states how many cases used
   frozen vectors versus live capture.

Query vectors never appear in public artifacts; the manifest and private
output are protected.

## Query accounting

The runner counts actual DBAPI executions for the whole evaluation
transaction and attributes them:

- fixed setup statements (identity checks, snapshot capture/verification);
- per case: the primary legacy/governed/exploratory comparison and the
  neutral-usefulness counterfactual replay, separately;
- metadata/report-support statements;
- fixed finalization statements (the after mutation snapshot).

Per-case counts (keyed by `case_id`) live only in the protected private
artifact. The public report carries aggregate statistics only (counts,
min/max/mean) plus the explicit reconciliation statement that per-case sums
equal the transaction total modulo the identified fixed blocks. The runner
fails if they do not reconcile.

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
   `label_set_digest`. Case strata must use the closed public-safe contract
   (`corpus_scale`, `query_class`); arbitrary strata values fail validation
   rather than reaching any public artifact.
4. Run `python -m evals.recall MANIFEST --capture-state`. Replace the
   placeholder snapshot digest everywhere in the manifest and reseal labels.
   Run capture again. Stop if the digest changes before evaluation.
5. Run the evaluator once with `--private-output` to capture query embeddings
   from the real shared gateway. Freeze each captured vector (values plus
   `vector_digest`) into its manifest case and re-verify the snapshot digest.
6. Create protected output directories (`0700`). Run the frozen-manifest
   evaluator with a private output path outside the repository. It creates
   private files with mode `0600` and public files with exclusive creation.
7. Check the read-only proof, aggregate query accounting (and the private
   per-case counts), provider-call counts, query-embedding sources, and the
   before/after mutation counters. Provider counts come from observers at the
   shared embedding, classification, and assessment gateways. Stop on any
   mismatch or any provider call at all during a fully frozen replay.
8. Re-run with the same frozen manifest and state. Verify equal input and
   public report digests. Collect the public JSON, Markdown, protected
   private artifact, and command output as completion evidence.

The framework status is `RECALL_CORRECTION_REQUIRED` until every framework
obligation passes; only then does it become
`FRAMEWORK_COMPLETE — AUTHORIZED_DOGFOOD_RUN_PENDING` until an authorized
evaluation is reviewed. A run reports only `EVALUATION_EVIDENCE_COLLECTED`. A
later human adjudication must be a separate, provenance-bound artifact.

The runner opens a `REPEATABLE READ READ ONLY` transaction. It records before
and after tenant-visible mutation counters. It fails if the counters differ.
The public JSON and Markdown reports contain aggregates only. They do not
contain query text, memory content, case IDs, arbitrary strata, or query
vectors. The private output uses mode `0600` and cannot be written inside the
repository.

The report digest excludes measured latency. Fixed input, state, profile
identity, configuration, and evaluation time produce the same packet report
digest. Latency remains in the protected report because it is run-dependent.

The report does not certify a profile or change `CERTIFIED_SERVING_PROFILES`.
