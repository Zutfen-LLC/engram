"""Thin async client for the Engram REST API.

Usage::

    from engram_client import EngramClient

    async with EngramClient("http://localhost:8000", api_key="eng_...") as client:
        resp = await client.remember("Always use lowercase table names")
        results = await client.kg_query("users")

The client is a thin wrapper over :class:`httpx.AsyncClient` with bearer-token
auth and typed exceptions for 4xx/5xx responses.
"""

from __future__ import annotations

import warnings
from typing import Any, TypeVar
from uuid import UUID

import httpx
from pydantic import BaseModel

from .models import (
    ClassifyRequest,
    ClassifyResponse,
    DiaryWrite,
    DiaryWriteResponse,
    KgAddRequest,
    KgAddResponse,
    KgTripleOut,
    LifecycleEvent,
    LifecycleSummaryRequest,
    LifecycleSummaryResponse,
    RecallRequest,
    RecallResponse,
    RememberRequest,
    RememberResponse,
    SearchMode,
    SearchRequest,
    SearchResponse,
    SensitivityKind,
    SourceKind,
)

_DEFAULT_TIMEOUT = 30.0

T = TypeVar("T", bound=BaseModel)


# ---- Exceptions ----


class EngramError(Exception):
    """Base class for all SDK errors."""


class EngramHTTPError(EngramError):
    """Raised when the API returns a 4xx/5xx response."""

    def __init__(self, status_code: int, detail: Any, body: Any) -> None:
        self.status_code = status_code
        self.detail = detail
        self.body = body
        message = detail if isinstance(detail, str) and detail else f"HTTP {status_code}"
        super().__init__(f"[{status_code}] {message}")


class EngramAuthError(EngramHTTPError):
    """401 / 403 — authentication or authorization failure."""


class EngramNotFoundError(EngramHTTPError):
    """404 — resource not found."""


class EngramValidationError(EngramHTTPError):
    """422 — request failed validation (semantic or secret-pattern)."""


class EngramClientError(EngramHTTPError):
    """Any other 4xx response."""


class EngramServerError(EngramHTTPError):
    """5xx — server-side failure."""


def _raise_for_status(resp: httpx.Response) -> None:
    try:
        body: Any = resp.json()
    except ValueError:
        body = resp.text
    detail: Any = body.get("detail") if isinstance(body, dict) else body
    status = resp.status_code
    if status in (401, 403):
        raise EngramAuthError(status, detail, body)
    if status == 404:
        raise EngramNotFoundError(status, detail, body)
    if status == 422:
        raise EngramValidationError(status, detail, body)
    if 400 <= status < 500:
        raise EngramClientError(status, detail, body)
    raise EngramServerError(status, detail, body)


# ---- Client ----


