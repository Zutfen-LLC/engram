"""Structured extraction with provenance-only receipts and proposed writes."""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from engram.api.routes.memory import RememberRequest, _remember_impl, _resolve_principal
from engram.auth import READ_SCOPE, WRITE_SCOPE, Principal
from engram.canonicalize import canonicalize, content_hash
from engram.db import get_session
from engram.extraction import (
    digest,
    extract_messages,
    ground_source_cues,
    preserve_context_spans,
    text_digest,
    validate_evidence,
)
from engram.extraction_schema import (
    EvidenceMessage,
    ExtractionCandidate,
    ExtractionReceipt,
    ExtractRequest,
    ExtractResponse,
)
from engram.memory_access import apply_write_eligibility
from engram.memory_context import ResolvedMemoryContext, resolve_memory_context
from engram.memory_kinds import get_enabled_memory_kinds
from engram.memory_scope import resolve_write_scope
from engram.models import ExtractionItemLink, ExtractionRun, MemoryItem
from engram.provider_clients import resolve_classification_provider
from engram.safety import has_secrets
from engram.usage import record_candidate_once, record_provider_call

router = APIRouter()


@router.post("/extract", response_model=ExtractResponse)
async def extract(
    req: ExtractRequest,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    caller: Principal = Depends(WRITE_SCOPE),  # noqa: B008
    memory_context: ResolvedMemoryContext = Depends(resolve_memory_context),  # noqa: B008
) -> ExtractResponse:
    """Extract propositions. Only write_proposed persists receipts and memory items."""
    await session.execute(
        text("SELECT set_config('app.extraction_admin', :admin, true)"),
        {
            "admin": "true" if caller.has_scope("admin") else "false",
        },
    )
    scope = await resolve_write_scope(
        session,
        memory_context=memory_context,
        caller_has_admin_scope=caller.has_scope("admin"),
        requested_visibility=req.visibility,
        requested_workspace=req.workspace,
    )
    tenant_id, principal_id = memory_context.tenant_id, memory_context.principal_id
    # Reject the complete batch before submission. Metadata can also contain credentials.
    if has_secrets(req.model_dump_json()) or any(
        has_secrets(value)
        for message in req.messages
        for value in message.model_dump().values()
        if isinstance(value, str)
    ):
        raise HTTPException(422, "extraction input contains secrets/credentials")
    for message in req.messages:
        if message.source_uri is None:
            continue
        try:
            prefix, item_id = message.source_uri.rsplit("/", 1)
            if prefix != "engram://items":
                raise ValueError
            source_id = UUID(item_id)
        except ValueError as exc:
            raise HTTPException(422, "unsupported extraction source reference") from exc
        source = await session.scalar(
            apply_write_eligibility(
                select(MemoryItem).where(
                    MemoryItem.id == source_id,
                    MemoryItem.tenant_id == tenant_id,
                    MemoryItem.workspace_id == scope.workspace_id,
                ),
                memory_context,
            )
        )
        if source is None:
            raise HTTPException(404, "extraction source reference not found")

    request_hash = digest(
        {
            "request": req.model_dump(mode="json"),
            "workspace_id": str(scope.workspace_id) if scope.workspace_id else None,
            "visibility": scope.visibility,
            "profile_revision": str(memory_context.memory_profile_revision_id),
        }
    )
    if req.mode == "write_proposed":
        lock_identity = digest(
            [
                str(tenant_id),
                str(principal_id),
                str(scope.workspace_id),
                req.idempotency_key,
            ]
        )
        # Transaction locks serialize provider execution and writes for one retry identity.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {
                "key": int(lock_identity[-16:], 16) - (1 << 63),
            },
        )
        existing = await session.scalar(
            select(ExtractionRun).where(
                ExtractionRun.tenant_id == tenant_id,
                ExtractionRun.principal_id == principal_id,
                ExtractionRun.workspace_id == scope.workspace_id,
                ExtractionRun.idempotency_key == req.idempotency_key,
            )
        )
        if existing is not None:
            if existing.request_hash != request_hash:
                raise HTTPException(
                    409, "idempotency key already used for different input or scope"
                )
            return ExtractResponse(
                receipt=ExtractionReceipt.model_validate(existing.receipt),
                receipt_hash=existing.receipt_hash,
            )

    kinds = [k.name for k in await get_enabled_memory_kinds(session, tenant_id)]
    provider_config = resolve_classification_provider()
    started = time.monotonic()
    provider = None
    failure_type = None
    try:
        provider = await extract_messages(req.messages, kinds)
    except Exception as exc:
        failure_type = type(exc).__name__
        raise HTTPException(503, "extraction provider unavailable or invalid output") from exc
    finally:
        configured = provider_config.provider_adapter == "openai" and bool(provider_config.api_key)
        await record_provider_call(
            tenant_id=tenant_id,
            principal_id=principal_id,
            workspace_id=scope.workspace_id,
            operation="extraction",
            status="succeeded" if provider else ("failed" if configured else "disabled"),
            usage_class="request",
            external_call_attempted=configured,
            provider_adapter=provider_config.provider_adapter,
            provider_host=provider_config.sanitized_provider_host,
            model=provider_config.model,
            input_count=len(req.messages),
            input_bytes=len(req.model_dump_json().encode()),
            prompt_tokens=provider.input_tokens if provider else None,
            completion_tokens=provider.output_tokens if provider else None,
            reported_cost_usd=provider.provider_cost_usd if provider else None,
            latency_ms=round((time.monotonic() - started) * 1000),
            metadata={"error_type": failure_type} if failure_type else None,
        )

    messages = [
        EvidenceMessage(
            message_id=m.message_id,
            role=m.role,
            input_hash=text_digest(m.content),
            character_count=len(m.content),
            created_at=m.created_at,
            tool_name=m.tool_name,
            source_uri=m.source_uri,
        )
        for m in req.messages
    ]
    root = digest(
        {
            "tenant_id": str(tenant_id),
            "workspace_id": str(scope.workspace_id),
            "messages": [m.model_dump(mode="json") for m in messages],
        }
    )
    run_id = uuid4()
    candidates: list[ExtractionCandidate] = []
    _, principal_type = await _resolve_principal(session, tenant_id)
    seen: set[str] = set()
    for proposition in provider.output.candidates:
        values: dict[str, Any] = proposition.model_dump()
        outcome = "preview"
        reason = None
        try:
            grounded = preserve_context_spans(proposition, req.messages)
            grounded = grounded.model_copy(
                update={
                    "source_cues": ground_source_cues(grounded, req.messages),
                }
            )
            role, mode, tool = validate_evidence(grounded, req.messages)
            values["assertion_mode"] = mode
            values["evidence"] = [span.model_dump() for span in grounded.evidence]
            values["source_cues"] = [cue.model_dump() for cue in grounded.source_cues]
        except ValueError:
            role, tool = "unknown", None
            values["assertion_mode"] = "unknown"
            outcome, reason = "rejected", "invalid_evidence"
        if has_secrets(proposition.model_dump_json()):
            # Do not persist or return a provider-generated credential.
            values.update(
                content="[rejected unsafe output]",
                evidence=[{"message_id": req.messages[0].message_id, "start": 0, "end": 1}],
                source_cues=[],
                subject=None,
                suggested_kind="fact",
                suggested_wing=None,
                suggested_room=None,
            )
            outcome, reason = "rejected", "unsafe_output"
        elif proposition.suggested_kind not in kinds:
            outcome, reason = "rejected", "unsupported_kind"
        elif "no_memory" in proposition.reason_codes:
            outcome, reason = "abstained", "no_memory"
        elif "unsafe" in proposition.reason_codes:
            outcome, reason = "rejected", "unsafe_candidate"
        elif proposition.content in seen:
            outcome, reason = "abstained", "duplicate_candidate"
        elif outcome != "rejected" and (
            proposition.retention_disposition != "retain" or proposition.retention_confidence < 0.65
        ):
            outcome = "volatile_recommended"
            reason = (
                "low_retention_confidence"
                if proposition.retention_disposition == "retain"
                else proposition.retention_disposition
            )
        seen.add(proposition.content)
        candidate = ExtractionCandidate.model_validate(
            {
                **values,
                "candidate_id": uuid4(),
                "content_hash": content_hash(canonicalize(values["content"])),
                "asserting_role": role,
                "asserting_tool": tool,
                "evidence_root": root,
                "outcome": outcome,
                "outcome_reason": reason,
            }
        )
        if req.mode == "write_proposed" and outcome == "preview":
            try:
                # Each candidate has a savepoint. Receipt and all successful writes commit together.
                async with session.begin_nested():
                    result = await _remember_impl(
                        RememberRequest(
                            content=candidate.content,
                            kind=candidate.suggested_kind,
                            wing=candidate.suggested_wing,
                            room=candidate.suggested_room,
                            workspace=scope.workspace_slug,
                            visibility=scope.visibility,
                            source_type=req.source_type,
                        ),
                        session,
                        correlation_id=run_id,
                        outcome_ctx={},
                        tenant_id=tenant_id,
                        principal_id=principal_id,
                        principal_type=principal_type,
                        attempt_id=uuid4(),
                        caller_has_admin_scope=caller.has_scope("admin"),
                        memory_context=memory_context,
                        extraction_transaction=True,
                    )
                candidate.outcome = "deduped" if result.status == "deduped" else "written"
                candidate.memory_item_id = result.id
                candidate.ingest_id = result.ingest_id
            except HTTPException:
                candidate.outcome, candidate.outcome_reason = "rejected", "write_boundary_rejected"
            except Exception:
                candidate.outcome, candidate.outcome_reason = "error", "write_failed"
        candidates.append(candidate)

    receipt = ExtractionReceipt(
        run_id=run_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        workspace_id=scope.workspace_id,
        memory_profile_revision_id=memory_context.memory_profile_revision_id,
        source_type=req.source_type,
        visibility=scope.visibility,
        mode=req.mode,
        input_hash=request_hash,
        evidence_root=root,
        messages=messages,
        provider=provider.provider,
        model=provider.model,
        provider_model=provider.provider_model,
        input_tokens=provider.input_tokens,
        output_tokens=provider.output_tokens,
        provider_cost_usd=provider.provider_cost_usd,
        latency_ms=provider.latency_ms,
        reason_codes=list(provider.output.reason_codes),
        candidates=candidates,
    )
    payload = receipt.model_dump(mode="json")
    response = ExtractResponse(receipt=receipt, receipt_hash=digest(payload))
    if req.mode == "write_proposed":
        session.add(
            ExtractionRun(
                id=run_id,
                tenant_id=tenant_id,
                principal_id=principal_id,
                workspace_id=scope.workspace_id,
                idempotency_key=req.idempotency_key,
                request_hash=request_hash,
                receipt=payload,
                receipt_hash=response.receipt_hash,
            )
        )
        await session.flush()
        for candidate in candidates:
            if candidate.memory_item_id is not None:
                session.add(
                    ExtractionItemLink(
                        run_id=run_id,
                        candidate_id=candidate.candidate_id,
                        tenant_id=tenant_id,
                        principal_id=principal_id,
                        workspace_id=scope.workspace_id,
                        memory_item_id=candidate.memory_item_id,
                        ingest_id=candidate.ingest_id,
                    )
                )
        await session.commit()
        for candidate in candidates:
            if candidate.ingest_id is not None:
                await record_candidate_once(
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    workspace_id=scope.workspace_id,
                    correlation_id=run_id,
                    ingest_id=candidate.ingest_id,
                    candidate_utf8_bytes=len(candidate.content.encode()),
                    source_type=req.source_type,
                )
    return response


