# semantic-context-manifest-v1 conformance

The `vectors` directory contains language-neutral finalized packet inputs and
frozen semantic manifests. The expected data includes canonical JSON and all
required hashes.

Run both independent reconstructions:

```text
.venv/bin/python scripts/verify_semantic_context_manifest_vectors.py
node conformance/semantic-context-manifest-v1/verify.mjs
.venv/bin/python conformance/semantic-context-manifest-v1/run_cross_language.py
```

The cross-language driver requires Python and JavaScript to reject the same
negative fixture set.
