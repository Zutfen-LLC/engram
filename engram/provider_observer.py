"""Evaluation-only observation of real provider gateway invocations.

The observer is inactive unless an evaluator installs it. Production provider
behavior, including errors and return values, is unchanged.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Final, Literal

ProviderCallCategory = Literal[
    "semantic_query_embedding", "classification", "assessment", "other_model"
]

_CATEGORY_KEYS: Final[tuple[ProviderCallCategory, ...]] = (
    "semantic_query_embedding",
    "classification",
    "assessment",
    "other_model",
)
_active_counter: ContextVar[Counter[str] | None] = ContextVar("provider_observer", default=None)


@contextmanager
def observe_provider_calls() -> Iterator[Counter[str]]:
    """Observe gateway calls in this async context without changing them."""
    counts: Counter[str] = Counter({category: 0 for category in _CATEGORY_KEYS})
    token = _active_counter.set(counts)
    try:
        yield counts
    finally:
        _active_counter.reset(token)


def record_provider_invocation(category: ProviderCallCategory) -> None:
    """Record one call at a shared provider gateway when observation is active."""
    counts = _active_counter.get()
    if counts is not None:
        counts[category] += 1


__all__ = ["observe_provider_calls", "record_provider_invocation"]
