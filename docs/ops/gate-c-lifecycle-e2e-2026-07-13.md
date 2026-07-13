# Gate C — Lifecycle E2E Verification

**Date:** 2026-07-13
**Proof level:** live-proven (against `https://engram.zutfen.com` dogfood instance)
**Prerequisite:** PR #81 merged (e3a801b) — ABC conformance, install() return type, guard API fixes

## Summary

All Gate C lifecycle sub-gates PASS against the live dogfood Engram instance.

| Gate | Description | Result |
|------|-------------|--------|
| C-3 | Plugin code fix (PR #81) | PASS — merged, CI green |
| C-3b | Plugin instantiation | PASS — `native_hook=True, compat_shim=False` |
| C-4 | SDK write + search/recall round-trip | PASS |
| C-5 | Guard routing (length pre-filter) | PASS — 10/10 correct |
| C-6 | Recall round-trip | PASS — startup recall returns written items |
| C-7 | Idempotent installation | PASS — 3 installs, same result |
| C-EXTRA | prepare_memory_write lifecycle hook | PASS — durable handled, trivial rejected |

## Evidence

### C-3b: Plugin instantiation

Direct import test in the Hermes venv:

```
Plugin import: OK
name=engram_memory, is_available=True
native_hook=True, compat_shim=False
```

**Note:** The engram profile's running process (PID 107121, started 10:52) still has the old cached module from before the fix. A `/reset` creates a new session but does not reload Python plugins. A full process restart (`hermes -p engram` from a new terminal) is needed for the plugin to load as the active memory provider. The plugin code on disk is verified correct via direct import.

### C-4: SDK Write + Search/Recall

Write via `engram_client.EngramClient.remember()`:

```
Write: id=80e0109c-2c29-4e69-8bfc-0ac9c21843f5, review_status=active, status=created
Search FOUND: id=80e0109c-2c29-4e69-8bfc-0ac9c21843f5
Recall FOUND: id=80e0109c-2c29-4e69-8bfc-0ac9c21843f5, score=0.63
```

Write succeeds. Item is immediately searchable via keyword search and appears in startup recall with active review_status. Source type `manual` from `user` principal maps to `review_status=active, source_trust=0.9` as designed.

### C-5: Guard Routing

The `prepare_memory_write_guard` function is a **length-based pre-filter**, not a semantic classifier:

- **< 12 non-whitespace chars:** REJECTED with reason `too short (N non-whitespace chars) — no durable signal`
- **>= 12 non-whitespace chars:** ALLOWED (passes to Engram for storage)

Semantic content classification is the Phase 1B LLM layer (`POST /v1/classify`), not the guard.

Test results (10/10 correct):

| Content | Non-ws chars | Allowed | Expected |
|---------|-------------|---------|----------|
| "Done." | 5 | False | False |
| "PR merged." | 9 | False | False |
| "File uploaded." | 13 | True | True |
| "Build finished." | 14 | True | True |
| "Task completed successfully." | 26 | True | True |
| "User prefers concise responses..." | 54 | True | True |
| "The production database is at..." | 54 | True | True |
| "SSH changes must use Match User..." | 50 | True | True |
| "" (empty) | 0 | False | False |
| "   " (whitespace) | 0 | False | False |

### C-6: Recall Round-Trip

Write a unique signature item, then verify it appears in startup recall:

```
Written: id=7ad661f6-6ae8-4f88-b88f-28311f867585
Startup recall returned 9 items
FOUND in recall: id=7ad661f6-..., score=0.63
working_set contains target: True
```

The recall scoring breakdown for written items:
```
score=0.63: importance=0.50, source_trust=0.90, memory_confidence=0.90, freshness=0.50, recency=0.50
```

**Note:** Semantic search (mode=semantic) returns 0 items because the dogfood instance has no embedding provider configured (`embedding_provider='none'`). Keyword search and startup recall work correctly. Embeddings are a Gate D concern (live embeddings/worker dogfood).

### C-7: Idempotent Installation

Three sequential `install()` calls return identical results:

```
Install #1: native=True, compat=False
Install #2: native=True, compat=False
Install #3: native=True, compat=False
```

### C-EXTRA: Plugin prepare_memory_write Lifecycle Hook

The `EngramMemoryProvider.prepare_memory_write()` hook:

- **Durable content:** Returns `{"handled": True, "result": "Stored in Engram: ..."}` — intercepts the write and routes it to Engram as a `sync_turn` source item.
- **Trivial content:** Returns `{"handled": True, "result": "Rejected by Engram guard: too short ..."}` — intercepts and rejects before the write reaches either store.

## Known limitations

1. **Embeddings not active** — dogfood instance has `embedding_provider='none'`. Semantic search and semantic recall return empty. Keyword search and startup recall work. This is a Gate D concern.

2. **Process restart required for in-session activation** — The running engram profile process cached the old plugin module. `/reset` creates a new session but doesn't reload Python plugins. A full process restart is needed. The plugin code on disk is verified correct via direct import test.

3. **Guard is length-based** — `prepare_memory_write_guard` rejects content shorter than 12 non-whitespace characters. It does not perform semantic classification (that's the Phase 1B LLM layer). Some short-but-meaningful phrases (e.g., "File uploaded.") pass the guard and would be stored in Engram.

## Test artifacts

- 5 test items written to `gate-c-test/lifecycle-e2e` wing/room in the dogfood DB
- 2 test items in `gate-c-test/recall-roundtrip`
- 1 item via plugin hook in default wing
- All items have `review_status=active` and are visible in startup recall
