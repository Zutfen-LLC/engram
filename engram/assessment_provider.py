"""Bounded taxonomy and retention inference. Models do not attest factual support."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Literal

from openai import AsyncOpenAI
from pydantic import Field

from engram.assessment_schema import StrictModel
from engram.provider_clients import resolve_classification_provider
from engram.provider_observer import record_provider_invocation
from engram.safety import has_secrets

# engram.assess.2 — #214 contract correction.
#
# Materially different from `engram.assess.1`, which embedded the raw
# ProviderValues JSON schema in the system prompt and asked the model to
# "preserve unknown scores as null" and to treat the governed kind as
# immutable. The #213 diagnostic showed those instructions made the provider
# echo the schema skeleton instead of values (70/200 frozen inputs) and
# abstain on nearly every judgment (suggested_kind null on 125/130 evaluable
# cases). The prompt below states the four fields and their vocabularies in
# plain prose, defines when null/uncertain are and are not legitimate, and
# separates advisory suggested_kind from the governed kind. Strict
# ProviderValues parsing is unchanged; schema echoes and out-of-contract
# values still fail closed.
PROMPT_VERSION = "engram.assess.2"

PROMPT_IDENTITY_FIELDS = frozenset(
    {"suggested_kind", "taxonomy_value", "retention_value", "retention_disposition"}
)

_SYSTEM_PROMPT = (
    "You are a memory-service annotation function. Your response is VALUES for "
    "one memory record, not a schema description: reply with one JSON object "
    "and nothing else.\n"
    "Give your four advisory judgments about the memory content provided by "
    "the user message:\n"
    "1. suggested_kind: the single best-fit kind for this content, chosen "
    "from: fact, observation, preference, procedure, decision, doctrine, "
    "invariant, summary, unknown. This is an independent classification of "
    "the content; it does not change and is not constrained by the governed "
    "kind supplied alongside the content. Use null only when the content is "
    "too fragmentary to classify at all.\n"
    "2. taxonomy_value: your confidence in that classification, a number "
    "from 0 to 0.95. Give a number whenever you give a suggested_kind other "
    "than null; give null only when no responsible classification is "
    "possible.\n"
    "3. retention_disposition: how durably useful this content is to keep, "
    "one of: retain (reference value later), transient (useful short-term "
    "only), noise (no value), uncertain. Judge durable usefulness only.\n"
    "4. retention_value: your confidence in that retention judgment, a "
    "number from 0 to 0.95. Give a number whenever retention_disposition is "
    "not uncertain; give null only when no responsible usefulness judgment "
    "is possible.\n"
    "These judgments are about what the content IS and whether keeping it "
    "helps later. They are not about whether the content is factually true. "
    "Uncertainty about truth, evidence, or authority must NOT push you "
    "toward null or uncertain answers: ordinary statements, instructions, "
    "decisions, and preferences are still fully classifiable. Reserve null "
    "and uncertain for content that is genuinely indeterminate (fragments, "
    "probe markers, text with no durable-usefulness signal).\n"
    "Reply with a JSON object with exactly these keys: suggested_kind, "
    "taxonomy_value, retention_value, retention_disposition."
)


class ProviderValues(StrictModel):
    suggested_kind: str | None = Field(default=None, max_length=64)
    taxonomy_value: float | None = Field(default=None, ge=0, le=0.95)
    retention_value: float | None = Field(default=None, ge=0, le=0.95)
    retention_disposition: Literal["retain", "transient", "noise", "uncertain"] = "uncertain"


def is_schema_echo(message: str) -> bool:
    """Detect a schema-skeleton echo deterministically from the raw bytes.

    The #213 diagnostic showed a content-driven failure mode where the
    provider replies with the JSON-schema skeleton instead of values. Such
    output is characterized by schema-vocabulary keys at the top level of
    the decoded object and carries no assessment signal; classify it as a
    contract defect rather than a transient failure so callers can stop
    wasting identical retries on it.
    """
    try:
        decoded = json.loads(message)
    except ValueError:
        return False
    if not isinstance(decoded, dict):
        return False
    schema_vocabulary = {
        "properties",
        "$defs",
        "$schema",
        "required",
        "items",
    }
    top_level = set(decoded)
    if top_level & schema_vocabulary:
        return True
    # {"type": "object"}-style skeletons put "type" at the top level; the
    # value contract never asks for or emits a "type" key.
    if "type" in top_level and not top_level & PROMPT_IDENTITY_FIELDS:
        return True
    # A payload with none of the four value keys carries no assessment
    # signal; that includes "additionalProperties"-only skeletons and any
    # other non-contract object.
    return not top_level & PROMPT_IDENTITY_FIELDS


class SchemaEchoError(ValueError):
    """Deterministic schema-skeleton output; retries cannot clear it."""


@dataclass(frozen=True)
class ProviderAssessment:
    values: ProviderValues
    model: str | None


async def assess_content(content: str, kind: str) -> ProviderAssessment:
    """Make one bounded call. Store only validated numeric and taxonomy outputs."""
    record_provider_invocation("assessment")
    if len(content.encode()) > 16000 or has_secrets(content):
        raise ValueError("assessment input rejected")
    config = resolve_classification_provider()
    async with AsyncOpenAI(
        api_key=config.api_key, base_url=config.base_url, timeout=30, max_retries=0
    ) as client:
        async with asyncio.timeout(35):
            response = await client.chat.completions.create(
                model=config.model,
                temperature=0,
                max_tokens=1024,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": f"{PROMPT_VERSION}\n{_SYSTEM_PROMPT}",
                    },
                    {"role": "user", "content": json.dumps({"content": content, "kind": kind})},
                ],
            )
    message = response.choices[0].message.content or ""
    if (
        response.choices[0].finish_reason != "stop"
        or len(message.encode()) > 8192
        or has_secrets(message)
    ):
        raise ValueError("assessment output rejected")
    if is_schema_echo(message):
        # Content-driven prompt-contract failure: identical retries reproduce
        # it. Raise a distinct, non-transient error instead of parsing.
        raise SchemaEchoError("provider echoed an output schema instead of values")
    return ProviderAssessment(ProviderValues.model_validate_json(message), response.model)
