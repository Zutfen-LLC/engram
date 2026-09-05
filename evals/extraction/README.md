# Extraction evaluation v1

The frozen golden set contains 19 synthetic cases. Runtime extraction does not
load this file. The set contains no risk or admission labels.

Run the live evaluation inside the repository CI Compose stack. Supply an
authorized configuration file that enables the classification provider.
The file must remain local and must not be committed.

```bash
docker compose -f docker-compose.ci.yml up -d --wait postgres
docker compose -f docker-compose.ci.yml run --rm \
  -v "$PWD:/app" engram-test \
  python evals/extraction/run.py --env-file /app/.env
```

The runner calls the real API with the non-owner application role and uses
preview mode. It does not create memory items. It writes `live-v1.json` with
the golden file hash, version metadata, per-case outputs, and aggregate
metrics. Provider credentials and raw error details are not included.

Proposition matching uses frozen lexical criteria. Attribution, taxonomy,
retention, evidence, and explicit cues are scored separately. Inspect
per-case results for valid paraphrases that the lexical matcher rejects.
Missing provider cost remains null. Token usage and latency are separate
measurements. A synthetic result does not certify production admission.

The deterministic tests in `tests/test_extraction*.py` use controlled provider
responses. They verify contract behavior, persistence, authorization,
concurrency, fallback, and rollback. They do not measure live model quality.
