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
    ClassifyRequest,
    ClassifyResponse,
    DiaryWrite,
    DiaryWriteResponse,
    KgAddRequest,
    KgAddResponse,
    KgTripleOut,
    LifecycleSummaryRequest,
    LifecycleSummaryResponse,
    RecallRequest,
    RecallResponse,
    RememberRequest,
    RememberResponse,
    SearchRequest,
    SearchResponse,
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
]
