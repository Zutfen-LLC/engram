"""Unit tests for the write-boundary guard (``prepare_memory_write_guard``).

No Hermes, no network — pure function tests. These are the guard-accept and
guard-reject cases the ENG-HERMES-001 acceptance criteria call for.
"""

from __future__ import annotations

import pytest

from engram_hooks import is_allowed, prepare_memory_write_guard


@pytest.mark.parametrize(
    "content",
    [
        "currently editing line 42 of foo.py",
        "cursor is at the top of the file",
        "selected lines 10-20 in the editor",
        "scrolled to the bottom of the diff",
        "I'm now typing a reply",
        "open file: config.yaml",
        "undo",
    ],
)
def test_guard_rejects_ephemeral_state(content: str) -> None:
    verdict = prepare_memory_write_guard(content)
    assert verdict["handled"] is True
    assert verdict["action"] == "reject"
    assert "ephemeral" in verdict["reason"]
    assert not is_allowed(verdict)


@pytest.mark.parametrize(
    "content",
    [
        "let me think about this for a second",
        "hmm",
        "how do I configure the database connection string",
        "# just a code comment",
    ],
)
def test_guard_rejects_ambiguous_content(content: str) -> None:
    verdict = prepare_memory_write_guard(content)
    assert verdict["action"] == "reject"
    assert "ambiguous" in verdict["reason"]
    assert not is_allowed(verdict)


def test_guard_rejects_empty_and_none() -> None:
    assert prepare_memory_write_guard("")["action"] == "reject"
    assert prepare_memory_write_guard("   ")["action"] == "reject"
    assert prepare_memory_write_guard(None)["action"] == "reject"  # type: ignore[arg-type]


def test_guard_rejects_too_short() -> None:
    verdict = prepare_memory_write_guard("nope")
    assert verdict["action"] == "reject"
    assert "too short" in verdict["reason"]


def test_guard_allows_durable_fact_and_forwards_taxonomy() -> None:
    verdict = prepare_memory_write_guard(
        "Always use lowercase table names in this schema.",
        kind="invariant",
        wing="engineering",
        room="conventions",
    )
    assert verdict["handled"] is True
    assert verdict["action"] == "allow"
    assert verdict["kind"] == "invariant"
    assert verdict["wing"] == "engineering"
    assert verdict["room"] == "conventions"
    assert is_allowed(verdict)


def test_is_allowed_rejects_none_and_malformed_verdicts() -> None:
    assert is_allowed(None) is False
    assert is_allowed({}) is False
    assert is_allowed({"handled": True, "action": "reject"}) is False
    assert is_allowed({"handled": False, "action": "allow"}) is False
