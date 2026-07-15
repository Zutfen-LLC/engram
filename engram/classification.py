"""Classification engine for memory writes and /v1/classify."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal, cast
from uuid import UUID

from openai import AsyncOpenAI
from pydantic import AliasChoices, BaseModel, Field, computed_field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engram.config import settings
from engram.memory_kinds import DEFAULT_KIND_TAXONOMY as _DEFAULT_KIND_TAXONOMY
from engram.memory_kinds import get_enabled_memory_kinds
from engram.models import ClassificationRule, MemoryItem
from engram.usage import (
    Timer,
    extract_openai_compatible_usage,
    record_provider_call,
    safe_provider_identity,
    utf8_byte_len,
)

RetentionDisposition = Literal["retain", "transient", "noise", "uncertain"]


class ClassificationResult(BaseModel):
    """Internal classification result with provenance for audit storage."""

    suggested_kind: str
    suggested_wing: str | None = None
    suggested_room: str | None = None
    # Advisory only. ``/v1/remember`` applies this downward (never widens);
    # ``/v1/classify`` returns it as a suggestion. ``None`` means "no suggestion"
    # and the caller's requested visibility is preserved.
    suggested_visibility: str | None = None
    taxonomy_confidence: float = Field(
        ge=0.0,
        le=0.95,
        validation_alias=AliasChoices("taxonomy_confidence", "confidence"),
    )
    retention_confidence: float = Field(default=0.0, ge=0.0, le=0.95)
    retention_disposition: RetentionDisposition = "uncertain"
    reason: str
    rules_matched: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def confidence(self) -> float:
        """Deprecated compatibility alias for taxonomy confidence."""
        return self.taxonomy_confidence


async def classify(
    content: str,
    tenant_id: UUID,
    session: AsyncSession,
    context: str | None = None,
    *,
    principal_id: UUID | None = None,
    workspace_id: UUID | None = None,
    correlation_id: UUID | None = None,
    source_type: str | None = None,
    job_id: UUID | None = None,
) -> ClassificationResult:
    """Classify raw memory text using tenant rules, with optional LLM enrichment.

    Used by ``/v1/classify`` and the async ``classification.refine`` worker. The
    synchronous ``/v1/remember`` write path uses :func:`classify_rules_only` so
    the OpenAI call never blocks the request (ENG-AUD-008 / F20).

    ``principal_id``/``workspace_id``/``correlation_id``/``source_type``/
    ``job_id`` are optional usage-telemetry context (ENG-METER-001) — passing
    them tags the resulting ``provider.call`` event; omitting them still
    classifies correctly, just without that context attached.
    """

    rules = await _load_rules_cached(session, tenant_id)
    taxonomy, wings, rooms = await _load_vocab_cached(session, tenant_id)

    rule_result = _classify_rules(content, rules, taxonomy)
    if settings.classification_provider != "openai":
        await record_provider_call(
            tenant_id=tenant_id,
            principal_id=principal_id,
            workspace_id=workspace_id,
            operation="classification",
            status="disabled",
            provider_adapter=settings.classification_provider or "none",
            model=settings.classification_model,
            input_count=1,
            input_bytes=utf8_byte_len(content),
            correlation_id=correlation_id,
            job_id=job_id,
            metadata={"source_type": source_type} if source_type else None,
        )
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
        llm_payload = await _call_openai_classification(
            prompt,
            tenant_id=tenant_id,
            principal_id=principal_id,
            workspace_id=workspace_id,
            correlation_id=correlation_id,
            source_type=source_type,
            job_id=job_id,
        )
    except Exception as exc:  # pragma: no cover - defensive fallback
        fallback = dict(rule_result.provenance)
        fallback.update(
            {"provider": "openai", "mode": "fallback", "error_type": type(exc).__name__}
        )
        return rule_result.model_copy(
            update={
                "retention_confidence": 0.0,
                "retention_disposition": "uncertain",
                "provenance": fallback,
            }
        )

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


# Built-in default classification rules. These run before tenant-configured
# rules and provide a baseline for high-value patterns that are otherwise hard
# for the LLM to distinguish (especially invariant vs doctrine). Each entry
# is (name, regex, target_kind). The patterns are intentionally specific to
# avoid false positives — when in doubt the LLM enrichment handles it.
_BUILTIN_RULES: tuple[RuleSnapshot, ...] = (
    # Invariant: "must never", "never store/share/expose", high-stakes prohibition
    RuleSnapshot(
        name="builtin_invariant_must_never",
        rule_type="regex_target",
        pattern=r"\b(?:must never|never store|never share|never expose|never log|never commit)\b",
        target_kind="invariant",
        target_wing=None,
        target_room=None,
        priority=1,
    ),
    RuleSnapshot(
        name="builtin_invariant_violations",
        rule_type="regex_target",
        pattern=r"\bviolations are (?:high-stakes|catastrophic)\b",
        target_kind="invariant",
        target_wing=None,
        target_room=None,
        priority=1,
    ),
    # Doctrine: standing policies, architectural rules, "must", "always", "all X must"
    RuleSnapshot(
        name="builtin_doctrine_policy",
        rule_type="regex_target",
        pattern=r"\b(?:all \w+s? must|every \w+ must|must (?:always )?be|is a (?:core|strict) (?:architectural )?(?:constraint|requirement|rule|policy))\b",  # noqa: E501
        target_kind="doctrine",
        target_wing=None,
        target_room=None,
        priority=5,
    ),
    # Procedure: how-to, runbook, step-by-step instructions
    RuleSnapshot(
        name="builtin_procedure_steps",
        rule_type="regex_target",
        pattern=r"\b(?:to (?:deploy|install|upgrade|configure|set up|run)|step \d|^\d+\.\s|runbook|how-to)\b",  # noqa: E501
        target_kind="procedure",
        target_wing=None,
        target_room=None,
        priority=5,
    ),
    # Summary: condensed output from sessions/events
    RuleSnapshot(
        name="builtin_summary",
        rule_type="regex_target",
        pattern=r"\b(?:session summary|condensed summary|we (?:closed|shipped|completed|finished)|sprint summary|retro summary)\b",  # noqa: E501
        target_kind="summary",
        target_wing=None,
        target_room=None,
        priority=5,
    ),
    # Decision: explicit decision language
    RuleSnapshot(
        name="builtin_decision",
        rule_type="regex_target",
        pattern=r"\b(?:we (?:decided|chose|selected|opted|agreed)|(?:the )?decision (?:was|is) to|we (?:went with|will use|will adopt))\b",  # noqa: E501
        target_kind="decision",
        target_wing=None,
        target_room=None,
        priority=10,
    ),
    # Observation: tentative, unverified, "seemed", "maybe", "appeared to"
    RuleSnapshot(
        name="builtin_observation",
        rule_type="regex_target",
        pattern=r"\b(?:seemed to|appeared to|maybe|might be|not yet (?:verified|confirmed)|unclear whether)\b",  # noqa: E501
        target_kind="observation",
        target_wing=None,
        target_room=None,
        priority=10,
    ),
)


def _classify_rules(
    content: str,
    rules: list[RuleSnapshot],
    taxonomy: list[str],
) -> ClassificationResult:
    matched_rules: list[str] = []
    matched_target_rules: list[RuleSnapshot] = []
    skip_rules: list[str] = []

    # Tenant rules run first (take precedence), then built-in rules as a
    # fallback. This ensures tenant-configured rules (e.g. kind_doctrine with
    # an explicit "policy:" pattern) win over builtins that match the same
    # content (e.g. builtin_invariant_must_never matching "must never").
    # Both lists are already priority-sorted within their own group.
    builtin_active = [
        r for r in _BUILTIN_RULES
        if r.target_kind is None or r.target_kind in taxonomy
    ]
    all_rules = list(rules) + list(builtin_active)

    for rule in all_rules:
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
            taxonomy_confidence=0.6,
            retention_confidence=0.0,
            retention_disposition="noise",
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
            taxonomy_confidence=0.6,
            retention_confidence=0.0,
            retention_disposition="uncertain",
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
        taxonomy_confidence=min(confidence, 0.95),
        retention_confidence=0.0,
        retention_disposition="uncertain",
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
            "with lower taxonomy confidence rather than over-promoting. "
            "Estimate how strongly this candidate deserves durable memory. It must be an "
            "atomic, faithful representation of the supplied context, remain useful beyond "
            "the current turn or command, and not merely report transient status, tool "
            "chatter, or an uncommitted possibility. Do not assess whether it is externally "
            "true beyond the supplied context. Taxonomy confidence and retention "
            "confidence are independent. Retention confidence measures only the positive "
            "case for durable retention; confidence that text is noise must not increase it."
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
            "taxonomy_confidence": "number 0.0-0.95",
            "retention_confidence": "number 0.0-0.95; positive case for durable retention",
            "retention_disposition": "one of retain|transient|noise|uncertain",
            "reason": "str",
            "rules_matched": "list[str]",
        },
        "retention_dispositions": {
            "retain": (
                "The candidate is an atomic, faithful representation of information that "
                "should remain useful beyond the current command or working moment and "
                "deserves durable memory."
            ),
            "transient": (
                "The candidate may be useful during the current working period or session "
                "but is unlikely to remain useful as durable memory."
            ),
            "noise": (
                "The candidate is acknowledgement text, status chatter, repeated tool "
                "output, routine CI/process narration, or other content that should not "
                "become memory."
            ),
            "uncertain": (
                "There is insufficient evidence to decide whether the candidate deserves "
                "durable retention, or the output cannot safely make that judgment."
            ),
        },
        "constraints": [
            "Return valid JSON only.",
            "Use only kinds from the taxonomy; otherwise choose fact.",
            "Use wing/room values from the provided vocabulary when possible.",
            "suggested_visibility is advisory; narrow it when the content looks "
            "sensitive/personal, or null when you have no opinion.",
            "Taxonomy confidence may be any value in 0.0-0.95. Low confidence is a real "
            "signal — express doubt rather than flooring it.",
            "If uncertain, prefer fact and a lower confidence.",
            "Retention confidence measures the positive case for durable retention.",
            "High confidence that text is noise must not produce high retention confidence; "
            "noise should normally have low retention confidence.",
            "Retention confidence does not assess external truth beyond supplied content "
            "and context.",
            "Taxonomy confidence and retention confidence are independent.",
            "Kind definitions:",
            "  invariant: non-negotiable safety or integrity prohibition — "
            "violations cause data loss, security breaches, or corruption. "
            "e.g. 'never store secrets', 'must never bypass RLS'.",
            "  doctrine: standing policy or architectural decision the team "
            "agreed to follow — important but violations are process errors, "
            "not catastrophes. e.g. 'all PRs must pass CI', 'append-first "
            "content model'.",
            "  fact: a factual statement about how the system works, what "
            "something is, or where something is. e.g. 'the database is at "
            "10.0.0.40', 'agent writes default to proposed at 0.6 trust'.",
            "  preference: a user's personal likes/dislikes or working style.",
            "  decision: a deliberate choice between alternatives.",
            "  procedure: step-by-step how-to instructions.",
            "  observation: a tentative, unverified, or time-bound note.",
            "  summary: a condensed recap of a session, sprint, or event.",
            "When the content describes how the system works (even if it uses "
            "words like 'must' or 'default'), prefer fact over doctrine unless "
            "it states a policy the team chose to enforce.",
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def _classification_failure_meta(
    source_type: str | None, failure_stage: str
) -> dict[str, Any]:
    """Metadata for a failed classification/conflict provider call.

    The application always falls back to the rule baseline when the LLM
    classification call fails, so ``application_fallback`` is recorded
    truthfully alongside a sanitized failure stage. ``source_type`` is a safe
    categorical dimension. Never carries content or raw error messages.
    """
    meta: dict[str, Any] = {"application_fallback": True, "failure_stage": failure_stage}
    if source_type is not None:
        meta["source_type"] = source_type
    return meta


async def _call_openai_classification(
    prompt: str,
    *,
    tenant_id: UUID | None = None,
    principal_id: UUID | None = None,
    workspace_id: UUID | None = None,
    correlation_id: UUID | None = None,
    source_type: str | None = None,
    job_id: UUID | None = None,
) -> dict[str, Any]:
    # Classification may use a different provider (e.g. DeepInfra) than
    # embeddings. Fall back to the shared openai_* settings for backward
    # compatibility.
    api_key = settings.classification_api_key or settings.openai_api_key
    base_url = settings.classification_base_url or settings.openai_base_url
    client_kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url
    client = AsyncOpenAI(**client_kwargs)

    adapter, host = safe_provider_identity("openai", base_url)
    timer = Timer()
    try:
        response = await client.chat.completions.create(
            model=settings.classification_model,
            messages=[
                {"role": "system", "content": "Return only valid JSON."},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
    except Exception:
        if tenant_id is not None:
            await record_provider_call(
                tenant_id=tenant_id,
                principal_id=principal_id,
                workspace_id=workspace_id,
                operation="classification",
                status="failed",
                provider_adapter=adapter,
                provider_host=host,
                model=settings.classification_model,
                input_count=1,
                input_bytes=utf8_byte_len(prompt),
                latency_ms=timer.elapsed_ms(),
                correlation_id=correlation_id,
                job_id=job_id,
                metadata=_classification_failure_meta(source_type, "provider_error"),
            )
        raise

    message = response.choices[0].message.content or "{}"
    usage = extract_openai_compatible_usage(response)
    try:
        payload = json.loads(message)
        if not isinstance(payload, dict):
            raise ValueError("classification response was not a JSON object")
    except (json.JSONDecodeError, ValueError) as parse_exc:
        # The provider delivered a response, but it was not usable JSON (or not
        # a JSON object). This is a real provider failure that previously
        # vanished: json.loads raised before the isinstance branch could record
        # it. Record it now, carrying any usage/cost the response DID carry.
        if tenant_id is not None:
            await record_provider_call(
                tenant_id=tenant_id,
                principal_id=principal_id,
                workspace_id=workspace_id,
                operation="classification",
                status="failed",
                provider_adapter=adapter,
                provider_host=host,
                model=settings.classification_model,
                input_count=1,
                input_bytes=utf8_byte_len(prompt),
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                reported_cost_usd=usage.reported_cost_usd,
                latency_ms=timer.elapsed_ms(),
                correlation_id=correlation_id,
                job_id=job_id,
                metadata=_classification_failure_meta(source_type, "response_parse"),
            )
        raise ValueError(
            "classification response was not valid JSON or not a JSON object"
        ) from parse_exc

    if tenant_id is not None:
        confidence = payload.get("taxonomy_confidence", payload.get("confidence"))
        meta = {
            k: v
            for k, v in {
                "mode": "llm",
                "confidence": confidence,
                "source_type": source_type,
            }.items()
            if v is not None
        }
        await record_provider_call(
            tenant_id=tenant_id,
            principal_id=principal_id,
            workspace_id=workspace_id,
            operation="classification",
            status="succeeded",
            provider_adapter=adapter,
            provider_host=host,
            model=settings.classification_model,
            input_count=1,
            input_bytes=utf8_byte_len(prompt),
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            reported_cost_usd=usage.reported_cost_usd,
            latency_ms=timer.elapsed_ms(),
            correlation_id=correlation_id,
            job_id=job_id,
            metadata=meta,
        )

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

    # Taxonomy confidence is independent from retention evidence. Accept the
    # legacy provider key during the output-schema transition.
    raw_confidence: float | None
    try:
        raw_confidence = float(
            payload.get("taxonomy_confidence", payload.get("confidence", 0.5))
        )
    except (TypeError, ValueError):
        raw_confidence = 0.5
    confidence = _clamp(raw_confidence, 0.0, 0.95)

    raw_retention = payload.get("retention_confidence")
    retention_valid = True
    try:
        if raw_retention is None:
            raise TypeError
        retention_confidence = _clamp(float(raw_retention), 0.0, 0.95)
    except (TypeError, ValueError):
        retention_valid = False
        retention_confidence = 0.0
    raw_disposition = payload.get("retention_disposition")
    disposition: RetentionDisposition
    if raw_disposition in {"retain", "transient", "noise", "uncertain"}:
        disposition = cast(RetentionDisposition, raw_disposition)
    else:
        disposition = "uncertain"
    if disposition == "retain" and not retention_valid:
        disposition = "uncertain"

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
                "raw_taxonomy_confidence": raw_confidence,
                "taxonomy_confidence": confidence,
                "confidence": confidence,
                "raw_retention_confidence": raw_retention,
                "retention_confidence": retention_confidence,
                "retention_disposition": disposition,
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
        # Do not re-floor confidence above the threshold. A doubtful taxonomy
        # result remains doubtful without changing overall memory confidence.

    # Rule precedence: when a priority-1 builtin rule matched a non-fact kind
    # (e.g. invariant) and the LLM disagreed, defer to the rule.  Priority-1
    # rules encode high-stakes patterns (safety prohibitions) that the LLM
    # systematically over-promotes to doctrine.  The LLM may only override a
    # priority-1 rule match when its own confidence is very high (>= 0.85).
    rule_kind = rule_result.suggested_kind
    if (
        rule_kind
        and rule_kind != "fact"
        and rule_kind != kind
        and rule_result.confidence >= 0.7
        and confidence < 0.85
    ):
        provenance["mode"] = "llm_rule_guarded"
        kind = rule_kind
        wing = rule_result.suggested_wing or wing
        room = rule_result.suggested_room or room
        reason = (
            f"LLM suggested {kind} but rule baseline matched {rule_kind} "
            f"(rules: {rule_result.rules_matched}); deferring to rule match."
        )

    return ClassificationResult(
        suggested_kind=kind,
        suggested_wing=wing,
        suggested_room=room,
        suggested_visibility=suggested_visibility,
        taxonomy_confidence=confidence,
        retention_confidence=retention_confidence,
        retention_disposition=disposition,
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
