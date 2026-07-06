"""SQLAlchemy ORM models for Engram."""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, String, Text, text
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
    created_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), nullable=False
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
        server_default=text("now()"), nullable=False
    )

    tenant: Mapped[Tenant] = relationship(back_populates="workspaces")


class Principal(Base):
    """An agent or user that can own/write memories."""

    __tablename__ = "principals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(String(50), default="agent")  # agent | user
    created_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), nullable=False
    )

    tenant: Mapped[Tenant] = relationship(back_populates="principals")


class MemoryItem(Base):
    """The core unit of memory — absorbs drawers (MemPalace) and ledger entries (CCA)."""

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
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    wing: Mapped[str | None] = mapped_column(Text, nullable=True)
    room: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Scoping
    visibility: Mapped[str] = mapped_column(String(20), default="workspace")
    # private | workspace | tenant | public

    # Provenance
    source_type: Mapped[str] = mapped_column(String(50), default="manual")
    # manual | sync_turn | pre_compress | session_end | import | migration
    source_session: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Semantic
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(settings.embedding_dim), nullable=True
    )

    # Temporal validity (append-first model)
    valid_from: Mapped[datetime] = mapped_column(
        server_default=text("now()"), nullable=False
    )
    valid_to: Mapped[datetime | None] = mapped_column(nullable=True)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memory_items.id"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), nullable=False
    )


class KgTriple(Base):
    """Knowledge graph fact: subject → predicate → object with temporal validity."""

    __tablename__ = "kg_triples"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True
    )
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    predicate: Mapped[str] = mapped_column(Text, nullable=False)
    object: Mapped[str] = mapped_column(Text, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(
        server_default=text("now()"), nullable=False
    )
    valid_to: Mapped[datetime | None] = mapped_column(nullable=True)
    source_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memory_items.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), nullable=False
    )


class Tunnel(Base):
    """Cross-wing/room link — a declared relationship between taxonomy nodes."""

    __tablename__ = "tunnels"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    source_wing: Mapped[str] = mapped_column(Text, nullable=False)
    source_room: Mapped[str] = mapped_column(Text, nullable=False)
    target_wing: Mapped[str] = mapped_column(Text, nullable=False)
    target_room: Mapped[str] = mapped_column(Text, nullable=True)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), nullable=False
    )


class ApiKey(Base):
    """API keys for authenticating requests."""

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
    created_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), nullable=False
    )
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
    # startup | semantic | keyword
    query: Mapped[str | None] = mapped_column(Text, nullable=True)
    item_ids: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    byte_budget: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), nullable=False
    )
