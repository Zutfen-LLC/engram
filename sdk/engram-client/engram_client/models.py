"""Pydantic models mirroring the Engram REST API request/response shapes.

These mirror the server-side models in ``engram.api.routes.*`` so the SDK
validates payloads client-side and gives callers typed return values. They are
intentionally a standalone copy: the SDK must not import from the ``engram``
server package.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

SourceKind = Literal[
    "manual", "import", "migration", "extraction", "sync_turn", "pre_compress", "session_end"
]
SensitivityKind = Literal["normal", "sensitive", "restricted"]
SearchMode = Literal["keyword", "semantic", "hybrid"]


# ---- /v1/remember ----


class RememberRequest(BaseModel):
    content: str
    kind: str | None = None
    wing: str | None = None
    room: str | None = None
    workspace: str | None = None
    visibility: str = "workspace"
    source_type: SourceKind = "manual"
    source_session: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    subject_type: str | None = None
    subject_id: str | None = None
    subject_name: str | None = None
    importance: float = 0.5
    sensitivity: SensitivityKind = "normal"
    external_id: str | None = None
    external_source: str | None = None
    classification_run_id: UUID | None = None


class RememberResponse(BaseModel):
    id: UUID
    status: str  # created | deduped | superseded
    review_status: str
    memory_confidence: float
    deduped_existing_id: UUID | None = None
    superseded_id: UUID | None = None


# ---- /v1/recall ----


class RecallRequest(BaseModel):
    mode: str = "startup"  # startup | semantic
    query: str | None = None
    workspace: str | None = None
    byte_budget: int | None = None
    token_budget: int | None = None
    item_budget: int | None = None


class RecallResponse(BaseModel):
    working_set: str
    item_count: int
    byte_count: int
    pinned_omitted_count: int = 0
    omitted_count: int
    items: list[dict[str, Any]] = Field(default_factory=list)
    scoring_version: str = "v1"
    config_version: str = "v1"
    recall_log_id: str | None = None
    message: str | None = None


# ---- /v1/search ----


class SearchRequest(BaseModel):
    query: str
    mode: SearchMode = "hybrid"
    limit: int = Field(default=10, ge=1, le=100)
    wing: str | None = None
    room: str | None = None
    kind: str | None = None


class SearchResponse(BaseModel):
    results: list[dict[str, Any]]
    total: int
    message: str | None = None


# ---- /v1/classify ----


class ClassifyRequest(BaseModel):
    content: str
    context: str | None = None
    workspace: str | None = None
    source_type: SourceKind = "manual"


class ClassifyResponse(BaseModel):
    classification_run_id: UUID
    expires_at: datetime
    suggested_kind: str
    suggested_wing: str | None = None
    suggested_room: str | None = None
    suggested_visibility: str | None = None
    taxonomy_confidence: float
    confidence: float
    retention_confidence: float
    retention_disposition: Literal["retain", "transient", "noise", "uncertain"]
    reason: str
    rules_matched: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def keep_legacy_confidence_alias_equal(self) -> ClassifyResponse:
        self.confidence = self.taxonomy_confidence
        return self


# ---- /v1/kg ----


class KgAddRequest(BaseModel):
    subject: str
    predicate: str
    object: str
    workspace: str | None = None
    valid_from: str | None = None
    source_item_id: UUID | None = None
    confidence: float = 0.5


class KgAddResponse(BaseModel):
    id: UUID
    triple: dict[str, Any]
    source_item_id: UUID | None = None
    memory_item: dict[str, Any] | None = None


class KgTripleOut(BaseModel):
    id: UUID
    subject: str
    predicate: str
    object: str
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    source_item_id: UUID | None = None
    confidence: float
    review_status: str
    created_at: datetime
    trust_annotation: str | None = None


# ---- /v1/diary ----


class DiaryWrite(BaseModel):
    entry: str
    topic: str | None = None
    principal: str | None = None
    on_behalf_of_principal_id: UUID | None = None
    reason: str | None = None


class DiaryWriteResponse(BaseModel):
    id: UUID
    status: str  # created | deduped
    review_status: str
    principal_id: UUID
    actor_principal_id: UUID | None
    represented: bool | None
    attribution_status: Literal["recorded", "legacy_unknown"]
    authority: int
    authority_label: str
