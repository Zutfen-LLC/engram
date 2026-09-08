"""Fail-open dark writer for authoritative semantic Context Receipts.

The caller supplies the completed semantic evaluation that the serving engine
already produced.  This module never evaluates retrieval, admission,
relationships, packing, or embeddings.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from engram.context_receipts import store_context_receipt, verify_context_receipt_record
from engram.db import apply_rls_context, async_session_factory
from engram.memory_context import ResolvedMemoryContext
from engram.recall_profiles import RECALL_PROFILE_CONTRACT_VERSION
from engram.semantic_context_manifest import (
    SemanticManifestDecisionContextV1,
    build_semantic_context_manifest_v1,
)

logger = logging.getLogger("engram.semantic_context_receipt_dark_write")


def _uuid_string(value: Any, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    parsed = UUID(str(value))
    if str(parsed) != str(value):
        raise ValueError("noncanonical UUID evidence")
    return str(parsed)


async def _persist_manifest(
    *,
    manifest: Any,
    recall_log_id: UUID,
    memory_context: ResolvedMemoryContext,
) -> None:
    """Persist, reload, and verify one receipt on an isolated session."""
    async with async_session_factory() as session:
        await apply_rls_context(
            session,
            tenant_id=memory_context.tenant_id,
            principal_id=memory_context.principal_id,
        )
        stored = await store_context_receipt(
            session,
            tenant_id=memory_context.tenant_id,
            principal_id=memory_context.principal_id,
            recall_log_id=recall_log_id,
            manifest=manifest,
        )
        await session.flush()
        await session.refresh(stored.receipt)
        verify_context_receipt_record(stored.receipt)
        await session.commit()


async def write_semantic_context_receipt_best_effort(
    *,
    raw_result: Mapping[str, Any],
    memory_context: ResolvedMemoryContext,
    query: str,
    workspace_supplied: bool,
    requested_byte_budget: int | None,
    requested_token_budget: int | None,
    requested_item_budget: int | None,
) -> None:
    """Persist one semantic receipt after the response and recall log exist.

    All ordinary failures are swallowed.  Logs contain only bounded IDs and
    exception types.  The request session is deliberately never used.
    """
    from engram.config import settings

    if not settings.semantic_context_receipt_dark_write_enabled:
        return
    try:
        evaluation = raw_result["_semantic_evaluation"]
        recall_log_id = UUID(str(raw_result["recall_log_id"]))
        workspace_id = _uuid_string(raw_result.get("workspace_id"), nullable=True)
        profile = evaluation.profile
        profile_id = memory_context.memory_profile_id
        profile_revision_id = memory_context.memory_profile_revision_id
        manifest = build_semantic_context_manifest_v1(
            items=evaluation.items,
            working_set=evaluation.working_set,
            item_count=evaluation.item_count,
            byte_count=evaluation.byte_count,
            candidate_count=evaluation.candidate_count,
            omitted_count=int(raw_result["omitted_count"]),
            omitted_by_admission=evaluation.omitted_by_admission,
            expansion=evaluation.expansion,
            packing=evaluation.packing,
            message=raw_result.get("message"),
            query=query,
            context=SemanticManifestDecisionContextV1(
                tenant_id=str(memory_context.tenant_id),
                principal_id=str(memory_context.principal_id),
                workspace_id=workspace_id,
                memory_profile_id=str(profile_id) if profile_id else None,
                memory_profile_revision_id=(
                    str(profile_revision_id) if profile_revision_id else None
                ),
                memory_profile_version=memory_context.memory_profile_version,
                workspace_supplied=workspace_supplied,
                requested_byte_budget=requested_byte_budget,
                requested_token_budget=requested_token_budget,
                requested_item_budget=requested_item_budget,
                effective_byte_budget=raw_result["effective_byte_budget"],
                effective_token_budget=raw_result["effective_token_budget"],
                effective_item_budget=raw_result["effective_item_budget"],
                recall_profile=profile.key,
                recall_profile_contract_version=RECALL_PROFILE_CONTRACT_VERSION,
                scoring_version=raw_result["scoring_version"],
                signals_version=raw_result.get("signals_version"),
                admission_policy=("recall-admission-v2" if profile.signals_enabled else None),
                relationship_relevance_version=(
                    evaluation.expansion.get("version") if evaluation.expansion else None
                ),
                packing_version=(evaluation.packing.get("version") if evaluation.packing else None),
                config_version=raw_result["config_version"],
            ),
        )
        await asyncio.wait_for(
            _persist_manifest(
                manifest=manifest,
                recall_log_id=recall_log_id,
                memory_context=memory_context,
            ),
            timeout=settings.context_receipt_dark_write_timeout_seconds,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - required fail-open boundary
        logger.warning(
            "semantic_context_receipt_dark_write_failed exc_type=%s tenant_id=%s principal_id=%s",
            type(exc).__name__,
            memory_context.tenant_id,
            memory_context.principal_id,
        )
