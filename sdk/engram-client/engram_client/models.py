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
VisibilityKind = Literal["private", "workspace", "tenant", "public"]
SearchMode = Literal["keyword", "semantic", "hybrid"]
ProfileVisibility = Literal["private", "workspace", "tenant", "public"]


# ---- /v1/memory-profiles and profile-aware credential issuance ----


class WorkspaceGrant(BaseModel):
    workspace_id: UUID
    can_read: bool
    can_write: bool


class MemoryProfilePolicy(BaseModel):
    include_private: bool = True
    include_tenant: bool = False
    include_public: bool = False
    allow_tenant_write: bool = False
    allow_public_write: bool = False
    default_write_visibility: ProfileVisibility = "private"
    default_write_workspace_id: UUID | None = None
    workspace_grants: list[WorkspaceGrant] = Field(default_factory=list)


class MemoryProfileCreate(BaseModel):
    name: str
    slug: str
    description: str | None = None
    policy: MemoryProfilePolicy = Field(default_factory=MemoryProfilePolicy)
    reason: str


class MemoryProfileRevisionCreate(BaseModel):
    expected_active_revision_id: UUID
    policy: MemoryProfilePolicy
    reason: str


class MemoryProfileRevision(BaseModel):
    id: UUID
    version: int
    include_private: bool
    include_tenant: bool
    include_public: bool
    allow_tenant_write: bool
    allow_public_write: bool
    default_write_visibility: ProfileVisibility
    default_write_workspace_id: UUID | None
    created_by_principal_id: UUID | None
    reason: str
    created_at: datetime
    workspace_grants: list[WorkspaceGrant]


class MemoryProfile(BaseModel):
    id: UUID
    name: str
    slug: str
    description: str | None
    enabled: bool
    active_revision_id: UUID
    active_revision: MemoryProfileRevision
    created_at: datetime
    updated_at: datetime


class MemoryProfileSummary(BaseModel):
    id: UUID
    name: str
    slug: str
    description: str | None
    enabled: bool
    active_revision_id: UUID | None
    active_revision_version: int | None
    created_at: datetime
    updated_at: datetime


class MemoryProfileLifecycle(BaseModel):
    reason: str


class ApiKeyCreateRequest(BaseModel):
    tenant_id: UUID
    principal_id: UUID | None = None
    scopes: list[str] = Field(default_factory=lambda: ["read", "write"])
    label: str | None = None
    memory_profile_id: UUID | None = None


class ApiKeyCreateResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    principal_id: UUID | None
    scopes: list[str]
    label: str | None
    key: str
    memory_profile_id: UUID | None = None
    memory_profile_revision_id: UUID | None = None
    memory_profile_slug: str | None = None
    memory_profile_version: int | None = None


class AgentCreateRequest(BaseModel):
    name: str
    scopes: list[str] = Field(default_factory=lambda: ["read", "write"])
    label: str | None = None
    memory_profile_id: UUID | None = None


class AgentCreated(BaseModel):
    id: UUID
    name: str
    type: str
    created_at: datetime
    key: str
    key_id: UUID
    scopes: list[str]
    label: str | None
    memory_profile_id: UUID | None = None
    memory_profile_revision_id: UUID | None = None
    memory_profile_slug: str | None = None
    memory_profile_version: int | None = None


class WhoAmIMemoryProfile(BaseModel):
    id: UUID
    slug: str
    active_revision_id: UUID
    version: int


class WhoAmIResponse(BaseModel):
    principal_id: UUID
    tenant_id: UUID
    scopes: list[str]
    api_key_id: UUID | None
    memory_profile: WhoAmIMemoryProfile | None


# ---- /v1/remember ----


class RememberRequest(BaseModel):
    content: str
    kind: str | None = None
    wing: str | None = None
    room: str | None = None
    workspace: str | None = None
    # Omitted/None resolves server-side (ENG-SCOPE-001): private with no
    # workspace, workspace-shared when an authorized workspace is supplied.
    visibility: str | None = None
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
    # Optional correlation id shared with a preceding classify() call for the
    # same candidate. When omitted the server generates one (see
    # RememberResponse.correlation_id).
    correlation_id: UUID | None = None
    ingest_id: UUID | None = None


class RememberResponse(BaseModel):
    id: UUID
    status: str  # created | deduped | superseded
    review_status: str
    memory_confidence: float
    deduped_existing_id: UUID | None = None
    superseded_id: UUID | None = None
    correlation_id: UUID
    ingest_id: UUID
    attempt_id: UUID


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
    # Optional correlation id shared with a subsequent remember() call for the
    # same candidate. When omitted the server generates one (see
    # ClassifyResponse.correlation_id).
    correlation_id: UUID | None = None


class ClassifyResponse(BaseModel):
    classification_run_id: UUID
    expires_at: datetime
    correlation_id: UUID
    ingest_id: UUID
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


# ---- /v1/telemetry/lifecycle ----

LifecycleEvent = Literal["sync_turn", "pre_compress", "session_end"]


class LifecycleSummaryRequest(BaseModel):
    """Diagnostic, client-reported aggregate for one lifecycle-hook invocation.

    Never carries candidate text — only counts and aggregate byte totals.
    Tenant/principal are derived from authentication server-side, never from
    this body.
    """

    invocation_id: UUID
    event: LifecycleEvent
    extracted: int = 0
    guard_rejected: int = 0
    classified: int = 0
    promoted: int = 0
    parked: int = 0
    errors: int = 0
    candidate_bytes: int = 0
    latency_ms: int | None = None
    adapter_version: str | None = None


class LifecycleSummaryResponse(BaseModel):
    status: Literal["succeeded", "partial"]
    invocation_id: UUID


# ---- /v1/kg ----


class KgAddRequest(BaseModel):
    subject: str
    predicate: str
    object: str
    workspace: str | None = None
    visibility: VisibilityKind | None = None
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
