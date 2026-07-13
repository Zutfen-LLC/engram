"""Classification engine for memory writes and /v1/classify."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engram.config import settings
from engram.memory_kinds import DEFAULT_KIND_TAXONOMY as _DEFAULT_KIND_TAXONOMY
from engram.memory_kinds import get_enabled_memory_kinds
from engram.models import ClassificationRule, MemoryItem


class ClassificationResult(BaseModel):
    """Internal classification result with provenance for audit storage."""

    suggested_kind: str
    suggested_wing: str | None = None
    suggested_room: str | None = None
    # Advisory only. ``/v1/remember`` applies this downward (never widens);
    # ``/v1/classify`` returns it as a suggestion. ``None`` means "no suggestion"
    # and the caller's requested visibility is preserved.
    suggested_visibility: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    rules_matched: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)


async def classify(
    content: str,
    tenant_id: UUID,
    session: AsyncSession,
    context: str | None = None,
) -> ClassificationResult:
    """Classify raw memory text using tenant rules, with optional LLM enrichment.

    Used by ``/v1/classify`` and the async ``classification.refine`` worker. The
    synchronous ``/v1/remember`` write path uses :func:`classify_rules_only` so
    the OpenAI call never blocks the request (ENG-AUD-008 / F20).
    """

    rules = await _load_rules_cached(session, tenant_id)
    taxonomy, wings, rooms = await _load_vocab_cached(session, tenant_id)

    rule_result = _classify_rules(content, rules, taxonomy)
    if settings.classification_provider != "openai":
        return rule_result

    prompt = _build_prompt(
        content=content,
        context=context,
        taxonomy=taxonomy,
        wings=wings,
        rooms=rooms,
        rules=rules,
        rule_result=rule_result,
    )

    try:
        llm_payload = await _call_openai_classification(prompt)
    except Exception as exc:  # pragma: no cover - defensive fallback
        fallback = dict(rule_result.provenance)
        fallback.update({"provider": "openai", "mode": "fallback", "error": str(exc)})
        return rule_result.model_copy(update={"provenance": fallback})

    return _apply_llm_payload(
        llm_payload,
        taxonomy=taxonomy,
        wings=wings,
        rooms=rooms,
        rule_result=rule_result,
    )


async def classify_rules_only(
    content: str,
    tenant_id: UUID,
    session: AsyncSession,
    context: str | None = None,
) -> ClassificationResult:
    """Synchronous (request-path) classification: rules only, never OpenAI.

    This is what ``/v1/remember`` calls. It runs the deterministic rule pass
    using the cached tenant vocab, and never makes a provider call — the LLM
    refinement happens later via an async ``classification.refine`` job
    (ENG-AUD-008 / F20). ``context`` is accepted for API parity but is unused
    by the rule pass.
    """
    rules = await _load_rules_cached(session, tenant_id)
    taxonomy, _wings, _rooms = await _load_vocab_cached(session, tenant_id)
    return _classify_rules(content, rules, taxonomy)


# ---------------------------------------------------------------------------
# Per-tenant in-process TTL cache for vocab + rules (ENG-AUD-008 / F20).
#
# F20: each unclassified ``remember`` previously ran six DISTINCT scans over
# memory_items + classification_rules for vocabulary, on every call. This cache
# serves that vocab at most once per TTL window per tenant. Keyed by tenant_id
# so cache entries never cross tenants. Bounded LRU eviction guards memory.
# TTL-based invalidation is sufficient for this slice; explicit invalidation on
# classification-rule writes is not required (no rule-write endpoint exists
# today) but :func:`invalidate_vocab_cache` is provided for tests + future use.
# ---------------------------------------------------------------------------


class _CacheEntry:
    __slots__ = ("value", "fetched_at")

    def __init__(self, value: object, fetched_at: float) -> None:
        self.value = value
        self.fetched_at = fetched_at


# OrderedDict gives O(1) LRU move-to-end on access.
_vocab_cache: OrderedDict[str, _CacheEntry] = OrderedDict()
_rules_cache: OrderedDict[str, _CacheEntry] = OrderedDict()
_cache_lock: asyncio.Lock = asyncio.Lock()


def _cache_expired(fetched_at: float) -> bool:
    ttl = settings.vocab_cache_ttl_seconds
    if ttl <= 0:  # 0 disables caching
        return True
    return (time.monotonic() - fetched_at) > ttl


def _cache_get(
    cache: OrderedDict[str, _CacheEntry], tenant_id: UUID
) -> object | None:
    key = str(tenant_id)
    entry = cache.get(key)
    if entry is None:
        return None
    if _cache_expired(entry.fetched_at):
        cache.pop(key, None)
        return None
    cache.move_to_end(key)  # LRU
    return entry.value


def _cache_put(
    cache: OrderedDict[str, _CacheEntry], tenant_id: UUID, value: object
) -> None:
    key = str(tenant_id)
    max_tenants = settings.vocab_cache_max_tenants
    cache[key] = _CacheEntry(value, time.monotonic())
    cache.move_to_end(key)
    while len(cache) > max_tenants:
        cache.popitem(last=False)  # evict oldest


def invalidate_vocab_cache(tenant_id: UUID | str | None = None) -> None:
    """Drop cached vocab/rules for a tenant (or all tenants when ``None``).

    Safe to call from tests to force a reload. A future classification-rule
    write endpoint would call this with the affected tenant id.
    """
    if tenant_id is None:
        _vocab_cache.clear()
        _rules_cache.clear()
    else:
        key = str(tenant_id)
        _vocab_cache.pop(key, None)
        _rules_cache.pop(key, None)


async def _load_rules_cached(
    session: AsyncSession, tenant_id: UUID
) -> list[RuleSnapshot]:
    cached = _cache_get(_rules_cache, tenant_id)
    if cached is not None:
        return cast(list[RuleSnapshot], cached)
    async with _cache_lock:
        # Re-check under the lock to avoid duplicate loads from racing callers.
        cached = _cache_get(_rules_cache, tenant_id)
        if cached is not None:
            return cast(list[RuleSnapshot], cached)
        rules = await _load_rules(session, tenant_id)
        _cache_put(_rules_cache, tenant_id, rules)
        return rules


async def _load_vocab_cached(
    session: AsyncSession, tenant_id: UUID
) -> tuple[list[str], list[str], list[str]]:
    cached = _cache_get(_vocab_cache, tenant_id)
    if cached is not None:
        return cast(tuple[list[str], list[str], list[str]], cached)
    async with _cache_lock:
        cached = _cache_get(_vocab_cache, tenant_id)
        if cached is not None:
            return cast(tuple[list[str], list[str], list[str]], cached)
        vocab = await _load_vocab(session, tenant_id)
        _cache_put(_vocab_cache, tenant_id, vocab)
        return vocab


@dataclass(frozen=True)
class RuleSnapshot:
    """Session-independent classification rule safe for the process cache."""

    name: str
    rule_type: str
    pattern: str
    target_kind: str | None
    target_wing: str | None
    target_room: str | None
    priority: int


async def _load_rules(session: AsyncSession, tenant_id: UUID) -> list[RuleSnapshot]:
    stmt = (
        select(ClassificationRule)
        .where(
            ClassificationRule.tenant_id == tenant_id,
            ClassificationRule.enabled.is_(True),
        )
        .order_by(ClassificationRule.priority.asc(), ClassificationRule.created_at.asc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        RuleSnapshot(
            name=row.name,
            rule_type=row.rule_type,
            pattern=row.pattern,
            target_kind=row.target_kind,
            target_wing=row.target_wing,
            target_room=row.target_room,
            priority=row.priority,
        )
        for row in rows
    ]


async def _load_vocab(
    session: AsyncSession,
    tenant_id: UUID,
) -> tuple[list[str], list[str], list[str]]:
    # Kind taxonomy is the governed registry (ENG-AUD-010 / F17), not a DISTINCT
    # scan over memory_items.kind or classification_rules.target_kind — those
    # would let stale/typo'd values leak into the classifier's allowed output.
    # _DEFAULT_KIND_TAXONOMY is a defensive fallback only, for the pathological
    # case of a tenant with zero enabled registry rows.
    registry_kinds = await get_enabled_memory_kinds(session, tenant_id)
    kinds = sorted({k.name for k in registry_kinds}) if registry_kinds else list(
        _DEFAULT_KIND_TAXONOMY
    )

    wing_rows = await session.execute(
        select(MemoryItem.wing)
        .where(MemoryItem.tenant_id == tenant_id, MemoryItem.wing.is_not(None))
        .distinct()
    )
    room_rows = await session.execute(
        select(MemoryItem.room)
        .where(MemoryItem.tenant_id == tenant_id, MemoryItem.room.is_not(None))
        .distinct()
    )

    rule_wing_rows = await session.execute(
        select(ClassificationRule.target_wing)
        .where(
            ClassificationRule.tenant_id == tenant_id,
            ClassificationRule.enabled.is_(True),
            ClassificationRule.target_wing.is_not(None),
        )
        .distinct()
    )
    rule_room_rows = await session.execute(
        select(ClassificationRule.target_room)
        .where(
            ClassificationRule.tenant_id == tenant_id,
            ClassificationRule.enabled.is_(True),
            ClassificationRule.target_room.is_not(None),
        )
        .distinct()
    )

    wings = _merge_vocab(wing_rows.scalars().all(), rule_wing_rows.scalars().all())
    rooms = _merge_vocab(room_rows.scalars().all(), rule_room_rows.scalars().all())
    return kinds, wings, rooms


def _merge_vocab(*groups: Iterable[Any]) -> list[str]:
    values: list[str] = []
    for group in groups:
        for candidate in group:
            if candidate is None:
                continue
            text = str(candidate).strip()
            if text and text not in values:
                values.append(text)
    return sorted(values)


def _classify_rules(
    content: str,
    rules: list[RuleSnapshot],
    taxonomy: list[str],
) -> ClassificationResult:
    matched_rules: list[str] = []
    matched_target_rules: list[RuleSnapshot] = []
    skip_rules: list[str] = []

    for rule in rules:
        try:
            matched = re.search(rule.pattern, content, flags=re.IGNORECASE | re.MULTILINE)
        except re.error:
            continue
        if not matched:
            continue
        matched_rules.append(rule.name)
        if rule.rule_type == "regex_skip":
            skip_rules.append(rule.name)
            continue
        matched_target_rules.append(rule)

    provenance: dict[str, Any] = {
        "provider": settings.classification_provider or "none",
        "mode": "rule",
        "matched_rules": matched_rules,
    }

    if skip_rules:
        reason = f"Matched skip rule(s): {', '.join(skip_rules)}. Conservative fact default."
        return ClassificationResult(
            suggested_kind="fact",
            suggested_wing=None,
            suggested_room=None,
            confidence=0.6,
            reason=reason,
            rules_matched=matched_rules,
            provenance=provenance,
        )

    kind = _pick_field(matched_target_rules, "target_kind", taxonomy)
    wing = _pick_field(matched_target_rules, "target_wing")
    room = _pick_field(matched_target_rules, "target_room")

    fields_set = sum(value is not None for value in (kind, wing, room))
    confidence = min(0.8, 0.6 + (0.07 * fields_set))

    if kind is None and wing is None and room is None:
        reason = "No rule matched. Conservative fact default."
        return ClassificationResult(
            suggested_kind="fact",
            suggested_wing=None,
            suggested_room=None,
            confidence=0.6,
            reason=reason,
            rules_matched=matched_rules,
            provenance=provenance,
        )

    reason_parts = []
    if kind is not None:
        reason_parts.append(f"kind={kind}")
    if wing is not None:
        reason_parts.append(f"wing={wing}")
    if room is not None:
        reason_parts.append(f"room={room}")
    reason = f"Matched {', '.join(matched_rules)}; chose {', '.join(reason_parts)}."
    provenance["selected_fields"] = reason_parts
    return ClassificationResult(
        suggested_kind=kind or "fact",
        suggested_wing=wing,
        suggested_room=room,
        confidence=confidence,
        reason=reason,
        rules_matched=matched_rules,
        provenance=provenance,
    )


def _pick_field(
    rules: list[RuleSnapshot],
    field_name: str,
    taxonomy: list[str] | None = None,
) -> str | None:
    for rule in rules:
        value = getattr(rule, field_name)
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        if taxonomy is not None and text not in taxonomy:
            continue
        return text
    return None


def _build_prompt(
    *,
    content: str,
    context: str | None,
    taxonomy: list[str],
    wings: list[str],
    rooms: list[str],
    rules: list[RuleSnapshot],
    rule_result: ClassificationResult,
) -> str:
    rules_payload = [
        {
            "name": rule.name,
            "rule_type": rule.rule_type,
            "pattern": rule.pattern,
            "target_kind": rule.target_kind,
            "target_wing": rule.target_wing,
            "target_room": rule.target_room,
            "priority": rule.priority,
        }
        for rule in rules
    ]
    payload = {
        "task": (
            "Classify a memory item. Be conservative: if uncertain, choose kind='fact' "
            "with lower confidence rather than over-promoting."
        ),
        "taxonomy": {
            "kinds": taxonomy,
            "wings": wings,
            "rooms": rooms,
        },
        "rules": rules_payload,
        "rule_baseline": rule_result.model_dump(exclude={"provenance"}),
        "content": content,
        "context": context,
        "output_schema": {
            "suggested_kind": "str",
            "suggested_wing": "str|null",
            "suggested_room": "str|null",
            "suggested_visibility": "one of private|workspace|tenant|public, or null",
            "confidence": "number 0.0-0.95 (low values are allowed and meaningful)",
            "reason": "str",
            "rules_matched": "list[str]",
        },
        "constraints": [
            "Return valid JSON only.",
            "Use only kinds from the taxonomy; otherwise choose fact.",
            "Use wing/room values from the provided vocabulary when possible.",
            "suggested_visibility is advisory; narrow it when the content looks "
            "sensitive/personal, or null when you have no opinion.",
            "Confidence may be any value in 0.0-0.95. Low confidence is a real "
            "signal — express doubt rather than flooring it.",
            "If uncertain, prefer fact and a lower confidence.",
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


async def _call_openai_classification(prompt: str) -> dict[str, Any]:
    client_kwargs: dict[str, Any] = {"api_key": settings.openai_api_key}
    if settings.openai_base_url:
        client_kwargs["base_url"] = settings.openai_base_url
    client = AsyncOpenAI(**client_kwargs)
    response = await client.chat.completions.create(
        model=settings.classification_model,
        messages=[
            {"role": "system", "content": "Return only valid JSON."},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
    )
    message = response.choices[0].message.content or "{}"
    payload = json.loads(message)
    if not isinstance(payload, dict):
        raise ValueError("classification response was not a JSON object")
    return payload


def _apply_llm_payload(
    payload: dict[str, Any],
    *,
    taxonomy: list[str],
    wings: list[str],
    rooms: list[str],
    rule_result: ClassificationResult,
) -> ClassificationResult:
    raw_kind = str(payload.get("suggested_kind") or payload.get("kind") or "fact").strip()
    kind = raw_kind if raw_kind in taxonomy else "fact"

    raw_wing = payload.get("suggested_wing") or payload.get("wing")
    raw_room = payload.get("suggested_room") or payload.get("room")
    wing = _normalize_vocab_value(raw_wing, wings)
    room = _normalize_vocab_value(raw_room, rooms)

    # Confidence is a real 0.0-0.95 signal: it is no longer floored to 0.7, so a
    # doubtful classifier result (e.g. 0.35) survives and can lower the stored
    # memory_confidence downstream. Missing/unparseable confidence defaults to a
    # neutral 0.5 rather than the old 0.7 floor.
    raw_confidence: float | None
    try:
        raw_confidence = float(payload.get("confidence", 0.5))
    except (TypeError, ValueError):
        raw_confidence = 0.5
    confidence = _clamp(raw_confidence, 0.0, 0.95)

    # Suggested visibility is advisory; validated against the enum. Invalid /
    # unknown values are dropped to None so the caller preserves the requested
    # visibility rather than trusting a malformed suggestion.
    suggested_visibility = _normalize_visibility(payload.get("suggested_visibility"))

    rules_matched = _merge_vocab(
        rule_result.rules_matched, _as_iterable(payload.get("rules_matched"))
    )
    reason = str(payload.get("reason") or rule_result.reason).strip()

    provenance: dict[str, Any] = dict(rule_result.provenance)
    provenance.update(
        {
            "provider": "openai",
            "mode": "llm",
            "model": settings.classification_model,
            "threshold": settings.classification_confidence_threshold,
            "llm_payload": {
                "suggested_kind": kind,
                "suggested_wing": wing,
                "suggested_room": room,
                "suggested_visibility": suggested_visibility,
                "raw_confidence": raw_confidence,
                "confidence": confidence,
                "rules_matched": rules_matched,
            },
        }
    )

    if confidence < settings.classification_confidence_threshold:
        provenance["mode"] = "llm_fallback"
        reason = (
            f"LLM confidence {confidence:.2f} below threshold "
            f"{settings.classification_confidence_threshold:.2f}; conservative fact default."
        )
        kind = "fact"
        wing = rule_result.suggested_wing or wing
        room = rule_result.suggested_room or room
        # Do NOT re-floor confidence above the threshold. The whole point of
        # removing the 0.7 floor is that a doubtful result stays doubtful, so the
        # downstream confidence blend can lower memory_confidence appropriately.

    return ClassificationResult(
        suggested_kind=kind,
        suggested_wing=wing,
        suggested_room=room,
        suggested_visibility=suggested_visibility,
        confidence=confidence,
        reason=reason,
        rules_matched=rules_matched,
        provenance=provenance,
    )


def _normalize_vocab_value(value: Any, vocab: list[str]) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text if text in vocab else None


_VALID_VISIBILITIES = {"private", "workspace", "tenant", "public"}


def _normalize_visibility(value: Any) -> str | None:
    """Coerce an LLM-provided visibility into a valid enum value or None."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text or text == "null":
        return None
    return text if text in _VALID_VISIBILITIES else None


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp ``value`` to ``[lo, hi]``. ``lo``/``hi`` are assumed ordered."""
    return max(lo, min(hi, value))


def _as_iterable(value: Any) -> Iterable[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return value
    return [value]
