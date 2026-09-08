"""Pure budget accounting for semantic recall packets."""

from __future__ import annotations

__all__ = ["semantic_item_byte_count", "semantic_item_token_cost"]


def semantic_item_byte_count(content: str) -> int:
    """Return the exact UTF-8 content-byte cost used by semantic recall."""
    return len(content.encode("utf-8"))


def semantic_item_token_cost(content: str) -> int:
    """Return the legacy semantic token cost for one selected item.

    The serving contract charges content bytes only. It does not charge the
    rendered kind prefix or item separators. It uses floor division with a
    one-token minimum.
    """
    return max(1, semantic_item_byte_count(content) // 4)