@router.get("/extract/{run_id}", response_model=ExtractResponse)
async def get_extraction(
    run_id: UUID,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    caller: Principal = Depends(READ_SCOPE),  # noqa: B008
    memory_context: ResolvedMemoryContext = Depends(resolve_memory_context),  # noqa: B008
) -> ExtractResponse:
    """Read a stored receipt under the current principal and profile boundary."""
    await session.execute(
        text("SELECT set_config('app.extraction_admin', :admin, true)"),
        {
            "admin": "true" if caller.has_scope("admin") else "false",
        },
    )
    run = await session.scalar(
        select(ExtractionRun).where(
            ExtractionRun.id == run_id,
            ExtractionRun.tenant_id == memory_context.tenant_id,
            ExtractionRun.principal_id == memory_context.principal_id,
        )
    )
    if run is None or not memory_context.allows_workspace_read(run.workspace_id):
        raise HTTPException(404, "extraction receipt not found")
    if run.workspace_id is not None:
        from engram.auth import check_workspace_membership

        if not caller.has_scope("admin") and not await check_workspace_membership(
            session,
            principal_id=str(run.principal_id),
            workspace_id=str(run.workspace_id),
        ):
            raise HTTPException(404, "extraction receipt not found")
    receipt = ExtractionReceipt.model_validate(run.receipt)
    if memory_context.is_profile_bound and (
        (receipt.visibility == "private" and not memory_context.include_private)
        or (receipt.visibility == "tenant" and not memory_context.include_tenant)
        or (receipt.visibility == "public" and not memory_context.include_public)
    ):
        raise HTTPException(404, "extraction receipt not found")
    if digest(run.receipt) != run.receipt_hash:
        raise HTTPException(409, "extraction receipt integrity failure")
    return ExtractResponse(receipt=receipt, receipt_hash=run.receipt_hash)
