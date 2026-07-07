"""Content canonicalization and hashing utilities for dedup.

These pure functions feed the unique index idx_memitems_dedup scoped to
(tenant, workspace, principal). The hash must be deterministic and stable
across releases — never change the algorithm without a migration plan.
"""

from __future__ import annotations

import hashlib

__all__ = ["canonicalize", "content_hash"]


def canonicalize(content: str) -> str:
    """Normalize content for dedup: strip, collapse whitespace, lowercase.

    ``str.split()`` with no arguments splits on any whitespace run and
    discards leading/trailing whitespace, so ``" ".join(...)`` gives us
    single-space-separated output in one call.
    """
    return " ".join(content.split()).lower()


def content_hash(canonical: str) -> str:
    """Return ``sha256:`` + hex digest of the UTF-8 encoded canonical string."""
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
