"""Canonical ``source_type`` vocabulary, shared by classify and remember.

Before ENG-METER-001 this 7-value list was duplicated verbatim in three
places (``engram/api/routes/memory.py``'s ``SourceKind``, ``engram/api/routes
/classify.py``'s inline ``Literal``, and the SDK's standalone
``engram_client/models.py`` mirror). Telemetry now records ``source_type`` on
every candidate/outcome event, so a fourth silent copy is exactly the kind of
drift this module exists to prevent for the two server-side definitions — the
SDK intentionally keeps its own copy (it does not import the server package)
and must be kept in sync by hand.
"""

from __future__ import annotations

from typing import Literal, get_args

SourceType = Literal[
    "manual", "import", "migration", "extraction", "sync_turn", "pre_compress", "session_end"
]

SOURCE_TYPES: tuple[str, ...] = get_args(SourceType)

__all__ = ["SOURCE_TYPES", "SourceType"]
