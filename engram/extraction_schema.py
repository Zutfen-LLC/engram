"""Version 1 extraction wire contract. Attribution is evidence, not authority."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "engram.extraction.v1"
PROMPT_VERSION = "engram.extract.3"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExtractionMessage(StrictModel):
    message_id: str = Field(min_length=1, max_length=128)
    role: Literal["user", "assistant", "system", "tool", "unknown"] = "unknown"
    content: str = Field(min_length=1, max_length=16000)
    created_at: datetime | None = None
    tool_name: str | None = Field(default=None, max_length=128)
    source_uri: str | None = Field(default=None, max_length=256)


class ExtractRequest(StrictModel):
    messages: list[ExtractionMessage] = Field(min_length=1, max_length=64)
    source_type: Literal["sync_turn", "pre_compress", "session_end", "extraction"] = "extraction"
    workspace: str | None = Field(default=None, max_length=128)
    visibility: Literal["private", "workspace", "tenant", "public"] | None = None
    mode: Literal["preview", "write_proposed"] = "preview"
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_batch(self) -> ExtractRequest:
        if len({m.message_id for m in self.messages}) != len(self.messages):
            raise ValueError("message IDs must be unique")
        if len(self.model_dump_json().encode("utf-8")) > 65536:
            raise ValueError("extraction input exceeds 65536 bytes")
        if self.mode == "write_proposed" and self.idempotency_key is None:
            raise ValueError("write_proposed requires idempotency_key")
        return self


class EvidenceSpan(StrictModel):
    message_id: str = Field(min_length=1, max_length=128)
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class SourceCue(StrictModel):
    cue_type: Literal["temporal", "scope", "security", "qualification", "negation"]
    evidence: EvidenceSpan
    value: str = Field(default="", max_length=512)


class ExtractedProposition(StrictModel):
    content: str = Field(min_length=1, max_length=4000)
    suggested_kind: str = Field(min_length=1, max_length=64)
    suggested_wing: str | None = Field(default=None, max_length=128)
    suggested_room: str | None = Field(default=None, max_length=128)
    subject: str | None = Field(default=None, max_length=256)
    taxonomy_confidence: float = Field(ge=0, le=0.95)
    retention_confidence: float = Field(ge=0, le=0.95)
    retention_disposition: Literal["retain", "transient", "noise", "uncertain"]
    assertion_mode: Literal[
        "direct_statement",
        "tool_observation",
        "quoted_source",
        "derived_summary",
        "inference",
        "unknown",
    ]
    evidence: list[EvidenceSpan] = Field(min_length=1, max_length=16)
    source_cues: list[SourceCue] = Field(default_factory=list, max_length=16)
    reason_codes: list[
        Literal[
            "durable",
            "ephemeral",
            "uncertain",
            "no_memory",
            "duplicate",
            "unsafe",
            "unsupported",
        ]
    ] = Field(default_factory=list, max_length=8)


class ExtractorOutput(StrictModel):
    candidates: list[ExtractedProposition] = Field(default_factory=list, max_length=32)
    reason_codes: list[Literal["no_memory", "uncertain", "unsupported"]] = Field(
        default_factory=list,
        max_length=3,
    )


class EvidenceMessage(StrictModel):
    message_id: str
    role: Literal["user", "assistant", "system", "tool", "unknown"]
    input_hash: str
    character_count: int
    created_at: datetime | None = None
    tool_name: str | None = None
    source_uri: str | None = None


class ExtractionCandidate(ExtractedProposition):
    candidate_id: UUID
    content_hash: str
    asserting_role: Literal["user", "assistant", "system", "tool", "unknown"]
    asserting_principal_id: UUID | None = None
    asserting_tool: str | None = None
    attribution_basis: Literal["caller_supplied_roles"] = "caller_supplied_roles"
    evidence_root: str
    evidence_root_basis: Literal["input_batch"] = "input_batch"
    outcome: Literal[
        "written",
        "deduped",
        "rejected",
        "abstained",
        "volatile_recommended",
        "error",
        "preview",
    ]
    outcome_reason: str | None = None
    memory_item_id: UUID | None = None
    ingest_id: UUID | None = None


class ExtractionReceipt(StrictModel):
    schema_version: Literal["engram.extraction.v1"] = "engram.extraction.v1"
    prompt_version: str = PROMPT_VERSION
    run_id: UUID
    tenant_id: UUID
    principal_id: UUID
    workspace_id: UUID | None
    memory_profile_revision_id: UUID | None
    source_type: str
    visibility: str
    mode: Literal["preview", "write_proposed"]
    input_hash: str
    evidence_root: str
    messages: list[EvidenceMessage]
    provider: str
    model: str
    provider_model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    provider_cost_usd: float | None = None
    latency_ms: int
    reason_codes: list[str]
    candidates: list[ExtractionCandidate]


class ExtractResponse(StrictModel):
    receipt: ExtractionReceipt
    receipt_hash: str
