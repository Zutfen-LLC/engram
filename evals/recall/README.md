# Recall shadow evaluation

This runner evaluates `legacy`, `governed`, and `exploratory` packets through
the shared production shadow comparison. It does not call an authoritative
recall endpoint. It does not write recall logs, exposure counters, receipts,
feedback, admission state, or promotion state.

Use a protected manifest outside the repository. The manifest has frozen query
digests, request identity, tenant configuration version, embedding profile,
repository SHA, snapshot digest, and evaluation time.

Run the evaluator with a database URL for the application role.

```bash
ENGRAM_DATABASE_URL=... .venv/bin/python -m evals.recall \
  /protected/recall-evaluation.json \
  --private-output /protected/recall-evaluation-private.json \
  --public-json evals/recall/report.json \
  --public-markdown evals/recall/report.md
```

The runner opens a `REPEATABLE READ READ ONLY` transaction. It records before
and after tenant-visible mutation counters. It fails if the counters differ.
The public JSON and Markdown reports contain aggregates only. They do not
contain query text or memory content. The private output uses mode `0600` and
cannot be written inside the repository.

The report digest excludes measured latency. Fixed input, state, profile
identity, configuration, and evaluation time produce the same packet report
digest. Latency remains in the protected report because it is run-dependent.

`INCONCLUSIVE` is the default terminal recommendation. A report does not
certify a profile or change `CERTIFIED_SERVING_PROFILES`.
