"""Write-boundary guard for the engram-hooks companion library.

The write-boundary guard sits in front of every memory write (lifecycle hook
candidates *and* the compatibility shim's ``prepare_memory_write`` path). Its
job is to reject candidates that should never reach Engram:

- **ambiguous** candidates — too short, no signal content, or recognized
  ephemeral patterns ("thinking out loud", prompts, system chatter).
- **ephemeral** candidates — transient state that will be stale within a turn:
  the current cursor position, "I'm now editing X", scratchpad noise.

The crucial contract: the guard must **actively reject** by returning
``{"handled": True, "action": "reject", ...}``. It must *not* signal "passthrough"
by returning ``None``. Returning ``None`` lets the write proceed unchanged; that
is the failure mode this library exists to prevent. Every code path that
evaluates a candidate ends in an explicit verdict.

Return shape (matches Hermes' ``prepare_memory_write`` hook contract from
PR #59898):

.. code-block:: python

    {
        "handled": True,          # the hook took ownership of this write
        "action": "allow" | "reject",
        "reason": str,            # why, for the audit log
        # only present on "allow": optional enrichment forwarded to remember()
        "kind": str | None,
        "wing": str | None,
        "room": str | None,
    }
"""

from __future__ import annotations

import re
from typing import Any, Literal

# ---------------------------------------------------------------------------
# Ephemeral / ambiguous detection.
#
# These patterns were lifted from the zutfen_memory plugin's routing rules and
# tightened. They intentionally err on the side of rejecting: a rejected
# candidate can be re-extracted on the next turn; a wrongly-persisted memory
# pollutes recall until a human reviews it. design.md §4 makes the same tradeoff
# for chatty low-trust sources.
# ---------------------------------------------------------------------------

# Cursor / selection / "now editing" state — will be stale within one turn.
_EPHEMERAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bcurrently (editing|viewing|on|in)\b", re.IGNORECASE),
    re.compile(r"\bcursor (is )?(at|on|in)\b", re.IGNORECASE),
    re.compile(r"\b(selected|highlighted|focus(ed)?) (line|lines|text|block)\b", re.IGNORECASE),
    re.compile(r"\bscroll(ed|ing)? (to|at|near)\b", re.IGNORECASE),
    re.compile(r"\bI('?m| am) (now|currently) (typing|writing|working on)\b", re.IGNORECASE),
    re.compile(r"\bopen (file|tab|buffer|document):", re.IGNORECASE),
    re.compile(r"\b(undo|redo|paste|copied)\b", re.IGNORECASE),
)

# Ambiguous content — not a fact, just noise the model happened to emit.
_AMBIGUOUS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(let me|let's|perhaps|maybe|i (think|guess|wonder|suppose))\b", re.IGNORECASE),
    re.compile(r"^(hmm|uh|ok|okay|so|well|alright)[,!.]?\s*$", re.IGNORECASE),
    re.compile(
        r"\b(how do i|what (does|is)|can you|could you|help (me )?(with|on))\b",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*(//|#|/\*|--|<!--)", re.IGNORECASE),  # bare code comment, not a fact
)

# A candidate needs at least this many non-whitespace characters to carry a
# durable fact. Below this, even if no pattern matched, we reject as ambiguous.
_MIN_CONTENT_CHARS = 12

GuardAction = Literal["allow", "reject"]


class GuardVerdict(dict[str, Any]):
    """Typed verdict returned by :func:`prepare_memory_write_guard`.

    Subclassing ``dict`` keeps the Hermes hook contract (JSON-ish mapping with
    ``handled``/``action`` keys) while giving callers a concrete type to
    introspect. ``handled`` is always ``True``: this guard takes ownership of
    every candidate it inspects — it never passes through silently.
    """


def _reject(reason: str) -> GuardVerdict:
    """Build an active-rejection verdict.

    ``handled=True`` means Hermes must NOT proceed with the native write; the
    hook has taken responsibility for this candidate (here, by dropping it).
    """
    return GuardVerdict(handled=True, action="reject", reason=reason)


def _allow(reason: str, *, kind: str | None = None, wing: str | None = None,
           room: str | None = None) -> GuardVerdict:
    """Build an allow verdict, optionally enriching the write with taxonomy."""
    verdict: GuardVerdict = GuardVerdict(handled=True, action="allow", reason=reason)
    if kind is not None:
        verdict["kind"] = kind
    if wing is not None:
        verdict["wing"] = wing
    if room is not None:
        verdict["room"] = room
    return verdict


def _matches_any(text: str, patterns: tuple[re.Pattern[str], ...]) -> str | None:
    """Return the matched pattern's source if ``text`` matches any pattern."""
    for pat in patterns:
        if pat.search(text):
            return pat.pattern
    return None


def prepare_memory_write_guard(
    content: str,
    *,
    kind: str | None = None,
    wing: str | None = None,
    room: str | None = None,
) -> GuardVerdict:
    """Evaluate a write candidate and return an explicit verdict.

    This is the single chokepoint both lifecycle hooks and the compatibility
    shim call. It returns a :class:`GuardVerdict` with ``handled=True`` for
    every input — never ``None``.

    Parameters
    ----------
    content:
        The candidate memory text.
    kind, wing, room:
        Optional taxonomy already proposed (e.g. by Engram classify). Forwarded
        through on an allow verdict; ignored on reject.

    Returns
    -------
    GuardVerdict
        ``{"handled": True, "action": "allow"|"reject", "reason": str, ...}``.
    """
    if content is None:
        return _reject("content is None")
    text = content.strip()
    if not text:
        return _reject("empty content")

    # Ephemeral state checks first — these are the highest-value rejections
    # because they would otherwise create a memory that's stale by next turn.
    ephemeral_hit = _matches_any(text, _EPHEMERAL_PATTERNS)
    if ephemeral_hit is not None:
        return _reject(f"ephemeral state (matched /{ephemeral_hit}/) — stale within a turn")

    ambiguous_hit = _matches_any(text, _AMBIGUOUS_PATTERNS)
    if ambiguous_hit is not None:
        return _reject(f"ambiguous content (matched /{ambiguous_hit}/) — not a durable fact")

    # Length floor: even with no pattern hit, very short strings rarely encode a
    # fact worth persisting. Reject rather than risk recall pollution.
    non_ws = sum(1 for ch in text if not ch.isspace())
    if non_ws < _MIN_CONTENT_CHARS:
        return _reject(
            f"too short ({non_ws} non-whitespace chars) — no durable signal"
        )

    # Passed every check: allow the write, forwarding any taxonomy we were given.
    return _allow("passed write-boundary guard", kind=kind, wing=wing, room=room)


def is_allowed(verdict: dict[str, Any] | GuardVerdict | None) -> bool:
    """True iff ``verdict`` is a non-None allow verdict.

    Centralizing the check makes the "None means passthrough" failure mode
    impossible at call sites: callers go through this helper rather than
    hand-rolling ``verdict and verdict.get('action') == 'allow'``.
    """
    return bool(verdict is not None and verdict.get("handled") is True
                and verdict.get("action") == "allow")
