"""Thin async Python SDK for the Engram memory service.

Public API::

    from engram_client import EngramClient, RememberResponse, EngramNotFoundError
"""

from __future__ import annotations

from .client import (
    EngramAuthError,
    EngramClient,
    EngramClientError,
    EngramError,
    EngramHTTPError,
    EngramNotFoundError,
    EngramServerError,
    EngramValidationError,
)
from .models import (
    AgentCreated,
    AgentCreateRequest,
    ApiKeyCreateRequest,
    ApiKeyCreateResponse,
    ClassifyRequest,
    ClassifyResponse,
    DiaryWrite,
    DiaryWriteResponse,
    KgAddRequest,
    KgAddResponse,
    KgTripleOut,
    LifecycleSummaryRequest,
    LifecycleSummaryResponse,
    MemoryProfile,
    MemoryProfileCreate,
    MemoryProfileLifecycle,
    MemoryProfilePolicy,
    MemoryProfileRevision,
    MemoryProfileRevisionCreate,
    MemoryProfileSummary,
    RecallRequest,
    RecallResponse,
    RememberRequest,
    RememberResponse,
    SearchRequest,
    SearchResponse,
    VisibilityKind,
    WhoAmIResponse,
    WorkspaceGrantInput,
    WorkspaceGrantOut,
)

__version__ = "0.1.0"

__all__ = [
    "EngramClient",
    # exceptions
    "EngramError",
    "EngramHTTPError",
    "EngramAuthError",
    "EngramNotFoundError",
    "EngramValidationError",
    "EngramClientError",
    "EngramServerError",
    # models
    "RememberRequest",
    "RememberResponse",
    "VisibilityKind",
    "RecallRequest",
    "RecallResponse",
    "SearchRequest",
    "SearchResponse",
    "ClassifyRequest",
    "ClassifyResponse",
    "KgAddRequest",
    "KgAddResponse",
    "KgTripleOut",
    "DiaryWrite",
    "DiaryWriteResponse",
    "LifecycleSummaryRequest",
    "LifecycleSummaryResponse",
    "WorkspaceGrantInput",
    "WorkspaceGrantOut",
    "MemoryProfilePolicy",
    "MemoryProfileCreate",
    "MemoryProfileRevisionCreate",
    "MemoryProfileRevision",
    "MemoryProfile",
    "MemoryProfileSummary",
    "MemoryProfileLifecycle",
    "ApiKeyCreateRequest",
    "ApiKeyCreateResponse",
    "AgentCreateRequest",
    "AgentCreated",
    "WhoAmIResponse",
]
