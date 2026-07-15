"""SQLAlchemy ORM models for Engram (v2 — trust model revision)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    workspaces: Mapped[list[Workspace]] = relationship(back_populates="tenant")
    principals: Mapped[list[Principal]] = relationship(back_populates="tenant")


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    tenant: Mapped[Tenant] = relationship(back_populates="workspaces")
    members: Mapped[list[WorkspaceMember]] = relationship(back_populates="workspace")


class Principal(Base):
    __tablename__ = "principals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(String(50), default="agent")
    # Server-owned internal identity key (V2-BL-003B). NULL for ordinary
    # principals (agent/user/admin/system created through the caller-facing
    # API). Non-null marks a trusted internal actor resolved by server code,
    # never by name/type/api-key. CHECK (internal_key IS NULL OR type='system')
    # and a partial unique index on (tenant_id, internal_key) are enforced by
    # migrations/010_internal_key.sql. Never expose as a writable API field.
    internal_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    tenant: Mapped[Tenant] = relationship(back_populates="principals")


class WorkspaceMember(Base):
    """Principal membership in a workspace with role-based access."""

    __tablename__ = "workspace_members"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("principals.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(50), default="member")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    workspace: Mapped[Workspace] = relationship(back_populates="members")


class MemoryItem(Base):
    """The core unit of memory with trust, provenance, and review state."""

    __tablename__ = "memory_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("principals.id"), nullable=False
    )

    # Content
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    wing: Mapped[str | None] = mapped_column(Text, nullable=True)
    room: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Subject / entity (what is this memory ABOUT)
    subject_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    subject_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Scoping
    visibility: Mapped[str] = mapped_column(String(20), default="workspace")

    # Trust & review
    review_status: Mapped[str] = mapped_column(String(20), default="proposed")
    memory_confidence: Mapped[float] = mapped_column(Float, default=0.5)
    source_trust: Mapped[float] = mapped_column(Float, default=0.5)
    source_confidence_prior: Mapped[float | None] = mapped_column(Float, nullable=True)
    retention_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    retention_disposition: Mapped[str | None] = mapped_column(String(20), nullable=True)
    retention_evidence_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    authority: Mapped[int] = mapped_column(
        SmallInteger, default=10, server_default=text("10"), nullable=False
    )
    human_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Recall ranking
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    last_recalled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    recall_count: Mapped[int] = mapped_column(Integer, default=0)
    startup_recall_count: Mapped[int] = mapped_column(Integer, default=0)
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Provenance (expanded)
    source_type: Mapped[str] = mapped_column(String(50), default="manual")
    source_session: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_by_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Conflict tracking
    conflicts_with_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memory_items.id"), nullable=True
    )
    conflict_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    conflict_resolution_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    conflict_resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("principals.id"), nullable=True
    )
    conflict_resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Privacy
    sensitivity: Mapped[str] = mapped_column(String(20), default="normal")

    # External linkage
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_source: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Temporal validity
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memory_items.id"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    embeddings: Mapped[list[MemoryEmbedding]] = relationship(back_populates="memory_item")
    events: Mapped[list[ItemEvent]] = relationship(back_populates="item")


class EmbeddingProfile(Base):
    """Deployment-global contract for one compatible embedding vector space."""

    __tablename__ = "embedding_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    distance_metric: Mapped[str] = mapped_column(Text, default="cosine", nullable=False)
    state: Mapped[str] = mapped_column(Text, default="candidate", nullable=False)
    index_status: Mapped[str] = mapped_column(Text, default="missing", nullable=False)
    index_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    profile_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )


class MemoryEmbedding(Base):
    """Embeddings stored separately to support multiple models and re-embedding."""

    __tablename__ = "memory_embeddings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["memory_item_id", "tenant_id"],
            ["memory_items.id", "memory_items.tenant_id"],
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    memory_item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("embedding_profiles.id"),
        server_default=text("active_embedding_profile_id()"),
        nullable=False,
    )
    embedding_model: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_dim: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(), nullable=True)
    embedded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    embedding_status: Mapped[str] = mapped_column(String(20), default="complete")

    memory_item: Mapped[MemoryItem] = relationship(back_populates="embeddings")
    profile: Mapped[EmbeddingProfile] = relationship()


class KgTriple(Base):
    """Knowledge graph triple with visibility inherited from source item."""

    __tablename__ = "kg_triples"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True
    )
    principal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("principals.id"), nullable=True
    )
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    predicate: Mapped[str] = mapped_column(Text, nullable=False)
    object: Mapped[str] = mapped_column(Text, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memory_items.id", ondelete="SET NULL"), nullable=True
    )
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    review_status: Mapped[str] = mapped_column(String(20), default="proposed")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class MemoryEdge(Base):
    """Typed, directed relationship between two memory items (ENG-AUD-012 / F19).

    Distinct from :class:`KgTriple`: a triple is a free-text subject/predicate/
    object fact optionally backed by one memory item, while an edge links two
    concrete ``memory_items`` rows so relationship-aware recall can expand a
    semantic hit to its directly related memories (depth = 1, bounded — see
    engram.relationship_recall). ``weight`` is optional per-edge override;
    when unset, recall falls back to the static ``edge_type`` -> strength
    mapping in engram.relationship_recall.EDGE_TYPE_WEIGHTS.
    """

    __tablename__ = "memory_edges"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    source_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memory_items.id", ondelete="CASCADE"), nullable=False
    )
    target_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memory_items.id", ondelete="CASCADE"), nullable=False
    )
    edge_type: Mapped[str] = mapped_column(String(30), nullable=False)
    weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    principal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("principals.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class Tunnel(Base):
    __tablename__ = "tunnels"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    source_wing: Mapped[str] = mapped_column(Text, nullable=False)
    source_room: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_wing: Mapped[str] = mapped_column(Text, nullable=False)
    target_room: Mapped[str | None] = mapped_column(Text, nullable=True)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class ItemEvent(Base):
    """Audit trail for metadata mutations (content stays append-first)."""

    __tablename__ = "item_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memory_items.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    field_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("principals.id"), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    item: Mapped[MemoryItem] = relationship(back_populates="events")


class ClassificationRun(Base):
    """Server-attested, tenant-scoped classification and retention evidence."""

    __tablename__ = "classification_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("principals.id", ondelete="CASCADE"), nullable=False
    )
    ingest_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("candidate_ingests.id", ondelete="RESTRICT"), nullable=True
    )
    memory_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("memory_items.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )
    bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    canonicalization_version: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True
    )
    context_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_length: Mapped[int | None] = mapped_column(Integer, nullable=True)
    suggested_kind: Mapped[str] = mapped_column(Text, nullable=False)
    suggested_wing: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_room: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_visibility: Mapped[str | None] = mapped_column(Text, nullable=True)
    taxonomy_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    retention_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    retention_disposition: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    classification_version: Mapped[str] = mapped_column(Text, nullable=False)
    retention_policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ClassificationRule(Base):
    __tablename__ = "classification_rules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    rule_type: Mapped[str] = mapped_column(String(30), nullable=False)
    pattern: Mapped[str] = mapped_column(Text, nullable=False)
    target_kind: Mapped[str | None] = mapped_column(String(50), nullable=True)
    target_wing: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_room: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class MemoryKind(Base):
    """Tenant-governed kind registry entry (ENG-AUD-010 / F17).

    The source of truth for which ``memory_items.kind`` values are valid and
    what behavior they carry. Behavior flags replace hard-coded kind-name
    checks: ``singleton`` (supersession), ``requires_review`` (initial
    review_status), and ``stays_in_recall_when_disputed`` (startup recall
    inclusion while disputed). Builtin kinds are seeded per tenant; tenants
    may add governed custom kinds without a schema migration.
    """

    __tablename__ = "memory_kinds"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    singleton: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    stays_in_recall_when_disputed: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    auto_promote_from_inferred: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    default_importance: Mapped[float | None] = mapped_column(Float, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    principal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("principals.id"), nullable=True
    )
    # Bcrypt hash for LEGACY keys (eng_<random>, pre-ENG-AUD-003). Nullable now:
    # new-format keys store a digest instead and leave this NULL.
    key_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    # New-format (eng_<key_id>_<secret>) indexed lookup + digest verification.
    key_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    secret_digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    digest_algorithm: Mapped[str | None] = mapped_column(Text, nullable=True)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String), default=["read", "write"])
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RecallLog(Base):
    """Audit record of recall operations."""

    __tablename__ = "recall_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("principals.id"), nullable=False
    )
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    query: Mapped[str | None] = mapped_column(Text, nullable=True)
    item_ids: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True
    )
    byte_budget: Mapped[int | None] = mapped_column(nullable=True)
    token_budget: Mapped[int | None] = mapped_column(nullable=True)
    scoring_version: Mapped[str] = mapped_column(String(20), default="v1")
    config_version: Mapped[str] = mapped_column(String(20), default="v1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    # Idempotency claim for the async recall.telemetry job (ENG-AUD-011 / F18):
    # NULL until the telemetry worker has applied last_recalled_at/recall
    # counter updates for this recall's item_ids. The worker claims by
    # transactionally setting this column (WHERE telemetry_applied_at IS NULL)
    # together with the item updates in one commit, so a retry (worker crash,
    # queue redelivery) that finds it already set is a safe no-op.
    telemetry_applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class TenantConfig(Base):
    """Tenant-configurable trust defaults, scoring weights, and recall policy.

    Versioned for audit reproducibility — recall_logs records config_version.
    """

    __tablename__ = "tenant_config"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    config_version: Mapped[str] = mapped_column(String(50), default="v1")

    # Scoring weights
    weight_importance: Mapped[float] = mapped_column(Float, default=0.30)
    weight_source_trust: Mapped[float] = mapped_column(Float, default=0.25)
    weight_memory_confidence: Mapped[float] = mapped_column(Float, default=0.20)
    weight_recency: Mapped[float] = mapped_column(Float, default=0.15)
    weight_verified: Mapped[float] = mapped_column(Float, default=0.10)

    # Auto-promotion
    auto_promote_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_promote_confidence_threshold: Mapped[float] = mapped_column(Float, default=0.7)
    auto_promote_min_age_hours: Mapped[int] = mapped_column(Integer, default=72)
    # New installations enable the evidence lane. Migration 016 deliberately
    # backfills existing tenants as disabled for an explicit rollout.
    auto_promote_evidence_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_promote_evidence_threshold: Mapped[float] = mapped_column(Float, default=0.70)

    # Recall limits
    max_pinned_tokens: Mapped[int] = mapped_column(Integer, default=2048)
    stale_after_days: Mapped[int] = mapped_column(Integer, default=90)
    startup_recall_penalty_threshold: Mapped[int] = mapped_column(Integer, default=5)
    startup_recall_penalty_factor: Mapped[float] = mapped_column(Float, default=0.5)
    feedback_daily_limit: Mapped[int] = mapped_column(Integer, default=500)

    # Source trust defaults
    trust_manual_user: Mapped[float] = mapped_column(Float, default=0.9)
    trust_manual_agent: Mapped[float] = mapped_column(Float, default=0.6)
    trust_import: Mapped[float] = mapped_column(Float, default=0.8)
    trust_extraction: Mapped[float] = mapped_column(Float, default=0.5)
    trust_sync_turn: Mapped[float] = mapped_column(Float, default=0.4)
    trust_pre_compress: Mapped[float] = mapped_column(Float, default=0.3)
    trust_session_end: Mapped[float] = mapped_column(Float, default=0.35)

    # Default memory_confidence per source_type (enables auto-promotion in 1A without LLM)
    confidence_manual_user: Mapped[float] = mapped_column(Float, default=0.9)
    confidence_manual_agent: Mapped[float] = mapped_column(Float, default=0.5)
    confidence_import: Mapped[float] = mapped_column(Float, default=0.8)
    confidence_extraction: Mapped[float] = mapped_column(Float, default=0.5)
    confidence_sync_turn: Mapped[float] = mapped_column(Float, default=0.4)
    confidence_pre_compress: Mapped[float] = mapped_column(Float, default=0.3)
    confidence_session_end: Mapped[float] = mapped_column(Float, default=0.35)

    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class DeletionEvent(Base):
    """Tombstone record for hard-deleted items. FK-safe (deleted_item_id is NOT a FK)."""

    __tablename__ = "deletion_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    deleted_item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    deleted_content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    deleted_by_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("principals.id"), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    deleted_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)


class FeedbackEvent(Base):
    """Records feedback on recalled items. Drives penalty resets, importance adjustment, quorum."""

    __tablename__ = "feedback_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memory_items.id", ondelete="CASCADE"), nullable=False
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("principals.id"), nullable=False
    )
    verdict: Mapped[str] = mapped_column(String(10), nullable=False)  # useful | noise
    recall_log_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recall_logs.id", ondelete="SET NULL"), nullable=True
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replaces_feedback_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("feedback_events.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class Job(Base):
    """A durable background job (Postgres-backed queue, ENG-AUD-008).

    Drives the async write path: embedding generation, LLM classification
    refinement, embedding-dependent conflict detection, and promotion sweeps.
    Workers claim via ``FOR UPDATE SKIP LOCKED`` (see engram/jobs.py); the table
    is tenant-scoped and RLS-protected (FORCE), consistent with ENG-AUD-002.
    """

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    job_type: Mapped[str] = mapped_column(Text, nullable=False)
    # pending | running | succeeded | failed | dead | cancelled (CHECK in DDL)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CandidateIngest(Base):
    """Immutable server-issued identity for one candidate entering the pipeline."""

    __tablename__ = "candidate_ingests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("principals.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True
    )
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    client_correlation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class UsageEvent(Base):
    """Append-only metering/usage telemetry ledger (ENG-METER-001).

    Diagnostic observability data for dogfood economics, not a billing record.
    Distinct from ``ItemEvent`` (trust/audit history of one memory item):
    covers operations — rejected candidates, provider calls, retrieval
    requests, client-reported lifecycle summaries — that never create a
    memory item. Written by :mod:`engram.usage` via a short-lived app-role
    session; RLS is FORCE-enabled and the app role has no UPDATE/DELETE grant
    (see migrations/017_usage_events.sql), so it is append-only from the
    application's perspective.
    """

    __tablename__ = "usage_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    principal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("principals.id", ondelete="SET NULL"), nullable=True
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True
    )

    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)

    correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    ingest_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("candidate_ingests.id", ondelete="RESTRICT"), nullable=True
    )
    dedupe_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    source_type: Mapped[str | None] = mapped_column(Text, nullable=True)

    provider_adapter: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_host: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding_profile: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage_class: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_call_attempted: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    input_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    input_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    prompt_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reported_cost_usd: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)

    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
