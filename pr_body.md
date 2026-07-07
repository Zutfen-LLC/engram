## Summary

Implements write-time conflict detection for Engram (T09). When a new memory item is written via `POST /v1/remember`, it now runs a semantic similarity check against active items in the same scope (tenant, workspace, kind). Above 0.85 cosine similarity, the classifier determines whether the relationship is **duplicate**, **refine**, or **contradict**, and applies the appropriate action: auto-dedup, conditional supersession (respecting the authority hierarchy), or conflict flagging for review. Two review endpoints (`GET /v1/review/conflicts`, `POST /v1/items/{id}/resolve-conflict`) surface and resolve flagged conflicts.

## Files changed

- **engram/conflicts.py** — Replaced the T09 stub with `detect_conflicts(new_item, session)`. Performs the pgvector cosine similarity search against active items in scope, classifies the relationship via LLM (with a heuristic fallback when no LLM is configured), and maps the verdict + authority comparison to an action via `_resolve_action`.
- **engram/api/routes/memory.py** — Wired `detect_conflicts` into `POST /v1/remember` after embedding generation, before commit. Handles dedup (return existing), auto-supersede (mark old + audit event), and conflict flagging (set `conflicts_with_item_id`/`conflict_type`/`conflict_resolution_status`, write audit event). Skipped cleanly when `embedding_provider='none'`.
- **engram/api/routes/review.py** — Implemented `GET /v1/review/conflicts` (lists unresolved conflicts) and `POST /v1/items/{id}/resolve-conflict` (accept/reject/merge, writes `item_event`). Other review stubs left unchanged.
- **tests/test_conflicts.py** — New: 15 tests covering duplicate auto-dedup, refine auto-supersede (high authority), refine proposed supersession (medium confidence), refine lower-authority (scope_overlap, never auto-supersede), contradict flag, below-threshold no-op, skip-when-no-embeddings, conflict listing, conflict resolution endpoint + event audit, invalid resolution 422, and 5 unit tests for the pure `_resolve_action` decision function.

## Testing

All commands run locally in the worktree against a live PostgreSQL 16 + pgvector instance:

- `ruff check .` — **PASS** (all checks passed)
- `mypy engram/` — **PASS** (Success: no issues found in 22 source files)
- `pytest -q` — **PASS** (61 passed: 15 new conflict tests + 46 existing, no regressions)
- `docker compose config -q` — **PASS**

The 29 deprecation warnings in the full pytest run are pre-existing `datetime.utcnow()` calls in `test_recall.py` — unrelated to this change.

## Notes

- **Pre-write rung check:** landed on rung 7 (write the minimum that works). This is core domain logic that reuses existing embeddings (T05) and classifier patterns (T08) — not derivable from stdlib, platform features, or one-liners.
- **Conflict classifier:** the LLM prompt includes both old and new content plus the similarity score. When `classification_provider != 'openai'`, a heuristic fallback treats near-identical embeddings (>0.97) as duplicates and everything else as low-confidence refine (never auto-supersedes). This means the no-LLM path is safe but conservative — it never makes destructive supersession decisions without LLM confirmation.
- **Authority hierarchy** (`explicit_user > trusted_import > trusted_agent > untrusted_agent > inferred`) is enforced via `source_trust` comparison: a lower-trust new item can never silently supersede a higher-trust old item. This is a strict numeric comparison (new < old → scope_overlap), which aligns with the design doc's ranking.
- **1:N conflict limitation (v1):** `conflicts_with_item_id` is a single column — only pairwise conflicts are tracked. This is a known design limitation documented in `docs/design.md` §10; a `memory_conflicts` join table is the eventual solution.
- **Cost escape valve:** conflict detection respects the existing `conflict_check_on_write` config flag (default true) from `engram.config.settings`.

Closes T09.