class EngramClient:
    """Async client for the Engram memory service."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers: dict[str, str] = {"accept": "application/json"}
        if api_key is not None:
            parsed = httpx.URL(base_url)
            if parsed.scheme == "http" and parsed.host not in ("localhost", "127.0.0.1", "::1"):
                warnings.warn(
                    "api_key sent over plaintext http to non-loopback host "
                    f"{parsed.host!r}; use https for production",
                    stacklevel=2,
                )
            headers["authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    @property
    def httpx_client(self) -> httpx.AsyncClient:
        """The underlying :class:`httpx.AsyncClient`."""
        return self._client

    async def __aenter__(self) -> EngramClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    # ---- internal request helpers ----

    async def _send(
        self,
        method: str,
        path: str,
        response_model: type[T],
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> T:
        resp = await self._client.request(method, path, params=params, json=json_body)
        if resp.is_error:
            _raise_for_status(resp)
        return response_model.model_validate(resp.json())

    async def _send_list(
        self,
        method: str,
        path: str,
        response_model: type[T],
        *,
        params: dict[str, Any] | None = None,
    ) -> list[T]:
        resp = await self._client.request(method, path, params=params)
        if resp.is_error:
            _raise_for_status(resp)
        data: Any = resp.json()
        return [response_model.model_validate(item) for item in data]

    # ---- /v1/remember ----

    async def remember(
        self,
        content: str,
        *,
        kind: str | None = None,
        wing: str | None = None,
        room: str | None = None,
        workspace: str | None = None,
        visibility: str = "workspace",
        source_type: SourceKind = "manual",
        source_session: str | None = None,
        metadata: dict[str, Any] | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
        subject_name: str | None = None,
        importance: float = 0.5,
        sensitivity: SensitivityKind = "normal",
        external_id: str | None = None,
        external_source: str | None = None,
        classification_run_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> RememberResponse:
        """Write a memory item with dedup, trust defaults, and supersession.

        ``correlation_id``, when shared with a preceding :meth:`classify`
        call for the same candidate, lets the server count the two calls as a
        single usage-telemetry candidate observation rather than two. Omit it
        for a direct remember with no preceding classify — the server
        generates one.
        """
        req = RememberRequest(
            content=content,
            kind=kind,
            wing=wing,
            room=room,
            workspace=workspace,
            visibility=visibility,
            source_type=source_type,
            source_session=source_session,
            metadata=metadata if metadata is not None else {},
            subject_type=subject_type,
            subject_id=subject_id,
            subject_name=subject_name,
            importance=importance,
            sensitivity=sensitivity,
            external_id=external_id,
            external_source=external_source,
            classification_run_id=classification_run_id,
            correlation_id=correlation_id,
        )
        return await self._send(
            "POST",
            "/v1/remember",
            RememberResponse,
            json_body=req.model_dump(mode="json", exclude_none=True),
        )

    # ---- /v1/recall ----

    async def recall(
        self,
        *,
        mode: str = "startup",
        query: str | None = None,
        workspace: str | None = None,
        byte_budget: int | None = None,
        token_budget: int | None = None,
        item_budget: int | None = None,
    ) -> RecallResponse:
        """Bounded recall: deterministic startup set or semantic query."""
        req = RecallRequest(
            mode=mode,
            query=query,
            workspace=workspace,
            byte_budget=byte_budget,
            token_budget=token_budget,
            item_budget=item_budget,
        )
        return await self._send(
            "POST",
            "/v1/recall",
            RecallResponse,
            json_body=req.model_dump(mode="json", exclude_none=True),
        )

    # ---- /v1/search ----

    async def search(
        self,
        query: str,
        *,
        mode: SearchMode = "hybrid",
        limit: int = 10,
        wing: str | None = None,
        room: str | None = None,
        kind: str | None = None,
    ) -> SearchResponse:
        """Keyword, semantic, or hybrid search."""
        req = SearchRequest(
            query=query,
            mode=mode,
            limit=limit,
            wing=wing,
            room=room,
            kind=kind,
        )
        return await self._send(
            "POST",
            "/v1/search",
            SearchResponse,
            json_body=req.model_dump(mode="json", exclude_none=True),
        )

    # ---- /v1/classify ----

    async def classify(
        self,
        content: str,
        *,
        context: str | None = None,
        workspace: str | None = None,
        source_type: SourceKind = "manual",
        correlation_id: UUID | None = None,
    ) -> ClassifyResponse:
        """Classify raw text: suggest kind, wing, room, visibility.

        ``correlation_id``, when shared with a subsequent :meth:`remember`
        call for the same candidate, lets the server count the two calls as a
        single usage-telemetry candidate observation rather than two.
        """
        req = ClassifyRequest(
            content=content,
            context=context,
            workspace=workspace,
            source_type=source_type,
            correlation_id=correlation_id,
        )
        return await self._send(
            "POST",
            "/v1/classify",
            ClassifyResponse,
            json_body=req.model_dump(mode="json", exclude_none=True),
        )

    # ---- /v1/telemetry/lifecycle ----

    async def report_lifecycle_summary(
        self,
        *,
        invocation_id: UUID,
        event: LifecycleEvent,
        extracted: int = 0,
        guard_rejected: int = 0,
        classified: int = 0,
        promoted: int = 0,
        parked: int = 0,
        errors: int = 0,
        candidate_bytes: int = 0,
        latency_ms: int | None = None,
        adapter_version: str | None = None,
    ) -> LifecycleSummaryResponse:
        """Report one diagnostic, client-reported lifecycle summary.

        Never send candidate text here — only aggregate counts/byte totals
        for one hook invocation. Tenant/principal are derived from
        authentication server-side; this is diagnostic and non-authoritative.
        """
        req = LifecycleSummaryRequest(
            invocation_id=invocation_id,
            event=event,
            extracted=extracted,
            guard_rejected=guard_rejected,
            classified=classified,
            promoted=promoted,
            parked=parked,
            errors=errors,
            candidate_bytes=candidate_bytes,
            latency_ms=latency_ms,
            adapter_version=adapter_version,
        )
        return await self._send(
            "POST",
            "/v1/telemetry/lifecycle",
            LifecycleSummaryResponse,
            json_body=req.model_dump(mode="json", exclude_none=True),
        )

    # ---- /v1/kg ----

    async def kg_add(
        self,
        subject: str,
        predicate: str,
        object: str,
        *,
        workspace: str | None = None,
        valid_from: str | None = None,
        source_item_id: UUID | str | None = None,
        confidence: float = 0.5,
    ) -> KgAddResponse:
        """Add a knowledge-graph triple."""
        resolved_id = (
            UUID(str(source_item_id)) if isinstance(source_item_id, str) else source_item_id
        )
        req = KgAddRequest(
            subject=subject,
            predicate=predicate,
            object=object,
            workspace=workspace,
            valid_from=valid_from,
            source_item_id=resolved_id,
            confidence=confidence,
        )
        return await self._send(
            "POST",
            "/v1/kg",
            KgAddResponse,
            json_body=req.model_dump(mode="json", exclude_none=True),
        )

    async def kg_query(
        self,
        entity: str,
        *,
        direction: str = "both",
        as_of: str | None = None,
        predicate: str | None = None,
    ) -> list[KgTripleOut]:
        """Query knowledge-graph triples for an entity."""
        params: dict[str, Any] = {"entity": entity, "direction": direction}
        if as_of is not None:
            params["as_of"] = as_of
        if predicate is not None:
            params["predicate"] = predicate
        return await self._send_list("GET", "/v1/kg/query", KgTripleOut, params=params)

    # ---- /v1/diary ----

    async def diary_write(
        self,
        entry: str,
        principal: str | None = None,
        *,
        topic: str | None = None,
        on_behalf_of_principal_id: UUID | None = None,
        reason: str | None = None,
    ) -> DiaryWriteResponse:
        """Write the caller's diary, or explicitly represent another principal."""
        req = DiaryWrite(
            entry=entry,
            principal=principal,
            topic=topic,
            on_behalf_of_principal_id=on_behalf_of_principal_id,
            reason=reason,
        )
        return await self._send(
            "POST",
            "/v1/diary",
            DiaryWriteResponse,
            json_body=req.model_dump(mode="json", exclude_none=True),
        )
