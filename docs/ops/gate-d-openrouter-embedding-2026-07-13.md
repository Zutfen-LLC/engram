# Gate D — OpenRouter Embedding Verification

**Date:** 2026-07-13
**Proof level:** live-proven (against `https://engram.zutfen.com` dogfood instance)
**Provider:** OpenRouter (OpenAI-compatible API)
**Model:** `nvidia/llama-nemotron-embed-vl-1b-v2:free`
**Dimensions:** 1536 (via OpenRouter's `dimensions` parameter)

## Summary

Gate D embedding pipeline verified end-to-end: write → embed → semantic search → semantic recall.

| Gate | Description | Result |
|------|-------------|--------|
| D-1 | OpenRouter env vars configured on dogfood | PASS |
| D-2 | Service restarted with embedding_provider=openai | PASS |
| D-3 | Nemotron embedding profile created + activated | PASS |
| D-fix | `encoding_format="float"` fix for OpenAI-compatible providers | PASS (commit 1498257) |
| D-4 | Worker generates embeddings via OpenRouter | PASS — 26 ready, 0 failed |
| D-5 | Semantic search returns relevant results | PASS — 5/5 queries correct |

## Root cause found and fixed

The OpenAI SDK v2.24.0+ defaults to `encoding_format="base64"` when not explicitly set. OpenRouter (and potentially other OpenAI-compatible providers) does not support base64-encoded embeddings — it returns a valid HTTP 200 with the embedding data as floats, but the SDK's post-parser detects the encoding mismatch and raises `ValueError("No embedding data received")`.

**Fix:** Added `encoding_format="float"` to the `client.embeddings.create()` call in `engram/embeddings.py` (commit 1498257). This is compatible with both native OpenAI and OpenRouter.

## Configuration

### Dogfood .env additions

```
ENGRAM_EMBEDDING_PROVIDER=openai
ENGRAM_OPENAI_API_KEY=<OpenRouter key>
OPENAI_BASE_URL=https://openrouter.ai/api/v1
```

### docker-compose.yml addition

```yaml
OPENAI_BASE_URL: ${OPENAI_BASE_URL:-https://api.openai.com/v1}
```

### Embedding profile

```
openrouter-nemotron-1536
  provider=openai
  model=nvidia/llama-nemotron-embed-vl-1b-v2:free
  dimensions=1536
  state=active
  index=ready:idx_emb_profile_c72c56b8eef69962
```

## Semantic search evidence

All 5 test queries returned semantically relevant results with correct ranking:

| Query | Top result | Score |
|-------|-----------|-------|
| "database vector similarity search" | Vector databases enable semantic similarity search... | 0.4797 |
| "machine learning embedding generation" | Machine learning models can generate embeddings... | 0.4458 |
| "container orchestration health probes" | Container orchestration platforms manage... | 0.5250 |
| "agent memory platform providers" | The Hermes agent platform supports multiple... | 0.4826 |
| "PostgreSQL approximate nearest neighbor" | PostgreSQL with pgvector provides efficient... | 0.5099 |

Semantic recall also returns relevant items. Hybrid search combines keyword + semantic results.

## Deployment note

The image must be rebuilt (`docker compose build engram-service`) for the encoding_format fix to take effect. Volume-mounting the source file over the installed package does NOT work because `pip install .` copies the package to `site-packages/` and Python's import system resolves the installed copy, not the `/app/engram/` source. A clean image rebuild bakes the fix into the wheel installed during `pip install .`.
