"""Shared helpers for context-receipt tests (ENG-CONTEXT-002A).

Builds valid ``ContextManifestV1`` instances and recall-log rows that agree with
each other, so the real-PostgreSQL and integrity test suites can exercise the
receipt repository without re-deriving the manifest contract inline.

This module is test-only and must not be imported by application code.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from engram.context_manifest import (
    MANIFEST_CONTRACT_VERSION,
    MEMORY_CONTEXT_VERSION,
    PACKET_RENDER_VERSION,
    ContextManifestEffectiveV1,
    ContextManifestRequestedV1,
    ContextManifestRequestInputV1,
    ContextManifestSubjectV1,
    ContextManifestV1,
    ContextManifestVersionsV1,
    build_startup_context_manifest_v1,
    compute_manifest_hash,
    sha256_digest,
)


def sha256_text(text: str) -> str:
    return sha256_digest(text.encode("utf-8"))


def make_item(
    *,
    ordinal: int,
    item_id: str,
    content: str = "content",
    kind: str = "fact",
    review_status: str = "active",
    authority: int = 10,
    visibility: str = "tenant",
    workspace_id: str | None = None,
    score: float | None = 0.5,
    reasons: list[str] | None = None,
    warnings: list[str] | None = None,
    pinned: bool = False,
    importance: float = 0.9,
    source_trust: float = 0.5,
    memory_confidence: float = 0.5,
    human_verified: bool = True,
    conflict_type: str | None = None,
    conflict_resolution_status: str | None = None,
) -> dict[str, Any]:
    """A served recall item dict in the loose shape ``RecallResponseLike`` uses."""
    return {
        "id": item_id,
        "kind": kind,
        "content": content,
        "review_status": review_status,
        "authority": authority,
        "visibility": visibility,
        "workspace_id": workspace_id,
        "score": score,
        "reasons": reasons if reasons is not None else [],
        "warnings": warnings if warnings is not None else [],
        "pinned": pinned,
        "importance": importance,
        "source_trust": source_trust,
        "memory_confidence": memory_confidence,
        "human_verified": human_verified,
        "conflict_type": conflict_type,
        "conflict_resolution_status": conflict_resolution_status,
    }


class ManifestResponse:
    """A minimal ``RecallResponseLike`` for the manifest builder."""

    def __init__(
        self,
        *,
        items: list[dict[str, Any]],
        working_set: str | None = None,
        pinned_omitted_count: int = 0,
        omitted_count: int = 0,
        message: str | None = None,
    ) -> None:
        self.items = items
        self.working_set = (
            working_set
            if working_set is not None
            else "\n".join(f"[{i['kind']}] {i['content']}" for i in items)
        )
        self.item_count = len(items)
        self.byte_count = sum(len(i["content"].encode("utf-8")) for i in items)
        self.pinned_omitted_count = pinned_omitted_count
        self.omitted_count = omitted_count
        self.message = message


def make_subject(
    *,
    tenant_id: str,
    principal_id: str,
    workspace_id: str | None = None,
    memory_profile_id: str | None = None,
    memory_profile_revision_id: str | None = None,
    memory_profile_version: int | None = None,
) -> ContextManifestSubjectV1:
    return ContextManifestSubjectV1(
        tenant_id=tenant_id,
        principal_id=principal_id,
        workspace_id=workspace_id,
        memory_context_version=MEMORY_CONTEXT_VERSION,
        memory_profile_id=memory_profile_id,
        memory_profile_revision_id=memory_profile_revision_id,
        memory_profile_version=memory_profile_version,
    )


def make_request_input(
    *,
    workspace_supplied: bool = False,
    byte_budget: int | None = 8192,
    token_budget: int | None = None,
    workspace_id: str | None = None,
) -> ContextManifestRequestInputV1:
    return ContextManifestRequestInputV1(
        requested=ContextManifestRequestedV1(
            workspace_supplied=workspace_supplied,
            byte_budget=byte_budget,
            token_budget=token_budget,
            item_budget=None,
        ),
        effective=ContextManifestEffectiveV1(
            workspace_id=workspace_id,
            byte_budget=byte_budget,
            token_budget=token_budget,
            item_budget=None,
        ),
        query_digest=None,
    )


def make_versions(
    *,
    scoring_version: str = "v1",
    config_version: str = "v1",
    candidate_strategy_version: str = "startup-candidates-v1",
) -> ContextManifestVersionsV1:
    return ContextManifestVersionsV1(
        scoring_version=scoring_version,
        config_version=config_version,
        candidate_strategy_version=candidate_strategy_version,
        manifest_contract_version=MANIFEST_CONTRACT_VERSION,
        packet_render_version=PACKET_RENDER_VERSION,
    )


def build_manifest(
    *,
    tenant_id: str,
    principal_id: str,
    item_ids: list[str],
    byte_budget: int | None = 8192,
    token_budget: int | None = None,
    workspace_id: str | None = None,
    workspace_supplied: bool = False,
    scoring_version: str = "v1",
    config_version: str = "v1",
    candidate_strategy_version: str = "startup-candidates-v1",
    memory_profile_id: str | None = None,
    memory_profile_revision_id: str | None = None,
    memory_profile_version: int | None = None,
    content: str = "served content",
    kind: str = "fact",
) -> ContextManifestV1:
    """Build a valid startup manifest over the given item IDs.

    All items share the same content/kind for simplicity; tests that need
    variation can call ``build_startup_context_manifest_v1`` directly.
    """
    items = [
        make_item(ordinal=i, item_id=item_id, content=content, kind=kind)
        for i, item_id in enumerate(item_ids)
    ]
    response = ManifestResponse(items=items)
    subject = make_subject(
        tenant_id=tenant_id,
        principal_id=principal_id,
        workspace_id=workspace_id,
        memory_profile_id=memory_profile_id,
        memory_profile_revision_id=memory_profile_revision_id,
        memory_profile_version=memory_profile_version,
    )
    request_input = make_request_input(
        workspace_supplied=workspace_supplied,
        byte_budget=byte_budget,
        token_budget=token_budget,
        workspace_id=workspace_id,
    )
    versions = make_versions(
        scoring_version=scoring_version,
        config_version=config_version,
        candidate_strategy_version=candidate_strategy_version,
    )
    return build_startup_context_manifest_v1(
        response=response,
        subject_context=subject,
        request_context=request_input,
        decision_versions=versions,
    )


def manifest_hash(manifest: ContextManifestV1) -> str:
    return compute_manifest_hash(manifest)


def packet_hash_for(working_set: str) -> str:
    return "sha256:" + hashlib.sha256(working_set.encode("utf-8")).hexdigest()


def fresh_uuids() -> dict[str, uuid.UUID]:
    return {
        "tenant_id": uuid.uuid4(),
        "principal_id": uuid.uuid4(),
        "recall_log_id": uuid.uuid4(),
        "other_tenant_id": uuid.uuid4(),
        "other_principal_id": uuid.uuid4(),
    }


def past_iso() -> str:
    return (datetime.now(UTC) - timedelta(hours=1)).isoformat()