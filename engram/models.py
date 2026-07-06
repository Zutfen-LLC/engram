"""SQLAlchemy ORM models for Engram (v2 — trust model revision)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from engram.config import settings


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)

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
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)

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
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)

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
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)

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
    human_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(nullable=True)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Recall ranking
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    last_recalled_at: Mapped[datetime | None] = mapped_column(nullable=True)
    recall_count: Mapped[int] = mapped_column(Integer, default=0)
    startup_recall_count: Mapped[int] = mapped_column(Integer, default=0)
    last_verified_at: Mapped[datetime | None] = mapped_column(nullable=True)

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
    conflict_resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # Privacy
    sensitivity: Mapped[str] = mapped_column(String(20), default="normal")

    # External linkage
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_source: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Temporal validity
    valid_from: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(nullable=True)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memory_items.id"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)

    embeddings: Mapped[list[MemoryEmbedding]] = relationship(back_populates="memory_item")
    events: Mapped[list[ItemEvent]] = relationship(back_populates="item")


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
    embedding_model: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_dim: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(settings.embedding_dim), nullable=True
    )
    embedded_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)
    embedding_status: Mapped[str] = mapped_column(String(20), default="complete")

    memory_item: Mapped[MemoryItem] = relationship(back_populates="embeddings")


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
    valid_from: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(nullable=True)
    source_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memory_items.id", ondelete="SET NULL"), nullable=True
    )
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    review_status: Mapped[str] = mapped_column(String(20), default="proposed")
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)


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
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)


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
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)

    item: Mapped[MemoryItem] = relationship(back_populates="events")


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
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    principal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("principals.id"), nullable=True
    )
    key_hash: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String), default=["read", "write"])
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)


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
    item_ids: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    byte_budget: Mapped[int | None] = mapped_column(nullable=True)
    token_budget: Mapped[int | None] = mapped_column(nullable=True)
    scoring_version: Mapped[str] = mapped_column(String(20), default="v1")
    config_version: Mapped[str] = mapped_column(String(20), default="v1")
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)


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

    # Recall limits
    max_pinned_tokens: Mapped[int] = mapped_column(Integer, default=2048)
    stale_after_days: Mapped[int] = mapped_column(Integer, default=90)
    startup_recall_penalty_threshold: Mapped[int] = mapped_column(Integer, default=5)
    startup_recall_penalty_factor: Mapped[float] = mapped_column(Float, default=0.5)

    # Source trust defaults
    trust_manual_user: Mapped[float] = mapped_column(Float, default=0.9)
    trust_manual_agent: Mapped[float] = mapped_column(Float, default=0.6)
    trust_import: Mapped[float] = mapped_column(Float, default=0.8)
    trust_extraction: Mapped[float] = mapped_column(Float, default=0.5)
    trust_sync_turn: Mapped[float] = mapped_column(Float, default=0.4)
    trust_pre_compress: Mapped[float] = mapped_column(Float, default=0.3)

    # Default memory_confidence per source_type (enables auto-promotion in 1A without LLM)
    confidence_manual_user: Mapped[float] = mapped_column(Float, default=0.9)
    confidence_manual_agent: Mapped[float] = mapped_column(Float, default=0.5)
    confidence_import: Mapped[float] = mapped_column(Float, default=0.8)
    confidence_extraction: Mapped[float] = mapped_column(Float, default=0.5)
    confidence_sync_turn: Mapped[float] = mapped_column(Float, default=0.4)
    confidence_pre_compress: Mapped[float] = mapped_column(Float, default=0.3)

    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)


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
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)
