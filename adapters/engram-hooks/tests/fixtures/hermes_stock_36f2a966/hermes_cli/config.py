"""Minimal config dependency for the pinned memory-status extraction."""
from __future__ import annotations

from typing import Any

CONFIG: dict[str, Any] = {}


def load_config() -> dict[str, Any]:
    return CONFIG
