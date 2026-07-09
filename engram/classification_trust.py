"""Write-path trust policy for classification output.

Pure functions — no DB or FastAPI dependencies. These decide how a
``ClassificationResult`` is allowed to influence stored trust when ``/v1/remember``
auto-classifies a memory:

* classifier ``confidence`` may **refine** ``memory_confidence``, but source
  authority caps how far a low-trust automated source can self-promote;
* classifier ``suggested_visibility`` may only **narrow** the requested
  visibility, never widen it.

Both functions are deliberately defensive: unknown/invalid inputs are ignored so
a misbehaving classifier can never widen visibility or blow up a write.
"""

from __future__ import annotations

# Visibility ordered narrowest → widest. ``narrow_visibility`` uses this so the
# classifier can only ever move the stored scope towards ``private``.
VISIBILITY_RANK: dict[str, int] = {
    "private": 0,
    "workspace": 1,
    "tenant": 2,
    "public": 3,
}

# Source types whose classification confidence should be felt strongly. These
# are the low-authority automated writers; a confident classifier is the main
# signal they have about whether a capture is worth keeping.
_AUTOMATED_SOURCES: frozenset[str] = frozenset(
    {"sync_turn", "pre_compress", "extraction"}
)

# Blend weights. ``_AUTOMATED_WEIGHT`` lets the classifier move confidence
# substantially for automated sources; ``_AUTHORITATIVE_WEIGHT`` keeps manual /
# import / migration writes stable so uncertain taxonomy classification can't
# aggressively downrate a high-authority memory.
_AUTOMATED_CLASSIFIER_WEIGHT = 0.5
_AUTHORITATIVE_CLASSIFIER_WEIGHT = 0.15


def narrow_visibility(requested: str, suggested: str | None) -> str:
    """Return the narrowest of ``requested`` and ``suggested``.

    The classifier may only narrow visibility, never widen it. ``None`` / unknown
    / invalid suggestions are ignored and the requested visibility is preserved
    unchanged. Equal scopes round-trip to the requested value.
    """
    if suggested is None:
        return requested
    req_rank = VISIBILITY_RANK.get(requested)
    sugg_rank = VISIBILITY_RANK.get(suggested)
    if req_rank is None:
        # Caller passed something outside the enum; preserve it verbatim rather
        # than silently rewriting. (DB CHECK will reject invalid values upstream.)
        return requested
    if sugg_rank is None:
        return requested
    return suggested if sugg_rank < req_rank else requested


def blend_memory_confidence(
    *,
    source_default_confidence: float,
    classifier_confidence: float,
    source_trust: float,
    source_type: str,
) -> tuple[float, bool]:
    """Blend classifier confidence into the source-default ``memory_confidence``.

    Policy:
    * Automated sources (``sync_turn`` / ``pre_compress`` / ``extraction``) feel
      the classifier strongly (0.5·default + 0.5·classifier).
    * Authoritative sources (``manual`` / ``import`` / ``migration``) are barely
      moved by uncertain taxonomy classification (0.85·default + 0.15·classifier),
      so a high-trust manual write is not severely downrated just because the
      classifier was unsure about kind.
    * The result is capped by source authority ``max(default, source_trust)`` so
      a low-trust automated source cannot self-promote past a safe ceiling, while
      a confident classifier can still pull a weak capture *down*.
    * Final value is clamped to ``[0.0, 1.0]``.

    Returns ``(memory_confidence, blended)`` where ``blended`` is True when the
    blend actually changed the value away from the source default.
    """
    classifier_weight = (
        _AUTOMATED_CLASSIFIER_WEIGHT
        if source_type in _AUTOMATED_SOURCES
        else _AUTHORITATIVE_CLASSIFIER_WEIGHT
    )
    default_weight = 1.0 - classifier_weight

    blended = (default_weight * source_default_confidence) + (
        classifier_weight * classifier_confidence
    )
    authority_cap = max(source_default_confidence, source_trust)
    result = min(blended, authority_cap)
    result = max(0.0, min(1.0, result))
    changed = abs(result - source_default_confidence) > 1e-9
    return result, changed
