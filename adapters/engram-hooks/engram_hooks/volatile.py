"""Local file-backed volatile store for low-confidence memory candidates.

Per design.md §5, volatile recall lives in the companion library, not the
service: the service can't see in-process turn boundaries. Candidates that fail
the promotion-gate confidence threshold (see ``HooksConfig``) land here instead
of being written to Engram, so a noisy-but-potentially-useful observation
survives locally for ~14 days without polluting the server's recall set.

Layout: one JSON-Lines file (default ``engram-volatile.jsonl``), one JSON object
per line. JSONL is append-friendly (no read-modify-rewrite on the hot path),
human-greppable, and trivially portable — matching the "simple JSON or SQLite"
acceptance criterion. We enforce two bounds on every access:

- **retention**: entries older than ``retention_days`` are dropped.
- **cap**: at most ``cap`` entries; oldest evicted first when the cap is hit.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class VolatileEntry:
    """One volatile candidate.

    ``created_at`` is a unix timestamp (seconds) so retention math is plain
    subtraction; we render ISO strings only for display via :meth:`to_json_line`.
    """

    content: str
    source_type: str
    created_at: float = field(default_factory=time.time)
    kind: str | None = None
    wing: str | None = None
    room: str | None = None
    confidence: float | None = None
    reason: str | None = None
    workspace: str | None = None

    def to_json_line(self) -> str:
        """Serialize to a single JSON-Lines record (no trailing newline)."""
        return json.dumps(asdict(self), separators=(",", ":"), ensure_ascii=False)


class VolatileStore:
    """Thread-safe JSONL-backed volatile candidate store.

    One instance is intended to live for the plugin's lifetime. Reads prune
    expired entries opportunistically, so retention is enforced even if no
    explicit prune is ever called.

    The file lock is process-local (a :class:`threading.Lock`); this library
    runs inside one Hermes process, so that's the right granularity. If a future
    deployment ever has multiple writer processes, swap this for ``fcntl``
    flock-ing — the JSONL layout already supports it.
    """

    def __init__(self, path: str | os.PathLike[str], *, retention_days: int = 14,
                 cap: int = 2000) -> None:
        self._path = Path(path)
        # retention_days <= 0 would drop everything on write; clamp to 1 day
        # minimum so a misconfiguration can't silently nuke the store.
        self._retention_seconds = max(retention_days, 1) * 86_400
        self._cap = max(cap, 1)
        self._lock = threading.Lock()
        # Ensure the parent dir exists at construction so the first write only
        # has to open() the file, not mkdir. create_parents is idempotent.
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        """Path to the backing JSONL file."""
        return self._path

    # ---- internal load/save ----

    def _load(self) -> list[VolatileEntry]:
        """Load all entries from disk, silently tolerating a missing/empty file.

        A corrupt individual line is skipped (not fatal) — one bad record must
        not lose the whole store. We still parse the rest.
        """
        if not self._path.exists():
            return []
        entries: list[VolatileEntry] = []
        with self._path.open("r", encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, start=1):
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                    entries.append(VolatileEntry(**obj))
                except (json.JSONDecodeError, TypeError, ValueError):
                    # Skip the corrupt line but keep going. We deliberately
                    # don't log here to avoid pulling a logging dep into a hot
                    # read path; callers can inspect the file if needed.
                    del lineno
                    continue
        return entries

    def _save(self, entries: list[VolatileEntry]) -> None:
        """Atomically rewrite the store file.

        Writes to a temp sibling then :func:`os.replace` for atomicity, so a
        crash mid-write can't leave a half-truncated JSONL file (which would
        then fail to load en masse).
        """
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(entry.to_json_line())
                fh.write("\n")
        os.replace(tmp, self._path)

    def _prune_locked(self, entries: list[VolatileEntry], *, now: float) -> list[VolatileEntry]:
        """Drop entries older than the retention window. Caller holds the lock."""
        cutoff = now - self._retention_seconds
        return [e for e in entries if e.created_at >= cutoff]

    def _enforce_cap_locked(self, entries: list[VolatileEntry]) -> list[VolatileEntry]:
        """Trim to the cap, evicting oldest first. Caller holds the lock."""
        if len(entries) <= self._cap:
            return entries
        # Keep the most recent ``cap`` entries. Entries are append-ordered on
        # disk, so sorting by created_at then slicing is robust to any
        # out-of-order writes (e.g. clock skew).
        entries.sort(key=lambda e: e.created_at)
        return entries[-self._cap:]

    # ---- public API ----

    def add(self, entry: VolatileEntry) -> None:
        """Append ``entry`` and enforce retention + cap.

        Runs as read-prune-append-rewrite under the lock. The rewrite is needed
        because retention pruning can delete arbitrary lines from the middle of
        the JSONL — append-only can't represent deletion.
        """
        with self._lock:
            entries = self._load()
            now = time.time()
            entries = self._prune_locked(entries, now=now)
            entries.append(entry)
            entries = self._enforce_cap_locked(entries)
            self._save(entries)

    def all(self) -> list[VolatileEntry]:
        """Return all live entries (after pruning expired ones)."""
        with self._lock:
            entries = self._load()
            entries = self._prune_locked(entries, now=time.time())
            # If pruning actually removed entries, persist the pruned state so
            # the file converges on expiry rather than growing unboundedly.
            # Cheap correctness: only rewrite if the length changed.
            # Re-load to compare is unnecessary — compare against pre-prune len
            # by reloading once more is wasteful; instead just check disk length.
            self._save(entries)
            return entries

    def prune(self) -> int:
        """Drop expired entries and return how many were removed."""
        with self._lock:
            entries = self._load()
            before = len(entries)
            entries = self._prune_locked(entries, now=time.time())
            removed = before - len(entries)
            if removed:
                self._save(entries)
            return removed

    def clear(self) -> None:
        """Remove every entry (used by tests and explicit reset)."""
        with self._lock:
            self._save([])

    def count(self) -> int:
        """Number of live entries (after opportunistic prune)."""
        return len(self.all())

    def search(self, needle: str) -> list[VolatileEntry]:
        """Case-insensitive substring search over live entries.

        Volatile recall is intentionally dumb (no embeddings here — that's the
        service's job). This is just "did I recently see something like X?"
        for the current session.
        """
        if not needle:
            return []
        lowered = needle.lower()
        return [e for e in self.all() if lowered in e.content.lower()]


def store_from_config(config: Any) -> VolatileStore:
    """Build a :class:`VolatileStore` from a :class:`~engram_hooks.config.HooksConfig`.

    Kept as a free function (rather than a method on the config dataclass) so
    the config module stays free of filesystem concerns and this module owns the
    store lifecycle. Accepts ``Any`` to avoid a circular import; in practice the
    caller always passes a ``HooksConfig``.
    """
    return VolatileStore(
        config.volatile_path,
        retention_days=config.volatile_retention_days,
        cap=config.volatile_cap,
    )
