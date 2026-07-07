"""Classification engine for memory writes and /v1/classify."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any
from uuid import UUID

from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engram.config import settings
from engram.models import ClassificationRule, MemoryItem

_DEFAULT_KIND_TAXONOMY = (
    "fact",
    "preference",
    "doctrine",
    "decision",
    "invariant",
    "observation",
    "diary_entry",
)


class ClassificationResult(BaseModel):
    """Internal classification result with provenance for audit storage."""

    suggested_kind: str
    suggested_wing: str | None = None
    suggested_room: str | None = None
    suggested_visibility: str = "workspace"
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
    """Classify raw memory text using tenant rules, with optional LLM enrichment."""

    rules = await _load_rules(session, tenant_id)
    taxonomy, wings, rooms = await _load_vocab(session, tenant_id)

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


async def _load_rules(session: AsyncSession, tenant_id: UUID) -> list[ClassificationRule]:
    stmt = (
        select(ClassificationRule)
        .where(
            ClassificationRule.tenant_id == tenant_id,
            ClassificationRule.enabled.is_(True),
        )
        .order_by(ClassificationRule.priority.asc(), ClassificationRule.created_at.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def _load_vocab(
    session: AsyncSession,
    tenant_id: UUID,
) -> tuple[list[str], list[str], list[str]]:
    kind_rows = await session.execute(
        select(MemoryItem.kind)
        .where(MemoryItem.tenant_id == tenant_id, MemoryItem.kind.is_not(None))
        .distinct()
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

    rule_kind_rows = await session.execute(
        select(ClassificationRule.target_kind)
        .where(
            ClassificationRule.tenant_id == tenant_id,
            ClassificationRule.enabled.is_(True),
            ClassificationRule.target_kind.is_not(None),
        )
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

    kinds = _merge_vocab(
        kind_rows.scalars().all(),
        rule_kind_rows.scalars().all(),
        _DEFAULT_KIND_TAXONOMY,
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
    rules: list[ClassificationRule],
    taxonomy: list[str],
) -> ClassificationResult:
    matched_rules: list[str] = []
    matched_target_rules: list[ClassificationRule] = []
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
    rules: list[ClassificationRule],
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
    rules: list[ClassificationRule],
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
            "confidence": "number 0.70-0.95",
            "reason": "str",
            "rules_matched": "list[str]",
        },
        "constraints": [
            "Return valid JSON only.",
            "Use only kinds from the taxonomy; otherwise choose fact.",
            "Use wing/room values from the provided vocabulary when possible.",
            "If uncertain, prefer fact and a lower confidence.",
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


async def _call_openai_classification(prompt: str) -> dict[str, Any]:
    client = (
        AsyncOpenAI()
        if settings.openai_api_key is None
        else AsyncOpenAI(api_key=settings.openai_api_key)
    )
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

    try:
        confidence = float(payload.get("confidence", 0.7))
    except (TypeError, ValueError):
        confidence = 0.7
    confidence = max(0.7, min(0.95, confidence))

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
        confidence = max(0.7, confidence)

    return ClassificationResult(
        suggested_kind=kind,
        suggested_wing=wing,
        suggested_room=room,
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


def _as_iterable(value: Any) -> Iterable[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return value
    return [value]
